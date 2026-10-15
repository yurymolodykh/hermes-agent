"""Tests for the extracted GatewayKanbanWatchersMixin (god-file Phase 3).

The kanban watcher loops were lifted out of gateway/run.py into a mixin that
GatewayRunner inherits. These tests confirm the mixin exposes the methods and
that GatewayRunner picks them up via the MRO (behavior-neutral relocation).

Also hosts focused behavioral tests of the kanban notifier's artifact-delivery
helper (``_deliver_kanban_artifacts``), including the F9 content-addressed
dedup (t_85a095e0): the same deliverable shipped twice — once from the
scratch/attachments copy and once from the durable copy — must be uploaded to
a channel exactly ONCE.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.kanban_watchers import GatewayKanbanWatchersMixin
from gateway.run import GatewayRunner

KANBAN_METHODS = [
    "_kanban_notifier_watcher",
    "_kanban_dispatcher_watcher",
    "_kanban_advance",
    "_kanban_unsub",
    "_kanban_rewind",
    "_deliver_kanban_artifacts",
]


def test_mixin_defines_kanban_methods():
    for m in KANBAN_METHODS:
        assert hasattr(GatewayKanbanWatchersMixin, m), f"mixin missing {m}"


class _ArtifactAdapter:
    """Minimal adapter recording artifact uploads for _deliver_kanban_artifacts."""

    def __init__(self):
        self.documents = []
        self.images = []

    def extract_local_files(self, text):
        import re

        return re.findall(r"(?m)^\s*([A-Za-z]:[^\s\n]+\.\w+)\s*$", text or ""), []

    async def send_document(self, chat_id, file_path, metadata=None):
        self.documents.append(str(file_path))

    async def send_multiple_images(self, chat_id, images, metadata=None):
        self.images.extend(img[0] for img in images)

    async def send_video(self, chat_id, video_path, metadata=None):
        self.documents.append(str(video_path))


def _make_mixin_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_dispatcher_lock_handle = object()
    runner._kanban_sub_fail_counts = {}
    runner._kanban_branch_summary_fail_counts = {}
    runner._profile_adapters = {}
    return runner


@pytest.mark.asyncio
async def test_deliver_kanban_artifacts_dedup_same_file_two_paths(tmp_path):
    """F9: один и тот же файл (одинаковое имя+содержимое) в двух разных путях
    artifacts — в канал уходит РОВНО один раз.

    Ровно сценарий sno-economics R4: воркер сдал и scratch-копию
    (kanban/attachments/<task>/ref_midels.md), и durable-копию
    (profiles/.../durable/.../ref_midels.md) — оба в artifacts. Оба должны
    быть загружены один раз, а не дважды."""
    scratch = tmp_path / "attachments" / "t_8c4528dc"
    durable = tmp_path / "durable" / "rgz" / "sno-economics-v2" / "research"
    scratch.mkdir(parents=True)
    durable.mkdir(parents=True)
    content = "# Мидели A (м²) — 8 референсных группировок\nref_midels content\n"
    (scratch / "ref_midels.md").write_text(content, encoding="utf-8")
    (durable / "ref_midels.md").write_text(content, encoding="utf-8")

    adapter = _ArtifactAdapter()
    runner = _make_mixin_runner(adapter)
    payload = {
        "artifacts": [
            str(scratch / "ref_midels.md"),
            str(durable / "ref_midels.md"),
        ],
        "summary": "Результат",
    }
    task = SimpleNamespace(result="")

    await runner._deliver_kanban_artifacts(
        adapter=adapter,
        chat_id="tg-1",
        metadata={},
        event_payload=payload,
        task=task,
    )

    assert adapter.documents == [str(scratch / "ref_midels.md")], (
        "дупликат по имени+содержимому должен отсеиваться — загружен только "
        f"первый путь: {adapter.documents}"
    )


@pytest.mark.asyncio
async def test_deliver_kanban_artifacts_same_content_diff_name_not_deduped(tmp_path):
    """F9: разные имена файлов с одинаковым содержимым НЕ схлопываются —
    дедупликация строго по (имя, sha256), чтобы не потерять легитимные
    разные файлы."""
    a = tmp_path / "report_v1.md"
    b = tmp_path / "report_v2.md"
    content = "identical bytes across names\n"
    a.write_text(content, encoding="utf-8")
    b.write_text(content, encoding="utf-8")

    adapter = _ArtifactAdapter()
    runner = _make_mixin_runner(adapter)
    payload = {"artifacts": [str(a), str(b)], "summary": "Результат"}
    task = SimpleNamespace(result="")

    await runner._deliver_kanban_artifacts(
        adapter=adapter,
        chat_id="tg-1",
        metadata={},
        event_payload=payload,
        task=task,
    )

    assert set(adapter.documents) == {str(a), str(b)}, (
        "одинаковое содержимое при РАЗНЫХ именах — оба файла должны дойти: "
        f"{adapter.documents}"
    )


@pytest.mark.asyncio
async def test_deliver_kanban_artifacts_same_name_diff_content_not_deduped(tmp_path):
    """F9: одинаковое имя при РАЗНОМ содержимом НЕ схлопывается — дедуп строго
    по (имя, sha256)."""
    # Force same basename via subdirs: A/result.md and B/result.md, diff bytes.
    d1 = tmp_path / "A"
    d2 = tmp_path / "B"
    d1.mkdir()
    d2.mkdir()
    (d1 / "result.md").write_text("AAAAAAAA\n", encoding="utf-8")
    (d2 / "result.md").write_text("BBBBBBBB\n", encoding="utf-8")

    adapter = _ArtifactAdapter()
    runner = _make_mixin_runner(adapter)
    payload = {"artifacts": [str(d1 / "result.md"), str(d2 / "result.md")], "summary": "Результат"}
    task = SimpleNamespace(result="")

    await runner._deliver_kanban_artifacts(
        adapter=adapter,
        chat_id="tg-1",
        metadata={},
        event_payload=payload,
        task=task,
    )

    assert set(adapter.documents) == {str(d1 / "result.md"), str(d2 / "result.md")}, (
        "одинаковое имя при РАЗНОМ содержимом — оба должны дойти: "
        f"{adapter.documents}"
    )


# ---------------------------------------------------------------------------
# do_not_deliver helper (t_01d91fbf / t_896ff347): the defensive read of the
# synthetic-readback delivery-suppression flag must never raise and must map
# every real-world stored value to the correct boolean.
# ---------------------------------------------------------------------------


def test_task_do_not_deliver_defensive_truth_table():
    from gateway.kanban_watchers import _task_do_not_deliver

    # None task row (e.g. task deleted / stale sub) -> not a delivery subject
    # reason, so False (deliver normally is NOT the case here — see the
    # helper's docstring: None -> False means "not flagged").
    assert _task_do_not_deliver(None) is False
    # Missing attribute (older cached model instances) -> False
    assert _task_do_not_deliver(SimpleNamespace()) is False
    # Default/ordinary stored value 0 -> False
    assert _task_do_not_deliver(SimpleNamespace(do_not_deliver=0)) is False
    assert _task_do_not_deliver(SimpleNamespace(do_not_deliver=False)) is False
    # Flagged synthetic readback -> True
    assert _task_do_not_deliver(SimpleNamespace(do_not_deliver=True)) is True
    assert _task_do_not_deliver(SimpleNamespace(do_not_deliver=1)) is True
