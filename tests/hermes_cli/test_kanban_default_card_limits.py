"""Board-level default card budget (t_ed744fd2, П3).

Before this, a card's budget depended entirely on whoever created it: only a
creator's explicit ``max_runtime_seconds`` / ``max_turns`` applied, so one
board ran 2-8 h / hundreds-of-steps cards next to 3600 s / 150-step ones.

``kanban.default_max_runtime_seconds`` / ``kanban.default_max_turns`` are
materialised onto the card row at CREATION time, so the limit is a property of
the card: visible in the DB, overridable per card, stable across retries.
"""

from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path, monkeypatch):
    # Clear inherited dispatcher env first — otherwise HERMES_KANBAN_DB pins
    # the LIVE board and these tests would create/claim real cards.
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_DB",
                "HERMES_KANBAN_WORKSPACE", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_STOP_NUDGE", "0")
    # The isolated HERMES_HOME has no installed profiles; assignee validation
    # is a separate concern (tests/hermes_cli/test_kanban_assignee*). Keep the
    # names verbatim so these tests exercise the budget defaults only.
    monkeypatch.setattr(kb, "_validate_assignee_profile", lambda a: a)
    kb.init_db()
    conn = kb.connect()
    yield conn
    conn.close()


def _cfg(monkeypatch, **kanban):
    """Pin the config the resolver reads (no host config.yaml leakage)."""
    monkeypatch.setattr(kb, "default_card_limits",
                        _resolver(kanban))


def _resolver(kanban):
    def _resolve(kanban_cfg=None):
        cfg = kanban if kanban_cfg is None else (kanban_cfg or {})

        def _opt(value):
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return None
            return parsed if parsed > 0 else None

        return _opt(cfg.get("default_max_runtime_seconds")), _opt(cfg.get("default_max_turns"))
    return _resolve


# ── resolver ────────────────────────────────────────────────────────────────


def test_schema_defaults_are_the_board_budget():
    """A fresh install gets 3 h / 300 steps without hand-editing config."""
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    section = DEFAULT_CONFIG["kanban"]
    assert section["default_max_runtime_seconds"] == 3 * 60 * 60
    assert section["default_max_turns"] == 300
    assert section["soft_finish_grace_steps"] >= 1


def test_resolver_reads_explicit_config_section():
    runtime, turns = kb.default_card_limits(
        {"default_max_runtime_seconds": 1800, "default_max_turns": 120}
    )
    assert (runtime, turns) == (1800, 120)


def test_resolver_treats_junk_as_unset():
    runtime, turns = kb.default_card_limits(
        {"default_max_runtime_seconds": "nope", "default_max_turns": -4}
    )
    # Unparseable junk falls back to the schema defaults (fail closed: junk
    # must not silently strip a card of its budget) ...
    assert runtime == 3 * 60 * 60
    # ... while an explicit non-positive value is an operator opt-out.
    assert turns is None


def test_resolver_zero_is_an_explicit_opt_out():
    runtime, turns = kb.default_card_limits(
        {"default_max_runtime_seconds": 0, "default_max_turns": 0}
    )
    assert runtime is None and turns is None


# ── creation ────────────────────────────────────────────────────────────────


def test_create_task_materialises_defaults(board, monkeypatch):
    _cfg(monkeypatch, default_max_runtime_seconds=7200, default_max_turns=200)
    tid = kb.create_task(board, title="routine", assignee="01-orchestrator")
    row = board.execute(
        "SELECT max_runtime_seconds, max_turns FROM tasks WHERE id = ?", (tid,)
    ).fetchone()
    assert row["max_runtime_seconds"] == 7200
    assert row["max_turns"] == 200


def test_explicit_creator_values_win(board, monkeypatch):
    _cfg(monkeypatch, default_max_runtime_seconds=7200, default_max_turns=200)
    tid = kb.create_task(
        board, title="big one", assignee="01-orchestrator",
        max_runtime_seconds=8 * 3600, max_turns=600,
    )
    row = board.execute(
        "SELECT max_runtime_seconds, max_turns FROM tasks WHERE id = ?", (tid,)
    ).fetchone()
    assert row["max_runtime_seconds"] == 8 * 3600
    assert row["max_turns"] == 600


def test_no_config_keeps_legacy_uncapped_card(board, monkeypatch):
    """Unset defaults = no cap from config (previous behaviour preserved)."""
    _cfg(monkeypatch)  # empty section
    tid = kb.create_task(board, title="legacy", assignee="01-orchestrator")
    row = board.execute(
        "SELECT max_runtime_seconds, max_turns FROM tasks WHERE id = ?", (tid,)
    ).fetchone()
    assert row["max_runtime_seconds"] is None
    assert row["max_turns"] is None


def test_defaults_are_stable_across_a_requeue(board, monkeypatch):
    """The limit lives on the card, so a retry keeps the same budget."""
    _cfg(monkeypatch, default_max_runtime_seconds=7200, default_max_turns=200)
    tid = kb.create_task(board, title="retry me", assignee="01-orchestrator")
    claimed = kb.claim_task(board, tid)
    assert claimed is not None
    kb.reclaim_task(board, tid) if hasattr(kb, "reclaim_task") else None
    row = board.execute(
        "SELECT max_runtime_seconds, max_turns FROM tasks WHERE id = ?", (tid,)
    ).fetchone()
    assert row["max_runtime_seconds"] == 7200
    assert row["max_turns"] == 200


# ── worker env carries the soft-finish budget arms ──────────────────────────


def test_spawn_env_exports_the_time_budget_arms(board, monkeypatch):
    """The dispatcher must hand the worker its wall-clock budget + start."""
    tid = kb.create_task(
        board, title="timed", assignee="01-orchestrator", max_runtime_seconds=3600,
    )
    task = kb.claim_task(board, tid)
    assert task is not None

    captured = {}

    def _fake_popen(cmd, **kwargs):
        captured["env"] = kwargs.get("env") or {}
        raise FileNotFoundError("stop here")  # we only want the env

    monkeypatch.setattr(kb.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kb, "_resolve_worker_cli_toolsets", lambda *_: None)
    import tempfile
    from pathlib import Path

    ws = tempfile.mkdtemp()
    with pytest.raises(RuntimeError):
        kb._default_spawn(task, ws)

    env = captured["env"]
    assert env["HERMES_KANBAN_MAX_RUNTIME_SECONDS"] == "3600"
    assert int(env["HERMES_KANBAN_RUN_STARTED_AT"]) > 0
    assert env["HERMES_KANBAN_TASK"] == tid


def test_spawn_env_omits_time_arms_without_a_cap(board, monkeypatch):
    tid = kb.create_task(board, title="uncapped", assignee="01-orchestrator")
    board.execute("UPDATE tasks SET max_runtime_seconds = NULL WHERE id = ?", (tid,))
    task = kb.claim_task(board, tid)
    assert task is not None

    captured = {}

    def _fake_popen(cmd, **kwargs):
        captured["env"] = kwargs.get("env") or {}
        raise FileNotFoundError("stop here")

    monkeypatch.setattr(kb.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kb, "_resolve_worker_cli_toolsets", lambda *_: None)
    import tempfile

    ws = tempfile.mkdtemp()
    with pytest.raises(RuntimeError):
        kb._default_spawn(task, ws)

    env = captured["env"]
    assert "HERMES_KANBAN_MAX_RUNTIME_SECONDS" not in env
    assert "HERMES_KANBAN_RUN_STARTED_AT" not in env
