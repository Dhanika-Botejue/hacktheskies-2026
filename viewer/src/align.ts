// Lining up overlapping scans for a merged map, from above: each scan's walls (splats at head
// height above its floor, one per 10 cm cell) as a 2D point set. The second set is turned and moved until
// its points land on the first set's: a chamfer match (distance to the nearest wall point,
// capped) over every 5° and a 0.5 m grid, then refined down to 0.5° and 5 cm. Needs some overlap
// between the scans; the align panel fixes the rest by hand.
import * as THREE from "three";
import type { SplatMesh } from "@sparkjsdev/spark";

// Head height: walls, doors and tall shelves reach it; desks and chairs (clutter that differs
// between scans) mostly don't.
const WALL_MIN = 1.3; // m above the floor
const WALL_MAX = 2.2;
const LOW_MIN = 0.4; // fallback band starts here
const MIN_CELLS = 300; // fewer head-height cells than this: use the fallback band
const CELL = 0.1; // m, distance grid resolution
const CAP = 0.6; // m, farther than this counts as "no match"
const MAX_POINTS = 6000;

/** Wall/furniture splats of a floor-calibrated scan, top-down (x, z pairs), subsampled. */
export function wallPoints(mesh: SplatMesh, transform: THREE.Matrix4): Float32Array {
  const total = mesh.packedSplats?.numSplats ?? 0;
  const stride = Math.max(1, Math.floor(total / 200_000));
  const raw: number[] = [];
  mesh.forEachSplat((i, c, _s, _q, opacity) => {
    if (i % stride === 0 && opacity >= 0.3) raw.push(c.x, c.y, c.z);
  });
  return wallPointsOf(Float32Array.from(raw), transform);
}

/** Same, from raw splat centres (x, y, z triples). Scans that barely reach head height (held low)
 * fall back to everything from knee to above head height. */
export function wallPointsOf(raw: Float32Array, transform: THREE.Matrix4): Float32Array {
  const placed = new Float32Array(raw.length);
  const v = new THREE.Vector3();
  for (let i = 0; i < raw.length; i += 3) {
    v.set(raw[i], raw[i + 1], raw[i + 2]).applyMatrix4(transform);
    placed.set([v.x, v.y, v.z], i);
  }
  const band = (min: number, max: number) => {
    const pts: number[] = [];
    for (let i = 0; i < placed.length; i += 3) {
      if (placed[i + 1] > min && placed[i + 1] < max) pts.push(placed[i], placed[i + 2]);
    }
    return onePerCell(withoutFar(Float32Array.from(pts)));
  };
  let cells = band(WALL_MIN, WALL_MAX);
  if (cells.length / 2 < MIN_CELLS) cells = band(LOW_MIN, WALL_MAX);
  return subsample(cells, MAX_POINTS);
}

/** One point per 10 cm cell: a long wall then counts for its length, not for how many splats
 * the scanner piled onto a cluttered desk, so walls drive the match. */
function onePerCell(pts: Float32Array) {
  const seen = new Set<string>();
  const out: number[] = [];
  for (let i = 0; i < pts.length; i += 2) {
    const key = `${Math.floor(pts[i] / CELL)},${Math.floor(pts[i + 1] / CELL)}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push((Math.floor(pts[i] / CELL) + 0.5) * CELL, (Math.floor(pts[i + 1] / CELL) + 0.5) * CELL);
  }
  return Float32Array.from(out);
}

/** Drop points far outside the scan (Scaniverse's background shell would make the search area
 * hundreds of metres wide): beyond 1.5× the radius holding 90% of them. */
function withoutFar(pts: Float32Array) {
  const n = pts.length / 2;
  if (n < 50) return pts;
  const xs = new Float32Array(n), zs = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    xs[i] = pts[2 * i];
    zs[i] = pts[2 * i + 1];
  }
  const cx = xs.slice().sort()[n >> 1], cz = zs.slice().sort()[n >> 1];
  const r = new Float32Array(n);
  for (let i = 0; i < n; i++) r[i] = Math.hypot(xs[i] - cx, zs[i] - cz);
  const cut = 1.5 * r.slice().sort()[Math.floor(n * 0.9)];
  const out: number[] = [];
  for (let i = 0; i < n; i++) if (r[i] <= cut) out.push(xs[i], zs[i]);
  return Float32Array.from(out);
}

function subsample(pts: Float32Array, max: number) {
  const n = pts.length / 2;
  if (n <= max) return pts;
  const out = new Float32Array(max * 2);
  for (let i = 0; i < max; i++) {
    const j = Math.floor((i * n) / max);
    out[2 * i] = pts[2 * j];
    out[2 * i + 1] = pts[2 * j + 1];
  }
  return out;
}

interface Grid {
  x0: number;
  z0: number;
  w: number;
  h: number;
  d: Float32Array; // metres to the nearest point, capped at CAP
}

/** Distance (capped) from every cell to the nearest point: two-pass chamfer transform. */
function distanceGrid(pts: Float32Array, margin: number): Grid {
  let minX = Infinity, maxX = -Infinity, minZ = Infinity, maxZ = -Infinity;
  for (let i = 0; i < pts.length; i += 2) {
    minX = Math.min(minX, pts[i]);
    maxX = Math.max(maxX, pts[i]);
    minZ = Math.min(minZ, pts[i + 1]);
    maxZ = Math.max(maxZ, pts[i + 1]);
  }
  const x0 = minX - margin, z0 = minZ - margin;
  const w = Math.ceil((maxX - minX + 2 * margin) / CELL) + 1;
  const h = Math.ceil((maxZ - minZ + 2 * margin) / CELL) + 1;
  const d = new Float32Array(w * h).fill(Infinity);
  for (let i = 0; i < pts.length; i += 2) {
    const cx = Math.floor((pts[i] - x0) / CELL), cz = Math.floor((pts[i + 1] - z0) / CELL);
    d[cz * w + cx] = 0;
  }
  const a = CELL, b = CELL * Math.SQRT2;
  for (let z = 0; z < h; z++) {
    for (let x = 0; x < w; x++) {
      const k = z * w + x;
      let v = d[k];
      if (x > 0) v = Math.min(v, d[k - 1] + a);
      if (z > 0) {
        v = Math.min(v, d[k - w] + a);
        if (x > 0) v = Math.min(v, d[k - w - 1] + b);
        if (x < w - 1) v = Math.min(v, d[k - w + 1] + b);
      }
      d[k] = v;
    }
  }
  for (let z = h - 1; z >= 0; z--) {
    for (let x = w - 1; x >= 0; x--) {
      const k = z * w + x;
      let v = d[k];
      if (x < w - 1) v = Math.min(v, d[k + 1] + a);
      if (z < h - 1) {
        v = Math.min(v, d[k + w] + a);
        if (x < w - 1) v = Math.min(v, d[k + w + 1] + b);
        if (x > 0) v = Math.min(v, d[k + w - 1] + b);
      }
      d[k] = Math.min(v, CAP);
    }
  }
  return { x0, z0, w, h, d };
}

/** Mean capped distance of points (relative to their centroid, turned by yaw) placed at (tx, tz). */
function cost(grid: Grid, rel: Float32Array, yaw: number, tx: number, tz: number, step: number) {
  const c = Math.cos(yaw), s = Math.sin(yaw);
  let sum = 0, n = 0;
  for (let i = 0; i < rel.length; i += 2 * step) {
    // Same rotation as THREE.Matrix4.makeRotationY.
    const x = c * rel[i] + s * rel[i + 1] + tx;
    const z = -s * rel[i] + c * rel[i + 1] + tz;
    const gx = Math.floor((x - grid.x0) / CELL), gz = Math.floor((z - grid.z0) / CELL);
    sum += gx >= 0 && gz >= 0 && gx < grid.w && gz < grid.h ? grid.d[gz * grid.w + gx] : CAP;
    n++;
  }
  return sum / n;
}

/**
 * Best placement of `moving` onto `fixed` (both top-down x, z pairs, metres): the matrix that
 * turns about the vertical and moves `moving`'s floor frame into `fixed`'s.
 */
export function align2D(fixed: Float32Array, moving: Float32Array): { matrix: THREE.Matrix4; score: number } {
  let cx = 0, cz = 0;
  const n = moving.length / 2;
  for (let i = 0; i < moving.length; i += 2) {
    cx += moving[i];
    cz += moving[i + 1];
  }
  cx /= n;
  cz /= n;
  const rel = new Float32Array(moving.length);
  for (let i = 0; i < moving.length; i += 2) {
    rel[i] = moving[i] - cx;
    rel[i + 1] = moving[i + 1] - cz;
  }
  const grid = distanceGrid(fixed, 1);

  // Coarse: every 5°, centroid anywhere over the fixed scan on a 0.5 m grid, every 6th point.
  let best = { yaw: 0, tx: 0, tz: 0, score: Infinity };
  const xMax = grid.x0 + grid.w * CELL, zMax = grid.z0 + grid.h * CELL;
  for (let deg = 0; deg < 360; deg += 5) {
    const yaw = (deg * Math.PI) / 180;
    for (let tx = grid.x0; tx <= xMax; tx += 0.5) {
      for (let tz = grid.z0; tz <= zMax; tz += 0.5) {
        const score = cost(grid, rel, yaw, tx, tz, 6);
        if (score < best.score) best = { yaw, tx, tz, score };
      }
    }
  }
  // Refine around the best: finer turns and moves, more points.
  for (const [dYaw, yawStep, dT, tStep, every] of [
    [6, 1, 0.6, 0.1, 2],
    [1.5, 0.5, 0.15, 0.05, 1],
  ]) {
    const around = best;
    for (let dd = -dYaw; dd <= dYaw + 1e-9; dd += yawStep) {
      const yaw = around.yaw + (dd * Math.PI) / 180;
      for (let ox = -dT; ox <= dT + 1e-9; ox += tStep) {
        for (let oz = -dT; oz <= dT + 1e-9; oz += tStep) {
          const score = cost(grid, rel, yaw, around.tx + ox, around.tz + oz, every);
          if (score < best.score) best = { yaw, tx: around.tx + ox, tz: around.tz + oz, score };
        }
      }
    }
  }
  const matrix = new THREE.Matrix4()
    .makeTranslation(best.tx, 0, best.tz)
    .multiply(new THREE.Matrix4().makeRotationY(best.yaw))
    .multiply(new THREE.Matrix4().makeTranslation(-cx, 0, -cz));
  return { matrix, score: best.score };
}

/** Points moved by a placement matrix (top-down x, z pairs). */
export function placePoints(pts: Float32Array, m: THREE.Matrix4): Float32Array {
  const out = new Float32Array(pts.length);
  const v = new THREE.Vector3();
  for (let i = 0; i < pts.length; i += 2) {
    v.set(pts[i], 0, pts[i + 1]).applyMatrix4(m);
    out[i] = v.x;
    out[i + 1] = v.z;
  }
  return out;
}
