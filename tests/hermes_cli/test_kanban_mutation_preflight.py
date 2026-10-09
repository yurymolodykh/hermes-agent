"""Fail-closed public Kanban mutation preflights."""

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    connection = kb.connect()
    yield connection
    connection.close()


@pytest.fixture
def configured_routes(monkeypatch):
    """A deterministic stand-in for dispatcher route resolution."""
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(
        profiles,
        "profile_exists",
        lambda name: name in {"known-profile", "01-orchestrator"},
    )

    def blocker(provider, model):
        if (provider, model) == ("openai-codex", "gpt-5.6-terra"):
            return None
        if provider in {"custom", "letovo"}:
            return "provider does not name a configured provider entry"
        return None

    monkeypatch.setattr(kb, "_provider_route_blocker", blocker)


def test_create_rejects_unknown_assignee(conn, configured_routes):
    with pytest.raises(ValueError, match="unknown Kanban assignee 'missing-profile'"):
        kb.create_task(conn, title="reject", assignee="missing-profile")

    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_synthetic_isolated_assignees_remain_rejected_without_test_fixture(conn):
    """The opt-in test registry must not relax the production DB boundary."""
    with pytest.raises(ValueError, match="unknown Kanban assignee 'test-worker'"):
        kb.create_task(conn, title="reject synthetic", assignee="test-worker")

    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


@pytest.mark.parametrize("assignee", ("test-worker", "builder", "parent-worker"))
def test_isolated_assignee_registry_allows_synthetic_profiles_only_when_requested(
    conn, isolated_kanban_assignee_registry, assignee
):
    task_id = kb.create_task(conn, title="isolated", assignee=assignee)

    assert kb.get_task(conn, task_id).assignee == assignee


def test_reassignment_rejects_unknown_assignee(conn, configured_routes):
    task_id = kb.create_task(conn, title="known", assignee="known-profile")

    with pytest.raises(ValueError, match="unknown Kanban assignee 'missing-profile'"):
        kb.assign_task(conn, task_id, "missing-profile")

    assert kb.get_task(conn, task_id).assignee == "known-profile"


def test_known_assignee_and_configured_route_are_accepted(conn, configured_routes):
    task_id = kb.create_task(
        conn,
        title="accepted",
        assignee="KNOWN-PROFILE",
        provider_override="openai-codex",
        model_override="gpt-5.6-terra",
    )

    task = kb.get_task(conn, task_id)
    assert task.assignee == "known-profile"
    assert (task.provider_override, task.model_override) == (
        "openai-codex",
        "gpt-5.6-terra",
    )


def test_create_rejects_provider_without_model(conn, configured_routes):
    with pytest.raises(ValueError, match="provider_override requires a model_override"):
        kb.create_task(
            conn,
            title="incomplete",
            assignee="known-profile",
            provider_override="openai-codex",
        )


@pytest.mark.parametrize("provider", ["custom", "letovo"])
def test_create_rejects_unusable_provider_route(conn, configured_routes, provider):
    with pytest.raises(ValueError, match="unusable provider/model route"):
        kb.create_task(
            conn,
            title="unusable",
            assignee="known-profile",
            provider_override=provider,
            model_override="gpt-5.6-terra",
        )

    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_set_model_override_rejects_unusable_route(conn, configured_routes):
    task_id = kb.create_task(conn, title="known", assignee="known-profile")

    with pytest.raises(ValueError, match="unusable provider/model route"):
        kb.set_model_override(
            conn,
            task_id,
            "gpt-5.6-terra",
            provider="custom",
        )

    task = kb.get_task(conn, task_id)
    assert (task.provider_override, task.model_override) == (None, None)


def test_set_model_override_accepts_configured_route(conn, configured_routes):
    task_id = kb.create_task(conn, title="known", assignee="known-profile")

    assert kb.set_model_override(
        conn,
        task_id,
        "gpt-5.6-terra",
        provider="openai-codex",
    )
    task = kb.get_task(conn, task_id)
    assert (task.provider_override, task.model_override) == (
        "openai-codex",
        "gpt-5.6-terra",
    )