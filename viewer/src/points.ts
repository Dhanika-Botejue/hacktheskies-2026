// The game's objectives on the map (the host marks them; the game server keeps them as the map's
// "points"): each team's base, a ring in its colour where its downed players come back in, and the
// flag, a ring with a pole whose cloth and ring take the colour of the team that owns it. While
// another team is taking the flag, an arc in its colour fills the ring. Who owns what comes from
// the shooting server, which runs the match.
import * as THREE from "three";
import { type Objectives, TEAM_COLORS, TEAM_NAMES, type Team, clock } from "./combat.ts";
import type { GameLink, MapObjective } from "./game.ts";

const NEUTRAL = "#e8ecf4";

/** Where the host can put objectives: one base per team and the flag. */
export const SLOTS: { id: string; kind: MapObjective["kind"]; team?: Team; name: string; r: number }[] = [
  { id: "base-red", kind: "base", team: "red", name: "Red base", r: 2.5 },
  { id: "base-blue", kind: "base", team: "blue", name: "Blue base", r: 2.5 },
  { id: "flag", kind: "flag", name: "Flag", r: 3 },
];

/** The colour an objective shows: its team's for a base, the owner's for the flag. */
export function objectiveColor(point: MapObjective, objs: Objectives | null) {
  if (point.kind === "base") return point.team ? TEAM_COLORS[point.team] : NEUTRAL;
  const owner = objs?.flag?.owner;
  return owner ? TEAM_COLORS[owner] : NEUTRAL;
}

/** One line on the flag: who owns it, its clock (unless short), and who is taking it. */
export function flagStatus(objs: Objectives | null, short = false) {
  const f = objs?.flag;
  if (!objs || !f) return "";
  const owner = f.owner && (short ? `${TEAM_NAMES[f.owner]}'s` : `${TEAM_NAMES[f.owner]} ${clock(objs.held[f.owner])} / ${clock(objs.win_s)}`);
  const parts = [owner || "nobody's"];
  if (f.challenger) parts.push(`${TEAM_NAMES[f.challenger]} taking ${Math.round(f.progress * 100)}%`);
  else if (f.inside.red && f.inside.blue) parts.push("contested");
  return parts.join(" · ");
}

const POLE_HEIGHT = 2.4;
const poleGeom = new THREE.CylinderGeometry(0.04, 0.04, POLE_HEIGHT, 8).translate(0, POLE_HEIGHT / 2, 0);
const capGeom = new THREE.OctahedronGeometry(0.16).translate(0, POLE_HEIGHT + 0.1, 0);
const clothGeom = new THREE.PlaneGeometry(0.7, 0.45).translate(0.37, POLE_HEIGHT - 0.25, 0);

class ObjectiveMarker {
  readonly group = new THREE.Group();
  readonly label = document.createElement("div");
  private ring: THREE.Mesh;
  private disc: THREE.Mesh;
  private progress: THREE.Mesh;
  private cloth: THREE.Mesh;
  private mat = new THREE.MeshBasicMaterial({ transparent: true, opacity: 0.9, side: THREE.DoubleSide, depthTest: false });
  private fill = new THREE.MeshBasicMaterial({ transparent: true, opacity: 0.12, side: THREE.DoubleSide, depthWrite: false });
  private progressMat = new THREE.MeshBasicMaterial({ transparent: true, opacity: 0.9, side: THREE.DoubleSide, depthTest: false });
  private r = 0;
  private shownProgress = -1;
  world = { x: 0, z: 0 };

  constructor(labels: HTMLElement) {
    this.ring = new THREE.Mesh(undefined, this.mat);
    this.disc = new THREE.Mesh(undefined, this.fill);
    this.progress = new THREE.Mesh(undefined, this.progressMat);
    this.cloth = new THREE.Mesh(clothGeom, this.mat);
    this.ring.position.y = 0.03;
    this.disc.position.y = 0.02;
    this.progress.position.y = 0.04;
    this.ring.renderOrder = 997;
    this.progress.renderOrder = 998;
    const pole = new THREE.Mesh(poleGeom, this.mat);
    const cap = new THREE.Mesh(capGeom, this.mat);
    this.group.add(this.ring, this.disc, this.progress, pole, cap, this.cloth);
    this.label.className = "tag point";
    labels.appendChild(this.label);
  }

  update(point: MapObjective, world: { x: number; z: number }, objs: Objectives | null) {
    this.world = world;
    this.group.position.set(world.x, 0, world.z);
    if (point.r !== this.r) {
      this.r = point.r;
      this.shownProgress = -1;
      this.ring.geometry.dispose();
      this.disc.geometry.dispose();
      this.ring.geometry = new THREE.RingGeometry(Math.max(0.05, point.r - 0.08), point.r, 64).rotateX(-Math.PI / 2);
      this.disc.geometry = new THREE.CircleGeometry(point.r, 64).rotateX(-Math.PI / 2);
    }
    const color = objectiveColor(point, objs);
    this.mat.color.set(color);
    this.fill.color.set(color);
    this.cloth.visible = point.kind === "flag";
    this.label.style.setProperty("--c", color);

    // The flag being taken: an arc in the challenger's colour, filling clockwise from map "up".
    const flag = point.kind === "flag" ? objs?.flag : null;
    const progress = flag?.challenger ? flag.progress : 0;
    this.progress.visible = progress > 0;
    if (flag?.challenger) this.progressMat.color.set(TEAM_COLORS[flag.challenger]);
    if (progress > 0 && progress !== this.shownProgress) {
      this.shownProgress = progress;
      this.progress.geometry.dispose();
      this.progress.geometry = new THREE.RingGeometry(
        Math.max(0.05, point.r - 0.3), Math.max(0.1, point.r - 0.1), 64, 1, Math.PI / 2, -progress * Math.PI * 2,
      ).rotateX(-Math.PI / 2);
    }

    const status = point.kind === "flag" ? flagStatus(objs) : "";
    this.label.innerHTML = `<b></b>${status ? "<span></span>" : ""}`;
    this.label.querySelector("b")!.textContent = point.name;
    if (status) this.label.querySelector("span")!.textContent = status;
  }

  setFill(on: boolean) {
    this.disc.visible = on;
  }

  dispose() {
    this.group.removeFromParent();
    this.ring.geometry.dispose();
    this.disc.geometry.dispose();
    this.progress.geometry.dispose();
    this.mat.dispose();
    this.fill.dispose();
    this.progressMat.dispose();
    this.label.remove();
  }
}

export class ObjectiveLayer {
  readonly group = new THREE.Group();
  private markers = new Map<string, ObjectiveMarker>();
  private tmp = new THREE.Vector3();

  constructor(
    private game: GameLink,
    private labels: HTMLElement,
  ) {}

  get points(): MapObjective[] {
    return this.game.map?.points ?? [];
  }

  /** Sync markers with the game map's objectives and the match state. */
  update(objs: Objectives | null) {
    const seen = new Set<string>();
    for (const p of this.points) {
      seen.add(p.id);
      let m = this.markers.get(p.id);
      if (!m) {
        m = new ObjectiveMarker(this.labels);
        this.group.add(m.group);
        this.markers.set(p.id, m);
      }
      m.update(p, this.game.toWorld(p.x, p.y), objs);
    }
    for (const [id, m] of this.markers) {
      if (!seen.has(id)) {
        m.dispose();
        this.markers.delete(id);
      }
    }
  }

  /** The tinted floor disc only reads from above: splats don't hide what's behind them, so in
   * first person it would wash over the walls. */
  setOverview(on: boolean) {
    for (const m of this.markers.values()) m.setFill(on);
  }

  /** Pin each name above its pole on screen. */
  animate(camera: THREE.Camera, width: number, height: number) {
    for (const m of this.markers.values()) {
      this.tmp.set(m.world.x, POLE_HEIGHT + 0.45, m.world.z).project(camera);
      const off = this.tmp.z > 1 || Math.abs(this.tmp.x) > 1.2 || Math.abs(this.tmp.y) > 1.2;
      m.label.hidden = off;
      if (!off) {
        m.label.style.transform = `translate(-50%, -100%) translate(${((this.tmp.x + 1) / 2) * width}px, ${((1 - this.tmp.y) / 2) * height}px)`;
      }
    }
  }
}
