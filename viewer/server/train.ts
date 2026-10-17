// Runs Brush (Gaussian-splat trainer, Metal GPU) on uploaded scans, one at a time.
// Brush exports a preview .ply every EXPORT_EVERY steps; each finished preview becomes the
// map's splatFile so the viewer shows the map sharpening while it trains.
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { getMap, listMaps, mapDir, updateMap } from "./maps.ts";

const BRUSH_BIN = process.env.BRUSH_BIN ?? path.resolve(import.meta.dirname, "../../tools/bin/brush_app");
const TOTAL_STEPS = Number(process.env.TRAIN_STEPS ?? 15000);
const EXPORT_EVERY = Number(process.env.EXPORT_EVERY ?? 2500);
const MAX_RESOLUTION = Number(process.env.MAX_RESOLUTION ?? 1440);

const queue: string[] = [];
let running = false;

export function enqueueTraining(id: string) {
  queue.push(id);
  void pump();
}

/** Re-queue scans whose training was interrupted by a server restart. */
export function resumeInterruptedTraining() {
  for (const map of listMaps().reverse()) {
    if (map.source === "scan" && (map.status === "uploaded" || map.status === "training")) enqueueTraining(map.id);
  }
}

async function pump() {
  if (running) return;
  const id = queue.shift();
  if (!id) return;
  running = true;
  try {
    await train(id);
  } catch (err) {
    console.error(`[train] ${id} failed:`, err);
    updateMap(id, { status: "failed", error: String((err as Error).message ?? err) });
  } finally {
    running = false;
    void pump();
  }
}

function train(id: string): Promise<void> {
  if (!fs.existsSync(BRUSH_BIN)) throw new Error(`Brush not found at ${BRUSH_BIN}. Run tools/setup.sh`);
  const dir = mapDir(id);
  const dataset = path.join(dir, "dataset");
  const outDir = path.join(dir, "splats");
  fs.rmSync(outDir, { recursive: true, force: true });
  fs.mkdirSync(outDir, { recursive: true });

  updateMap(id, { status: "training", progress: 0, error: undefined });
  console.log(`[train] ${id}: ${TOTAL_STEPS} steps`);

  const args = [
    dataset,
    "--total-steps", String(TOTAL_STEPS),
    "--export-every", String(EXPORT_EVERY),
    "--export-path", outDir,
    "--export-name", "splat_{iter}.ply",
    "--max-resolution", String(MAX_RESOLUTION),
  ];
  const proc = spawn(BRUSH_BIN, args, { env: { ...process.env, RUST_LOG: "info" } });

  // Promote a preview once its size has stopped changing (Brush writes it in place).
  const sizes = new Map<string, number>();
  let published: string | null = null;
  const publishLatest = (final: boolean) => {
    const files = fs
      .readdirSync(outDir)
      .filter((f) => /^splat_\d+\.ply$/.test(f))
      .sort((a, b) => Number(a.match(/\d+/)![0]) - Number(b.match(/\d+/)![0]));
    const latest = files.at(-1);
    if (!latest || latest === published) return;
    const size = fs.statSync(path.join(outDir, latest)).size;
    const stable = final || sizes.get(latest) === size;
    sizes.set(latest, size);
    if (!stable || size === 0) return;
    published = latest;
    updateMap(id, { splatFile: `splats/${latest}` });
    // Keep the previous preview around in case a viewer is still downloading it.
    for (const old of files.slice(0, -2)) fs.rmSync(path.join(outDir, old), { force: true });
  };
  const poll = setInterval(() => publishLatest(false), 1000);

  let lastLines: string[] = [];
  let lastProgressWrite = 0;
  const onOutput = (chunk: Buffer) => {
    for (const line of chunk.toString().split("\n")) {
      if (!line.trim()) continue;
      lastLines = [...lastLines.slice(-20), line];
      const m = line.match(/Refine iter (\d+)/);
      if (m && Date.now() - lastProgressWrite > 500) {
        lastProgressWrite = Date.now();
        updateMap(id, { progress: Math.min(1, Number(m[1]) / TOTAL_STEPS) });
      }
    }
  };
  proc.stdout.on("data", onOutput);
  proc.stderr.on("data", onOutput);

  return new Promise((resolve, reject) => {
    proc.on("error", reject);
    proc.on("close", (code) => {
      clearInterval(poll);
      if (code !== 0) {
        console.error(lastLines.join("\n"));
        return reject(new Error(`Brush exited with code ${code}: ${lastLines.at(-1) ?? ""}`));
      }
      publishLatest(true);
      if (!getMap(id)?.splatFile) return reject(new Error("Brush finished without exporting a splat"));
      updateMap(id, { status: "ready", progress: 1 });
      console.log(`[train] ${id}: done`);
      resolve();
    });
  });
}
