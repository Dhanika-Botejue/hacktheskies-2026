// DOM side of the viewer: map list, connect QR code, uploads, HUD.
import type { MapInfo } from "../server/maps.ts";

const $ = <T extends HTMLElement>(sel: string) => document.querySelector(sel) as T;

export interface MapsState {
  maps: MapInfo[];
  activeId: string | null;
}

function statusText(m: MapInfo) {
  if (m.status === "training") return `training ${Math.round(m.progress * 100)}%`;
  if (m.status === "uploaded") return "queued";
  if (m.parts) return `merged · ${m.parts.length} scans`;
  return m.status;
}

// Merge mode: clicking maps ticks them instead of opening them.
let selecting: Set<string> | null = null;
let lastState: MapsState = { maps: [], activeId: null };

const mergeable = (m: MapInfo) => m.status === "ready" && !!m.splatFile;

export function renderMapList(state: MapsState) {
  lastState = state;
  const list = $<HTMLUListElement>("#map-list");
  $("#merge-bar").hidden = !selecting;
  $<HTMLButtonElement>("#merge-toggle").classList.toggle("active", !!selecting);
  if (selecting) {
    const n = selecting.size;
    $<HTMLButtonElement>("#merge-go").disabled = n < 2;
    $<HTMLButtonElement>("#merge-go").textContent = n < 2 ? "Tick 2 or more maps" : `Merge ${n} maps`;
  }
  if (!state.maps.length) {
    list.innerHTML = `<li class="empty">No maps yet. Scan one with the phone app.</li>`;
    return;
  }
  list.replaceChildren(
    ...state.maps.map((m) => {
      const li = document.createElement("li");
      const ticked = !!selecting?.has(m.id);
      li.className = [m.id === state.activeId && !selecting ? "active" : "", ticked ? "ticked" : "", selecting && !mergeable(m) ? "disabled" : ""].join(" ");
      li.innerHTML = `
        <div class="title">${selecting ? `<input type="checkbox" ${ticked ? "checked" : ""} ${mergeable(m) ? "" : "disabled"}>` : ""}<span class="name"></span><button class="delete" title="Delete map">✕</button></div>
        <div class="meta"><span class="chip ${m.status}">${statusText(m)}</span><span>${
          m.frames ? `${m.frames} photos · ` : ""
        }${new Date(m.createdAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span></div>
        ${m.status === "training" || m.status === "uploaded" ? `<div class="bar"><div style="width:${m.progress * 100}%"></div></div>` : ""}
        ${m.status === "failed" ? `<div class="meta" title="${escapeAttr(m.error ?? "")}">${escapeHtml((m.error ?? "").slice(0, 120))}</div>` : ""}`;
      li.querySelector(".name")!.textContent = m.name;
      li.onclick = () => {
        if (!selecting) return void fetch(`/api/maps/${m.id}/activate`, { method: "POST" });
        if (!mergeable(m)) return;
        if (selecting.has(m.id)) selecting.delete(m.id);
        else selecting.add(m.id);
        renderMapList(lastState);
      };
      li.querySelector<HTMLButtonElement>(".delete")!.onclick = async (e) => {
        e.stopPropagation();
        if (!confirm(`Delete map "${m.name}"?`)) return;
        const res = await fetch(`/api/maps/${m.id}`, { method: "DELETE" });
        if (!res.ok) alert((await res.json()).error ?? "Couldn't delete the map");
      };
      return li;
    }),
  );
}

/** The Merge button and bar of the Maps panel. `onMerge` gets the ticked maps, in list order. */
export function setupMerge(onMerge: (maps: MapInfo[]) => void) {
  $<HTMLButtonElement>("#merge-toggle").onclick = () => {
    selecting = selecting ? null : new Set();
    renderMapList(lastState);
  };
  $<HTMLButtonElement>("#merge-cancel").onclick = () => {
    selecting = null;
    renderMapList(lastState);
  };
  $<HTMLButtonElement>("#merge-go").onclick = () => {
    if (!selecting || selecting.size < 2) return;
    const picked = lastState.maps.filter((m) => selecting!.has(m.id));
    selecting = null;
    renderMapList(lastState);
    onMerge(picked);
  };
}

export function escapeHtml(s: string) {
  return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);
}
const escapeAttr = escapeHtml;

export function subscribeMaps(onState: (state: MapsState) => void) {
  const source = new EventSource("/api/events");
  source.onmessage = (e) => onState(JSON.parse(e.data));
}

// The servers run.py starts, on this laptop's network address.
const GAME_PORT = 8443;
const SCAN_STATION_PORT = 8766;

export async function setupConnectPanel() {
  const info = await fetch("/api/info").then((r) => r.json());
  $("#qr").innerHTML = info.qrSvg;
  $("#server-url").textContent = info.serverUrl;
  const host = new URL(info.serverUrl).hostname;
  $("#join-address").textContent = `${host}:${GAME_PORT}`;
  $<HTMLAnchorElement>("#scan-station").href = `https://${host}:${SCAN_STATION_PORT}/scan`;
  const panel = $("#connect-panel");
  const toggle = $<HTMLButtonElement>("#connect-toggle");
  const apply = (collapsed: boolean) => {
    panel.classList.toggle("collapsed", collapsed);
    toggle.textContent = collapsed ? "Show" : "Hide";
    try {
      localStorage.setItem("connectCollapsed", collapsed ? "1" : "");
    } catch {}
  };
  let collapsed = false;
  try {
    collapsed = localStorage.getItem("connectCollapsed") === "1";
  } catch {}
  apply(collapsed);
  toggle.onclick = () => apply(!panel.classList.contains("collapsed"));
}

async function uploadSplat(file: File) {
  const ext = file.name.split(".").pop()?.toLowerCase();
  if (!["ply", "spz", "splat", "ksplat", "sog"].includes(ext ?? "")) {
    alert("Drop a .ply, .spz, .splat, .ksplat or .sog file");
    return;
  }
  setStatus(`Uploading ${file.name}…`);
  const res = await fetch(`/api/maps?filename=${encodeURIComponent(file.name)}`, { method: "POST", body: file });
  setStatus(null);
  if (!res.ok) alert(`Upload failed: ${(await res.json()).error}`);
}

export function setupUploads() {
  document.querySelectorAll<HTMLInputElement>("input.map-file").forEach((input) => {
    input.onchange = () => {
      if (input.files?.[0]) uploadSplat(input.files[0]);
      input.value = "";
    };
  });
  const hint = $("#drop-hint");
  let depth = 0;
  window.addEventListener("dragenter", (e) => {
    e.preventDefault();
    depth++;
    hint.hidden = false;
  });
  window.addEventListener("dragleave", () => {
    if (--depth <= 0) hint.hidden = true;
  });
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => {
    e.preventDefault();
    depth = 0;
    hint.hidden = true;
    const file = e.dataTransfer?.files[0];
    if (file) uploadSplat(file);
  });
}

export function setStatus(html: string | null, corner = false) {
  const el = $("#status-overlay");
  el.hidden = !html;
  el.classList.toggle("corner", corner);
  if (html) el.innerHTML = html;
}

export function setHud(mode: string, pos: { x: number; y: number; z: number }, fps: number, game: string) {
  $("#hud-mode").textContent = mode.toUpperCase();
  $("#hud-pos").textContent = `x ${pos.x.toFixed(2)}  y ${pos.y.toFixed(2)}  z ${pos.z.toFixed(2)} m`;
  $("#hud-fps").textContent = `${fps.toFixed(0)} fps`;
  $("#hud-game").textContent = game;
}

/** One line of the Get ready checklist: done, still to do, or a heads-up. */
export interface ReadyItem {
  state: "done" | "todo" | "warn";
  label: string;
  detail?: string;
}

let lastReadyHtml = "";
export function renderReadyList(items: ReadyItem[]) {
  const html = items
    .map((it) => `<li class="${it.state}"><span class="mark">${it.state === "done" ? "✓" : it.state === "warn" ? "!" : "○"}</span>
      <span><b>${escapeHtml(it.label)}</b>${it.detail ? `<small>${escapeHtml(it.detail)}</small>` : ""}</span></li>`)
    .join("");
  if (html === lastReadyHtml) return;
  lastReadyHtml = html;
  $("#ready-list").innerHTML = html;
}

/** Click a side panel's title to fold it away; remembered per panel. */
export function setupFoldingPanels() {
  document.querySelectorAll<HTMLElement>("#left .panel").forEach((panel) => {
    const title = panel.querySelector<HTMLElement>("header h1");
    if (!title || !panel.id) return;
    const key = `folded:${panel.id}`;
    const apply = (folded: boolean) => {
      panel.classList.toggle("folded", folded);
      try {
        localStorage.setItem(key, folded ? "1" : "");
      } catch {}
    };
    try {
      apply(localStorage.getItem(key) === "1");
    } catch {}
    title.classList.add("fold-title");
    title.title = "Click to fold or unfold";
    title.onclick = () => apply(!panel.classList.contains("folded"));
  });
}
