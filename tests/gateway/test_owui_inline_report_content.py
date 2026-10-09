"""api_server (Open WebUI) inline report delivery."""

import json
import sqlite3

from gateway import kanban_watchers as kw
from gateway.kanban_origin_delivery import stage_terminal
from gateway.origin_delivery_worker import drain_openwebui
from gateway.origin_outbox import status

RAW = "98d69f26-8263-4e1c-84a5-6d9fcdbf593d"


def _registry(tmp_path, owner="owner-A"):
    registry = tmp_path / "webui.db"
    with sqlite3.connect(registry) as db:
        db.execute("CREATE TABLE chat (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, chat JSON NOT NULL)")
        db.execute("INSERT INTO chat VALUES (?, ?, ?)", (RAW, owner, json.dumps({"history": {
            "messages": {"u": {"id": "u", "role": "user", "parentId": None}}, "currentId": "u"}})))
    return registry


def _sub(owner="owner-A"):
    return {"platform": "api_server", "chat_id": "owui-" + RAW, "thread_id": "",
            "notifier_profile": "01-orchestrator", "user_id": owner}


def test_owui_artifact_text_reads_markdown_report(tmp_path):
    art = tmp_path / "report.md"
    art.write_text("# Итог\n\nВнешних ссылок НЕ обнаружено.", encoding="utf-8")
    text = kw._owui_artifact_text([str(art)])
    assert "# Итог" in text and "Внешних ссылок" in text
    assert "report.md" in text
    # Missing file / non-text extension / empty -> ""
    assert kw._owui_artifact_text([str(tmp_path / "nope.md")]) == ""
    assert kw._owui_artifact_text(["C:/x/report.png"]) == ""
    assert kw._owui_artifact_text(None) == ""


def test_owui_completed_text_embeds_summary_and_artifact(tmp_path):
    art = tmp_path / "report.md"
    art.write_text("Полный текст отчёта".center(400, "~"), encoding="utf-8")
    payload = {"summary": "2-й проход завершён", "artifacts": [str(art)]}
    msg = kw._owui_completed_text("t_abc", "Задача", payload, "fallback ✔ ... done")
    assert msg.startswith("✅ Kanban t_abc done — Задача")
    assert "2-й проход завершён" in msg
    assert "Полный текст отчёта" in msg
    # No richer payload -> fallback preserved.
    long_fallback = "✔ [default] @04-web-researcher Kanban t_abc done — Задача\nконтекст"
    assert kw._owui_completed_text("t_abc", "Задача", None, long_fallback) == long_fallback
    assert kw._owui_completed_text("t_abc", "Задача", {}, long_fallback) == long_fallback


def test_end_to_end_report_reaches_chat_inline(tmp_path):
    """The full report text lands inside the persisted chat message."""
    registry = _registry(tmp_path)
    outbox = tmp_path / "outbox.db"
    art = tmp_path / "report.md"
    art.write_text("СПИСОК 1\n-ref\nСПИСОК 2\n-blog\n" * 20, encoding="utf-8")
    payload = {"summary": "2-й проход завершён", "artifacts": [str(art)]}
    fallback = "✔ [default] @worker Kanban t_x done — Задача"
    msg = kw._owui_completed_text("t_x", "Задача", payload, fallback)
    sub = _sub()
    state, event = stage_terminal(outbox, registry, sub, "01-orchestrator", "board-A", 42, msg)
    assert state == "pending"
    assert drain_openwebui(outbox, registry)["acknowledged"] == 1
    assert status(outbox, event) == "acknowledged"
    with sqlite3.connect(registry) as db:
        history = json.loads(db.execute("SELECT chat FROM chat WHERE id=?", (RAW,)).fetchone()[0])["history"]
    content = history["messages"][event]["content"]
    assert "СПИСОК 1" in content and "2-й проход завершён" in content
    assert len(content) > 200  # full report, not the short fallback one-liner
