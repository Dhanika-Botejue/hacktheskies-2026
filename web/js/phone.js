// Player phone. Camera tracking (ARCore via WebXR) is the main source of
// position; the step counter, gyro and compass run alongside and the server
// fuses them all. Phones without WebXR AR (iPhone Safari) fall back to steps.

import { MapView, connect } from './mapview.js';
import { StepDetector, headingFromOrientation, requestSensorPermission, yawFromQuaternion } from './pdr.js';

const $ = (id) => document.getElementById(id);
const POSE_INTERVAL = 1000 / 15; // ms between camera poses sent to the server

function load(key, fallback) {
  try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
}
function save(key, value) {
  try { localStorage.setItem(key, value); } catch { /* private mode */ }
}

let playerId = load('htsPlayerId', null);
if (!playerId) { playerId = crypto.randomUUID(); save('htsPlayerId', playerId); }
$('name').value = load('htsName', '');

let ws = null;
let view = null;
let me = null; // this player's latest state from the server
let gyro = null; // phone heading from the orientation sensor (deg, arbitrary zero)
let compass = null; // heading from magnetic north (deg), if the phone has one
let placing = null; // null | 'pos' | {x, y}

// Camera tracking state
let arSupported = false;
let session = null;
let refSpace = null;
let gl = null;
let arYaw = null; // camera heading in AR space (deg)
let arTracking = false;
let epoch = 0; // bumps whenever camera tracking restarts with a new frame
let lastPoseSent = 0;

const detector = new StepDetector(({ heading, len }) => {
  ws.send({ t: 'step', heading, len, src: session ? 'ar' : 'gyro' });
  $('steps').textContent = detector.count;
});
detector.k = parseFloat(load('htsStrideK', detector.k));
detector.high = parseFloat(load('htsStepHigh', detector.high));

(async () => {
  arSupported = !!(window.isSecureContext && navigator.xr && await navigator.xr.isSessionSupported('immersive-ar').catch(() => false));
  $('arNote').textContent = arSupported
    ? 'Camera tracking is available on this phone.'
    : 'Camera tracking isn\'t available in this browser, so this phone will track by counting steps (less accurate). iPhones: use the iPhone app. Android: use Chrome with Google Play Services for AR.';
})();

$('start').addEventListener('click', async () => {
  const name = $('name').value.trim();
  if (!name) { $('setupErr').textContent = 'Enter a name first.'; return; }
  save('htsName', name);
  if (!window.isSecureContext) {
    $('setupErr').textContent = 'This page must be opened over https:// for the sensors to work.';
    return;
  }
  try {
    await requestSensorPermission();
  } catch (e) {
    $('setupErr').textContent = e.message;
    return;
  }
  keepAwake();
  start(name);
  if (arSupported) startCamera();
});

function start(name) {
  $('setup').style.display = 'none';
  $('run').style.display = 'contents';
  view = new MapView($('map'));
  view.showParticles = false;
  view.highlightId = playerId;
  view.overlay = drawPlacing;

  ws = connect({ role: 'player', id: playerId, name, platform: arSupported ? 'android' : 'web' },
    onMessage, (up) => $('conn').classList.toggle('on', up));

  window.addEventListener('deviceorientation', (e) => {
    if (e.alpha == null) return;
    gyro = headingFromOrientation(e.alpha, e.beta, e.gamma);
    if (e.webkitCompassHeading != null) compass = e.webkitCompassHeading; // iPhone
    if (!session) detector.addHeading(gyro);
  });
  window.addEventListener('deviceorientationabsolute', (e) => { // Android: alpha is relative to north
    if (e.alpha != null) compass = headingFromOrientation(e.alpha, e.beta, e.gamma);
  });
  window.addEventListener('devicemotion', (e) => {
    const a = e.accelerationIncludingGravity;
    if (!a || a.x == null) return;
    detector.addMotion(a.x, a.y, a.z, e.timeStamp);
  });

  setInterval(() => {
    if (gyro == null && arYaw == null) { banner('No motion sensors found. Open this page on a phone.'); return; }
    ws.send({ t: 'heading', gyro, compass }, false);
    const shown = session ? arYaw : gyro;
    if (shown != null) $('heading').textContent = `${Math.round(shown)}°`;
    $('mode').textContent = session ? (arTracking ? 'Camera' : 'Camera lost') : 'Steps';
    updateBanner();
  }, 200);
}

function onMessage(msg) {
  if (msg.t === 'map') { view.needsFit = true; view.setMap(msg.map); }
  else if (msg.t === 'state') {
    view.players = msg.players;
    me = msg.players.find((p) => p.id === playerId) || null;
  }
}

// ---------- camera tracking (WebXR) ----------

async function startCamera() {
  try {
    session = await navigator.xr.requestSession('immersive-ar', {
      requiredFeatures: ['local'],
      optionalFeatures: ['dom-overlay'],
      domOverlay: { root: $('root') },
    });
    gl = gl || document.createElement('canvas').getContext('webgl', { xrCompatible: true });
    await gl.makeXRCompatible();
    await session.updateRenderState({ baseLayer: new XRWebGLLayer(session, gl) });
    refSpace = await session.requestReferenceSpace('local');
  } catch (e) {
    session?.end();
    session = null;
    banner(`Couldn't start camera tracking (${e.message}). Using step counting.`);
    $('cameraBtn').style.display = '';
    return;
  }
  epoch++;
  refSpace.addEventListener('reset', () => { epoch++; }); // tracking restarted with a new origin
  session.addEventListener('end', () => {
    session = null;
    arTracking = false;
    $('cameraBtn').style.display = '';
  });
  $('cameraBtn').style.display = 'none';
  session.requestAnimationFrame(onXRFrame);
}

function onXRFrame(t, frame) {
  const s = frame.session;
  s.requestAnimationFrame(onXRFrame);
  const layer = s.renderState.baseLayer;
  gl.bindFramebuffer(gl.FRAMEBUFFER, layer.framebuffer);
  gl.clearColor(0, 0, 0, 0);
  gl.clear(gl.COLOR_BUFFER_BIT);

  const pose = frame.getViewerPose(refSpace);
  arTracking = !!pose && !pose.emulatedPosition;
  if (pose) {
    arYaw = yawFromQuaternion(pose.transform.orientation);
    detector.addHeading(arYaw);
    if (t - lastPoseSent >= POSE_INTERVAL) {
      lastPoseSent = t;
      const p = pose.transform.position;
      ws.send({ t: 'pose', x: p.x, z: p.z, yaw: arYaw, tracking: arTracking, epoch }, false);
    }
  }
  view.draw(); // the page's own animation loop pauses during an AR session
}

$('cameraBtn').addEventListener('click', () => { if (!session) startCamera(); });

// The overlay is touchable; don't let taps also count as AR "select" input.
$('root').addEventListener('beforexrselect', (e) => e.preventDefault());

// ---------- status ----------

function banner(text) {
  $('banner').textContent = text;
  $('banner').style.display = text ? '' : 'none';
}

function updateBanner() {
  if (placing === 'pos') banner('Tap where you are standing.');
  else if (placing) banner('Face forward, then tap the direction you are facing.');
  else if (!me || !me.calibrated) banner('Tap "Set my position" to start, or ask the host to place you.');
  else if (session && !arTracking) banner('Camera lost tracking — using steps. Keep the camera uncovered and pointed at the room.');
  else if (arSupported && !session) banner('Camera tracking is off — using steps. Tap "Restart camera".');
  else banner('');
}

// ---------- placing yourself on the map ----------

$('placeBtn').addEventListener('click', () => {
  placing = placing ? null : 'pos';
  $('placeBtn').classList.toggle('active', !!placing);
  updateBanner();
});

$('map').addEventListener('pointerup', (e) => {
  if (!placing) return;
  const p = view.toWorld(e.clientX, e.clientY);
  if (placing === 'pos') { placing = p; updateBanner(); return; }
  const mapHeading = ((Math.atan2(p.x - placing.x, -(p.y - placing.y)) * 180) / Math.PI + 360) % 360;
  // Send the freshest readings first so the server calibrates against this exact moment.
  ws.send({ t: 'heading', gyro, compass });
  ws.send({ t: 'calibrate', x: placing.x, y: placing.y, mapHeading });
  placing = null;
  $('placeBtn').classList.remove('active');
  updateBanner();
});

function drawPlacing(ctx) {
  if (!placing || placing === 'pos') return;
  const P = view.toScreen(placing.x, placing.y);
  ctx.fillStyle = '#ffc914';
  ctx.beginPath();
  ctx.arc(P.x, P.y, 7, 0, Math.PI * 2);
  ctx.fill();
}

// ---------- keep the screen on while strapped to the chest ----------

// The browser drops the lock whenever the page is hidden, so take it again on return.
async function keepAwake() {
  try { await navigator.wakeLock?.request('screen'); } catch { /* unsupported */ }
}
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && ws) keepAwake();
});
