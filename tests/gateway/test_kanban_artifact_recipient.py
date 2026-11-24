"""An artifact handoff is a journalled decision, not a silent side effect.

Criterion 5 of card ``t_6aab4d66``: ``_deliver_kanban_artifacts`` used to upload a finished
card's file to whatever ``chat_id`` the subscription named, with no relation check to the card
and no record either way — so a file that never arrived was indistinguishable from a
deliberate refusal. Two claims are proven here:

* a recipient WITH a relation to the card (its customer) is delivered to, and the handoff
  leaves a ``delivered`` row in ``logs/notify-delivery.jsonl``;
* a recipient with NO relation is still served while card materials are shared (the owner's
  standing decision) and journalled as such — and, with the switch off, is refused with a row
  naming the reason instead of vanishing.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from gateway import kanban_delivery as kdel
from gateway import kanban_watchers
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


class _RecordingAdapter:
    """Stands in for a platform adapter: records what the transport was asked to send."""

    platform = type("P", (), {"value": "telegram"})()

    def __init__(self):
        self.documents: list[tuple[str, str]] = []

    def extract_local_files(self, text):
        return [], text

    async def send_document(self, *, chat_id, file_path, metadata=None):
        self.documents.append((chat_id, file_path))

    async def send_video(self, *, chat_id, video_path, metadata=None):
        self.documents.append((chat_id, video_path))

    async def send_multiple_images(self, *, chat_id, images, metadata=None):
        for url, _caption in images:
            self.documents.append((chat_id, url))


def _journals(home: Path) -> list[dict]:
    path = home / "logs" / "notify-delivery.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def registry(monkeypatch):
    """``person/anna`` is on the card; ``person/bob`` is a colleague with no stamp on it."""

    class _FakeRegistry:
        def subjects_of(self, subject):
            return {
                "person/anna": ("person/anna", "tg:555000111"),
                "person/bob": ("person/bob", "tg:555000222"),
            }.get(str(subject or "").strip(), ())

        def personal_profiles_of(self, subject):
            return ()

    import hermes_acl_bindings as hb

    monkeypatch.setattr(hb, "load_cached", lambda: _FakeRegistry())
    import hermes_acl_results as hr

    monkeypatch.setattr(hr, "_multi_user", lambda: True)
    monkeypatch.setattr(hr, "_is_owner", lambda subject: False)
    return _FakeRegistry()


def _card(created_by: str = "person/anna"):
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="Собрать отчёт", assignee="03-software-engineer",
                                 created_by=created_by)
        return task_id, kb.get_task(conn, task_id)


def _deliver(adapter, task, *, chat_id: str, artifact: Path, subject: str, event_kind="completed"):
    class _Runner(kanban_watchers.GatewayKanbanWatchersMixin):
        pass

    asyncio.run(_Runner()._deliver_kanban_artifacts(
        adapter=adapter, chat_id=chat_id, metadata={"customer_subject": subject},
        event_payload={"artifacts": [str(artifact)]}, task=None,
        relation_task=task, event_kind=event_kind))


def test_the_cards_customer_receives_the_artifact_and_the_handoff_is_journalled(board, registry, tmp_path):
    task_id, task = _card()
    artifact = tmp_path / "report.pdf"
    artifact.write_bytes(b"%PDF-1.4 report")
    adapter = _RecordingAdapter()

    _deliver(adapter, task, chat_id="555000111", artifact=artifact, subject="person/anna")

    assert adapter.documents == [("555000111", str(artifact))], "the customer's chat got the file"
    (row,) = _journals(board)
    assert row["task_id"] == task_id
    assert row["chat_id"] == "555000111"
    assert row["subject"] == "person/anna"
    assert row["verdict"] == kdel.VERDICT_DELIVERED
    assert row["reason"] == "recipient-on-card"
    assert row["artifacts"] == 1


def test_a_colleague_still_receives_shared_materials_and_the_reason_is_named(board, registry, tmp_path):
    _task_id, task = _card()
    artifact = tmp_path / "report.pdf"
    artifact.write_bytes(b"%PDF-1.4 report")
    adapter = _RecordingAdapter()

    _deliver(adapter, task, chat_id="555000222", artifact=artifact, subject="person/bob")

    assert adapter.documents == [("555000222", str(artifact))], "shared materials are not withheld"
    (row,) = _journals(board)
    assert row["verdict"] == kdel.VERDICT_DELIVERED
    assert "materials-shared" in row["reason"], "the mismatch is stated, not hidden"
    assert row["subject"] == "person/bob"


def test_withholding_shared_materials_refuses_with_a_journalled_reason(board, registry, tmp_path, monkeypatch):
    _task_id, task = _card()
    artifact = tmp_path / "report.pdf"
    artifact.write_bytes(b"%PDF-1.4 report")
    adapter = _RecordingAdapter()
    monkeypatch.setattr(kdel, "materials_shared", lambda: False)

    _deliver(adapter, task, chat_id="555000222", artifact=artifact, subject="person/bob")

    assert adapter.documents == [], "nobody outside the card receives it"
    (row,) = _journals(board)
    assert row["verdict"] == kdel.VERDICT_REFUSED
    assert row["reason"] == "recipient-not-on-card; materials-not-shared"
    assert row["chat_id"] == "555000222"


def test_an_acl_off_installation_records_the_legacy_answer(board, registry, tmp_path, monkeypatch):
    import hermes_acl_results as hr

    monkeypatch.setattr(hr, "_multi_user", lambda: False)
    _task_id, task = _card()
    artifact = tmp_path / "report.pdf"
    artifact.write_bytes(b"%PDF-1.4 report")
    adapter = _RecordingAdapter()

    _deliver(adapter, task, chat_id="125805788", artifact=artifact, subject="")

    assert adapter.documents == [("125805788", str(artifact))], "single-user delivery is unchanged"
    (row,) = _journals(board)
    assert row["verdict"] == kdel.VERDICT_DELIVERED
    assert row["reason"] == "acl-off; no relation check"
