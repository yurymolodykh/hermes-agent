"""A card created in the web interface is stamped with the PERSON who ordered it.

Acceptance criteria 1, 2 and 4 of task t_6aab4d66:

1. ``created_by`` is the request's verified subject, read back from the DB.
2. ``kanban_notify_subs`` carries a row addressed to that person's chat.
4. With ``acl.multi_user`` off the create path is unchanged (``created_by == "dashboard"``
   and no customer subscription is written).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import hermes_acl_identity as hai
import hermes_acl_results as har
import hermes_acl_scope as has_
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_notify as kbn

CARD = {
    "title": "Собрать отчёт по конкурсу",
    "assignee": "03-software-engineer",
}
CUSTOMER = "person/anna"
TG_ID = "555000111"


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_customer_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME carrying a bindings registry for one person."""
    hermes_home = tmp_path / ".hermes"
    (hermes_home / "acl").mkdir(parents=True)
    (hermes_home / "acl" / "profile-bindings.json").write_text(json.dumps({
        "schema": 1,
        "persons": [{
            "slug": CUSTOMER, "display_name": "Анна", "shared": False,
            "profiles": [], "subjects": [f"tg:{TG_ID}"], "owner": CUSTOMER,
            "source": "test", "created_at": "2026-11-25T00:00:00+00:00",
            "updated_at": "2026-11-25T00:00:00+00:00",
        }],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return hermes_home


@pytest.fixture
def client(home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


def _signed_in_as(monkeypatch, subject: str) -> None:
    """Make the request carry *subject* as its verified identity, multi-user ON."""
    monkeypatch.setattr(hai, "multi_user_enabled", lambda: True)
    monkeypatch.setattr(has_, "request_identity_subject", lambda request: subject)
    monkeypatch.setattr(har, "request_subject", lambda request: subject)


def test_card_created_in_ui_is_stamped_with_the_customers_subject(client, home, monkeypatch):
    _signed_in_as(monkeypatch, CUSTOMER)

    r = client.post("/api/plugins/kanban/tasks", json=CARD)
    assert r.status_code == 200, r.text
    task_id = r.json()["task"]["id"]

    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task.created_by == CUSTOMER, "the card must name the person, not the surface"
        subs = kbn.list_notify_subs(conn, task_id)

    addressed = [
        s for s in subs
        if s["platform"] == "telegram" and s["chat_id"] == TG_ID and s["user_id"] == TG_ID
    ]
    assert addressed, f"the customer must be subscribed on their own chat: {subs}"
    assert addressed[0]["delivery_metadata"].get("customer_subject") == CUSTOMER


def test_multi_user_off_keeps_the_legacy_surface_label(client, home, monkeypatch):
    monkeypatch.setattr(hai, "multi_user_enabled", lambda: False)

    r = client.post("/api/plugins/kanban/tasks", json=CARD)
    assert r.status_code == 200, r.text
    task_id = r.json()["task"]["id"]

    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn:
        task = kb.get_task(conn, task_id)
        subs = kbn.list_notify_subs(conn, task_id)

    assert task.created_by == "dashboard", "legacy behaviour is byte for byte the old row"
    assert not [s for s in subs if s["platform"] == "telegram"], (
        f"a surface label resolves to no address, so nothing is written: {subs}"
    )
