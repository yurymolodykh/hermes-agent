"""Unit tests for inbound document persistence to the ~/workspaces/inbox.

Covers the best-effort save of raw OpenAI file/input_file bytes from the API
server's multimodal normalization path (`_save_inbound_document` and its
integration inside `_anonymize_inline_document_part`):

* round-trip: base64 document → file in inbox with the ORIGINAL basename and
  matching sha256;
* path traversal: ../evil.xlsx resolves to evil.xlsx inside the inbox only;
* collision: a second upload with the same name gets an increasing (N) suffix
  and never overwrites the first;
* anonymization still works when `privacy.anonymize_documents` is enabled;
* the gateway.max_inbound_media_bytes cap is honoured (oversized → not saved);
* writes are best-effort: a save failure does not fail the chat turn.
"""

import base64
import hashlib
from pathlib import Path

import pytest

from gateway.platforms.api_server import (
    APIServerAdapter,
    _normalize_multimodal_content,
    _save_inbound_document,
)

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from unittest.mock import MagicMock, patch
from gateway.config import PlatformConfig


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------


class TestInboxDir:
    def test_default_dir_is_home_workspaces_inbox(self, monkeypatch):
        from gateway.platforms import api_server

        monkeypatch.delenv("HERMES_INBOX_DIR", raising=False)
        assert str(api_server._inbox_dir()) == str(Path.home() / "workspaces" / "inbox")

    def test_env_override(self, monkeypatch, tmp_path):
        from gateway.platforms import api_server

        custom = tmp_path / "custom-inbox"
        monkeypatch.setenv("HERMES_INBOX_DIR", str(custom))
        assert api_server._inbox_dir() == custom

    def test_blank_env_ignored(self, monkeypatch):
        from gateway.platforms import api_server

        monkeypatch.setenv("HERMES_INBOX_DIR", "   ")
        assert api_server._inbox_dir() == Path(api_server.DEFAULT_INBOX_DIR)


class TestSaveInboundDocument:
    def test_saves_with_original_basename_and_matching_hash(self, tmp_path, monkeypatch):
        raw = b"fake-xlsx-bytes-for-inbox-save-test"
        monkeypatch.setenv("HERMES_INBOX_DIR", str(tmp_path / "inbox"))

        saved = _save_inbound_document(raw, "Отчёт 2026.xlsx")

        assert saved is not None
        target = Path(saved)
        assert target.parent == tmp_path / "inbox"
        assert target.name == "Отчёт 2026.xlsx"
        assert hashlib.sha256(target.read_bytes()).hexdigest() == hashlib.sha256(raw).hexdigest()

    def test_path_traversal_is_neutralized(self, tmp_path, monkeypatch):
        raw = b"evil-content"
        monkeypatch.setenv("HERMES_INBOX_DIR", str(tmp_path / "inbox"))

        saved = _save_inbound_document(raw, "../evil.xlsx")

        assert saved is not None
        target = Path(saved)
        # Saved inside the inbox with only the basename.
        assert target.parent == tmp_path / "inbox"
        assert target.name == "evil.xlsx"
        # Nothing escaped the inbox.
        assert not (tmp_path / "evil.xlsx").exists()

    def test_absolute_path_is_reduced_to_basename(self, tmp_path, monkeypatch):
        raw = b"abs-content"
        monkeypatch.setenv("HERMES_INBOX_DIR", str(tmp_path / "inbox"))

        saved = _save_inbound_document(raw, "C:\\Windows\\system32\\drivers\\etc\\hosts")

        assert saved is not None
        target = Path(saved)
        assert target.parent == tmp_path / "inbox"
        assert target.name == "hosts"
        assert (tmp_path / "inbox" / "hosts").exists()

    def test_collision_gets_suffix_and_never_overwrites(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_INBOX_DIR", str(tmp_path / "inbox"))
        raw1, raw2 = b"first", b"second"

        first = _save_inbound_document(raw1, "report.xlsx")
        second = _save_inbound_document(raw2, "report.xlsx")

        assert first is not None
        assert second is not None
        first_path, second_path = Path(first), Path(second)
        assert first_path.name == "report.xlsx"
        assert second_path.name == "report (1).xlsx"
        assert first_path.read_bytes() == raw1
        assert second_path.read_bytes() == raw2

    def test_multiple_collisions_increment_suffix(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_INBOX_DIR", str(tmp_path / "inbox"))
        paths = [
            _save_inbound_document(data, "doc.txt")
            for data in (b"a", b"b", b"c")
        ]
        names = [Path(p).name for p in paths if p]
        assert names == ["doc.txt", "doc (1).txt", "doc (2).txt"]

    def test_inbox_directory_is_created(self, tmp_path, monkeypatch):
        inbox = tmp_path / "a" / "b" / "inbox"
        monkeypatch.setenv("HERMES_INBOX_DIR", str(inbox))

        saved = _save_inbound_document(b"x", "note.txt")

        assert saved is not None
        assert inbox.is_dir()

    def test_oversized_document_is_not_saved(self, tmp_path, monkeypatch, capsys=None):
        inbox = tmp_path / "inbox"
        monkeypatch.setenv("HERMES_INBOX_DIR", str(inbox))
        monkeypatch.setattr(
            "gateway.platforms.api_server._inbound_document_over_limit",
            lambda size: True,
        )

        saved = _save_inbound_document(b"way-too-big", "big.bin")

        assert saved is None
        assert not (inbox / "big.bin").exists()

    def test_cap_disabled_saves_any_size(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_INBOX_DIR", str(tmp_path / "inbox"))
        monkeypatch.setattr(
            "gateway.platforms.api_server._inbound_document_over_limit",
            lambda size: False,
        )

        saved = _save_inbound_document(b"tiny", "t.txt")

        assert saved is not None
        assert Path(saved).read_bytes() == b"tiny"

    def test_unwritable_inbox_does_not_raise(self, tmp_path, monkeypatch):
        inbox = tmp_path / "inbox"
        monkeypatch.setenv("HERMES_INBOX_DIR", str(inbox))
        # Pretend mkdir fails — the helper must swallow it and return None.
        monkeypatch.setattr(
            "gateway.platforms.api_server._inbox_dir",
            lambda: (_ for _ in ()).throw(PermissionError("denied")),
        )

        assert _save_inbound_document(b"x", "n.txt") is None


# ---------------------------------------------------------------------------
# Integration through the multimodal normalizer (anonymization intact)
# ---------------------------------------------------------------------------


class TestInboxSaveThroughNormalization:
    def _make_document_part(self, filename: str, raw: bytes) -> dict:
        return {
            "type": "file",
            "file": {
                "filename": filename,
                "file_data": "data:text/plain;base64," + base64.b64encode(raw).decode(),
            },
        }

    def test_file_part_saved_to_inbox_and_anonymized(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_DOCUMENT_ANONYMIZATION", "1")
        monkeypatch.setenv("HERMES_INBOX_DIR", str(tmp_path / "inbox"))
        raw = "Иванов Иван Иванович получает 123 000 рублей".encode()

        result = _normalize_multimodal_content(
            [
                {"type": "text", "text": "Обработай документ"},
                self._make_document_part("contract.txt", raw),
            ]
        )

        # Anonymization untouched: PII is gone from what reaches the model.
        assert isinstance(result, str)
        assert "Иванов Иван Иванович" not in result
        assert "123 000" not in result
        assert "person1" not in result or "SUM1" in result
        # Raw bytes persisted with original name and matching hash.
        target = tmp_path / "inbox" / "contract.txt"
        assert target.exists()
        assert hashlib.sha256(target.read_bytes()).hexdigest() == hashlib.sha256(raw).hexdigest()

    def test_save_failure_does_not_fail_the_turn(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_DOCUMENT_ANONYMIZATION", "1")
        monkeypatch.setenv("HERMES_INBOX_DIR", str(tmp_path / "inbox"))
        raw = "Петров Пётр Петрович — 5 000 рублей".encode()

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr("gateway.platforms.api_server._save_inbound_document", _boom)

        # Even if the save raises (shouldn't — it's defensive), normalization
        # still yields the anonymized text and never blows up the chat.
        result = _normalize_multimodal_content(
            [
                {"type": "text", "text": "прочитай"},
                self._make_document_part("petrov.txt", raw),
            ]
        )
        assert "Петров Пётр Петрович" not in result
        assert "5 000" not in result


# ---------------------------------------------------------------------------
# HTTP integration — a real aiohttp request to the live adapter handler
# ---------------------------------------------------------------------------


def _make_adapter() -> "APIServerAdapter":
    return APIServerAdapter(PlatformConfig(enabled=True))


def _create_app(adapter) -> web.Application:
    from gateway.platforms.api_server import (
        cors_middleware,
        security_headers_middleware,
    )

    app = web.Application()
    app["api_server_adapter"] = adapter
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    return app


class TestInboxSaveThroughHTTP:
    @pytest.mark.asyncio
    async def test_file_part_saved_to_inbox_on_chat_completions(self, tmp_path, monkeypatch):
        """POST /v1/chat/completions with a base64 file part writes the raw
        document to the inbox through the real HTTP handler."""
        from gateway.platforms.api_server import APIServerAdapter  # noqa: F401  (type use only)

        monkeypatch.setenv("HERMES_DOCUMENT_ANONYMIZATION", "1")
        monkeypatch.setenv("HERMES_INBOX_DIR", str(tmp_path / "inbox"))
        raw = "Секретный договор — 9 999 999 рублей".encode()

        adapter = _make_adapter()
        app = _create_app(adapter)

        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new=MagicMock()) as mock_run:
                async def _stub(**kwargs):
                    mock_run.captured = kwargs
                    return (
                        {"final_response": "ok", "messages": [], "api_calls": 1},
                        {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                    )

                mock_run.side_effect = _stub

                resp = await cli.post(
                    "/v1/chat/completions",
                    json={
                        "model": "hermes-agent",
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": "прочитай договор"},
                                    {
                                        "type": "file",
                                        "file": {
                                            "filename": "dogovor.txt",
                                            "file_data": "data:text/plain;base64,"
                                            + base64.b64encode(raw).decode(),
                                        },
                                    },
                                ],
                            }
                        ],
                    },
                )

            assert resp.status == 200, await resp.text()

        target = tmp_path / "inbox" / "dogovor.txt"
        assert target.exists()
        assert hashlib.sha256(target.read_bytes()).hexdigest() == hashlib.sha256(raw).hexdigest()

