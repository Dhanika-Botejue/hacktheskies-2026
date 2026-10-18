// Canvas renderer for the floor map, shared by the host and phone pages.
// World coordinates are meters (x right, y down). Headings are degrees
// clockwise from map "up".

const TEAM_COLORS = ['#e4572e', '#29b6f6', '#76b041', '#ffc914', '#b07bd6', '#ff8fab', '#17bebb', '#f0f0f0'];
// Players on a red/blue team wear its colour (same as the 3D viewer's).
const SIDE_COLORS = { red: '#ff4d5e', blue: '#4da3ff' };
export const playerColor = (p) => SIDE_COLORS[p.team] || colorFor(p.id);

export function colorFor(id) {
  let h = 0;
  for (const ch of id) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return TEAM_COLORS[h % TEAM_COLORS.length];
}

export class MapView {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.map = { width: 40, height: 30, image: null, walls: [] };
    this.players = [];
    this.image = null;
    this.imageUrl = null;
    this.scale = 20; // px per meter
    this.ox = 0; // screen px of world origin
    this.oy = 0;
    this.showParticles = true;
    this.showTruth = true;
    this.highlightId = null;
    this.highlightWall = -1;
    this.overlay = null; // (ctx, view) => void, drawn on top
    this.needsFit = true;

    new ResizeObserver(() => this.resize()).observe(canvas);
    this.resize();
    const loop = () => { this.draw(); requestAnimationFrame(loop); };
    requestAnimationFrame(loop);
  }

  resize() {
    const dpr = window.devicePixelRatio || 1;
    const r = this.canvas.getBoundingClientRect();
    this.canvas.width = Math.round(r.width * dpr);
    this.canvas.height = Math.round(r.height * dpr);
    this.dpr = dpr;
    if (this.needsFit) this.fit();
  }

  setMap(map) {
    this.map = map;
    const url = map.image && map.image.url;
    if (url !== this.imageUrl) {
      this.imageUrl = url;
      this.image = null;
      if (url) {
        const img = new Image();
        img.onload = () => { if (this.imageUrl === url) this.image = img; };
        img.src = url;
      }
    }
    if (this.needsFit) this.fit();
  }

  fit() {
    const r = this.canvas.getBoundingClientRect();
    if (!r.width || !r.height) return;
    const pad = 16;
    this.scale = Math.min((r.width - 2 * pad) / this.map.width, (r.height - 2 * pad) / this.map.height);
    this.ox = (r.width - this.map.width * this.scale) / 2;
    this.oy = (r.height - this.map.height * this.scale) / 2;
    this.needsFit = false;
  }

  // Screen (CSS px relative to canvas) <-> world (m)
  toWorld(clientX, clientY) {
    const r = this.canvas.getBoundingClientRect();
    return { x: (clientX - r.left - this.ox) / this.scale, y: (clientY - r.top - this.oy) / this.scale };
  }
  toScreen(x, y) {
    return { x: this.ox + x * this.scale, y: this.oy + y * this.scale };
  }

  zoomAt(clientX, clientY, factor) {
    const r = this.canvas.getBoundingClientRect();
    const sx = clientX - r.left, sy = clientY - r.top;
    const w = this.toWorld(clientX, clientY);
    this.scale = Math.min(400, Math.max(2, this.scale * factor));
    this.ox = sx - w.x * this.scale;
    this.oy = sy - w.y * this.scale;
  }

  panBy(dx, dy) { this.ox += dx; this.oy += dy; }

  draw() {
    const { ctx, dpr, scale: s } = this;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = '#14161a';
    ctx.fillRect(0, 0, this.canvas.width / dpr, this.canvas.height / dpr);

    ctx.save();
    ctx.translate(this.ox, this.oy);

    // Floor
    ctx.fillStyle = '#1e2229';
    ctx.fillRect(0, 0, this.map.width * s, this.map.height * s);
    if (this.image) {
      ctx.globalAlpha = 0.55;
      ctx.drawImage(this.image, 0, 0, this.map.width * s, this.map.height * s);
      ctx.globalAlpha = 1;
    }
    this.drawGrid();

    // Walls
    ctx.lineCap = 'round';
    this.map.walls.forEach((w, i) => {
      ctx.strokeStyle = i === this.highlightWall ? '#ff5252' : '#e8eaed';
      ctx.lineWidth = Math.max(2, 0.15 * s);
      ctx.beginPath();
      ctx.moveTo(w[0] * s, w[1] * s);
      ctx.lineTo(w[2] * s, w[3] * s);
      ctx.stroke();
    });

    this.drawPoints();
    for (const p of this.players) this.drawPlayer(p);
    ctx.restore();

    if (this.overlay) this.overlay(ctx, this);
  }

  // The bases (in their team's colour) and the flag the host placed in the 3D viewer: circle + name.
  drawPoints() {
    const { ctx, scale: s } = this;
    for (const p of this.map.points || []) {
      const color = SIDE_COLORS[p.team] || '#e8ecf4';
      ctx.beginPath();
      ctx.arc(p.x * s, p.y * s, p.r * s, 0, Math.PI * 2);
      ctx.globalAlpha = 0.14;
      ctx.fillStyle = color;
      ctx.fill();
      ctx.globalAlpha = 1;
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.stroke();
      ctx.fillStyle = color;
      ctx.font = '700 13px system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(p.kind === 'flag' ? `⚑ ${p.name}` : p.name, p.x * s, p.y * s);
      ctx.textBaseline = 'alphabetic';
    }
  }

  drawGrid() {
    const { ctx, scale: s } = this;
    const step = s > 30 ? 1 : s > 8 ? 5 : 10; // meters
    ctx.strokeStyle = 'rgba(255,255,255,0.05)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let x = 0; x <= this.map.width; x += step) { ctx.moveTo(x * s, 0); ctx.lineTo(x * s, this.map.height * s); }
    for (let y = 0; y <= this.map.height; y += step) { ctx.moveTo(0, y * s); ctx.lineTo(this.map.width * s, y * s); }
    ctx.stroke();
  }

  drawPlayer(p) {
    const { ctx, scale: s } = this;
    const color = playerColor(p);

    if (this.showParticles && p.particles) {
      ctx.fillStyle = color;
      ctx.globalAlpha = 0.35;
      for (const [x, y] of p.particles) ctx.fillRect(x * s - 1, y * s - 1, 2, 2);
      ctx.globalAlpha = 1;
    }
    if (this.showTruth && p.truth) {
      const [tx, ty] = p.truth;
      ctx.strokeStyle = '#fff';
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(tx * s - 5, ty * s - 5); ctx.lineTo(tx * s + 5, ty * s + 5);
      ctx.moveTo(tx * s + 5, ty * s - 5); ctx.lineTo(tx * s - 5, ty * s + 5);
      ctx.stroke();
    }
    if (!p.calibrated) return;

    const x = p.x * s, y = p.y * s;
    const r = Math.max(6, 0.35 * s);
    // Uncertainty ring
    ctx.strokeStyle = color;
    ctx.globalAlpha = 0.4;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.arc(x, y, Math.max(r, p.spread * s), 0, Math.PI * 2);
    ctx.stroke();
    ctx.globalAlpha = p.connected ? 1 : 0.4;

    // Facing wedge
    const h = (p.heading * Math.PI) / 180;
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.moveTo(x + Math.sin(h) * r * 2.2, y - Math.cos(h) * r * 2.2);
    ctx.lineTo(x + Math.sin(h + 2.4) * r, y - Math.cos(h + 2.4) * r);
    ctx.lineTo(x + Math.sin(h - 2.4) * r, y - Math.cos(h - 2.4) * r);
    ctx.closePath();
    ctx.fill();
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fill();
    if (p.id === this.highlightId) {
      ctx.strokeStyle = '#fff';
      ctx.lineWidth = 2;
      ctx.stroke();
    }

    ctx.fillStyle = '#fff';
    ctx.font = '12px system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(p.name, x, y - r - 6);
    ctx.globalAlpha = 1;
  }
}

export function connect(hello, onMessage, onStatus) {
  // WebSocket that reconnects and queues messages sent while offline.
  const queue = [];
  let ws = null;
  let delay = 500;
  const url = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`;

  function open() {
    ws = new WebSocket(url);
    ws.onopen = () => {
      delay = 500;
      ws.send(JSON.stringify(hello));
      while (queue.length) ws.send(queue.shift());
      onStatus && onStatus(true);
    };
    ws.onmessage = (e) => onMessage(JSON.parse(e.data));
    ws.onclose = () => {
      onStatus && onStatus(false);
      setTimeout(open, delay);
      delay = Math.min(delay * 2, 5000);
    };
  }
  open();

  return {
    // reliable=true queues the message if offline (steps); false drops it (heading pings).
    send(msg, reliable = true) {
      const text = JSON.stringify(msg);
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(text);
      else if (reliable) queue.push(text);
    },
  };
}
