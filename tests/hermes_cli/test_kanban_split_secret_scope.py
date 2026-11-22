"""The auto-split phase must work on the FIRST tick after a gateway restart.

Owner's defect (rollout probe Б): right after ``hermes gateway restart`` the
auto-split of a burned card never happened and "fixed itself" minutes later. The
tick called ``run_split_phase`` with no profile secret scope, so under
``gateway.multiplex_profiles`` the decomposer's credential read raised
``UnscopedSecretError``; ``agent.auxiliary_client`` silently retries that one
error against ``os.environ``, where a profile-scoped key never lives. Once an
``kanban_decomposer`` aux client existed in ``_client_cache`` the key was never
re-read — hence "cold process = broken, warm process = fine".

Pinned here:

1. ``set_multiplex_active(True)`` + a COLD aux client cache: one dispatcher tick
   splits the card into children (red on the pre-fix code, which reported
   ``LLM error: UnscopedSecretError`` and merely continued the card);
2. the guard: a simulated restart (cache cleared) leaves the FIRST split after it
   working — no warm-up call needed;
3. the failure CLASS stays visible: a missing scope is ``secret-scope: …`` in the
   journal and ``failure_class="secret_scope"`` on the card's state, never a bare
   "the provider failed".

No network: the SDK client is stubbed at ``_create_openai_client``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_auto_continue as kac
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_decompose as decomp
from hermes_cli import kanban_specify as specify

_SPLIT_PAYLOAD = json.dumps({
    "fanout": True,
    "rationale": "the work is bigger than one run",
    "tasks": [
        {"title": "part A", "body": "do A", "assignee": "engineer", "parents": []},
        {"title": "part B", "body": "do B", "assignee": "engineer", "parents": []},
    ],
})

_KEY_ENV = "T4911_LETOVO_API_KEY"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Throwaway HERMES_HOME holding the provider key ONLY in the profile .env.

    That is the multiplexed-gateway shape: the key is profile-scoped, so it is
    invisible to ``os.environ`` and reachable only through the secret scope.
    """
    root = tmp_path / ".hermes"
    root.mkdir()
    (root / ".env").write_text(f"{_KEY_ENV}=profile-scoped-key\n", encoding="utf-8")
    (root / "config.yaml").write_text(
        "model:\n"
        "  provider: letovo\n"
        "  model: probe-model\n"
        "providers:\n"
        "  letovo:\n"
        "    base_url: https://llm.example.invalid/openai\n"
        f"    key_env: {_KEY_ENV}\n"
        "auxiliary:\n"
        "  kanban_decomposer:\n"
        "    provider: auto\n"
        "    model: ''\n"
        "    timeout: 5\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv(_KEY_ENV, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return root


@pytest.fixture
def conn(home: Path):
    connection = kbc.connect()
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def multiplexed(home: Path):
    """Process-level multiplex flag, as the gateway sets it at boot; cleared after."""
    from agent.secret_scope import set_multiplex_active

    set_multiplex_active(True)
    try:
        yield
    finally:
        set_multiplex_active(False)


@pytest.fixture
def cold_aux_cache():
    """Cold aux-client cache: nothing built yet in this "process" — and again on restart."""
    from agent import auxiliary_client as ac

    def _freeze() -> None:
        ac.shutdown_cached_clients()

    _freeze()
    yield _freeze
    _freeze()


@pytest.fixture
def stub_llm():
    """Stub the SDK client so the decomposer's reply never leaves the process."""
    from agent import auxiliary_client as ac

    response = SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=_SPLIT_PAYLOAD),
            finish_reason="stop",
        )],
        model="probe-model",
    )
    client = MagicMock()
    client.chat.completions.create = MagicMock(return_value=response)
    with patch.object(ac, "_create_openai_client", return_value=client) as create:
        yield create


@pytest.fixture
def roster():
    """Pretend the profiles the decomposer routes into exist."""
    fake_profiles = [
        SimpleNamespace(name=name, is_default=(idx == 0), description=f"desc {name}",
                        description_auto=False, model="m", provider="p", skill_count=1)
        for idx, name in enumerate(["orchestrator", "engineer"])
    ]
    patches = [
        patch("hermes_cli.profiles.list_profiles", return_value=fake_profiles),
        patch("hermes_cli.profiles.profile_exists", side_effect=lambda n: n in {"orchestrator", "engineer"}),
        patch("hermes_cli.profiles.get_active_profile_name", return_value="orchestrator"),
    ]
    for item in patches:
        item.start()
    try:
        yield
    finally:
        for item in patches:
            item.stop()


def _split_pending_card(conn, monkeypatch, *, title: str = "big job") -> str:
    """A card that burned its budget twice in generation 0: its split is owed."""
    monkeypatch.setattr(
        kac, "resolve_settings",
        lambda: kac.AutoContinueSettings(enabled=True, max_splits=2),
    )
    tid = kb.create_task(conn, title=title, assignee="engineer")
    conn.execute("UPDATE tasks SET workspace_path = ? WHERE id = ?", (f"/tmp/ws/{tid}", tid))
    conn.commit()

    def _burn(attempt: int) -> None:
        kb.claim_task(conn, tid, claimer=f"{kb._claimer_id().split(':')[0]}:worker")
        kbd._set_worker_pid(conn, tid, 970_000 + attempt)
        kbd._record_task_failure(
            conn, tid, error=f"Iteration budget exhausted ({attempt}/1)",
            outcome="timed_out", release_claim=True, end_run=True,
        )

    _burn(1)   # reaches failure_limit, below it
    _burn(2)   # -> auto_continued
    _burn(3)   # -> auto_split_queued
    assert kac.read_state(conn, tid).split_pending is True
    return tid


def _children(conn, tid: str) -> list[str]:
    """Cards the split produced for ``tid``.

    The decomposed graph links the ROOT under every child (the root waits for
    its whole graph), so the root's lineage view of its children is
    ``parent_ids``; ``child_ids`` would list cards that depend on the root.
    """
    return kb.parent_ids(conn, tid)


# ---------------------------------------------------------------------------
# 1. cold cache + multiplex: one tick splits the card
# ---------------------------------------------------------------------------

def test_cold_cache_split_under_multiplex_produces_children(
    conn, monkeypatch, multiplexed, cold_aux_cache, stub_llm, roster,
):
    """The pre-fix code failed here with ``LLM error: UnscopedSecretError``."""
    tid = _split_pending_card(conn, monkeypatch)

    result = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None, failure_limit=1)

    assert result.auto_split == [tid], (
        "the split phase must split on a cold aux cache under multiplexing; "
        f"failed={result.auto_split_failed}"
    )
    children = _children(conn, tid)
    assert len(children) == 2, "the decomposer's two children must exist as real cards"
    assert result.auto_split_failed == []
    payload = [e.payload or {} for e in kb.list_events(conn, tid) if e.kind == "auto_split"][-1]
    assert sorted(payload["child_ids"]) == sorted(children)


# ---------------------------------------------------------------------------
# 2. restart guard: the first split after a reset cache works
# ---------------------------------------------------------------------------

def test_first_split_after_a_restart_needs_no_warm_up(
    conn, monkeypatch, multiplexed, cold_aux_cache, stub_llm, roster,
):
    """A cache reset IS the restart: the next split must not depend on a prior aux call."""
    first = _split_pending_card(conn, monkeypatch, title="first card")
    result = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None, failure_limit=1)
    assert result.auto_split == [first]

    cold_aux_cache()  # restart: the gateway process starts with an empty client cache
    stub_llm.return_value.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=_SPLIT_PAYLOAD), finish_reason="stop",
        )],
        model="probe-model",
    )
    second = _split_pending_card(conn, monkeypatch, title="second card")
    result = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None, failure_limit=1)

    assert result.auto_split == [second], (
        "after a restart the FIRST split must give children without a warm-up call; "
        f"failed={result.auto_split_failed}"
    )
    assert len(_children(conn, second)) == 2


# ---------------------------------------------------------------------------
# 3. the failure class stays visible
# ---------------------------------------------------------------------------

def test_missing_scope_is_marked_as_a_scope_failure_not_a_provider_error(
    conn, monkeypatch, multiplexed, cold_aux_cache, stub_llm, roster,
):
    """Scope loss is our defect: the journal must say so, not "LLM error".

    ``run_split_phase`` now binds the scope itself, so the *unbound* call path is
    reproduced one level down (the decomposer invoked directly, as a caller that
    forgot the scope would do) and its reason is then carried through the phase.
    """
    from agent.secret_scope import UnscopedSecretError

    assert specify.failure_class(f"secret-scope: LLM error {UnscopedSecretError.__name__}") == "secret_scope"
    assert specify.failure_class("LLM error: APITimeoutError") == ""

    tid = _split_pending_card(conn, monkeypatch)
    outcome = decomp.decompose_failed_card(tid, generation=1, author="dispatcher")
    assert outcome.ok is False
    assert outcome.reason.startswith("secret-scope:"), outcome.reason
    assert "UnscopedSecretError" in outcome.reason

    # The phase journals that reason with its class, and the class survives into
    # the card's automation state.
    split, failed = kac.run_split_phase(conn, decompose_fn=lambda *a, **k: outcome)
    assert split == [] and failed == [tid]
    state = kac.read_state(conn, tid)
    assert state.split_failures == 1
    assert state.split_failure_class == "secret_scope"

    detail = [e.payload or {} for e in kb.list_events(conn, tid) if e.kind == "auto_continued"][-1]
    assert detail["failure_class"] == "secret_scope"
    assert detail["detail"].startswith("secret-scope:")

    # ... and it reaches the terminal block as a field, not as prose.
    plan = kac.plan_after_failure(
        outcome="timed_out", extra=None, failures=2, effective_limit=2,
        limit_source="dispatcher", generation=0, continuations=1,
        split_failures=2, split_failure_class="secret_scope",
        settings=kac.AutoContinueSettings(enabled=True, max_splits=1),
    )
    assert plan.mode == "block"
    assert plan.payload["decomposer_unavailable"] is True
    assert plan.payload["decomposer_failure_class"] == "secret_scope"


def test_a_provider_failure_stays_an_ordinary_llm_error(
    conn, monkeypatch, multiplexed, cold_aux_cache, stub_llm, roster,
):
    """The old semantic is kept: a real provider refusal is not tagged as scope loss."""
    from types import SimpleNamespace

    assert specify.failure_class("LLM error: APITimeoutError") == ""

    tid = _split_pending_card(conn, monkeypatch)
    outage = SimpleNamespace(ok=False, reason="LLM error: APITimeoutError", child_ids=None)
    split, failed = kac.run_split_phase(conn, decompose_fn=lambda *a, **k: outage)

    assert split == [] and failed == [tid]
    row = kb.get_task(conn, tid)
    assert row is not None and row.status == "ready", "a provider outage must keep the card working"
    detail = [e.payload or {} for e in kb.list_events(conn, tid) if e.kind == "auto_continued"][-1]
    assert detail["failure_class"] == ""
    assert kac.read_state(conn, tid).split_failure_class == ""
