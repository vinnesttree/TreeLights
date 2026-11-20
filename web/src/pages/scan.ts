// Walk-around scan (architecture §7.9): the user walks once around the tree with
// the phone's camera running. The LEDs blink their indices; the phone streams
// frames and its orientation to the server live, records full-rate video at the
// same time, and uploads the recording when the walk ends. The server decodes
// both and builds the map.

import { api, fmtErr, h, LiveSocket, type Page } from "../api";
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
      root.append(
        h("h1", {}, "Scan the tree"),
        addTo ? h("div", { class: "notice ok" }, `This walk is added to scan ${addTo} and solved together with it.`) : "",
        h("div", { class: "card small" },
          h("ol", {},
            h("li", {}, "Turn off other lights you can. Stand far enough back that the whole tree fits in the picture."),
            h("li", {}, "Tap Start. All LEDs start blinking; hold the phone upright and keep the tree in the middle of the picture."),
            h("li", {}, "Walk slowly, at an even pace, once around the tree (about a minute). The ring fills in as you go."),
            h("li", {}, "When you're back where you started, tap Stop. The video is uploaded and the map is built."),
          )),
        wrap,
        h("div", { class: "row" }, startBtn, stopBtn),
        progress,
        msg,
        result,
      );
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
          await cam!.start(null, 1280, 720);
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
          camera: { width, height, device: cam!.deviceId, sensor: orient.source, screen: screen.orientation?.type } };
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

      function beginWalk() {
        state = "walking";
        walkStart = phoneNow();
        startBtn.style.display = "none";
        stopBtn.style.display = "";
        camStatus.textContent = "Walk slowly around the tree...";
        // Full-rate recording.
        chunks.length = 0;
        const mime = pickMime();
        try {
          recorder = new MediaRecorder(cam!.stream!, { mimeType: mime, videoBitsPerSecond: 8_000_000 });
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
              h("button", { onclick: () => { state = "idle"; startBtn.style.display = ""; startBtn.disabled = false; result.replaceChildren(); } },
                "Scan again")),
          ));
        } catch (e) {
          state = "done";
          progress.style.display = "none";
          camStatus.textContent = "No map was built.";
          setMsg(`Building the map failed: ${fmtErr(e)}`, "fail");
          startBtn.style.display = "";
          startBtn.disabled = false;
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
      const res = await fetch(`${url}?offset=${size}`, { method: "PUT", body: blob.slice(size, size + CHUNK), credentials: "same-origin" });
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
