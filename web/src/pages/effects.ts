// Effects page: the 3D volumetric and surface-pattern library, WLED style. Pick an effect, set its
// sliders, three colors and a palette; changes apply live to the running effect.
import { api, ApiError, fmtErr, h, LiveSocket, type Page } from "../api";
import { TreeViewer, type TreeMap } from "../viewer";

type Slider = "speed" | "intensity" | "c1" | "c2" | "c3";
const SLIDERS: Slider[] = ["speed", "intensity", "c1", "c2", "c3"];
type Params = Record<Slider, number> & { palette: string; colors: string[] };
type EffectInfo = {
  name: string; label: string; category: "3d" | "surface" | "basic"; description: string; needs_map: boolean;
  controls: Partial<Record<Slider, string>>; color_slots: string[]; default_palette: string; uses_palette: boolean;
  defaults: Params;
};
type PaletteInfo = { id: string; name: string; dynamic: boolean; wraps?: boolean; stops?: [number, string][] };
type Current = { effect: string; params: Params; brightness: number } | null;

const CATEGORIES: [EffectInfo["category"], string, string][] = [
  ["3d", "3D volumetric", "Shapes and fields that fill the tree's volume, inner LEDs included."],
  ["surface", "Surface patterns", "Patterns painted on the tree's outer surface. Depth fade hides the inner LEDs."],
  ["basic", "Basic", "Work along the string; no map needed."],
];
const QUICK = ["#ff0000", "#ff6000", "#ffa000", "#ffd000", "#ffffff", "#ffe0a0", "#00ff00", "#00c080", "#00ffff",
  "#0060ff", "#0000ff", "#8000ff", "#ff00c0", "#ff80a0", "#000000"];
const STORE_KEY = "treelights.effects";
// Palettes built from the color slots, mirroring the server's palettes.py.
const COLOR_PALETTES = new Set(["color1", "colors12", "colorgrad", "colorsonly"]);

function load(): any {
  try {
    return JSON.parse(localStorage.getItem(STORE_KEY) ?? "null");
  } catch {
    return null;
  }
}
function save(v: unknown): void {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(v));
  } catch {
    /* storage unavailable */
  }
}

function gradientCss(p: PaletteInfo | undefined, colors: string[], all: Map<string, PaletteInfo>, effect?: EffectInfo): string {
  if (!p) return "#000";
  if (p.id === "default" && effect) return gradientCss(all.get(effect.default_palette), colors, all);
  if (p.id === "random") return "linear-gradient(90deg,#f04,#fa0,#0d6,#08f,#a0f,#f04)";
  const [c1, c2, c3] = colors;
  if (p.id === "color1") return c1;
  if (p.id === "colors12") return `linear-gradient(90deg,${c1} 0 50%,${c2} 50% 100%)`;
  if (p.id === "colorgrad") return `linear-gradient(90deg,${c1},${c2},${c3})`;
  if (p.id === "colorsonly") return `linear-gradient(90deg,${c1} 0 33.3%,${c2} 33.3% 66.6%,${c3} 66.6% 100%)`;
  const stops = (p.stops ?? []).map(([pos, c]) => `${c} ${((pos / 255) * 100).toFixed(1)}%`);
  if (p.wraps && p.stops?.length) stops.push(`${p.stops[0][1]} 100%`);
  return `linear-gradient(90deg,${stops.join(",")})`;
}

export function effectsPage(): Page {
  let viewer: TreeViewer | null = null;
  let preview: LiveSocket | null = null;
  let timer = 0;
  return {
    mount(root) {
      const msg = h("div");
      const show = (text: string, cls = "ok") => msg.replaceChildren(text ? h("div", { class: `notice ${cls}` }, text) : "");
      const mapWarning = h("div");
      const viewerBox = h("div", { class: "viewer compact" });
      const powerBtn = h("button", { class: "primary" }, "Start") as HTMLButtonElement;
      const runState = h("span", { class: "pill unknown" }, "Off");
      const bright = h("input", { type: "range", min: "0.02", max: "1", step: "0.01", value: "0.5" }) as HTMLInputElement;
      const brightVal = h("span", { class: "small muted" });
      const fxFilter = h("input", { type: "search", placeholder: "Search effects" }) as HTMLInputElement;
      const fxBox = h("div");
      const fxTitle = h("h2", { class: "fx-title" });
      const fxDesc = h("p", { class: "small muted" });
      const sliderBox = h("div");
      const colorCard = h("div", { class: "card" });
      const palFilter = h("input", { type: "search", placeholder: "Search palettes" }) as HTMLInputElement;
      const palBox = h("div", { class: "pal-list" });
      const palCard = h("div", { class: "card" }, h("h2", {}, "Palette"), palFilter, palBox);

      let effects: EffectInfo[] = [];
      const byName = new Map<string, EffectInfo>();
      const palettes = new Map<string, PaletteInfo>();
      let hasMap = false;
      let running = false;
      let slot = 0;
      let cur: { effect: string; params: Params; brightness: number } | null = null;

      const effect = () => byName.get(cur!.effect)!;

      function persist(): void {
        if (cur) save(cur);
      }

      function setRunning(on: boolean): void {
        running = on;
        powerBtn.textContent = on ? "Stop" : "Start";
        powerBtn.className = on ? "danger" : "primary";
        runState.textContent = on ? "Running" : "Off";
        runState.className = `pill ${on ? "ok" : "unknown"}`;
      }

      async function start(): Promise<void> {
        if (!cur) return;
        try {
          const r = await api<{ effect: string }>("/api/test/start",
            { body: { effect: cur.effect, brightness: cur.brightness, params: cur.params } });
          setRunning(true);
          show(r.effect !== cur.effect ? "There's no active map yet, so the index chase runs instead. Map the tree first." : "");
        } catch (e) {
          setRunning(false);
          show(fmtErr(e), "fail");
        }
      }

      async function stop(): Promise<void> {
        await api("/api/test/stop", { body: {} }).catch(() => {});
        setRunning(false);
        show("Stopped. WLED returns to its own effect in a few seconds.");
      }

      // Send changes to the running effect, at most every 60 ms while a slider is dragged.
      function changed(): void {
        persist();
        if (!running) return;
        clearTimeout(timer);
        timer = window.setTimeout(async () => {
          try {
            await api("/api/effects/params", { body: { params: cur!.params, brightness: cur!.brightness } });
          } catch (e) {
            if (e instanceof ApiError && e.status === 409) {
              setRunning(false);
              show("The effect stopped (another task took over the LEDs). Press Start to run it again.", "warn");
            } else show(fmtErr(e), "fail");
          }
        }, 60);
      }

      function renderEffects(): void {
        const q = fxFilter.value.trim().toLowerCase();
        fxBox.replaceChildren(...CATEGORIES.map(([cat, title, blurb]) => {
          const list = effects.filter((e) => e.category === cat && (!q || e.label.toLowerCase().includes(q)));
          if (!list.length) return "";
          return h("div", { class: "fx-group" },
            h("div", { class: "fx-cat" }, title, h("span", { class: "small muted" }, ` · ${blurb}`)),
            h("div", { class: "fx-grid" }, ...list.map((e) => {
              const off = e.needs_map && !hasMap;
              return h("button", {
                class: `fx-item${cur?.effect === e.name ? " active" : ""}`,
                title: off ? "Needs an active map" : e.description,
                disabled: off,
                onclick: () => selectEffect(e.name),
              }, e.label);
            })));
        }));
      }

      function selectEffect(name: string): void {
        const e = byName.get(name);
        if (!e || !cur) return;
        // Like WLED: an effect's own slider defaults and palette apply; the colors are kept.
        cur = { ...cur, effect: name, params: { ...e.defaults, colors: cur.params.colors } };
        renderEffects();
        renderSliders();
        renderColors();
        renderPalettes();
        persist();
        void start();
      }

      const fxSelect = h("select", { "aria-label": "Effect", onchange: () => selectEffect(fxSelect.value) }) as HTMLSelectElement;

      function renderSelect(): void {
        fxSelect.replaceChildren(...CATEGORIES.map(([cat, title]) => {
          const g = h("optgroup", { label: title });
          for (const e of effects.filter((x) => x.category === cat)) {
            g.append(h("option", { value: e.name, disabled: e.needs_map && !hasMap }, e.label));
          }
          return g;
        }));
        if (cur) fxSelect.value = cur.effect;
      }

      function renderSliders(): void {
        const e = effect();
        fxSelect.value = e.name;
        fxTitle.textContent = e.label;
        fxDesc.textContent = e.description;
        const rows = SLIDERS.filter((k) => e.controls[k]).map((k) => {
          const val = h("span", { class: "small muted" }, String(cur!.params[k]));
          const input = h("input", { type: "range", min: "0", max: "255", step: "1", value: String(cur!.params[k]) }) as HTMLInputElement;
          input.oninput = () => {
            cur!.params[k] = Number(input.value);
            val.textContent = input.value;
            changed();
          };
          return h("div", { class: "slider" }, h("label", {}, e.controls[k]!, " ", val), input);
        });
        sliderBox.replaceChildren(...(rows.length ? rows : [h("p", { class: "small muted" }, "This effect has no sliders.")]));
      }

      function renderColors(): void {
        const e = effect();
        const usesColorPal = COLOR_PALETTES.has(cur!.params.palette);
        const n = usesColorPal ? 3 : Math.max(e.color_slots.length, 1);
        const names = [0, 1, 2].map((i) => e.color_slots[i] ?? `Color ${i + 1}`);
        if (slot >= n) slot = 0;
        const picker = h("input", { type: "color", value: cur!.params.colors[slot], class: "color-input" }) as HTMLInputElement;
        const setColor = (c: string) => {
          cur!.params.colors[slot] = c;
          picker.value = c;
          for (const [i, b] of slots.entries()) (b.firstChild as HTMLElement).style.background = cur!.params.colors[i];
          renderPalettes();
          changed();
        };
        picker.oninput = () => setColor(picker.value);
        const slots = [0, 1, 2].slice(0, n).map((i) =>
          h("button", { class: `color-slot${i === slot ? " active" : ""}`, onclick: () => { slot = i; renderColors(); } },
            h("span", { class: "dot", style: { background: cur!.params.colors[i] } }), names[i]));
        colorCard.replaceChildren(
          h("h2", {}, "Colors"),
          h("div", { class: "row" }, ...slots),
          h("label", {}, `Pick ${names[slot].toLowerCase()}`),
          h("div", { class: "row" }, picker,
            h("div", { class: "quick" }, ...QUICK.map((c) =>
              h("button", { class: "quick-c", style: { background: c }, title: c, onclick: () => setColor(c) })))),
          e.uses_palette && !usesColorPal
            ? h("p", { class: "small muted" }, "Most effects take their colors from the palette. Choose a * Color palette to use these colors throughout.")
            : "",
        );
      }

      function renderPalettes(): void {
        const e = effect();
        palCard.style.display = e.uses_palette ? "" : "none";
        const q = palFilter.value.trim().toLowerCase();
        palBox.replaceChildren(...[...palettes.values()]
          .filter((p) => !q || p.name.toLowerCase().includes(q))
          .map((p) => h("button", {
            class: `pal-item${cur!.params.palette === p.id ? " active" : ""}`,
            onclick: () => {
              cur!.params.palette = p.id;
              renderPalettes();
              renderColors();
              changed();
            },
          },
          h("span", { class: "pal-swatch", style: { background: gradientCss(p, cur!.params.colors, palettes, e) } }),
          h("span", { class: "pal-name" }, p.id === "default" ? `Default (${palettes.get(e.default_palette)?.name ?? ""})` : p.name))));
      }

      powerBtn.onclick = () => (running ? stop() : start());
      bright.oninput = () => {
        cur!.brightness = Number(bright.value);
        brightVal.textContent = `${Math.round(cur!.brightness * 100)}%`;
        changed();
      };
      fxFilter.oninput = renderEffects;
      palFilter.oninput = renderPalettes;

      root.append(
        h("h1", {}, "Effects"),
        mapWarning,
        msg,
        viewerBox,
        h("div", { class: "card" },
          h("div", { class: "row" }, powerBtn, runState, h("span", { class: "grow" })),
          h("label", {}, "Brightness ", brightVal),
          bright,
          h("p", { class: "small muted" }, "Brightness is also capped by the current limit from Setup or WLED."),
        ),
        h("div", { class: "card" }, h("div", { class: "row" }, h("div", { class: "grow" }, fxTitle), fxSelect), fxDesc, sliderBox),
        colorCard,
        palCard,
        h("div", { class: "card" }, h("h2", {}, "Effect"), fxFilter, fxBox),
      );

      viewer = new TreeViewer(viewerBox);
      api("/api/state").then((s) => {
        if (s.map_warning) mapWarning.replaceChildren(h("div", { class: "notice warn" }, s.map_warning));
      }).catch(() => {});

      const mapP = api<TreeMap>("/api/maps/active").then((m) => {
        hasMap = true;
        try {
          viewer!.setMap(m);
        } catch (e) {
          console.error("preview", e);
        }
      }).catch(() => {
        hasMap = false;
        viewerBox.replaceChildren(h("div", { class: "muted", style: { padding: "16px", color: "#9aa8b6" } },
          "No active map yet. 3D and surface effects need one; the Basic effects work now."));
      });

      Promise.all([api<{ effects: EffectInfo[]; palettes: PaletteInfo[]; current: Current }>("/api/effects"), mapP]).then(([cat]) => {
        effects = cat.effects;
        for (const e of effects) byName.set(e.name, e);
        for (const p of cat.palettes) palettes.set(p.id, p);
        const saved = load();
        if (cat.current && byName.has(cat.current.effect)) {
          cur = { ...cat.current, params: { ...cat.current.params, colors: [...cat.current.params.colors] } };
          setRunning(true);
        } else if (saved && byName.has(saved.effect) && saved.params) {
          const e = byName.get(saved.effect)!;
          cur = { effect: saved.effect, brightness: Number(saved.brightness) || 0.5,
            params: { ...e.defaults, ...saved.params, colors: [...(saved.params.colors ?? e.defaults.colors)] } };
        } else {
          const first = effects.find((e) => !e.needs_map || hasMap) ?? effects[0];
          cur = { effect: first.name, brightness: 0.5, params: { ...first.defaults, colors: [...first.defaults.colors] } };
        }
        if (effect().needs_map && !hasMap) {
          const b = effects.find((e) => !e.needs_map)!;
          cur = { ...cur, effect: b.name, params: { ...b.defaults, colors: cur.params.colors } };
        }
        bright.value = String(cur.brightness);
        brightVal.textContent = `${Math.round(cur.brightness * 100)}%`;
        renderSelect();
        renderEffects();
        renderSliders();
        renderColors();
        renderPalettes();
        preview = new LiveSocket("/ws/preview");
        preview.onmessage = (ev) => {
          if (!(ev.data instanceof ArrayBuffer) || !hasMap) return;
          // Frames arrive as sent to the LEDs (gamma 2.2, scaled by brightness). Undo both so the
          // preview shows the effect's colors as designed, whatever the brightness.
          const raw = new Uint8Array(ev.data);
          const k = 1 / Math.max(cur?.brightness ?? 1, 0.02);
          const vis = new Uint8Array(raw.length);
          for (let i = 0; i < raw.length; i++) vis[i] = Math.min(255, Math.round(255 * Math.pow(Math.min(1, (raw[i] / 255) * k), 1 / 2.2)));
          viewer?.setColors(vis);
        };
      }).catch((e) => show(fmtErr(e), "fail"));
    },
    unmount() {
      clearTimeout(timer);
      preview?.close();
      viewer?.dispose();
    },
  };
}
