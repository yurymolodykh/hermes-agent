"""OPS-12: a restart must never kill a worker in-tree, and a retry must resume, not restart.

Companions to ``test_kanban_restart_interrupt_budget.py`` (which pins the booking half: a
host-caused interruption is requeued without spending the card's failure budget). Here:

1. a ``gateway restart`` issued from INSIDE the gateway cgroup is scheduled outside the tree
   (``systemd-run --user --on-active``) instead of restarting in place and killing its own turn;
   when that cannot be scheduled it refuses rather than restarting in place;
2. the per-card retry budget is settable and readable through ``kanban edit --max-retries``;
3. the next attempt of a card actually sees the previous attempt's ``PROGRESS.md`` checkpoint.
"""

from __future__ import annotations

import argparse
from argparse import Namespace
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_context as kbctx


# --- 1. an in-cgroup restart is deferred, never run in place ---------------------------------

def _restart_args(**kw) -> Namespace:
    base = dict(system=False, all=False, force=False, detach=False, interrupt=False)
    base.update(kw)
    return Namespace(**base)


@pytest.fixture
def gw(monkeypatch):
    from hermes_cli import gateway as gw_mod
    # _cmd_restart's first guard also refuses when THIS process is the gateway; not our subject here.
    monkeypatch.setattr(gw_mod, "_refuse_from_inside_gateway", lambda *_a, **_k: None)
    return gw_mod


def test_in_cgroup_restart_is_scheduled_out_of_tree_not_run_in_place(gw, monkeypatch):
    """The worker's own turn must survive: no stop/start, only a timer is created."""
    from hermes_cli import gateway_deferred_restart as defer

    monkeypatch.setattr(defer, "in_gateway_cgroup", lambda: True)
    calls = {}
    monkeypatch.setattr(defer, "schedule_deferred_restart",
                        lambda verb="restart", **kw: calls.update(verb=verb, **kw) or "hermes-deferred-restart-1")
    # Any in-place restart path would reach one of these; none may be touched.
    def _boom(*_a, **_k):
        raise AssertionError("an in-cgroup restart must not restart the gateway in place")
    monkeypatch.setattr(gw, "_guard_named_profile_under_multiplexer", _boom)
    monkeypatch.setattr(gw, "_installed_service_kind_for", _boom)

    gw._cmd_restart(_restart_args())

    assert calls["verb"] == "restart"
    # Flags that change WHAT is restarted ride along; the unit path adds --in-tree so the
    # scheduled command does not defer to yet another timer forever.
    assert calls["extra_args"] == []


def test_in_cgroup_restart_forwards_all_and_system(gw, monkeypatch):
    from hermes_cli import gateway_deferred_restart as defer

    monkeypatch.setattr(defer, "in_gateway_cgroup", lambda: True)
    seen = {}
    monkeypatch.setattr(defer, "schedule_deferred_restart",
                        lambda verb="restart", **kw: seen.update(kw) or "unit")
    gw._cmd_restart(_restart_args(all=True, system=True, force=True))
    assert seen["extra_args"] == ["--all", "--system", "--force"]


def test_restart_refuses_when_it_cannot_leave_the_tree(gw, monkeypatch):
    """No user bus / no systemd-run: refusing beats killing the caller's own turn."""
    from hermes_cli import gateway_deferred_restart as defer

    monkeypatch.setattr(defer, "in_gateway_cgroup", lambda: True)

    def _unavailable(*_a, **_k):
        raise defer.DeferredRestartUnavailable("no user D-Bus session")
    monkeypatch.setattr(defer, "schedule_deferred_restart", _unavailable)

    with pytest.raises(SystemExit) as exc:
        gw._cmd_restart(_restart_args())
    assert exc.value.code == 1


def test_out_of_cgroup_restart_takes_the_normal_path(gw, monkeypatch):
    """A shell outside the cgroup is not affected: the guard must not fire for it."""
    from hermes_cli import gateway_deferred_restart as defer

    monkeypatch.setattr(defer, "in_gateway_cgroup", lambda: False)
    monkeypatch.setattr(defer, "schedule_deferred_restart",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not defer")))
    reached = {}
    monkeypatch.setattr(defer, "schedule_deferred_restart",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not defer")))
    from hermes_cli import gateway_profile_lifecycle as lifecycle
    monkeypatch.setattr(lifecycle, "profile_lifecycle", lambda verb, args: reached.setdefault("verb", verb) or True)
    gw._cmd_restart(_restart_args())
    assert reached["verb"] == "restart"


def test_scheduled_command_is_out_of_tree_and_pins_the_home(monkeypatch):
    """The unit argv: a timer (not a scope), the caller's HERMES_HOME, and --in-tree."""
    from hermes_cli import gateway_deferred_restart as defer

    captured = {}

    class _Done:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(defer, "_systemd_run_available", lambda: True)
    monkeypatch.setattr(defer, "_hermes_binary", lambda: "/usr/bin/hermes")
    monkeypatch.setattr(defer.subprocess, "run",
                        lambda argv, **kw: captured.update(argv=argv) or _Done())
    monkeypatch.setenv("HERMES_HOME", "/home/hermes/.hermes/profiles/02-hermes-platform")

    unit = defer.schedule_deferred_restart("restart", extra_args=["--all"])

    argv = captured["argv"]
    assert argv[:2] == ["systemd-run", "--user"]
    assert "--on-active=5" in argv and "--scope" not in argv, "--on-active runs after the caller is gone"
    assert "--unit" in argv and argv[argv.index("--unit") + 1] == unit
    assert "HERMES_HOME=/home/hermes/.hermes/profiles/02-hermes-platform" in argv
    tail = argv[argv.index("--") + 1:]
    assert tail == ["/usr/bin/hermes", "gateway", "restart", "--in-tree", "--all"]


def test_restart_parser_accepts_the_internal_in_tree_flag():
    """The scheduled command passes --in-tree; without the parser flag it would exit 2."""
    from hermes_cli.subcommands.gateway import build_gateway_parser

    root = argparse.ArgumentParser()
    subs = root.add_subparsers(dest="command")
    build_gateway_parser(subs, cmd_gateway=lambda _a: 0, cmd_proxy=lambda _a: 0,
                         cmd_gateway_enroll=lambda _a: 0)
    ns = root.parse_args(["gateway", "restart", "--in-tree"])
    assert getattr(ns, "in_tree", False) is True
    # And the ordinary form still parses without it.
    assert getattr(root.parse_args(["gateway", "restart"]), "in_tree", False) is False


# --- 2. the per-card retry budget is settable and readable -----------------------------------

@pytest.fixture
def board_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_edit_sets_the_cards_retry_budget(board_home):
    """Acceptance: an operations card carries max_retries=3, read back from its own row."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="ops", assignee="default")
        assert kb.get_task(conn, tid).max_retries is None
        assert kb.edit_task(conn, tid, max_retries=3) is True
        assert int(kb.get_task(conn, tid).max_retries) == 3


def test_edit_refuses_a_zero_retry_budget(board_home):
    """1 means 'trip on first failure'; 0 would block a card that never even ran."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="ops", assignee="default")
        with pytest.raises(ValueError):
            kb.edit_task(conn, tid, max_retries=0)


def test_cli_max_retries_reaches_the_row(board_home, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="ops", assignee="default")
    ns = Namespace(task_id=tid, title=None, body=None, priority=None, result=None,
                   summary=None, metadata=None, max_retries=3)
    assert kc._cmd_edit(ns) == 0
    with kbc.connect_closing() as conn:
        assert int(kb.get_task(conn, tid).max_retries) == 3


def test_edit_max_retries_on_a_running_card_does_not_disturb_its_status(board_home):
    """The budget is editable on a live card — raising it must not requeue the card."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="ops", assignee="default")
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (tid,))
        conn.commit()
        assert kb.edit_task(conn, tid, max_retries=3) is True
        task = kb.get_task(conn, tid)
        assert task.status == "running"
        assert int(task.max_retries) == 3


# --- 3. the next attempt resumes from the prior attempt's checkpoint -------------------------

def test_worker_context_carries_the_progress_checkpoint(board_home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "PROGRESS.md").write_text("# Progress\n- step 1 done\n- next: run pytest\n", encoding="utf-8")
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="long ops", assignee="default",
                             workspace_kind="scratch", workspace_path=str(ws))
        context = kb.build_worker_context(conn, tid)
    assert "## Progress checkpoint from a previous attempt" in context
    assert "step 1 done" in context
    assert "run pytest" in context
    assert str(ws / "PROGRESS.md") in context


def test_worker_context_without_a_checkpoint_says_nothing(board_home, tmp_path):
    ws = tmp_path / "empty"
    ws.mkdir()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="long ops", assignee="default",
                             workspace_kind="scratch", workspace_path=str(ws))
        context = kb.build_worker_context(conn, tid)
    assert "Progress checkpoint" not in context


def test_checkpoint_is_capped_and_never_fails_the_spawn(board_home, tmp_path):
    ws = tmp_path / "big"
    ws.mkdir()
    (ws / "PROGRESS.md").write_text("x" * (kbctx._CTX_MAX_CHECKPOINT_BYTES + 5000), encoding="utf-8")
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="long ops", assignee="default",
                             workspace_kind="scratch", workspace_path=str(ws))
        context = kb.build_worker_context(conn, tid)
    assert "truncated" in context

    # An unreadable/absent workspace path must degrade to no section, not an exception.
    with kbc.connect_closing() as conn:
        tid2 = kb.create_task(conn, title="gone", assignee="default",
                              workspace_kind="scratch", workspace_path=str(tmp_path / "nope"))
        assert "Progress checkpoint" not in kb.build_worker_context(conn, tid2)


def test_shared_dir_workspace_is_not_read_as_this_cards_checkpoint(board_home, tmp_path):
    """A ``dir`` workspace is shared with sibling cards; its PROGRESS.md is not ours to attribute."""
    ws = tmp_path / "shared"
    ws.mkdir()
    (ws / "PROGRESS.md").write_text("someone else's notes", encoding="utf-8")
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="ops", assignee="default",
                             workspace_kind="dir", workspace_path=str(ws))
        assert "someone else's notes" not in kb.build_worker_context(conn, tid)
