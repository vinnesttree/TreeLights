import { api, fmtErr, h, type Page } from "../api";
import { STATUS_COLORS, TreeViewer, type TreeMap } from "../viewer";

export function mapPage(): Page {
  let viewer: TreeViewer | null = null;
  return {
    mount(root) {
      const msg = h("div");
      const versions = h("div");
      const report = h("div");
      const viewerBox = h("div", { class: "viewer" });
      const sel = h("div", { class: "muted small" }, "Tap an LED to select it.");
      const editsBox = h("div", { class: "small" });
      const rescanBox = h("div");
      let map: TreeMap | null = null;
      let overrides: any = {};
      let selected: number | null = null;

      const show = (text: string, cls = "ok") => msg.replaceChildren(h("div", { class: `notice ${cls}` }, text));
      const unit = () => (map?.units === "cm" ? "cm" : "tree heights");
      const fmt = (v: number | null | undefined, d = 1) => (v === null || v === undefined ? "–" : Number(v).toFixed(d));

      viewer = new TreeViewer(viewerBox);
      viewer.onSelect = (i) => selectLed(i);
      viewer.onDragEnd = async (i, p) => {
        overrides.positions = { ...(overrides.positions ?? {}), [i]: p.map((v) => Math.round(v * 1000) / 1000) };
        await saveOverrides(`LED ${i} moved. Re-solve to apply.`);
        viewer?.stopDrag();
      };

      async function loadVersions(preferId?: string) {
        const list = await api<any[]>("/api/maps");
        if (!list.length) {
          versions.replaceChildren(h("div", { class: "muted" }, "No maps yet. Capture views, then solve on the Control page."));
          return;
        }
        const pick = preferId ?? list.find((m) => m.active)?.id ?? list[0].id;
        const select = h("select", { onchange: (e: Event) => loadMap((e.target as HTMLSelectElement).value) },
          ...list.map((m) => h("option", { value: m.id, selected: m.id === pick },
            `${m.id}${m.active ? " (active)" : ""}${m.blocked ? " — blocked" : ""} · ${m.measured !== null ? Math.round(100 * m.measured) + "% measured" : ""}`)));
        versions.replaceChildren(select);
        await loadMap(pick);
      }

      async function loadMap(id: string) {
        map = await api<TreeMap>(`/api/maps/${id}`);
        overrides = await api(`/api/scans/${map.scan}/overrides`).catch(() => ({}));
        viewer!.setMap(map);
        renderReport();
        renderEdits();
        renderRescan();
        selectLed(null);
      }

      function renderReport() {
        if (!map) return;
        const q = map.quality;
        const c = q.counts;
        report.replaceChildren(
          h("table", {},
            h("tr", {}, h("th", {}, "Measured (2+ views)"), h("td", {}, `${c.measured} of ${map.leds.length}`)),
            h("tr", {}, h("th", {}, "Single view / estimated / unknown"), h("td", {}, `${c.single_view} / ${c.estimated} / ${c.unknown}`)),
            h("tr", {}, h("th", {}, "Camera segments used"), h("td", {}, `${q.registered_segments} of ${q.segments_total}`)),
            h("tr", {}, h("th", {}, "Median reprojection error"), h("td", {}, `${fmt(q.median_err_px, 2)} px`)),
            h("tr", {}, h("th", {}, "Learned LED spacing bound"), h("td", {}, `${fmt(map.chain.base_link, 1)} ${unit()} (from ${map.chain.trusted_links} trusted links)`)),
            h("tr", {}, h("th", {}, "Long links (connectors)"), h("td", {}, map.chain.long_links.length
              ? map.chain.long_links.map((l) => `${l.from}→${l.from + 1}${l.kind === "long_predicted" ? "*" : ""}`).join(", ") + (map.chain.period ? ` (every ${map.chain.period})` : "")
              : "none")),
            h("tr", {}, h("th", {}, "Breaks"), h("td", {}, map.chain.breaks.length ? map.chain.breaks.map((b) => `${b.from}→${b.from + 1} (${b.reason.replace("break_", "")})`).join(", ") : "none")),
            h("tr", {}, h("th", {}, "Up direction"), h("td", {}, `agreed: ${q.up_axis.used.join(", ")}${q.up_axis.disagree.length ? `; disagree: ${q.up_axis.disagree.join(", ")}` : ""}`)),
          ),
          ...q.warnings.map((w: string) => h("div", { class: "notice warn small" }, w)),
          q.activation_blocked ? h("div", { class: "notice fail" }, `Can't activate: ${q.activation_blocked}`) : "",
        );
      }

      function selectLed(i: number | null) {
        selected = i;
        if (map) renderRescan();
        viewer?.select(i);
        viewer?.stopDrag();
        if (i === null || !map) {
          sel.replaceChildren(h("span", { class: "muted small" }, "Tap an LED to select it."));
          return;
        }
        const l = map.leds[i];
        sel.replaceChildren(
          h("div", {}, h("b", {}, `LED ${i}`), " ", h("span", { class: "pill", style: { background: STATUS_COLORS[l.status], color: "#000" } }, l.status.replace("_", " "))),
          h("div", { class: "small muted" }, `Position ${l.p ? l.p.map((v) => v.toFixed(1)).join(", ") : "none"} ${unit()} · uncertainty ±${fmt(l.sigma, 1)} · ${l.segments} view(s) · error ${fmt(l.err_px, 2)} px`),
          h("div", { class: "row", style: { marginTop: "8px" } },
            h("button", { onclick: () => identify(i) }, "Blink on tree"),
            h("button", { onclick: () => toggleList("bad", i, `LED ${i} marked bad.`) }, (overrides.bad ?? []).includes(i) ? "Unmark bad" : "Mark bad"),
            h("button", { onclick: () => toggleList("breaks", i, `Break after LED ${i}.`) }, (overrides.breaks ?? []).includes(i) ? `Remove break after ${i}` : `Break after ${i}`),
            l.p ? h("button", { onclick: () => viewer?.startDrag(i) }, "Move") : "",
          ),
        );
      }

      async function identify(i: number) {
        try {
          await api("/api/wled/identify", { body: { index: i } });
        } catch (e) {
          show(fmtErr(e), "fail");
        }
      }

      async function toggleList(key: "bad" | "breaks", i: number, text: string) {
        const list: number[] = overrides[key] ?? [];
        overrides[key] = list.includes(i) ? list.filter((x) => x !== i) : [...list, i];
        await saveOverrides(`${text} Re-solve to apply.`);
        selectLed(selected);
      }

      async function saveOverrides(text: string) {
        if (!map) return;
        try {
          overrides = await api(`/api/scans/${map.scan}/overrides`, { method: "PUT", body: overrides });
          show(text);
          renderEdits();
        } catch (e) {
          show(fmtErr(e), "fail");
        }
      }

      function renderEdits() {
        const n = (overrides.bad?.length ?? 0) + (overrides.breaks?.length ?? 0) + Object.keys(overrides.positions ?? {}).length;
        editsBox.replaceChildren(n ? h("div", {}, `${n} edit(s) saved for this scan: `,
          h("button", { onclick: resolve }, "Re-solve with edits"), " ",
          h("button", { onclick: async () => { overrides = {}; await saveOverrides("Edits cleared."); } }, "Clear edits")) : "");
      }

      async function resolve() {
        if (!map) return;
        show("Solving...", "warn");
        try {
          let job = await api(`/api/scans/${map.scan}/solve`, { body: {} });
          while (job.status === "running") {
            await new Promise((r) => setTimeout(r, 1500));
            job = await api(`/api/jobs/${job.id}`);
          }
          if (job.status !== "done") throw new Error(job.error ?? "solve failed");
          show(`New version ${job.result.map}.`);
          await loadVersions(job.result.map);
        } catch (e) {
          show(fmtErr(e), "fail");
        }
      }

      // ---- re-scan (§11.5) ----
      const rescanInput = h("input", { type: "text", placeholder: "e.g. 12, 40-45, 300" }) as HTMLInputElement;
      const rescanMsg = h("div");
      let lowConfidence: number[] = [];

      async function renderRescan() {
        if (!map) return;
        try {
          const info = await api(`/api/maps/${map.id}/rescan`);
          lowConfidence = info.low_confidence;
        } catch {
          lowConfidence = [];
        }
        rescanBox.replaceChildren(
          h("h2", {}, "Re-scan"),
          h("p", { class: "small muted" },
            "Capture some LEDs again: ones that weren't measured from two views, or that look wrong. ",
            "The new capture is added to this scan and solved with everything else into a new version."),
          h("label", {}, "LEDs"),
          rescanInput,
          h("div", { class: "row", style: { marginTop: "8px" } },
            h("button", { onclick: () => { rescanInput.value = compact(lowConfidence); } }, `All low-confidence (${lowConfidence.length})`),
            selected !== null ? h("button", { onclick: () => { rescanInput.value = [rescanInput.value, String(selected)].filter(Boolean).join(", "); } }, `Add LED ${selected}`) : "",
            h("button", { onclick: showOnTree }, "Show on tree"),
            h("button", { onclick: () => api("/api/test/stop", { body: {} }).catch(() => {}) }, "Stop showing"),
          ),
          h("div", { class: "row", style: { marginTop: "8px" } }, h("button", { class: "primary", onclick: startRescan }, "Set up re-scan")),
          rescanMsg,
        );
      }

      function compact(list: number[]): string {
        const out: string[] = [];
        let i = 0;
        while (i < list.length) {
          let j = i;
          while (j + 1 < list.length && list[j + 1] === list[j] + 1) j++;
          out.push(j > i ? `${list[i]}-${list[j]}` : `${list[i]}`);
          i = j + 1;
        }
        return out.join(", ");
      }

      function parseList(): number[] {
        const out = new Set<number>();
        for (const part of rescanInput.value.split(/[,\s]+/).filter(Boolean)) {
          const m = part.match(/^(\d+)-(\d+)$/);
          if (m) for (let k = Math.min(+m[1], +m[2]); k <= Math.max(+m[1], +m[2]); k++) out.add(k);
          else if (/^\d+$/.test(part)) out.add(+part);
        }
        return [...out].sort((a, b) => a - b);
      }

      async function showOnTree() {
        const leds = parseList();
        if (!leds.length) return rescanMsg.replaceChildren(h("div", { class: "notice warn" }, "Choose LEDs first."));
        try {
          await api("/api/leds/show", { body: { leds } });
          rescanMsg.replaceChildren(h("div", { class: "notice ok" }, `${leds.length} LEDs are blinking on the tree.`));
        } catch (e) {
          rescanMsg.replaceChildren(h("div", { class: "notice fail" }, fmtErr(e)));
        }
      }

      async function startRescan() {
        if (!map) return;
        try {
          const r = await api(`/api/maps/${map.id}/rescan`, { body: { text: rescanInput.value } });
          await api("/api/test/stop", { body: {} }).catch(() => {});
          if (r.kind === "walk") {
            rescanMsg.replaceChildren(h("div", { class: "notice ok" },
              `Walk again past those ${r.leds.length} LEDs (all the way round, or just that side). The walk is added to the same scan. `,
              h("a", { href: `#/scan?scan=${encodeURIComponent(r.scan)}` }, "Open the Scan page")));
          } else {
            rescanMsg.replaceChildren(h("div", { class: "notice ok" },
              `Re-scan set up: ${r.leds.length} LEDs plus ${r.anchors.length} well-measured anchor LEDs to place the camera. `,
              "Put the phone where it sees those LEDs, open ", h("a", { href: "#/calibrate" }, "Calibrate"),
              " on it, then start the re-scan from ", h("a", { href: "#/control" }, "Control"), ". Solve afterwards."));
          }
        } catch (e) {
          rescanMsg.replaceChildren(h("div", { class: "notice fail" }, fmtErr(e)));
        }
      }

      async function activate(force = false) {
        if (!map) return;
        try {
          const r = await api(`/api/maps/${map.id}/activate`, { body: { force } });
          show(`Map ${r.id} is active.${r.warning ? " " + r.warning : ""} Run the sweep test on the Test page to check it on the tree.`);
          await loadVersions(map.id);
        } catch (e) {
          const text = fmtErr(e);
          if (text.includes("Activate anyway") && confirm(`${text}`)) return activate(true);
          show(text, "fail");
        }
      }

      async function rollback() {
        const r = await api("/api/maps/rollback", { body: {} });
        show(r.active ? `Active map is ${r.active} again.` : "Nothing to roll back to.", r.active ? "ok" : "warn");
        await loadVersions(r.active ?? undefined);
      }

      const mapWarning = h("div");
      api("/api/state").then((s) => {
        if (s.map_warning) mapWarning.replaceChildren(h("div", { class: "notice warn" }, s.map_warning));
      }).catch(() => {});
      root.append(
        h("h1", {}, "Map"),
        mapWarning,
        msg,
        h("div", { class: "card" },
          h("label", {}, "Version"),
          versions,
          h("div", { class: "row", style: { marginTop: "10px" } },
            h("button", { class: "primary", onclick: () => activate() }, "Activate"),
            h("button", { onclick: rollback }, "Roll back"),
            h("button", { onclick: () => map && window.open(`/api/maps/${map.id}/export?fmt=json`) }, "Export JSON"),
            h("button", { onclick: () => map && window.open(`/api/maps/${map.id}/export?fmt=csv`) }, "Export CSV"),
          ),
        ),
        viewerBox,
        h("div", { class: "legend" }, ...Object.entries(STATUS_COLORS).map(([k, c]) => h("span", { style: { "--c": c } as any }, k.replace("_", " ")))),
        h("div", { class: "card", style: { marginTop: "12px" } }, sel, editsBox),
        h("div", { class: "card" }, rescanBox),
        h("div", { class: "card" }, h("h2", {}, "Quality report"), report),
      );
      const id = new URLSearchParams(location.hash.split("?")[1] ?? "").get("id") ?? undefined;
      loadVersions(id).catch((e) => show(fmtErr(e), "fail"));
    },
    unmount() {
      viewer?.dispose();
    },
  };
}
