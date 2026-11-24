// Schedule page: switch the tree on and off, and timers that do it at set times or relative to
// sunrise and sunset. The server runs the timers, so they work with no browser open.
import { api, fmtErr, h, type Page } from "../api";

type Timer = {
  id?: string; enabled: boolean; action: "on" | "off"; trigger: "time" | "sunrise" | "sunset";
  time: string; offset_min: number; days: number[]; effect: string | null; brightness: number | null;
};
type Settings = { enabled: boolean; timezone: string | null; latitude: number | null; longitude: number | null; timers: Timer[] };
type Upcoming = { at: string; action: "on" | "off"; timer: string; description: string };
type Status = {
  settings: Settings; timezone: string; timezone_source: string; now: string;
  sun: null | Record<"today" | "tomorrow", { date: string; sunrise: string | null; sunset: string | null }>;
  upcoming: Upcoming[]; problems: Record<string, string>; descriptions: Record<string, string>;
  last_fired: (Upcoming & { result: string }) | null;
};
type Lights = {
  on: boolean; effect: string; label: string; brightness: number; changed: number | null; source: string | null;
  busy: string | null; error: string | null;
};
type EffectInfo = { name: string; label: string; needs_map: boolean; category: string };

const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function browserTz(): string | null {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || null;
  } catch {
    return null;
  }
}

function fmtTime(iso: string | null, tz: string, withDay = false): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString([], {
      timeZone: tz, hour: "2-digit", minute: "2-digit", ...(withDay ? { weekday: "short", day: "numeric", month: "short" } : {}),
    });
  } catch {
    return iso;
  }
}

export function schedulePage(): Page {
  let refresh = 0;
  return {
    mount(root) {
      const msg = h("div");
      const show = (text: string, cls = "ok") => msg.replaceChildren(text ? h("div", { class: `notice ${cls}` }, text) : "");
      let status: Status | null = null;
      let effects: EffectInfo[] = [];
      let hasMap = false;

      // ---- lights ------------------------------------------------------------------------
      const lightState = h("div");
      const effectSel = h("select") as HTMLSelectElement;
      const bright = h("input", { type: "range", min: "0.02", max: "1", step: "0.01", value: "0.5" }) as HTMLInputElement;
      const brightVal = h("span", { class: "small muted" });
      let lightsOn = false;
      let brightTimer = 0;

      function renderLights(l: Lights): void {
        lightsOn = l.on;
        if (effectSel.value !== l.effect && [...effectSel.options].some((o) => o.value === l.effect)) effectSel.value = l.effect;
        if (document.activeElement !== bright) bright.value = String(l.brightness);
        brightVal.textContent = `${Math.round(Number(bright.value) * 100)}%`;
        const since = l.changed ? new Date(l.changed * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" }) : null;
        lightState.replaceChildren(
          h("div", { class: "row" },
            h("span", { class: `pill ${l.on ? "ok" : "unknown"}` }, l.on ? "On" : "Off"),
            h("span", {}, l.on ? `${l.label} at ${Math.round(l.brightness * 100)}%` : `Next "on" runs ${l.label}`),
          ),
          since ? h("div", { class: "small muted" }, `Switched ${l.on ? "on" : "off"} ${since}${l.source ? ` (${l.source})` : ""}`) : "",
          l.busy ? h("div", { class: "notice warn" }, `A scan or calibration is using the LEDs (${l.busy}). Timers wait until it's done.`) : "",
          l.error ? h("div", { class: "notice warn" }, l.error) : "",
        );
      }

      async function loadLights(): Promise<void> {
        renderLights(await api<Lights>("/api/lights"));
      }

      async function setLights(body: Record<string, unknown>): Promise<void> {
        try {
          renderLights(await api<Lights>("/api/lights", { body }));
          show("");
        } catch (e) {
          show(fmtErr(e), "fail");
        }
      }

      effectSel.onchange = () => { if (lightsOn) setLights({ on: true, effect: effectSel.value }); };
      bright.oninput = () => {
        brightVal.textContent = `${Math.round(Number(bright.value) * 100)}%`;
        clearTimeout(brightTimer);
        brightTimer = window.setTimeout(() => setLights({ brightness: Number(bright.value) }), 120);
      };

      // ---- location and time zone ------------------------------------------------------------
      const enabled = h("input", { type: "checkbox" }) as HTMLInputElement;
      const tzInput = h("input", { placeholder: "e.g. Europe/Berlin", list: "tz-list", autocomplete: "off" }) as HTMLInputElement;
      const tzList = h("datalist", { id: "tz-list" });
      try {
        const zones: string[] = (Intl as any).supportedValuesOf?.("timeZone") ?? [];
        tzList.append(...zones.map((z) => h("option", { value: z })));
      } catch {
        /* older browser: free text */
      }
      const lat = h("input", { type: "number", step: "0.0001", min: "-90", max: "90", inputmode: "decimal", placeholder: "52.5200" }) as HTMLInputElement;
      const lon = h("input", { type: "number", step: "0.0001", min: "-180", max: "180", inputmode: "decimal", placeholder: "13.4050" }) as HTMLInputElement;
      const tzNote = h("div", { class: "small muted" });
      const sunBox = h("div", { class: "small" });

      async function useDeviceLocation(): Promise<void> {
        if (!navigator.geolocation) return show("This browser can't tell its location. Enter it instead.", "warn");
        show("Asking for this device's location...", "ok");
        navigator.geolocation.getCurrentPosition(
          (p) => {
            lat.value = p.coords.latitude.toFixed(4);
            lon.value = p.coords.longitude.toFixed(4);
            if (!tzInput.value) tzInput.value = browserTz() ?? "";
            show("Location filled in. Press Save.");
          },
          (err) => show(`Couldn't get the location (${err.message}). Enter it instead.`, "warn"),
          { enableHighAccuracy: false, timeout: 15000 },
        );
      }

      async function saveLocation(): Promise<void> {
        const num = (el: HTMLInputElement) => (el.value.trim() === "" ? null : Number(el.value));
        await saveSchedule({ enabled: enabled.checked, timezone: tzInput.value.trim() || null, latitude: num(lat), longitude: num(lon) }, "Saved.");
      }

      // ---- timers ------------------------------------------------------------------------
      const timersBox = h("div");
      const upcomingBox = h("div");
      const editor = h("div");

      async function saveSchedule(patch: Partial<Settings>, ok = ""): Promise<boolean> {
        try {
          status = await api<Status>("/api/schedule", { method: "PUT", body: patch });
          render();
          show(ok);
          return true;
        } catch (e) {
          show(fmtErr(e), "fail");
          return false;
        }
      }

      function effectOptions(selected: string | null, keepLabel: string): HTMLOptionElement[] {
        const opts = [h("option", { value: "" }, keepLabel)];
        for (const e of effects) {
          if (e.needs_map && !hasMap && e.name !== selected) continue;
          opts.push(h("option", { value: e.name, selected: e.name === selected }, e.label));
        }
        return opts;
      }

      function edit(t: Timer | null): void {
        const isNew = t === null;
        const cur: Timer = t ? { ...t, days: [...t.days] } : {
          enabled: true, action: "on", trigger: "sunset", time: "17:00", offset_min: 0, days: [0, 1, 2, 3, 4, 5, 6], effect: null, brightness: null,
        };
        const action = h("select", {}, h("option", { value: "on" }, "Turn on"), h("option", { value: "off" }, "Turn off")) as HTMLSelectElement;
        const trigger = h("select", {}, h("option", { value: "time" }, "At a time"), h("option", { value: "sunset" }, "At sunset"),
          h("option", { value: "sunrise" }, "At sunrise")) as HTMLSelectElement;
        const time = h("input", { type: "time", value: cur.time }) as HTMLInputElement;
        const offset = h("input", { type: "number", step: "5", min: "-720", max: "720", value: String(cur.offset_min) }) as HTMLInputElement;
        const eff = h("select", {}, ...effectOptions(cur.effect, "The last effect used")) as HTMLSelectElement;
        const setBright = h("input", { type: "checkbox", checked: cur.brightness !== null }) as HTMLInputElement;
        const tb = h("input", { type: "range", min: "0.02", max: "1", step: "0.01", value: String(cur.brightness ?? 0.5) }) as HTMLInputElement;
        const tbVal = h("span", { class: "small muted" }, `${Math.round(Number(tb.value) * 100)}%`);
        tb.oninput = () => { tbVal.textContent = `${Math.round(Number(tb.value) * 100)}%`; setBright.checked = true; };
        const days = DAY_NAMES.map((d, i) => {
          const cb = h("input", { type: "checkbox", checked: cur.days.includes(i) }) as HTMLInputElement;
          return { cb, el: h("label", { class: "row", style: { display: "inline-flex", marginRight: "10px" } }, cb, d) };
        });
        action.value = cur.action;
        trigger.value = cur.trigger;
        const timeRow = h("div", {}, h("label", {}, "Time"), time);
        const offRow = h("div", {}, h("label", {}, "Offset in minutes (negative: before)"), offset);
        const onOnly = h("div", {},
          h("label", {}, "Effect"), eff,
          h("label", {}, h("span", { class: "row" }, setBright, "Set brightness")),
          h("div", { class: "row" }, h("div", { class: "grow" }, tb), tbVal));
        const sync = () => {
          timeRow.style.display = trigger.value === "time" ? "" : "none";
          offRow.style.display = trigger.value === "time" ? "none" : "";
          onOnly.style.display = action.value === "on" ? "" : "none";
        };
        action.onchange = trigger.onchange = sync;
        sync();

        async function save(): Promise<void> {
          const next: Timer = {
            ...cur, action: action.value as Timer["action"], trigger: trigger.value as Timer["trigger"],
            time: time.value || "00:00", offset_min: Math.round(Number(offset.value) || 0),
            days: days.flatMap((d, i) => (d.cb.checked ? [i] : [])),
            effect: action.value === "on" ? eff.value || null : null,
            brightness: action.value === "on" && setBright.checked ? Number(tb.value) : null,
          };
          if (!next.days.length) return show("Choose at least one day.", "warn");
          const timers = [...(status?.settings.timers ?? [])];
          if (isNew) timers.push(next);
          else timers[timers.findIndex((x) => x.id === cur.id)] = next;
          if (await saveSchedule({ timers }, isNew ? "Timer added." : "Timer saved.")) editor.replaceChildren();
        }

        editor.replaceChildren(h("div", { class: "card" },
          h("h2", {}, isNew ? "New timer" : "Edit timer"),
          h("label", {}, "Action"), action,
          h("label", {}, "When"), trigger,
          timeRow, offRow,
          h("label", {}, "Days"), h("div", {}, ...days.map((d) => d.el)),
          onOnly,
          h("div", { class: "row", style: { marginTop: "10px" } },
            h("button", { class: "primary", onclick: save }, "Save"),
            h("button", { onclick: () => editor.replaceChildren() }, "Cancel")),
        ));
        editor.scrollIntoView({ behavior: "smooth", block: "start" });
      }

      function render(): void {
        if (!status) return;
        const s = status.settings;
        const tz = status.timezone;
        if (document.activeElement !== tzInput) tzInput.value = s.timezone ?? "";
        if (document.activeElement !== lat) lat.value = s.latitude === null ? "" : String(s.latitude);
        if (document.activeElement !== lon) lon.value = s.longitude === null ? "" : String(s.longitude);
        enabled.checked = s.enabled;
        const btz = browserTz();
        tzNote.replaceChildren(
          `Timers use ${tz}${status.timezone_source === "setting" ? "" : status.timezone_source === "TZ" ? " (from the TZ variable)" : " (no time zone set)"}. Now: ${fmtTime(status.now, tz, true)}.`,
          btz && btz !== tz ? h("div", {}, h("button", { class: "small", onclick: () => { tzInput.value = btz; saveLocation(); } }, `Use ${btz}`)) : "",
        );
        sunBox.replaceChildren(status.sun
          ? h("div", {}, ...(["today", "tomorrow"] as const).map((k) =>
              h("div", {}, `${k === "today" ? "Today" : "Tomorrow"}: sunrise ${fmtTime(status!.sun![k].sunrise, tz)}, sunset ${fmtTime(status!.sun![k].sunset, tz)}`)))
          : h("div", { class: "muted" }, "Enter the location for sunrise and sunset."));

        timersBox.replaceChildren(s.timers.length
          ? h("ul", { class: "checks" }, ...s.timers.map((t) => {
              const toggle = h("input", { type: "checkbox", checked: t.enabled, title: "Enabled" }) as HTMLInputElement;
              toggle.onchange = () => saveSchedule({ timers: s.timers.map((x) => (x.id === t.id ? { ...x, enabled: toggle.checked } : x)) });
              const effectLabel = t.action === "on" && t.effect ? effects.find((e) => e.name === t.effect)?.label ?? t.effect : null;
              const extra = [effectLabel, t.action === "on" && t.brightness !== null ? `${Math.round(t.brightness * 100)}%` : null].filter(Boolean).join(", ");
              return h("li", { style: { flexDirection: "column", gap: "4px" } },
                h("div", { class: "row", style: { flexWrap: "nowrap" } },
                  toggle,
                  h("span", { class: "grow" }, status!.descriptions[t.id!] ?? "", extra ? h("span", { class: "muted" }, ` — ${extra}`) : "")),
                h("div", { class: "row" },
                  h("button", { onclick: () => edit(t) }, "Edit"),
                  h("button", { class: "danger", onclick: () => saveSchedule({ timers: s.timers.filter((x) => x.id !== t.id) }, "Timer deleted.") }, "Delete")),
                status!.problems[t.id!] ? h("div", { class: "notice warn" }, status!.problems[t.id!]) : "");
            }))
          : h("div", { class: "muted" }, "No timers yet."));

        upcomingBox.replaceChildren(
          !s.enabled ? h("div", { class: "notice warn" }, "The schedule is paused: these won't run until it's turned on again.") : "",
          status.upcoming.length
            ? h("ul", { class: "checks" }, ...status.upcoming.map((e) =>
                h("li", {}, h("span", { class: `pill ${e.action === "on" ? "ok" : "unknown"}` }, e.action === "on" ? "On" : "Off"),
                  h("span", {}, ` ${fmtTime(e.at, tz, true)} `), h("span", { class: "small muted" }, e.description))))
            : h("div", { class: "muted" }, "Nothing in the next week."),
          status.last_fired
            ? h("div", { class: "small muted", style: { marginTop: "8px" } },
                `Last: ${status.last_fired.action} at ${fmtTime(status.last_fired.at, tz, true)} — ${status.last_fired.result}`)
            : "",
        );
      }

      async function loadAll(): Promise<void> {
        const [st, cat, state] = await Promise.all([
          api<Status>("/api/schedule"), api<{ effects: EffectInfo[] }>("/api/effects"), api("/api/state")]);
        status = st;
        effects = cat.effects;
        hasMap = !!state.active_map;
        effectSel.replaceChildren(...effects.filter((e) => !e.needs_map || hasMap).map((e) => h("option", { value: e.name }, e.label)));
        render();
        await loadLights();
      }

      root.append(
        h("h1", {}, "Schedule"),
        msg,
        h("div", { class: "card" },
          h("h2", {}, "Lights"),
          lightState,
          h("label", {}, "Effect"), effectSel,
          h("label", {}, "Brightness"),
          h("div", { class: "row" }, h("div", { class: "grow" }, bright), brightVal),
          h("div", { class: "row", style: { marginTop: "10px" } },
            h("button", { class: "primary", onclick: () => setLights({ on: true, effect: effectSel.value, brightness: Number(bright.value) }) }, "On"),
            h("button", { class: "danger", onclick: () => setLights({ on: false }) }, "Off")),
          h("p", { class: "small muted" }, "Off stops the effect and turns WLED off. Colors, palette and sliders come from the Effects page and are remembered."),
        ),
        h("div", { class: "card" },
          h("h2", {}, "Timers"),
          h("label", {}, h("span", { class: "row" }, enabled, "Run the timers")),
          timersBox,
          h("div", { class: "row", style: { marginTop: "10px" } }, h("button", { class: "primary", onclick: () => edit(null) }, "Add timer")),
        ),
        editor,
        h("div", { class: "card" }, h("h2", {}, "Coming up"), upcomingBox),
        h("div", { class: "card" },
          h("h2", {}, "Time zone and location"),
          h("label", {}, "Time zone"), tzInput, tzList, tzNote,
          h("label", {}, "Latitude (north positive)"), lat,
          h("label", {}, "Longitude (east positive)"), lon,
          h("div", { class: "row", style: { marginTop: "10px" } },
            h("button", { class: "primary", onclick: saveLocation }, "Save"),
            h("button", { onclick: useDeviceLocation }, "Use this device's location")),
          h("div", { style: { marginTop: "10px" } }, sunBox),
          h("p", { class: "small muted" }, "The location is only used to work out sunrise and sunset, on this server."),
        ),
      );
      enabled.onchange = () => saveSchedule({ enabled: enabled.checked }, enabled.checked ? "Timers on." : "Timers paused.");
      loadAll().catch((e) => show(fmtErr(e), "fail"));
      refresh = window.setInterval(() => {
        loadLights().catch(() => {});
        api<Status>("/api/schedule").then((st) => { status = st; render(); }).catch(() => {});
      }, 15000);
    },
    unmount() {
      clearInterval(refresh);
    },
  };
}
