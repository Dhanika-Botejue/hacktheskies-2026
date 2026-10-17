"""Fake phones: walk the demo path over and over so you can watch tracking on the host map.

    python -m sim.simulate --demo-map        # also load the demo floor plan
    python -m sim.simulate --players 3
    python -m sim.simulate --trouble         # camera loses tracking / gets blocked / restarts each lap

Each sim player runs camera tracking with step counting and compass, like a real
phone. The host map shows its estimate, particles and a white X at the true position.
"""

import argparse
import asyncio
import json
import ssl
import time
import urllib.request

import websockets

from .scenario import DEMO_MAP, DEMO_PATH, simulate_session

TROUBLE = [{"loss": (6, 10)}, {"blocked": (6, 10)}, {"reset_at": 8}]


def insecure_ssl():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # the dev server uses a self-signed cert
    return ctx


def put_demo_map(base: str):
    req = urllib.request.Request(f"{base}/api/map", data=json.dumps(DEMO_MAP).encode(), method="PUT",
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, context=insecure_ssl() if base.startswith("https") else None)


async def drain(ws):
    """Read (and ignore) the server's updates; an unread socket stalls and times out."""
    async for _ in ws:
        pass


async def player(url: str, idx: int, trouble: bool):
    path = DEMO_PATH if idx % 2 == 0 else list(reversed(DEMO_PATH))
    async with websockets.connect(url, ssl=insecure_ssl() if url.startswith("wss") else None) as ws:
        await ws.send(json.dumps({"role": "player", "id": f"sim-{idx}", "name": f"Sim {idx}", "platform": "sim"}))
        asyncio.create_task(drain(ws))
        lap = 0
        while True:
            extra = TROUBLE[lap % len(TROUBLE)] if trouble else {}
            start = time.monotonic()
            for t, kind, data in simulate_session(path, seed=idx * 1000 + lap, **extra):
                delay = start + t - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                await ws.send(json.dumps({"t": kind, **data}))
            await asyncio.sleep(2)
            lap += 1


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="https://localhost:8443")
    parser.add_argument("--players", type=int, default=1)
    parser.add_argument("--demo-map", action="store_true", help="replace the server's map with the demo floor")
    parser.add_argument("--trouble", action="store_true", help="inject camera problems, one kind per lap")
    args = parser.parse_args()

    if args.demo_map:
        put_demo_map(args.server)
    url = args.server.replace("http", "ws", 1) + "/ws"
    await asyncio.gather(*(player(url, i, args.trouble) for i in range(args.players)))


if __name__ == "__main__":
    asyncio.run(main())
