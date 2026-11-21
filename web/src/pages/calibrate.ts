import { api, fmtErr, h, LiveSocket, type Page } from "../api";
import { CameraCapture, MotionSensor, WakeLock } from "../camera";
import { statusView, type Status } from "./status";

const DEVICE_KEY = "treelights.cameraDevice";

function storedDevice(): string | null {
  try {
    return localStorage.getItem(DEVICE_KEY);
  } catch {
    return null;
  }
}

function storeDevice(id: string | undefined): void {
  try {
    if (id) localStorage.setItem(DEVICE_KEY, id);
  } catch {
    /* storage unavailable */
  }
}

export function calibratePage(): Page {
  let cam: CameraCapture | null = null;
  let sock: LiveSocket | null = null;
  let ctl: LiveSocket | null = null;
  const motion = new MotionSensor();
  const wake = new WakeLock();
  let hb: number | undefined;
  let quality = 0.9;
  let reqSize = { width: 1920, height: 1080 };
  const queue: Promise<void>[] = [];

  return {
    mount(root) {
      if (!window.isSecureContext || !navigator.mediaDevices) {
        root.append(h("h1", {}, "Calibrate"), h("div", { class: "notice fail" },
          "The camera needs HTTPS. Open this page through the https:// address shown on the server's setup page."));
        return;
      }
      const video = h("video", { autoplay: true, muted: true, playsinline: true }) as HTMLVideoElement;
      const overlay = h("canvas", { class: "overlay" }) as HTMLCanvasElement;
      const camStatus = h("div", { class: "cam-status" }, "Camera off");
      const countdown = h("div", { class: "countdown" });
      const wrap = h("div", { class: "cam-wrap" }, video, overlay, countdown, camStatus);
      const devSelect = h("select") as HTMLSelectElement;
      const lockInfo = h("div", { class: "small muted" });
      const modeSelect = h("select", {},
        h("option", { value: "hybrid" }, "Hybrid (fast, default)"),
        h("option", { value: "sequential" }, "One LED at a time (slower)")) as HTMLSelectElement;
      const panel = h("div");
      const msg = h("div");
      const startBtn = h("button", { class: "primary", disabled: true, onclick: () => startView() }, "Start view (5 s)");
      const sv = statusView();

      cam = new CameraCapture(video);

      async function listDevices() {
        const devs = await CameraCapture.backCameras();
        devSelect.replaceChildren(...devs.map((d, k) => h("option", { value: d.deviceId }, d.label || `Camera ${k + 1}`)));
        const cur = cam?.deviceId;
        if (cur) devSelect.value = cur;
      }

      async function startCamera(deviceId: string | null) {
        try {
          await cam!.start(deviceId, reqSize.width, reqSize.height);
          storeDevice(cam!.deviceId);
          await listDevices();
          const { width, height } = cam!.size;
          camStatus.textContent = `Camera on: ${width}×${height}. Light the tree, then lock the camera.`;
          connect();
        } catch (e) {
          msg.replaceChildren(h("div", { class: "notice fail" }, `Camera: ${fmtErr(e)}`));
        }
      }

      async function lockCamera() {
        const l = await cam!.lock();
        const chip = (name: string, ok: boolean) => h("span", { class: `pill ${ok ? "ok" : "warn"}` }, `${name}: ${ok ? "locked" : "auto"}`);
        lockInfo.replaceChildren(chip("Exposure", l.exposure), " ", chip("Focus", l.focus), " ", chip("White balance", l.whiteBalance),
          !(l.exposure && l.focus) ? h("div", {}, "Some settings can't be locked on this phone. Capture still works; it relies more on its checks.") : "");
        sendHello();
      }

      function hello() {
        const { width, height } = cam!.size;
        return { type: "hello", role: "camera", camera: { width, height, deviceId: cam!.deviceId, locks: cam!.locks }, up_cam: motion.up };
      }

      function sendHello() {
        // Updates the camera description (resolution, locks) without reconnecting.
        sock?.sendJson({ ...hello(), type: "camera_info" });
      }

      function connect() {
        if (sock) return;
        sock = new LiveSocket("/ws/capture");
        sock.onopen = () => sock!.sendJson(hello());
        sock.onstate = (open) => {
          startBtn.disabled = !open;
          if (!open) camStatus.textContent = "Reconnecting to the server...";
        };
        sock.onmessage = (ev) => {
          if (typeof ev.data !== "string") return;
          const m = JSON.parse(ev.data);
          if (m.type === "welcome") {
            quality = m.settings?.jpeg_quality ?? quality;
            const s = m.settings;
            if (s && (s.width !== reqSize.width || s.height !== reqSize.height)) reqSize = { width: s.width, height: s.height };
          } else if (m.type === "capture") {
            enqueue(() => capture(m));
          } else if (m.type === "probe_start") {
            probe.start(m.id);
          } else if (m.type === "probe_mark") {
            probe.mark(m.on);
          } else if (m.type === "probe_end") {
            const delays = probe.finish();
            sock!.sendJson({ type: "probe_result", id: m.id, delays_ms: delays });
          } else if (m.type === "status") {
            onStatus(m);
          }
        };
        ctl = new LiveSocket("/ws/control");
        ctl.onmessage = (ev) => {
          const m = JSON.parse(ev.data);
          if (m.type === "error") msg.replaceChildren(h("div", { class: "notice fail" }, m.message));
        };
        hb = window.setInterval(() => sock?.sendJson({ type: "hb", up_cam: motion.up }), 1000);
        motion.onSpike = (mag) => sock?.sendJson({ type: "motion", magnitude: mag });
        motion.start();
      }

      function enqueue(fn: () => Promise<void>) {
        const prev = queue[queue.length - 1] ?? Promise.resolve();
        const next = prev.then(fn, fn);
        queue.push(next);
        if (queue.length > 4) queue.shift();
      }

      async function capture(m: { step: number; kind: string; wait_ms: number }) {
        const blob = await cam!.grab(m.wait_ms, quality);
        const header = new TextEncoder().encode(JSON.stringify({ type: "frame", step: m.step, kind: m.kind }));
        const buf = new Uint8Array(4 + header.length + blob.size);
        new DataView(buf.buffer).setUint32(0, header.length);
        buf.set(header, 4);
        buf.set(new Uint8Array(await blob.arrayBuffer()), 4 + header.length);
        sock!.send(buf.buffer);
      }

      // Latency probe: time from each mark to the first frame whose brightness moved.
      const probe = (() => {
        let active = false;
        let samples: number[] = [];
        let pending: { t: number; on: boolean; base: number } | null = null;
        let delays: number[] = [];
        let level = 0;
        const loop = async () => {
          while (active) {
            await cam!.nextFrame();
            level = cam!.brightness();
            samples.push(level);
            if (pending) {
              const noise = Math.max(1.0, spread(samples.slice(-30)) * 0.25);
              const moved = pending.on ? level > pending.base + noise : level < pending.base - noise;
              if (moved) {
                delays.push(performance.now() - pending.t);
                pending = null;
              }
            }
          }
        };
        return {
          start(_id: number) {
            active = true;
            samples = [];
            delays = [];
            pending = null;
            loop();
          },
          mark(on: boolean) {
            pending = { t: performance.now(), on, base: level };
          },
          finish(): number[] {
            active = false;
            return delays;
          },
        };
      })();

      function onStatus(s: Status) {
        sv.update(s);
        const pct = s.total ? Math.round((100 * s.done) / s.total) : 0;
        camStatus.textContent = `${s.phase}${s.total ? ` ${pct}%` : ""} — ${s.message || ""}`;
        const ctx = overlay.getContext("2d")!;
        overlay.width = video.clientWidth;
        overlay.height = video.clientHeight;
        ctx.clearRect(0, 0, overlay.width, overlay.height);
        if (s.last && video.videoWidth) {
          const k = video.clientWidth / video.videoWidth;
          ctx.strokeStyle = "#3fb37f";
          ctx.lineWidth = 2;
          ctx.beginPath();
          ctx.arc(s.last.x * k, s.last.y * k, 10, 0, 2 * Math.PI);
          ctx.stroke();
        }
      }

      function startView() {
        let n = 5;
        countdown.textContent = String(n);
        const t = window.setInterval(() => {
          n -= 1;
          countdown.textContent = n > 0 ? String(n) : "";
          if (n <= 0) window.clearInterval(t);
        }, 1000);
        ctl?.sendJson({ type: "start_view", mode: modeSelect.value, delay_s: 5 });
      }

      async function goFullscreen() {
        try {
          await wrap.requestFullscreen();
          await (screen.orientation as any).lock?.(screen.orientation.type);
        } catch {
          /* not supported: fine */
        }
      }

      root.append(
        h("h1", {}, "Calibrate"),
        h("p", { class: "muted small" }, "Put this phone on a tripod facing the tree and leave it there for the whole scan. Start each view from here or from the Control page on another device. Turn the tree between views."),
        msg,
        wrap,
        h("div", { class: "card", style: { marginTop: "12px" } },
          h("div", { class: "row" },
            h("button", { class: "primary", onclick: async () => { await wake.acquire(); startCamera(storedDevice()); } }, "Start camera"),
            h("button", { onclick: lockCamera }, "Lock camera"),
            h("button", { onclick: goFullscreen }, "Fullscreen"),
          ),
          h("label", {}, "Camera"),
          devSelect,
          lockInfo,
          h("label", {}, "Capture mode"),
          modeSelect,
          h("div", { class: "row", style: { marginTop: "10px" } },
            startBtn,
            h("button", { onclick: () => ctl?.sendJson({ type: "stop" }) }, "Stop"),
            h("button", { onclick: () => ctl?.sendJson({ type: "resume" }) }, "Resume"),
          ),
        ),
        panel,
        sv.el,
        videoCard(),
      );
      devSelect.onchange = () => startCamera(devSelect.value);
      api("/api/state").then((s) => {
        if (!s.wled.host) msg.replaceChildren(h("div", { class: "notice warn" }, "No WLED controller selected yet. Go to Setup first."));
      }).catch(() => {});
    },
    unmount() {
      if (hb) window.clearInterval(hb);
      motion.stop();
      wake.release();
      sock?.close();
      ctl?.close();
      cam?.stop();
    },
  };
}

function spread(a: number[]): number {
  if (a.length < 2) return 0;
  return Math.max(...a) - Math.min(...a);
}

/** Recorded-video mode (architecture §7.7): play the sequence, record it with the phone's
 *  own camera app (manual exposure/focus possible), upload, decode. */
function videoCard(): HTMLElement {
  const msg = h("div");
  const file = h("input", { type: "file", accept: "video/*" }) as HTMLInputElement;
  file.setAttribute("capture", "environment");
  const bar = h("div");
  const progress = h("div", { class: "progress", style: { display: "none" } }, bar);
  let view: string | null = null;
  let scan: string | null = null;
  const show = (text: string, cls = "ok") => msg.replaceChildren(h("div", { class: `notice ${cls}` }, text));

  async function play() {
    try {
      const st = await api("/api/state");
      scan = st.scan ?? (await api("/api/scans", { body: {} })).scan;
      const r = await api(`/api/scans/${scan}/video/play`, { body: { countdown_s: 10 } });
      view = r.view;
      show(`Start recording now. The sequence begins in ${r.countdown_s} s (the tree pulses orange until then) and lasts ${Math.round(r.duration_s / 60 * 10) / 10} min. Keep the phone still, stop recording after the final flashes, then upload.`, "warn");
    } catch (e) {
      show(fmtErr(e), "fail");
    }
  }

  async function upload() {
    const f = file.files?.[0];
    if (!f || !scan || !view) return show("Play the sequence first, then choose the recording.", "warn");
    const base = `/api/scans/${scan}/video/${view}`;
    progress.style.display = "";
    const CHUNK = 4 * 2 ** 20;
    try {
      let { size } = await api(`${base}/upload`);
      while (size < f.size) {
        const body = f.slice(size, Math.min(size + CHUNK, f.size));
        const res = await fetch(`${base}/upload?offset=${size}`, { method: "PUT", body, credentials: "same-origin" });
        if (res.ok) size = (await res.json()).size;
        else if (res.status === 409) size = (await api(`${base}/upload`)).size; // resume from the server's size
        else throw new Error(`upload failed (${res.status})`);
        bar.style.width = `${(100 * size) / f.size}%`;
      }
      show("Uploaded. Decoding... (about a minute)", "warn");
      let job = await api(`${base}/decode`, { body: {} });
      while (job.status === "running") {
        await new Promise((r) => setTimeout(r, 2000));
        job = await api(`/api/jobs/${job.id}`);
      }
      if (job.status !== "done") throw new Error(job.error ?? "decode failed");
      const r = job.result;
      show(`Decoded ${r.detections} LEDs in ${r.segments.length} segment(s), ${r.blocks_ok} of ${r.blocks_total} blocks aligned.` +
        (r.failed_leds.length ? ` ${r.failed_leds.length} LEDs need a re-scan.` : "") + (r.warnings.length ? " " + r.warnings.join(" ") : ""),
        r.blocks_ok === r.blocks_total ? "ok" : "warn");
    } catch (e) {
      show(`${fmtErr(e)} You can retry; the upload resumes where it stopped.`, "fail");
    }
  }

  return h("div", { class: "card" },
    h("h2", {}, "Video mode (fallback)"),
    h("p", { class: "small muted" }, "If live capture doesn't work, record the sequence with the phone's own camera app instead. Pro/manual mode with fixed focus and exposure works best. Turn the tree between recordings like between views."),
    h("div", { class: "row" }, h("button", { onclick: play }, "Play sequence")),
    h("label", {}, "Recording"),
    file,
    h("div", { class: "row", style: { marginTop: "8px" } }, h("button", { class: "primary", onclick: upload }, "Upload and decode")),
    progress,
    msg,
  );
}
