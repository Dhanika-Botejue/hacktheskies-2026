// Top-down drawing of a walked path, used by the trail test pages.
// Coordinates are meters, x right, y down ("up" = direction faced at start).
// Headings are degrees clockwise from up.

export class TrailView {
  constructor(canvas) {
    this.canvas = canvas;
    this.cam = { x: 0, y: 0, scale: 30 };
    this.follow = true;
    this.background = '#14161a';
  }

  draw(path, pos, heading) {
    const c = this.canvas, dpr = devicePixelRatio || 1, r = c.getBoundingClientRect();
    const w = r.width, h = r.height;
    if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) {
      c.width = Math.round(w * dpr);
      c.height = Math.round(h * dpr);
    }
    const ctx = c.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = this.background;
    ctx.fillRect(0, 0, w, h);

    // Follow the walker, or fit the whole path (at least 8 m across).
    const xs = path.map((p) => p.x), ys = path.map((p) => p.y);
    const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
    const span = Math.max(8, maxX - minX + 2, (maxY - minY + 2) * (w / h));
    const target = this.follow
      ? { x: pos.x, y: pos.y, scale: Math.min(w, h) / 12 }
      : { x: (minX + maxX) / 2, y: (minY + maxY) / 2, scale: w / span };
    const cam = this.cam;
    cam.x += (target.x - cam.x) * 0.15;
    cam.y += (target.y - cam.y) * 0.15;
    cam.scale += (target.scale - cam.scale) * 0.15;
    const S = (p) => ({ x: w / 2 + (p.x - cam.x) * cam.scale, y: h / 2 + (p.y - cam.y) * cam.scale });

    // 1 m grid
    ctx.strokeStyle = 'rgba(255,255,255,0.08)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    const x0 = Math.floor(cam.x - w / 2 / cam.scale), x1 = Math.ceil(cam.x + w / 2 / cam.scale);
    const y0 = Math.floor(cam.y - h / 2 / cam.scale), y1 = Math.ceil(cam.y + h / 2 / cam.scale);
    for (let x = x0; x <= x1; x++) { const a = S({ x, y: 0 }).x; ctx.moveTo(a, 0); ctx.lineTo(a, h); }
    for (let y = y0; y <= y1; y++) { const b = S({ x: 0, y }).y; ctx.moveTo(0, b); ctx.lineTo(w, b); }
    ctx.stroke();

    // Path
    ctx.strokeStyle = '#29b6f6';
    ctx.lineWidth = 3;
    ctx.lineJoin = ctx.lineCap = 'round';
    ctx.beginPath();
    path.forEach((p, i) => { const s = S(p); i ? ctx.lineTo(s.x, s.y) : ctx.moveTo(s.x, s.y); });
    if (path.length) { const s = S(pos); ctx.lineTo(s.x, s.y); }
    ctx.stroke();

    // Start marker
    if (path.length) {
      const st = S(path[0]);
      ctx.fillStyle = '#76b041';
      ctx.beginPath(); ctx.arc(st.x, st.y, 6, 0, Math.PI * 2); ctx.fill();
    }

    // You + facing direction
    if (heading != null) {
      const me = S(pos), a = (heading * Math.PI) / 180, rad = 9;
      ctx.fillStyle = '#ffc914';
      ctx.beginPath();
      ctx.moveTo(me.x + Math.sin(a) * rad * 2.4, me.y - Math.cos(a) * rad * 2.4);
      ctx.lineTo(me.x + Math.sin(a + 2.4) * rad, me.y - Math.cos(a + 2.4) * rad);
      ctx.lineTo(me.x + Math.sin(a - 2.4) * rad, me.y - Math.cos(a - 2.4) * rad);
      ctx.closePath(); ctx.fill();
      ctx.beginPath(); ctx.arc(me.x, me.y, rad, 0, Math.PI * 2); ctx.fill();
    }

    // Scale bar
    ctx.fillStyle = '#8a919c';
    ctx.font = '11px system-ui, sans-serif';
    ctx.fillRect(12, h - 14, cam.scale, 2);
    ctx.fillText('1 m', 12, h - 20);
  }
}
