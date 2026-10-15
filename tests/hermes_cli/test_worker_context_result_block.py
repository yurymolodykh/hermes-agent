"""Integration tests: result-block contract renders compactly in worker context.

Parent handoffs written in the RESULT-BLOCK format must reach the child worker
as the compact structured block WITHOUT the redundant full ``_metadata_`` JSON
dump (facts already carried by the block), while a legacy prose summary must
keep the old full-text + metadata rendering (backward compatibility).

These assert the *reduction* (Н3 goal: −30% input on routine paths), not just
pass-through:
  * RESULT-BLOCK parent + structured metadata → block present, metadata JSON NOT
    duplicated for keys already covered by the block.
  * legacy parent → summary verbatim + metadata JSON dump (unchanged).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.handoff_result_block import chat_line, metadata_diff


pytestmark = pytest.mark.usefixtures("isolated_kanban_assignee_registry")


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _complete_parent(conn, *, summary, metadata=None):
    pid = kb.create_task(conn, title="parent", assignee="worker")
    kb.complete_task(conn, pid, summary=summary, metadata=metadata or {})
    return pid


def _child_ctx(conn, pid):
    child = kb.create_task(conn, title="child", parents=[pid], assignee="worker")
    return kb.build_worker_context(conn, child)


RESULT_BLOCK_SUMMARY = (
    "RESULT-BLOCK v1\n"
    "FACTS:\n"
    "- p50 25 мин (было 40)\n"
    "- 3 артефакта доставлено\n"
    "PATHS:\n"
    "- C:/tmp/report.md\n"
    "LIMITATIONS:\n"
    "- manual_review: экран недоступен\n"
)


def test_parent_result_block_skips_redundant_metadata_dump(kanban_home):
    """Structured facts in the block hide the identical metadata JSON dump."""
    conn = kb.connect()
    try:
        meta = {
            "changed_files": ["a.py"],
            "tests_run": 12,
            "findings": ["fixed x"],
            "artifacts": ["C:/tmp/report.md"],
            "model": "deepseek-v4-flash",
        }
        pid = _complete_parent(conn, summary=RESULT_BLOCK_SUMMARY, metadata=meta)
        ctx = _child_ctx(conn, pid)
        # block is present
        assert "RESULT-BLOCK v1" in ctx
        assert "C:/tmp/report.md" in ctx
        # covered structured keys do NOT produce a giant JSON dump
        assert "_metadata_:" not in ctx
        # but the bare model key (not covered by block) may still ride through
        # as part of the block render — we only assert no JSON dump of facts.
    finally:
        conn.close()


def test_parent_legacy_summary_keeps_full_render(kanban_home):
    """Legacy parent handoffs render exactly as before (summary + metadata JSON)."""
    conn = kb.connect()
    try:
        summary = "Первая строка короткая\nВторая строка с деталями\nТретья строка"
        meta = {"tests_run": 5, "changed_files": ["x.py"]}
        pid = _complete_parent(conn, summary=summary, metadata=meta)
        ctx = _child_ctx(conn, pid)
        assert summary in ctx
        assert "Parent task results" in ctx
        assert "_metadata_:" in ctx  # legacy path keeps the JSON dump
        assert '"tests_run": 5' in ctx
    finally:
        conn.close()


def test_parent_legacy_no_meta_keeps_plain(kanban_home):
    conn = kb.connect()
    try:
        summary = "обычный результат"
        pid = _complete_parent(conn, summary=summary)
        ctx = _child_ctx(conn, pid)
        assert "обычный результат" in ctx
        assert "_metadata_:" not in ctx
    finally:
        conn.close()


def test_parent_inline_block_skips_metadata_dump(kanban_home):
    conn = kb.connect()
    try:
        summary = "RESULT-BLOCK v1 FACTS: a; b PATHS: C:/tmp/x LIMITATIONS: l"
        meta = {"findings": ["a", "b"]}
        pid = _complete_parent(conn, summary=summary, metadata=meta)
        ctx = _child_ctx(conn, pid)
        assert "RESULT-BLOCK" in ctx
        assert "_metadata_:" not in ctx
    finally:
        conn.close()


def test_chat_line_prefers_block_fact(kanban_home):
    conn = kb.connect()
    try:
        pid = _complete_parent(conn, summary=RESULT_BLOCK_SUMMARY)
        _child_ctx(conn, pid)  # ensure db path works
        assert chat_line(RESULT_BLOCK_SUMMARY) == "p50 25 мин (было 40)"
    finally:
        conn.close()


def test_metadata_diff_helper_contract(kanban_home):
    """The metadata fields the worker context avoids duplicating."""
    meta = {
        "changed_files": ["a.py"],
        "tests_run": 12,
        "artifacts": ["C:/tmp/report.md"],
        "uncovered": "keep me",
    }
    diff = metadata_diff(meta)
    assert "changed_files" in diff
    assert "tests_run" in diff
    assert "artifacts" in diff
    assert "uncovered" not in diff
