// Spectator camera: first-person walking (the default), free fly, a top-down overview of the
// floor plan, and spectating a player from their eyes or over their shoulder.
import * as THREE from "three";
import type { Pose } from "./players.ts";

export type Mode = "walk" | "fly" | "overview" | "follow";

const EYE_HEIGHT = 1.6;
const OVERVIEW_CUT = 2.0; // overview camera sits here, so splats above it (ceiling) are not drawn
const LOOK_SPEED = 0.0022;
const JUMP_SPEED = 3.2; // m/s
const GRAVITY = 9.8;
const THIRD_PERSON_DIST = 2.6;

export class SpectatorControls {
  mode: Mode = "walk";
  readonly perspective: THREE.PerspectiveCamera;
  readonly overview: THREE.OrthographicCamera;
  speed = 2.5; // m/s
  /** While following: the followed player's pose, and whether we look over their shoulder. */
  followPose: (() => Pose | null) | null = null;
  thirdPerson = false;
  onModeChange: (mode: Mode) => void = () => {};
  private yaw = 0;
  private pitch = 0;
  private orbitYaw = 0; // third person: camera angle around the player, relative to their facing
  private vy = 0;
  private jumpY = 0;
  private keys = new Set<string>();
  private overviewSize = 12; // metres visible vertically
  private dragging: { x: number; y: number } | null = null;
  private raycaster = new THREE.Raycaster();

  constructor(private canvas: HTMLCanvasElement) {
    this.perspective = new THREE.PerspectiveCamera(75, 1, 0.02, 500);
    this.perspective.rotation.order = "YXZ";
    this.overview = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.01, 100);
    this.overview.position.set(0, OVERVIEW_CUT, 0);
    this.overview.up.set(0, 0, -1);
    this.overview.lookAt(0, 0, 0);
    this.resetPose();

    canvas.addEventListener("click", () => {
      if (this.mode !== "overview") canvas.requestPointerLock();
    });
    document.addEventListener("mousemove", (e) => this.onMouseMove(e));
    canvas.addEventListener("mousedown", (e) => {
      if (this.mode === "overview") this.dragging = { x: e.clientX, y: e.clientY };
    });
    window.addEventListener("mouseup", () => (this.dragging = null));
    canvas.addEventListener("wheel", (e) => this.onWheel(e), { passive: false });
    window.addEventListener("keydown", (e) => {
      if (e.target instanceof HTMLInputElement) return;
      this.keys.add(e.code);
      if (e.code === "KeyF") this.setMode(this.mode === "fly" ? "walk" : "fly");
      if (e.code === "KeyM") this.setMode(this.mode === "overview" ? "walk" : "overview");
      if (e.code === "KeyV" && this.mode === "follow") this.thirdPerson = !this.thirdPerson;
      if (e.code === "Space" && this.mode === "walk" && this.jumpY === 0) this.vy = JUMP_SPEED;
      // Moving takes over the camera from the spectated player, right where they stand.
      if (this.mode === "follow" && ["KeyW", "KeyA", "KeyS", "KeyD"].includes(e.code)) this.setMode("walk");
    });
    window.addEventListener("keyup", (e) => this.keys.delete(e.code));
    window.addEventListener("blur", () => this.keys.clear());
  }

  get camera(): THREE.Camera {
    return this.mode === "overview" ? this.overview : this.perspective;
  }

  /** Direction the camera faces, as a yaw (rad, 0 = -Z). */
  get facing() {
    return this.perspective.rotation.y;
  }

  /** Stand in the middle of the map at eye height. */
  resetPose() {
    this.perspective.position.set(0, EYE_HEIGHT, 0);
    this.yaw = 0;
    this.pitch = 0;
    this.overview.position.set(0, OVERVIEW_CUT, 0);
  }

  setMode(mode: Mode) {
    const prev = this.mode;
    this.mode = mode;
    if (mode !== "follow") this.followPose = null;
    if (prev === "follow" && mode !== "follow") {
      this.yaw = this.perspective.rotation.y;
      this.pitch = this.perspective.rotation.x;
      this.thirdPerson = false;
    }
    if (mode === "overview") {
      document.exitPointerLock();
      this.overview.position.x = this.perspective.position.x;
      this.overview.position.z = this.perspective.position.z;
    }
    if (mode === "walk") {
      this.perspective.position.y = EYE_HEIGHT;
      this.vy = this.jumpY = 0;
    }
    if (prev !== mode) this.onModeChange(mode);
  }

  follow(pose: () => Pose | null) {
    this.followPose = pose;
    this.orbitYaw = 0;
    this.pitch = Math.min(this.pitch, 0);
    this.setMode("follow");
  }

  resize(width: number, height: number) {
    this.perspective.aspect = width / height;
    this.perspective.updateProjectionMatrix();
    const half = this.overviewSize / 2;
    const aspect = width / height;
    Object.assign(this.overview, { left: -half * aspect, right: half * aspect, top: half, bottom: -half });
    this.overview.updateProjectionMatrix();
  }

  /** Floor point (y = 0) under a screen position in the overview. */
  floorAt(clientX: number, clientY: number): THREE.Vector3 | null {
    const r = this.canvas.getBoundingClientRect();
    const ndc = new THREE.Vector2(((clientX - r.left) / r.width) * 2 - 1, -((clientY - r.top) / r.height) * 2 + 1);
    this.raycaster.setFromCamera(ndc, this.camera);
    return this.raycaster.ray.intersectPlane(new THREE.Plane(new THREE.Vector3(0, 1, 0), 0), new THREE.Vector3());
  }

  private onMouseMove(e: MouseEvent) {
    if (this.mode === "overview") {
      if (!this.dragging) return;
      const metresPerPixel = this.overviewSize / this.canvas.clientHeight;
      this.overview.position.x -= (e.clientX - this.dragging.x) * metresPerPixel;
      this.overview.position.z -= (e.clientY - this.dragging.y) * metresPerPixel;
      this.dragging = { x: e.clientX, y: e.clientY };
      return;
    }
    if (document.pointerLockElement !== this.canvas) return;
    if (this.mode === "follow" && this.thirdPerson) this.orbitYaw -= e.movementX * LOOK_SPEED;
    else this.yaw -= e.movementX * LOOK_SPEED;
    this.pitch = THREE.MathUtils.clamp(this.pitch - e.movementY * LOOK_SPEED, -1.5, 1.5);
  }

  private onWheel(e: WheelEvent) {
    e.preventDefault();
    if (this.mode === "overview") {
      this.overviewSize = THREE.MathUtils.clamp(this.overviewSize * Math.exp(e.deltaY * 0.001), 2, 200);
      this.resize(this.canvas.clientWidth, this.canvas.clientHeight);
    } else {
      this.speed = THREE.MathUtils.clamp(this.speed * Math.exp(-e.deltaY * 0.001), 0.3, 30);
    }
  }

  update(dt: number) {
    if (this.mode === "overview") return;
    const cam = this.perspective;

    if (this.mode === "follow") {
      const pose = this.followPose?.();
      if (!pose) return;
      if (this.thirdPerson) {
        const angle = pose.yaw + this.orbitYaw;
        const pitch = THREE.MathUtils.clamp(this.pitch, -1.2, 0.3);
        const back = new THREE.Vector3(Math.sin(angle), 0, Math.cos(angle)).multiplyScalar(THIRD_PERSON_DIST * Math.cos(pitch));
        cam.position.set(pose.x + back.x, EYE_HEIGHT + 0.3 - THIRD_PERSON_DIST * Math.sin(pitch), pose.z + back.z);
        cam.rotation.set(pitch, angle, 0);
      } else {
        // Their eyes: the phone tells us where they face, the mouse only tilts up and down.
        cam.position.set(pose.x, EYE_HEIGHT, pose.z);
        cam.rotation.set(this.pitch, pose.yaw, 0);
      }
      return;
    }

    cam.rotation.set(this.pitch, this.yaw, 0);
    const move = new THREE.Vector3(
      Number(this.keys.has("KeyD")) - Number(this.keys.has("KeyA")),
      0,
      Number(this.keys.has("KeyS")) - Number(this.keys.has("KeyW")),
    );
    const speed = this.speed * (this.keys.has("ShiftLeft") || this.keys.has("ShiftRight") ? 2.2 : 1);

    if (this.mode === "fly") {
      move.applyQuaternion(cam.quaternion);
      move.y += Number(this.keys.has("KeyE")) - Number(this.keys.has("KeyQ"));
    } else {
      // Walking: move on the floor plane in the direction we face, at eye height (plus a jump).
      move.applyAxisAngle(new THREE.Vector3(0, 1, 0), this.yaw);
      this.vy -= GRAVITY * dt;
      this.jumpY = Math.max(0, this.jumpY + this.vy * dt);
      if (this.jumpY === 0) this.vy = 0;
      cam.position.y = EYE_HEIGHT + this.jumpY;
    }
    if (move.lengthSq() > 0) cam.position.addScaledVector(move.normalize(), speed * dt);
  }
}
