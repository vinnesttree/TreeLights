import { api, fmtErr, h, type Page } from "../api";

export function homePage(): Page {
  return {
    mount(root) {
      const status = h("div", { class: "muted" }, "Loading...");
      root.append(
        h("h1", {}, "TreeLights"),
        h("div", { class: "card" },
          h("ol", {},
            h("li", {}, h("a", { href: "#/setup" }, "Setup"), ": choose the WLED controller, run the checks, enter the tree height."),
            h("li", {}, h("a", { href: "#/calibrate" }, "Calibrate"), ": put this phone on a tripod facing the tree and open this page on it."),
            h("li", {}, h("a", { href: "#/control" }, "Control"), ": from another phone or computer, start each view and turn the tree between views."),
            h("li", {}, h("a", { href: "#/map" }, "Map"), ": solve, review, fix and activate the 3D map."),
            h("li", {}, h("a", { href: "#/test" }, "Test"), ": run the sweep test on the real tree."),
          ),
        ),
        h("div", { class: "card" }, h("h2", {}, "Status"), status),
      );
      api("/api/state").then((s) => {
        status.replaceChildren(
          h("div", {}, `Controller: ${s.wled.host ?? "not selected"}${s.wled.version ? ` (WLED ${s.wled.version})` : ""}`),
          h("div", {}, `LEDs: ${s.led_count}`),
          h("div", {}, `Tree height: ${s.tree_height_cm ? s.tree_height_cm + " cm" : "not set"}`),
          h("div", {}, `Active map: ${s.active_map ?? "none yet"}`),
          h("div", {}, `Current scan: ${s.scan ?? "none"}`),
        );
      }).catch((e) => status.replaceChildren(h("div", { class: "notice fail" }, fmtErr(e))));
    },
  };
}
