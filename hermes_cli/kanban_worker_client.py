"""Dispatcher subprocess: submit one claim to its profile owner; never run an agent."""
# Spawned as a bare ``sys.executable -m``: in a PM install that interpreter carries no dependencies
# until hermes_bootstrap selects the committed environment, so it must be the first import.
try:
    import hermes_bootstrap
except ModuleNotFoundError as exc:
    if exc.name != "hermes_bootstrap" and not "hermes_bootstrap".startswith(f"{exc.name}."):
        raise

import asyncio
import contextlib
from dataclasses import dataclass
import os
from pathlib import Path
import sys
import time
from typing import Optional

# A worker that loses its owner (a dropped socket, a restart, `hermes update` draining it or a new
# owner still starting) waits for it instead of exiting: the condition is the host's, not the card's,
# and a failed exit here spends the card's failure budget and lets the owner end the turn. Asking
# again is safe: ``kanban.run`` is idempotent per attempt and ``prompt.receipt`` is a read.
#
# OPS-15 (18.11 20:01): ``OWNER_WAIT_S`` was 180s, which was SHORTER than a gateway restart's own
# after-turn wait can be — and while that wait is open the owner refuses this submitter's socket, so
# the submitter killed itself after exactly 183-187s and took its managed turn down with it. The gate
# has to outlast a legitimate restart, so the floor is 10 minutes (``OWNER_WAIT_FLOOR_S``) and it is
# tunable upward; a restart that takes longer than this is a real failure and must still be reported.
OWNER_WAIT_S = 600.0
OWNER_WAIT_FLOOR_S = 600.0
# Exit code of a submitter that outlived the owner: EX_UNAVAILABLE. It is NOT a card failure — the
# owner was absent, not the work wrong — so the crash sweep requeues without spending the budget.
OWNER_LOST_EXIT_CODE = 69
# Exit code of a submitter whose attempt the platform lost: the owner forgot the execution it
# admitted and no ``worker_result`` exists, so nothing says the work failed. Booking it as 1 charged
# the card's failure budget for a host watchdog kill or an OOM (23.11.2026, run 708: 42 minutes of
# work booked ``crashed``). 71 == BSD EX_OSERR; the crash sweep requeues without counting a failure.
WORKER_LOST_EXIT_CODE = 71
# Exit code of a submitter that refused to start because the checkout on disk cannot speak the
# running owner's ``worker.persist`` contract (``hermes_cli.code_skew_gate``). 76 == BSD
# EX_PROTOCOL; the crash sweep requeues without counting a failure (see
# ``hermes_cli.kanban_db.KANBAN_CODE_SKEW_EXIT_CODE``).
CODE_SKEW_EXIT_CODE = 76


def owner_wait_seconds():
    """The owner-silence budget: ``HERMES_KANBAN_OWNER_WAIT_SECONDS``, floored at 10 minutes."""
    try:
        configured = float(os.environ.get('HERMES_KANBAN_OWNER_WAIT_SECONDS') or 0)
    except ValueError:
        configured = 0.0
    return max(configured, OWNER_WAIT_FLOOR_S) if configured else OWNER_WAIT_S


_TRANSIENT_OWNER_STATES = frozenset({'draining', 'starting'})
# In-band owner refusals that leave the attempt intact: a draining owner admits nothing new, and a
# retired authority's successor holds the admission the next connection reads.
_TRANSIENT_RPC_REASONS = frozenset({'runtime_draining', 'runtime_retired'})
# Control-socket answers a restarting owner gives (an ``inaccessible`` owner's reason, from
# discovery or the ticket fetch): a reset or half-written answer, or a replaced owner refusing a
# stale instance. Unsafe nodes or peers, a refused authorization, a bad pointer or a failed start
# stay final.
_RESTART_CONTROL_REASONS = frozenset({'invalid_control_peer', 'incomplete_control_response',
                                      'invalid_control_response'})
# Past the board lock wait: the owner's session setup and receipt before and after the write.
ADMISSION_ANSWER_MARGIN_S = 60.0


def admission_answer_budget():
    """Seconds to wait for ``kanban.run``'s answer: the longest the owner's admission write can
    wait on the board lock (``write_txn`` retries ``BEGIN IMMEDIATE`` ``_BUSY_MAX_RETRIES`` times,
    each under the busy timeout) plus ``ADMISSION_ANSWER_MARGIN_S``."""
    from hermes_cli import kanban_db_connect
    lock_wait = (kanban_db_connect._BUSY_MAX_RETRIES + 1) * kanban_db_connect._resolve_busy_timeout_ms() / 1000
    return lock_wait + ADMISSION_ANSWER_MARGIN_S


@dataclass
class _Claim:
    """What the owner has said about this attempt, carried across connections."""
    answered: float  # monotonic time of the owner's last answer
    session_id: Optional[str] = None
    admission_id: Optional[str] = None

    def silent_for(self):
        return time.monotonic() - self.answered


async def run(params, board_db):
    from agent.retry_utils import jittered_backoff
    from hermes_cli.gateway_client import connect_gateway, GatewayClientError, outside_restart_wait
    from websockets.exceptions import WebSocketException
    if os.environ.get('HERMES_TUI_GATEWAY_URL'):
        raise GatewayClientError('Kanban requires the assigned local profile owner')
    # A lost socket is not a lost attempt, and exiting here would let ``_watch_submitter`` end the
    # owner's in-flight turn, so the next connection resumes whatever the last one reached. The
    # wait is bounded from the owner's last answer: an owner that keeps answering keeps the
    # worker, and one that stays silent past ``owner_wait_seconds()`` (>=10 min) fails the worker
    # honestly — with a checkpoint on the card and an exit code the sweep does not charge.
    claim = _Claim(answered=time.monotonic())
    owner_wait = owner_wait_seconds()
    failures = 0
    while True:
        seen = claim.answered
        try:
            # Never into a restart's after-turn wait: it cut this socket so the turn's submitter
            # waits for the NEXT owner, and a worker polling its receipt inside the wait keeps the
            # turn it waits on alive, holding `hermes update` until restart_after_turn_timeout.
            with outside_restart_wait():
                async with connect_gateway() as client:
                    # kanban.run subscribes this socket to the card's session, but only receipts
                    # matter here: discard the turn's events, or they fill the client's bounded
                    # queue and its reader stops, with the next receipt behind it.
                    drain = asyncio.create_task(_discard(client.events))
                    try:
                        receipt = await _await_receipt(client, params, board_db, claim)
                    finally:
                        drain.cancel()
        # A restart also surfaces as a refused or reset dial, a failed handshake, or an answer
        # that never came.
        except (GatewayClientError, WebSocketException, OSError, TimeoutError) as exc:
            if not _restartable(exc):
                raise
            if claim.silent_for() >= owner_wait:
                # WebSocket errors can embed remote bodies; only our own messages are quoted.
                status = getattr(getattr(exc, 'response', None), 'status_code', None)
                detail = exc if isinstance(exc, GatewayClientError) else type(exc).__name__ + (
                    f' (HTTP {status})' if status else '')
                _checkpoint_owner_lost(params, board_db, claim, owner_wait, detail)
                print(f'Kanban owner lost for {claim.silent_for():.0f}s '
                      f'(budget {owner_wait:.0f}s): {detail}', file=sys.stderr)
                # EX_UNAVAILABLE, not 1: the owner was gone, the card's work was not wrong. The
                # crash sweep sees this code and requeues without spending the failure budget.
                return OWNER_LOST_EXIT_CODE
        else:
            return _exit_code(receipt, params, board_db)
        failures = 1 if claim.answered != seen else failures + 1
        # Jittered (1-2 s first, up to 16 s): workers of one dead owner do not all redial, and so
        # start a replacement, in the same instant.
        await asyncio.sleep(jittered_backoff(failures, base_delay=1.0, max_delay=8.0, jitter_ratio=1.0))


def _checkpoint_worker_lost(params, board_db, cause) -> None:
    """Leave a durable note that this attempt stopped because the platform lost its execution.

    A run with no settlement and no ``worker_result`` cannot say its work failed — an operator
    reading the card must see that the platform lost the attempt, not that the card was wrong. The
    note carries the cause the client already read, so the next attempt's worker context explains
    what happened without re-diagnosing. Best-effort: a note that cannot be written must never
    replace the exit code the sweep depends on.
    """
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_db_connect import connect_closing
        note = {
            'kind': 'worker_lost',
            'task_id': params['task_id'],
            'run_id': params['run_id'],
            'claim_lock': params['claim_lock'],
            'detail': str(cause or '')[:300],
        }
        with connect_closing(Path(board_db)) as conn, kb.write_txn(conn):
            kb._append_event(conn, params['task_id'], 'checkpoint', note, run_id=params['run_id'])
    except Exception as exc:  # never mask the exit code the crash sweep reads
        print(f'Kanban worker-lost checkpoint not written: {type(exc).__name__}: {exc}',
              file=sys.stderr)


def _checkpoint_owner_lost(params, board_db, claim, owner_wait, detail) -> None:
    """Leave a durable note on the card that this attempt stopped because its owner vanished.

    The card must not read this as "my work failed". The note names the owner-silence budget and how
    long the owner was actually silent, so a human (or the next attempt's worker context) can tell a
    long restart from a wedged gateway. Best-effort: a note that cannot be written must never
    replace the exit code the sweep depends on.
    """
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_db_connect import connect_closing
        note = {
            'kind': 'owner_lost',
            'owner_silent_s': round(claim.silent_for(), 1),
            'budget_s': round(owner_wait, 1),
            'session_id': claim.session_id,
            'admission_id': claim.admission_id,
            'detail': str(detail)[:300],
        }
        with connect_closing(Path(board_db)) as conn:
            kb._append_event(conn, params['task_id'], 'checkpoint', note, run_id=params['run_id'])
            conn.commit()
    except Exception as exc:  # never mask the exit code the crash sweep reads
        print(f'Kanban owner-lost checkpoint not written: {type(exc).__name__}: {exc}', file=sys.stderr)


def _restartable(exc):
    """False when *exc* is the owner's own final answer; True for a restarting owner or a lost transport."""
    from hermes_cli.gateway_client import GatewayRPCError, GatewayUnavailableError
    if isinstance(exc, GatewayRPCError):
        return str(exc) in _TRANSIENT_RPC_REASONS
    if isinstance(exc, GatewayUnavailableError):
        return exc.state in _TRANSIENT_OWNER_STATES or exc.reason_code in _RESTART_CONTROL_REASONS
    return True


async def _discard(events):
    while True:
        await events.get()


async def _await_receipt(client, params, board_db, claim):
    """Submit the claim (or, on a later connection, re-read the admission it got) and return its
    settled receipt, recording each owner answer on *claim*."""
    if claim.admission_id is None:
        db = str(await asyncio.to_thread(Path(board_db).resolve))
        # The owner answers only after it checks the claim and writes its ``owner_admitted`` marker
        # in one board write transaction. The board (the cross-profile dispatch bus) can make that
        # wait out its busy timeout, so the 30 s default left the owner admitting an attempt whose
        # submitter had exited (the later-bound worker's submitter-liveness watcher ended it, and the
        # admission was left ``unknown``). The wait is still finite: once the marker commits,
        # ``enforce_max_runtime`` and TTL reclaim skip the run (``owner_reclaim_paused``), so an owner
        # that stalls after admitting is bounded by this wait and ``OWNER_WAIT_S``.
        accepted = await client.rpc('kanban.run', _timeout=admission_answer_budget(), **dict(params, db=db))
        receipt = accepted['receipt']
        claim.session_id, claim.admission_id = accepted['session_id'], receipt['admission_id']
        print(f'Session: {claim.session_id}', file=sys.stderr)
    else:
        receipt = await _receipt(client, claim)
    claim.answered = time.monotonic()
    while receipt['status'] not in {'terminal', 'unknown'}:
        await asyncio.sleep(.2)
        receipt = await _receipt(client, claim)
        claim.answered = time.monotonic()
    return receipt


def _saved_result(params, board_db):
    """This attempt's ``worker_result`` (claim_lock matched), or ``{}`` when there is none.

    The board is the primary source: the owner writes the result inside the attempt, so it is
    readable even when the admission never settled. An unreadable board is "no record", never an
    exception on the submitter's exit path.
    """
    from gateway.session_kanban import worker_result
    try:
        return worker_result(board_db, params) or {}
    except Exception:
        return {}


def _unknown_cause(params, board_db):
    """Best-effort "why" for an ``unknown`` receipt with no saved result, or ``''``.

    The receipt only says the admission did not settle; the card and the worker log still know
    what happened. Reads, in order: the run's latest explanatory event on the board, then the tail
    of the worker's own log for this task. Never raises — the refusal message must still print.
    """
    import sqlite3
    from contextlib import closing
    # The log scan below is the ONLY source when the board is unreadable or silent, so it must not
    # share a swallow-all with the board read. Every failure is appended as part of the answer
    # instead of being dropped: "the worker log is empty" must never be said about a log we did not
    # manage to read (the reviewer caught exactly that, 19.11.2026: an unimported marker raised
    # inside `any(...)` and `except Exception: pass` turned the whole branch into dead code).
    from hermes_cli.turn_exit import SESSION_PERSIST_MARKER
    parts = []
    try:
        from pathlib import Path
        uri = Path(board_db).resolve().as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            row = conn.execute(
                "SELECT kind, payload FROM task_events WHERE task_id=? AND run_id=? "
                "AND kind NOT IN ('worker_result', 'heartbeat', 'checkpoint') "
                "ORDER BY id DESC LIMIT 1", (params['task_id'], params['run_id'])).fetchone()
        if row:
            detail = ''
            try:
                import json
                payload = json.loads(row[1] or '{}')
                detail = str(payload.get('worker_output') or payload.get('error')
                             or payload.get('reason') or '')[:200]
            except (ValueError, TypeError):
                detail = ''
            parts.append(f'last board event `{row[0]}`' + (f': {detail}' if detail else ''))
    except (OSError, sqlite3.Error) as exc:
        parts.append(f'board unreadable: {exc}')
    try:
        from hermes_cli import kanban_db as kb
        import re
        log = kb.read_worker_log(params['task_id'], tail_bytes=4000,
                                 board=params.get('board'))
        if log is None:
            # ``read_worker_log`` says None for two different worlds — the file is missing, and the
            # read failed (its own ``except OSError``). Collapsing them to '' made the refusal claim
            # "worker log empty" about a log that exists and was never read (reviewer, 19.11.2026:
            # a chmod 0 log produced a false, checkable statement).
            try:
                path = kb.worker_log_path(params['task_id'], board=params.get('board'))
                exists = path.exists()
            except OSError as exc:
                parts.append(f'worker log unreadable: {exc}')
                return '; '.join(parts)
            parts.append(f'worker log unreadable: {path}' if exists else 'worker log empty')
            return '; '.join(parts)
        # The worker's own last words before it gave up, minus CLI chrome.
        lines = [ln.strip() for ln in log.splitlines() if ln.strip()]
        for line in reversed(lines):
            if any(marker in line for marker in (SESSION_PERSIST_MARKER, 'Нет ответа', 'No reply',
                                                 'не смог сохранить', "couldn't save")):
                return '; '.join(parts + [f'worker log: {line[-300:]}'])
        if lines:
            last = re.sub(r"\s+", " ", lines[-1])[-200:]
            parts.append(f'worker log ends: {last}')
        else:
            parts.append('worker log empty')
    except (OSError, sqlite3.Error) as exc:
        parts.append(f'worker log unreadable: {exc}')
    return '; '.join(parts)


def _exit_code(receipt, params, board_db):
    from hermes_cli.gateway_client import GatewayClientError
    # Primary source FIRST: ``worker_result`` is written inside the attempt, and it outranks the
    # admission's settlement. ``unknown`` means the admission did not settle — it does NOT mean the
    # work did not happen. Reading the receipt first used to erase a saved failure and replace it
    # with "the board did not recognise the worker" (19.11.2026, run 102: the session failed to
    # persist, exit 1 and its message were on the board, and the next attempt reported only
    # `Kanban execution unknown`).
    result = _saved_result(params, board_db)
    # The attempt's reply ends the log, as a one-shot worker's did: crash diagnostics quote it.
    if result.get('last_output'):
        print(result['last_output'])
    if receipt['status'] == 'unknown' and not result:
        # No settlement AND no result: nothing on the board can say the work failed — the owner
        # forgot the execution (a host watchdog's SIGTERM, an OOM kill, a manual kill). That is a
        # PLATFORM loss, not a card failure: exiting 1 here spent the card's failure budget for it
        # (23.11.2026, run 708 — 42 minutes of work booked ``crashed``). Leave the durable checkpoint
        # and exit the platform-loss code the crash sweep requeues without charging.
        cause = _unknown_cause(params, board_db)
        _checkpoint_worker_lost(params, board_db, cause)
        print('Kanban execution unknown; retained by owner — the admission never settled and no '
              'worker_result was recorded for this attempt'
              # Never assert an unverified fact here: `_unknown_cause` reports what it actually saw
              # (a board event, a log line, or the read failure itself), so the absence of a cause
              # means both sources were consulted and silent — not that the log "is empty".
              + (f'. Cause: {cause}' if cause else
                 '. Cause unknown: neither the board nor the worker log could explain this attempt'),
              file=sys.stderr)
        return WORKER_LOST_EXIT_CODE
    return result.get('exit_code', 1)


async def _receipt(client, claim):
    return await client.rpc('prompt.receipt', session_id=claim.session_id, admission_id=claim.admission_id)


def _install_stop_handler():
    """Name the cause of an external stop from inside the worker, then exit.

    A dispatcher reclaim, an archive or a shutdown SIGTERMs this process. Without a handler that
    death is invisible: no trailer, no event, and the owner's side reads only ``EOFError`` where
    the frame stream ended. The handler reads the reason the stopper wrote BEFORE signalling
    (``hermes_cli.kanban_stop_reason``), appends a durable ``worker_stopping`` event to the card
    and prints the exit trailer, so the log and the board both say WHY.

    The card identity is captured HERE, at install time, not read from ``os.environ`` when the
    signal arrives: ``_submit()`` pops every ``HERMES_KANBAN_*`` key before the turn precisely so
    the owner never inherits this worker's task identity, so at signal time the environment is
    empty and an env-reading handler would always answer ``unknown``.

    Never raises and never blocks the stop: an unknown reason still exits, with the trailer, and
    the board then reads ``unknown`` — which is exactly a SIGKILL's meaning.
    """
    import signal

    context = _card_context()

    def _on_stop(signum, frame):  # noqa: ARG001 - signal handler signature
        reason, detail = "unknown", ""
        try:
            from hermes_cli import kanban_stop_reason as ksr
            payload = ksr.read_reason_for_workspace(context.get("workspace"),
                                                    run_id=context.get("run_id"))
            if payload:
                reason, detail = payload.get("reason", "unknown"), payload.get("detail", "")
            _record_stopping(reason, detail, signum, context=context)
        except BaseException:
            pass
        finally:
            try:
                from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER
                print(f'\n{KANBAN_WORKER_EXIT_TRAILER}1', file=sys.stderr, flush=True)
                print(f'KANBAN_WORKER_STOPPED reason={reason} detail={detail}', file=sys.stderr, flush=True)
            except BaseException:
                pass
            # A half-written frame on the socket must not hold the process open.
            os._exit(1)

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is not None:
            with contextlib.suppress(ValueError, OSError, RuntimeError):
                signal.signal(sig, _on_stop)


def _card_context() -> dict:
    """The card identity this process belongs to, read once (``None`` when not a Kanban worker).

    Captured before ``_submit()`` scrubs the environment (see ``_install_stop_handler``), and
    re-read after it: the bind path sets the same keys a second time, so either source works
    while only this one survives the scrub.
    """
    try:
        run_id = os.environ.get("HERMES_KANBAN_RUN_ID")
        return {
            "task_id": os.environ.get("HERMES_KANBAN_TASK"),
            "run_id": int(run_id) if run_id else None,
            "db": os.environ.get("HERMES_KANBAN_DB"),
            "workspace": os.environ.get("HERMES_KANBAN_WORKSPACE"),
        }
    except (TypeError, ValueError):
        return {}


def _record_stopping(reason, detail, signum, *, context=None):
    """Append the durable ``worker_stopping`` event for this attempt (best effort).

    Runs inside a signal handler, so the board write is bounded: a contended database must not
    hold a dying worker open past the stopper's SIGKILL grace (the reason is already on disk by
    then, which is what the trailer and ``record_worker_loss`` read). ``busy_timeout=0`` makes a
    locked board give up immediately instead of waiting the 120 s the normal connections allow —
    a lost event is recoverable from the file, a 45 s hang is not.
    """
    context = context if context is not None else _card_context()
    task_id, run_id, db_path = context.get("task_id"), context.get("run_id"), context.get("db")
    if not (task_id and run_id and db_path):
        return
    import sqlite3
    from hermes_cli import kanban_db as kb
    from pathlib import Path
    payload = {"reason": reason, "detail": str(detail or "")[:200], "signal": int(signum), "pid": os.getpid()}
    conn = sqlite3.connect(str(Path(db_path)), timeout=0.0, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=0")
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute('SELECT status, current_run_id FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None or row[1] != int(run_id):
            conn.execute("ROLLBACK")
            return  # the attempt already moved on: this note would describe a later run
        kb._append_event(conn, task_id, 'worker_stopping', payload, run_id=int(run_id))
        conn.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(BaseException):
            conn.execute("ROLLBACK")
    finally:
        conn.close()


def main():
    # The dead-worker sweep books this process's exit from the trailer in its log (it never
    # reaps us), so every exit path writes it — the same contract as a quiet one-shot worker.
    from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER
    _install_stop_handler()
    rc = 1
    try:
        rc = _submit()
    finally:
        print(f'\n{KANBAN_WORKER_EXIT_TRAILER}{int(rc)}', file=sys.stderr, flush=True)
    return rc


def _submit():
    try:
        params = {'board': os.environ['HERMES_KANBAN_BOARD'], 'task_id': os.environ['HERMES_KANBAN_TASK'],
            'run_id': int(os.environ['HERMES_KANBAN_RUN_ID']), 'claim_lock': os.environ['HERMES_KANBAN_CLAIM_LOCK']}
        board_db = os.environ['HERMES_KANBAN_DB']
        # Owner startup must never inherit this worker's task/tool/goal identity.
        for key in list(os.environ):
            # The busy timeout and the owner-silence budget are host tuning knobs, not task identity:
            # the answer budget reads the latter, so stripping it would silently pin every worker to
            # the 10-minute floor and quietly undo HERMES_KANBAN_OWNER_WAIT_SECONDS.
            if key.startswith('HERMES_KANBAN_') and key not in (
                    'HERMES_KANBAN_HOME', 'HERMES_KANBAN_BUSY_TIMEOUT_MS', 'HERMES_KANBAN_OWNER_WAIT_SECONDS'):
                os.environ.pop(key)
        for key in ('HERMES_SESSION_SOURCE', 'HERMES_TENANT', 'TERMINAL_CWD'):
            os.environ.pop(key, None)
        if (skew := _refuse_on_code_skew(params, board_db)) is not None:
            return skew
        return asyncio.run(run(params, board_db))
    except (KeyError, ValueError, OSError) as exc:
        print(f'Kanban owner refused: {exc}', file=sys.stderr)
        return 1


def _refuse_on_code_skew(params, board_db):
    """``CODE_SKEW_EXIT_CODE`` when this checkout cannot speak the running owner, else None.

    The gate is consulted BEFORE the first dial, so a split contract costs one refused spawn instead
    of a 60-second turn that dies inside ``usage.main`` (26.11.2026). A refusal here is the host's
    condition, not the card's: the exit code is separate, it spends no failure budget, and a
    checkpoint records both commits so the board explains itself without re-diagnosis.
    """
    from hermes_cli import code_skew_gate
    try:
        skew = code_skew_gate.check_code_skew()
    except Exception as exc:  # the gate must never be the reason a worker cannot start
        print(f'Kanban code-skew gate not conclusive ({type(exc).__name__}: {exc}); starting anyway',
              file=sys.stderr)
        return None
    if skew is None:
        return None
    print(f'Kanban worker refused to start: {skew.message}', file=sys.stderr)
    _checkpoint_code_skew(params, board_db, skew)
    return CODE_SKEW_EXIT_CODE


def _checkpoint_code_skew(params, board_db, skew) -> None:
    """Leave the refusal on the card (both commits + the fix). Best-effort, like its siblings."""
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_db_connect import connect_closing
        note = {
            'kind': 'code_skew',
            'disk_sha': skew.disk_sha,
            'gateway_sha': skew.gateway_sha,
            'gateway_pid': skew.gateway_pid,
            'problems': list(skew.problems),
            'message': skew.message[:500],
        }
        with connect_closing(Path(board_db)) as conn:
            kb._append_event(conn, params['task_id'], 'checkpoint', note, run_id=params['run_id'])
            conn.commit()
    except Exception as exc:  # never mask the exit code the crash sweep reads
        print(f'Kanban code-skew checkpoint not written: {type(exc).__name__}: {exc}', file=sys.stderr)


if __name__ == '__main__':
    raise SystemExit(main())
