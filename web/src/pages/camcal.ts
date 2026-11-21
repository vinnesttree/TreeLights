import { api, fmtErr, h, type Page } from "../api";
import { CameraCapture } from "../camera";

/** Optional one-time camera calibration (architecture §9.2). */
export function camcalPage(): Page {
  let cam: CameraCapture | null = null;
  return {
    mount(root) {
      const msg = h("div");
      const show = (text: string, cls = "ok") => msg.replaceChildren(h("div", { class: `notice ${cls}` }, text));

      function showBoard() {
        const w = Math.round(window.screen.width * devicePixelRatio);
        const hgt = Math.round(window.screen.height * devicePixelRatio);
        const img = h("img", { src: `/api/camcal/board.png?w=${w}&h=${hgt}`, style: { width: "100vw", height: "100vh", objectFit: "contain", background: "#fff" } });
        const wrap = h("div", { style: { position: "fixed", inset: "0", background: "#fff", zIndex: "100" }, onclick: () => { document.exitFullscreen().catch(() => {}); wrap.remove(); } }, img);
        document.body.append(wrap);
        wrap.requestFullscreen().catch(() => {});
      }

      const video = h("video", { autoplay: true, muted: true, playsinline: true }) as HTMLVideoElement;
      const count = h("div", { class: "small muted" }, "No views yet.");
      cam = new CameraCapture(video);

      async function capture() {
        if (!cam?.track) return show("Start the camera first.", "warn");
        try {
          const blob = await cam.grab(0, 0.95);
          const { width, height } = cam.size;
          const res = await fetch(`/api/camcal/frame?width=${width}&height=${height}&device=${encodeURIComponent(cam.deviceId ?? "")}`,
            { method: "POST", body: blob, credentials: "same-origin" });
          if (!res.ok) throw new Error(await res.text());
          const r = await res.json();
          count.textContent = `${r.frames} good view(s). ${r.corners ? `Last view: ${r.corners} corners.` : "Last view: board not found. Get the whole board in the picture, without glare."}`;
        } catch (e) {
          show(fmtErr(e), "fail");
        }
      }

      async function solve() {
        if (!cam?.track) return show("Start the camera first.", "warn");
        const { width, height } = cam.size;
        try {
          const p = await api("/api/camcal/solve", { body: { device: cam.deviceId, width, height } });
          show(`Calibrated from ${p.views} views: focal length ${p.f.toFixed(0)} px (${(2 * Math.atan(Math.max(width, height) / 2 / p.f) * 180 / Math.PI).toFixed(1)}° wide), fit error ${p.rms_px.toFixed(2)} px. Future maps from this camera use it.`);
        } catch (e) {
          show(fmtErr(e), "fail");
        }
      }

      root.append(
        h("h1", {}, "Camera calibration (optional)"),
        h("p", { class: "muted small" }, "Improves map accuracy a little. Do it once per phone, with the same camera and resolution used for scanning. Maps work without it."),
        msg,
        h("div", { class: "card" },
          h("h2", {}, "1. On a computer"),
          h("p", { class: "small" }, "Open this page on a computer or TV and show the board full screen. Turn screen brightness down if it glares."),
          h("button", { onclick: showBoard }, "Show board"),
        ),
        h("div", { class: "card" },
          h("h2", {}, "2. On the phone"),
          h("p", { class: "small" }, "Capture about 15 views of the board from different angles and distances, tilting the phone each time. The whole board should be visible."),
          h("div", { class: "cam-wrap" }, video),
          h("div", { class: "row", style: { marginTop: "8px" } },
            h("button", { onclick: async () => { try { await cam!.start(null, 1920, 1080); } catch (e) { show(fmtErr(e), "fail"); } } }, "Start camera"),
            h("button", { class: "primary", onclick: capture }, "Capture view"),
            h("button", { onclick: solve }, "Compute"),
          ),
          count,
        ),
      );
    },
    unmount() {
      cam?.stop();
    },
  };
}
