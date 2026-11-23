"""Drive one bench connection and count wire bytes through a byte-counting TCP relay.

Wire bytes are counted on the relay, not in the client library, so they are exactly what the
network carries (deflate on: compressed; deflate off: raw), independent of client-side framing.

Usage: python drive.py --port N --mode MODE [--repeat R]  -> prints JSON
"""

from __future__ import annotations

import argparse
import asyncio
import json

import websockets

counts = {"c2s": 0, "s2c": 0}


async def relay(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter,
                target_port: int) -> None:
    server_reader, server_writer = await asyncio.open_connection("127.0.0.1", target_port)

    async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter, key: str) -> None:
        try:
            while data := await src.read(65536):
                counts[key] += len(data)
                dst.write(data)
                await dst.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            try:
                dst.close()
            except Exception:
                pass

    await asyncio.gather(pump(client_reader, server_writer, "c2s"),
                         pump(server_reader, client_writer, "s2c"))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--mode", default="panel")
    ap.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args()

    relay_server = await asyncio.start_server(
        lambda r, w: relay(r, w, args.port), "127.0.0.1", 0)
    relay_port = relay_server.sockets[0].getsockname()[1]
    app_bytes = frames = 0
    try:
        async with websockets.connect(
            f"ws://127.0.0.1:{relay_port}/bench?mode={args.mode}&repeat={args.repeat}",
            # A browser always offers permessage-deflate; the server decides. Offering it here is
            # what makes the "deflate on vs off" comparison apples-to-apples on wire bytes.
            compression="deflate",
        ) as ws:
            while True:
                try:
                    msg = await ws.recv()
                except websockets.ConnectionClosed:
                    break
                app_bytes += len(msg) if isinstance(msg, bytes) else len(msg.encode())
                frames += 1
    finally:
        relay_server.close()
        await relay_server.wait_closed()
    print(json.dumps({"mode": args.mode, "repeat": args.repeat, "frames": frames,
                      "app_bytes": app_bytes, "wire_s2c": counts["s2c"],
                      "wire_c2s": counts["c2s"]}))


if __name__ == "__main__":
    asyncio.run(main())
