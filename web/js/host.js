import { MapView, colorFor, connect } from './mapview.js';

const $ = (id) => document.getElementById(id);
const view = new MapView($('map'));
const canvas = view.canvas;

let map = view.map;
let mapLoaded = false;
let players = [];
let selectedId = null;
let tool = 'wall';
let mouse = null; // world coords of cursor
let shift = false;
let chainStart = null; // wall tool: last committed point
let scaleA = null; // scale tool: first point
let placePos = null; // place tool: chosen position, waiting for facing direction
const history = [];

const HINTS = {
  move: 'Drag to pan. Pinch or Ctrl+scroll to zoom.',
  wall: 'Click to start a wall, click again to add segments. Esc, right-click or double-click ends the chain. Hold Shift to keep it straight.',
  erase: 'Click a wall to delete it.',
  scale: 'Click two points a known distance apart (e.g. a door width or hallway length), then enter the real distance.',
  place: 'Select a player in the list, click where they are standing, then click the direction they are facing.',
};

const ws = connect({ role: 'host' }, onMessage, (up) => $('conn').classList.toggle('on', up));

function onMessage(msg) {
  if (msg.t === 'map' && (!mapLoaded || msg.map.splat?.id !== map.splat?.id || msg.map.image?.url !== map.image?.url)) {
    // This page edits the walls. Besides the first load, only take the server's map when the 3D
    // viewer has made a (new) scan the game map, or re-rendered its floor plan.
    if (mapLoaded && msg.map.splat?.id !== map.splat?.id) history.length = 0;
    const firstOrNewScan = !mapLoaded || msg.map.splat?.id !== map.splat?.id;
    mapLoaded = true;
    map = msg.map;
    view.needsFit = firstOrNewScan;
    view.setMap(map);
    updateInfo();
    updateScanLock();
  } else if (msg.t === 'state') {
    players = msg.players;
    view.players = players;
    renderPlayers();
  }
}

// ---------- map editing ----------

let saveTimer = null;
function commit(mutate) {
  history.push(JSON.stringify(map));
  if (history.length > 100) history.shift();
  mutate();
  afterChange();
}
function afterChange() {
  view.setMap(map);
  updateInfo();
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    fetch('/api/map', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(map) });
  }, 250);
}
function undo() {
  if (!history.length) return;
  map = JSON.parse(history.pop());
  chainStart = null;
  afterChange();
}
function updateInfo() {
  const source = map.splat ? ' · from 3D scan' : map.image ? '' : ' · no image';
  $('mapInfo').textContent = `${map.width.toFixed(1)} × ${map.height.toFixed(1)} m · ${map.walls.length} walls${source}`;
}

// A map made from a 3D scan is already in real metres and lined up with the scan, so rescaling it
// or swapping its floor plan would break the alignment.
function updateScanLock() {
  const locked = !!map.splat;
  for (const id of ['upload', 'clearImage']) $(id).disabled = locked;
  document.querySelector('[data-tool="scale"]').disabled = locked;
  if (locked && tool === 'scale') setTool('wall');
}

const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);

function snap(p, from) {
  const radius = 10 / view.scale;
  let best = null, bestD = radius;
  for (const w of map.walls) {
    for (const e of [{ x: w[0], y: w[1] }, { x: w[2], y: w[3] }]) {
      const d = dist(p, e);
      if (d < bestD) { best = e; bestD = d; }
    }
  }
  if (best) return { ...best, snapped: true };
  if (shift && from) {
    return Math.abs(p.x - from.x) > Math.abs(p.y - from.y) ? { x: p.x, y: from.y } : { x: from.x, y: p.y };
  }
  return p;
}

function nearestWall(p) {
  const radius = 8 / view.scale;
  let best = -1, bestD = radius;
  map.walls.forEach((w, i) => {
    const dx = w[2] - w[0], dy = w[3] - w[1];
    const len2 = dx * dx + dy * dy || 1e-9;
    const t = Math.max(0, Math.min(1, ((p.x - w[0]) * dx + (p.y - w[1]) * dy) / len2));
    const d = Math.hypot(p.x - (w[0] + t * dx), p.y - (w[1] + t * dy));
    if (d < bestD) { best = i; bestD = d; }
  });
  return best;
}

function setTool(t) {
  if (t === 'scale' && map.splat) return;
  tool = t;
  chainStart = scaleA = placePos = null;
  document.querySelectorAll('[data-tool]').forEach((b) => b.classList.toggle('active', b.dataset.tool === t));
  $('hint').textContent = HINTS[t];
  canvas.style.cursor = t === 'move' ? 'grab' : 'crosshair';
}

function headingFrom(a, b) {
  return ((Math.atan2(b.x - a.x, -(b.y - a.y)) * 180) / Math.PI + 360) % 360;
}

function handleClick(p) {
  if (tool === 'wall') {
    const q = snap(p, chainStart);
    if (chainStart && dist(chainStart, q) > 0.05) {
      const a = chainStart;
      commit(() => map.walls.push([a.x, a.y, q.x, q.y].map((v) => Math.round(v * 100) / 100)));
    }
    chainStart = q;
  } else if (tool === 'erase') {
    const i = nearestWall(p);
    if (i >= 0) commit(() => map.walls.splice(i, 1));
  } else if (tool === 'scale') {
    if (!scaleA) { scaleA = snap(p); return; }
    const b = snap(p);
    const measured = dist(scaleA, b);
    scaleA = null;
    const real = parseFloat(prompt(`That line is ${measured.toFixed(2)} m on the map. How long is it in reality (meters)?`, measured.toFixed(2)));
    if (!(real > 0) || measured < 1e-3) return;
    const k = real / measured;
    commit(() => {
      map.width *= k;
      map.height *= k;
      map.walls = map.walls.map((w) => w.map((v) => Math.round(v * k * 100) / 100));
    });
    view.scale /= k;
  } else if (tool === 'place') {
    if (!selectedId) { $('hint').textContent = 'Select a player in the list first.'; return; }
    if (!placePos) { placePos = p; return; }
    ws.send({ t: 'place', id: selectedId, x: placePos.x, y: placePos.y, mapHeading: headingFrom(placePos, p) });
    placePos = null;
  }
}

// ---------- pointer / keyboard ----------

let drag = null;
canvas.addEventListener('contextmenu', (e) => e.preventDefault());
canvas.addEventListener('pointerdown', (e) => {
  canvas.setPointerCapture(e.pointerId);
  const pan = e.button === 1 || (e.button === 0 && tool === 'move');
  if (e.button === 2) { chainStart = scaleA = placePos = null; return; }
  drag = { x: e.clientX, y: e.clientY, pan, moved: false };
});
canvas.addEventListener('pointermove', (e) => {
  mouse = view.toWorld(e.clientX, e.clientY);
  if (tool === 'erase') view.highlightWall = nearestWall(mouse);
  if (!drag) return;
  const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
  if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
  if (drag.pan) { view.panBy(dx, dy); drag.x = e.clientX; drag.y = e.clientY; }
});
canvas.addEventListener('pointerup', (e) => {
  if (drag && !drag.pan && !drag.moved && e.button === 0) handleClick(view.toWorld(e.clientX, e.clientY));
  drag = null;
});
canvas.addEventListener('pointerleave', () => { mouse = null; view.highlightWall = -1; });
canvas.addEventListener('dblclick', () => { if (tool === 'wall') chainStart = null; });
canvas.addEventListener('wheel', (e) => {
  e.preventDefault();
  if (e.ctrlKey) view.zoomAt(e.clientX, e.clientY, Math.exp(-e.deltaY * 0.01));
  else view.panBy(-e.deltaX, -e.deltaY);
}, { passive: false });

const KEYS = { m: 'move', w: 'wall', e: 'erase', s: 'scale', p: 'place' };
window.addEventListener('keydown', (e) => {
  shift = e.shiftKey;
  if (e.target.tagName === 'INPUT') return;
  if ((e.metaKey || e.ctrlKey) && e.key === 'z') { e.preventDefault(); undo(); return; }
  if (e.key === 'Escape') chainStart = scaleA = placePos = null;
  else if (e.key === 'f') view.fit();
  else if (KEYS[e.key] && !e.metaKey && !e.ctrlKey) setTool(KEYS[e.key]);
});
window.addEventListener('keyup', (e) => { shift = e.shiftKey; });

document.querySelectorAll('[data-tool]').forEach((b) => b.addEventListener('click', () => setTool(b.dataset.tool)));
$('fit').addEventListener('click', () => view.fit());
$('undo').addEventListener('click', undo);
$('clearWalls').addEventListener('click', () => {
  if (map.walls.length && confirm('Delete all walls?')) commit(() => { map.walls = []; });
});
$('showParticles').addEventListener('change', (e) => { view.showParticles = e.target.checked; });
$('showTruth').addEventListener('change', (e) => { view.showTruth = e.target.checked; });

// ---------- floor plan image ----------

$('upload').addEventListener('click', () => $('file').click());
$('file').addEventListener('change', async (e) => {
  const file = e.target.files[0];
  e.target.value = '';
  if (!file) return;
  const img = new Image();
  img.src = URL.createObjectURL(file);
  await img.decode();
  const res = await fetch('/api/map/image', { method: 'POST', headers: { 'Content-Type': file.type }, body: file });
  if (!res.ok) { alert(`Upload failed: ${await res.text()}`); return; }
  const { url } = await res.json();
  commit(() => {
    map.image = { url, px: [img.naturalWidth, img.naturalHeight] };
    map.height = (map.width * img.naturalHeight) / img.naturalWidth;
  });
  view.fit();
  setTool('scale');
});
$('clearImage').addEventListener('click', () => { if (map.image) commit(() => { map.image = null; }); });

// ---------- players list ----------

let lastPlayersHtml = '';
function renderPlayers() {
  const html = players.length ? players.map((p) => {
    const status = !p.calibrated ? 'not placed yet' : `±${p.spread.toFixed(1)} m${p.lost ? ` · ${p.lost} wall misses` : ''}`;
    const sensors = `${p.platform} · ${p.source}${p.compass ? ' · compass' : ''}`;
    return `<div class="player ${p.id === selectedId ? 'sel' : ''}" data-id="${p.id}">
      <div class="top"><span class="dot ${p.connected ? 'on' : ''}"></span>
        <span class="swatch" style="background:${colorFor(p.id)}"></span>${escapeHtml(p.name)}
        <button data-remove="${p.id}" title="Forget this player">✕</button></div>
      <div class="muted">${escapeHtml(sensors)}</div>
      <div class="muted">${p.steps} steps · ${status}</div></div>`;
  }).join('') : '<p class="muted">No phones connected yet.</p>';
  if (html !== lastPlayersHtml) { $('players').innerHTML = html; lastPlayersHtml = html; }
}
// pointerdown rather than click: the list re-renders often and a click can land on a replaced node.
$('players').addEventListener('pointerdown', (e) => {
  const rm = e.target.closest('[data-remove]');
  if (rm) { ws.send({ t: 'remove', id: rm.dataset.remove }); return; }
  const el = e.target.closest('[data-id]');
  if (!el) return;
  selectedId = el.dataset.id;
  view.highlightId = selectedId;
  setTool('place');
  renderPlayers();
});

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}

// ---------- tool previews ----------

view.overlay = (ctx) => {
  if (!mouse) return;
  const s = view.toScreen.bind(view);
  ctx.save();
  ctx.lineWidth = 2;
  ctx.font = '12px system-ui, sans-serif';
  ctx.fillStyle = '#fff';

  const line = (a, b, color, label) => {
    const A = s(a.x, a.y), B = s(b.x, b.y);
    ctx.strokeStyle = color;
    ctx.setLineDash([6, 4]);
    ctx.beginPath(); ctx.moveTo(A.x, A.y); ctx.lineTo(B.x, B.y); ctx.stroke();
    ctx.setLineDash([]);
    if (label) ctx.fillText(label, (A.x + B.x) / 2 + 8, (A.y + B.y) / 2 - 8);
  };

  if (tool === 'wall' || tool === 'scale') {
    const from = tool === 'wall' ? chainStart : scaleA;
    const q = snap(mouse, from);
    const Q = s(q.x, q.y);
    ctx.strokeStyle = q.snapped ? '#76b041' : '#29b6f6';
    ctx.beginPath(); ctx.arc(Q.x, Q.y, 5, 0, Math.PI * 2); ctx.stroke();
    if (from) line(from, q, tool === 'wall' ? '#29b6f6' : '#ffc914', `${dist(from, q).toFixed(2)} m`);
  } else if (tool === 'place' && placePos) {
    line(placePos, mouse, '#ffc914', 'facing');
    const P = s(placePos.x, placePos.y);
    ctx.fillStyle = '#ffc914';
    ctx.beginPath(); ctx.arc(P.x, P.y, 6, 0, Math.PI * 2); ctx.fill();
  }
  ctx.restore();
};

setTool('wall');
updateInfo();
updateScanLock();
