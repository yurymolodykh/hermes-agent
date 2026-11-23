"""A/B bench server: a real uvicorn listen with the production WS settings.

The only difference from production is the payload source: frames are built from real
state.db message bodies (streamed deltas, control-card sized JSON, a model list) instead of
an agent turn, so the measurement isolates what permessage-deflate costs per frame.

Usage: python server_app.py --port N --deflate {on,off}
"""

from __future__ import annotations

import argparse
import json
import time
import sqlite3

import uvicorn
from fastapi import FastAPI, WebSocket

app = FastAPI()

STATE_DB = "/home/hermes/.hermes/state.db"


def real_bodies(limit: int = 400) -> list[str]:
    conn = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "select content from messages where length(content) between 40 and 2000 "
            "order by rowid desc limit ?", (limit,)
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows if r[0]]


BODIES = real_bodies()


def delta_frames(text: str, chunk: int = 24, nonce: str = "") -> list[str]:
    """The dashboard's streamed-token shape: one small JSON frame per chunk."""
    out = []
    for i in range(0, min(len(text), 1200), chunk):
        out.append(json.dumps({"jsonrpc": "2.0", "method": "message.delta",
                               "params": {"delta": text[i:i + chunk], "seq": nonce + str(i)}},
                              ensure_ascii=False))
    return out


def control_card(nonce: str = "") -> str:
    """A tool-call / control-card frame: nested JSON with paths, args, previews."""
    return json.dumps({
        "jsonrpc": "2.0", "method": "session.control",
        "params": {
            "session_id": "20261123_0259_ab12cd", "turn": 12, "seq": nonce,
            "tool_calls": [
                {"id": f"call_{i}", "name": "terminal",
                 "arguments": {"command": "cd /home/hermes/.hermes/hermes-agent/.worktrees/t_5bb8406e "
                                          "&& git status --short | head -40",
                               "timeout": 180},
                 "result_preview": " M hermes_cli/web_server_lifecycle.py\n?? bench/\n" * 3}
                for i in range(6)
            ],
            "todos": [{"id": i, "text": f"проверить шаг {i} стенда сжатия", "status": "pending"}
                      for i in range(12)],
        },
    }, ensure_ascii=False)


def model_options(nonce: str = "") -> str:
    """The /api model list frame that got truncated in the gateway log."""
    return json.dumps({
        "jsonrpc": "2.0", "method": "model.options", "id": 7, "seq": nonce,
        "result": {"providers": [
            {"id": f"provider-{p}", "name": f"Provider {p}", "auth": "api_key",
             "models": [{"id": f"model-{p}-{m}", "name": f"Model {p}/{m}", "context": 128000,
                         "input_price": 0.27, "output_price": 1.1, "vision": bool(m % 2),
                         "notes": "локальный каталог провайдера, цена за 1M токенов"}
                        for m in range(24)]}
            for p in range(6)
        ]},
    }, ensure_ascii=False)


@app.websocket("/bench")
async def bench_ws(ws: WebSocket) -> None:
    await ws.accept()
    mode = ws.query_params.get("mode", "panel")
    repeat = int(ws.query_params.get("repeat", "1"))
    # A unique nonce per frame stops deflate's context takeover from "remembering" a repeated frame:
    # without it a loop of identical 32 KB frames compresses ~100x and overstates the traffic saving.
    nonce = f"{time.monotonic_ns()}"
    for it in range(repeat):
        tag = f"{nonce}-{it}"
        if mode == "delta-coalesced":
            # ws.py coalesces streamed deltas on a 33 ms timer, so a real frame carries ~0.5-1 KB of
            # text, not one token. This mode is the shape the loop actually sends.
            for idx, body in enumerate(BODIES[:40]):
                for frame in delta_frames(body, chunk=600, nonce=f"{tag}-{idx}"):
                    await ws.send_text(frame)
        elif mode == "delta":
            for idx, body in enumerate(BODIES[:40]):
                for frame in delta_frames(body, nonce=f"{tag}-{idx}"):
                    await ws.send_text(frame)
        elif mode == "card":
            for _i in range(60):
                await ws.send_text(control_card(f"{tag}-{_i}"))
        elif mode == "models":
            for _i in range(30):
                await ws.send_text(model_options(f"{tag}-{_i}"))
        else:  # one panel session: streamed reply + cards + one model list
            for idx, body in enumerate(BODIES[:20]):
                for frame in delta_frames(body, nonce=f"{tag}-{idx}"):
                    await ws.send_text(frame)
                await ws.send_text(control_card(f"{tag}-{idx}"))
            await ws.send_text(model_options(tag))
    await ws.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--deflate", choices=("on", "off"), default="on")
    ap.add_argument("--window", type=int, default=None, help="server_max_window_bits")
    ap.add_argument("--memlevel", type=int, default=None, help="zlib memLevel")
    ap.add_argument("--level", type=int, default=None, help="zlib compress level (1..9)")
    args = ap.parse_args()
    if args.deflate == "on" and (args.window or args.memlevel or args.level):
        # uvicorn's sansio protocol builds the deflate factory inline and passes window/memLevel
        # explicitly, so a functools.partial would be overridden by the call's own keywords: the
        # override has to be a subclass that forces its values regardless of what is passed.
        from uvicorn.protocols.websockets import websockets_sansio_impl as _sansio
        from websockets.extensions.permessage_deflate import (
            ServerPerMessageDeflateFactory as _Factory,
        )

        window, memlevel, level = args.window, args.memlevel, args.level

        class _TunedFactory(_Factory):  # type: ignore[misc, valid-type]
            def __init__(self, *a, **kw):
                settings = {"memLevel": memlevel} if memlevel else {}
                if level:
                    settings["level"] = level
                super().__init__(
                    server_max_window_bits=window or 12,
                    client_max_window_bits=window or 12,
                    **({"compress_settings": settings} if settings else {}),
                )

        _sansio.ServerPerMessageDeflateFactory = _TunedFactory
    print(f"deflate={args.deflate} window={args.window} memlevel={args.memlevel} level={args.level}",
          flush=True)
    config = uvicorn.Config(
        app, host="127.0.0.1", port=args.port, log_level="warning",
        ws_per_message_deflate=args.deflate == "on",
        # same cap as hermes_cli.web_server_boundary._DESKTOP_ATTACHMENT_WS_MAX_BYTES
        ws_max_size=64 * 1024 * 1024,
        ws_ping_interval=None, ws_ping_timeout=None,
    )
    uvicorn.Server(config).run()


if __name__ == "__main__":
    main()
