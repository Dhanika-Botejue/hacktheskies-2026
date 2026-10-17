// Laptop server: serves the viewer (Vite), receives scans from the phone app, trains them into
// Gaussian splats and pushes map updates to viewers over Server-Sent Events.
import { spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import https from "node:https";
import os from "node:os";
import path from "node:path";
import type { Duplex } from "node:stream";
import { pipeline } from "node:stream/promises";
import QRCode from "qrcode";
import { createServer as createVite } from "vite";
import {
  MAPS_DIR, deleteMap, events, getActiveId, getMap, listMaps, mapDir, newMapId, saveMap, setActiveId, updateMap,
} from "./maps.ts";
import { enqueueTraining, resumeInterruptedTraining } from "./train.ts";

const PORT = Number(process.env.PORT ?? 5173);
const SPLAT_EXTS = [".ply", ".spz", ".splat", ".ksplat", ".sog"];
// Python game server (run.py): tracks the players. Proxied under /game/ so the viewer reaches it
// on its own origin, without tripping over the game server's self-signed certificate.
const GAME_SERVER = new URL(process.env.GAME_SERVER ?? "https://127.0.0.1:8443");

function lanAddress() {
  const ifaces = os.networkInterfaces();
  const order = ["en0", "en1", ...Object.keys(ifaces)];
  for (const name of order) {
    const v4 = ifaces[name]?.find((a) => a.family === "IPv4" && !a.internal);
    if (v4) return v4.address;
  }
  return "localhost";
}

const serverUrl = `http://${lanAddress()}:${PORT}`;
// Scanning this with the iPhone camera opens our app and points it at this laptop.
const connectUrl = `hacktheskies://connect?server=${encodeURIComponent(serverUrl)}`;

function json(res: http.ServerResponse, status: number, body: unknown) {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(body));
}

async function readJson(req: http.IncomingMessage) {
  let body = "";
  for await (const chunk of req) body += chunk;
  return JSON.parse(body || "{}");
}

function unzip(zip: string, dest: string) {
  return new Promise<void>((resolve, reject) => {
    const p = spawn("unzip", ["-q", "-o", zip, "-d", dest]);
    p.on("error", reject);
    p.on("close", (code) => (code === 0 ? resolve() : reject(new Error(`unzip exited with ${code}`))));
  });
}

function findFile(dir: string, name: string): string | null {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, entry.name);
    if (entry.isFile() && entry.name === name) return p;
    if (entry.isDirectory() && !entry.name.startsWith("__MACOSX")) {
      const found = findFile(p, name);
      if (found) return found;
    }
  }
  return null;
}

/** POST /api/scans?name=...  body: zip of images/ + transforms.json + points.ply from the phone. */
async function receiveScan(req: http.IncomingMessage, res: http.ServerResponse, url: URL) {
  const name = url.searchParams.get("name") || `Scan ${new Date().toLocaleString()}`;
  const id = newMapId(name);
  const dir = mapDir(id);
  fs.mkdirSync(dir, { recursive: true });
  const zip = path.join(dir, "upload.zip");
  await pipeline(req, fs.createWriteStream(zip));

  const dataset = path.join(dir, "dataset");
  await unzip(zip, dataset);
  fs.rmSync(zip);
  let transforms = findFile(dataset, "transforms.json");
  if (!transforms) {
    fs.rmSync(dir, { recursive: true, force: true });
    return json(res, 400, { error: "zip has no transforms.json" });
  }
  // iOS zips a folder with the folder itself at the top level; make the dataset root the folder
  // that holds transforms.json so Brush sees a flat dataset.
  const root = path.dirname(transforms);
  if (root !== dataset) {
    const tmp = path.join(dir, "dataset-tmp");
    fs.renameSync(root, tmp);
    fs.rmSync(dataset, { recursive: true, force: true });
    fs.renameSync(tmp, dataset);
    transforms = path.join(dataset, "transforms.json");
  }
  const frames = JSON.parse(fs.readFileSync(transforms, "utf8")).frames?.length ?? 0;

  saveMap({
    id, name, createdAt: new Date().toISOString(), source: "scan", status: "uploaded",
    progress: 0, splatFile: null, transform: null, frames,
  });
  setActiveId(id);
  enqueueTraining(id);
  console.log(`[scan] received ${id} (${frames} frames)`);
  json(res, 201, { id });
}

/** POST /api/maps?name=...&filename=scan.spz  body: a finished splat file (Scaniverse etc. fallback). */
async function receiveSplat(req: http.IncomingMessage, res: http.ServerResponse, url: URL) {
  const filename = url.searchParams.get("filename") ?? "";
  const ext = path.extname(filename).toLowerCase();
  if (!SPLAT_EXTS.includes(ext)) return json(res, 400, { error: `unsupported file type ${ext}` });
  const name = url.searchParams.get("name") || path.basename(filename, ext);
  const id = newMapId(name);
  fs.mkdirSync(mapDir(id), { recursive: true });
  await pipeline(req, fs.createWriteStream(path.join(mapDir(id), `scan${ext}`)));
  saveMap({
    id, name, createdAt: new Date().toISOString(), source: "upload", status: "ready",
    progress: 1, splatFile: `scan${ext}`, transform: null,
  });
  setActiveId(id);
  json(res, 201, { id });
}

function serveMapFile(res: http.ServerResponse, rel: string) {
  const file = path.resolve(MAPS_DIR, rel);
  if (!file.startsWith(MAPS_DIR + path.sep) || !SPLAT_EXTS.includes(path.extname(file)) || !fs.existsSync(file)) {
    res.writeHead(404).end();
    return;
  }
  res.writeHead(200, {
    "content-type": "application/octet-stream",
    "content-length": fs.statSync(file).size,
    "cache-control": "public, max-age=31536000, immutable",
  });
  fs.createReadStream(file).pipe(res);
}

function sse(req: http.IncomingMessage, res: http.ServerResponse) {
  res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache", connection: "keep-alive" });
  const send = () => res.write(`data: ${JSON.stringify({ maps: listMaps(), activeId: getActiveId() })}\n\n`);
  send();
  events.on("change", send);
  const ping = setInterval(() => res.write(": ping\n\n"), 15000);
  req.on("close", () => {
    events.off("change", send);
    clearInterval(ping);
  });
}

async function handleApi(req: http.IncomingMessage, res: http.ServerResponse, url: URL) {
  const parts = url.pathname.split("/").filter(Boolean); // ["api", ...]
  const method = req.method ?? "GET";

  if (parts[1] === "events") return sse(req, res);
  if (parts[1] === "info") {
    const qrSvg = await QRCode.toString(connectUrl, { type: "svg", margin: 1 });
    return json(res, 200, { serverUrl, connectUrl, qrSvg });
  }
  if (parts[1] === "scans" && method === "POST") return receiveScan(req, res, url);
  if (parts[1] === "maps" && parts.length === 2) {
    if (method === "GET") return json(res, 200, { maps: listMaps(), activeId: getActiveId() });
    if (method === "POST") return receiveSplat(req, res, url);
  }
  if (parts[1] === "maps" && parts[2]) {
    const id = parts[2];
    if (!getMap(id)) return json(res, 404, { error: "no such map" });
    if (parts[3] === "activate" && method === "POST") {
      setActiveId(id);
      return json(res, 200, { ok: true });
    }
    if (parts[3] === "retrain" && method === "POST") {
      enqueueTraining(id);
      return json(res, 200, { ok: true });
    }
    if (method === "PUT") {
      const body = await readJson(req);
      const patch: Record<string, unknown> = {};
      if (Array.isArray(body.transform) && body.transform.length === 16) patch.transform = body.transform.map(Number);
      if (body.transform === null) patch.transform = null;
      if (typeof body.name === "string") patch.name = body.name.slice(0, 80);
      return json(res, 200, updateMap(id, patch));
    }
    if (method === "DELETE") {
      deleteMap(id);
      return json(res, 200, { ok: true });
    }
  }
  json(res, 404, { error: "not found" });
}

// ---- Game server proxy ----------------------------------------------------------------------

function gameRequest(req: http.IncomingMessage) {
  const lib = GAME_SERVER.protocol === "https:" ? https : http;
  return lib.request({
    hostname: GAME_SERVER.hostname,
    port: GAME_SERVER.port,
    path: req.url!.slice("/game".length) || "/",
    method: req.method,
    headers: { ...req.headers, host: GAME_SERVER.host },
    rejectUnauthorized: false, // run.py's self-signed certificate
  });
}

function proxyGame(req: http.IncomingMessage, res: http.ServerResponse) {
  const upstream = gameRequest(req);
  upstream.on("response", (up) => {
    res.writeHead(up.statusCode ?? 502, up.headers);
    up.pipe(res);
  });
  upstream.on("error", () => {
    if (!res.headersSent) json(res, 502, { error: `game server not reachable at ${GAME_SERVER.origin}` });
    else res.destroy();
  });
  req.pipe(upstream);
}

function proxyGameSocket(req: http.IncomingMessage, socket: Duplex, head: Buffer) {
  const upstream = gameRequest(req);
  upstream.on("upgrade", (up, upSocket, upHead) => {
    const lines = [`HTTP/1.1 101 Switching Protocols`];
    for (let i = 0; i < up.rawHeaders.length; i += 2) lines.push(`${up.rawHeaders[i]}: ${up.rawHeaders[i + 1]}`);
    socket.write(lines.join("\r\n") + "\r\n\r\n");
    if (upHead.length) socket.write(upHead);
    if (head.length) upSocket.write(head);
    upSocket.pipe(socket).pipe(upSocket);
    const close = () => {
      socket.destroy();
      upSocket.destroy();
    };
    socket.on("error", close);
    upSocket.on("error", close);
    socket.on("close", close);
    upSocket.on("close", close);
  });
  upstream.on("response", (up) => {
    socket.end(`HTTP/1.1 ${up.statusCode} ${up.statusMessage}\r\n\r\n`);
  });
  upstream.on("error", () => socket.destroy());
  upstream.end();
}

const vite = await createVite({
  root: path.resolve(import.meta.dirname, ".."),
  server: { middlewareMode: true },
  appType: "spa",
});

const server = http.createServer((req, res) => {
  const url = new URL(req.url ?? "/", "http://x");
  if (url.pathname.startsWith("/api/")) {
    handleApi(req, res, url).catch((err) => {
      console.error(err);
      if (!res.headersSent) json(res, 500, { error: String(err.message ?? err) });
    });
    return;
  }
  if (url.pathname.startsWith("/maps/")) return serveMapFile(res, decodeURIComponent(url.pathname.slice(6)));
  if (url.pathname.startsWith("/game/")) return proxyGame(req, res);
  vite.middlewares(req, res);
});

server.on("upgrade", (req, socket, head) => {
  if (req.url?.startsWith("/game/")) proxyGameSocket(req, socket, head);
});

server.listen(PORT, "0.0.0.0", () => {
  console.log(`\n  Viewer:          http://localhost:${PORT}`);
  console.log(`  Phone app URL:   ${serverUrl}`);
  console.log(`  Game server:     ${GAME_SERVER.origin} (proxied at /game/)\n`);
  resumeInterruptedTraining();
});
