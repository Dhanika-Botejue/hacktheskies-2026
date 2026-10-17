// Game markers drawn in map coordinates (metres, floor at y=0). The sample list is a stand-in
// until the game server streams flags, bases and players over WebSocket.
import * as THREE from "three";

export interface Marker {
  id: string;
  kind: "flag" | "base" | "player";
  team?: "red" | "blue" | null;
  x: number;
  z: number;
}

const TEAM_COLORS = { red: 0xff4d5e, blue: 0x4da3ff, none: 0xf2f2f2 };

export const SAMPLE_MARKERS: Marker[] = [
  { id: "base-red", kind: "base", team: "red", x: -2, z: 0 },
  { id: "base-blue", kind: "base", team: "blue", x: 2, z: 0 },
  { id: "flag-1", kind: "flag", team: null, x: 0, z: -1.5 },
  { id: "flag-2", kind: "flag", team: null, x: 0, z: 1.5 },
];

function build(marker: Marker): THREE.Object3D {
  const color = TEAM_COLORS[marker.team ?? "none"];
  const mat = new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.85 });
  const group = new THREE.Group();
  if (marker.kind === "base") {
    const disc = new THREE.Mesh(new THREE.CylinderGeometry(0.8, 0.8, 0.03, 40), mat);
    disc.position.y = 0.02;
    group.add(disc);
  } else if (marker.kind === "flag") {
    const pole = new THREE.Mesh(new THREE.CylinderGeometry(0.03, 0.03, 1.8, 8), mat);
    pole.position.y = 0.9;
    const cloth = new THREE.Mesh(new THREE.PlaneGeometry(0.5, 0.32), mat);
    cloth.material.side = THREE.DoubleSide;
    cloth.position.set(0.25, 1.6, 0);
    const zone = new THREE.Mesh(
      new THREE.RingGeometry(1.4, 1.5, 48),
      new THREE.MeshBasicMaterial({ color, side: THREE.DoubleSide }),
    );
    zone.rotation.x = -Math.PI / 2;
    zone.position.y = 0.02;
    group.add(pole, cloth, zone);
  } else {
    const body = new THREE.Mesh(new THREE.CapsuleGeometry(0.25, 1.1, 4, 12), mat);
    body.position.y = 0.8;
    group.add(body);
  }
  group.position.set(marker.x, 0, marker.z);
  return group;
}

export class MarkerLayer {
  readonly group = new THREE.Group();
  private objects = new Map<string, THREE.Object3D>();

  set(markers: Marker[]) {
    const seen = new Set<string>();
    for (const m of markers) {
      seen.add(m.id);
      let obj = this.objects.get(m.id);
      if (!obj) {
        obj = build(m);
        this.objects.set(m.id, obj);
        this.group.add(obj);
      }
      obj.position.set(m.x, 0, m.z);
    }
    for (const [id, obj] of this.objects) {
      if (!seen.has(id)) {
        this.group.remove(obj);
        this.objects.delete(id);
      }
    }
  }
}
