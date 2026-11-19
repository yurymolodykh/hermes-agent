"""PERSIST-1: an ``outbox_full`` refusal must read as what it is, and must not lose a turn.

The managed worker's session store is a private, byte-capped retry journal
(``worker-outboxes/<execution>/pending.json``, ``RuntimeSessionStore(max_bytes=4 MiB)``). Hitting
that cap is neither a full disk nor another process holding ``state.db`` — yet every persistence
failure used to render the same ``explainer.persistence.default`` text ("the drive is out of room,
or another Hermes process is holding the database"), which sent the owner down the wrong path twice.

These tests pin three contracts:

1. ``classify_persistence_error`` gives the journal its own bucket, before the disk prose bucket,
   and never says "disk" for it.
2. The rendered explanation names the journal path and its ceiling, and carries no disk/lock advice.
3. The refusal is lossless: a rejected write leaves the journal byte-identical with an empty
   ``pending`` list, the failure is sticky for the rest of the run, and a re-opened store replays
   whatever was pending.
"""

import json

import pytest

from agent.error_surface import LAYER_GATEWAY, build_error_surface_from_result
from agent.runtime_session_store import RuntimeSessionStore, WorkerPersistenceError
from agent.turn_explainers import TurnExplainersMixin
from hermes_state_errors import PERSISTENCE_ERROR_CAUSES, classify_persistence_error


# ---------------------------------------------------------------------------
# 1. Classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("probe", [
    "outbox_full",
    WorkerPersistenceError("outbox_full"),
    "worker.persist refused: outbox_full",
])
def test_outbox_full_is_its_own_bucket_not_disk(probe):
    cause = classify_persistence_error(probe)
    assert cause == "outbox_full"
    assert cause in PERSISTENCE_ERROR_CAUSES


@pytest.mark.parametrize("probe", [
    "pending_receipt", "outbox_in_use", "outbox_scope_mismatch", "outbox_closed", "unsafe_outbox",
])
def test_journal_busy_refusals_are_retryable_not_damage(probe):
    assert classify_persistence_error(probe) == "outbox_busy"


@pytest.mark.parametrize("probe", [
    "[Errno 13] Permission denied: '/home/hermes/.hermes/profiles/p/state.db'",
    "permission_denied",
])
def test_permission_refusals_are_not_disk_full_advice(probe):
    assert classify_persistence_error(probe) == "permission_denied"


def test_disk_full_still_wins_for_a_real_enospc():
    """The new buckets must not swallow the genuine disk case."""
    assert classify_persistence_error("database or disk is full") == "disk"
    assert classify_persistence_error("database is locked") == "locked"


# ---------------------------------------------------------------------------
# 2. User-facing copy
# ---------------------------------------------------------------------------

JOURNAL = "/home/hermes/.hermes/profiles/02-hermes-platform/worker-outboxes/admission-worker-0bad/pending.json"
LIMIT = 4 * 1024 * 1024


def _explain(cause, **kwargs):
    return TurnExplainersMixin._format_turn_completion_explanation(
        "session_persistence_failed", cause, journal_path=JOURNAL, journal_limit=LIMIT, **kwargs)


def test_outbox_full_copy_names_the_journal_and_its_ceiling():
    out = _explain("outbox_full")
    assert out.strip()
    assert "outbox_full" in out
    assert "4.0 MiB" in out
    assert "worker-outboxes" in out and "pending.json" in out
    # The whole point: no wrong diagnosis.
    lower = out.lower()
    assert "full disk" not in lower
    assert "free some space" not in lower
    assert "out of room" not in lower
    # And no useless "restart everything" advice for a per-run buffer.
    assert "close other hermes" not in lower


def test_outbox_full_copy_is_not_read_back_as_a_disk_failure():
    """The rendered text feeds ``error_surface``; ``is_disk_full_error`` must not match it, or the
    desktop card would show the very "disk full" copy this change removes."""
    from hermes_state_errors import is_disk_full_error

    for cause in ("outbox_full", "outbox_busy", "permission_denied"):
        assert not is_disk_full_error(_explain(cause)), cause


def test_every_classifier_cause_has_explainer_copy():
    """Adding a bucket to ``PERSISTENCE_ERROR_CAUSES`` without its ``explainer.persistence.<cause>``
    copy silently degrades the user's message to the generic "disk or another Hermes" text — which
    is exactly how ``outbox_full`` sent an operator hunting a full disk."""
    from agent.turn_explainers import _PERSISTENCE_CAUSES
    from hermes_state_errors import PERSISTENCE_ERROR_CAUSES
    from agent.i18n_layers import parse_locale_file
    from pathlib import Path

    assert set(PERSISTENCE_ERROR_CAUSES) - {"unknown"} <= _PERSISTENCE_CAUSES
    catalog = parse_locale_file(Path(__file__).resolve().parents[2] / "locales" / "en.yaml")
    missing = [c for c in PERSISTENCE_ERROR_CAUSES
               if c != "unknown" and f"explainer.persistence.{c}" not in catalog]
    assert not missing, missing


def test_outbox_full_without_a_store_handle_never_names_state_db():
    """Cron suppression renders every cause variant with no store in hand; the fallback must still
    describe the journal, not point at the healthy database."""
    out = TurnExplainersMixin._format_turn_completion_explanation(
        "session_persistence_failed", "outbox_full")
    assert "worker-outboxes" in out and "pending.json" in out
    assert "state.db" not in out


@pytest.mark.parametrize("cause", ["outbox_full", "outbox_busy", "permission_denied"])
def test_new_causes_have_no_unformatted_placeholders(cause):
    out = _explain(cause)
    assert "{" not in out and "}" not in out


def test_outbox_full_surfaces_as_a_gateway_layer_card():
    """A storage refusal is not a provider verdict: the client must not offer "switch provider"."""
    surface = build_error_surface_from_result({
        "error": _explain("outbox_full"),
        "failure_reason": "session_persistence_failed:outbox_full",
        "failure_retryable": True,
    })
    assert surface["layer"] == LAYER_GATEWAY
    assert surface["code"] == "session_persistence_failed:outbox_full"


# ---------------------------------------------------------------------------
# 3. Loss behaviour
# ---------------------------------------------------------------------------


def _oversized_store(tmp_path, journal_bytes=600):
    """A store whose cap is a few hundred bytes, so one real frame overflows it."""
    def rpc(method, **params):  # pragma: no cover - never reached on the refusal path
        raise AssertionError("a refused write must not reach the owner")

    return RuntimeSessionStore(rpc, {"session_id": "s", "execution_id": "w", "epoch": 1},
                               tmp_path / "private", max_bytes=journal_bytes)


def test_outbox_full_leaves_the_journal_intact_and_empty(tmp_path):
    store = _oversized_store(tmp_path)
    try:
        before = store.path.read_bytes()
        assert json.loads(before)["pending"] == []
        with pytest.raises(WorkerPersistenceError, match="outbox_full"):
            store.append_messages_batch("s", [{"role": "user", "content": "x" * 2000}])
        # Nothing was appended, nothing was truncated, nothing was half-written.
        assert store.path.read_bytes() == before
        assert store.journal["pending"] == []
        assert store.failure == "outbox_full"
    finally:
        store._outbox_owner.close()


def test_outbox_full_is_sticky_for_the_rest_of_the_run(tmp_path):
    """One refused frame must not be retried into a partial journal: every later write fails the
    same way, so the caller's transcript keeps its own copy instead of diverging."""
    store = _oversized_store(tmp_path)
    try:
        with pytest.raises(WorkerPersistenceError, match="outbox_full"):
            store.append_messages_batch("s", [{"role": "user", "content": "x" * 2000}])
        with pytest.raises(WorkerPersistenceError, match="outbox_full"):
            store.append_messages_batch("s", [{"role": "user", "content": "small"}])
        with pytest.raises(WorkerPersistenceError, match="outbox_full"):
            store.flush_token_counts()
        assert json.loads(store.path.read_bytes())["pending"] == []
    finally:
        store._outbox_owner.close()


def test_pending_frame_survives_a_crash_and_replays_on_reopen(tmp_path):
    """The other half of "no silent loss": a frame that WAS written before the crash is retried by
    the next store instance, not dropped."""
    scope = {"session_id": "s", "execution_id": "w", "epoch": 1}

    def offline(method, **params):
        raise TimeoutError("owner gone")

    store = RuntimeSessionStore(offline, scope, tmp_path / "private", max_bytes=64 * 1024)
    try:
        with pytest.raises(TimeoutError):
            store.append_messages_batch("s", [{"role": "user", "content": "keep me"}])
        on_disk = json.loads(store.path.read_text())
        assert on_disk["pending"][0]["payload"]["messages"][0]["content"] == "keep me"
    finally:
        store._outbox_owner.close()

    seen = []

    def commit(method, **params):
        seen.append(params)
        return {"count": 1, "annotations": [{"_row_id": 7}]}

    reopened = RuntimeSessionStore(commit, scope, tmp_path / "private", max_bytes=64 * 1024)
    try:
        assert json.loads(reopened.path.read_text())["pending"] == []
        assert seen and seen[0]["payload"]["messages"][0]["content"] == "keep me"
    finally:
        reopened.close()
