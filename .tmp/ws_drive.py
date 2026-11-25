"""Drive N concurrent /api/ws sessions on the card's isolated gateway, each polling profiles.list.

Mimics what the Bots roster does (a profiles.list poll per connection every 5 s); the load is
tightened so py-spy samples the WS request path rather than idle time.
"""

import asyncio
import json
import sys

import websockets

URL = sys.argv[1]
TICKET = sys.argv[2]
CONNS = int(sys.argv[3]) if len(sys.argv) > 3 else 40
DURATION = float(sys.argv[4]) if len(sys.argv) > 4 else 120.0
PROTO = 'hermes-gateway-ticket.' + TICKET

errors = []
done = 0


async def one(i, deadline):
    global done
    url = f'{URL}?token={TICKET}'
    try:
        async with websockets.connect(url, subprotocols=[PROTO], max_size=None) as ws:
            req = 0
            while asyncio.get_running_loop().time() < deadline:
                req += 1
                await ws.send(json.dumps({'jsonrpc': '2.0', 'id': req, 'method': 'profiles.list', 'params': {}}))
                raw = await asyncio.wait_for(ws.recv(), timeout=30)
                msg = json.loads(raw)
                if 'error' in msg:
                    errors.append(msg['error'])
                done += 1
                await asyncio.sleep(float(sys.argv[5]) if len(sys.argv) > 5 else 0.05)
    except Exception as exc:  # noqa: BLE001 - the probe reports every failure mode it can hit
        errors.append(f'{type(exc).__name__}: {exc}')


async def main():
    loop = asyncio.get_running_loop()
    deadline = loop.time() + DURATION
    await asyncio.gather(*(one(i, deadline) for i in range(CONNS)))
    print(f'connections={CONNS} profiles.list calls={done} errors={len(errors)}')
    for err in errors[:5]:
        print(' ', err)


asyncio.run(main())
