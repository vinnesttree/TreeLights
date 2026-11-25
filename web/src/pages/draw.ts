// Draw page: paint on the tree with a finger. The 3D view paints where the finger meets the tree's
// surface model; the unwrapped view paints on the surface laid flat (u around the tree, v up). The
// server colors the LEDs within the brush and shows the picture on the real tree as it changes.
import { api, fmtErr, h, LiveSocket, type Page } from "../api";
import { TreeViewer, type TreeMap } from "../viewer";

type Status = { live: boolean; busy: string | null; undo: number; lights_on: boolean; brightness: number; map: string | null };
type Saved = { id: string; name: string; saved: number | null; map: string | null; led_count: number };
type Space = "3d" | "uv";
type Point = number[];

const QUICK = ["#ff0000", "#ff6000", "#ffa000", "#ffd000", "#ffffff", "#ffe0a0", "#00ff00", "#00c080", "#00ffff",
  "#0060ff", "#0000ff", "#8000ff", "#ff00c0", "#ff80a0"];
const STORE_KEY = "treelights.draw";
const SEND_MS = 30; // strokes are sent in small batches while the finger moves

function loadPrefs(): any {
  try {
    return JSON.parse(localStorage.getItem(STORE_KEY) ?? "null") ?? {};
  } catch {
    return {};
  }
}
function savePrefs(v: unknown): void {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(v));
  } catch {
    /* storage unavailable */
  }
}

function hexToRgb(hex: string): [number, number, number] {
  const s = hex.replace("#", "");
  return [parseInt(s.slice(0, 2), 16) || 0, parseInt(s.slice(2, 4), 16) || 0, parseInt(s.slice(4, 6), 16) || 0];
}

/** The map's geometry as the server's brush sees it (effects/drawing.py): u, v and the
 * circumference at each height, in tree heights. */
class TreeShape {
  lo = 0;
  span = 1;
  rmax = 1e-6;
  u: Float64Array;
  v: Float64Array;
  placed: number[] = [];
  constructor(public map: TreeMap) {
    const n = map.leds.length;
    this.u = new Float64Array(n);
    this.v = new Float64Array(n);
    const ys = map.leds.filter((l) => l.p).map((l) => l.p![1]);
    this.lo = Math.min(...ys);
    this.span = Math.max(Math.max(...ys) - this.lo, 1e-9);
    for (const l of map.leds) {
      if (!l.p) continue;
      this.placed.push(l.i);
      const [x, y, z] = l.p;
      const th = Math.atan2(-z, x);
      this.u[l.i] = (((th / (2 * Math.PI)) % 1) + 1) % 1;
      this.v[l.i] = (y - this.lo) / this.span;
      this.rmax = Math.max(this.rmax, Math.hypot(x, z));
    }
  }

  /** Surface radius at height y (map units), as numpy's interp: clamped at the ends. */
  radiusAt(y: number): number {
    const { h: hs, r: rs } = this.map.surface;
    if (!hs.length) return this.rmax;
    if (y <= hs[0]) return rs[0];
    for (let k = 1; k < hs.length; k++) {
      if (y <= hs[k]) return rs[k - 1] + ((rs[k] - rs[k - 1]) * (y - hs[k - 1])) / Math.max(hs[k] - hs[k - 1], 1e-9);
    }
    return rs[rs.length - 1];
  }

  /** Circumference at height v, in tree heights. */
  circAt(v: number): number {
    return Math.max((2 * Math.PI * this.radiusAt(this.lo + v * this.span)) / this.span, 1e-3);
  }

  /** Where a ray first meets the surface model, else the LED nearest the ray within `tol`. */
  hit(o: { x: number; y: number; z: number }, d: { x: number; y: number; z: number }, tol: number): [number, number, number] | null {
    const c = { x: 0, y: this.lo + this.span / 2, z: 0 };
    const R = Math.hypot(this.rmax, this.span / 2) * 1.05;
    const oc = { x: o.x - c.x, y: o.y - c.y, z: o.z - c.z };
    const b = oc.x * d.x + oc.y * d.y + oc.z * d.z;
    const disc = b * b - (oc.x * oc.x + oc.y * oc.y + oc.z * oc.z - R * R);
    if (disc >= 0 && this.map.surface.h.length) {
      const t0 = Math.max(0, -b - Math.sqrt(disc));
      const t1 = -b + Math.sqrt(disc);
      const at = (t: number) => [o.x + d.x * t, o.y + d.y * t, o.z + d.z * t] as [number, number, number];
      const inside = (p: number[]) => p[1] >= this.lo && p[1] <= this.lo + this.span && Math.hypot(p[0], p[2]) <= this.radiusAt(p[1]);
      const step = this.span / 300;
      for (let t = t0; t <= t1; t += step) {
        if (!inside(at(t))) continue;
        let a = Math.max(t0, t - step);
        let z = t;
        for (let k = 0; k < 14; k++) {
          const m = (a + z) / 2;
          if (inside(at(m))) z = m;
          else a = m;
        }
        return at(z);
      }
    }
    // No surface under the finger: the nearest LED to the ray, if close.
    let best: [number, number, number] | null = null;
    let bestD = tol;
    for (const i of this.placed) {
      const p = this.map.leds[i].p!;
      const w = { x: p[0] - o.x, y: p[1] - o.y, z: p[2] - o.z };
      const t = Math.max(0, w.x * d.x + w.y * d.y + w.z * d.z);
      const dist = Math.hypot(w.x - d.x * t, w.y - d.y * t, w.z - d.z * t);
      if (dist < bestD) {
        bestD = dist;
        best = [p[0], p[1], p[2]];
      }
    }
    return best;
  }
}

/** The tree's surface laid flat: u (around the tree) across, v (height) up. */
class UnwrapView {
  canvas = h("canvas", { class: "unwrap" }) as HTMLCanvasElement;
  private ctx: CanvasRenderingContext2D;
  private ro: ResizeObserver;
  private cursor: { u: number; v: number; radius: number; color: string } | null = null;
  private pad = 12;
  private w = 300;
  private hgt = 300;

  constructor(private box: HTMLElement, private shape: TreeShape, private colors: () => Uint8Array) {
    this.ctx = this.canvas.getContext("2d")!;
    box.append(this.canvas);
    this.ro = new ResizeObserver(() => this.resize());
    this.ro.observe(box);
    this.resize();
  }

  private resize(): void {
    this.w = this.box.clientWidth || 300;
    this.hgt = this.box.clientHeight || 300;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.canvas.width = Math.round(this.w * dpr);
    this.canvas.height = Math.round(this.hgt * dpr);
    this.canvas.style.width = `${this.w}px`;
    this.canvas.style.height = `${this.hgt}px`;
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.draw();
  }

  private x(u: number): number {
    return this.pad + u * (this.w - 2 * this.pad);
  }
  private y(v: number): number {
    return this.pad + (1 - v) * (this.hgt - 2 * this.pad);
  }

  /** Screen point -> (u, v). u may leave 0..1 at the edges: the server wraps it around the tree. */
  toUV(clientX: number, clientY: number): Point {
    const r = this.canvas.getBoundingClientRect();
    const u = (clientX - r.left - this.pad) / Math.max(this.w - 2 * this.pad, 1);
    const v = 1 - (clientY - r.top - this.pad) / Math.max(this.hgt - 2 * this.pad, 1);
    return [u, Math.min(Math.max(v, 0), 1)];
  }

  setCursor(c: { u: number; v: number; radius: number; color: string } | null): void {
    this.cursor = c;
    this.draw();
  }

  draw(): void {
    const g = this.ctx;
    g.fillStyle = "#05080b";
    g.fillRect(0, 0, this.w, this.hgt);
    g.strokeStyle = "rgba(255,255,255,0.12)";
    g.lineWidth = 1;
    g.strokeRect(this.x(0), this.y(1), this.x(1) - this.x(0), this.y(0) - this.y(1));
    g.fillStyle = "rgba(255,255,255,0.35)";
    g.font = "11px system-ui, sans-serif";
    g.textAlign = "center";
    g.fillText("front", this.x(0.75), this.y(0) + 10);
    g.fillText("back", this.x(0.25), this.y(0) + 10);
    const rgb = this.colors();
    const dot = Math.max(2.5, Math.min(5, this.w / 120));
    for (const i of this.shape.placed) {
      const r = Math.max(rgb[3 * i], 20), gg = Math.max(rgb[3 * i + 1], 20), b = Math.max(rgb[3 * i + 2], 20);
      g.fillStyle = `rgb(${r},${gg},${b})`;
      g.beginPath();
      g.arc(this.x(this.shape.u[i]), this.y(this.shape.v[i]), dot, 0, 2 * Math.PI);
      g.fill();
    }
    const c = this.cursor;
    if (c) {
      // The brush is round on the tree, so on the flat view it's wider where the tree is narrow.
      const rx = (c.radius / this.shape.circAt(c.v)) * (this.w - 2 * this.pad);
      const ry = c.radius * (this.hgt - 2 * this.pad);
      g.strokeStyle = c.color;
      g.lineWidth = 1.5;
      for (const du of [-1, 0, 1]) {
        g.beginPath();
        g.ellipse(this.x(c.u + du), this.y(c.v), Math.min(rx, this.w), ry, 0, 0, 2 * Math.PI);
        g.stroke();
      }
    }
  }

  dispose(): void {
    this.ro.disconnect();
    this.canvas.remove();
  }
}

export function drawPage(): Page {
  let viewer: TreeViewer | null = null;
  let flat: UnwrapView | null = null;
  let sock: LiveSocket | null = null;
  let brightTimer = 0;
  let sendTimer = 0;
  return {
    mount(root) {
      const prefs = loadPrefs();
      let color: string = typeof prefs.color === "string" ? prefs.color : "#ff0000";
      let size: number = Number(prefs.size) || 6; // % of the tree's height
      let erase = false;
      let view: "3d" | "flat" = prefs.view === "flat" ? "flat" : "3d";
      let turn = false; // 3D view: one finger turns instead of painting
      let shape: TreeShape | null = null;
      let colors = new Uint8Array(0);
      let status: Status | null = null;
      let connected = false;

      const msg = h("div");
      const show = (text: string, cls = "ok") => msg.replaceChildren(text ? h("div", { class: `notice ${cls}` }, text) : "");
      const pill = h("span", { class: "pill unknown" }, "Connecting");
      const showBtn = h("button", { class: "primary" }, "Show on tree") as HTMLButtonElement;
      const viewBtns = { "3d": h("button", {}, "3D"), flat: h("button", {}, "Unwrapped") };
      const turnBtn = h("button", {}, "Turn") as HTMLButtonElement;
      const area = h("div", { class: "viewer draw-area" });
      const hint = h("p", { class: "small muted" });
      const brushBtn = h("button", {}, "Brush");
      const eraserBtn = h("button", {}, "Eraser");
      const picker = h("input", { type: "color", value: color, class: "color-input" }) as HTMLInputElement;
      const quick = h("div", { class: "quick" });
      const sizeIn = h("input", { type: "range", min: "1", max: "30", step: "1", value: String(size) }) as HTMLInputElement;
      const sizeVal = h("span", { class: "small muted" });
      const undoBtn = h("button", {}, "Undo") as HTMLButtonElement;
      const clearBtn = h("button", { class: "danger" }, "Clear");
      const fillBtn = h("button", {}, "Fill with color");
      const bright = h("input", { type: "range", min: "0.02", max: "1", step: "0.01", value: "0.5" }) as HTMLInputElement;
      const brightVal = h("span", { class: "small muted" });
      const nameIn = h("input", { type: "text", placeholder: "Name (optional)", maxlength: "60" }) as HTMLInputElement;
      const savedBox = h("div");

      const persist = () => savePrefs({ color, size, view });
      const radius = () => size / 100;

      // ---- server state ------------------------------------------------------------------------
      function renderStatus(): void {
        const st = status;
        if (!connected || !st) {
          pill.textContent = "Connecting";
          pill.className = "pill unknown";
        } else if (st.live) {
          pill.textContent = "On the tree";
          pill.className = "pill ok";
        } else if (st.busy) {
          pill.textContent = `LEDs in use (${st.busy})`;
          pill.className = "pill warn";
        } else {
          pill.textContent = "Not on the tree";
          pill.className = "pill unknown";
        }
        showBtn.style.display = st && !st.live ? "" : "none";
        showBtn.disabled = !connected || !!st?.busy;
        undoBtn.disabled = !connected || !st?.undo;
      }

      function setColors(hexes: string[], leds?: number[]): void {
        if (!leds) {
          colors = new Uint8Array(hexes.length * 3);
          hexes.forEach((c, i) => colors.set(hexToRgb(c), 3 * i));
        } else {
          leds.forEach((i, k) => {
            if (3 * i + 2 < colors.length) colors.set(hexToRgb(hexes[k]), 3 * i);
          });
        }
        repaint();
      }

      let repaintQueued = false;
      function repaint(): void {
        if (repaintQueued) return;
        repaintQueued = true;
        requestAnimationFrame(() => {
          repaintQueued = false;
          if (view === "3d") viewer?.setColors(colors);
          else flat?.draw();
        });
      }

      function onMessage(m: any): void {
        if (m.type === "state") {
          setColors(m.colors);
          status = m;
          bright.value = String(m.brightness);
          brightVal.textContent = `${Math.round(m.brightness * 100)}%`;
        } else if (m.type === "update") {
          setColors(m.colors, m.leds);
          if (status) status.undo = m.undo;
        } else if (m.type === "canvas") {
          setColors(m.colors);
          if (status) status.undo = m.undo;
        } else if (m.type === "status") {
          const was = status;
          status = m;
          if (was?.live && !m.live && m.busy) show(`A ${m.busy} took over the LEDs. Your drawing is kept; tap Show on tree when it's done.`, "warn");
        } else if (m.type === "error") {
          show(m.message, "fail");
        }
        renderStatus();
      }

      // ---- strokes ----------------------------------------------------------------------------
      let pts: Point[] = [];
      let fresh = 0;
      let first = false;
      const space = (): Space => (view === "3d" ? "3d" : "uv");

      function flush(): void {
        clearTimeout(sendTimer);
        sendTimer = 0;
        if (!fresh) return;
        const sent = sock?.sendJson({ type: "stroke", space: space(), points: pts, radius: radius(), color, erase, start: first });
        if (sent) first = false;
        pts = pts.slice(-1);
        fresh = 0;
      }
      function addPoint(p: Point): void {
        pts.push(p);
        fresh++;
        if (!sendTimer) sendTimer = window.setTimeout(flush, SEND_MS);
      }
      function gap(): void { // the finger left the tree: the next point starts a new line
        flush();
        pts = [];
      }

      const pointers = new Set<number>();
      let painting = false;
      let lastXY = { x: 0, y: 0 };
      const cursorColor = () => (erase ? "#888888" : color);

      function pointAt(e: PointerEvent): Point | null {
        if (!shape) return null;
        if (view === "flat") return flat!.toUV(e.clientX, e.clientY);
        const ray = viewer!.rayAt(e.clientX, e.clientY);
        return shape.hit(ray.origin, ray.direction, radius() * shape.span);
      }
      function showCursor(p: Point | null): void {
        if (!shape) return;
        if (view === "3d") viewer?.setBrush(p as [number, number, number] | null, radius() * shape.span, cursorColor());
        else flat?.setCursor(p ? { u: ((p[0] % 1) + 1) % 1, v: p[1], radius: radius(), color: cursorColor() } : null);
      }

      function down(e: PointerEvent): void {
        pointers.add(e.pointerId);
        if (pointers.size > 1 || (view === "3d" && turn) || (e.pointerType === "mouse" && e.button !== 0)) {
          if (painting) { // a second finger: it's a turn or zoom, not paint
            painting = false;
            gap();
          }
          return;
        }
        if (!connected) {
          show("Not connected to the server yet.", "warn");
          return;
        }
        // Keep receiving the stroke's events when the finger leaves the view, so it always ends.
        try {
          (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
        } catch {
          /* pointer already gone */
        }
        painting = true;
        first = true;
        pts = [];
        fresh = 0;
        lastXY = { x: e.clientX, y: e.clientY };
        const p = pointAt(e);
        showCursor(p);
        if (p) addPoint(p);
      }
      function move(e: PointerEvent): void {
        if (!painting) {
          if (e.pointerType === "mouse" && !pointers.size) showCursor(pointAt(e));
          return;
        }
        if (Math.hypot(e.clientX - lastXY.x, e.clientY - lastXY.y) < 3) return;
        lastXY = { x: e.clientX, y: e.clientY };
        const p = pointAt(e);
        showCursor(p);
        if (p) addPoint(p);
        else gap();
      }
      function up(e: PointerEvent): void {
        pointers.delete(e.pointerId);
        if (painting) {
          painting = false;
          flush();
          pts = [];
        }
        if (e.pointerType !== "mouse") showCursor(null);
      }

      function attach(el: HTMLElement): void {
        el.addEventListener("pointerdown", down);
        el.addEventListener("pointermove", move);
        el.addEventListener("pointerup", up);
        el.addEventListener("pointercancel", up);
        el.addEventListener("pointerleave", (e) => {
          if (e.pointerType === "mouse") showCursor(null);
        });
      }

      // ---- views -------------------------------------------------------------------------------
      function setView(v: "3d" | "flat"): void {
        view = v;
        persist();
        viewer?.dispose();
        flat?.dispose();
        viewer = null;
        flat = null;
        area.replaceChildren();
        viewBtns["3d"].className = v === "3d" ? "seg active" : "seg";
        viewBtns.flat.className = v === "flat" ? "seg active" : "seg";
        turnBtn.style.display = v === "3d" ? "" : "none";
        if (!shape) return;
        if (v === "3d") {
          viewer = new TreeViewer(area);
          viewer.setMap(shape.map);
          viewer.setPaintMode(!turn);
          attach(viewer.element);
          hint.textContent = "One finger paints on the tree. Two fingers turn and zoom (or tap Turn). With a mouse: right-drag turns.";
        } else {
          flat = new UnwrapView(area, shape, () => colors);
          attach(flat.canvas);
          hint.textContent = "The tree's surface laid flat: across is around the tree (the edges join at the back), up is up.";
        }
        repaint();
      }
      viewBtns["3d"].onclick = () => setView("3d");
      viewBtns.flat.onclick = () => setView("flat");
      turnBtn.onclick = () => {
        turn = !turn;
        turnBtn.className = turn ? "seg active" : "seg";
        viewer?.setPaintMode(!turn);
      };

      // ---- tools ---------------------------------------------------------------------------------
      function renderTools(): void {
        brushBtn.className = erase ? "seg" : "seg active";
        eraserBtn.className = erase ? "seg active" : "seg";
        picker.value = color;
        sizeVal.textContent = `${size}% of the tree's height`;
        quick.replaceChildren(...QUICK.map((c) =>
          h("button", { class: `quick-c${c === color && !erase ? " active" : ""}`, style: { background: c }, title: c,
            onclick: () => setColor(c) })));
      }
      function setColor(c: string): void {
        color = c;
        erase = false;
        persist();
        renderTools();
      }
      brushBtn.onclick = () => { erase = false; renderTools(); };
      eraserBtn.onclick = () => { erase = true; renderTools(); };
      picker.oninput = () => setColor(picker.value);
      sizeIn.oninput = () => {
        size = Number(sizeIn.value);
        persist();
        renderTools();
      };
      const command = (m: object) => {
        if (!sock?.sendJson(m)) show("Not connected to the server yet.", "warn");
      };
      undoBtn.onclick = () => command({ type: "undo" });
      clearBtn.onclick = () => command({ type: "clear" });
      fillBtn.onclick = () => command({ type: "fill", color });
      showBtn.onclick = () => {
        show("");
        command({ type: "show" });
      };
      bright.oninput = () => {
        brightVal.textContent = `${Math.round(Number(bright.value) * 100)}%`;
        clearTimeout(brightTimer);
        brightTimer = window.setTimeout(() => {
          api("/api/lights", { body: { brightness: Number(bright.value) } }).catch((e) => show(fmtErr(e), "fail"));
        }, 80);
      };

      // ---- saved drawings ------------------------------------------------------------------------
      function renderSaved(list: Saved[]): void {
        savedBox.replaceChildren(...(list.length ? list.map((d) =>
          h("div", { class: "rec-row" },
            h("div", {}, h("div", {}, d.name),
              h("div", { class: "small muted" }, d.saved ? new Date(d.saved * 1000).toLocaleString() : "",
                shape && d.led_count !== shape.map.leds.length ? ` · made for ${d.led_count} LEDs` : "")),
            h("div", { class: "row" },
              h("button", { onclick: () => api(`/api/drawings/${d.id}/load`, { body: {} }).then(() => show(`Loaded "${d.name}".`)).catch((e) => show(fmtErr(e), "fail")) }, "Load"),
              h("button", { class: "danger", onclick: () => {
                if (!confirm(`Delete "${d.name}"?`)) return;
                api<{ drawings: Saved[] }>(`/api/drawings/${d.id}`, { method: "DELETE" }).then((r) => renderSaved(r.drawings)).catch((e) => show(fmtErr(e), "fail"));
              } }, "Delete"))))
          : [h("p", { class: "small muted" }, "No saved drawings yet.")]));
      }
      const saveBtn = h("button", { onclick: async () => {
        try {
          const r = await api<{ name: string; drawings: Saved[] }>("/api/drawings", { body: { name: nameIn.value } });
          nameIn.value = "";
          renderSaved(r.drawings);
          show(`Saved "${r.name}".`);
        } catch (e) {
          show(fmtErr(e), "fail");
        }
      } }, "Save");

      root.append(
        h("h1", {}, "Draw"),
        msg,
        h("div", { class: "card" },
          h("div", { class: "row" }, pill, showBtn, h("span", { class: "grow" }),
            h("div", { class: "segs" }, viewBtns["3d"], viewBtns.flat), turnBtn)),
        area,
        hint,
        h("div", { class: "card" },
          h("div", { class: "row" }, h("div", { class: "segs" }, brushBtn, eraserBtn), h("span", { class: "grow" }), undoBtn),
          h("label", {}, "Color"),
          h("div", { class: "row" }, picker, quick),
          h("label", { class: "slider-label" }, "Brush size ", sizeVal),
          sizeIn,
          h("div", { class: "row", style: { marginTop: "10px" } }, fillBtn, clearBtn),
        ),
        h("div", { class: "card" },
          h("label", {}, "Brightness ", brightVal),
          bright,
          h("p", { class: "small muted" }, "The drawing runs like an effect: the Schedule page and Home Assistant switch it on and off. Brightness is also capped by the current limit from Setup or WLED."),
        ),
        h("div", { class: "card" },
          h("h2", { style: { marginTop: "0" } }, "Saved drawings"),
          h("div", { class: "row" }, h("div", { class: "grow" }, nameIn), saveBtn),
          savedBox,
        ),
      );
      renderTools();
      renderStatus();

      api<TreeMap>("/api/maps/active").then((m) => {
        shape = new TreeShape(m);
        setView(view);
        sock = new LiveSocket("/ws/draw");
        sock.onstate = (open) => {
          connected = open;
          renderStatus();
        };
        sock.onmessage = (ev) => {
          if (typeof ev.data !== "string") return;
          try {
            onMessage(JSON.parse(ev.data));
          } catch (e) {
            console.error("draw", e);
          }
        };
        api<Saved[]>("/api/drawings").then(renderSaved).catch(() => {});
      }).catch(() => {
        area.replaceChildren(h("div", { class: "muted", style: { padding: "16px", color: "#9aa8b6" } },
          "No active map yet. Drawing needs a map of the tree: scan it first."));
        hint.textContent = "";
      });
    },
    unmount() {
      clearTimeout(brightTimer);
      clearTimeout(sendTimer);
      sock?.close();
      viewer?.dispose();
      flat?.dispose();
    },
  };
}
