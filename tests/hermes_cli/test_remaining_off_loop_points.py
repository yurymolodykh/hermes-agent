"""The five remaining blocking points of §5.1 run on the offload seam, and inline when it is off.

Each route below used to do CPU/disk work directly on the event loop: base64 decode+stage of a
25 MiB upload, base64 encode of a synthesized 25 MiB clip, base64 encode of up to 25 MiB of
proxied remote media, base64 encode of a 16 MiB SSH read, and tailing a rotated action log.

The contract pinned here is thread identity, not latency: with the switch on the blocking helper
must run on a worker thread; with ``HERMES_HANDLER_OFFLOAD=0`` it must run on the loop thread
(the rollback). A latency assertion would be vacuous — the client's own clock cannot advance on a
frozen loop.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from hermes_cli.web_routers import handler_offload as ho


@pytest.fixture(autouse=True)
def _reset_seam(monkeypatch):
    monkeypatch.delenv("HERMES_HANDLER_OFFLOAD", raising=False)
    ho.HANDLER_OFFLOAD.reset()
    yield
    ho.HANDLER_OFFLOAD.reset()


def _run(coro):
    return asyncio.run(coro)


def _arm(monkeypatch, enabled: bool) -> None:
    monkeypatch.setenv("HERMES_HANDLER_OFFLOAD", "1" if enabled else "0")
    ho.HANDLER_OFFLOAD.install(None, workers=4)


# ── №2 audio.py transcribe_audio_upload: b64decode + temp write ───────────────────


def test_transcribe_decode_and_stage_runs_off_the_loop(monkeypatch, tmp_path):
    from hermes_cli.web_routers import audio as audio_router

    _arm(monkeypatch, True)
    loop_thread = threading.get_ident()
    seen: list[int] = []
    staged = tmp_path / "voice.webm"

    def _fake_stage(encoded, suffix):
        seen.append(threading.get_ident())
        staged.write_bytes(b"x")
        return str(staged)

    monkeypatch.setattr(audio_router, "_decode_and_stage", _fake_stage)

    async def _fake_scoped(profile, fn):
        return {"success": True, "transcript": "hi", "provider": "p"}

    monkeypatch.setattr(audio_router, "_run_config_scoped", _fake_scoped)

    payload = audio_router.AudioTranscriptionRequest(
        data_url="data:audio/webm;base64," + "AAAA", mime_type="audio/webm"
    )
    result = _run(audio_router.transcribe_audio_upload(payload))

    assert result == {"ok": True, "transcript": "hi", "provider": "p"}
    assert seen and seen[0] != loop_thread, "decode/stage ran on the event loop"


def test_transcribe_decode_and_stage_is_inline_when_the_switch_is_off(monkeypatch, tmp_path):
    from hermes_cli.web_routers import audio as audio_router

    _arm(monkeypatch, False)
    loop_thread = threading.get_ident()
    seen: list[int] = []
    staged = tmp_path / "voice.webm"

    def _fake_stage(encoded, suffix):
        seen.append(threading.get_ident())
        staged.write_bytes(b"x")
        return str(staged)

    monkeypatch.setattr(audio_router, "_decode_and_stage", _fake_stage)

    async def _fake_scoped(profile, fn):
        return {"success": True, "transcript": "", "provider": None}

    monkeypatch.setattr(audio_router, "_run_config_scoped", _fake_scoped)

    payload = audio_router.AudioTranscriptionRequest(
        data_url="data:audio/webm;base64," + "AAAA", mime_type="audio/webm"
    )
    _run(audio_router.transcribe_audio_upload(payload))
    assert seen == [loop_thread], "switch off must run the pre-change inline path"


def test_invalid_audio_payload_still_answers_400(monkeypatch):
    """Validation moved into the worker; the HTTPException must cross the seam unchanged."""
    from hermes_cli.web_routers import audio as audio_router
    from fastapi import HTTPException

    _arm(monkeypatch, True)
    payload = audio_router.AudioTranscriptionRequest(
        data_url="data:audio/webm;base64,%%%%", mime_type="audio/webm"
    )
    with pytest.raises(HTTPException) as excinfo:
        _run(audio_router.transcribe_audio_upload(payload))
    assert excinfo.value.status_code == 400


# ── №3-tail audio.py speak_text: base64 encode ───────────────────────────────────


def test_speak_text_encodes_off_the_loop(monkeypatch, tmp_path):
    from hermes_cli.web_routers import audio as audio_router

    _arm(monkeypatch, True)
    loop_thread = threading.get_ident()
    seen: list[int] = []
    clip = tmp_path / "clip.mp3"
    clip.write_bytes(b"\x00" * 1024)

    async def _fake_scoped(profile, fn):
        return json.dumps({"success": True, "file_path": str(clip), "provider": "p"})

    monkeypatch.setattr(audio_router, "_run_config_scoped", _fake_scoped)

    real_encode = audio_router._encode_b64

    def _spy(data):
        seen.append(threading.get_ident())
        return real_encode(data)

    monkeypatch.setattr(audio_router, "_encode_b64", _spy)

    result = _run(audio_router.speak_text(audio_router.TTSSpeakRequest(text="hi")))
    assert result["data_url"].startswith("data:audio/mpeg;base64,")
    assert seen and seen[0] != loop_thread, "base64 encode ran on the event loop"
    assert not clip.exists(), "the temp clip must still be unlinked"


def test_speak_text_encodes_inline_when_the_switch_is_off(monkeypatch, tmp_path):
    from hermes_cli.web_routers import audio as audio_router

    _arm(monkeypatch, False)
    loop_thread = threading.get_ident()
    seen: list[int] = []
    clip = tmp_path / "clip.mp3"
    clip.write_bytes(b"\x00" * 1024)

    async def _fake_scoped(profile, fn):
        return json.dumps({"success": True, "file_path": str(clip), "provider": "p"})

    monkeypatch.setattr(audio_router, "_run_config_scoped", _fake_scoped)

    real_encode = audio_router._encode_b64

    def _spy(data):
        seen.append(threading.get_ident())
        return real_encode(data)

    monkeypatch.setattr(audio_router, "_encode_b64", _spy)

    _run(audio_router.speak_text(audio_router.TTSSpeakRequest(text="hi")))
    assert seen == [loop_thread], "switch off must encode inline, as before the change"
    assert not clip.exists(), "the temp clip must still be unlinked"


# ── №4 files.py proxy_remote_media: base64 encode ────────────────────────────────


def _fake_http_client(monkeypatch, content: bytes, content_type: str) -> None:
    import httpx

    class _Resp:
        status_code = 200

        def __init__(self, body: bytes, ctype: str):
            self.content = body
            self.headers = {"content-type": ctype}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _Resp(content, content_type)

    monkeypatch.setattr(httpx, "AsyncClient", _Client, raising=False)


@pytest.mark.parametrize("enabled", [True, False])
def test_proxy_remote_media_encodes_on_the_seam(monkeypatch, enabled):
    from hermes_cli.web_routers import files as files_router

    _arm(monkeypatch, enabled)
    loop_thread = threading.get_ident()
    seen: list[int] = []

    real = files_router._b64_data_url

    def _spy(data, mime_type):
        seen.append(threading.get_ident())
        return real(data, mime_type)

    monkeypatch.setattr(files_router, "_require_token", lambda request: None, raising=False)
    monkeypatch.setattr(files_router, "_b64_data_url", _spy)
    _fake_http_client(monkeypatch, b"\x89PNG\r\n\x1a\n" + b"0" * 8, "image/png")

    result = _run(files_router.proxy_remote_media("https://v3.fal.media/media/abc123", None))
    assert result["data_url"].startswith("data:image/png;base64,")
    if enabled:
        assert seen and seen[0] != loop_thread, "base64 encode ran on the event loop"
    else:
        assert seen == [loop_thread], "switch off must encode inline, as before the change"


# ── №6 files.py fs_read_data_url, remote branch: base64 encode ───────────────────


@pytest.mark.parametrize("enabled", [True, False])
def test_fs_read_data_url_remote_branch_encodes_on_the_seam(monkeypatch, enabled):
    from hermes_cli.web_routers import files as files_router

    _arm(monkeypatch, enabled)
    loop_thread = threading.get_ident()
    seen: list[int] = []

    class _Backend:
        def read_bytes(self, path, max_bytes=None):
            return b"remote-bytes", 12, "/srv/ssh/chart.png"

    async def _no_session(session_id, profile):
        return None

    real = files_router._b64_data_url

    def _spy(data, mime_type):
        seen.append(threading.get_ident())
        return real(data, mime_type)

    monkeypatch.setattr(files_router, "_fs_owned_session", _no_session)
    monkeypatch.setattr(files_router, "_remote_fs_backend", lambda path, profile: _Backend())
    monkeypatch.setattr(files_router, "_b64_data_url", _spy)

    result = _run(files_router.fs_read_data_url("/srv/ssh/chart.png", None))
    assert result["dataUrl"].startswith("data:image/png;base64,")
    if enabled:
        assert seen and seen[0] != loop_thread, "base64 encode ran on the event loop"
    else:
        assert seen == [loop_thread], "switch off must encode inline, as before the change"


# ── №7 actions.py get_action_status: _tail_lines ─────────────────────────────────


def test_action_status_tails_off_the_loop(monkeypatch, tmp_path):
    from hermes_cli.web_routers import actions as actions_router

    _arm(monkeypatch, True)
    loop_thread = threading.get_ident()
    seen: list[int] = []
    log = tmp_path / "gateway-restart.log"
    log.write_text("line\n", encoding="utf-8")

    def _spy(path: Path, n: int):
        seen.append(threading.get_ident())
        return ["line"]

    monkeypatch.setattr(actions_router, "_server_path", lambda name: tmp_path)
    monkeypatch.setattr(actions_router, "_tail_lines", _spy)

    result = _run(actions_router.get_action_status("gateway-restart"))
    assert result["lines"] == ["line"]
    assert seen and seen[0] != loop_thread, "_tail_lines ran on the event loop"


def test_action_status_tails_inline_when_the_switch_is_off(monkeypatch, tmp_path):
    from hermes_cli.web_routers import actions as actions_router

    _arm(monkeypatch, False)
    loop_thread = threading.get_ident()
    seen: list[int] = []

    def _spy(path: Path, n: int):
        seen.append(threading.get_ident())
        return []

    monkeypatch.setattr(actions_router, "_server_path", lambda name: tmp_path)
    monkeypatch.setattr(actions_router, "_tail_lines", _spy)

    _run(actions_router.get_action_status("gateway-restart"))
    assert seen == [loop_thread], "switch off must tail inline, as before the change"
