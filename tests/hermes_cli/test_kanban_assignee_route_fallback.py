"""Assignee-aware provider/model route fallback for kanban dispatch (t_f2341b7a).

Regression: a child task inherits the root line's provider/model route
(deepseek-kb) that exists on the dispatcher host but NOT in the assignee
profile (profile 05-rag-knowledge-engineer only has letovo). The host-scoped
pre-flight let it through and the worker died on startup ("No API key found",
exit 1). Now the effective route is resolved against the ASSIGNEE profile's
config and falls back to the assignee's default route (or blocks with a
readable reason when the assignee has no usable route at all).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def assignee_registry(monkeypatch, tmp_path):
    """Map logical profile names to isolated profile config dirs.

    ``resolve_profile_env`` is stubbed so ``_assignee_home`` switches
    HERMES_HOME to the directory whose config.yaml carries that profile's
    route set. ``_provider_route_blocker`` is stubbed to answer based on the
    *active* HERMES_HOME (the exact context a worker would run in), so a route
    is "usable" only when the profile whose env is currently active declares it.
    """
    import hermes_cli.profiles as profiles
    import hermes_cli.config as config

    # create_task/_validate_assignee_profile requires the named profile to
    # exist; in this isolated suite every logical profile is constructible.
    monkeypatch.setattr(profiles, "profile_exists", lambda _n: True)

    homes: dict[str, Path] = {}

    _PROVIDER_SPECS = {
        "letovo": {"base_url": "https://llm.letovo.site/openai",
                  "model": "deepseek-v4-flash"},
        "deepseek-kb": {"base_url": "https://api.nti.kts.tech/v1",
                       "model": "deepseek-v4-flash-local"},
    }

    def make(name: str, provider: str | None, model: str | None, *,
             default_provider=None, default_model=None, providers=None):
        home = tmp_path / "profiles" / name
        home.mkdir(parents=True, exist_ok=True)
        if providers is None:
            # Declare exactly the providers the profile can actually run.
            providers = [p for p in (provider, default_provider) if p in _PROVIDER_SPECS]
        lines = []
        if default_provider or provider:
            lines.append("model:")
            lines.append(f"  provider: {default_provider or provider}")
            if default_model:
                lines.append(f"  default: {default_model}")
            if providers:
                lines.append("  providers:")
                for p in providers:
                    spec = _PROVIDER_SPECS[p]
                    lines.append(f"  {p}:")
                    lines.append(f"    base_url: {spec['base_url']}")
                    lines.append(f"    model: {spec['model']}")
        else:
            lines.append("model: {}")
        (home / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
        homes[name] = home
        return home

    monkeypatch.setattr(profiles, "resolve_profile_env", lambda name: str(homes[name]))

    def blocker(provider, model):
        # Mirrors the real _provider_route_blocker contract: None = usable.
        if not provider or not model:
            return None
        active = Path(config.get_hermes_home())
        active_cfg_path = active / "config.yaml"
        if not active_cfg_path.exists():
            # No config in the active (default/.hermes) home -> no provider is
            # usable there; only the profile homes written by `make` carry one.
            return f"provider {provider!r} is not configured in profile {active.name}"
        active_cfg = active_cfg_path.read_text(encoding="utf-8")
        # A provider is "usable" in the active profile iff that profile's
        # config declares it under model.providers.
        if provider in ("deepseek-kb", "letovo"):
            if f"  {provider}:" in active_cfg and "  providers:" in active_cfg:
                return None
            return f"provider {provider!r} is not configured in profile {active.name}"
        return None

    monkeypatch.setattr(kb, "_provider_route_blocker", blocker)
    return SimpleReg(make)


class SimpleReg:
    def __init__(self, make):
        self.make = make


@pytest.fixture
def home(monkeypatch, tmp_path):
    h = tmp_path / ".hermes"
    h.mkdir()
    # Model the dispatcher HOST: it CAN run deepseek-kb (like 01-orchestrator),
    # while individual assignee profiles may not. The host config is what the
    # create-time ``_validate_provider_model_route`` check sees.
    (h / "config.yaml").write_text(
        "model:\n"
        "  provider: deepseek-kb\n"
        "  default: deepseek-v4-flash-local\n"
        "  providers:\n"
        "    letovo:\n"
        "      base_url: https://llm.letovo.site/openai\n"
        "      model: deepseek-v4-flash\n"
        "    deepseek-kb:\n"
        "      base_url: https://api.nti.kts.tech/v1\n"
        "      model: deepseek-v4-flash-local\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return h


# ---------------------------------------------------------------------------
# Unit: resolve_assignee_spawn_route
# ---------------------------------------------------------------------------


def test_unpinned_card_uses_assignee_default(assignee_registry, home):
    assignee_registry.make("rag", None, None, default_provider="letovo",
                           default_model="deepseek-v4-flash")
    # No pin -> (None, None) note is "no change"; worker boots on profile default.
    assert kb.resolve_assignee_spawn_route("rag", None, None) == (None, None, None)


def test_matching_route_no_change(assignee_registry, home):
    # Assignee CAN run deepseek-kb -> pinned route kept verbatim.
    assignee_registry.make("orchestrator", "deepseek-kb", "deepseek-v4-flash-local")
    assert kb.resolve_assignee_spawn_route(
        "orchestrator", "deepseek-kb", "deepseek-v4-flash-local"
    ) == ("deepseek-kb", "deepseek-v4-flash-local", None)


def test_inherited_route_unavailable_falls_back_to_assignee_default(
    assignee_registry, home,
):
    # deepseek-kb NOT configured in the rag profile (only letovo is its model
    # provider default) -> fall back to letovo by clearing the override.
    assignee_registry.make("rag", None, None, default_provider="letovo",
                           default_model="deepseek-v4-flash")
    effective = kb.resolve_assignee_spawn_route(
        "rag", "deepseek-kb", "deepseek-v4-flash-local"
    )
    provider, model, note = effective
    assert provider is None and model is None
    assert "falling back" in note and "letovo" in note


def test_assignee_with_no_route_fails_closed(assignee_registry, home):
    # The rag profile defines no model at all -> no usable default -> raise.
    assignee_registry.make("rag", None, None)
    with pytest.raises(ValueError, match="defines no default provider/model route"):
        kb.resolve_assignee_spawn_route("rag", "deepseek-kb", "deepseek-v4-flash-local")


def test_assignee_default_also_unusable_fails_closed(assignee_registry, home):
    # Card pinned to deepseek-kb AND assignee default is deepseek-kb which its
    # own config lacks -> both unusable -> block reason names both.
    # Profile whose configured default (deepseek-kb) is NOT actually
    # declared in its own config -> no usable route at all.
    assignee_registry.make("broken", None, None, default_provider="deepseek-kb",
                           default_model="deepseek-v4-flash-local", providers=[])
    with pytest.raises(ValueError, match="also unusable"):
        kb.resolve_assignee_spawn_route(
            "broken", "deepseek-kb", "deepseek-v4-flash-local"
        )


# ---------------------------------------------------------------------------
# Dispatch-level: real dispatch_once drives _default_spawn cmd building
# ---------------------------------------------------------------------------


def test_dispatch_child_inherits_deepseek_kb_spawns_assignee_default(
    assignee_registry, home, monkeypatch, tmp_path,
):
    """(a) child of a deepseek-kb line, assignee that only runs letovo -> the
    spawned command omits -m/--provider so the worker uses its profile default
    instead of dying on startup (t_f2341b7a)."""
    import hermes_cli.profiles as profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda _n: True)
    assignee_registry.make("rag", None, None, default_provider="letovo",
                           default_model="deepseek-v4-flash")
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])

    captured = {}

    class FakeProc:
        pid = 5001

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    with kb.connect() as conn:
        kb.create_board(slug="default", name="T")
        # Host home declares deepseek-kb, so create-time validation passes even
        # though the rag ASSIGNEE profile only runs letovo. The child inherits
        # the deepseek-kb route from its parent (MODEL_LOCK lineage), exactly the
        # deepseek-kb -> rag crash scenario.
        parent = kb.create_task(
            conn, title="parent", assignee="rag",
            model_override="deepseek-v4-flash-local",
            provider_override="deepseek-kb",
        )
        child = kb.create_task(conn, title="child", assignee="rag", parents=(parent,))
        row = conn.execute(
            "SELECT provider_override, model_override FROM tasks WHERE id=?",
            (child,),
        ).fetchone()
        assert (row["provider_override"], row["model_override"]) == (
            "deepseek-kb", "deepseek-v4-flash-local",
        )
        # NEW: spawn resolves the ASSIGNEE's route, not the host's. rag only runs
        # letovo, so the deepseek-kb pin falls back to the assignee default.
        pid = kb._default_spawn(kb.get_task(conn, child), str(workspace))
        assert pid == 5001

    # Spawned WITHOUT -m/--provider -> worker boots on rag profile default
    # (letovo/deepseek-v4-flash) instead of dying at startup on the
    # deepseek-kb route the assignee cannot run. Assert the actual spawned
    # cmd content, not just the pid, so criterion 6(a) is genuinely covered
    # at the dispatcher level (t_0918d8f9): a buggy spawn that still pinned
    # `-m deepseek-v4-flash-local --provider deepseek-kb` must fail this test.
    assert "-m" not in captured["cmd"]
    assert "--provider" not in captured["cmd"]


def test_dispatch_matching_route_pins_provider_and_model(
    assignee_registry, home, monkeypatch, tmp_path,
):
    """(c) assignee can run the inherited route -> spelled out verbatim."""
    import hermes_cli.profiles as profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda _n: True)
    assignee_registry.make("rag", "deepseek-kb", "deepseek-v4-flash-local",
                           default_provider="deepseek-kb",
                           default_model="deepseek-v4-flash-local")
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])

    captured = {}

    class FakeProc:
        pid = 5002

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)

    with kb.connect() as conn:
        kb.create_board(slug="default", name="T")
        tid = kb.create_task(
            conn, title="pinned", assignee="rag",
            model_override="deepseek-v4-flash-local",
            provider_override="deepseek-kb",
        )
        workspace = tmp_path / "ws"
        workspace.mkdir()
        res = kb.dispatch_once(conn, max_in_progress=10)

    assert any(sp[0] == tid for sp in res.spawned)
    assert "-m" in captured["cmd"]
    assert "deepseek-v4-flash-local" in captured["cmd"]
    assert "--provider" in captured["cmd"]
    assert "deepseek-kb" in captured["cmd"]


def test_dispatch_assignee_without_route_blocks_capability(
    assignee_registry, home, monkeypatch, tmp_path,
):
    """(b) assignee with no provider at all -> blocked with a readable reason
    (capability), never spawned."""
    import hermes_cli.profiles as profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda _n: True)
    # rag profile: no model at all.
    assignee_registry.make("rag", None, None)
    spawn_calls = []

    def spy_spawn(task, workspace, board=None, **_kw):
        spawn_calls.append(task)
        return 4242

    with kb.connect() as conn:
        kb.create_board(slug="default", name="T")
        tid = kb.create_task(
            conn, title="no-route", assignee="rag",
            model_override="deepseek-v4-flash-local",
            provider_override="deepseek-kb",
        )
        res = kb.dispatch_once(conn, spawn_fn=spy_spawn, dry_run=False,
                              max_in_progress=10)

    assert tid in res.auto_blocked
    assert tid not in res.spawned
    assert spawn_calls == []
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        blocked = next(e for e in kb.list_events(conn, tid) if e.kind == "blocked")
        assert blocked.payload["kind"] == "capability"
        assert "defines no default provider/model route" in blocked.payload["reason"]


# ---------------------------------------------------------------------------
# create_task relaxation (criterion 3)
# ---------------------------------------------------------------------------


def test_recreate_child_onto_assignee_usable_route_allowed(
    assignee_registry, home, monkeypatch,
):
    """A child of a deepseek-kb line may be re-created onto letovo when the
    assignee cannot run deepseek-kb (no MODEL_LOCK block)."""
    assignee_registry.make("rag", None, None, default_provider="letovo",
                           default_model="deepseek-v4-flash")
    with kb.connect() as conn:
        parent = kb.create_task(
            conn, title="parent", assignee="rag",
            model_override="deepseek-v4-flash-local",
            provider_override="deepseek-kb",
        )
        child = kb.create_task(
            conn, title="child", assignee="rag", parents=(parent,),
            model_override="deepseek-v4-flash",
            provider_override="letovo",
        )
        row = conn.execute(
            "SELECT provider_override, model_override FROM tasks WHERE id=?",
            (child,),
        ).fetchone()
        assert (row["provider_override"], row["model_override"]) == (
            "letovo", "deepseek-v4-flash",
        )


def test_recreate_child_conflict_still_fails_when_inherited_route_usable(
    assignee_registry, home, monkeypatch,
):
    """When the assignee CAN run the inherited route, a conflicting explicit
    route is still a MODEL_LOCK block (no over-relaxation)."""
    assignee_registry.make("rag", "deepseek-kb", "deepseek-v4-flash-local",
                           default_provider="deepseek-kb",
                           default_model="deepseek-v4-flash-local")
    with kb.connect() as conn:
        parent = kb.create_task(
            conn, title="parent", assignee="rag",
            model_override="deepseek-v4-flash-local",
            provider_override="deepseek-kb",
        )
        with pytest.raises(ValueError, match="MODEL_LOCK lineage conflict"):
            kb.create_task(
                conn, title="child", assignee="rag", parents=(parent,),
                model_override="deepseek-v4-flash",
                provider_override="letovo",
            )
