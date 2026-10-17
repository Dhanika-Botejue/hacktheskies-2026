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
  return m.status;
}

export function renderMapList(state: MapsState) {
  const list = $<HTMLUListElement>("#map-list");
  if (!state.maps.length) {
    list.innerHTML = `<li class="empty">No maps yet. Scan one with the phone app.</li>`;
    return;
  }
  list.replaceChildren(
    ...state.maps.map((m) => {
      const li = document.createElement("li");
      li.className = m.id === state.activeId ? "active" : "";
      li.innerHTML = `
        <div class="title"><span class="name"></span><button class="delete" title="Delete map">✕</button></div>
        <div class="meta"><span class="chip ${m.status}">${statusText(m)}</span><span>${
          m.frames ? `${m.frames} photos · ` : ""
        }${new Date(m.createdAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span></div>
        ${m.status === "training" || m.status === "uploaded" ? `<div class="bar"><div style="width:${m.progress * 100}%"></div></div>` : ""}
        ${m.status === "failed" ? `<div class="meta" title="${escapeAttr(m.error ?? "")}">${escapeHtml((m.error ?? "").slice(0, 120))}</div>` : ""}`;
      li.querySelector(".name")!.textContent = m.name;
      li.onclick = () => fetch(`/api/maps/${m.id}/activate`, { method: "POST" });
      li.querySelector<HTMLButtonElement>(".delete")!.onclick = (e) => {
        e.stopPropagation();
        if (confirm(`Delete map "${m.name}"?`)) fetch(`/api/maps/${m.id}`, { method: "DELETE" });
      };
      return li;
    }),
  );
}

function escapeHtml(s: string) {
  return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);
}
const escapeAttr = escapeHtml;

export function subscribeMaps(onState: (state: MapsState) => void) {
  const source = new EventSource("/api/events");
  source.onmessage = (e) => onState(JSON.parse(e.data));
}

export async function setupConnectPanel() {
  const info = await fetch("/api/info").then((r) => r.json());
  $("#qr").innerHTML = info.qrSvg;
  $("#server-url").textContent = info.serverUrl;
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
  const input = $<HTMLInputElement>("#file-input");
  input.onchange = () => {
    if (input.files?.[0]) uploadSplat(input.files[0]);
    input.value = "";
  };
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

export function setHud(mode: string, pos: { x: number; y: number; z: number }, fps: number) {
  $("#hud-mode").textContent = mode.toUpperCase();
  $("#hud-pos").textContent = `x ${pos.x.toFixed(2)}  y ${pos.y.toFixed(2)}  z ${pos.z.toFixed(2)} m`;
  $("#hud-fps").textContent = `${fps.toFixed(0)} fps`;
}
