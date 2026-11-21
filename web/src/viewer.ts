import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { TransformControls } from "three/examples/jsm/controls/TransformControls.js";

export type MapLed = { i: number; p: number[] | null; status: string; segments: number; err_px: number | null; sigma: number | null };
export type TreeMap = {
  id: string;
  scan: string;
  units: string;
  leds: MapLed[];
  chain: {
    base_link: number; long_links: { from: number; bound: number; kind: string }[]; breaks: { from: number; reason: string }[];
    period: number | null; trusted_links: number;
    link_bounds?: (number | null)[]; // every link's bound (null = break); maps solved before this field have none
    cap?: number | null;
  };
  quality: any;
  surface: { h: number[]; r: number[] };
};

export const STATUS_COLORS: Record<string, string> = {
  measured: "#3fb37f",
  single_view: "#f2c94c",
  estimated: "#f2994a",
  manual: "#56a0ff",
  unknown: "#5d6b7a",
};

export class TreeViewer {
  private renderer: THREE.WebGLRenderer;
  private scene = new THREE.Scene();
  private camera: THREE.PerspectiveCamera;
  private controls: OrbitControls;
  private mesh: THREE.InstancedMesh | null = null;
  private line: THREE.LineSegments | null = null;
  private marker: THREE.Mesh;
  private drag: TransformControls | null = null;
  private dragObj = new THREE.Object3D();
  private raf = 0;
  private ro: ResizeObserver;
  private map: TreeMap | null = null;
  private index: number[] = []; // instance -> LED index
  private ledToInst = new Map<number, number>();
  private pointer = { x: 0, y: 0, down: 0 };
  onSelect: (i: number | null) => void = () => {};
  onDragEnd: (i: number, p: [number, number, number]) => void = () => {};

  constructor(private container: HTMLElement) {
    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    container.append(this.renderer.domElement);
    this.camera = new THREE.PerspectiveCamera(45, 1, 0.1, 10000);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.scene.add(new THREE.AmbientLight(0xffffff, 1.5));
    this.marker = new THREE.Mesh(new THREE.SphereGeometry(1, 16, 12), new THREE.MeshBasicMaterial({ color: 0xffffff, wireframe: true }));
    this.marker.visible = false;
    this.scene.add(this.marker);
    this.ro = new ResizeObserver(() => this.resize());
    this.ro.observe(container);
    const el = this.renderer.domElement;
    el.addEventListener("pointerdown", (e) => (this.pointer = { x: e.clientX, y: e.clientY, down: performance.now() }));
    el.addEventListener("pointerup", (e) => {
      const moved = Math.hypot(e.clientX - this.pointer.x, e.clientY - this.pointer.y);
      if (moved < 6 && performance.now() - this.pointer.down < 500 && !this.drag?.dragging) this.pick(e);
    });
    this.resize();
    const tick = () => {
      this.controls.update();
      this.renderer.render(this.scene, this.camera);
      this.raf = requestAnimationFrame(tick);
    };
    tick();
  }

  private resize(): void {
    const w = this.container.clientWidth || 300;
    const h = this.container.clientHeight || 300;
    this.renderer.setSize(w, h);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  setMap(map: TreeMap): void {
    this.map = map;
    if (this.mesh) this.scene.remove(this.mesh);
    if (this.line) this.scene.remove(this.line);
    const placed = map.leds.filter((l) => l.p);
    const box = new THREE.Box3();
    for (const l of placed) box.expandByPoint(new THREE.Vector3(...(l.p as [number, number, number])));
    const size = box.getSize(new THREE.Vector3());
    const height = Math.max(size.y, 1e-6);
    const r = height * 0.008;
    const geom = new THREE.SphereGeometry(r, 10, 8);
    const mat = new THREE.MeshBasicMaterial({ color: 0xffffff });
    this.mesh = new THREE.InstancedMesh(geom, mat, placed.length);
    this.index = [];
    this.ledToInst.clear();
    const m4 = new THREE.Matrix4();
    placed.forEach((l, k) => {
      m4.makeTranslation(l.p![0], l.p![1], l.p![2]);
      this.mesh!.setMatrixAt(k, m4);
      this.index.push(l.i);
      this.ledToInst.set(l.i, k);
    });
    this.colorByStatus();
    this.scene.add(this.mesh);
    // Chain: a line between consecutive placed LEDs, skipping breaks; long links highlighted.
    const breaks = new Set(map.chain.breaks.map((b) => b.from));
    const longs = new Set(map.chain.long_links.map((b) => b.from));
    const pos: number[] = [];
    const col: number[] = [];
    for (let i = 0; i + 1 < map.leds.length; i++) {
      const a = map.leds[i].p;
      const b = map.leds[i + 1].p;
      if (!a || !b || breaks.has(i)) continue;
      pos.push(...a, ...b);
      const c = longs.has(i) ? [0.95, 0.6, 0.2] : [0.35, 0.42, 0.5];
      col.push(...c, ...c);
    }
    const lg = new THREE.BufferGeometry();
    lg.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    lg.setAttribute("color", new THREE.Float32BufferAttribute(col, 3));
    this.line = new THREE.LineSegments(lg, new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.6 }));
    this.scene.add(this.line);
    this.marker.scale.setScalar(r * 2.5);
    const center = box.getCenter(new THREE.Vector3());
    this.controls.target.copy(center);
    this.camera.near = height / 100;
    this.camera.far = height * 100;
    this.camera.position.set(center.x, center.y + height * 0.2, center.z + height * 2.2);
    this.camera.updateProjectionMatrix();
  }

  colorByStatus(): void {
    if (!this.mesh || !this.map) return;
    const c = new THREE.Color();
    this.index.forEach((i, k) => {
      c.set(STATUS_COLORS[this.map!.leds[i].status] ?? "#888");
      this.mesh!.setColorAt(k, c);
    });
    this.mesh.instanceColor!.needsUpdate = true;
  }

  /** Live colors (n*3 bytes, LED order), e.g. from the output preview. */
  setColors(rgb: Uint8Array): void {
    if (!this.mesh) return;
    const c = new THREE.Color();
    this.index.forEach((i, k) => {
      const r = rgb[3 * i] / 255, g = rgb[3 * i + 1] / 255, b = rgb[3 * i + 2] / 255;
      // Dim LEDs are drawn dark grey so the tree's shape stays visible.
      c.setRGB(Math.max(r, 0.08), Math.max(g, 0.08), Math.max(b, 0.08));
      this.mesh!.setColorAt(k, c);
    });
    this.mesh.instanceColor!.needsUpdate = true;
  }

  select(i: number | null): void {
    const p = i !== null ? this.map?.leds[i]?.p : null;
    this.marker.visible = !!p;
    if (p) this.marker.position.set(p[0], p[1], p[2]);
  }

  private pick(e: PointerEvent): void {
    if (!this.mesh) return;
    const rect = this.renderer.domElement.getBoundingClientRect();
    const ndc = new THREE.Vector2(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1);
    const ray = new THREE.Raycaster();
    ray.setFromCamera(ndc, this.camera);
    // Generous pick: nearest LED to the ray within a few marker radii.
    let best = -1;
    let bestD = Infinity;
    const v = new THREE.Vector3();
    const m4 = new THREE.Matrix4();
    const tol = this.marker.scale.x * 2;
    for (let k = 0; k < this.index.length; k++) {
      this.mesh.getMatrixAt(k, m4);
      v.setFromMatrixPosition(m4);
      const d = ray.ray.distanceToPoint(v);
      if (d < tol && d < bestD) {
        bestD = d;
        best = k;
      }
    }
    const i = best >= 0 ? this.index[best] : null;
    this.select(i);
    this.onSelect(i);
  }

  /** Let the user drag one LED; reports the new position when released. */
  startDrag(i: number): void {
    this.stopDrag();
    const p = this.map?.leds[i]?.p;
    if (!p) return;
    this.dragObj.position.set(p[0], p[1], p[2]);
    this.scene.add(this.dragObj);
    const tc = new TransformControls(this.camera, this.renderer.domElement);
    tc.setSize(1.2);
    tc.attach(this.dragObj);
    tc.addEventListener("dragging-changed", (ev: any) => {
      this.controls.enabled = !ev.value;
      if (!ev.value) {
        const q = this.dragObj.position;
        this.marker.position.copy(q);
        const k = this.ledToInst.get(i);
        if (k !== undefined && this.mesh) {
          this.mesh.setMatrixAt(k, new THREE.Matrix4().makeTranslation(q.x, q.y, q.z));
          this.mesh.instanceMatrix.needsUpdate = true;
        }
        this.onDragEnd(i, [q.x, q.y, q.z]);
      }
    });
    this.scene.add(tc.getHelper());
    this.drag = tc;
  }

  stopDrag(): void {
    if (this.drag) {
      this.scene.remove(this.drag.getHelper());
      this.drag.detach();
      this.drag.dispose();
      this.drag = null;
    }
    this.controls.enabled = true;
  }

  dispose(): void {
    cancelAnimationFrame(this.raf);
    this.ro.disconnect();
    this.stopDrag();
    this.controls.dispose();
    this.renderer.dispose();
    this.renderer.domElement.remove();
  }
}
