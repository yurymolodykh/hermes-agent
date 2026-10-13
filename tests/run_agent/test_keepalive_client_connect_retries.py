"""Behavioral + config regression for connect-phase retries in the
keepalive httpx client (t_e613f74a).

Incident class: llm.letovo.site (direct egress, NO_PROXY) intermittently
drops TCP/TLS handshakes under provider-side network flakiness.  The old
build produced an instant ``APIConnectionError`` on the FIRST failed
connect, forcing the outer conversation loop into its slow multi-second
retry path and surfacing "APIConnectionError / Превышен таймаут" to users.

The fix: every transport built by ``AIAgent._build_keepalive_http_client``
carries ``retries=2`` — httpcore connect-phase retries with 0 s / 0.5 s
exponential backoff, effective on ``ConnectError`` / ``ConnectTimeout``
only (a request that was already sent is never re-sent).

Layer 1 (behavioral): a custom httpcore NetworkBackend that fails the
first N ``connect_tcp`` calls with ``ConnectError`` then succeeds.  With
retries=2, a single dropped handshake must be absorbed INSIDE the pool —
the request completes and the backend saw 2 connect attempts.  A control
client built with ``retries=0`` must surface ``httpx.ConnectError``.

Layer 2 (config pins): the direct-egress mounts carry retries=2, the
proxied base transport carries retries=2, and the pool limits /
keepalive_expiry / ssl settings that previous regressions pinned
(#54049 streaming, #10933 stale transport) are unchanged.
"""
from __future__ import annotations

import ssl

import certifi
import httpcore
import httpx
import pytest

from agent.ssl_verify import resolve_httpx_verify
from run_agent import AIAgent

_CA_ENV_VARS = (
    "HERMES_CA_BUNDLE",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "ALL_PROXY",
    "https_proxy",
    "http_proxy",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)


@pytest.fixture
def clean_env(monkeypatch):
    """Isolate proxy/CA env so the direct-egress (no-proxy mounts) branch
    is the one under test."""
    for var in _CA_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Layer 1: behavioral — flaky connect is absorbed inside the pool
# ---------------------------------------------------------------------------


class _FlakyBackend(httpcore.NetworkBackend):
    """Fail the first ``fail_first`` connect_tcp calls with ConnectError,
    then hand out a normal MockStream serving a canned HTTP/1.1 response."""

    def __init__(self, fail_first: int, response: bytes) -> None:
        self.fail_first = fail_first
        self.attempts = 0
        self.slept: list[float] = []
        self._response = response

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ) -> httpcore.NetworkStream:
        self.attempts += 1
        if self.attempts <= self.fail_first:
            raise httpcore.ConnectError(
                f"simulated dropped handshake to {host}:{port} "
                f"(attempt {self.attempts})"
            )
        return httpcore.MockStream([self._response])

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise NotImplementedError

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


_OK_RESPONSE = (
    b"HTTP/1.1 200 OK\r\n"
    b"content-length: 2\r\n"
    b"\r\n"
    b"ok"
)


def _client_with_backend(
    verify: object = True, backend: httpcore.NetworkBackend | None = None
) -> tuple[httpx.Client, object]:
    client = AIAgent._build_keepalive_http_client(
        "https://llm.letovo.site/v1", verify=verify,
    )
    assert client is not None
    # Swap the pool's network backend for the flaky one so connect-phase
    # retry behavior is observable without real network egress.
    https_mount = client._mounts[httpx._utils.URLPattern("https://")]
    pool = https_mount._pool
    if backend is not None:
        pool._network_backend = backend
    return client, pool


def test_flaky_connect_absorbed_inside_pool(clean_env):
    """One dropped handshake + retries=2 → request still completes, backend
    saw exactly 2 connect attempts (1 fail + 1 success), backoff slept."""
    backend = _FlakyBackend(fail_first=1, response=_OK_RESPONSE)
    client, _pool = _client_with_backend(backend=backend)
    try:
        resp = client.get("https://llm.letovo.site/v1/models")
        assert resp.status_code == 200
        assert resp.text == "ok"
        assert backend.attempts == 2, (
            "expected 1 failed + 1 retried connect, got "
            f"{backend.attempts}"
        )
        assert backend.slept == [0.0], (
            f"expected httpcore backoff [0.0], got {backend.slept}"
        )
    finally:
        client.close()


def test_two_dropped_handshakes_still_succeed(clean_env):
    """retries=2 absorbs up to two consecutive connect failures."""
    backend = _FlakyBackend(fail_first=2, response=_OK_RESPONSE)
    client, _pool = _client_with_backend(verify=True, backend=backend)
    try:
        resp = client.get("https://llm.letovo.site/v1/models")
        assert resp.status_code == 200
        assert backend.attempts == 3
    finally:
        client.close()


def test_three_drops_exhaust_retries_and_raise(clean_env):
    """Three consecutive drops exceed retries=2 → the request MUST fail so
    the outer conversation loop sees a real error (no infinite retry)."""
    backend = _FlakyBackend(fail_first=3, response=_OK_RESPONSE)
    client, _pool = _client_with_backend(verify=True, backend=backend)
    try:
        with pytest.raises(httpx.ConnectError):
            client.get("https://llm.letovo.site/v1/models")
        assert backend.attempts == 3
    finally:
        client.close()


def test_control_client_without_retries_fails_immediately(clean_env):
    """Control: plain httpx transport without retries surfaces ConnectError
    on the first drop — proves the behavioral test actually exercises the
    retry path added by the fix (and not some happy-path artifact)."""
    backend = _FlakyBackend(fail_first=1, response=_OK_RESPONSE)
    transport = httpx.HTTPTransport(retries=0)
    transport._pool._network_backend = backend
    client = httpx.Client(transport=transport)
    try:
        with pytest.raises(httpx.ConnectError):
            client.get("https://llm.letovo.site/v1/models")
        assert backend.attempts == 1
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Layer 2: config pins — retries literal on every transport, prior
# regression pins unchanged (pool limits, keepalive_expiry, ssl).
# ---------------------------------------------------------------------------


def test_direct_mounts_carry_retries_literal(clean_env):
    client = AIAgent._build_keepalive_http_client(
        "https://llm.letovo.site/v1", verify=True,
    )
    assert client is not None
    https_pattern = httpx._utils.URLPattern("https://")
    https_mount = client._mounts[https_pattern]
    assert https_mount._pool._retries == 2, (
        "https:// mount pool must carry retries=2"
    )
    http_mount = client._mounts[httpx._utils.URLPattern("http://")]
    assert http_mount._pool._retries == 2

    # Prior regression pins (do not regress #54049 / CLOSE-WAIT reaping):
    pool = https_mount._pool
    assert pool._max_keepalive_connections == 20
    assert pool._max_connections == 100
    assert pool._keepalive_expiry == 20.0


def test_proxied_base_transport_carries_retries_literal(clean_env, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:3128")
    client = AIAgent._build_keepalive_http_client(
        "https://llm.letovo.site/v1", verify=True,
    )
    assert client is not None
    # Proxied branch: the explicit base transport we pass carries retries=2.
    assert client._transport._pool._retries == 2


def test_ssl_verify_settings_unchanged(clean_env):
    """Per-provider ssl_verify=False must keep disabling hostname checks —
    the retries change must not disturb the verify plumbing."""
    verify = resolve_httpx_verify(ssl_verify=False)
    client = AIAgent._build_keepalive_http_client(
        "https://llm.letovo.site/v1", verify=verify,
    )
    assert client is not None
    https_mount = client._mounts[httpx._utils.URLPattern("https://")]
    assert https_mount._pool._ssl_context.check_hostname is False


def test_ssl_ca_bundle_settings_unchanged(clean_env, monkeypatch):
    monkeypatch.setenv("HERMES_CA_BUNDLE", certifi.where())
    verify = resolve_httpx_verify()
    client = AIAgent._build_keepalive_http_client(
        "https://llm.letovo.site/v1", verify=verify,
    )
    assert client is not None
    https_mount = client._mounts[httpx._utils.URLPattern("https://")]
    assert isinstance(https_mount._pool._ssl_context, ssl.SSLContext)
