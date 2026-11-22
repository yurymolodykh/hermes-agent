"""Read-only ``/api/fs`` routes on the api_server platform — the chat's own download.

The desktop chat reaches its gateway over this platform, and its "Download" button asks
``GET /api/fs/download`` (fallback ``/api/fs/read-data-url``) for the bytes of a message
attachment, see ``apps/desktop/electron/gateway-file-download.ts``. Those routes existed only on
the dashboard panel (``hermes_cli/web_routers/files.py``), so every chat download answered 404
while the preview of the same file worked — the preview's bytes ride inside the transcript as a
data URL and never touch the server.

The READ rules are imported from the panel, not restated: ``_fs_download_path`` (the session's
cwd and container-path translation), ``_fs_owned_session`` (session ownership, 404 for a foreign
or unknown id), ``_fs_regular_file`` (regular file + sensitive-path refusal),
``_hosted_fs_read_guard`` (managed read root + per-caller containment), ``_refuse_live_database``
(409 rather than serving a file a SQLite connection holds) and the panel's own size ceilings
(``_MANAGED_FILE_MAX_BYTES``, ``_FS_DATA_URL_MAX_BYTES``). A profile's SSH workspace is read
through the panel's remote branch, unchanged.

No write route (upload / mkdir / delete) is registered here: this surface is bytes out, never
bytes in.

Auth is the platform's own — the same API_SERVER_KEY every other route on this listener takes,
resolved for the URL-selected profile by ``APIServerAdapter._check_auth``.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import urllib.parse
from functools import partial
from typing import Any, Optional, cast

try:
    from aiohttp import web
except ImportError:  # pragma: no cover - the platform itself refuses to start without aiohttp
    web = None  # type: ignore[assignment]

logger = logging.getLogger("gateway.platforms.api_server")


def http_routes(adapter) -> list[tuple[str, str, object]]:
    """``(method, path, handler)`` rows for the read-only file surface.

    Both methods on both paths: aiohttp routes by exact method (a registered GET is not a HEAD),
    and a HEAD is how a client sizes a download before asking for its bytes.
    """
    return [
        ("GET", "/api/fs/download", partial(_handle_download, adapter)),
        ("HEAD", "/api/fs/download", partial(_handle_download, adapter)),
        ("GET", "/api/fs/read-data-url", partial(_handle_data_url, adapter)),
        ("HEAD", "/api/fs/read-data-url", partial(_handle_data_url, adapter)),
    ]


async def _handle_download(adapter, request: web.Request) -> web.StreamResponse:
    return await _run_guarded(adapter, request, _download)


async def _handle_data_url(adapter, request: web.Request) -> web.StreamResponse:
    return await _run_guarded(adapter, request, _data_url)


async def _run_guarded(adapter, request, body) -> web.StreamResponse:
    """Authenticate, then run *body*, answering a panel refusal in the panel's own shape.

    The panel's helpers raise FastAPI's ``HTTPException`` because they are its routes' code; the
    status/detail pair is the contract both surfaces answer with, so it is translated rather than
    re-derived here.
    """
    auth_error = adapter._check_auth(request)
    if auth_error is not None:
        return auth_error
    try:
        return await body(adapter, request)
    except Exception as exc:
        from fastapi import HTTPException

        if isinstance(exc, HTTPException):
            return web.json_response({"detail": exc.detail}, status=int(exc.status_code))
        raise


async def _owned_session(session_id: Optional[str], profile: Optional[str]) -> Optional[dict]:
    """The session *session_id* names in *profile*'s own store, or None when none was named.

    The panel's rule (``files._fs_owned_session``): a blank id, or one that profile does not own,
    is 404 — validated even for an absolute path, so a client's ``cwd`` is never trusted. The
    profile is passed as the KEYWORD here, so the lookup opens that profile's ``state.db`` (the
    panel's positional call lands the profile in the ``request`` slot and reads the process home's
    store; on this listener a ``/p/<profile>/`` request must read its own).
    """
    from fastapi import HTTPException

    if session_id is None:
        return None
    if not session_id.strip():
        raise HTTPException(status_code=404, detail="Session not found")
    from hermes_cli.web_routers.sessions import get_session_detail

    # A request-less call: this listener's identity IS the API key, so there is no per-person
    # scope to resolve here (the panel's dashboard cookie has one; that route is not this one).
    return await get_session_detail(session_id, cast("Any", None), profile=profile)


def _session_param(request: web.Request) -> Optional[str]:
    return request.query.get("session_id")


def _profile_param(request: web.Request) -> Optional[str]:
    """The ``?profile=`` this read is for, resolved the way the panel resolves it.

    The panel honours any profile because its own listener serves the host; here the request also
    carries a ``/p/<profile>/`` prefix (the platform's only profile selector) and that prefix is
    authenticated as that profile, so a ``?profile=`` naming somebody else is refused. With no
    prefix the request is the listener's own profile (the owner's key, host-wide — exactly the
    panel's trust level) and the panel's own unknown-profile 404 applies.
    """
    from fastapi import HTTPException

    from gateway.platforms.api_server import _api_request_profile
    from hermes_cli.web_server_cron import _cron_profile_home
    from hermes_cli.web_server_profiles import _is_current_profile

    requested = (request.query.get("profile") or "").strip()
    if _is_current_profile(requested):
        return None
    canonical, _home = _cron_profile_home(requested)  # 404 for a profile that does not exist
    prefixed = _api_request_profile.get()
    if prefixed and canonical != prefixed.strip().lower():
        logger.warning(
            "api_server file read refused: ?profile=%r is not the request's profile %r",
            canonical, prefixed)
        raise HTTPException(status_code=403, detail="Profile does not match the request scope")
    return canonical


def _path_param(request: web.Request) -> str:
    from fastapi import HTTPException

    path = request.query.get("path") or ""
    if not path.strip():
        raise HTTPException(status_code=400, detail="Query parameter 'path' is required")
    return path


async def _download(adapter, request: web.Request) -> web.StreamResponse:
    """The panel's ``/api/fs/download``: the file as an attachment, or its HEAD headers."""
    from hermes_cli.web_server import _MANAGED_FILE_MAX_BYTES
    from hermes_cli.web_routers import files as panel

    path = _path_param(request)
    profile = _profile_param(request)
    session_id = _session_param(request)
    session = await _owned_session(session_id, profile)
    backend = await asyncio.to_thread(panel._remote_fs_backend, path, profile)
    if backend is not None:
        target, size = await asyncio.to_thread(panel._remote_download_target, backend, path)
        if size > _MANAGED_FILE_MAX_BYTES:
            return _too_large()
        return _remote_attachment(request, backend, target, size)
    target = await panel._fs_download_path(path, profile, session_id, session=session)
    panel._hosted_fs_read_guard(target, request)
    target, st = panel._fs_regular_file(target, panel._fs_profile_home(profile))
    await asyncio.to_thread(panel._refuse_live_database, target)
    if st.st_size > _MANAGED_FILE_MAX_BYTES:
        return _too_large()
    if request.method == "HEAD":
        return _attachment_headers_response(target, st.st_size)
    response = web.FileResponse(str(target))
    _apply_attachment_headers(response.headers, target)
    return response


async def _data_url(adapter, request: web.Request) -> web.StreamResponse:
    """The panel's ``/api/fs/read-data-url``: the file inline as a base64 data URL.

    The chat's download falls back to this route, and it is also how an attachment the client
    already holds is re-read when its own byte cache is cold.
    """
    from hermes_cli.web_server import _FS_DATA_URL_MAX_BYTES
    from hermes_cli.web_routers import files as panel

    path = _path_param(request)
    profile = _profile_param(request)
    session_id = _session_param(request)
    session = await _owned_session(session_id, profile)
    backend = await asyncio.to_thread(panel._remote_fs_backend, path, profile)
    if backend is not None:
        data, _size, target = await asyncio.to_thread(
            backend.read_bytes, path, max_bytes=_FS_DATA_URL_MAX_BYTES)
        encoded = base64.b64encode(data).decode("ascii")
        return web.json_response(
            {"dataUrl": f"data:{panel._fs_mime_type(_remote_path(target))};base64,{encoded}"})
    target = await panel._fs_download_path(path, profile, session_id, session=session)
    # A browser paste or a picker-staged upload reads back from the profile's own cache, outside
    # the managed tree — the same exemption the panel makes.
    if not await asyncio.to_thread(panel._is_host_file, str(target), profile):
        panel._hosted_fs_read_guard(target, request)
    target, st = panel._fs_regular_file(target, panel._fs_profile_home(profile))
    if st.st_size > _FS_DATA_URL_MAX_BYTES:
        return _too_large()
    # A transcript image is read again on every remount: let an unchanged file cost a 304.
    validators = {"Cache-Control": "private, no-cache", "ETag": panel._fs_etag(target, st)}
    if "if-none-match" in request.headers:
        tags = {tag.strip().removeprefix("W/")
                for tag in request.headers["if-none-match"].split(",")}
        if tags & {validators["ETag"], "*"}:
            return web.Response(status=304, headers=validators)
    if request.method == "HEAD":
        return web.Response(headers=validators)

    def _render() -> web.Response:
        encoded = base64.b64encode(panel._fs_read_bytes(target)).decode("ascii")
        return web.json_response(
            {"dataUrl": f"data:{panel._fs_mime_type(target)};base64,{encoded}"},
            headers=validators)

    # Read, encode and render off the event loop: up to the data-URL ceiling of base64 stalls
    # every other request on this listener (and its SSE streams).
    return await asyncio.to_thread(_render)


def _too_large() -> web.Response:
    """A file past the route's ceiling: the panel raises FastAPI's 413 ``"File too large"``."""
    return web.json_response({"detail": "File too large"}, status=413)


def _remote_path(target: str):
    from pathlib import PurePosixPath

    return PurePosixPath(target)


def _apply_attachment_headers(headers, target) -> None:
    """``Content-Disposition: attachment`` with the RFC 5987 UTF-8 filename, plus the panel's mime
    type (``FileResponse`` would otherwise guess from the suffix alone)."""
    from hermes_cli.web_routers import files as panel

    name = urllib.parse.quote(target.name)
    headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{name}"
    headers["Content-Type"] = panel._fs_mime_type(target)


def _attachment_headers_response(target, size: int) -> web.Response:
    """A HEAD carries the download's own headers and no body."""
    headers = {"Content-Length": str(size)}
    _apply_attachment_headers(headers, target)
    return web.Response(headers=headers)


def _remote_attachment(request: web.Request, backend, target: str, size: int) -> web.StreamResponse:
    """*target* streamed from the profile's SSH workspace as an attachment (the panel's shape):
    200 for the whole file, 206 for a byte range, and a HEAD reads none of it."""
    from hermes_cli.web_routers import files as panel
    from hermes_cli.web_server_remote_media import byte_range

    span = byte_range(request.headers.get("range"), size)
    first, last = span if span is not None else (0, size - 1)
    length = max(last - first + 1, 0)
    headers = {"Content-Length": str(length)}
    _apply_attachment_headers(headers, _remote_path(target))
    status = 200
    if span is not None:
        status = 206
        headers["Content-Range"] = f"bytes {first}-{last}/{size}"
    if request.method == "HEAD" or length == 0:
        return web.Response(status=status, headers=headers)
    body = (backend.stream_file(target, offset=first, length=length)
            if span else backend.stream_file(target))
    response = web.StreamResponse(status=status, headers=headers)
    response.content_type = headers["Content-Type"]
    return _stream_into(response, body, request)


async def _stream_into(response, chunks, request) -> web.StreamResponse:
    """Write *chunks* (a blocking generator on the SSH side) without holding the event loop."""
    iterator = iter(chunks)
    await response.prepare(request)
    while True:
        chunk = await asyncio.to_thread(next, iterator, None)
        if not chunk:
            break
        await response.write(chunk)
    await response.write_eof()
    return response
