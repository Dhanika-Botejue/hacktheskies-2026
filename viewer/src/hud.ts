// Shooter-style overlays on the 3D view: compass strip with player bearings, rotating minimap,
// hold-Tab scoreboard, event feed, and the players panel used to spectate and place players.
import { type GameLink, type GamePlayer, colorFor, mapOrigin } from "./game.ts";
import type { Pose } from "./players.ts";

const $ = <T extends HTMLElement>(sel: string) => document.querySelector(sel) as T;

function escapeHtml(s: string) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

function sizeCanvas(canvas: HTMLCanvasElement) {
  const dpr = Math.min(window.devicePixelRatio, 2);
  const w = Math.round(canvas.clientWidth * dpr);
  const h = Math.round(canvas.clientHeight * dpr);
  if (canvas.width !== w || canvas.height !== h) Object.assign(canvas, { width: w, height: h });
  const ctx = canvas.getContext("2d")!;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w: canvas.clientWidth, h: canvas.clientHeight };
}

export interface Viewpoint {
  x: number;
  z: number;
  yaw: number; // rad, 0 = facing -Z (map "up")
}

interface Blip {
  player: GamePlayer;
  pose: Pose;
}

// ---- Compass ----------------------------------------------------------------------------------

const COMPASS_FOV = 180; // degrees shown across the strip
const CARDINALS: Record<number, string> = { 0: "N", 45: "NE", 90: "E", 135: "SE", 180: "S", 225: "SW", 270: "W", 315: "NW" };

/** Map "up" is labelled N; bearings are map headings, clockwise. */
export function drawCompass(view: Viewpoint, blips: Blip[]) {
  const { ctx, w, h } = sizeCanvas($<HTMLCanvasElement>("#compass"));
  ctx.clearRect(0, 0, w, h);
  const heading = (((-view.yaw * 180) / Math.PI) % 360 + 360) % 360;
  const pxPerDeg = w / COMPASS_FOV;
  const xOf = (bearing: number) => {
    const d = ((bearing - heading + 540) % 360) - 180;
    return Math.abs(d) > COMPASS_FOV / 2 ? null : w / 2 + d * pxPerDeg;
  };
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  for (let b = 0; b < 360; b += 15) {
    const x = xOf(b);
    if (x == null) continue;
    const fade = 1 - Math.abs(x - w / 2) / (w / 2);
    ctx.globalAlpha = 0.25 + 0.75 * fade;
    ctx.fillStyle = "#fff";
    const label = CARDINALS[b];
    ctx.fillRect(x - 0.5, 0, 1, label ? 8 : 5);
    if (label) {
      ctx.font = `${label.length === 1 ? 700 : 500} 12px ui-sans-serif, system-ui`;
      ctx.fillText(label, x, 10);
    } else if (b % 45) {
      ctx.font = "10px ui-sans-serif, system-ui";
      ctx.fillStyle = "rgba(255,255,255,0.6)";
      ctx.fillText(String(b), x, 9);
    }
  }
  // Where each player is, relative to where we look.
  for (const { player, pose } of blips) {
    const bearing = ((Math.atan2(pose.x - view.x, -(pose.z - view.z)) * 180) / Math.PI + 360) % 360;
    const x = xOf(bearing);
    if (x == null) continue;
    ctx.globalAlpha = player.connected ? 1 : 0.4;
    ctx.fillStyle = colorFor(player.id);
    ctx.beginPath();
    ctx.moveTo(x, h - 2);
    ctx.lineTo(x - 5, h - 10);
    ctx.lineTo(x + 5, h - 10);
    ctx.fill();
  }
  ctx.globalAlpha = 1;
  ctx.fillStyle = "#ffd34d";
  ctx.fillRect(w / 2 - 1, 0, 2, 12);
  $("#compass-deg").textContent = `${Math.round(heading) % 360}°`;
}

// ---- Minimap ----------------------------------------------------------------------------------

export class Minimap {
  radius = 7; // metres from the centre to the edge
  private image: HTMLImageElement | null = null;
  private imageUrl: string | null = null;

  constructor(private game: GameLink) {
    window.addEventListener("keydown", (e) => {
      if (e.target instanceof HTMLInputElement) return;
      if (e.key === "-" || e.key === "_") this.radius = Math.min(60, this.radius * 1.25);
      if (e.key === "=" || e.key === "+") this.radius = Math.max(2, this.radius / 1.25);
    });
  }

  private syncImage() {
    const url = this.game.imageUrl();
    if (url === this.imageUrl) return;
    this.imageUrl = url;
    this.image = null;
    if (!url) return;
    const img = new Image();
    img.onload = () => {
      if (this.imageUrl === url) this.image = img;
    };
    img.src = url;
  }

  draw(view: Viewpoint, blips: Blip[], selfId: string | null) {
    this.syncImage();
    const { ctx, w, h } = sizeCanvas($<HTMLCanvasElement>("#minimap"));
    const r = Math.min(w, h) / 2;
    const s = r / this.radius; // px per metre
    ctx.clearRect(0, 0, w, h);
    ctx.save();
    ctx.beginPath();
    ctx.arc(w / 2, h / 2, r - 1, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(10, 12, 18, 0.78)";
    ctx.fill();
    ctx.clip();

    // World (X right, Z down) rotated so the way we face points up.
    ctx.translate(w / 2, h / 2);
    ctx.rotate(view.yaw);
    ctx.scale(s, s);
    ctx.translate(-view.x, -view.z);

    const map = this.game.map;
    const o = mapOrigin(map);
    if (map) {
      if (this.image) {
        ctx.globalAlpha = 0.9;
        ctx.drawImage(this.image, o.x, o.z, map.width, map.height);
        ctx.globalAlpha = 1;
      } else {
        ctx.fillStyle = "rgba(255,255,255,0.04)";
        ctx.fillRect(o.x, o.z, map.width, map.height);
      }
      ctx.strokeStyle = "rgba(232, 234, 237, 0.9)";
      ctx.lineWidth = 2 / s;
      ctx.lineCap = "round";
      ctx.beginPath();
      for (const [x1, y1, x2, y2] of map.walls) {
        ctx.moveTo(x1 + o.x, y1 + o.z);
        ctx.lineTo(x2 + o.x, y2 + o.z);
      }
      ctx.stroke();
    }

    for (const { player, pose } of blips) {
      if (player.id === selfId) continue;
      ctx.globalAlpha = player.connected ? 1 : 0.4;
      ctx.fillStyle = colorFor(player.id);
      ctx.save();
      ctx.translate(pose.x, pose.z);
      ctx.rotate(-pose.yaw);
      const k = 1 / s;
      ctx.beginPath();
      ctx.moveTo(0, -11 * k);
      ctx.lineTo(6 * k, 5 * k);
      ctx.lineTo(0, 2 * k);
      ctx.lineTo(-6 * k, 5 * k);
      ctx.closePath();
      ctx.fill();
      ctx.restore();
    }
    ctx.globalAlpha = 1;
    ctx.restore();

    // Us, always in the middle facing up, with a view cone.
    ctx.save();
    ctx.translate(w / 2, h / 2);
    const cone = ctx.createRadialGradient(0, 0, 0, 0, 0, r * 0.6);
    cone.addColorStop(0, "rgba(255, 211, 77, 0.35)");
    cone.addColorStop(1, "rgba(255, 211, 77, 0)");
    ctx.fillStyle = cone;
    ctx.beginPath();
    ctx.moveTo(0, 0);
    ctx.arc(0, 0, r * 0.6, -Math.PI / 2 - 0.65, -Math.PI / 2 + 0.65);
    ctx.fill();
    ctx.fillStyle = "#ffd34d";
    ctx.beginPath();
    ctx.moveTo(0, -8);
    ctx.lineTo(6, 6);
    ctx.lineTo(0, 3);
    ctx.lineTo(-6, 6);
    ctx.closePath();
    ctx.fill();
    ctx.restore();

    ctx.strokeStyle = "rgba(255,255,255,0.25)";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.arc(w / 2, h / 2, r - 1, 0, Math.PI * 2);
    ctx.stroke();
    // Map "up" marker on the rim.
    ctx.fillStyle = "#fff";
    ctx.font = "700 11px ui-sans-serif, system-ui";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText("N", w / 2 + Math.sin(view.yaw) * (r - 10), h / 2 - Math.cos(view.yaw) * (r - 10));
    $("#minimap-scale").textContent = `${Math.round(this.radius)} m`;
  }
}

// ---- Scoreboard, feed and players panel -------------------------------------------------------

export function renderScoreboard(players: GamePlayer[]) {
  const rows = players.map((p) => `
    <tr class="${p.connected ? "" : "offline"}">
      <td><span class="swatch" style="background:${colorFor(p.id)}"></span>${escapeHtml(p.name)}</td>
      <td>${p.connected ? "online" : "offline"}</td>
      <td>${escapeHtml(p.platform)}</td>
      <td>${escapeHtml(p.calibrated ? p.source : "not placed")}</td>
      <td>${p.calibrated ? `±${(p.spread ?? 0).toFixed(1)} m` : "–"}</td>
      <td>${p.steps}</td>
    </tr>`).join("");
  $("#scoreboard tbody").innerHTML = rows || `<tr><td colspan="6" class="empty">No players connected.</td></tr>`;
}

export class Feed {
  private known = new Map<string, { connected: boolean; calibrated: boolean; source: string }>();

  push(text: string, color = "#fff") {
    const el = document.createElement("div");
    el.className = "feed-item";
    el.innerHTML = `<span class="swatch" style="background:${color}"></span>`;
    el.append(text);
    const feed = $("#feed");
    feed.prepend(el);
    while (feed.children.length > 5) feed.lastElementChild!.remove();
    setTimeout(() => el.classList.add("fade"), 5000);
    setTimeout(() => el.remove(), 6000);
  }

  /** Announce joins, leaves, placements and tracking hand-overs. */
  diff(players: GamePlayer[]) {
    const seen = new Set<string>();
    for (const p of players) {
      seen.add(p.id);
      const prev = this.known.get(p.id);
      const c = colorFor(p.id);
      if (!prev) this.push(`${p.name} joined`, c);
      else {
        if (prev.connected && !p.connected) this.push(`${p.name} disconnected`, c);
        if (!prev.connected && p.connected) this.push(`${p.name} reconnected`, c);
        if (!prev.calibrated && p.calibrated) this.push(`${p.name} placed on the map`, c);
        const lost = (s: string) => s.includes("lost") || s.includes("disagrees");
        if (p.calibrated && !lost(prev.source) && lost(p.source)) this.push(`${p.name}: ${p.source}`, c);
      }
      this.known.set(p.id, { connected: p.connected, calibrated: p.calibrated, source: p.source });
    }
    for (const [id] of this.known) if (!seen.has(id)) this.known.delete(id);
  }
}

export interface PlayerPanelActions {
  spectate: (id: string) => void;
  place: (id: string) => void;
  remove: (id: string) => void;
}

let lastPanelHtml = "";
export function renderPlayersPanel(players: GamePlayer[], selectedId: string | null, gameUp: boolean, actions: PlayerPanelActions) {
  const list = $("#player-list");
  const html = !gameUp
    ? `<li class="empty">Game server offline. Start it with <code>python run.py</code>.</li>`
    : !players.length
      ? `<li class="empty">No players yet. Join from the HTS Player app or <code>/web/phone.html</code>.</li>`
      : players.map((p, i) => `
        <li data-id="${p.id}" class="${p.id === selectedId ? "active" : ""}">
          <div class="title">
            <span class="name"><span class="dot ${p.connected ? "on" : ""}"></span><span class="swatch" style="background:${colorFor(p.id)}"></span>${i < 9 ? `<kbd>${i + 1}</kbd>` : ""}${escapeHtml(p.name)}</span>
            <span class="row">
              <button data-act="spectate" title="Watch through their eyes (${i + 1})" ${p.calibrated ? "" : "disabled"}>Watch</button>
              <button data-act="place" title="Set where they stand (P)">Place</button>
              <button data-act="remove" class="delete" title="Forget this player">✕</button>
            </span>
          </div>
          <div class="meta"><span>${escapeHtml(p.platform)} · ${escapeHtml(p.calibrated ? p.source : "not placed yet")}</span><span>${p.calibrated ? `±${(p.spread ?? 0).toFixed(1)} m` : ""}</span></div>
        </li>`).join("");
  if (html === lastPanelHtml) return;
  lastPanelHtml = html;
  list.innerHTML = html;
  list.querySelectorAll<HTMLButtonElement>("button[data-act]").forEach((btn) => {
    const id = btn.closest<HTMLElement>("[data-id]")!.dataset.id!;
    btn.onclick = (e) => {
      e.stopPropagation();
      actions[btn.dataset.act as keyof PlayerPanelActions](id);
    };
  });
}
