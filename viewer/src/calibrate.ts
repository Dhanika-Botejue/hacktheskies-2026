// Automatic map calibration: find "up" and the floor in a splat cloud so the map ends up in
// metres with Y up, the floor at y=0 and the origin in the middle of the floor.
import * as THREE from "three";
import type { SplatMesh } from "@sparkjsdev/spark";

const MAX_SAMPLES = 400_000;
const BIN = 0.05; // metres

/** Splat centres of reasonably opaque splats, subsampled: raw file coords, or placed by `matrix`
 * (a merged map's parts). Several meshes share the sample budget. */
export function sampleCenters(parts: { mesh: SplatMesh; matrix?: THREE.Matrix4 }[]): Float32Array {
  const total = parts.reduce((n, p) => n + (p.mesh.packedSplats?.numSplats ?? 0), 0);
  const stride = Math.max(1, Math.floor(total / MAX_SAMPLES));
  const out: number[] = [];
  const v = new THREE.Vector3();
  for (const { mesh, matrix } of parts) {
    mesh.forEachSplat((i, c, _s, _q, opacity) => {
      if (i % stride !== 0 || opacity <= 0.3) return;
      v.copy(c);
      if (matrix) v.applyMatrix4(matrix);
      out.push(v.x, v.y, v.z);
    });
  }
  return new Float32Array(out);
}

function percentile(sorted: Float32Array, p: number) {
  return sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * p))];
}

const KEEP_RADIUS = 1.5; // × the distance from the middle that holds 90% of the splats

/**
 * Drop splats far outside the scan itself. Scaniverse exports wrap the room in a background
 * shell tens to hundreds of metres out (a few % of the splats); left in, it stretches the height
 * range so the floor fit lands on the shell and the map comes out upside down and metres up.
 */
function withoutFarSplats(pts: Float32Array): Float32Array {
  const n = pts.length / 3;
  if (n < 100) return pts;
  const median = (axis: number) => {
    const v = new Float32Array(n);
    for (let i = 0; i < n; i++) v[i] = pts[3 * i + axis];
    return percentile(v.sort(), 0.5);
  };
  const [cx, cy, cz] = [median(0), median(1), median(2)];
  const r = new Float32Array(n);
  for (let i = 0; i < n; i++) r[i] = Math.hypot(pts[3 * i] - cx, pts[3 * i + 1] - cy, pts[3 * i + 2] - cz);
  const cut = KEEP_RADIUS * percentile(r.slice().sort(), 0.9);
  const out: number[] = [];
  for (let i = 0; i < n; i++) if (r[i] <= cut) out.push(pts[3 * i], pts[3 * i + 1], pts[3 * i + 2]);
  return Float32Array.from(out);
}

const FLOOR_DENSITY = 0.35; // a floor layer has at least this share of the densest layer's splats

/**
 * The floor along `up`: the LOWEST dense layer (desks and shelves can be denser than the floor,
 * but they're above it), and how much of the scan is above it. Sparse noise beyond the floor
 * (reflections "under" a shiny floor) doesn't matter, unlike a fit that only looks at the ends.
 */
function findFloor(pts: Float32Array, up: THREE.Vector3): { h: number; above: number } {
  const n = pts.length / 3;
  const h = new Float32Array(n);
  for (let i = 0; i < n; i++) h[i] = pts[3 * i] * up.x + pts[3 * i + 1] * up.y + pts[3 * i + 2] * up.z;
  const sorted = h.slice().sort();
  const lo = percentile(sorted, 0.005);
  const hi = percentile(sorted, 0.995);
  const bins = new Uint32Array(Math.max(1, Math.ceil((hi - lo) / BIN)));
  for (const v of h) {
    const b = Math.floor((v - lo) / BIN);
    if (b >= 0 && b < bins.length) bins[b]++;
  }
  const peak = bins.reduce((m, c) => Math.max(m, c), 0);
  const dense = FLOOR_DENSITY * peak;
  let b = 0;
  while (b < bins.length - 1 && bins[b] < dense) b++;
  // A floor smeared over some height (tracking drift across a big scan): its densest part.
  let floorBin = b;
  for (; b < bins.length && bins[b] >= dense; b++) if (bins[b] > bins[floorBin]) floorBin = b;
  const floor = lo + (floorBin + 0.5) * BIN;
  let above = 0;
  for (const v of h) if (v >= floor - 0.1) above++;
  return { h: floor, above: above / n };
}

/**
 * Build the raw→map transform. `upCandidates` are raw-space directions that might be "up":
 * scans from our phone app are already gravity-aligned (+Y); uploads try ±Y. "Up" is the
 * direction that puts the most of the scan above its floor (a room's contents stand on the
 * floor; the wrong way round, the floor looks like a ceiling with everything hanging below it).
 */
export function autoCalibrate(allPts: Float32Array, upCandidates: THREE.Vector3[]): number[] {
  const pts = withoutFarSplats(allPts);
  let best: { up: THREE.Vector3; floor: number; score: number } | null = null;
  for (const up of upCandidates) {
    const f = findFloor(pts, up);
    if (!best || f.above > best.score) best = { up: up.clone().normalize(), floor: f.h, score: f.above };
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

const AXES = [new THREE.Vector3(0, 1, 0), new THREE.Vector3(0, 0, 1), new THREE.Vector3(1, 0, 0)];

/** The next raw axis (Y → Z → X → Y) to try as "up", both ways round: for a map lying on its side,
 * which Flip (upside down) and Rotate (about the vertical) can't fix. */
export function nextUpAxis(transform: number[]): THREE.Vector3[] {
  const up = currentUp(transform);
  const i = [Math.abs(up.y), Math.abs(up.z), Math.abs(up.x)].reduce((best, v, k, a) => (v > a[best] ? k : best), 0);
  const next = AXES[(i + 1) % AXES.length];
  return [next.clone(), next.clone().negate()];
}

export function rotateY90(transform: number[]): number[] {
  return new THREE.Matrix4().makeRotationY(Math.PI / 2).multiply(new THREE.Matrix4().fromArray(transform)).toArray();
}

export function nudgeFloor(transform: number[], dy: number): number[] {
  return new THREE.Matrix4().makeTranslation(0, dy, 0).multiply(new THREE.Matrix4().fromArray(transform)).toArray();
}
