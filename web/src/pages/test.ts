import { api, fmtErr, h, LiveSocket, type Page } from "../api";
import { TreeViewer, type TreeMap } from "../viewer";

export function testPage(): Page {
  let viewer: TreeViewer | null = null;
  let preview: LiveSocket | null = null;
  return {
    mount(root) {
      const msg = h("div");
      const bright = h("input", { type: "range", min: "0.05", max: "1", step: "0.05", value: "0.5" }) as HTMLInputElement;
      const viewerBox = h("div", { class: "viewer" });
      const show = (text: string, cls = "ok") => msg.replaceChildren(h("div", { class: `notice ${cls}` }, text));

      async function start(effect: string) {
        try {
          await api("/api/test/start", { body: { effect, brightness: Number(bright.value) } });
          show(effect === "sweep" ? "Sweep test: a plane rises, a half-plane rotates, a sphere grows. Each should look clean on the tree."
            : effect === "status" ? "Status colors: green measured, yellow single view, orange estimated, blue manual, off unknown."
            : "Index chase running.");
        } catch (e) {
          show(fmtErr(e), "fail");
        }
      }

      root.append(
        h("h1", {}, "Test"),
        msg,
        h("div", { class: "card" },
          h("div", { class: "row" },
            h("button", { class: "primary", onclick: () => start("sweep") }, "Sweep test"),
            h("button", { onclick: () => start("status") }, "Status colors"),
            h("button", { onclick: () => start("chase") }, "Index chase"),
            h("button", { class: "danger", onclick: async () => { await api("/api/test/stop", { body: {} }); show("Stopped. WLED returns to its own effect in a few seconds."); } }, "Stop"),
          ),
          h("label", {}, "Brightness"),
          bright,
          h("p", { class: "small muted" }, "Brightness is also capped by the current limit from Setup or WLED."),
        ),
        h("h2", {}, "Live preview"),
        viewerBox,
      );
      viewer = new TreeViewer(viewerBox);
      api<TreeMap>("/api/maps/active").then((m) => {
        viewer!.setMap(m);
        preview = new LiveSocket("/ws/preview");
        preview.onmessage = (ev) => {
          if (ev.data instanceof ArrayBuffer) viewer?.setColors(new Uint8Array(ev.data));
        };
      }).catch(() => viewerBox.replaceChildren(h("div", { class: "muted", style: { padding: "16px", color: "#9aa8b6" } }, "No active map yet. The index chase works without one.")));
    },
    unmount() {
      preview?.close();
      viewer?.dispose();
    },
  };
}
