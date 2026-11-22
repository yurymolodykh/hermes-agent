"""Live proof for t_84ccbf64: the api_server platform really serves the chat's download.

Runs the REAL ``APIServerAdapter`` from this worktree on a spare loopback port, against the
REAL ``HERMES_HOME`` (profiles, state.db, the owner's report), and drives it exactly as the
desktop client does (``gateway-file-download.ts``: ``/api/fs/download?path=…&session_id=…``,
fallback ``/api/fs/read-data-url``, Bearer API_SERVER_KEY).

Usage (in the worktree, with the test environment's python):
    python verify-fs-download-live.py [port]

Exit code 0 = every check passed. Prints one line per check. Never prints the key.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import sys
import tempfile

REPORT = "/home/hermes/win-transfer/deliver-2111/SNO-economics-report-2026-11-13.html"
REPORT_SIZE = 2_988_017
REPORT_SHA = "5e754cc223dc9c4d558c18acd207859b2ce14c15e0ec993d5161e5c2927c4e5c"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: object, detail: str = "") -> None:
    passed = bool(ok)
    RESULTS.append((name, passed, detail))
    print(f"{'OK  ' if passed else 'FAIL'} {name}{(' — ' + detail) if detail else ''}", flush=True)


def _key() -> str:
    """The configured API_SERVER_KEY, read the way the adapter reads it (never printed)."""
    env = os.path.join(os.environ.get("HERMES_HOME", ""), ".env")
    if os.path.isfile(env):
        for line in open(env, encoding="utf-8", errors="replace"):
            if line.startswith("API_SERVER_KEY="):
                return line.split("=", 1)[1].strip().strip("'\"")
    return os.environ.get("API_SERVER_KEY", "")


async def _json(response) -> dict:
    """The response's JSON object, or {} when the body is not one (a 404 on a base without the
    route answers plain text, and this probe must still count that as a failure)."""
    try:
        payload = await response.json(content_type=None)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def main(port: int) -> int:
    from aiohttp import ClientSession

    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    key = _key()
    if not key:
        check("API_SERVER_KEY resolvable", False, "no key in HERMES_HOME/.env")
        return 2
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": key, "host": "127.0.0.1", "port": port}))
    adapter._port = port
    adapter._host = "127.0.0.1"
    started = await adapter.connect()
    check("adapter started on 127.0.0.1:%d" % port, bool(started))
    if not started:
        return 2

    base = f"http://127.0.0.1:{port}"
    auth = {"Authorization": f"Bearer {key}"}
    try:
        async with ClientSession() as http:
            # ── 1. the client's own request: the report as an attachment ─────────────
            r = await http.get(f"{base}/api/fs/download", params={"path": REPORT}, headers=auth)
            body = await r.read()
            check("download 200", r.status == 200, f"status={r.status}")
            check("download exact size", len(body) == REPORT_SIZE, f"{len(body)} bytes")
            check("download sha256", _sha(body) == REPORT_SHA, _sha(body)[:16] + "…")
            check("download Content-Disposition attachment",
                  r.headers.get("Content-Disposition", "").startswith("attachment;"),
                  r.headers.get("Content-Disposition", "")[:80])
            check("download Content-Type", r.headers.get("Content-Type", "").startswith("text/html"),
                  r.headers.get("Content-Type", ""))

            # HEAD sizes it without the body.
            h = await http.head(f"{base}/api/fs/download", params={"path": REPORT}, headers=auth)
            check("HEAD 200 with Content-Length", h.status == 200
                  and h.headers.get("Content-Length") == str(REPORT_SIZE),
                  f"{h.status} {h.headers.get('Content-Length')}")
            await h.release()

            # ── 2. the fallback route: same bytes as a data URL ──────────────────────
            r = await http.get(f"{base}/api/fs/read-data-url", params={"path": REPORT}, headers=auth)
            payload = await _json(r) if r.status == 200 else {}
            data_url = (payload or {}).get("dataUrl", "")
            prefix, _, encoded = data_url.partition(",")
            decoded_sha = _sha(base64.b64decode(encoded)) if encoded else ""
            check("read-data-url 200", r.status == 200, f"status={r.status}")
            check("read-data-url sha256 matches", decoded_sha == REPORT_SHA, decoded_sha[:16] + "…")
            check("read-data-url prefix", prefix.startswith("data:text/html;base64"), prefix)

            # ── 3. negative cases ───────────────────────────────────────────────────
            r = await http.get(f"{base}/api/fs/download", params={"path": REPORT})
            await r.release()
            check("no token -> 401", r.status == 401, f"status={r.status}")

            r = await http.get(f"{base}/api/fs/download",
                               params={"path": "/home/hermes/win-transfer/deliver-2111/nope.html"},
                               headers=auth)
            missing = await _json(r)
            check("missing path -> 404 with detail",
                  r.status == 404 and isinstance(missing, dict) and bool(missing.get("detail")),
                  f"{r.status} {str(missing)[:80]}")

            for name, path in (("sensitive .env", "/home/hermes/.hermes/.env"),
                               ("credential key", "/home/hermes/.ssh/id_ed25519"),
                               ("credential dir config", "/home/hermes/.ssh/config")):
                r = await http.get(f"{base}/api/fs/download", params={"path": path}, headers=auth)
                await r.release()
                check(f"{name} -> 403", r.status == 403, f"status={r.status}")

            # A path outside the allowed roots is refused only when a managed root is locked;
            # this host's default policy (no HERMES_DASHBOARD_FILES_ROOT, HERMES_HOME !=
            # /opt/data) leaves $HOME open, exactly as the panel does. Prove the refusal with
            # the same locked-root environment the panel is tested under.
            os.environ["HERMES_DASHBOARD_FILES_ROOT"] = "/home/hermes/win-transfer"
            try:
                outside = "/home/hermes/.hermes/config.yaml"
                r = await http.get(f"{base}/api/fs/download", params={"path": outside}, headers=auth)
                detail = await _json(r)
                check("outside locked root -> 403",
                      r.status == 403 and "outside" in str(detail).lower(),
                      f"{r.status} {str(detail)[:80]}")
            finally:
                os.environ.pop("HERMES_DASHBOARD_FILES_ROOT", None)

            # A file past the 100 MiB ceiling: sparse, so the probe costs no disk.

            def _make_sparse() -> str:
                folder = tempfile.mkdtemp(dir=os.path.expanduser("~/win-transfer"), prefix="oversize-")
                name = os.path.join(folder, "too-big.html")
                with open(name, "wb") as handle:
                    handle.truncate(100 * 1024 * 1024 + 1)
                return name

            big = await asyncio.to_thread(_make_sparse)
            try:
                r = await http.get(f"{base}/api/fs/download", params={"path": big}, headers=auth)
                detail = await _json(r)
                check("over the size ceiling -> 413",
                      r.status == 413 and isinstance(detail, dict) and detail.get("detail"),
                      f"{r.status} {str(detail)[:60]}")
            finally:
                os.unlink(big)
                os.rmdir(os.path.dirname(big))

            # ── 4. session_id / profile, as the panel handles them ─────────────────
            import sqlite3

            db = os.path.join(os.environ["HERMES_HOME"], "state.db")
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            row = conn.execute(
                "select id, session_key from sessions where source='api_server'"
                " order by rowid desc limit 1").fetchone()
            conn.close()
            if row is None:
                check("an api_server session exists to test session_id", False, "none in state.db")
            else:
                own, own_key = row
                r = await http.get(f"{base}/api/fs/download",
                                   params={"path": REPORT, "session_id": own}, headers=auth)
                body = await r.read()
                check("own session_id -> 200 same bytes",
                      r.status == 200 and _sha(body) == REPORT_SHA, f"status={r.status}")
                r = await http.get(f"{base}/api/fs/download",
                                   params={"path": REPORT, "session_id": "api-deadbeefdeadbeef"},
                                   headers=auth)
                detail = await _json(r)
                check("unknown session_id -> 404",
                      r.status == 404 and isinstance(detail, dict) and detail.get("detail"),
                      f"{r.status} {str(detail)[:60]}")
                r = await http.get(f"{base}/api/fs/read-data-url",
                                   params={"path": REPORT, "session_id": own}, headers=auth)
                payload = await _json(r) if r.status == 200 else {}
                encoded = (payload or {}).get("dataUrl", "").partition(",")[2]
                check("own session_id data-url -> 200 same bytes",
                      r.status == 200 and _sha(base64.b64decode(encoded)) == REPORT_SHA,
                      f"status={r.status}")
                if own_key:
                    check("(info) session key known", True, own_key[:12] + "…")

            r = await http.get(f"{base}/api/fs/download",
                               params={"path": REPORT, "profile": "no-such-profile"}, headers=auth)
            await r.release()
            check("unknown profile -> 404", r.status == 404, f"status={r.status}")

            # ── 5. no write route on this surface ──────────────────────────────────
            write_routes = []
            for method, path, _handler in adapter._http_route_table():
                if path.startswith("/api/fs") and method not in ("GET", "HEAD"):
                    write_routes.append(f"{method} {path}")
            check("no write route under /api/fs", not write_routes, ",".join(write_routes))
            fs_routes = sorted(f"{m} {p}" for m, p, _h in adapter._http_route_table()
                               if p.startswith("/api/fs"))
            check("registered read surface", fs_routes == [
                "GET /api/fs/download", "GET /api/fs/read-data-url",
                "HEAD /api/fs/download", "HEAD /api/fs/read-data-url"], ",".join(fs_routes))
    finally:
        await adapter.disconnect()

    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed", flush=True)
    if failed:
        print("failed: " + "; ".join(failed), flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 8649)))
