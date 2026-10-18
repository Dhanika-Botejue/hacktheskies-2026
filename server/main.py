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

DEFAULT_MAP = {"width": 40.0, "height": 30.0, "image": None, "walls": [], "points": []}
BROADCAST_HZ = 20
HOST_PARTICLES = 120
SILENT_S = 5.0  # a phone not heard from this long is gone (screen locked, out of Wi-Fi), even if its socket isn't closed yet
TEAMS = ("red", "blue")
MAX_TAG = 34  # player bibs are tag25h9 AprilTags 1-34, the IDs the shooting server (combat/) uses
POINT_RADIUS = (0.5, 10.0)  # m
# The game's objectives (the "points" on the map): each team's base, where its downed players come
# back in, and the flag in the middle. The shooting server (combat/) runs the rules.
OBJECTIVES = {("base", "red"): "Red base", ("base", "blue"): "Blue base", ("flag", None): "Flag"}


@dataclass
class Player:
    id: str
    name: str
    fusion: PlayerFusion = field(default_factory=PlayerFusion)
    platform: str = "web"
    connected: bool = True
    truth: list | None = None  # ground truth from the simulator, for tuning
    tag: int | None = None  # number on the player's chest tag: their ID on the shooting server
    team: str | None = None
    heard_at: float = field(default_factory=time.monotonic)

    def to_json(self, with_particles: bool) -> dict:
        f, t, now = self.fusion, self.fusion.tracker, time.monotonic()
        d = {
            "id": self.id,
            "name": self.name,
            "platform": self.platform,
            # A frozen phone's last position mustn't keep counting (e.g. holding the flag).
            "connected": self.connected and now - self.heard_at < SILENT_S,
            "tag": self.tag,
            "team": self.team,
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


class Outbox:
    """What's waiting to go out to one WebSocket: the latest message of each kind ("state",
    "map"), since each replaces the last. Its own task sends them, so a phone that stops reading
    (its screen locked, or it walked out of Wi-Fi) holds up only itself, not everyone's updates."""

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.pending: dict[str, str] = {}
        self.ready = asyncio.Event()
        self.closing = False
        self.task = asyncio.create_task(self._run())

    def put(self, kind: str, text: str) -> None:
        self.pending[kind] = text
        self.ready.set()

    def hang_up(self) -> None:
        self.closing = True
        self.ready.set()

    async def _run(self) -> None:
        try:
            while not self.closing:
                await self.ready.wait()
                self.ready.clear()
                while self.pending and not self.closing:
                    await self.ws.send_text(self.pending.pop(next(iter(self.pending))))
            await self.ws.close()
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass  # the socket closed; its handler cleans up


class Game:
    def __init__(self):
        DATA.mkdir(exist_ok=True)
        self.map = json.loads(MAP_FILE.read_text()) if MAP_FILE.exists() else dict(DEFAULT_MAP)
        self.walls = walls_array(self.map)
        self.players: dict[str, Player] = {}
        self.hosts: set[WebSocket] = set()
        self.phones: dict[WebSocket, str] = {}
        self.watchers: set[WebSocket] = set()  # read-only: the shooting server, for positions and objectives
        self.outboxes: dict[WebSocket, Outbox] = {}

    def set_map(self, new_map: dict) -> None:
        walls = walls_array(new_map)  # first, so a bad map is refused rather than half taken
        self.map, self.walls = new_map, walls
        MAP_FILE.write_text(json.dumps(new_map))

    def broadcast(self, msg: dict, hosts=True, phones=True) -> None:
        """phones: the phones and the watchers, which get the same particle-free updates."""
        targets = (list(self.hosts) if hosts else []) + (list(self.phones) + list(self.watchers) if phones else [])
        text = json.dumps(msg)
        for ws in targets:
            if ws in self.outboxes:
                self.outboxes[ws].put(msg["t"], text)

    async def broadcast_loop(self) -> None:
        while True:
            await asyncio.sleep(1 / BROADCAST_HZ)
            if not (self.hosts or self.phones or self.watchers):
                continue
            players = list(self.players.values())
            self.broadcast({"t": "state", "players": [p.to_json(True) for p in players]}, phones=False)
            self.broadcast({"t": "state", "players": [p.to_json(False) for p in players]}, hosts=False)

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

    async def handle_host(self, msg: dict) -> None:
        kind = msg.get("t")
        p = self.players.get(msg.get("id", ""))
        if kind == "place" and p:
            p.fusion.calibrate(float(msg["x"]), float(msg["y"]), float(msg["mapHeading"]), time.monotonic())
        elif kind == "remove" and p:
            del self.players[p.id]
            # Hang up on their phone too: it reconnects and joins afresh, rather than carrying on
            # unheard (the server ignores players it doesn't know).
            for ws, pid in self.phones.items():
                if pid == p.id and ws in self.outboxes:
                    self.outboxes[ws].hang_up()
        elif kind == "points":
            # Bases and flag the host marked on the map, replaced as a whole.
            self.set_map({**self.map, "points": clean_points(msg.get("points"))})
            self.broadcast({"t": "map", "map": self.map})


def walls_array(m: dict) -> np.ndarray:
    return np.array(m["walls"], dtype=float).reshape(-1, 4)


def num(v) -> float | None:
    return None if v is None else float(v)


def parse_tag(v) -> int | None:
    try:
        tag = int(v)
    except (TypeError, ValueError):
        return None
    return tag if 1 <= tag <= MAX_TAG else None


def parse_team(v) -> str | None:
    team = str(v or "").lower()
    return team if team in TEAMS else None


def clean_points(points) -> list[dict]:
    """Objectives: [{id, kind, team, name, x, y, r}] in map metres, r = radius. kind "base" (with
    its team) or "flag"; one of each at most, the first one given wins."""
    out = {}
    for p in points if isinstance(points, list) else []:
        try:
            kind = str(p["kind"])
            team = parse_team(p.get("team")) if kind == "base" else None
            name = OBJECTIVES[(kind, team)]
            if (kind, team) in out:
                continue
            out[(kind, team)] = {
                "id": f"{kind}-{team}" if team else kind,
                "kind": kind,
                **({"team": team} if team else {}),
                "name": name,
                "x": round(float(p["x"]), 2),
                "y": round(float(p["y"]), 2),
                "r": round(min(max(float(p.get("r", 2.0)), POINT_RADIUS[0]), POINT_RADIUS[1]), 2),
            }
        except (KeyError, TypeError, ValueError):
            continue
    return list(out.values())


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
    if not isinstance(new_map, dict):
        raise HTTPException(400, "Expected a map object")
    # Objectives (bases, flag) only change through the host's "points" message: pages that save the map
    # (walls on the 2D host page) may hold an old copy of them. The viewer clears them when the
    # map's frame changes (a new scan, flip or rotate), since they'd no longer line up.
    new_map["points"] = [] if new_map.pop("pointsReset", False) else game.map.get("points", [])
    try:
        game.set_map(new_map)
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "Walls must be a list of [x1, y1, x2, y2]")
    game.broadcast({"t": "map", "map": new_map})
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
    pid = None
    try:
        hello = await ws.receive_json()
        role = hello.get("role")
        if role == "host":
            game.hosts.add(ws)
        elif role == "watch":
            game.watchers.add(ws)
        else:
            pid = str(hello["id"])
            name = str(hello.get("name") or pid[:6])
            p = game.players.get(pid) or Player(id=pid, name=name)
            p.name, p.connected = name, True
            p.platform = str(hello.get("platform", "web"))
            p.tag, p.team = parse_tag(hello.get("tag")), parse_team(hello.get("team"))
            p.heard_at = time.monotonic()
            game.players[pid] = p
            game.phones[ws] = pid
        game.outboxes[ws] = Outbox(ws)
        game.outboxes[ws].put("map", json.dumps({"t": "map", "map": game.map}))

        while True:
            msg = await ws.receive_json()
            try:
                if role == "host":
                    await game.handle_host(msg)
                elif pid in game.players:
                    game.players[pid].heard_at = time.monotonic()
                    game.handle_phone(pid, msg)
            except (AttributeError, KeyError, TypeError, ValueError):
                continue  # a malformed message: skip it rather than drop the connection
    except WebSocketDisconnect:
        pass
    finally:
        game.hosts.discard(ws)
        game.watchers.discard(ws)
        game.phones.pop(ws, None)
        if outbox := game.outboxes.pop(ws, None):
            outbox.task.cancel()
        if pid in game.players and pid not in game.phones.values():
            game.players[pid].connected = False


app.mount("/web", StaticFiles(directory=WEB), name="web")
