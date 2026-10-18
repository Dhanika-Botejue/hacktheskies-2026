// Shooter-style overlays on the 3D view: compass strip with player bearings, rotating minimap,
// hold-Tab scoreboard, event feed, the players panel used to spectate and place players, and
// the match and objectives (bases, flag) panels.
import {
  type CombatLink, type Fighter, type Objectives, TEAM_COLORS, TEAM_NAMES, type Team, clock, healthText,
} from "./combat.ts";
import { type GameLink, type GamePlayer, type MapObjective, mapOrigin } from "./game.ts";
import { type Pose, playerColor } from "./players.ts";
import { SLOTS, flagStatus, objectiveColor } from "./points.ts";

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
  fighter: Fighter | null;
}

const DOWN_COLOR = "#6b7280";
const blipColor = ({ player, fighter }: Blip) => (fighter?.alive === false ? DOWN_COLOR : playerColor(player));

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
  for (const blip of blips) {
    const { player, pose } = blip;
    const bearing = ((Math.atan2(pose.x - view.x, -(pose.z - view.z)) * 180) / Math.PI + 360) % 360;
    const x = xOf(bearing);
    if (x == null) continue;
    ctx.globalAlpha = player.connected ? 1 : 0.4;
    ctx.fillStyle = blipColor(blip);
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

  draw(view: Viewpoint, blips: Blip[], selfId: string | null, points: MapObjective[], objs: Objectives | null) {
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

    // Bases in their team's colour, the flag in its owner's; names upright.
    for (const point of points) {
      const color = objectiveColor(point, objs);
      const cx = point.x + o.x;
      const cz = point.y + o.z;
      ctx.beginPath();
      ctx.arc(cx, cz, point.r, 0, Math.PI * 2);
      ctx.globalAlpha = 0.18;
      ctx.fillStyle = color;
      ctx.fill();
      ctx.globalAlpha = 0.9;
      ctx.strokeStyle = color;
      ctx.lineWidth = 2 / s;
      ctx.stroke();
      ctx.save();
      ctx.translate(cx, cz);
      ctx.rotate(-view.yaw);
      ctx.scale(1 / s, 1 / s);
      ctx.fillStyle = color;
      ctx.font = "700 11px ui-sans-serif, system-ui";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(point.name, 0, 0);
      ctx.restore();
    }
    ctx.globalAlpha = 1;

    for (const blip of blips) {
      const { player, pose } = blip;
      if (player.id === selfId) continue;
      ctx.globalAlpha = player.connected ? 1 : 0.4;
      ctx.fillStyle = blipColor(blip);
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

const tagLabel = (p: GamePlayer) => (p.tag != null ? `#${p.tag}` : "no tag");

export function renderScoreboard(players: GamePlayer[], combat: CombatLink) {
  const order = { red: 0, blue: 1 } as Record<string, number>;
  const sorted = [...players].sort((a, b) => (order[a.team ?? ""] ?? 2) - (order[b.team ?? ""] ?? 2));
  const rows = sorted.map((p) => {
    const f = combat.fighter(p.tag);
    return `
    <tr class="${p.connected ? "" : "offline"}">
      <td><span class="swatch" style="background:${playerColor(p)}"></span>${escapeHtml(p.name)}</td>
      <td>${p.team ?? "–"} · ${tagLabel(p)}</td>
      <td class="hp">${healthText(f)}</td>
      <td>${f ? `${f.kills} / ${f.deaths}` : "–"}</td>
      <td>${p.connected ? "online" : "offline"}</td>
      <td>${escapeHtml(p.calibrated ? p.source : "not placed")}</td>
      <td>${p.calibrated ? `±${(p.spread ?? 0).toFixed(1)} m` : "–"}</td>
    </tr>`;
  }).join("");
  $("#scoreboard tbody").innerHTML = rows || `<tr><td colspan="7" class="empty">No players connected.</td></tr>`;
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
      const c = playerColor(p);
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
  respawn: (id: string) => void;
  remove: (id: string) => void;
}

function combatMeta(p: GamePlayer, combat: CombatLink) {
  if (p.tag == null || !p.team) return `<span class="warn">no tag/team: can't be shot</span>`;
  const f = combat.fighter(p.tag);
  if (!f) return `<span>${tagLabel(p)} · ${combat.connected ? "not on the shooting server" : "shooting server offline"}</span>`;
  return `<span>${tagLabel(p)} · <span class="hp">${healthText(f)}</span> · ${f.kills}K ${f.deaths}D${f.connected ? "" : " · camera offline"}</span>`;
}

let lastPanelHtml = "";
export function renderPlayersPanel(
  players: GamePlayer[], combat: CombatLink, selectedId: string | null, gameUp: boolean, actions: PlayerPanelActions,
) {
  const list = $("#player-list");
  const html = !gameUp
    ? `<li class="empty">Game server offline. Start it with <code>python run.py</code>.</li>`
    : !players.length
      ? `<li class="empty">No players yet. Join from the HTS Player app or <code>/web/phone.html</code>.</li>`
      : players.map((p, i) => `
        <li data-id="${escapeHtml(p.id)}" class="${p.id === selectedId ? "active" : ""}">
          <div class="title">
            <span class="name"><span class="dot ${p.connected ? "on" : ""}"></span><span class="swatch" style="background:${playerColor(p)}"></span>${i < 9 ? `<kbd>${i + 1}</kbd>` : ""}${escapeHtml(p.name)}</span>
            <span class="row">
              ${combat.fighter(p.tag)?.alive === false ? `<button data-act="respawn" title="Bring them back in now (e.g. their position is wrong and they can't reach their base)">Respawn</button>` : ""}
              <button data-act="spectate" title="Watch through their eyes (${i + 1})" ${p.calibrated ? "" : "disabled"}>Watch</button>
              <button data-act="place" title="Set where they stand (P)">Place</button>
              <button data-act="remove" class="delete" title="Forget this player">✕</button>
            </span>
          </div>
          <div class="meta">${combatMeta(p, combat)}</div>
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

// ---- Match and objectives --------------------------------------------------------------------

function flagClocks(held: Record<Team, number>, winS: number, owner: Team | null) {
  return (["red", "blue"] as Team[]).map((t) => `
    <div class="flag-clock${owner === t ? " owner" : ""}">
      <span style="color:${TEAM_COLORS[t]}">${owner === t ? "⚑ " : ""}${TEAM_NAMES[t]}</span>
      <div class="bar"><div style="width:${Math.min(100, (held[t] / winS) * 100)}%;background:${TEAM_COLORS[t]}"></div></div>
      <span class="time">${clock(held[t])}</span>
    </div>`).join("");
}

/** What's missing for the game to work, for the host to fix before (or during) the match. */
function setupWarnings(combat: CombatLink, players: GamePlayer[], points: MapObjective[]) {
  const warn: string[] = [];
  const objs = combat.objectives;
  if (!points.some((p) => p.kind === "flag")) warn.push("No flag on the map: nobody can win on flag time.");
  for (const t of ["red", "blue"] as Team[]) {
    if (!points.some((p) => p.kind === "base" && p.team === t)) {
      warn.push(`No ${TEAM_NAMES[t]} base: ${TEAM_NAMES[t]}'s downed players come back after 10 s.`);
    }
  }
  if (objs && !objs.tracking) warn.push("The shooting server can't reach the game server: it doesn't know where anyone is.");
  const untagged = players.filter((p) => p.tag == null || !p.team).length;
  if (untagged) warn.push(`${untagged} player${untagged === 1 ? " has" : "s have"} no tag/team and can't be shot.`);
  return warn;
}

export function renderMatchPanel(combat: CombatLink, players: GamePlayer[], points: MapObjective[]) {
  const s = combat.scores;
  const objs = combat.objectives;
  $("#match-clock").textContent = combat.active ? clock(combat.elapsed) : "";
  // The flag clocks while there is a flag (and after a flag match); kills underneath.
  const flagMatch = !!objs?.flag || !!s?.flag;
  const held = combat.active || !s ? objs?.held : { red: s.red_flag_s, blue: s.blue_flag_s };
  const kills = s ? `<div class="kills"><span style="color:${TEAM_COLORS.red}">Red ${s.red_kills}</span> – <span style="color:${TEAM_COLORS.blue}">${s.blue_kills} Blue</span> kills</div>` : "";
  const result = !combat.active && s?.reason
    ? `<div class="result">${s.winner === "tie" ? "It's a tie" : `${TEAM_NAMES[s.winner]} wins`}${s.reason === "flag" ? " on the flag" : ""}</div>`
    : "";
  const html = result + (flagMatch && held ? flagClocks(held, (combat.active ? objs?.win_s : s?.win_s) ?? 120, objs?.flag?.owner ?? null) : "") + kills;
  if ($("#match-score").innerHTML !== html) $("#match-score").innerHTML = html;

  $<HTMLButtonElement>("#match-start").disabled = !combat.connected || combat.active;
  $<HTMLButtonElement>("#match-stop").disabled = !combat.connected || !combat.active;
  $<HTMLInputElement>("#match-win").disabled = combat.active;
  const warn = combat.connected ? setupWarnings(combat, players, points) : [];
  const note = !combat.connected
    ? "Shooting server offline. run.py starts it (needs pip install -r requirements.txt)."
    : !combat.active
      ? `Not running. ${combat.fighters.size} phone${combat.fighters.size === 1 ? "" : "s"} streaming to the shooting server.`
      : objs?.flag
        ? `Live. Hold the flag ${clock(objs.win_s)} in total to win. Downed players walk back to their base.`
        : "Live. Downed players walk back to their base.";
  const html2 = `${note}${warn.map((w) => `<br><span class="warn">${w}</span>`).join("")}`;
  if ($("#match-note").innerHTML !== html2) $("#match-note").innerHTML = html2;
}

export interface ObjectivePanelActions {
  place: (id: string) => void;
  resize: (id: string, delta: number) => void;
  remove: (id: string) => void;
}

let lastPointsHtml = "";
export function renderObjectivesPanel(points: MapObjective[], objs: Objectives | null, placing: string | null, actions: ObjectivePanelActions) {
  const list = $("#point-list");
  const html = SLOTS.map((slot) => {
    const point = points.find((p) => p.id === slot.id);
    const color = point ? objectiveColor(point, objs) : slot.team ? TEAM_COLORS[slot.team] : "#e8ecf4";
    const status = !point ? "not placed" : point.kind === "flag" ? flagStatus(objs, true) || "nobody's" : "";
    return `
      <li data-id="${slot.id}" class="${point ? "" : "unplaced"}">
        <span class="swatch" style="background:${color}"></span>
        <span class="name">${slot.name}</span>
        <span class="holder">${escapeHtml(status)}</span>
        <span class="row">
          <button data-act="place" class="${placing === slot.id ? "active" : ""}" title="Click the top-down map where it goes">${point ? "Move" : "Place"}</button>
          ${point ? `
          <button data-act="shrink" title="Smaller radius">−</button>
          <span class="radius">${point.r.toFixed(1)} m</span>
          <button data-act="grow" title="Larger radius">+</button>
          <button data-act="remove" class="delete" title="Remove">✕</button>` : ""}
        </span>
      </li>`;
  }).join("");
  if (html === lastPointsHtml) return;
  lastPointsHtml = html;
  list.innerHTML = html;
  list.querySelectorAll<HTMLButtonElement>("button[data-act]").forEach((btn) => {
    const id = btn.closest<HTMLElement>("[data-id]")!.dataset.id!;
    btn.onclick = (e) => {
      e.stopPropagation();
      const act = btn.dataset.act;
      if (act === "place") actions.place(id);
      else if (act === "remove") actions.remove(id);
      else actions.resize(id, act === "grow" ? 0.5 : -0.5);
    };
  });
}
