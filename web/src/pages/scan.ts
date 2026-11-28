// Walk-around scan (architecture §7.9): the user walks once around the tree with
// the phone's camera running. The LEDs blink their indices; the phone streams
// frames and its orientation to the server live, records full-rate video at the
// same time, and uploads the recording when the walk ends. The server decodes
// both and builds the map.

import { api, fmtErr, h, LiveSocket, withTree, type Page } from "../api";
import { CameraCapture, WakeLock } from "../camera";
import { Orientation, phoneNow, phoneTime, type Quat } from "../orientation";

const SECTORS = 12;
const LIVE_FPS = 8;
const LIVE_WIDTH = 960;
const MAX_RATE_DEG_S = 35; // turning faster than this blurs the LEDs
const CHUNK = 1 << 20;

type WalkStatus = {
  type: "walk_status";
  state: string;
  locked?: boolean;
  identified?: number;
  sectors?: number[];
  covered?: number;
  heading_deg?: number;
  warnings?: string[];
  level?: number;
  elapsed_s?: number;
  blobs_per_frame?: number;
};

const WARNINGS: Record<string, string> = {
  out_of_frame: "Some LEDs are at the edge of the picture: step back so the whole tree fits.",
  few_leds: "Hardly any LEDs in view: point the camera at the tree.",
  pattern_not_found: "The blinking pattern isn't being picked up. Is the tree in view and the room dark enough?",
};

function packFrame(header: object, jpeg: ArrayBuffer): ArrayBuffer {
  const hb = new TextEncoder().encode(JSON.stringify(header));
  const out = new Uint8Array(4 + hb.length + jpeg.byteLength);
  new DataView(out.buffer).setUint32(0, hb.length, false);
  out.set(hb, 4);
  out.set(new Uint8Array(jpeg), 4 + hb.length);
  return out.buffer;
}

function pickMime(): string | undefined {
  const cands = ["video/mp4;codecs=avc1", "video/mp4", "video/webm;codecs=vp9", "video/webm;codecs=vp8", "video/webm"];
  const MR = (window as any).MediaRecorder;
  return cands.find((m) => MR?.isTypeSupported?.(m));
}

export function scanPage(): Page {
  let cam: CameraCapture | null = null;
  let sock: LiveSocket | null = null;
  let recorder: MediaRecorder | null = null;
  const chunks: Blob[] = [];
  const orient = new Orientation();
  const wake = new WakeLock();
  let timers: number[] = [];
  let state: "idle" | "starting" | "walking" | "stopping" | "uploading" | "processing" | "done" = "idle";
  let fullRes = false; // this walk records at full resolution (Setup option)
  let scan: string | null = null;
  // ?scan=<id>: add this walk to an existing scan (a re-scan, §11.5).
  const addTo = new URLSearchParams(location.hash.split("?")[1] ?? "").get("scan");
  let view: string | null = null;
  let server: WalkStatus | null = null;
  let visited = new Array(SECTORS).fill(false);
  let heading0: number | null = null;
  let turned = 0; // degrees turned in total (unwrapped)
  let lastHeading: number | null = null;
  let walkStart = 0;
  let unmounted = false;

  return {
    mount(root) {
      if (!window.isSecureContext || !navigator.mediaDevices) {
        root.append(h("h1", {}, "Scan"), h("div", { class: "notice fail" },
          "The camera and motion sensors need HTTPS. Open this page through the https:// address shown on the server's setup page."));
        return;
      }
      const video = h("video", { autoplay: true, muted: true, playsinline: true }) as HTMLVideoElement;
      const ring = h("canvas", { class: "ring", width: 240, height: 240 }) as HTMLCanvasElement;
      const banner = h("div", { class: "walk-warn" });
      const camStatus = h("div", { class: "cam-status" }, "Camera off");
      const wrap = h("div", { class: "cam-wrap walk" }, video, ring, banner, camStatus);
      const startBtn = h("button", { class: "primary big", onclick: () => start() }, "Start walking");
      const stopBtn = h("button", { class: "danger big", onclick: () => stop("stopped"), style: { display: "none" } }, "Stop");
      const msg = h("div");
      const progress = h("div", { class: "progress", style: { display: "none" } }, h("div", { style: { width: "0%" } }));
      const result = h("div");
      const checks = h("ul", { class: "checklist" }, h("li", { class: "muted" }, "Checking the server..."));
      const guide = h("div", {},
        addTo ? h("div", { class: "notice ok" }, `This walk is added to scan ${addTo} and solved together with it. Walk past the LEDs you chose to re-scan (all the way round, or just that side).`) : "",
        h("div", { class: "card small" },
          h("h2", {}, "Before you start"),
          checks,
          h("ul", { class: "checklist" },
            h("li", {}, "Dim the room: turn off lamps, ceiling lights and other fairy lights you can. A TV or a window behind the tree is fine, but darker is better."),
            h("li", {}, "Clear a path all the way around the tree, about 1.5–2 m out, so you can walk without looking down."),
            h("li", {}, "Use a phone that is charged, on good Wi-Fi near the router. 5 GHz Wi-Fi is best."),
            h("li", {}, "Wipe the camera lens."),
            h("li", {}, "Find the starting point: stand where the whole tree, top to bottom, fits in the picture with a little space around it."),
          )),
        h("div", { class: "card small" },
          h("h2", {}, "How to walk"),
          h("ol", {},
            h("li", {}, h("b", {}, "Tap Start walking. "), "Allow the camera and motion sensors if asked. All LEDs start blinking. That's the pattern being read, so leave them on."),
            h("li", {}, h("b", {}, "Hold the phone steady, pointing at the tree, "), "best at about the middle height of the tree. Don't rotate it between portrait and landscape during the walk."),
            h("li", {}, h("b", {}, "Walk slowly once around the tree, "), "about a minute for the whole circle, keeping the whole tree in the picture. Your path doesn't need to be a neat circle: going closer or further (around furniture, say) and holding the phone higher or lower is fine. Small steps, no hurry."),
            h("li", {}, h("b", {}, "Watch the ring. "), "Each segment is one side of the tree: it turns yellow when you've passed it and green when enough LEDs were found there. The number in the middle counts the LEDs found so far."),
            h("li", {}, h("b", {}, "Keep going a little past where you started, "), "then tap Stop. Keep this page open while the video uploads and the map is built (a minute or two)."),
          )),
      );
      const guideMore = h("div", {},
        h("details", { class: "card small" },
          h("summary", {}, "What the warnings mean"),
          h("ul", {},
            h("li", {}, h("b", {}, "Slow down: "), "you're turning faster than the camera can follow; the LEDs smear. Walk slower."),
            h("li", {}, h("b", {}, "Step back: "), "LEDs are at the edge of the picture. Move further out until the whole tree fits."),
            h("li", {}, h("b", {}, "Point the camera at the tree: "), "hardly any LEDs are in view."),
            h("li", {}, h("b", {}, "The blinking pattern isn't being picked up: "), "after about 20 s the server still can't see the pattern. Check that the tree is in view and blinking, and dim the room more."),
            h("li", {}, h("b", {}, "Reconnecting... keep walking: "), "the Wi-Fi dropped. Keep walking; the walk continues when the phone reconnects (within 15 s)."),
          )),
        h("details", { class: "card small" },
          h("summary", {}, "After you tap Stop"),
          h("p", {}, "The phone uploads its recording of the walk (it resumes if the connection drops). The server then decodes it together with the frames it received during the walk, and builds the 3D map. You'll see how many LEDs were measured, then you can open the map to check it, run the sweep test, and activate it."),
          h("p", {}, "LEDs that were hidden from every side are placed from the wiring and marked as estimated. To improve them, use Re-scan on the Map page and walk past that side again."),
        ),
        h("details", { class: "card small" },
          h("summary", {}, "If something goes wrong"),
          h("ul", {},
            h("li", {}, h("b", {}, "The camera doesn't start: "), "allow camera access for this site in Chrome (lock icon next to the address), then reload."),
            h("li", {}, h("b", {}, "\"Using the basic orientation events\": "), "fine; the scan still works. The map is built from the video either way."),
            h("li", {}, h("b", {}, "The LEDs don't blink when you tap Start: "), "check Setup: the controller must be selected and its realtime check must pass."),
            h("li", {}, h("b", {}, "The upload fails: "), "the map is built from the frames sent during the walk, which works but measures fewer LEDs. Walk again on better Wi-Fi if the result is poor."),
            h("li", {}, h("b", {}, "Few LEDs found on one side: "), "walk that side again, slower and a step further away, using Re-scan on the Map page."),
            h("li", {}, h("b", {}, "Phone held very low (near the floor) or very high: "), "it works, but LEDs at the same height as the phone line up behind each other in the picture and some are missed. A second walk with the phone at a different height (Re-scan) finds them."),
            h("li", {}, h("b", {}, "The tree is against a wall: "), "walk as far round as you can. The rest is estimated from the wiring, or use the tripod mode (link below the camera picture)."),
          )),
      );
      const key = (sw: HTMLElement, text: string) => h("span", { class: "key" }, sw, text);
      const legend = h("div", { class: "ring-legend small" },
        key(h("span", { class: "sw", style: { background: "rgba(150,150,150,.6)" } }), "not passed yet"),
        key(h("span", { class: "sw", style: { background: "rgb(240,190,60)" } }), "passed"),
        key(h("span", { class: "sw", style: { background: "rgb(60,200,110)" } }), "enough LEDs found"),
        key(h("span", { class: "sw dot" }), "where you are now"));
      root.append(
        h("h1", {}, "Scan the tree"),
        guide,
        wrap,
        legend,
        h("p", { class: "muted small" }, "Tree turnable, or can't be walked around? ",
          h("a", { href: "#/calibrate" }, "Use the tripod mode instead"), "."),
        h("div", { class: "row" }, startBtn, stopBtn),
        progress,
        msg,
        result,
        guideMore,
      );
      runChecks();

      // The server's side of the checklist: controller, LED count, tree height, map warnings.
      async function runChecks() {
        const item = (ok: boolean | null, text: string, link?: [string, string]) =>
          h("li", { class: ok === null ? "info" : ok ? "ok" : "bad" }, text, link ? " " : "", link ? h("a", { href: link[0] }, link[1]) : "");
        try {
          const s = await api("/api/state");
          const host = s.wled?.host;
          const items = [
            item(!!host, host ? `Controller: ${host}${s.wled.version ? ` (WLED ${s.wled.version})` : ""}` : "No WLED controller selected yet.", host ? undefined : ["#/setup", "Open Setup"]),
            ...(host ? [item(s.wled.monitor_ok !== false, s.wled.monitor_ok === false ? "The controller isn't answering right now." : `LEDs: ${s.led_count}`,
              s.wled.monitor_ok === false ? ["#/setup", "Check Setup"] : undefined)] : []),
            item(s.tree_height_cm ? true : null, s.tree_height_cm ? `Tree height: ${s.tree_height_cm} cm` : "No tree height entered: the map will be in tree heights instead of cm (optional).", s.tree_height_cm ? undefined : ["#/setup", "Enter it"]),
          ];
          if (s.lease && s.lease.owner !== "walk") items.push(item(false, `The LEDs are in use (${s.lease.kind}). Stop the test animation or capture first.`, ["#/test", "Test page"]));
          if (s.map_warning) items.push(item(null, s.map_warning));
          checks.replaceChildren(...items);
        } catch (e) {
          checks.replaceChildren(item(false, `Couldn't reach the server: ${fmtErr(e)}`));
        }
      }
      cam = new CameraCapture(video);
      drawRing();

      function setMsg(text: string, kind = "") {
        msg.replaceChildren(text ? h("div", { class: `notice ${kind}` }, text) : "");
      }

      async function start() {
        if (state !== "idle" && state !== "done") return;
        state = "starting";
        startBtn.disabled = true;
        result.replaceChildren();
        setMsg("");
        try {
          const src = await orient.start();
          if (src !== "sensor") setMsg("Using the basic orientation events (the motion sensor API isn't available).", "warn");
          // Full resolution (Setup, off by default) helps dense trees; the server then finds the
          // LEDs at the recording's own size.
          fullRes = await api("/api/config").then((c) => !!c.capture?.walk_full_resolution, () => false);
          await cam!.start(null, fullRes ? 1920 : 1280, fullRes ? 1080 : 720);
          await new Promise((r) => setTimeout(r, 800)); // let exposure settle on the lit tree
          await cam!.lock();
          await wake.acquire();
          try {
            await document.documentElement.requestFullscreen?.();
            await (screen.orientation as any)?.lock?.(screen.orientation.type);
          } catch {
            /* keeping the screen orientation is nicer, not required */
          }
        } catch (e) {
          state = "idle";
          startBtn.disabled = false;
          setMsg(`Couldn't start: ${fmtErr(e)}`, "fail");
          return;
        }
        visited = new Array(SECTORS).fill(false);
        heading0 = lastHeading = null;
        turned = 0;
        server = null;
        connect();
      }

      function hello() {
        const { width, height } = cam!.size;
        const resume = state === "walking" || state === "stopping";
        return { type: "hello", role: "walk", scan: resume ? scan : addTo ?? undefined, view: resume ? view : undefined,
          camera: { width, height, device: cam!.deviceId, sensor: orient.source, screen: screen.orientation?.type,
            full_resolution: fullRes } };
      }

      function connect() {
        sock = new LiveSocket("/ws/walk");
        sock.onopen = () => sock!.sendJson(hello());
        sock.onstate = (open) => {
          if (!open && state === "walking") banner.textContent = "Reconnecting to the server... keep walking.";
        };
        sock.onmessage = (ev) => {
          if (typeof ev.data !== "string") return;
          const m = JSON.parse(ev.data);
          if (m.type === "welcome") {
            scan = m.scan;
            if (state === "starting") sock!.sendJson({ type: "start" });
            else banner.textContent = "";
          } else if (m.type === "started") {
            view = m.view;
            beginWalk();
          } else if (m.type === "walk_status") {
            server = m;
            renderStatus();
          } else if (m.type === "stopped") {
            void finish(m);
          } else if (m.type === "error") {
            setMsg(m.message, "fail");
            if (state === "starting") {
              state = "idle";
              startBtn.disabled = false;
              sock?.close();
              sock = null;
            }
          }
        };
      }

      function showGuide(on: boolean) {
        guide.style.display = guideMore.style.display = on ? "" : "none";
        if (!on) wrap.scrollIntoView({ block: "start", behavior: "smooth" });
      }

      function beginWalk() {
        showGuide(false);
        state = "walking";
        walkStart = phoneNow();
        startBtn.style.display = "none";
        stopBtn.style.display = "";
        camStatus.textContent = "Walk slowly around the tree...";
        // Full-rate recording.
        chunks.length = 0;
        const mime = pickMime();
        try {
          recorder = new MediaRecorder(cam!.stream!, { mimeType: mime, videoBitsPerSecond: fullRes ? 16_000_000 : 8_000_000 });
          recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
          recorder.onstart = () => sock?.sendJson({ type: "rec_start", t: phoneNow() });
          recorder.start(1000);
        } catch (e) {
          recorder = null;
          setMsg(`Recording isn't available (${fmtErr(e)}); the live frames are used on their own.`, "warn");
        }
        // Orientation samples, ~10 batches/s.
        timers.push(window.setInterval(() => {
          const d = orient.drain();
          if (d.t.length) sock?.sendJson({ type: "motion", t: d.t, q: d.q });
          updateRing();
        }, 100));
        liveLoop();
      }

      // Live frames: ~8/s, downscaled, with the capture time and the latest orientation.
      const canvas = document.createElement("canvas");
      let lastSent = 0;
      let encoding = false;
      function liveLoop() {
        const v: any = video;
        const next = (cb: (t: number) => void) => {
          if (typeof v.requestVideoFrameCallback === "function") {
            v.requestVideoFrameCallback((_now: number, meta: any) => cb(meta.captureTime ?? meta.expectedDisplayTime ?? performance.now()));
          } else {
            requestAnimationFrame(() => cb(performance.now()));
          }
        };
        const onFrame = (domTs: number) => {
          if (state !== "walking" || unmounted) return;
          const t = phoneTime(domTs);
          const ws = sock?.ws;
          if (!encoding && t - lastSent >= 1 / LIVE_FPS - 0.01 && ws && ws.readyState === WebSocket.OPEN && ws.bufferedAmount < 2e6) {
            lastSent = t;
            encoding = true;
            const { width, height } = cam!.size;
            const w = Math.min(LIVE_WIDTH, width);
            canvas.width = w;
            canvas.height = Math.round((height * w) / width);
            canvas.getContext("2d")!.drawImage(video, 0, 0, canvas.width, canvas.height);
            const q: Quat | null = orient.latest;
            canvas.toBlob(async (b) => {
              encoding = false;
              if (!b || state !== "walking") return;
              sock?.send(packFrame({ t, q }, await b.arrayBuffer()));
            }, "image/jpeg", 0.85);
          }
          next(onFrame);
        };
        next(onFrame);
      }

      function updateRing() {
        const hd = orient.heading();
        if (hd !== null) {
          if (heading0 === null) heading0 = hd;
          if (lastHeading !== null) {
            let d = hd - lastHeading;
            d -= 360 * Math.round(d / 360);
            turned += d;
          }
          lastHeading = hd;
          const rel = (((hd - heading0) % 360) + 360) % 360;
          visited[Math.floor(rel / (360 / SECTORS)) % SECTORS] = true;
        }
        drawRing();
        const warn: string[] = [];
        if (orient.rate > MAX_RATE_DEG_S) warn.push("Slow down: turning too fast blurs the LEDs.");
        for (const w of server?.warnings ?? []) if (WARNINGS[w]) warn.push(WARNINGS[w]);
        if (state === "walking") banner.textContent = warn.join(" ");
        if (state === "walking" && Math.abs(turned) > 370 && (server?.covered ?? 0) >= SECTORS - 2) {
          camStatus.textContent = "Full circle done. Tap Stop when you're back at the start.";
        }
      }

      function drawRing() {
        const ctx = ring.getContext("2d")!;
        const W = ring.width, c = W / 2, r = W * 0.4;
        ctx.clearRect(0, 0, W, W);
        const counts = server?.sectors ?? [];
        for (let k = 0; k < SECTORS; k++) {
          const a0 = -Math.PI / 2 + (k * 2 * Math.PI) / SECTORS + 0.03, a1 = a0 + (2 * Math.PI) / SECTORS - 0.06;
          const n = counts[k] ?? 0;
          ctx.beginPath();
          ctx.arc(c, c, r, a0, a1);
          ctx.lineWidth = W * 0.12;
          ctx.strokeStyle = n >= 15 ? "rgba(60,200,110,.9)" : visited[k] ? "rgba(240,190,60,.85)" : "rgba(255,255,255,.18)";
          ctx.stroke();
        }
        // Where the camera faces now.
        if (lastHeading !== null && heading0 !== null) {
          const rel = ((lastHeading - heading0) * Math.PI) / 180 - Math.PI / 2;
          ctx.beginPath();
          ctx.arc(c + Math.cos(rel) * r, c + Math.sin(rel) * r, W * 0.05, 0, 2 * Math.PI);
          ctx.fillStyle = "#fff";
          ctx.fill();
        }
        ctx.fillStyle = "#fff";
        ctx.font = `600 ${Math.round(W * 0.11)}px system-ui, sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(server?.identified !== undefined ? String(server.identified) : "–", c, c - W * 0.03);
        ctx.font = `${Math.round(W * 0.06)}px system-ui, sans-serif`;
        ctx.fillText("LEDs found", c, c + W * 0.09);
      }

      function renderStatus() {
        if (!server || state !== "walking") return;
        const secs = Math.round(phoneNow() - walkStart);
        const lock = server.locked ? "pattern locked" : "finding the pattern...";
        camStatus.textContent = `${secs} s · ${lock} · ${server.identified ?? 0} LEDs identified · ${server.covered ?? 0}/${SECTORS} sides covered`;
        updateRing();
      }

      function stop(reason: string) {
        if (state !== "walking") return;
        state = "stopping";
        stopBtn.disabled = true;
        timers.forEach((t) => clearInterval(t));
        timers = [];
        const d = orient.drain();
        if (d.t.length) sock?.sendJson({ type: "motion", t: d.t, q: d.q });
        banner.textContent = "";
        camStatus.textContent = "Stopping...";
        // If the connection is down right now, keep trying: the socket reconnects (and
        // resumes the walk) by itself, and the server answers "stopped".
        const trySend = () => {
          if (state !== "stopping" || unmounted) return;
          if (!sock?.sendJson({ type: "stop", reason })) timers.push(window.setTimeout(trySend, 1000));
        };
        trySend();
      }

      async function finish(m: { scan: string; view: string; upload: string; process: string; reason?: string }) {
        timers.forEach((t) => clearInterval(t));
        timers = [];
        if (m.reason === "server restarted") setMsg("The server restarted during the walk; using what was captured up to then.", "warn");
        // Wait for the recorder's last chunk.
        let video: Blob | null = null;
        if (recorder && recorder.state !== "inactive") {
          await new Promise<void>((resolve) => {
            recorder!.onstop = () => resolve();
            recorder!.stop();
          });
        }
        if (chunks.length) video = new Blob(chunks, { type: recorder?.mimeType || "video/webm" });
        recorder = null;
        orient.stop();
        cam?.stop();
        wake.release();
        sock?.close();
        sock = null;
        try {
          await document.exitFullscreen?.();
        } catch {
          /* not in fullscreen */
        }
        stopBtn.style.display = "none";
        stopBtn.disabled = false;
        progress.style.display = "";
        const bar = progress.firstElementChild as HTMLElement;
        let useVideo = false;
        if (video) {
          state = "uploading";
          camStatus.textContent = `Uploading the recording (${(video.size / 1e6).toFixed(0)} MB)...`;
          try {
            await upload(m.upload, video, (f) => (bar.style.width = `${Math.round(f * 100)}%`));
            useVideo = true;
          } catch (e) {
            setMsg(`The upload failed (${fmtErr(e)}); building the map from the live frames only.`, "warn");
          }
        }
        state = "processing";
        bar.style.width = "100%";
        camStatus.textContent = "Building the map (a minute or two)...";
        try {
          let job = await api(m.process, { body: { video: useVideo } });
          while (job.status === "running") {
            await new Promise((r) => setTimeout(r, 1500));
            if (unmounted) return;
            job = await api(`/api/jobs/${job.id}`);
          }
          if (job.status !== "done") throw new Error(job.error ?? "failed");
          const q = job.result.quality;
          state = "done";
          progress.style.display = "none";
          camStatus.textContent = "Done.";
          result.replaceChildren(h("div", { class: "card" },
            h("h2", {}, "Map built"),
            h("div", {}, `${q.counts.measured} LEDs measured, ${q.counts.estimated} estimated, ${q.counts.unknown} unknown.`),
            ...(q.warnings ?? []).map((w: string) => h("div", { class: "notice warn" }, w)),
            h("div", { class: "row" },
              h("a", { href: `#/map?id=${job.result.map}` }, h("button", { class: "primary" }, "Review the map")),
              h("button", { onclick: () => { state = "idle"; startBtn.style.display = ""; startBtn.disabled = false; result.replaceChildren(); showGuide(true); runChecks(); } },
                "Scan again")),
          ));
        } catch (e) {
          state = "done";
          progress.style.display = "none";
          camStatus.textContent = "No map was built.";
          setMsg(`Building the map failed: ${fmtErr(e)}`, "fail");
          startBtn.style.display = "";
          startBtn.disabled = false;
          showGuide(true);
          runChecks();
        }
      }
    },
    unmount() {
      unmounted = true;
      timers.forEach((t) => clearInterval(t));
      if (state === "walking") sock?.sendJson({ type: "stop", reason: "page closed" });
      try {
        recorder?.stop();
      } catch {
        /* already stopped */
      }
      orient.stop();
      wake.release();
      sock?.close();
      cam?.stop();
    },
  };
}

async function upload(url: string, blob: Blob, onProgress: (f: number) => void): Promise<void> {
  let size = (await api<{ size: number }>(url)).size;
  let failures = 0;
  while (size < blob.size) {
    try {
      const res = await fetch(withTree(`${url}?offset=${size}`), { method: "PUT", body: blob.slice(size, size + CHUNK), credentials: "same-origin" });
      if (res.status === 409) {
        size = (await api<{ size: number }>(url)).size; // resume from what the server has
        continue;
      }
      if (!res.ok) throw new Error(res.statusText);
      size = (await res.json()).size;
      failures = 0;
      onProgress(size / blob.size);
    } catch (e) {
      if (++failures > 5) throw e;
      await new Promise((r) => setTimeout(r, 1000 * failures));
    }
  }
}
