// Spectator viewer: shows the active map as a Gaussian splat, walked through first-person like a
// shooter map, with the players' live positions from the game server, their hit points and shots
// from the shooting server, and the match: the bases and the flag teams fight over.
import * as THREE from "three";
import { SparkRenderer, SplatMesh } from "@sparkjsdev/spark";
import type { MapInfo } from "../server/maps.ts";
import { autoCalibrate, currentUp, nudgeFloor, rotateY90, sampleCenters } from "./calibrate.ts";
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
  type MapsState, renderMapList, setHud, setStatus, setupConnectPanel, setupUploads, subscribeMaps,
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
const ANY_UP = [new THREE.Vector3(0, 1, 0), new THREE.Vector3(0, -1, 0), new THREE.Vector3(0, 0, 1), new THREE.Vector3(0, 0, -1)];

let state: MapsState = { maps: [], activeId: null };
let current: { map: MapInfo; mesh: SplatMesh; centers?: Float32Array } | null = null;
let loadingKey: string | null = null;
const calibrating = new Set<string>();

const keyOf = (m: MapInfo) => `${m.id}/${m.splatFile}`;

function applyTransform(mesh: SplatMesh, transform: number[] | null) {
  const m = new THREE.Matrix4();
  if (transform) m.fromArray(transform);
  m.decompose(mesh.position, mesh.quaternion, mesh.scale);
}

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
  const mesh = new SplatMesh({ url: `/maps/${map.id}/${map.splatFile}` });
  try {
    await mesh.initialized;
  } catch (err) {
    console.error("failed to load splat", err);
    if (loadingKey === key) loadingKey = null;
    mesh.dispose();
    return;
  }
  if (loadingKey !== key) {
    mesh.dispose(); // a newer preview or another map was requested meanwhile
    return;
  }
  loadingKey = null;

  const latest = state.maps.find((m) => m.id === map.id) ?? map;
  let centers: Float32Array | undefined;
  let transform = latest.transform;
  if (!transform) {
    centers = sampleCenters(mesh);
    transform = autoCalibrate(centers, latest.source === "scan" ? SCAN_UP : ANY_UP);
    if (!calibrating.has(map.id)) {
      calibrating.add(map.id);
      saveTransform(map.id, transform).finally(() => calibrating.delete(map.id));
    }
  }
  applyTransform(mesh, transform);
  scene.add(mesh);

  const switchedMap = current?.map.id !== map.id;
  if (current) {
    scene.remove(current.mesh);
    current.mesh.dispose();
  }
  current = { map: { ...latest, transform }, mesh, centers };
  if (switchedMap) controls.resetPose();
  updateAdjustPanel();
  syncGameMap();
}

// ---- Game map -------------------------------------------------------------------------------

// The map on screen is the game map: the game server gets its footprint and floor plan, so
// player positions from the phones line up with the scan.
let publishing = false;

function syncGameMap() {
  if (publishing || !game.connected || !game.map || !current?.map.transform) return;
  const { map, mesh } = current;
  const transform = map.transform!;
  if (map.id !== state.activeId || isLinked(game.map, map, transform)) return;
  const fresh = game.map.splat?.id !== map.id;
  publishing = true;
  publishGameMap(game.map, map, mesh, transform)
    .then((next) => {
      if (!next) return;
      game.map = next;
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
  document.querySelector<HTMLElement>("#adjust")!.hidden = !(active?.splatFile && current?.map.id === active.id);
}

function onState(next: MapsState) {
  state = next;
  renderMapList(state);
  const active = state.maps.find((m) => m.id === state.activeId) ?? null;
  updateAdjustPanel();

  if (!active) {
    setStatus("No map yet.<br>Scan one with the phone app (QR code, top right) or drop a .ply/.spz here.");
  } else if (active.status === "failed") {
    setStatus(`Training <b>${active.name}</b> failed.<br><small>${active.error ?? ""}</small>`);
  } else if (!active.splatFile) {
    const pct = Math.round(active.progress * 100);
    setStatus(
      active.status === "uploaded"
        ? `Scan <b>${active.name}</b> received. Waiting to train…`
        : `Training <b>${active.name}</b>… ${pct}%<br><small>A first preview appears in about a minute.</small>`,
    );
  } else if (active.status === "training") {
    setStatus(`Training ${Math.round(active.progress * 100)}% · the map sharpens as it trains`, true);
  } else {
    setStatus(null);
  }

  if (!active?.splatFile) {
    if (current && current.map.id !== active?.id) {
      scene.remove(current.mesh);
      current.mesh.dispose();
      current = null;
    }
    return;
  }
  const key = keyOf(active);
  if (current && keyOf(current.map) === key) {
    if (active.transform && JSON.stringify(active.transform) !== JSON.stringify(current.map.transform)) {
      applyTransform(current.mesh, active.transform);
    }
    current.map = { ...active, transform: active.transform ?? current.map.transform };
  } else if (loadingKey !== key) {
    void loadMap(active);
  }
}

// ---- Adjust panel ---------------------------------------------------------------------------

document.querySelectorAll<HTMLButtonElement>("[data-adjust]").forEach((btn) => {
  btn.onclick = () => {
    if (!current?.map.transform) return;
    const t = current.map.transform;
    current.centers ??= sampleCenters(current.mesh);
    const next = {
      flip: () => autoCalibrate(current!.centers!, [currentUp(t).negate()]),
      rotate: () => rotateY90(t),
      "floor-up": () => nudgeFloor(t, -0.05),
      "floor-down": () => nudgeFloor(t, 0.05),
      reset: () => autoCalibrate(current!.centers!, current!.map.source === "scan" ? SCAN_UP : ANY_UP),
    }[btn.dataset.adjust!]!();
    current.map.transform = next;
    applyTransform(current.mesh, next);
    void saveTransform(current.map.id, next);
    syncGameMap();
  };
});

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
  controls.follow(() => players.pose(id));
  feed.push(`Watching ${nameOf(id)} · V: over the shoulder · WASD: take over`);
}

function startPlacing(id: string) {
  selectedId = players.selectedId = id;
  placing = { id, at: null };
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
    const flag = objectives.points.some((p) => p.kind === "flag");
    feed.push(flag ? `Match started: hold the flag ${clock(winSeconds())} to win` : "Match started: guns are live", "#ffd34d");
  } else if (scores) {
    const result = scores.winner === "tie" ? "it's a tie" : `${teamText(scores.winner)} wins`;
    const how = scores.flag
      ? `flag ${clock(scores.red_flag_s)}–${clock(scores.blue_flag_s)}, kills ${scores.red_kills}–${scores.blue_kills}`
      : `${scores.red_kills}–${scores.blue_kills}`;
    feed.push(`Match over: ${result}${scores.reason === "flag" ? " on the flag" : ""} (${how})`, "#ffd34d");
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

/** Flag time to win, from the Match panel (minutes). */
function winSeconds() {
  const min = Number(document.querySelector<HTMLInputElement>("#match-win")!.value);
  return Number.isFinite(min) && min > 0 ? Math.round(min * 60) : 120;
}
document.querySelector<HTMLButtonElement>("#match-start")!.onclick = () => combat.start(winSeconds());
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
  }
});

setupUploads();
void setupConnectPanel();
subscribeMaps(onState);
