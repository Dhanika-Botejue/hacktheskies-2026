"""Game server: serves the web clients, stores the map, tracks players over WebSocket."""

import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .fusion import PlayerFusion

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
WEB = ROOT / "web"
MAP_FILE = DATA / "map.json"
IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}

DEFAULT_MAP = {"width": 40.0, "height": 30.0, "image": None, "walls": []}
BROADCAST_HZ = 20
HOST_PARTICLES = 120
SEND_TIMEOUT_S = 1.0


@dataclass
class Player:
    id: str
    name: str
    fusion: PlayerFusion = field(default_factory=PlayerFusion)
    platform: str = "web"
    connected: bool = True
    truth: list | None = None  # ground truth from the simulator, for tuning

    def to_json(self, with_particles: bool) -> dict:
        f, t, now = self.fusion, self.fusion.tracker, time.monotonic()
        d = {
            "id": self.id,
            "name": self.name,
            "platform": self.platform,
            "connected": self.connected,
            "calibrated": t.calibrated,
            "steps": f.steps,
            "lost": t.lost_steps,
            "source": f.source(now),
            "compass": f.compass is not None,
        }
        if t.calibrated:
            x, y, spread = t.estimate()
            facing = f.facing(now)
            d.update(x=round(x, 2), y=round(y, 2), spread=round(spread, 2),
                     heading=round(facing, 1) if facing is not None else 0)
            if with_particles:
                d["particles"] = t.sample_particles(HOST_PARTICLES)
        if self.truth is not None:
            d["truth"] = self.truth
        return d


class Game:
    def __init__(self):
        DATA.mkdir(exist_ok=True)
        self.map = json.loads(MAP_FILE.read_text()) if MAP_FILE.exists() else dict(DEFAULT_MAP)
        self.walls = self._walls_array()
        self.players: dict[str, Player] = {}
        self.hosts: set[WebSocket] = set()
        self.phones: dict[WebSocket, str] = {}

    def _walls_array(self) -> np.ndarray:
        return np.array(self.map["walls"], dtype=float).reshape(-1, 4)

    def set_map(self, new_map: dict) -> None:
        self.map = new_map
        self.walls = self._walls_array()
        MAP_FILE.write_text(json.dumps(new_map))

    async def broadcast(self, msg: dict, hosts=True, phones=True) -> None:
        targets = (list(self.hosts) if hosts else []) + (list(self.phones) if phones else [])
        text = json.dumps(msg)
        # A phone that stops reading (e.g. its screen locked) mustn't stall updates to everyone else.
        await asyncio.gather(*(asyncio.wait_for(ws.send_text(text), SEND_TIMEOUT_S) for ws in targets),
                             return_exceptions=True)

    async def broadcast_loop(self) -> None:
        while True:
            await asyncio.sleep(1 / BROADCAST_HZ)
            if not (self.hosts or self.phones):
                continue
            players = list(self.players.values())
            await self.broadcast(
                {"t": "state", "players": [p.to_json(True) for p in players]}, phones=False)
            await self.broadcast(
                {"t": "state", "players": [p.to_json(False) for p in players]}, hosts=False)

    def handle_phone(self, pid: str, msg: dict) -> None:
        f, kind, now = self.players[pid].fusion, msg.get("t"), time.monotonic()
        if kind == "heading":
            f.on_heading(num(msg.get("gyro")), num(msg.get("compass")), now)
        elif kind == "pose":
            f.on_pose(float(msg["x"]), float(msg["z"]), float(msg["yaw"]), bool(msg["tracking"]),
                      int(msg.get("epoch", 0)), now, self.walls)
        elif kind == "step":
            f.on_step(num(msg.get("heading")), float(msg["len"]), msg.get("src", "gyro"), now, self.walls)
        elif kind == "calibrate":
            f.calibrate(float(msg["x"]), float(msg["y"]), float(msg["mapHeading"]), now)
        elif kind == "truth":
            self.players[pid].truth = [msg["x"], msg["y"]]

    def handle_host(self, msg: dict) -> None:
        kind = msg.get("t")
        p = self.players.get(msg.get("id", ""))
        if kind == "place" and p:
            p.fusion.calibrate(float(msg["x"]), float(msg["y"]), float(msg["mapHeading"]), time.monotonic())
        elif kind == "remove" and p:
            del self.players[p.id]


def num(v) -> float | None:
    return None if v is None else float(v)


game = Game()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(game.broadcast_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/")
def index():
    return RedirectResponse("/web/index.html")


@app.get("/api/map")
def get_map():
    return game.map


@app.put("/api/map")
async def put_map(request: Request):
    new_map = await request.json()
    game.set_map(new_map)
    await game.broadcast({"t": "map", "map": new_map})
    return {"ok": True}


@app.post("/api/map/image")
async def upload_image(request: Request):
    ext = IMAGE_TYPES.get(request.headers.get("content-type", ""))
    if not ext:
        raise HTTPException(415, "Use PNG, JPEG or WebP")
    for old in DATA.glob("map-image.*"):
        old.unlink()
    (DATA / f"map-image.{ext}").write_bytes(await request.body())
    return {"url": f"/api/map/image?v={int(time.time())}"}


@app.get("/api/map/image")
def get_image():
    found = next(DATA.glob("map-image.*"), None)
    if not found:
        raise HTTPException(404)
    return FileResponse(found)


@app.websocket("/ws")
async def websocket(ws: WebSocket):
    await ws.accept()
    hello = await ws.receive_json()
    role = hello.get("role")
    pid = None
    try:
        if role == "host":
            game.hosts.add(ws)
        else:
            pid = str(hello["id"])
            name = str(hello.get("name") or pid[:6])
            p = game.players.get(pid) or Player(id=pid, name=name)
            p.name, p.connected = name, True
            p.platform = str(hello.get("platform", "web"))
            game.players[pid] = p
            game.phones[ws] = pid
        await ws.send_json({"t": "map", "map": game.map})

        while True:
            msg = await ws.receive_json()
            if role == "host":
                game.handle_host(msg)
            elif pid in game.players:
                game.handle_phone(pid, msg)
    except WebSocketDisconnect:
        pass
    finally:
        game.hosts.discard(ws)
        game.phones.pop(ws, None)
        if pid in game.players and pid not in game.phones.values():
            game.players[pid].connected = False


app.mount("/web", StaticFiles(directory=WEB), name="web")
