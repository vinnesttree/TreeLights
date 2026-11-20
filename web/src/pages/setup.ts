import { api, fmtErr, h, type Page } from "../api";

const STATUS_LABEL: Record<string, string> = { ok: "OK", warn: "Check", fail: "Problem", unknown: "Unknown" };

export function setupPage(): Page {
  return {
    mount(root) {
      const msg = h("div");
      const devices = h("div", { class: "small muted" }, "Not searched yet.");
      const hostInput = h("input", { placeholder: "192.168.1.50 or wled-tree.local", inputmode: "url" }) as HTMLInputElement;
      const checksBox = h("div", { class: "muted" }, "Select a controller, then run the checks.");
      const heightInput = h("input", { type: "number", min: "10", max: "1000", step: "1", inputmode: "decimal" }) as HTMLInputElement;
      const ledInput = h("input", { type: "number", min: "1", max: "4096" }) as HTMLInputElement;
      const powerInput = h("input", { type: "number", min: "0", step: "100", placeholder: "use WLED's limit" }) as HTMLInputElement;
      const thorough = h("input", { type: "checkbox" }) as HTMLInputElement;

      const show = (text: string, cls = "ok") => msg.replaceChildren(h("div", { class: `notice ${cls}` }, text));

      async function load() {
        const [cfg, st] = await Promise.all([api("/api/config"), api("/api/state")]);
        hostInput.value = cfg.wled.host ?? "";
        heightInput.value = cfg.tree_height_cm ?? "";
        ledInput.value = String(cfg.wled.led_count);
        powerInput.value = cfg.power.max_current_ma ?? "";
        thorough.checked = !!cfg.capture.thorough;
        if (st.wled.host) checksBox.replaceChildren(h("span", { class: "muted" }, `Selected: ${st.wled.host}. Run the checks.`));
      }

      async function select(host: string) {
        try {
          const r = await api("/api/wled/select", { body: { host } });
          show(`Selected ${host}: WLED ${r.version ?? "?"}, ${r.led_count ?? "?"} LEDs.`);
          ledInput.value = String(r.led_count ?? ledInput.value);
          hostInput.value = host;
          await runChecks();
        } catch (e) {
          show(fmtErr(e), "fail");
        }
      }

      async function discover() {
        devices.textContent = "Searching the network (a few seconds)...";
        try {
          const list = await api<any[]>("/api/wled/discover");
          if (!list.length) {
            devices.textContent = "None found on this server's network. Enter the controller's address instead (shown in WLED's Wi-Fi settings or your router).";
            return;
          }
          devices.replaceChildren(...list.map((d) =>
            h("div", { class: "row", style: { padding: "4px 0" } },
              h("span", { class: "grow" }, `${d.name ?? "WLED"} — ${d.host}${d.version ? ` (v${d.version})` : ""}${d.led_count ? `, ${d.led_count} LEDs` : ""}`),
              h("button", { onclick: () => select(d.host) }, "Use"),
            )));
        } catch (e) {
          devices.textContent = fmtErr(e);
        }
      }

      async function runChecks() {
        checksBox.textContent = "Checking...";
        try {
          const checks = await api<any[]>("/api/wled/checks");
          checksBox.replaceChildren(h("ul", { class: "checks" }, ...checks.map((c) =>
            h("li", {}, h("span", { class: `pill ${c.status}` }, STATUS_LABEL[c.status] ?? c.status), h("span", {}, h("b", {}, c.name.replace("_", " ")), " ", c.detail)))));
        } catch (e) {
          checksBox.replaceChildren(h("div", { class: "notice fail" }, fmtErr(e)));
        }
      }

      async function save() {
        const patch: any = {
          tree_height_cm: heightInput.value ? Number(heightInput.value) : null,
          wled: { led_count: Number(ledInput.value) || 600 },
          power: { max_current_ma: powerInput.value === "" ? null : Number(powerInput.value) },
          capture: { thorough: thorough.checked },
        };
        try {
          await api("/api/config", { method: "PUT", body: patch });
          show("Saved.");
        } catch (e) {
          show(fmtErr(e), "fail");
        }
      }

      async function chase() {
        try {
          await api("/api/test/start", { body: { effect: "chase", brightness: 0.4 } });
          show("Chase running: one bright spot runs along the string. Check it reaches the last LED, then stop it on the Test page.");
        } catch (e) {
          show(fmtErr(e), "fail");
        }
      }

      root.append(
        h("h1", {}, "Setup"),
        msg,
        h("div", { class: "card" },
          h("h2", {}, "WLED controller"),
          h("div", { class: "row" }, h("button", { onclick: discover }, "Find controllers"), devices),
          h("label", {}, "Or enter its address"),
          h("div", { class: "row" }, h("div", { class: "grow" }, hostInput), h("button", { class: "primary", onclick: () => select(hostInput.value.trim()) }, "Use")),
        ),
        h("div", { class: "card" },
          h("h2", {}, "Checks"),
          checksBox,
          h("div", { class: "row", style: { marginTop: "10px" } },
            h("button", { onclick: runChecks }, "Run checks"),
            h("button", { onclick: chase }, "Index chase"),
          ),
          h("p", { class: "small muted" }, "In WLED's Sync settings, 'Receive UDP realtime' must be on. TreeLights sends DDP to port 4048."),
        ),
        h("div", { class: "card" },
          h("h2", {}, "Tree"),
          h("label", {}, "Tree height in cm, from the lowest light to the highest light"),
          heightInput,
          h("p", { class: "small muted" }, "Only used to convert the map to centimeters. Leave empty to get a map in tree heights."),
          h("label", {}, "Number of LEDs (from WLED)"),
          ledInput,
          h("label", {}, "Current limit for the test animation (mA)"),
          powerInput,
          h("label", {}, h("span", { class: "row" }, thorough, "Thorough capture: also try every LED the binary pass missed (slower)")),
          h("div", { class: "row", style: { marginTop: "10px" } }, h("button", { class: "primary", onclick: save }, "Save")),
        ),
        h("div", { class: "card small" },
          h("h2", {}, "Camera calibration (optional)"),
          h("p", { class: "muted" }, "A one-time check of the phone camera's lens with a board shown on a screen. Maps work without it."),
          h("a", { href: "#/camcal" }, "Calibrate the camera"),
        ),
        h("div", { class: "card small muted" },
          h("h2", {}, "Network"),
          h("p", {}, "The phone must reach this server directly (no guest network or client isolation). If your router has 5 GHz, put the phone on it so camera uploads don't compete with the LED packets on 2.4 GHz."),
          h("p", {}, "Chrome warns about this server's certificate because it comes from a local authority. Tap Advanced, then Proceed. It asks when the page is opened, not for every camera frame."),
        ),
      );
      load().catch((e) => show(fmtErr(e), "fail"));
    },
  };
}
