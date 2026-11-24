"""The customer subscription actually delivers: notifier → adapter → the person's chat.

Criterion 3 of card ``t_6aab4d66``: a card whose customer is subscribed must have its finished
result pushed into THAT person's chat, and the delivery must be visible in a log line rather
than inferred. This drives the real collection + delivery path
(``_notifier_collect`` → ``_KanbanNotification.deliver``) with a recording adapter, so the
assertion is on what the transport was actually asked to send, and on the
``kanban notifier: delivered`` record the gateway writes for it.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.kanban_watchers_notifier import _KanbanNotification, _notifier_collect
from gateway.run import GatewayRunner
from hermes_cli import kanban_customer as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


class RecordingAdapter:
    supports_async_delivery = True

    def __init__(self):
        self.sent: list[tuple] = []

    async def send(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))


class _FakeRegistry:
    def subjects_of(self, subject):
        return ("person/anna", "tg:555000111") if subject == "person/anna" else ()


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    (home / "profiles" / "01-orchestrator").mkdir(parents=True)
    (home / "profiles" / "01-orchestrator" / "config.yaml").write_text("{}\n", encoding="utf-8")
    kb.init_db()

    import hermes_acl_bindings as hb

    monkeypatch.setattr(hb, "load_cached", lambda: _FakeRegistry())
    monkeypatch.setattr(kc, "notifier_profile_for", lambda platform: "01-orchestrator")

    r = GatewayRunner.__new__(GatewayRunner)
    r.adapters = {Platform.TELEGRAM: RecordingAdapter()}
    r._profile_adapters = {"01-orchestrator": {}}
    r._primary_profile_name = "default"
    r._kanban_notifier_profile = "default"
    r._kanban_dispatcher_lock_handle = object()
    r.config = GatewayConfig(multiplex_profiles=True, profile_routes=[])
    return r


def _card_owned_by_anna() -> str:
    """A card ordered by Anna, with exactly the subscription the create path writes."""
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="Собрать отчёт", assignee="03-software-engineer",
                                 created_by="person/anna")
        for target in kc.notify_targets("person/anna"):
            kbn.add_notify_sub(conn, task_id=task_id, **target)
        kb.complete_task(conn, task_id, result="Отчёт готов")
    return task_id


def test_customer_subscription_delivers_into_the_customers_chat(runner, caplog):
    task_id = _card_owned_by_anna()

    with caplog.at_level(logging.DEBUG, logger="gateway.kanban_watchers_notifier"):
        rows = _notifier_collect(runner, kb, notifier_profile="default", gc_due=False, gc_retention_days=30)
        assert [row["task"].id for row in rows] == [task_id], "the customer sub must be claimed"
        for row in rows:
            asyncio.run(_KanbanNotification(runner, row, platform_cls=Platform, sub_fail_counts={}).deliver())

    adapter = runner.adapters[Platform.TELEGRAM]
    assert adapter.sent, "nothing reached the transport"
    # The recipient is the PERSON's chat from the bindings registry, not the creating session's.
    assert {chat_id for chat_id, _text, _kw in adapter.sent} == {"555000111"}
    assert "kanban notifier: delivered" in caplog.text

    # The cursor advanced: the same event is not re-delivered on the next tick.
    assert _notifier_collect(runner, kb, notifier_profile="default",
                             gc_due=False, gc_retention_days=30) == []
