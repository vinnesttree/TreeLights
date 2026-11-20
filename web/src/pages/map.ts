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

      root.append(
        h("h1", {}, "Map"),
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
