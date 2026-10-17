// Spectator camera: free fly, eye-height walking, and a top-down overview of the floor plan.
import * as THREE from "three";

export type Mode = "fly" | "walk" | "overview";

const EYE_HEIGHT = 1.6;
const OVERVIEW_CUT = 2.0; // overview camera sits here, so splats above it (ceiling) are not drawn
const LOOK_SPEED = 0.0022;

export class SpectatorControls {
  mode: Mode = "fly";
  readonly perspective: THREE.PerspectiveCamera;
  readonly overview: THREE.OrthographicCamera;
  speed = 2.5; // m/s
  private yaw = 0;
  private pitch = 0;
  private keys = new Set<string>();
  private overviewSize = 12; // metres visible vertically
  private dragging: { x: number; y: number } | null = null;

  constructor(private canvas: HTMLCanvasElement) {
    this.perspective = new THREE.PerspectiveCamera(70, 1, 0.02, 500);
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
      if (e.code === "KeyF") this.setMode(this.mode === "walk" ? "fly" : "walk");
      if (e.code === "KeyM") this.setMode(this.mode === "overview" ? "fly" : "overview");
    });
    window.addEventListener("keyup", (e) => this.keys.delete(e.code));
    window.addEventListener("blur", () => this.keys.clear());
  }

  get camera(): THREE.Camera {
    return this.mode === "overview" ? this.overview : this.perspective;
  }

  /** Stand in the middle of the map at eye height. */
  resetPose() {
    this.perspective.position.set(0, EYE_HEIGHT, 0);
    this.yaw = 0;
    this.pitch = 0;
    this.overview.position.set(0, OVERVIEW_CUT, 0);
  }

  setMode(mode: Mode) {
    this.mode = mode;
    if (mode === "overview") {
      document.exitPointerLock();
      this.overview.position.x = this.perspective.position.x;
      this.overview.position.z = this.perspective.position.z;
    }
    if (mode === "walk") this.perspective.position.y = EYE_HEIGHT;
  }

  resize(width: number, height: number) {
    this.perspective.aspect = width / height;
    this.perspective.updateProjectionMatrix();
    const half = this.overviewSize / 2;
    const aspect = width / height;
    Object.assign(this.overview, { left: -half * aspect, right: half * aspect, top: half, bottom: -half });
    this.overview.updateProjectionMatrix();
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
    this.yaw -= e.movementX * LOOK_SPEED;
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
    cam.rotation.set(this.pitch, this.yaw, 0);

    const move = new THREE.Vector3(
      Number(this.keys.has("KeyD")) - Number(this.keys.has("KeyA")),
      0,
      Number(this.keys.has("KeyS")) - Number(this.keys.has("KeyW")),
    );
    const speed = this.speed * (this.keys.has("ShiftLeft") || this.keys.has("ShiftRight") ? 3 : 1);

    if (this.mode === "fly") {
      move.applyQuaternion(cam.quaternion);
      move.y += Number(this.keys.has("KeyE")) - Number(this.keys.has("KeyQ"));
    } else {
      // Walking: move on the floor plane in the direction we face, at a fixed eye height.
      move.applyAxisAngle(new THREE.Vector3(0, 1, 0), this.yaw);
      cam.position.y = EYE_HEIGHT;
    }
    if (move.lengthSq() > 0) cam.position.addScaledVector(move.normalize(), speed * dt);
  }
}
