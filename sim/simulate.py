"""Fake phones: walk the demo path over and over so you can watch tracking on the host map.

    python -m sim.simulate --demo-map        # also load the demo floor plan
    python -m sim.simulate --players 3
    python -m sim.simulate --trouble         # camera loses tracking / gets blocked / restarts each lap
    python -m sim.simulate --players 4 --teams   # red/blue players in the match: try the bases and the flag

Each sim player runs camera tracking with step counting and compass, like a real
phone. The host map shows its estimate, particles and a white X at the true position.
With --teams they wear tags 1..n on alternating teams (even: red, odd: blue; the
two teams walk the path in opposite directions) and join the shooting server, so
they count for the flag. They never shoot.
"""

import argparse
import asyncio
import json
import ssl
import time
import urllib.parse
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
    try:
        async for _ in ws:
            pass
    except websockets.ConnectionClosed:
        pass  # whoever sends on it notices too


def team_of(idx: int) -> str:
    return "red" if idx % 2 == 0 else "blue"


async def fighter(url: str, idx: int):
    """Join the shooting server as tag idx + 1, so the match counts this sim player; rejoin if it drops."""
    while True:
        try:
            async with websockets.connect(url, ssl=insecure_ssl() if url.startswith("wss") else None) as ws:
                await ws.send(json.dumps({"type": "register", "player_id": idx + 1, "team": team_of(idx)}))
                await drain(ws)
        except (OSError, websockets.WebSocketException) as exc:
            print(f"Sim {idx}: shooting server at {url} not reachable ({exc}), retrying")
        await asyncio.sleep(2)


async def player(url: str, idx: int, trouble: bool, teams: bool):
    """Walk the path lap after lap. If the server drops us (restarted, or the host removed this
    player), rejoin like a phone does and start the lap over, which places the player again."""
    path = DEMO_PATH if idx % 2 == 0 else list(reversed(DEMO_PATH))
    hello = {"role": "player", "id": f"sim-{idx}", "name": f"Sim {idx}", "platform": "sim"}
    if teams:
        hello.update(tag=idx + 1, team=team_of(idx))
    lap = 0
    while True:
        try:
            async with websockets.connect(url, ssl=insecure_ssl() if url.startswith("wss") else None) as ws:
                await ws.send(json.dumps(hello))
                reader = asyncio.create_task(drain(ws))
                try:
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
                finally:
                    reader.cancel()
        except (OSError, websockets.WebSocketException) as exc:
            print(f"Sim {idx}: game server at {url} dropped or not reachable ({exc}), rejoining")
        await asyncio.sleep(2)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="https://localhost:8443")
    parser.add_argument("--players", type=int, default=1)
    parser.add_argument("--demo-map", action="store_true", help="replace the server's map with the demo floor")
    parser.add_argument("--trouble", action="store_true", help="inject camera problems, one kind per lap")
    parser.add_argument("--teams", action="store_true", help="tags 1..n on alternating teams, joined to the shooting server")
    parser.add_argument("--combat", help="shooting server phone socket (default: wss://<server host>:8766/ws/phone)")
    args = parser.parse_args()

    if args.demo_map:
        put_demo_map(args.server)
    url = args.server.replace("http", "ws", 1) + "/ws"
    combat = args.combat or f"wss://{urllib.parse.urlparse(args.server).hostname}:8766/ws/phone"
    tasks = [player(url, i, args.trouble, args.teams) for i in range(args.players)]
    if args.teams:
        tasks += [fighter(combat, i) for i in range(args.players)]
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
