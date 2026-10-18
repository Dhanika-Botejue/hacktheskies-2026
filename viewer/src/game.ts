// Link to the game server (run.py, proxied at /game/): live player positions, the game map and
// player placement.
//
// Game coordinates are the 2D host map's: metres, x right, y down, headings in degrees clockwise
// from map "up". On a scanned map they lie on the splat's floor plane: X = x + x0, Z = y + z0,
// so map "up" (-y) is -Z, which is also "up" in the viewer's top-down overview.
import * as THREE from "three";
import type { SplatMesh } from "@sparkjsdev/spark";
import type { MapInfo } from "../server/maps.ts";
import type { Team } from "./combat.ts";

export interface GamePlayer {
  id: string;
  name: string;
  platform: string;
  connected: boolean;
  /** Number on their chest tag: their player ID on the shooting server. */
  tag: number | null;
  team: Team | null;
  calibrated: boolean;
  steps: number;
  lost: number;
  source: string;
  compass: boolean;
  x?: number;
  y?: number;
  spread?: number;
  heading?: number;
}

/** An objective the host marked on the map (game map metres, r = radius): a team's base, where
 * its downed players come back in, or the flag. One of each; the id is "base-red", "base-blue" or "flag". */
export interface MapObjective {
  id: string;
  kind: "base" | "flag";
  team?: Team;
  name: string;
  x: number;
  y: number;
  r: number;
}

/** Which splat map (and which calibration of it) the game map was made from. */
export interface SplatLink {
  id: string;
  splatFile: string | null;
  transform: number[];
  x0: number;
  z0: number;
}

export interface GameMap {
  width: number;
  height: number;
  image: { url: string; px: [number, number] } | null;
  walls: number[][];
  points?: MapObjective[];
  splat?: SplatLink;
}

// Same palette and hash as web/js/mapview.js, so a player has one colour everywhere.
const PLAYER_COLORS = ["#e4572e", "#29b6f6", "#76b041", "#ffc914", "#b07bd6", "#ff8fab", "#17bebb", "#f0f0f0"];

export function colorFor(id: string) {
  let h = 0;
  for (const ch of id) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return PLAYER_COLORS[h % PLAYER_COLORS.length];
}

/** Game map heading (deg) <-> three.js camera yaw (rad, 0 = looking along -Z). */
export const headingToYaw = (heading: number) => (-heading * Math.PI) / 180;
export const yawToHeading = (yaw: number) => ((((-yaw * 180) / Math.PI) % 360) + 360) % 360;

export function mapOrigin(map: GameMap | null) {
  if (map?.splat) return { x: map.splat.x0, z: map.splat.z0 };
  // A hand-drawn 2D map with no scan behind it: centre it on the 3D origin.
  return { x: -(map?.width ?? 0) / 2, z: -(map?.height ?? 0) / 2 };
}

export class GameLink {
  connected = false;
  map: GameMap | null = null;
  players: GamePlayer[] = [];
  onMap: (map: GameMap) => void = () => {};
  onPlayers: (players: GamePlayer[]) => void = () => {};
  onStatus: (connected: boolean) => void = () => {};
  private ws: WebSocket | null = null;
  private delay = 500;

  constructor() {
    this.open();
  }

  private open() {
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/game/ws`);
    this.ws = ws;
    ws.onopen = () => {
      this.delay = 500;
      ws.send(JSON.stringify({ role: "host" }));
    };
    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data);
      if (!this.connected) {
        this.connected = true;
        this.onStatus(true);
      }
      if (msg.t === "map") {
        this.map = msg.map;
        this.onMap(msg.map);
      } else if (msg.t === "state") {
        this.players = msg.players;
        this.onPlayers(msg.players);
      }
    };
    ws.onclose = () => {
      if (this.connected) {
        this.connected = false;
        this.players = [];
        this.onStatus(false);
        this.onPlayers([]);
      }
      setTimeout(() => this.open(), this.delay);
      this.delay = Math.min(this.delay * 2, 5000);
    };
  }

  private send(msg: unknown) {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(msg));
  }

  /** Put a player at a world-space floor point, facing a game map heading. */
  place(id: string, X: number, Z: number, heading: number) {
    const o = mapOrigin(this.map);
    this.send({ t: "place", id, x: X - o.x, y: Z - o.z, mapHeading: heading });
  }

  remove(id: string) {
    this.send({ t: "remove", id });
  }

  /** Replace the bases and flag; the server stores them with the map and sends it back. */
  setPoints(points: MapObjective[]) {
    this.send({ t: "points", points });
  }

  /** World-space floor position of a game map point. */
  toWorld(x: number, y: number) {
    const o = mapOrigin(this.map);
    return { x: x + o.x, z: y + o.z };
  }

  imageUrl() {
    return this.map?.image ? `/game${this.map.image.url}` : null;
  }
}

// ---- Making a game map from a splat ---------------------------------------------------------

const CEILING_CUT = 2.0; // m above the floor; splats higher up (ceiling, lights) are left out
const MARGIN = 1.5; // m of empty floor around the scanned footprint
const PX_PER_M = 25;
const MAX_PX = 2048;

/** The raw->map transform minus its vertical offset: walls and players stay valid while this matches. */
function planar(transform: number[]) {
  return transform.filter((_, i) => i !== 13).map((v) => Math.round(v * 1e4) / 1e4);
}

function sameFrame(a: number[], b: number[]) {
  return JSON.stringify(planar(a)) === JSON.stringify(planar(b));
}

export function isLinked(game: GameMap | null, map: MapInfo, transform: number[]) {
  const s = game?.splat;
  return !!s && s.id === map.id && s.splatFile === map.splatFile && JSON.stringify(s.transform) === JSON.stringify(transform);
}

interface FloorPoint {
  x: number;
  y: number;
  z: number;
  r: number;
  g: number;
  b: number;
}

function floorPoints(mesh: SplatMesh, transform: number[]): FloorPoint[] {
  const m = new THREE.Matrix4().fromArray(transform);
  const total = mesh.packedSplats?.numSplats ?? 0;
  const stride = Math.max(1, Math.floor(total / 600_000));
  const p = new THREE.Vector3();
  const out: FloorPoint[] = [];
  mesh.forEachSplat((i, c, _s, _q, opacity, color) => {
    if (i % stride !== 0 || opacity < 0.25) return;
    p.copy(c).applyMatrix4(m);
    if (p.y < -0.3 || p.y > CEILING_CUT) return;
    out.push({ x: p.x, y: p.y, z: p.z, r: color.r, g: color.g, b: color.b });
  });
  return out;
}

function percentile(values: number[], q: number) {
  const s = Float64Array.from(values).sort();
  return s[Math.min(s.length - 1, Math.floor(s.length * q))];
}

/**
 * Top-down picture of the scan: for every pixel the highest splat below the ceiling cut, like a
 * camera looking straight down with the ceiling removed. Taller things are drawn brighter so
 * walls and furniture stand out from the floor.
 */
function renderFloorPlan(points: FloorPoint[], x0: number, z0: number, width: number, height: number) {
  const ppm = Math.min(PX_PER_M, MAX_PX / Math.max(width, height));
  const w = Math.max(1, Math.round(width * ppm));
  const h = Math.max(1, Math.round(height * ppm));
  const top = new Float32Array(w * h).fill(-Infinity);
  const rgb = new Uint8ClampedArray(w * h * 3);
  const r = ppm > 15 ? 1 : 0; // splat radius in pixels
  for (const p of points) {
    const cx = Math.floor((p.x - x0) * ppm);
    const cy = Math.floor((p.z - z0) * ppm);
    const shade = 0.55 + 0.45 * Math.min(1, Math.max(0, p.y / 1.2));
    for (let dy = -r; dy <= r; dy++) {
      for (let dx = -r; dx <= r; dx++) {
        const px = cx + dx;
        const py = cy + dy;
        if (px < 0 || py < 0 || px >= w || py >= h) continue;
        const k = py * w + px;
        if (p.y <= top[k]) continue;
        top[k] = p.y;
        rgb[3 * k] = p.r * 255 * shade;
        rgb[3 * k + 1] = p.g * 255 * shade;
        rgb[3 * k + 2] = p.b * 255 * shade;
      }
    }
  }
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d")!;
  const img = ctx.createImageData(w, h);
  for (let k = 0; k < w * h; k++) {
    if (top[k] === -Infinity) continue;
    img.data.set([rgb[3 * k], rgb[3 * k + 1], rgb[3 * k + 2], 255], 4 * k);
  }
  ctx.putImageData(img, 0, 0);
  return canvas;
}

/**
 * Makes the splat map the game map: sends the game server the scan's footprint and a top-down
 * floor plan, so the 2D host page, the phones and the particle filter all use the scan's floor
 * coordinates. Walls drawn on the 2D host page and the map origin are kept while the map's
 * calibration (apart from the floor height) stays the same, so placed players keep their spots.
 */
export async function publishGameMap(game: GameMap | null, map: MapInfo, mesh: SplatMesh, transform: number[]) {
  const points = floorPoints(mesh, transform);
  if (points.length < 100) return null;
  const xs = points.map((p) => p.x);
  const zs = points.map((p) => p.z);
  const minX = percentile(xs, 0.005) - MARGIN;
  const maxX = percentile(xs, 0.995) + MARGIN;
  const minZ = percentile(zs, 0.005) - MARGIN;
  const maxZ = percentile(zs, 0.995) + MARGIN;

  const prev = game?.splat;
  const keep = !!prev && prev.id === map.id && sameFrame(prev.transform, transform);
  const x0 = keep ? prev.x0 : Math.floor(minX * 2) / 2;
  const z0 = keep ? prev.z0 : Math.floor(minZ * 2) / 2;
  // The origin stays put once players are placed; the map may only grow to the right/bottom.
  const width = Math.max(keep ? game!.width : 0, Math.ceil((maxX - x0) * 2) / 2);
  const height = Math.max(keep ? game!.height : 0, Math.ceil((maxZ - z0) * 2) / 2);

  const canvas = renderFloorPlan(points, x0, z0, width, height);
  const blob = await new Promise<Blob>((resolve, reject) =>
    canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("floor plan encode failed"))), "image/png"),
  );
  const up = await fetch("/game/api/map/image", { method: "POST", headers: { "content-type": "image/png" }, body: blob });
  if (!up.ok) throw new Error(`floor plan upload failed: ${up.status}`);
  const { url } = await up.json();

  const next: GameMap = {
    width,
    height,
    image: { url, px: [canvas.width, canvas.height] },
    walls: keep ? game!.walls : [],
    points: keep ? (game!.points ?? []) : [],
    splat: { id: map.id, splatFile: map.splatFile, transform, x0, z0 },
  };
  const put = await fetch("/game/api/map", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    // The bases and flag stay with the server unless the frame changed and they'd be in the wrong place.
    body: JSON.stringify({ ...next, pointsReset: !keep }),
  });
  if (!put.ok) throw new Error(`game map update failed: ${put.status}`);
  return next;
}
