// Automatic map calibration: find "up" and the floor in a splat cloud so the map ends up in
// metres with Y up, the floor at y=0 and the origin in the middle of the floor.
import * as THREE from "three";
import type { SplatMesh } from "@sparkjsdev/spark";

const MAX_SAMPLES = 400_000;
const BIN = 0.05; // metres

/** Splat centres (raw file coords) of reasonably opaque splats, subsampled. */
export function sampleCenters(mesh: SplatMesh): Float32Array {
  const total = mesh.packedSplats?.numSplats ?? 0;
  const stride = Math.max(1, Math.floor(total / MAX_SAMPLES));
  const out: number[] = [];
  mesh.forEachSplat((i, c, _s, _q, opacity) => {
    if (i % stride === 0 && opacity > 0.3) out.push(c.x, c.y, c.z);
  });
  return new Float32Array(out);
}

function percentile(sorted: Float32Array, p: number) {
  return sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * p))];
}

interface Peaks {
  low: { h: number; count: number };
  high: { h: number; count: number };
}

/** Densest height bin near the bottom and near the top of the cloud along `up`. */
function findPeaks(pts: Float32Array, up: THREE.Vector3): Peaks {
  const n = pts.length / 3;
  const h = new Float32Array(n);
  for (let i = 0; i < n; i++) h[i] = pts[3 * i] * up.x + pts[3 * i + 1] * up.y + pts[3 * i + 2] * up.z;
  const sorted = h.slice().sort();
  const lo = percentile(sorted, 0.01);
  const hi = percentile(sorted, 0.99);
  const bins = new Uint32Array(Math.max(1, Math.ceil((hi - lo) / BIN)));
  for (const v of h) {
    const b = Math.floor((v - lo) / BIN);
    if (b >= 0 && b < bins.length) bins[b]++;
  }
  const band = Math.max(1, Math.floor(bins.length * 0.3));
  let low = 0;
  for (let b = 0; b < band; b++) if (bins[b] > bins[low]) low = b;
  let high = bins.length - 1;
  for (let b = bins.length - band; b < bins.length; b++) if (bins[b] > bins[high]) high = b;
  return {
    low: { h: lo + (low + 0.5) * BIN, count: bins[low] },
    high: { h: lo + (high + 0.5) * BIN, count: bins[high] },
  };
}

/**
 * Build the raw→map transform. `upCandidates` are raw-space directions that might be "up":
 * scans from our phone app are already gravity-aligned (+Y); arbitrary uploads try ±Y and ±Z.
 * A floor is a dense plane at the low end; scanners see more floor than ceiling, so the
 * direction with the stronger low-end peak wins.
 */
export function autoCalibrate(pts: Float32Array, upCandidates: THREE.Vector3[]): number[] {
  let best: { up: THREE.Vector3; floor: number; score: number } | null = null;
  for (const up of upCandidates) {
    const peaks = findPeaks(pts, up);
    const score = peaks.low.count * (upCandidates.length > 1 && peaks.low.count < peaks.high.count ? 0.5 : 1);
    if (!best || score > best.score) best = { up: up.clone().normalize(), floor: peaks.low.h, score };
  }
  const { up, floor } = best!;

  const rot = new THREE.Matrix4().makeRotationFromQuaternion(
    new THREE.Quaternion().setFromUnitVectors(up, new THREE.Vector3(0, 1, 0)),
  );

  // Centre the origin on the footprint of splats near the floor.
  const xs: number[] = [];
  const zs: number[] = [];
  const p = new THREE.Vector3();
  for (let i = 0; i < pts.length; i += 3) {
    p.set(pts[i], pts[i + 1], pts[i + 2]).applyMatrix4(rot);
    if (Math.abs(p.y - floor) < 0.2) {
      xs.push(p.x);
      zs.push(p.z);
    }
  }
  const mid = (arr: number[]) => {
    if (!arr.length) return 0;
    const s = Float32Array.from(arr).sort();
    return (percentile(s, 0.05) + percentile(s, 0.95)) / 2;
  };
  const translate = new THREE.Matrix4().makeTranslation(-mid(xs), -floor, -mid(zs));
  return translate.multiply(rot).toArray();
}

/** Raw-space direction that the current transform maps to +Y. */
export function currentUp(transform: number[]): THREE.Vector3 {
  const m = new THREE.Matrix4().fromArray(transform);
  const inv = new THREE.Matrix3().setFromMatrix4(m).invert();
  return new THREE.Vector3(0, 1, 0).applyMatrix3(inv).normalize();
}

export function rotateY90(transform: number[]): number[] {
  return new THREE.Matrix4().makeRotationY(Math.PI / 2).multiply(new THREE.Matrix4().fromArray(transform)).toArray();
}

export function nudgeFloor(transform: number[], dy: number): number[] {
  return new THREE.Matrix4().makeTranslation(0, dy, 0).multiply(new THREE.Matrix4().fromArray(transform)).toArray();
}
