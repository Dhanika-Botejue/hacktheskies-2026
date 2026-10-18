// Spectator viewer: shows the active map as a Gaussian splat, walked through first-person like a
// shooter map, with the players' live positions from the game server, their hit points and shots
// from the shooting server, and the match: the bases and the flag teams fight over.
import * as THREE from "three";
import { SparkRenderer, SplatMesh } from "@sparkjsdev/spark";
import type { MapInfo } from "../server/maps.ts";
import { align2D, wallPoints } from "./align.ts";
import { autoCalibrate, currentUp, nextUpAxis, nudgeFloor, rotateY90, sampleCenters } from "./calibrate.ts";
import { CombatLink, TEAM_COLORS, TEAM_NAMES, type Team, clock } from "./combat.ts";
import { SpectatorControls } from "./controls.ts";
import { GameLink, type MapObjective, isLinked, mapOrigin, publishGameMap, yawToHeading } from "./game.ts";
import {
  Feed, Minimap, drawCompass, renderMatchPanel, renderObjectivesPanel, renderPlayersPanel, renderScoreboard,
} from "./hud.ts";
import { PlayerLayer, playerColor } from "./players.ts";
import { ObjectiveLayer, SLOTS } from "./points.ts";
import { TracerLayer } from "./tracers.ts";
import {
  type MapsState, type ReadyItem, escapeHtml, renderMapList, renderReadyList, setHud, setStatus, setupConnectPanel,
  setupFoldingPanels, setupMerge, setupUploads, subscribeMaps,
} from "./ui.ts";

const canvas = document.querySelector<HTMLCanvasElement>("#view")!;
const renderer = new THREE.WebGLRenderer({ canvas, antialias: false });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0b0d12);
scene.add(new SparkRenderer({ renderer }));
// Splats ignore lights; these only shade the player avatars.
scene.add(new THREE.HemisphereLight(0xffffff, 0x404050, 2.2));

const controls = new SpectatorControls(canvas);

const labels = document.querySelector<HTMLElement>("#labels")!;
const game = new GameLink();
const combat = new CombatLink();
const players = new PlayerLayer(game, combat, labels);
scene.add(players.group);
const objectives = new ObjectiveLayer(game, labels);
scene.add(objectives.group);
const tracers = new TracerLayer();
scene.add(tracers.group);
const minimap = new Minimap(game);
const feed = new Feed();

// Arrow showing where the spectator is standing, visible in the top-down overview.
const you = new THREE.Mesh(
  new THREE.ConeGeometry(0.35, 0.9, 3),
  new THREE.MeshBasicMaterial({ color: 0xffd34d }),
);
you.geometry.rotateX(-Math.PI / 2);
scene.add(you);

function resize() {
  const w = window.innerWidth;
  const h = window.innerHeight;
  renderer.setSize(w, h, false);
  controls.resize(w, h);
}
window.addEventListener("resize", resize);
resize();

// ---- Map loading ----------------------------------------------------------------------------

const SCAN_UP = [new THREE.Vector3(0, 1, 0)]; // phone scans are gravity-aligned already
// Splat files from other apps (Scaniverse, Polycam, 3DGS) keep Y vertical, one way round or the
// other. A map that still comes out on its side is fixed with Tip over.
const UPLOAD_UP = [new THREE.Vector3(0, 1, 0), new THREE.Vector3(0, -1, 0)];
// Merged maps are built from parts that already have their floors at 0, Y up.
const upFor = (m: MapInfo) => (m.source === "upload" ? UPLOAD_UP : SCAN_UP);
const IDENTITY = new THREE.Matrix4().toArray();

/** A splat file on screen; `matrix` places it in its map's raw frame (a merged map's part). */
interface Part {
  id: string;
  mesh: SplatMesh;
  matrix: THREE.Matrix4;
}

interface Loaded {
  map: MapInfo;
  root: THREE.Group; // carries map.transform: raw (or merged) frame → map
  parts: Part[];
  centers?: Float32Array;
}

let state: MapsState = { maps: [], activeId: null };
let current: Loaded | null = null;
let loadingKey: string | null = null;
const calibrating = new Set<string>();

/** The files to load for a map: its splat, or a merged map's parts. */
function filesOf(m: MapInfo) {
  return m.parts
    ? m.parts.map((p) => ({ id: p.id, url: `/maps/${p.id}/${p.splatFile}`, transform: p.transform }))
    : [{ id: m.id, url: `/maps/${m.id}/${m.splatFile}`, transform: IDENTITY }];
}

// A merged map only reloads when its parts change, not when one is moved.
const keyOf = (m: MapInfo) =>
  m.parts ? `${m.id}/merged/${m.parts.map((p) => `${p.id}:${p.splatFile}`).join(",")}` : `${m.id}/${m.splatFile}`;

function setMatrix(obj: THREE.Object3D, transform: ArrayLike<number> | null) {
  const m = new THREE.Matrix4();
  if (transform) m.fromArray(transform);
  m.decompose(obj.position, obj.quaternion, obj.scale);
}

function unload(l: Loaded) {
  scene.remove(l.root);
  for (const p of l.parts) p.mesh.dispose();
}

const centersOf = (l: Loaded) => (l.centers ??= sampleCenters(l.parts));

async function saveTransform(id: string, transform: number[] | null) {
  await fetch(`/api/maps/${id}`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ transform }),
  });
}

async function loadMap(map: MapInfo) {
  const key = keyOf(map);
  loadingKey = key;
  const files = filesOf(map);
  const meshes = files.map((f) => new SplatMesh({ url: f.url }));
  const results = await Promise.allSettled(meshes.map((m) => m.initialized));
  const parts: Part[] = [];
  results.forEach((r, i) => {
    if (r.status === "fulfilled") {
      parts.push({ id: files[i].id, mesh: meshes[i], matrix: new THREE.Matrix4().fromArray(files[i].transform) });
    } else {
      console.error("failed to load splat", files[i].url, r.reason);
      meshes[i].dispose();
    }
  });
  if (loadingKey !== key || !parts.length) {
    for (const p of parts) p.mesh.dispose(); // a newer preview or another map was requested meanwhile
    if (loadingKey === key) loadingKey = null;
    return;
  }
  loadingKey = null;
  if (parts.length < files.length) feed.push(`${files.length - parts.length} part(s) of "${map.name}" didn't load`, "#ff5c6c");

  const root = new THREE.Group();
  for (const p of parts) {
    setMatrix(p.mesh, p.matrix.toArray());
    root.add(p.mesh);
  }
  const loaded: Loaded = { map, root, parts };
  const latest = state.maps.find((m) => m.id === map.id) ?? map;
  let transform = latest.transform;
  if (!transform) {
    transform = autoCalibrate(centersOf(loaded), upFor(latest));
    if (!calibrating.has(map.id)) {
      calibrating.add(map.id);
      saveTransform(map.id, transform).finally(() => calibrating.delete(map.id));
    }
  }
  setMatrix(root, transform);
  scene.add(root);

  const switchedMap = current?.map.id !== map.id;
  if (current) unload(current);
  loaded.map = { ...latest, transform };
  current = loaded;
  if (switchedMap) {
    controls.resetPose();
    alignId = null;
  }
  updateAdjustPanel();
  syncGameMap();
}

// ---- Game map -------------------------------------------------------------------------------

// The map on screen is the game map: the game server gets its footprint and floor plan, so
// player positions from the phones line up with the scan.
let publishing = false;

function syncGameMap() {
  if (publishing || partsDirty || !game.connected || !game.map || !current?.map.transform) return;
  const { map, parts } = current;
  const transform = map.transform!;
  if (map.id !== state.activeId || isLinked(game.map, map, transform)) return;
  const fresh = game.map.splat?.id !== map.id;
  publishing = true;
  publishGameMap(game.map, map, parts, transform)
    .then((next) => {
      if (!next) return;
      // The server's own copy may be here already, with a base or flag the host moved meanwhile.
      if (!isLinked(game.map, map, transform)) game.map = next;
      if (fresh) feed.push(`Game map is now "${map.name}"`, "#ffd34d");
    })
    .catch((err) => console.error("couldn't update the game map", err))
    .finally(() => {
      publishing = false;
      // The map may have changed again (new training preview, adjustment) while we worked.
      if (current && current.map.transform && !isLinked(game.map, current.map, current.map.transform)) {
        setTimeout(syncGameMap, 1000);
      }
    });
}

function updateAdjustPanel() {
  const active = state.maps.find((m) => m.id === state.activeId);
  const shown = !!(active?.splatFile && current?.map.id === active.id);
  document.querySelector<HTMLElement>("#adjust")!.hidden = !shown;
  renderAlignPanel(shown && !!current?.map.parts);
}

function onState(next: MapsState) {
  state = next;
  renderMapList(state);
  const active = state.maps.find((m) => m.id === state.activeId) ?? null;
  const name = escapeHtml(active?.name ?? "");
  updateAdjustPanel();

  if (!active) {
    setStatus("No map yet.<br><b>Open map file</b> (top right) to load a Scaniverse scan, or scan one with the HackTheSkies app.");
  } else if (active.status === "failed") {
    setStatus(`Training <b>${name}</b> failed.<br><small>${escapeHtml(active.error ?? "")}</small>`);
  } else if (!active.splatFile) {
    const pct = Math.round(active.progress * 100);
    setStatus(
      active.status === "uploaded"
        ? `Scan <b>${name}</b> received. Waiting to train…`
        : `Training <b>${name}</b>… ${pct}%<br><small>A first preview appears in about a minute.</small>`,
    );
  } else if (active.status === "training") {
    setStatus(`Training ${Math.round(active.progress * 100)}% · the map sharpens as it trains`, true);
  } else if (!merging) {
    setStatus(null);
  }

  if (!active?.splatFile) {
    loadingKey = null; // a load of another map still on its way is not wanted any more
    if (current && current.map.id !== active?.id) {
      unload(current);
      current = null;
    }
    return;
  }
  const key = keyOf(active);
  if (current && keyOf(current.map) === key) {
    loadingKey = null; // e.g. the host clicked another map and came back before it loaded
    if (active.transform && JSON.stringify(active.transform) !== JSON.stringify(current.map.transform)) {
      setMatrix(current.root, active.transform);
      current.centers = undefined;
    }
    // Parts moved elsewhere (another viewer): follow, unless we're the ones moving them.
    if (active.parts && !partsDirty) {
      active.parts.forEach((p, i) => {
        const part = current!.parts.find((q) => q.id === p.id);
        if (part && JSON.stringify(part.matrix.toArray()) !== JSON.stringify(p.transform)) {
          part.matrix.fromArray(p.transform);
          setMatrix(part.mesh, p.transform);
          current!.centers = undefined;
        }
        void i;
      });
    }
    current.map = { ...active, transform: active.transform ?? current.map.transform, parts: partsDirty ? current.map.parts : active.parts };
  } else if (loadingKey !== key) {
    void loadMap(active);
  }
}

// ---- Adjust panel ---------------------------------------------------------------------------

document.querySelectorAll<HTMLButtonElement>("[data-adjust]").forEach((btn) => {
  btn.onclick = () => {
    if (!current?.map.transform) return;
    const t = current.map.transform;
    const centers = centersOf(current);
    const next = {
      flip: () => autoCalibrate(centers, [currentUp(t).negate()]),
      tip: () => autoCalibrate(centers, nextUpAxis(t)),
      rotate: () => rotateY90(t),
      "floor-up": () => nudgeFloor(t, -0.05),
      "floor-down": () => nudgeFloor(t, 0.05),
      reset: () => autoCalibrate(centers, upFor(current!.map)),
    }[btn.dataset.adjust!]!();
    current.map.transform = next;
    setMatrix(current.root, next);
    void saveTransform(current.map.id, next);
    syncGameMap();
  };
});

// ---- Merging scans ----------------------------------------------------------------------------

let merging = false;

/** One map made of the ticked ones, each scan where it was scanned: its raw positions, only stood
 * upright with its floor at 0 (a merged input keeps its parts' arrangement). Nothing is moved
 * towards anything else, so unscanned space between scans stays empty. Scans share positions when
 * they were started from the same spot facing the same way; Align parts fixes the rest by hand.
 * Merged maps can be merged again. */
async function mergeMaps(maps: MapInfo[]) {
  const name = prompt("Name the merged map", maps.map((m) => m.name).join(" + ").slice(0, 60));
  if (name === null) return;
  merging = true;
  // Every input becomes a group of pieces (a merged input brings its parts, already arranged).
  const groups = maps.map((m) =>
    filesOf(m).map((f) => ({ id: f.id, splatFile: f.url.split("/").slice(3).join("/"), url: f.url, raw: new THREE.Matrix4().fromArray(f.transform), map: m })),
  );
  const pieces = groups.flat();
  try {
    setStatus(`Merging ${maps.length} maps: loading ${pieces.length} scans…`);
    const meshes = pieces.map((p) => new SplatMesh({ url: p.url }));
    await Promise.all(meshes.map((m) => m.initialized));
    // Each piece stays where it was scanned: its own map's calibration stands it upright with its
    // floor at 0, but without the calibration's horizontal re-centring, so positions are the
    // scan's own raw ones and the gaps between scans are kept.
    const placed = pieces.map((p, i) => {
      const t = new THREE.Matrix4().fromArray(
        p.map.transform ?? autoCalibrate(sampleCenters([{ mesh: meshes[i], matrix: p.raw }]), upFor(p.map)),
      );
      t.elements[12] = 0;
      t.elements[14] = 0;
      return t.multiply(p.raw);
    });
    meshes.forEach((m) => m.dispose());
    const res = await fetch("/api/maps/merge", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name, parts: pieces.map((p, i) => ({ id: p.id, splatFile: p.splatFile, transform: placed[i].toArray() })) }),
    });
    if (!res.ok) throw new Error((await res.json()).error ?? `merge failed (${res.status})`);
    feed.push(`Merged "${name}": each scan kept where it was scanned`, "#ffd34d");
  } catch (err) {
    console.error(err);
    alert(`Couldn't merge: ${(err as Error).message ?? err}`);
  } finally {
    merging = false;
    setStatus(null);
  }
}

function concat(arrays: Float32Array[]) {
  const out = new Float32Array(arrays.reduce((n, a) => n + a.length, 0));
  let o = 0;
  for (const a of arrays) {
    out.set(a, o);
    o += a.length;
  }
  return out;
}

setupMerge((maps) => void mergeMaps(maps));

// ---- Align parts (merged maps) ----------------------------------------------------------------

let alignId: string | null = null;
let partsDirty = false;
let partsSaveTimer: ReturnType<typeof setTimeout> | undefined;

function renderAlignPanel(shown: boolean) {
  const panel = document.querySelector<HTMLElement>("#align")!;
  panel.hidden = !shown;
  if (!shown || !current) {
    alignId = null;
    return;
  }
  const list = document.querySelector<HTMLUListElement>("#part-list")!;
  list.replaceChildren(
    ...current.parts.map((p) => {
      const li = document.createElement("li");
      li.className = p.id === alignId ? "active" : "";
      li.textContent = state.maps.find((m) => m.id === p.id)?.name ?? p.id;
      li.onclick = () => {
        alignId = alignId === p.id ? null : p.id;
        if (alignId) controls.setMode("overview");
        renderAlignPanel(true);
      };
      return li;
    }),
  );
  panel.querySelectorAll<HTMLButtonElement>("button").forEach((b) => (b.disabled = !alignId));
}

const alignPart = () => current?.parts.find((p) => p.id === alignId) ?? null;

/** Move a part by `delta`, given in map coordinates (what you see), and save it shortly. */
function movePart(part: Part, delta: THREE.Matrix4) {
  const root = new THREE.Matrix4().fromArray(current!.map.transform!);
  part.matrix.premultiply(root.clone().invert().multiply(delta).multiply(root)); // root⁻¹ · delta · root
  setMatrix(part.mesh, part.matrix.toArray());
  current!.centers = undefined;
  partsDirty = true;
  clearTimeout(partsSaveTimer);
  partsSaveTimer = setTimeout(savePartPlacement, 600);
}

async function savePartPlacement() {
  if (!current?.map.parts) return;
  const map = current.map;
  const parts = map.parts!.map((p) => ({ ...p, transform: current!.parts.find((q) => q.id === p.id)?.matrix.toArray() ?? p.transform }));
  map.parts = parts;
  await fetch(`/api/maps/${map.id}`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ parts }),
  });
  partsDirty = false;
  syncGameMap(); // redraw the floor plan with the part where it is now
}

/** Where a part is on the map (map coordinates): the middle of its splats. */
function partCentre(part: Part) {
  const m = new THREE.Matrix4().fromArray(current!.map.transform!).multiply(part.matrix);
  const pts = sampleCenters([{ mesh: part.mesh, matrix: m }]);
  let x = 0, z = 0;
  for (let i = 0; i < pts.length; i += 3) {
    x += pts[i];
    z += pts[i + 2];
  }
  const n = Math.max(1, pts.length / 3);
  return { x: x / n, z: z / n };
}

controls.onOverviewDrag = (dx, dz) => {
  const part = alignPart();
  if (!part) return false;
  movePart(part, new THREE.Matrix4().makeTranslation(dx, 0, dz));
  return true;
};

document.querySelectorAll<HTMLButtonElement>("[data-turn]").forEach((btn) => {
  btn.onclick = () => {
    const part = alignPart();
    if (!part) return;
    const c = partCentre(part);
    const turn = (-Number(btn.dataset.turn) * Math.PI) / 180; // positive = clockwise from above
    movePart(
      part,
      new THREE.Matrix4().makeTranslation(c.x, 0, c.z).multiply(new THREE.Matrix4().makeRotationY(turn)).multiply(new THREE.Matrix4().makeTranslation(-c.x, 0, -c.z)),
    );
  };
});

document.querySelector<HTMLButtonElement>("#part-auto")!.onclick = () => {
  const part = alignPart();
  if (!part || !current) return;
  const root = new THREE.Matrix4().fromArray(current.map.transform!);
  const others = current.parts.filter((p) => p !== part);
  const fixed = concat(others.map((p) => wallPoints(p.mesh, root.clone().multiply(p.matrix))));
  const moving = wallPoints(part.mesh, root.clone().multiply(part.matrix));
  if (!fixed.length || !moving.length) return feed.push("Not enough walls in these scans to line them up automatically", "#ff5c6c");
  movePart(part, align2D(fixed, moving).matrix);
  feed.push("Auto-aligned: drag or turn it if it's not right", "#ffd34d");
};

// ---- Players: spectating and placing ---------------------------------------------------------

let followId: string | null = null;
let selectedId: string | null = null;
// Overview placement: first click = where they stand, second click = the way they face.
let placing: { id: string; at: THREE.Vector3 | null } | null = null;

const placeMarker = new THREE.Group();
{
  const mat = new THREE.MeshBasicMaterial({ color: 0xffd34d, depthTest: false, transparent: true });
  const dot = new THREE.Mesh(new THREE.CircleGeometry(0.25, 32).rotateX(-Math.PI / 2), mat);
  const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3()]),
    new THREE.LineBasicMaterial({ color: 0xffd34d, depthTest: false }));
  line.name = "aim";
  placeMarker.add(dot, line);
  placeMarker.renderOrder = 1000;
  placeMarker.children.forEach((c) => (c.renderOrder = 1000));
  placeMarker.visible = false;
  scene.add(placeMarker);
}

const nameOf = (id: string) => game.players.find((p) => p.id === id)?.name ?? "player";

function spectate(id: string) {
  if (!players.pose(id)) return;
  followId = id;
  placing = null;
  updatePlacingHint();
  controls.follow(() => players.pose(id));
  feed.push(`Watching ${nameOf(id)} · V: over the shoulder · WASD: take over`);
}

function startPlacing(id: string) {
  selectedId = players.selectedId = id;
  placing = { id, at: null };
  placingObjective = null; // else the next click would drop the base or flag instead
  controls.setMode("overview");
  updatePlacingHint();
}

function finishPlacing(id: string, x: number, z: number, heading: number) {
  game.place(id, x, z, heading);
  feed.push(`Placed ${nameOf(id)}`, "#ffd34d");
  placing = null;
  updatePlacingHint();
}

function updatePlacingHint() {
  placeMarker.visible = !!placing?.at;
  if (placing?.at) placeMarker.position.set(placing.at.x, 0.03, placing.at.z);
  const hint = document.querySelector<HTMLElement>("#place-hint")!;
  hint.hidden = !placing && !placingObjective;
  if (placing) {
    hint.textContent = placing.at
      ? `Now click the direction ${nameOf(placing.id)} is facing. Esc cancels.`
      : `Click where ${nameOf(placing.id)} is standing. Esc cancels. (Or walk there in first person and press P.)`;
  } else if (placingObjective) {
    hint.textContent = `Click the map where the ${placingObjective.name.toLowerCase()} goes. Esc cancels.`;
  }
}

// ---- Objectives: the host puts the bases and the flag on the top-down map --------------------

const POINT_RADIUS = { min: 0.5, max: 10 };
let placingObjective: (typeof SLOTS)[number] | null = null;

function setPoints(points: MapObjective[]) {
  if (!game.map) return;
  game.map = { ...game.map, points }; // show it now; the server's map message confirms it
  game.setPoints(points);
  objectives.update(combat.objectives);
}

function startPlacingObjective(id: string) {
  if (!game.connected) return feed.push("Game server offline: start run.py first", "#ff5c6c");
  placing = null;
  placingObjective = SLOTS.find((s) => s.id === id) ?? null;
  controls.setMode("overview");
  updatePlacingHint();
}

function stopPlacingObjective() {
  placingObjective = null;
  updatePlacingHint();
}

/** Put the objective being placed at a world floor point (moving it if it's already on the map). */
function dropObjective(at: { x: number; z: number }) {
  const slot = placingObjective!;
  const o = mapOrigin(game.map);
  const old = objectives.points.find((p) => p.id === slot.id);
  const point: MapObjective = {
    id: slot.id, kind: slot.kind, ...(slot.team ? { team: slot.team } : {}), name: slot.name,
    x: at.x - o.x, y: at.z - o.z, r: old?.r ?? slot.r,
  };
  setPoints([...objectives.points.filter((p) => p.id !== slot.id), point]);
  feed.push(`${slot.name} ${old ? "moved" : "placed"}`, slot.team ? TEAM_COLORS[slot.team] : "#ffd34d");
  stopPlacingObjective();
}

const objectiveActions = {
  place: (id: string) => (placingObjective?.id === id ? stopPlacingObjective() : startPlacingObjective(id)),
  resize: (id: string, delta: number) => {
    const clamp = (r: number) => Math.min(POINT_RADIUS.max, Math.max(POINT_RADIUS.min, r));
    setPoints(objectives.points.map((q) => (q.id === id ? { ...q, r: clamp(q.r + delta) } : q)));
  },
  remove: (id: string) => setPoints(objectives.points.filter((q) => q.id !== id)),
};

let downAt: { x: number; y: number } | null = null;
canvas.addEventListener("pointerdown", (e) => (downAt = { x: e.clientX, y: e.clientY }));
canvas.addEventListener("click", (e) => {
  const dragged = downAt && Math.hypot(e.clientX - downAt.x, e.clientY - downAt.y) > 4;
  if (controls.mode !== "overview" || dragged) return;
  if (placingObjective) {
    const at = controls.floorAt(e.clientX, e.clientY);
    if (at) dropObjective(at);
    return;
  }
  if (!placing) return;
  const p = controls.floorAt(e.clientX, e.clientY);
  if (!p) return;
  if (!placing.at) {
    placing.at = p;
    updatePlacingHint();
    return;
  }
  const heading = ((Math.atan2(p.x - placing.at.x, -(p.z - placing.at.z)) * 180) / Math.PI + 360) % 360;
  finishPlacing(placing.id, placing.at.x, placing.at.z, heading);
});
canvas.addEventListener("mousemove", (e) => {
  if (!placing?.at || controls.mode !== "overview") return;
  const p = controls.floorAt(e.clientX, e.clientY);
  if (!p) return;
  const line = placeMarker.getObjectByName("aim") as THREE.Line;
  line.geometry.setFromPoints([new THREE.Vector3(), new THREE.Vector3(p.x - placing.at.x, 0, p.z - placing.at.z)]);
});

window.addEventListener("keydown", (e) => {
  if (e.target instanceof HTMLInputElement) return;
  if (e.code === "Tab") {
    e.preventDefault();
    document.querySelector<HTMLElement>("#scoreboard")!.hidden = false;
  }
  if (e.code === "Escape" && (placing || placingObjective)) {
    placing = null;
    stopPlacingObjective();
  }
  const n = Number(e.key);
  if (n >= 1 && n <= 9 && game.players[n - 1]) spectate(game.players[n - 1].id);
  if (e.code === "KeyP") {
    if (!selectedId) return feed.push("Pick a player with Place first");
    if (controls.mode === "walk" || controls.mode === "fly") {
      // Stand where they are in the 3D map, look where they look, press P.
      const cam = controls.perspective;
      finishPlacing(selectedId, cam.position.x, cam.position.z, yawToHeading(controls.facing));
    } else {
      startPlacing(selectedId);
    }
  }
});
window.addEventListener("keyup", (e) => {
  if (e.code === "Tab") document.querySelector<HTMLElement>("#scoreboard")!.hidden = true;
});
window.addEventListener("blur", () => (document.querySelector<HTMLElement>("#scoreboard")!.hidden = true));

controls.onModeChange = (mode) => {
  if (mode !== "follow") followId = null;
  if (mode !== "overview" && (placing || placingObjective)) {
    placing = null;
    stopPlacingObjective();
  }
  document.body.dataset.mode = mode;
};
document.body.dataset.mode = controls.mode;
document.addEventListener("pointerlockchange", () => {
  document.body.classList.toggle("playing", document.pointerLockElement === canvas);
});

const panelActions = {
  spectate,
  place: startPlacing,
  respawn: (id: string) => {
    const tag = game.players.find((p) => p.id === id)?.tag;
    if (tag != null) combat.respawn(tag);
  },
  remove: (id: string) => {
    if (confirm(`Forget ${nameOf(id)}?`)) game.remove(id);
  },
};

function refreshPlayers() {
  players.set(game.players);
  renderScoreboard(game.players, combat);
  renderPlayersPanel(game.players, combat, selectedId, game.connected, panelActions);
  renderMatchPanel(combat, game.players, objectives.points);
}

game.onStatus = (up) => {
  feed.push(up ? "Connected to the game server" : "Game server offline", up ? "#3ccf8e" : "#ff5c6c");
  refreshPlayers();
};
game.onMap = () => {
  syncGameMap();
  objectives.update(combat.objectives);
};
game.onPlayers = (list) => {
  feed.diff(list);
  refreshPlayers();
  if (followId && !list.some((p) => p.id === followId && p.calibrated)) controls.setMode("walk");
};

// ---- Shooting: hit points, shots and the match ------------------------------------------------

const tagName = (tag: number) => players.byTag(tag)?.player.name ?? `P${tag}`;
const teamText = (t: Team) => TEAM_NAMES[t];

combat.onStatus = (up) => {
  feed.push(up ? "Connected to the shooting server" : "Shooting server offline", up ? "#3ccf8e" : "#ff5c6c");
  refreshPlayers();
};
combat.onChange = refreshPlayers;
combat.onShot = (shot) => {
  const shooter = players.byTag(shot.shooter_id);
  const target = players.byTag(shot.target_id);
  const team = combat.fighter(shot.shooter_id)?.team;
  const color = shooter ? playerColor(shooter.player) : team ? TEAM_COLORS[team] : "#fff";
  // The shot only shows in the map if position tracking knows where both players are.
  if (shooter?.pose && target?.pose) tracers.add(shooter.pose, target.pose, color, shot.killed);
  target?.avatar.hit();
  feed.push(
    shot.killed
      ? `${tagName(shot.shooter_id)} eliminated ${tagName(shot.target_id)}`
      : `${tagName(shot.shooter_id)} hit ${tagName(shot.target_id)} · ${shot.hp}/${shot.max_hp} HP`,
    color,
  );
};
combat.onMatch = (started, scores) => {
  if (started) {
    feed.push(`Match started: most kills in ${clock(matchSeconds())} wins`, "#ffd34d");
  } else if (scores) {
    const result = scores.winner === "tie" ? "it's a tie" : `${teamText(scores.winner)} wins`;
    const how = scores.flag
      ? `flag ${clock(scores.red_flag_s)}–${clock(scores.blue_flag_s)}, kills ${scores.red_kills}–${scores.blue_kills}`
      : `${scores.red_kills}–${scores.blue_kills} kills`;
    const why = scores.reason === "time" ? "Time's up: " : scores.reason === "flag" ? "On the flag: " : "Match over: ";
    feed.push(`${why}${result} (${how})`, "#ffd34d");
  }
};
combat.onFlag = ({ team, from }) =>
  feed.push(`${teamText(team)} took the flag${from ? ` from ${teamText(from)}` : ""}`, TEAM_COLORS[team]);
combat.onRespawn = ({ player_id, how }) => {
  const who = players.byTag(player_id)?.player;
  const color = who ? playerColor(who) : "#fff";
  const name = tagName(player_id);
  feed.push(how === "base" ? `${name} is back in at base` : how === "host" ? `${name} brought back in by the host` : `${name} is back in`, color);
};

/** Match length from the Match panel (seconds): most kills when it runs out wins. */
function matchSeconds() {
  const s = Number(document.querySelector<HTMLInputElement>("#match-length")!.value);
  return Number.isFinite(s) && s >= 10 ? Math.round(s) : 30;
}
document.querySelector<HTMLButtonElement>("#match-start")!.onclick = () => combat.start(matchSeconds());
document.querySelector<HTMLButtonElement>("#match-stop")!.onclick = () => {
  if (confirm("End the match?")) combat.stop();
};
refreshPlayers();

// ---- Render loop ----------------------------------------------------------------------------

let last = performance.now();
let fps = 60;
let hudAt = 0;
renderer.setAnimationLoop((now) => {
  const dt = Math.min(0.1, (now - last) / 1000);
  last = now;
  fps = fps * 0.95 + (1 / Math.max(dt, 1e-3)) * 0.05;
  controls.update(dt);

  const eye = controls.perspective;
  you.visible = controls.mode === "overview";
  you.position.set(eye.position.x, 1.0, eye.position.z);
  you.rotation.y = eye.rotation.y;

  players.hiddenId = controls.mode === "follow" && !controls.thirdPerson ? followId : null;
  players.animate(dt, controls.camera, canvas.clientWidth, canvas.clientHeight);
  objectives.setOverview(controls.mode === "overview");
  objectives.animate(controls.camera, canvas.clientWidth, canvas.clientHeight);
  tracers.animate(dt);
  renderer.render(scene, controls.camera);

  const view = { x: eye.position.x, z: eye.position.z, yaw: eye.rotation.y };
  const blips = players.poses();
  if (controls.mode !== "overview") {
    drawCompass(view, blips);
    minimap.draw(view, blips, players.hiddenId, objectives.points, combat.objectives);
  }
  if (now - hudAt > 200) {
    hudAt = now;
    objectives.update(combat.objectives);
    renderObjectivesPanel(objectives.points, combat.objectives, placingObjective?.id ?? null, objectiveActions);
    const mode = controls.mode === "follow"
      ? `Watching ${nameOf(followId ?? "")}${controls.thirdPerson ? " (shoulder)" : ""}`
      : { walk: "Walk", fly: "Fly", overview: "Map" }[controls.mode];
    const online = game.players.filter((p) => p.connected).length;
    setHud(mode, eye.position, fps, game.connected ? `${online} player${online === 1 ? "" : "s"} online` : "game server offline");
    renderReadyList(readyChecklist());
  }
});

/** The host's checklist before a match, from what the servers report right now. */
function readyChecklist(): ReadyItem[] {
  const items: ReadyItem[] = [];
  const active = state.maps.find((m) => m.id === state.activeId);
  items.push(
    current && active && current.map.id === active.id
      ? { state: "done", label: `Map: ${active.name}` }
      : { state: "todo", label: "Map", detail: "Open a Scaniverse scan (Open map file) or pick one in Maps" },
  );
  if (!game.connected || !combat.connected) {
    items.push({ state: "warn", label: "Servers", detail: `${!game.connected ? "game server" : "shooting server"} offline: start run.py` });
  }
  const joined = game.players.filter((p) => p.connected);
  // Tags mode: the station button prints the tags; clothes mode: it scans players.
  const station = document.querySelector<HTMLAnchorElement>("#scan-station")!;
  const tagsMode = combat.idMode === "tags";
  if (station.dataset.mode !== combat.idMode) {
    station.dataset.mode = combat.idMode;
    station.textContent = tagsMode ? "Print tags" : "Scan station";
    station.title = tagsMode ? "The printable chest tags (players 1–10), to print or show on a screen" : station.title;
    if (tagsMode) station.href = "/combat/tags";
  }
  if (tagsMode) {
    items.push({ state: "todo", label: "Tags on", detail: "Each player wears their printed chest tag, a different number" });
  }
  if (combat.idMode === "clothes") {
    const scanned = joined.filter((p) => combat.fighter(p.tag)?.scanned);
    const missing = joined.filter((p) => !combat.fighter(p.tag)?.scanned).map((p) => p.name);
    items.push(
      !joined.length
        ? { state: "todo", label: "Scan players", detail: "Each player turns around once at the scan station" }
        : missing.length
          ? { state: "warn", label: `Scanned ${scanned.length} of ${joined.length}`, detail: `Not scanned yet: ${missing.join(", ")}` }
          : { state: "done", label: `All ${joined.length} players scanned` },
    );
  }
  const placed = joined.filter((p) => p.calibrated);
  items.push(
    !joined.length
      ? { state: "todo", label: "Players join", detail: "HTS Player app, with the address below" }
      : placed.length < joined.length
        ? { state: "warn", label: `${joined.length} joined, ${placed.length} placed`, detail: "Click Place next to each player, then where they stand" }
        : { state: "done", label: `${joined.length} player${joined.length === 1 ? "" : "s"} joined and placed` },
  );
  const has = (kind: string, team?: string) => objectives.points.some((p) => p.kind === kind && (!team || p.team === team));
  const missingObj = [!has("base", "red") && "Red base", !has("base", "blue") && "Blue base"].filter(Boolean);
  items.push(
    missingObj.length
      ? { state: "todo", label: "Bases", detail: `Place: ${missingObj.join(", ")}` }
      : { state: "done", label: "Both bases placed" },
  );
  items.push(combat.active ? { state: "done", label: "Match running" } : { state: "todo", label: "Start match", detail: "In the Match panel" });
  return items;
}

setupUploads();
setupFoldingPanels();
void setupConnectPanel();
subscribeMaps(onState);
