// File-backed store of maps. Each map lives in maps/<id>/ with a map.json describing it.
import fs from "node:fs";
import path from "node:path";
import { EventEmitter } from "node:events";

export type MapStatus = "uploaded" | "training" | "ready" | "failed";

/** One scan inside a merged map: its splat file and where it sits in the merged map. */
export interface MapPart {
  id: string; // the source map
  splatFile: string;
  /** Column-major 4x4 taking the part's raw splat coords into the merged map's frame. */
  transform: number[];
}

export interface MapInfo {
  id: string;
  name: string;
  createdAt: string;
  source: "scan" | "upload" | "merged";
  /** Merged maps: the scans it's made of. Their files stay in the source maps' folders. */
  parts?: MapPart[];
  status: MapStatus;
  progress: number; // 0..1 while training
  /** Latest renderable splat file inside the map folder (training previews replace it). */
  splatFile: string | null;
  /** Column-major 4x4 taking raw splat coords to map coords (metres, Y up, floor at 0). */
  transform: number[] | null;
  frames?: number;
  error?: string;
}

export const MAPS_DIR = path.resolve(import.meta.dirname, "../maps");
const ACTIVE_FILE = path.join(MAPS_DIR, "active.json");

export const events = new EventEmitter();
fs.mkdirSync(MAPS_DIR, { recursive: true });

export function mapDir(id: string) {
  if (!/^[a-z0-9-]+$/.test(id)) throw new Error(`bad map id ${id}`);
  return path.join(MAPS_DIR, id);
}

export function newMapId(name: string) {
  const slug = name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 32) || "map";
  return `${slug}-${Date.now().toString(36)}`;
}

export function listMaps(): MapInfo[] {
  return fs
    .readdirSync(MAPS_DIR, { withFileTypes: true })
    .filter((d) => d.isDirectory() && fs.existsSync(path.join(MAPS_DIR, d.name, "map.json")))
    .map((d) => getMap(d.name)!)
    .sort((a, b) => b.createdAt.localeCompare(a.createdAt));
}

export function getMap(id: string): MapInfo | null {
  try {
    return JSON.parse(fs.readFileSync(path.join(mapDir(id), "map.json"), "utf8"));
  } catch {
    return null;
  }
}

export function saveMap(map: MapInfo) {
  fs.mkdirSync(mapDir(map.id), { recursive: true });
  const file = path.join(mapDir(map.id), "map.json");
  fs.writeFileSync(file + ".tmp", JSON.stringify(map, null, 2));
  fs.renameSync(file + ".tmp", file);
  events.emit("change");
}

export function updateMap(id: string, patch: Partial<MapInfo>): MapInfo | null {
  const map = getMap(id);
  if (!map) return null;
  const next = { ...map, ...patch, id };
  saveMap(next);
  return next;
}

export function deleteMap(id: string) {
  fs.rmSync(mapDir(id), { recursive: true, force: true });
  if (getActiveId() === id) fs.rmSync(ACTIVE_FILE, { force: true });
  events.emit("change");
}

export function getActiveId(): string | null {
  try {
    const { id } = JSON.parse(fs.readFileSync(ACTIVE_FILE, "utf8"));
    return getMap(id) ? id : null;
  } catch {
    return null;
  }
}

export function setActiveId(id: string) {
  fs.writeFileSync(ACTIVE_FILE, JSON.stringify({ id }));
  events.emit("change");
}
