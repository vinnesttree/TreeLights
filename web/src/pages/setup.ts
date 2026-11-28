import { api, currentTree, fmtErr, h, setCurrentTree, type Page } from "../api";

const STATUS_LABEL: Record<string, string> = { ok: "OK", warn: "Check", fail: "Problem", unknown: "Unknown" };

type Tree = { id: string; name: string; default: boolean; led_count: number; controllers: { host: string | null }[] };
type Controller = {
  host: string | null; mac: string | null; http_port: number; led_count: number; protocol: string;
  udp_port: number | null; name: string | null; max_current_ma: number | null;
  start: number; last: number; wled: { name: string | null; version: string | null; led_count: number | null } | null;
};

function treesChanged(): void {
  window.dispatchEvent(new Event("trees-changed"));
}

export function setupPage(): Page {
  return {
    mount(root) {
      const msg = h("div");
      const treeList = h("div", { class: "small muted" }, "Loading...");
      const newTreeName = h("input", { placeholder: "Name, e.g. Porch tree" }) as HTMLInputElement;
      const ctlList = h("div", { class: "small muted" }, "Loading...");
      const devices = h("div", { class: "small muted" }, "Not searched yet.");
      const hostInput = h("input", { placeholder: "192.168.1.50 or wled-tree.local", inputmode: "url" }) as HTMLInputElement;
      const checksBox = h("div", { class: "muted" }, "Select a controller, then run the checks.");
      const heightInput = h("input", { type: "number", min: "10", max: "1000", step: "1", inputmode: "decimal" }) as HTMLInputElement;
      const powerInput = h("input", { type: "number", min: "0", step: "100", placeholder: "use WLED's limit" }) as HTMLInputElement;
      const thorough = h("input", { type: "checkbox" }) as HTMLInputElement;
      const fullRes = h("input", { type: "checkbox" }) as HTMLInputElement;
      const treeTitle = h("span", {}, "Tree");
      // Home Assistant (MQTT)
      const mqttOn = h("input", { type: "checkbox" }) as HTMLInputElement;
      const mqttHost = h("input", { placeholder: "192.168.1.10 or homeassistant.local", inputmode: "url" }) as HTMLInputElement;
      const mqttPort = h("input", { type: "number", min: "1", max: "65535", value: "1883" }) as HTMLInputElement;
      const mqttUser = h("input", { autocomplete: "off" }) as HTMLInputElement;
      const mqttPass = h("input", { type: "password", autocomplete: "new-password" }) as HTMLInputElement;
      const mqttTls = h("input", { type: "checkbox" }) as HTMLInputElement;
      const mqttBase = h("input", { value: "treelights" }) as HTMLInputElement;
      const mqttPrefix = h("input", { value: "homeassistant" }) as HTMLInputElement;
      const mqttStatus = h("div");
      let controllers: Controller[] = [];
      let trees: Tree[] = [];

      const show = (text: string, cls = "ok") => msg.replaceChildren(h("div", { class: `notice ${cls}` }, text));

      async function load() {
        const [cfg, st] = await Promise.all([api("/api/config"), api("/api/state")]);
        treeTitle.textContent = trees.length > 1 ? `Tree: ${cfg.tree_name}` : "Tree";
        heightInput.value = cfg.tree_height_cm ?? "";
        powerInput.value = cfg.power.max_current_ma ?? "";
        thorough.checked = !!cfg.capture.thorough;
        fullRes.checked = !!cfg.capture.walk_full_resolution;
        mqttOn.checked = !!cfg.mqtt.enabled;
        mqttHost.value = cfg.mqtt.host ?? "";
        mqttPort.value = String(cfg.mqtt.port);
        mqttUser.value = cfg.mqtt.username ?? "";
        mqttPass.value = "";
        mqttPass.placeholder = cfg.mqtt.password ? "saved (type to change)" : "";
        mqttTls.checked = !!cfg.mqtt.tls;
        mqttBase.value = cfg.mqtt.base_topic;
        mqttPrefix.value = cfg.mqtt.discovery_prefix;
        await haStatus();
        if (st.wled.host) checksBox.replaceChildren(h("span", { class: "muted" }, `Selected: ${st.controllers.map((c: any) => c.host ?? "?").join(", ")}. Run the checks.`));
      }

      // ---- trees ------------------------------------------------------------------------
      async function loadTrees() {
        trees = await api<Tree[]>("/api/trees");
        const cur = currentTree() ?? trees.find((t) => t.default)?.id;
        treeList.replaceChildren(...trees.map((t) => {
          const name = h("input", { value: t.name }) as HTMLInputElement;
          const here = t.id === cur;
          return h("div", { class: "row", style: { padding: "4px 0" } },
            h("div", { class: "grow" }, name),
            h("span", { class: "muted" }, `${t.led_count} LEDs, ${t.controllers.length} controller${t.controllers.length === 1 ? "" : "s"}`),
            h("button", { onclick: () => renameTree(t.id, name.value) }, "Rename"),
            here ? h("span", { class: "pill ok" }, "This page") : h("button", { onclick: () => useTree(t.id) }, "Set up"),
            t.default ? "" : h("button", { class: "danger", onclick: () => removeTree(t) }, "Remove"),
          );
        }));
      }

      async function renameTree(id: string, name: string) {
        try {
          await api(`/api/trees/${id}`, { method: "PUT", body: { name: name.trim() } });
          show("Renamed.");
          await loadTrees();
          treesChanged();
        } catch (e) {
          show(fmtErr(e), "fail");
        }
      }

      function useTree(id: string) {
        setCurrentTree(id);
        treesChanged();
        root.replaceChildren();
        setupPage().mount(root);
      }

      async function addTree() {
        try {
          const t = await api<Tree>("/api/trees", { body: { name: newTreeName.value.trim() || undefined } });
          newTreeName.value = "";
          show(`Added ${t.name}. Choose its controller below.`);
          useTree(t.id);
        } catch (e) {
          show(fmtErr(e), "fail");
        }
      }

      async function removeTree(t: Tree) {
        if (!confirm(`Remove ${t.name}? Its scans and maps are kept in the data folder (trees/.removed), but the app no longer shows them.`)) return;
        try {
          await api(`/api/trees/${t.id}`, { method: "DELETE" });
          if (currentTree() === t.id) setCurrentTree(null);
          show(`Removed ${t.name}.`);
          treesChanged();
          root.replaceChildren();
          setupPage().mount(root);
        } catch (e) {
          show(fmtErr(e), "fail");
        }
      }

      // ---- controllers of this tree -----------------------------------------------------------
      async function loadControllers() {
        controllers = await api<Controller[]>("/api/controllers");
        renderControllers();
      }

      function renderControllers() {
        const multi = controllers.length > 1;
        let start = 0;
        ctlList.replaceChildren(...controllers.map((c, k) => {
          const count = h("input", { type: "number", min: "1", max: "8192", value: String(c.led_count) }) as HTMLInputElement;
          count.onchange = () => {
            c.led_count = Math.max(1, Number(count.value) || 1);
            renderControllers();
          };
          const first = start;
          start += c.led_count;
          const wled = c.wled ? `${c.wled.name ?? "WLED"}${c.wled.version ? ` ${c.wled.version}` : ""}` : "not checked yet";
          const mismatch = c.wled?.led_count && c.wled.led_count !== c.led_count ? ` WLED drives ${c.wled.led_count}.` : "";
          return h("div", { class: "ctl-row" },
            h("b", {}, `${k + 1}`),
            h("div", {},
              h("div", {}, c.host ?? "no address yet", h("span", { class: "muted" }, ` (${wled})`)),
              h("div", { class: "small muted" }, `LEDs ${first}–${first + c.led_count - 1}${mismatch}`)),
            count,
            h("div", { class: "btns" },
              multi && k > 0 ? h("button", { title: "Move up", onclick: () => move(k, -1) }, "↑") : "",
              multi && k < controllers.length - 1 ? h("button", { title: "Move down", onclick: () => move(k, 1) }, "↓") : "",
              multi ? h("button", { class: "danger", title: "Remove", onclick: () => removeController(k) }, "✕") : ""),
          );
        }));
      }

      function move(k: number, d: number) {
        const [c] = controllers.splice(k, 1);
        controllers.splice(k + d, 0, c);
        renderControllers();
      }

      function removeController(k: number) {
        controllers.splice(k, 1);
        renderControllers();
      }

      async function saveControllers() {
        const body = controllers.map(({ start: _s, last: _l, wled: _w, ...c }) => c);
        try {
          await api("/api/controllers", { method: "PUT", body });
          show("Saved. The LED numbers follow the order of the controllers; run the checks.");
          await loadControllers();
          await loadTrees();
          treesChanged();
        } catch (e) {
          show(fmtErr(e), "fail");
        }
      }

      async function select(host: string, add = false) {
        if (!host) return show("Enter the controller's address.", "warn");
        try {
          const r = await api("/api/wled/select", { body: add ? { host, add: true } : { host } });
          show(`${add ? "Added" : "Selected"} ${host} as controller ${r.controller + 1}: WLED ${r.version ?? "?"}, ${r.led_count ?? "?"} LEDs.`);
          hostInput.value = "";
          await loadControllers();
          await loadTrees();
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
          const cur = currentTree() ?? trees.find((t) => t.default)?.id;
          const treeName = (id: string) => trees.find((t) => t.id === id)?.name ?? id;
          devices.replaceChildren(...list.map((d) => {
            const actions = d.tree && d.tree !== cur ? [h("span", { class: "muted" }, `used by ${treeName(d.tree)}`)]
              : d.tree ? [h("span", { class: "pill ok" }, "this tree")]
              : [h("button", { onclick: () => select(d.host) }, "Use"), h("button", { onclick: () => select(d.host, true) }, "Add")];
            return h("div", { class: "row", style: { padding: "4px 0" } },
              h("span", { class: "grow" }, `${d.name ?? "WLED"} — ${d.host}${d.version ? ` (v${d.version})` : ""}${d.led_count ? `, ${d.led_count} LEDs` : ""}`),
              ...actions);
          }));
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
          power: { max_current_ma: powerInput.value === "" ? null : Number(powerInput.value) },
          capture: { thorough: thorough.checked, walk_full_resolution: fullRes.checked },
        };
        try {
          await api("/api/config", { method: "PUT", body: patch });
          show("Saved.");
        } catch (e) {
          show(fmtErr(e), "fail");
        }
      }

      async function haStatus() {
        try {
          const st = await api("/api/homeassistant");
          const [cls, text] = !st.enabled ? ["unknown", "Off"]
            : st.connected ? ["ok", `Connected to ${st.broker}`]
            : st.error ? ["fail", st.error] : ["warn", `Connecting to ${st.broker ?? "?"}...`];
          mqttStatus.replaceChildren(h("div", { class: "row" }, h("span", { class: `pill ${cls}` }, cls === "ok" ? "OK" : cls === "fail" ? "Problem" : cls === "warn" ? "Wait" : "Off"),
            h("span", {}, text)));
          return st;
        } catch (e) {
          mqttStatus.replaceChildren(h("div", { class: "notice fail" }, fmtErr(e)));
          return null;
        }
      }

      async function saveMqtt() {
        const mqtt: any = {
          enabled: mqttOn.checked, host: mqttHost.value.trim() || null, port: Number(mqttPort.value) || 1883,
          username: mqttUser.value.trim() || null, tls: mqttTls.checked,
          base_topic: mqttBase.value.trim(), discovery_prefix: mqttPrefix.value.trim(),
        };
        if (mqttPass.value) mqtt.password = mqttPass.value;
        else if (!mqtt.username) mqtt.password = null;
        try {
          await api("/api/config", { method: "PUT", body: { mqtt } });
          mqttPass.value = "";
          show(mqtt.enabled ? "Saved. Connecting to the broker..." : "Saved. Home Assistant integration is off.");
          for (let i = 0; i < 6; i++) {
            const st = await haStatus();
            if (!st || !st.enabled || st.connected || st.error) break;
            await new Promise((r) => setTimeout(r, 1000));
          }
        } catch (e) {
          show(fmtErr(e), "fail");
        }
      }

      async function chase() {
        try {
          await api("/api/test/start", { body: { effect: "chase", brightness: 0.4 } });
          show("Chase running: one bright spot runs along the string, across every controller in order. Check it reaches the last LED, then stop it on the Test page.");
        } catch (e) {
          show(fmtErr(e), "fail");
        }
      }

      root.append(
        h("h1", {}, "Setup"),
        msg,
        h("div", { class: "card" },
          h("h2", {}, "Trees"),
          h("p", { class: "small muted" }, "Each tree has its own controllers, scans, maps, effect, timers and Home Assistant light. The menu at the top chooses which tree the other pages act on."),
          treeList,
          h("div", { class: "row", style: { marginTop: "8px" } }, h("div", { class: "grow" }, newTreeName), h("button", { onclick: addTree }, "Add a tree")),
        ),
        h("div", { class: "card" },
          h("h2", {}, "WLED controllers of this tree"),
          h("p", { class: "small muted" }, "A tree can use several controllers. Its LEDs are numbered across them in this order: controller 1 has the first LEDs, controller 2 the next ones, and so on. There is no wire between controllers, so the map treats each boundary as a break in the chain."),
          ctlList,
          h("div", { class: "row", style: { marginTop: "8px" } }, h("button", { class: "primary", onclick: saveControllers }, "Save controllers")),
          h("h2", {}, "Find or add a controller"),
          h("div", { class: "row" }, h("button", { onclick: discover }, "Find controllers"), devices),
          h("label", {}, "Or enter its address"),
          h("div", { class: "row" }, h("div", { class: "grow" }, hostInput),
            h("button", { onclick: () => select(hostInput.value.trim()) }, "Use as controller 1"),
            h("button", { onclick: () => select(hostInput.value.trim(), true) }, "Add")),
        ),
        h("div", { class: "card" },
          h("h2", {}, "Checks"),
          checksBox,
          h("div", { class: "row", style: { marginTop: "10px" } },
            h("button", { onclick: runChecks }, "Run checks"),
            h("button", { onclick: chase }, "Index chase"),
          ),
          h("p", { class: "small muted" }, "In WLED's Sync settings, 'Receive UDP realtime' must be on. TreeLights sends DDP to port 4048. With several controllers, every controller is checked."),
        ),
        h("div", { class: "card" },
          h("h2", {}, treeTitle),
          h("label", {}, "Tree height in cm, from the lowest light to the highest light"),
          heightInput,
          h("p", { class: "small muted" }, "Only used to convert the map to centimeters. Leave empty to get a map in tree heights."),
          h("label", {}, "Current limit per controller for effects (mA)"),
          powerInput,
          h("label", {}, h("span", { class: "row" }, thorough, "Thorough capture: also try every LED the binary pass missed (slower)")),
          h("label", {}, h("span", { class: "row" }, fullRes, "Walk-around at full resolution (1920x1080): for dense trees (over ~1000 LEDs); bigger uploads, slower processing")),
          h("div", { class: "row", style: { marginTop: "10px" } }, h("button", { class: "primary", onclick: save }, "Save")),
        ),
        h("div", { class: "card" },
          h("h2", {}, "Home Assistant"),
          h("p", { class: "small muted" }, "Through an MQTT broker (for example Home Assistant's Mosquitto add-on, with the MQTT integration set up). Each tree then appears in Home Assistant by itself, as a light with effects and brightness, plus palette, speed, a switch for the timers and the next timer. The broker is shared by all trees; the topic is this tree's own."),
          mqttStatus,
          h("label", {}, h("span", { class: "row" }, mqttOn, "Connect to Home Assistant")),
          h("label", {}, "Broker address"), mqttHost,
          h("label", {}, "Port"), mqttPort,
          h("label", {}, "Username"), mqttUser,
          h("label", {}, "Password"), mqttPass,
          h("label", {}, h("span", { class: "row" }, mqttTls, "TLS (usually port 8883)")),
          h("label", {}, "Topic of this tree (each tree has its own)"), mqttBase,
          h("label", {}, "Discovery prefix"), mqttPrefix,
          h("div", { class: "row", style: { marginTop: "10px" } }, h("button", { class: "primary", onclick: saveMqtt }, "Save")),
          h("p", { class: "small muted" }, "Timers for switching on at sunset and off later are on the ", h("a", { href: "#/schedule" }, "Schedule"), " page; they run on this server, with or without Home Assistant."),
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
      loadTrees()
        .then(() => Promise.all([load(), loadControllers()]))
        .catch((e) => show(fmtErr(e), "fail"));
    },
  };
}
