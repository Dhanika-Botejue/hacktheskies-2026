// Live players in the 3D map: an avatar per placed player with a facing visor, an uncertainty
// ring on the floor, a faint x-ray copy that shows through walls, and a floating name tag.
// Positions arrive ~20 times a second; avatars glide between updates.
import * as THREE from "three";
import { type GameLink, type GamePlayer, colorFor, headingToYaw } from "./game.ts";

const SMOOTHING = 15; // 1/s
const TURN_SMOOTHING = 30; // 1/s; facing follows the phone closely, position glides more
const BODY_HEIGHT = 1.45;
const HEAD_Y = 1.62;
export const EYE_HEIGHT = 1.6;

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
  private ghost = new THREE.Group();
  private ring: THREE.Mesh;
  private solid: THREE.MeshLambertMaterial;
  private ghostMat: THREE.MeshBasicMaterial;
  private ringMat: THREE.MeshBasicMaterial;
  pose: Pose | null = null;
  private target: Pose | null = null;
  private spread = 0;
  player!: GamePlayer;

  constructor(
    readonly id: string,
    labels: HTMLElement,
  ) {
    const color = new THREE.Color(colorFor(id));
    this.solid = new THREE.MeshLambertMaterial({ color, transparent: true });
    this.ghostMat = new THREE.MeshBasicMaterial({
      color, transparent: true, opacity: 0.22, depthTest: false, depthWrite: false,
    });
    this.ringMat = new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.55, side: THREE.DoubleSide });
    const visorMat = new THREE.MeshBasicMaterial({ color: 0xffffff });
    this.group.add(new THREE.Mesh(bodyGeom, this.solid), new THREE.Mesh(headGeom, this.solid), new THREE.Mesh(visorGeom, visorMat));
    this.ghost.add(new THREE.Mesh(bodyGeom, this.ghostMat), new THREE.Mesh(headGeom, this.ghostMat));
    this.ghost.children.forEach((m) => (m.renderOrder = 999));
    this.group.add(this.ghost);
    this.ring = new THREE.Mesh(ringGeom, this.ringMat);
    this.ring.position.y = 0.02;
    this.ring.renderOrder = 998;

    this.label.className = "tag";
    this.label.style.setProperty("--c", colorFor(id));
    this.label.append(this.nameEl, this.metaEl);
    labels.appendChild(this.label);
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

  update(p: GamePlayer, game: GameLink) {
    this.player = p;
    if (!p.calibrated || p.x == null || p.y == null) {
      this.target = null;
      this.pose = null;
      return;
    }
    const w = game.toWorld(p.x, p.y);
    this.target = { x: w.x, z: w.z, yaw: headingToYaw(p.heading ?? 0) };
    this.pose ??= { ...this.target };
    this.spread = p.spread ?? 0;
    const alpha = p.connected ? 1 : 0.35;
    this.solid.opacity = alpha;
    this.ghostMat.opacity = 0.22 * alpha;
    this.label.classList.toggle("offline", !p.connected);
  }

  animate(dt: number, hidden: boolean) {
    const visible = !!(this.pose && this.target) && !hidden;
    this.group.visible = visible;
    this.ring.visible = !!this.pose;
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
      a.update(p, this.game);
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
    return [...this.avatars.values()].filter((a) => a.pose).map((a) => ({ player: a.player, pose: a.pose! }));
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
      const meta = `${dist.toFixed(1)} m${(p.spread ?? 0) > 1.5 ? ` · ±${p.spread!.toFixed(1)}` : ""}`;
      if (a.nameEl.textContent !== p.name) a.nameEl.textContent = p.name;
      if (a.metaEl.textContent !== meta) a.metaEl.textContent = meta;
      a.label.classList.toggle("selected", a.id === this.selectedId);
      a.label.style.transform = `translate(-50%, -100%) translate(${((this.tmp.x + 1) / 2) * width}px, ${((1 - this.tmp.y) / 2) * height}px)`;
    }
  }
}
