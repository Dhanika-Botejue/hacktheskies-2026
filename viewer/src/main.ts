// Spectator viewer: shows the active map as a Gaussian splat and lets you fly/walk through it.
import * as THREE from "three";
import { SparkRenderer, SplatMesh } from "@sparkjsdev/spark";
import type { MapInfo } from "../server/maps.ts";
import { autoCalibrate, currentUp, nudgeFloor, rotateY90, sampleCenters } from "./calibrate.ts";
import { SpectatorControls } from "./controls.ts";
import { MarkerLayer, SAMPLE_MARKERS } from "./markers.ts";
import {
  type MapsState, renderMapList, setHud, setStatus, setupConnectPanel, setupUploads, subscribeMaps,
} from "./ui.ts";

const canvas = document.querySelector<HTMLCanvasElement>("#view")!;
const renderer = new THREE.WebGLRenderer({ canvas, antialias: false });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0b0d12);
scene.add(new SparkRenderer({ renderer }));

const controls = new SpectatorControls(canvas);
const markers = new MarkerLayer();
markers.set(SAMPLE_MARKERS);
scene.add(markers.group);

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
}

function onState(next: MapsState) {
  state = next;
  renderMapList(state);
  const active = state.maps.find((m) => m.id === state.activeId) ?? null;
  document.querySelector<HTMLElement>("#adjust")!.hidden = !(active?.splatFile && current?.map.id === active.id);

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
  };
});

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

  renderer.render(scene, controls.camera);
  if (now - hudAt > 200) {
    hudAt = now;
    setHud(controls.mode, eye.position, fps);
  }
});

setupUploads();
void setupConnectPanel();
subscribeMaps(onState);
