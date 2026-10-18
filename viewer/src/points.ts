// Key positions on the map (the host marks them; teams will claim them): a ring on the floor at
// the capture radius, a pole and a floating name. The colour says who is standing in it right
// now: a team's colour, yellow when both teams are there, white when nobody is. Claiming over
// time isn't scored yet; this is what it will be built on.
import * as THREE from "three";
import { TEAM_COLORS, type Fighter, type Team } from "./combat.ts";
import type { GameLink, GamePlayer, KeyPoint } from "./game.ts";
import type { Pose } from "./players.ts";

export type Holder = Team | "contested" | null;

export const HOLDER_COLORS: Record<string, string> = { ...TEAM_COLORS, contested: "#ffd34d", none: "#e8ecf4" };
export const holderColor = (h: Holder) => HOLDER_COLORS[h ?? "none"];

interface Blip {
  player: GamePlayer;
  pose: Pose;
  fighter: Fighter | null;
}

/** Who is inside a key position: teams of players there who are in the game and not down. */
export function holderOf(point: KeyPoint, world: { x: number; z: number }, blips: Blip[]): Holder {
  const teams = new Set<Team>();
  for (const { player, pose, fighter } of blips) {
    if (!player.team || !player.connected || fighter?.alive === false) continue;
    if (Math.hypot(pose.x - world.x, pose.z - world.z) <= point.r) teams.add(player.team);
  }
  if (teams.size === 2) return "contested";
  return teams.size ? [...teams][0] : null;
}

const POLE_HEIGHT = 2.4;
const poleGeom = new THREE.CylinderGeometry(0.04, 0.04, POLE_HEIGHT, 8).translate(0, POLE_HEIGHT / 2, 0);
const capGeom = new THREE.OctahedronGeometry(0.16).translate(0, POLE_HEIGHT + 0.1, 0);

class PointMarker {
  readonly group = new THREE.Group();
  readonly label = document.createElement("div");
  private ring: THREE.Mesh;
  private disc: THREE.Mesh;
  private mat = new THREE.MeshBasicMaterial({ transparent: true, opacity: 0.9, side: THREE.DoubleSide, depthTest: false });
  private fill = new THREE.MeshBasicMaterial({ transparent: true, opacity: 0.12, side: THREE.DoubleSide, depthWrite: false });
  private r = 0;
  holder: Holder = null;
  world = { x: 0, z: 0 };

  constructor(labels: HTMLElement) {
    this.ring = new THREE.Mesh(undefined, this.mat);
    this.disc = new THREE.Mesh(undefined, this.fill);
    this.ring.position.y = 0.03;
    this.disc.position.y = 0.02;
    this.ring.renderOrder = 997;
    const pole = new THREE.Mesh(poleGeom, this.mat);
    const cap = new THREE.Mesh(capGeom, this.mat);
    this.group.add(this.ring, this.disc, pole, cap);
    this.label.className = "tag point";
    labels.appendChild(this.label);
  }

  update(point: KeyPoint, world: { x: number; z: number }, holder: Holder) {
    this.world = world;
    this.group.position.set(world.x, 0, world.z);
    if (point.r !== this.r) {
      this.r = point.r;
      this.ring.geometry.dispose();
      this.disc.geometry.dispose();
      this.ring.geometry = new THREE.RingGeometry(Math.max(0.05, point.r - 0.08), point.r, 64).rotateX(-Math.PI / 2);
      this.disc.geometry = new THREE.CircleGeometry(point.r, 64).rotateX(-Math.PI / 2);
    }
    this.holder = holder;
    const color = holderColor(holder);
    this.mat.color.set(color);
    this.fill.color.set(color);
    this.label.style.setProperty("--c", color);
    const status = holder === "contested" ? "contested" : holder ? `${holder} holds` : "";
    this.label.innerHTML = `<b></b>${status ? `<span>${status}</span>` : ""}`;
    this.label.querySelector("b")!.textContent = point.name;
  }

  setFill(on: boolean) {
    this.disc.visible = on;
  }

  dispose() {
    this.group.removeFromParent();
    this.ring.geometry.dispose();
    this.disc.geometry.dispose();
    this.mat.dispose();
    this.fill.dispose();
    this.label.remove();
  }
}

export class KeyPointLayer {
  readonly group = new THREE.Group();
  private markers = new Map<string, PointMarker>();
  private tmp = new THREE.Vector3();

  constructor(
    private game: GameLink,
    private labels: HTMLElement,
  ) {}

  get points(): KeyPoint[] {
    return this.game.map?.points ?? [];
  }

  /** Sync markers with the game map's key positions and who is standing in each. */
  update(blips: Blip[]) {
    const seen = new Set<string>();
    for (const p of this.points) {
      seen.add(p.id);
      let m = this.markers.get(p.id);
      if (!m) {
        m = new PointMarker(this.labels);
        this.group.add(m.group);
        this.markers.set(p.id, m);
      }
      const world = this.game.toWorld(p.x, p.y);
      m.update(p, world, holderOf(p, world, blips));
    }
    for (const [id, m] of this.markers) {
      if (!seen.has(id)) {
        m.dispose();
        this.markers.delete(id);
      }
    }
  }

  holders() {
    return this.points.map((p) => ({ point: p, holder: this.markers.get(p.id)?.holder ?? null }));
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
