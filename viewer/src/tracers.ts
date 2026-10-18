// Shots in the 3D map: when the shooting server reports a hit, a tracer flies from the shooter's
// chest to the target's (both positions come from the position tracking) and fades out, with a
// burst where it lands. Drawn over the splat so a spectator sees shots through walls too.
import * as THREE from "three";
import { CHEST_Y } from "./players.ts";

const LIFETIME = 0.9; // s
const burstGeom = new THREE.SphereGeometry(0.18, 16, 12);

interface Tracer {
  line: THREE.Line;
  burst: THREE.Mesh;
  age: number;
}

export class TracerLayer {
  readonly group = new THREE.Group();
  private tracers: Tracer[] = [];

  add(from: { x: number; z: number }, to: { x: number; z: number }, color: string, killed: boolean) {
    const lineMat = new THREE.LineBasicMaterial({ color, transparent: true, depthTest: false });
    const line = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints([
        new THREE.Vector3(from.x, CHEST_Y, from.z),
        new THREE.Vector3(to.x, CHEST_Y, to.z),
      ]),
      lineMat,
    );
    const burst = new THREE.Mesh(
      burstGeom,
      new THREE.MeshBasicMaterial({ color: killed ? 0xffffff : color, transparent: true, depthTest: false }),
    );
    burst.position.set(to.x, CHEST_Y, to.z);
    line.renderOrder = burst.renderOrder = 1001;
    this.group.add(line, burst);
    this.tracers.push({ line, burst, age: 0 });
  }

  animate(dt: number) {
    for (const t of this.tracers) {
      t.age += dt;
      const fade = Math.max(0, 1 - t.age / LIFETIME);
      (t.line.material as THREE.LineBasicMaterial).opacity = fade;
      (t.burst.material as THREE.MeshBasicMaterial).opacity = fade;
      t.burst.scale.setScalar(1 + 3 * (t.age / LIFETIME));
    }
    this.tracers = this.tracers.filter((t) => {
      if (t.age < LIFETIME) return true;
      this.group.remove(t.line, t.burst);
      t.line.geometry.dispose();
      (t.line.material as THREE.Material).dispose();
      (t.burst.material as THREE.Material).dispose();
      return false;
    });
  }
}
