import { api, fmtErr, h, LiveSocket, type Page } from "../api";
import { statusView, type Status } from "./status";

export function controlPage(): Page {
  let ctl: LiveSocket | null = null;
  let poll: number | undefined;
  return {
    mount(root) {
      const sv = statusView();
      const msg = h("div");
      const scanInfo = h("div", { class: "small muted" });
      const modeSelect = h("select", {},
        h("option", { value: "hybrid" }, "Hybrid (fast, default)"),
        h("option", { value: "sequential" }, "One LED at a time (slower)")) as HTMLSelectElement;
      const rescanOpt = h("option", { value: "rescan", disabled: true }, "Re-scan (set up on the Map page)") as HTMLOptionElement;
      modeSelect.append(rescanOpt);
      const solveBox = h("div");
      let scanId: string | null = null;

      const show = (text: string, cls = "ok") => msg.replaceChildren(h("div", { class: `notice ${cls}` }, text));

      async function refresh() {
        const s = await api("/api/state");
        scanId = s.scan;
        scanInfo.textContent = scanId ? `Scan ${scanId}` : "No scan yet: starting a view creates one.";
        const r = s.rescan;
        rescanOpt.disabled = !r;
        rescanOpt.textContent = r ? `Re-scan ${r.leds} LEDs (+${r.anchors} anchors)` : "Re-scan (set up on the Map page)";
        if (r && modeSelect.dataset.auto !== "done") {
          modeSelect.value = "rescan";
          modeSelect.dataset.auto = "done";
        }
        if (s.session) sv.update({ ...s.session, guidance: s.guidance } as Status);
      }

      async function newScan() {
        if (!confirm("Start a new scan? The current scan stays saved.")) return;
        try {
          const r = await api("/api/scans", { body: {} });
          scanId = r.scan;
          show(`New scan ${r.scan}.`);
          refresh();
        } catch (e) {
          show(fmtErr(e), "fail");
        }
      }

      async function solve() {
        if (!scanId) return show("No scan to solve.", "warn");
        try {
          let job = await api(`/api/scans/${scanId}/solve`, { body: {} });
          solveBox.replaceChildren(h("div", { class: "notice warn" }, "Solving... this takes up to a minute."));
          while (job.status === "running") {
            await new Promise((r) => setTimeout(r, 1500));
            job = await api(`/api/jobs/${job.id}`);
          }
          if (job.status === "done") {
            const q = job.result.quality;
            solveBox.replaceChildren(h("div", { class: "notice ok" },
              `Map ${job.result.map}: ${q.counts.measured} measured, ${q.counts.single_view} single-view, ${q.counts.estimated} estimated, ${q.counts.unknown} unknown. `,
              h("a", { href: `#/map?id=${job.result.map}` }, "Review it")));
          } else {
            solveBox.replaceChildren(h("div", { class: "notice fail" }, job.error ?? "Solve failed."));
          }
        } catch (e) {
          solveBox.replaceChildren(h("div", { class: "notice fail" }, fmtErr(e)));
        }
      }

      ctl = new LiveSocket("/ws/control");
      ctl.onmessage = (ev) => {
        const m = JSON.parse(ev.data);
        if (m.type === "status") sv.update(m as Status);
        else if (m.type === "error") show(m.message, "fail");
        else if (m.type === "scan") refresh();
      };

      root.append(
        h("h1", {}, "Control"),
        h("p", { class: "muted small" }, "Use this on a second phone or computer so the tripod phone isn't touched. Turn the tree by the suggested angle, step out of the picture, then start the next view."),
        msg,
        h("div", { class: "card" },
          scanInfo,
          h("label", {}, "Capture mode"),
          modeSelect,
          h("div", { class: "row", style: { marginTop: "10px" } },
            h("button", { class: "primary", onclick: () => ctl?.sendJson({ type: "start_view", mode: modeSelect.value }) }, "Start view"),
            h("button", { onclick: () => ctl?.sendJson({ type: "stop" }) }, "Stop"),
            h("button", { onclick: () => ctl?.sendJson({ type: "resume" }) }, "Resume"),
            h("button", { onclick: newScan }, "New scan"),
          ),
        ),
        sv.el,
        h("div", { class: "card" },
          h("h2", {}, "Solve"),
          h("p", { class: "small muted" }, "When the guidance says coverage looks good (typically 8 views, one full turn), build the map."),
          h("button", { class: "primary", onclick: solve }, "Solve map"),
          solveBox,
        ),
      );
      refresh().catch((e) => show(fmtErr(e), "fail"));
      poll = window.setInterval(() => refresh().catch(() => {}), 10000);
    },
    unmount() {
      ctl?.close();
      if (poll) window.clearInterval(poll);
    },
  };
}
