from __future__ import annotations

import pytest

from agent import provider_media


def _save_video(url: str, **kwargs):
    return provider_media.save_url(
        "videos",
        url,
        prefix="provider-test",
        timeout=30,
        max_bytes=1024,
        chunk_size=64,
        content_types={"video/mp4": "mp4"},
        url_extensions=("mp4",),
        default_extension="mp4",
        label="Video",
        empty_error="empty: {url}",
        **kwargs,
    )


def _stub_fetch(monkeypatch, handler):
    """Route save_url's fetch through an httpx MockTransport and stub the URL
    policy check — SSRF behavior has dedicated tests in test_save_url_image.py;
    these pin the streaming/content-type/headers contract."""
    import httpx

    monkeypatch.setattr("tools.url_safety.is_safe_url", lambda url: True)
    monkeypatch.setattr(
        "tools.url_safety.create_ssrf_safe_client",
        lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), **kw),
    )


def test_save_url_forwards_headers_and_streams_to_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    calls = []

    import httpx

    def handler(request):
        calls.append(request)
        return httpx.Response(200, headers={"Content-Type": "video/mp4"}, content=b"clip")

    _stub_fetch(monkeypatch, handler)

    path = _save_video(
        "https://api.example/videos/job/content",
        headers={"Authorization": "Bearer test"},
        require_known_content_type=True,
    )

    assert path.read_bytes() == b"clip"
    assert path.suffix == ".mp4"
    assert len(calls) == 1
    assert str(calls[0].url) == "https://api.example/videos/job/content"
    assert calls[0].headers["Authorization"] == "Bearer test"


def test_save_url_strict_content_type_rejects_non_video(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    import httpx

    _stub_fetch(
        monkeypatch,
        lambda request: httpx.Response(200, headers={"Content-Type": "text/html"}, content=b"not a video"),
    )

    with pytest.raises(ValueError, match="unexpected Content-Type text/html"):
        _save_video(
            "https://api.example/videos/job/content",
            require_known_content_type=True,
        )

    assert not list(provider_media.cache_dir("videos").iterdir())


def test_save_url_redirect_scopes_caller_headers_to_first_hop_and_fails_closed(monkeypatch, tmp_path):
    """Caller auth headers (provider base_url fetches) go to the first hop only — a
    redirect target must never receive them — and a 3xx without ``Location`` is an
    error, never a cached body."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    calls = []

    import httpx

    def handler(request):
        calls.append(request)
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/final"})
        if request.url.path == "/no-location":
            return httpx.Response(302, content=b"<html>3xx body</html>")
        return httpx.Response(200, headers={"Content-Type": "video/mp4"}, content=b"clip")

    _stub_fetch(monkeypatch, handler)

    path = _save_video(
        "https://api.example/start",
        headers={"Authorization": "Bearer test"},
        require_known_content_type=True,
    )

    assert path.read_bytes() == b"clip"
    assert len(calls) == 2
    assert calls[0].headers.get("Authorization") == "Bearer test"
    assert calls[1].headers.get("Authorization") is None

    with pytest.raises(ValueError, match="without a Location"):
        _save_video("https://api.example/no-location", require_known_content_type=True)
    assert list(provider_media.cache_dir("videos").iterdir()) == [path]


def test_save_url_trusted_origin_skips_private_check_on_first_hop_only(monkeypatch, tmp_path):
    """``trusted_origin=True`` (the caller built the URL from the operator's own
    provider ``base_url``) must let a LAN/loopback relay serve the first hop, but the
    cloud-metadata floor still applies and every redirect target is re-validated in
    full — a relay cannot bounce us to another internal address."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.delenv("HERMES_ALLOW_PRIVATE_URLS", raising=False)
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append((self.path, self.headers.get("Authorization")))
            if self.path == "/bounce":
                self.send_response(302)
                self.send_header("Location", "/content")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.end_headers()
            self.wfile.write(b"clip")

        def log_message(self, *_):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"

        path = _save_video(f"{base}/content", headers={"Authorization": "Bearer test"},
                           require_known_content_type=True, trusted_origin=True)
        assert path.read_bytes() == b"clip"
        assert hits == [("/content", "Bearer test")]

        with pytest.raises(ValueError, match="SSRF safety check"):
            _save_video(f"{base}/bounce", headers={"Authorization": "Bearer test"},
                        require_known_content_type=True, trusted_origin=True)
        assert hits[1:] == [("/bounce", "Bearer test")]  # hop 2 (loopback) refused before connecting

        with pytest.raises(ValueError, match="always-blocked"):
            _save_video("http://169.254.169.254/latest/meta-data", trusted_origin=True)
        assert len(hits) == 2
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    "prefix",
    [
        "openai_community/scriptsnsenses-sys/flux-2-dev-free",  # slash model id (the live bug)
        "openai_black-forest-labs/flux.2-klein-4b",             # slash model id, dotted
        "openai_model with space",                              # space
        "openai_.._.._etc_passwd",                              # dotted segment, sanitised-safe
        "..",                                                   # bare traversal token
        "...",                                                  # bare dot-run
        "../escape",                                            # traversal with slash
        "..\\..\\windows",                                      # windows-style traversal
        "openai_\u043c\u043e\u0434\u0435\u043b\u044c",          # non-ASCII model id
        "",                                                     # empty prefix
    ],
)
def test_cache_path_sanitises_prefix_to_one_file_inside_cache(monkeypatch, tmp_path, prefix):
    """Regression: a ``prefix`` carrying ``/`` (an image-gen model id like
    ``vendor/model``) used to build a nested path under a subdirectory that does not
    exist, so ``save_bytes`` raised FileNotFoundError and the generated image was
    silently dropped. ``..`` could escape the cache directory entirely. The sanitiser
    must collapse every unsafe character and keep the result directly under the cache
    directory — verified by the resolved absolute parent, not by string matching.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    path = provider_media.cache_path("generated/images", prefix, "png")

    base = provider_media.cache_dir("generated/images").resolve()
    assert path.resolve().parent == base, f"{path!r} escaped {base!r}"
    assert path.parent == provider_media.cache_dir("generated/images")

    written = provider_media.save_bytes("generated/images", b"png-bytes", prefix=prefix, extension="png")
    assert written.exists() and written.read_bytes() == b"png-bytes"
    assert written.resolve().parent == base
    # No subdirectory was created for a slashed prefix.
    assert [p for p in base.iterdir() if p.is_dir()] == []
    # Filename is a single path component over an allowed charset.
    assert "/" not in written.name and "\\" not in written.name
    assert written.name.startswith(provider_media._safe_prefix(prefix) + "_")


def test_cache_path_keeps_safe_prefix_unchanged(monkeypatch, tmp_path):
    """A prefix already limited to ``[A-Za-z0-9._-]`` must keep its exact shape —
    existing cache filenames (``flux-2-dev``, ``z-image``) do not change."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    for prefix in ("openai_flux-2-dev", "z-image", "openai.black_forest_v1.2"):
        path = provider_media.cache_path("generated/images", prefix, "jpg")
        assert path.name.startswith(prefix + "_"), path.name
        assert provider_media._safe_prefix(prefix) == prefix


def test_cache_path_sanitiser_is_idempotent(monkeypatch, tmp_path):
    """Sanitising twice changes nothing — a value read back from a filename stays valid."""
    for prefix in ("openai_community/scriptsnsenses-sys/flux-2-dev-free", "..", "a b$c"):
        once = provider_media._safe_prefix(prefix)
        assert provider_media._safe_prefix(once) == once
