"""A card's customer is the PERSON who ordered it, and their chat is where its result goes.

Criterion this file exists for (card ``t_6aab4d66``): a card created from the web interface
carried ``created_by="dashboard"`` — the surface, not the person — so the delivery pipeline had
no address to send the finished artifact to. Two claims are proven here, and one backward
compatibility:

* on the HTTP surface, ``created_by`` is the request's verified subject and a notify
  subscription exists for that person's chat;
* with ``acl.multi_user`` off the old literal is written byte for byte, and no extra row;
* the address is read from the bindings registry, never guessed: a subject no record names
  resolves to no destination (fail closed).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_customer as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_notify as kbn


# ---------------------------------------------------------------------------
# registry fixture: one person reachable on Telegram, one with no chat at all
# ---------------------------------------------------------------------------

class _FakeRegistry:
    def __init__(self, people: dict[str, tuple[str, ...]]):
        self._people = people

    def subjects_of(self, subject):
        return self._people.get(str(subject or "").strip(), ())


@pytest.fixture
def registry(monkeypatch):
    """Bindings registry with ``person/anna`` on Telegram and ``person/quiet`` nowhere."""
    fake = _FakeRegistry({
        "person/anna": ("person/anna", "tg:555000111"),
        "person/quiet": ("person/quiet",),
    })
    import hermes_acl_bindings as hb

    monkeypatch.setattr(hb, "load_cached", lambda: fake)
    return fake


# ---------------------------------------------------------------------------
# creator_label — the flag is additive
# ---------------------------------------------------------------------------

def test_creator_label_is_the_subject_under_multi_user(monkeypatch, registry):
    monkeypatch.setattr(kc, "_multi_user", lambda: True)
    monkeypatch.setattr("hermes_acl_results.request_subject", lambda request: "person/anna")

    assert kc.creator_label("dashboard", object()) == "person/anna"


def test_creator_label_keeps_the_surface_when_the_flag_is_off(monkeypatch, registry):
    monkeypatch.setattr(kc, "_multi_user", lambda: False)
    monkeypatch.setattr("hermes_acl_results.request_subject", lambda request: "person/anna")

    assert kc.creator_label("dashboard", object()) == "dashboard"


def test_creator_label_keeps_the_surface_when_the_request_names_nobody(monkeypatch, registry):
    monkeypatch.setattr(kc, "_multi_user", lambda: True)
    monkeypatch.setattr("hermes_acl_results.request_subject", lambda request: "")

    assert kc.creator_label("dashboard", object()) == "dashboard"


# ---------------------------------------------------------------------------
# delivery_addresses / notify_targets — the registry is the only source
# ---------------------------------------------------------------------------

def test_addresses_come_from_the_registry(registry):
    assert kc.delivery_addresses("person/anna") == [("telegram", "555000111")]


def test_a_subject_without_a_chat_has_no_address(registry):
    assert kc.delivery_addresses("person/quiet") == []
    assert kc.notify_targets("person/quiet") == []


def test_a_surface_label_is_not_a_person(registry):
    assert kc.notify_targets("dashboard") == []


def test_unusable_registry_delivers_nothing(monkeypatch):
    import hermes_acl_bindings as hb

    def _boom():
        raise RuntimeError("corrupt registry")

    monkeypatch.setattr(hb, "load_cached", _boom)
    assert kc.delivery_addresses("person/anna") == []


def test_notify_target_addresses_the_customer_without_waking_an_agent(monkeypatch, registry):
    monkeypatch.setattr(kc, "notifier_profile_for", lambda platform: "01-orchestrator")

    (target,) = kc.notify_targets("person/anna")
    assert target["platform"] == "telegram"
    assert target["chat_id"] == target["user_id"] == "555000111"
    assert target["chat_type"] == "dm"
    assert target["notifier_profile"] == "01-orchestrator"
    # A customer address is a destination, never a worker slot.
    assert target["delivery_mode"] == "notify"
    assert target["delivery_metadata"]["customer_subject"] == "person/anna"


def test_platform_filter_keeps_only_the_requested_transport(monkeypatch, registry):
    assert kc.notify_targets("person/anna", platform="discord") == []
    assert len(kc.notify_targets("person/anna", platform="telegram")) == 1


# ---------------------------------------------------------------------------
# the HTTP surface: created_by + the subscription row
# ---------------------------------------------------------------------------

def _load_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_kanban_customer", plugin_file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    mod = _load_router()
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/kanban")
    return TestClient(app)


def test_dashboard_card_records_the_employee_subject_and_subscribes_their_chat(
        dashboard, monkeypatch, registry):
    monkeypatch.setattr(kc, "_multi_user", lambda: True)
    monkeypatch.setattr("hermes_acl_results.request_subject", lambda request: "person/anna")
    monkeypatch.setattr(kc, "notifier_profile_for", lambda platform: "01-orchestrator")

    r = dashboard.post("/api/plugins/kanban/tasks",
                       json={"title": "Посчитать бюджет", "assignee": "03-software-engineer"})
    assert r.status_code == 200, r.text
    task_id = r.json()["task"]["id"]

    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        task = kb.get_task(conn, task_id)
        subs = kbn.list_notify_subs(conn, task_id)

    assert task.created_by == "person/anna"
    customer = [s for s in subs if (s.get("delivery_metadata") or {}).get("customer_subject") == "person/anna"]
    assert len(customer) == 1
    assert (customer[0]["platform"], customer[0]["chat_id"], customer[0]["user_id"]) == (
        "telegram", "555000111", "555000111")


def test_multi_user_off_writes_the_legacy_literal_and_no_customer_row(dashboard, monkeypatch, registry):
    monkeypatch.setattr(kc, "_multi_user", lambda: False)
    monkeypatch.setattr("hermes_acl_results.request_subject", lambda request: "person/anna")

    r = dashboard.post("/api/plugins/kanban/tasks", json={"title": "Старое поведение"})
    assert r.status_code == 200, r.text
    task_id = r.json()["task"]["id"]

    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        task = kb.get_task(conn, task_id)
        subs = kbn.list_notify_subs(conn, task_id)

    assert task.created_by == "dashboard"
    assert subs == []
