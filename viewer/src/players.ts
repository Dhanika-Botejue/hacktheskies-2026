// Live players in the 3D map: an avatar per placed player with a facing visor, an uncertainty
// ring on the floor, a faint x-ray copy that shows through walls, and a floating name tag.
// Positions arrive ~20 times a second; avatars glide between updates. Players on a team wear
// its colour; their shooting-server state (hit points, downed) shows on the avatar and its tag.
import * as THREE from "three";
import { type CombatLink, type Fighter, TEAM_COLORS, healthText } from "./combat.ts";
import { type GameLink, type GamePlayer, colorFor, headingToYaw } from "./game.ts";

const SMOOTHING = 15; // 1/s
const TURN_SMOOTHING = 30; // 1/s; facing follows the phone closely, position glides more
const FLASH_DECAY = 4; // 1/s, how fast the white hit flash fades
const BODY_HEIGHT = 1.45;
const HEAD_Y = 1.62;
export const EYE_HEIGHT = 1.6;
export const CHEST_Y = 1.25;

/** One colour per player everywhere: their team's, or their own until they pick a team. */
export const playerColor = (p: Pick<GamePlayer, "id" | "team">) => (p.team ? TEAM_COLORS[p.team] : colorFor(p.id));

const bodyGeom = new THREE.CapsuleGeometry(0.22, BODY_HEIGHT - 0.44, 4, 12).translate(0, BODY_HEIGHT / 2, 0);
const headGeom = new THREE.SphereGeometry(0.14, 16, 12).translate(0, HEAD_Y, 0);
// Visor wedge pointing along -Z (the avatar's forward).
const visorGeom = new THREE.ConeGeometry(0.09, 0.32, 12).rotateX(-Math.PI / 2).translate(0, HEAD_Y, -0.22);
const ringGeom = new THREE.RingGeometry(0.92, 1, 48).rotateX(-Math.PI / 2);

export interface Pose {
  x: number;
  z: number;
  yaw: number;
}

class Avatar {
  readonly group = new THREE.Group();
  readonly label = document.createElement("div");
  readonly nameEl = document.createElement("b");
  readonly metaEl = document.createElement("span");
  private body = new THREE.Group(); // tips over when the player is downed
  private ghost = new THREE.Group();
  private ring: THREE.Mesh;
  private solid: THREE.MeshLambertMaterial;
  private ghostMat: THREE.MeshBasicMaterial;
  private ringMat: THREE.MeshBasicMaterial;
  private color = "";
  private flash = 0;
  pose: Pose | null = null;
  private target: Pose | null = null;
  private spread = 0;
  player!: GamePlayer;
  fighter: Fighter | null = null;

  constructor(
    readonly id: string,
    labels: HTMLElement,
  ) {
    this.solid = new THREE.MeshLambertMaterial({ transparent: true });
    this.ghostMat = new THREE.MeshBasicMaterial({ transparent: true, opacity: 0.22, depthTest: false, depthWrite: false });
    this.ringMat = new THREE.MeshBasicMaterial({ transparent: true, opacity: 0.55, side: THREE.DoubleSide });
    const visorMat = new THREE.MeshBasicMaterial({ color: 0xffffff });
    this.body.add(new THREE.Mesh(bodyGeom, this.solid), new THREE.Mesh(headGeom, this.solid), new THREE.Mesh(visorGeom, visorMat));
    this.ghost.add(new THREE.Mesh(bodyGeom, this.ghostMat), new THREE.Mesh(headGeom, this.ghostMat));
    this.ghost.children.forEach((m) => (m.renderOrder = 999));
    this.body.add(this.ghost);
    this.group.add(this.body);
    this.ring = new THREE.Mesh(ringGeom, this.ringMat);
    this.ring.position.y = 0.02;
    this.ring.renderOrder = 998;

    this.label.className = "tag";
    this.label.append(this.nameEl, this.metaEl);
    labels.appendChild(this.label);
  }

  private setColor(color: string) {
    if (color === this.color) return;
    this.color = color;
    for (const m of [this.solid, this.ghostMat, this.ringMat]) m.color.set(color);
    this.label.style.setProperty("--c", color);
  }

  /** Flash white: this player was just hit. */
  hit() {
    this.flash = 1;
  }

  addTo(scene: THREE.Object3D) {
    scene.add(this.group, this.ring);
  }

  dispose() {
    this.group.removeFromParent();
    this.ring.removeFromParent();
    this.label.remove();
    this.solid.dispose();
    this.ghostMat.dispose();
    this.ringMat.dispose();
  }

  update(p: GamePlayer, game: GameLink, fighter: Fighter | null) {
    this.player = p;
    this.fighter = fighter;
    this.setColor(playerColor(p));
    if (!p.calibrated || p.x == null || p.y == null) {
      this.target = null;
      this.pose = null;
      return;
    }
    const w = game.toWorld(p.x, p.y);
    this.target = { x: w.x, z: w.z, yaw: headingToYaw(p.heading ?? 0) };
    this.pose ??= { ...this.target };
    this.spread = p.spread ?? 0;
    const down = fighter?.alive === false;
    const alpha = p.connected && !down ? 1 : 0.35;
    this.solid.opacity = alpha;
    this.ghostMat.opacity = 0.22 * alpha;
    // Downed: lying on the floor, face down.
    this.body.rotation.x = down ? -Math.PI / 2 : 0;
    this.body.position.y = down ? 0.22 : 0;
    this.label.classList.toggle("offline", !p.connected);
    this.label.classList.toggle("down", down);
  }

  animate(dt: number, hidden: boolean) {
    const visible = !!(this.pose && this.target) && !hidden;
    this.group.visible = visible;
    this.ring.visible = !!this.pose;
    if (this.flash > 0) {
      this.flash = Math.max(0, this.flash - FLASH_DECAY * dt);
      this.solid.emissive.setScalar(this.flash);
    }
    if (!this.pose || !this.target) return;
    const k = 1 - Math.exp(-SMOOTHING * dt);
    this.pose.x += (this.target.x - this.pose.x) * k;
    this.pose.z += (this.target.z - this.pose.z) * k;
    const dYaw = Math.atan2(Math.sin(this.target.yaw - this.pose.yaw), Math.cos(this.target.yaw - this.pose.yaw));
    this.pose.yaw += dYaw * (1 - Math.exp(-TURN_SMOOTHING * dt));
    this.group.position.set(this.pose.x, 0, this.pose.z);
    this.group.rotation.y = this.pose.yaw;
    this.ring.position.set(this.pose.x, 0.02, this.pose.z);
    this.ring.scale.setScalar(Math.max(0.4, this.spread));
  }
}

export class PlayerLayer {
  readonly group = new THREE.Group();
  private avatars = new Map<string, Avatar>();
  private tmp = new THREE.Vector3();
  /** Player whose eyes the camera is in; their avatar is hidden so it doesn't block the view. */
  hiddenId: string | null = null;
  selectedId: string | null = null;

  constructor(
    private game: GameLink,
    private combat: CombatLink,
    private labels: HTMLElement,
  ) {}

  set(players: GamePlayer[]) {
    const seen = new Set<string>();
    for (const p of players) {
      seen.add(p.id);
      let a = this.avatars.get(p.id);
      if (!a) {
        a = new Avatar(p.id, this.labels);
        a.addTo(this.group);
        this.avatars.set(p.id, a);
      }
      a.update(p, this.game, this.combat.fighter(p.tag));
    }
    for (const [id, a] of this.avatars) {
      if (!seen.has(id)) {
        a.dispose();
        this.avatars.delete(id);
      }
    }
  }

  /** Smoothed world pose of a player, or null if they haven't been placed. */
  pose(id: string): Pose | null {
    return this.avatars.get(id)?.pose ?? null;
  }

  poses() {
    return [...this.avatars.values()]
      .filter((a) => a.pose)
      .map((a) => ({ player: a.player, pose: a.pose!, fighter: a.fighter }));
  }

  /** The player wearing chest tag `tag`, with their pose if placed. */
  byTag(tag: number) {
    for (const a of this.avatars.values()) if (a.player.tag === tag) return { player: a.player, pose: a.pose, avatar: a };
    return null;
  }

  /** Move avatars and pin each name tag above its head on screen. */
  animate(dt: number, camera: THREE.Camera, width: number, height: number) {
    const camPos = camera.getWorldPosition(new THREE.Vector3());
    for (const a of this.avatars.values()) {
      a.animate(dt, a.id === this.hiddenId);
      const show = a.group.visible;
      a.label.hidden = !show;
      if (!show || !a.pose) continue;
      this.tmp.set(a.pose.x, HEAD_Y + 0.38, a.pose.z).project(camera);
      if (this.tmp.z > 1 || Math.abs(this.tmp.x) > 1.2 || Math.abs(this.tmp.y) > 1.2) {
        a.label.hidden = true;
        continue;
      }
      const dist = Math.hypot(a.pose.x - camPos.x, a.pose.z - camPos.z);
      const p = a.player;
      const f = a.fighter;
      const health = !f ? "" : f.alive ? `${healthText(f)} · ` : `${healthText(f).toUpperCase()} · `;
      const meta = `${health}${dist.toFixed(1)} m${(p.spread ?? 0) > 1.5 ? ` · ±${p.spread!.toFixed(1)}` : ""}`;
      if (a.nameEl.textContent !== p.name) a.nameEl.textContent = p.name;
      if (a.metaEl.textContent !== meta) a.metaEl.textContent = meta;
      a.label.classList.toggle("selected", a.id === this.selectedId);
      a.label.style.transform = `translate(-50%, -100%) translate(${((this.tmp.x + 1) / 2) * width}px, ${((1 - this.tmp.y) / 2) * height}px)`;
    }
  }
}
