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
      const limitsBox = h("div");
      const recBox = h("div");
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
        renderLimits();
        renderRescan();
        renderRecordings();
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
          i + 1 < map.leds.length ? linkEditor(i) : "",
        );
      }

      /** The link from LED i to i+1: its measured length, current bound, and your own bound. */
      function linkEditor(i: number): HTMLElement {
        const m = map!;
        const a = m.leds[i].p, b = m.leds[i + 1].p;
        const len = a && b ? Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]) : null;
        const isBreak = m.chain.breaks.some((x) => x.from === i);
        const long = m.chain.long_links.find((x) => x.from === i);
        const bound = m.chain.link_bounds ? m.chain.link_bounds[i] : (long ? long.bound : m.chain.base_link);
        const kind = isBreak ? "break (no limit)" : long ? (long.kind === "user" ? "your bound" : long.kind === "long_predicted" ? "connector (predicted)" : "connector") : "normal";
        const mine: number | undefined = overrides.links?.[String(i)];
        const input = h("input", { type: "number", min: "0", step: "0.1", placeholder: fmt(bound ?? m.chain.base_link, 1), value: mine ?? "" }) as HTMLInputElement;
        return h("div", { class: "small", style: { marginTop: "10px" } },
          h("div", {}, h("b", {}, `Link ${i}→${i + 1}: `),
            `${len !== null ? fmt(len, 1) + " " + unit() + " long" : "not measured"} · `,
            `allowed up to ${bound === null || bound === undefined ? "any distance" : fmt(bound, 1) + " " + unit()} (${kind})`),
          h("div", { class: "row", style: { marginTop: "6px" } },
            h("span", {}, `Your bound (${unit()})`), input,
            h("button", { onclick: () => setLinkBound(i, input.value) }, "Set bound"),
            mine !== undefined ? h("button", { onclick: () => setLinkBound(i, "") }, "Clear bound") : "",
          ),
          h("div", { class: "muted" }, "A bound replaces the learned one for this link. Use it for a connector the solver missed, or to stop it bridging a gap it shouldn't."),
        );
      }

      async function setLinkBound(i: number, value: string) {
        const links = { ...(overrides.links ?? {}) };
        if (value.trim() === "") delete links[String(i)];
        else {
          const v = Number(value);
          if (!(v > 0)) return show("Enter a positive length.", "fail");
          links[String(i)] = v;
        }
        overrides.links = links;
        await saveOverrides(value.trim() === "" ? `Bound for link ${i}→${i + 1} cleared. Re-solve to apply.` : `Link ${i}→${i + 1} limited to ${value} ${unit()}. Re-solve to apply.`);
        renderLimits();
        selectLed(selected);
      }

      function renderLimits() {
        if (!map) return;
        const cap: number | undefined = overrides.cap ?? undefined;
        const input = h("input", { type: "number", min: "0", step: "0.1", placeholder: "none", value: cap ?? "" }) as HTMLInputElement;
        const nLinks = Object.keys(overrides.links ?? {}).length;
        limitsBox.replaceChildren(
          h("h2", {}, "Link limits"),
          h("p", { class: "small muted" },
            `The solver learned a normal spacing of ${fmt(map.chain.base_link, 1)} ${unit()} between neighbouring LEDs (from ${map.chain.trusted_links} trusted links). `,
            "Connectors between strands get their own, longer bound. To set a bound for one link, tap an LED: its link to the next LED is shown there."),
          h("label", {}, `Cap: no link longer than (${unit()})`),
          h("div", { class: "row" }, input,
            h("button", { onclick: () => setCap(input.value) }, "Set cap"),
            cap !== undefined ? h("button", { onclick: () => setCap("") }, "Clear cap") : ""),
          h("p", { class: "small muted" }, "The cap applies to every link except breaks, including connectors, so set it above your longest connector. Leave it empty to rely on the learned bounds."),
          nLinks ? h("div", { class: "small" }, `${nLinks} link bound(s) of your own: `,
            ...Object.entries(overrides.links).map(([k, v]) => h("span", { class: "pill", style: { marginRight: "4px" } }, `${k}→${Number(k) + 1}: ${v} ${unit()}`))) : "",
        );
      }

      async function setCap(value: string) {
        if (value.trim() === "") delete overrides.cap;
        else {
          const v = Number(value);
          if (!(v > 0)) return show("Enter a positive length.", "fail");
          overrides.cap = v;
        }
        await saveOverrides(value.trim() === "" ? "Cap cleared. Re-solve to apply." : `No link may be longer than ${value} ${unit()}. Re-solve to apply.`);
        renderLimits();
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
        const n = (overrides.bad?.length ?? 0) + (overrides.breaks?.length ?? 0) + Object.keys(overrides.positions ?? {}).length
          + Object.keys(overrides.links ?? {}).length + (overrides.cap ? 1 : 0);
        editsBox.replaceChildren(n ? h("div", {}, `${n} edit(s) saved for this scan: `,
          h("button", { onclick: resolve }, "Re-solve with edits"), " ",
          h("button", { onclick: async () => { overrides = {}; await saveOverrides("Edits cleared."); renderLimits(); } }, "Clear edits")) : "");
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

      // ---- decoding old walks again with the current version ----
      async function renderRecordings() {
        let list: any[] = [];
        try {
          list = await api<any[]>("/api/walks");
        } catch (e) {
          recBox.replaceChildren(h("h2", {}, "Recordings"), h("div", { class: "notice fail" }, fmtErr(e)));
          return;
        }
        const recMsg = h("div");
        const rows = list.map((w) => {
          const raw = w.video_mb > 0 || w.live_frames > 0;
          const what = [w.video_mb > 0 ? `recording ${w.video_mb} MB` : "", w.live_frames ? `${w.live_frames} live frames` : ""].filter(Boolean).join(", ");
          return h("div", { class: "rec-row" },
            h("div", {},
              h("b", {}, `${w.created ?? w.scan} · ${w.view}`), map && w.scan === map.scan ? h("span", { class: "pill ok", style: { marginLeft: "6px" } }, "this map") : "",
              h("div", { class: "small muted" },
                `${w.duration_s ? Math.round(w.duration_s) + " s walk · " : ""}${w.decoded ? `decoded ${Object.entries(w.decoded).map(([k, v]) => `${v} (${k})`).join(", ")} · ` : ""}`,
                raw ? `stored: ${what}` : "raw recording deleted (older scan); keyframes kept")),
            h("div", { class: "row" },
              h("button", { disabled: !raw || w.busy, title: raw ? "" : "The raw recording of this walk is no longer stored", onclick: () => reprocess(w, recMsg) }, "Decode again"),
              h("button", { disabled: w.keyframes === 0 || w.busy, onclick: () => resolveScan(w.scan, recMsg) }, "Solve again")),
          );
        });
        recBox.replaceChildren(
          h("h2", {}, "Recordings"),
          h("p", { class: "small muted" },
            "Run old walks through the current version of TreeLights. Decode again re-reads the stored recording and live frames, then builds a new map version. ",
            "Solve again only re-runs the 3D solve on the LEDs decoded before (always possible). The active map doesn't change until you activate the new version."),
          list.length ? h("div", {}, ...rows) : h("div", { class: "muted small" }, "No walks recorded yet."),
          recMsg,
          h("p", { class: "small muted" }, "To save space, the raw recordings of all but the 2 most recent scans (and the active map's scan) are deleted when a new scan starts."),
        );
      }

      async function waitJob(job: any, recMsg: HTMLElement, what: string): Promise<void> {
        recMsg.replaceChildren(h("div", { class: "notice warn" }, `${what}... this can take a few minutes.`));
        while (job.status === "running") {
          await new Promise((r) => setTimeout(r, 2000));
          job = await api(`/api/jobs/${job.id}`);
        }
        if (job.status !== "done") throw new Error(job.error ?? "failed");
        const c = job.result.quality.counts;
        recMsg.replaceChildren(h("div", { class: "notice ok" }, `New version ${job.result.map}: ${c.measured} measured, ${c.estimated} estimated, ${c.unknown} unknown. Showing it now; activate it if it looks right.`));
        await loadVersions(job.result.map);
      }

      async function reprocess(w: any, recMsg: HTMLElement) {
        try {
          const job = await api(`/api/scans/${w.scan}/walk/${w.view}/process`, { body: { video: w.video_mb > 0 } });
          await waitJob(job, recMsg, `Decoding ${w.view} of ${w.created ?? w.scan} again`);
        } catch (e) {
          recMsg.replaceChildren(h("div", { class: "notice fail" }, fmtErr(e)));
        }
      }

      async function resolveScan(scan: string, recMsg: HTMLElement) {
        try {
          const job = await api(`/api/scans/${scan}/solve`, { body: {} });
          await waitJob(job, recMsg, "Solving again");
        } catch (e) {
          recMsg.replaceChildren(h("div", { class: "notice fail" }, fmtErr(e)));
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
        h("div", { class: "card" }, limitsBox),
        h("div", { class: "card" }, rescanBox),
        h("div", { class: "card" }, recBox),
        h("div", { class: "card" }, h("h2", {}, "Quality report"), report),
      );
      const id = new URLSearchParams(location.hash.split("?")[1] ?? "").get("id") ?? undefined;
      loadVersions(id).catch((e) => show(fmtErr(e), "fail"));
      renderRecordings();
    },
    unmount() {
      viewer?.dispose();
    },
  };
}
