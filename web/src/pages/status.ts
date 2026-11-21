import { h } from "../api";

export type Status = {
  type: "status";
  scan: string;
  view?: string;
  segment?: string;
  phase: string;
  done: number;
  total: number;
  message: string;
  warnings: string[];
  paused: boolean;
  camera: boolean;
  wait_ms?: number;
  last?: { x: number; y: number; led: number };
  guidance?: { message?: string; measured?: number; total?: number; azimuths?: Record<string, number>; turn_deg?: number; done?: boolean } | null;
};

const PHASES: Record<string, string> = {
  idle: "Idle",
  starting: "Starting",
  waiting_camera: "Waiting for camera",
  latency: "Measuring delay",
  settling: "Waiting for the tree to settle",
  color_check: "Color check",
  binary: "Binary pass",
  spot_check: "Spot-check",
  sequential: "One LED at a time",
  retries: "Retries",
  paused: "Paused",
  done: "View done",
  stopped: "Stopped",
  failed: "Failed",
};

/** Progress, messages, warnings and turn guidance with a top-down sketch. */
export function statusView() {
  const phase = h("b");
  const bar = h("div");
  const progress = h("div", { class: "progress" }, bar);
  const message = h("div");
  const warnings = h("div");
  const guidance = h("div");
  const sketch = h("canvas", { width: 220, height: 220, style: { maxWidth: "220px", width: "100%" } }) as HTMLCanvasElement;
  const meta = h("div", { class: "small muted" });
  const el = h("div", { class: "card" }, h("div", { class: "row" }, phase, h("span", { class: "grow" }), meta), progress, message, warnings, guidance, sketch);

  function drawSketch(az: Record<string, number> | undefined, turn?: number) {
    const ctx = sketch.getContext("2d")!;
    const w = sketch.width;
    const c = w / 2;
    ctx.clearRect(0, 0, w, w);
    const fg = getComputedStyle(document.body).color;
    ctx.strokeStyle = fg;
    ctx.globalAlpha = 0.5;
    ctx.beginPath();
    ctx.arc(c, c, w * 0.18, 0, 2 * Math.PI);
    ctx.stroke();
    ctx.globalAlpha = 1;
    if (!az) return;
    const entries = Object.entries(az);
    for (const [k, a] of entries) {
      // Azimuth: counter-clockwise seen from above; screen y grows downward.
      const r = (a * Math.PI) / 180;
      const x = c + Math.cos(r) * w * 0.38;
      const y = c - Math.sin(r) * w * 0.38;
      ctx.fillStyle = "#3fb37f";
      ctx.beginPath();
      ctx.arc(x, y, 6, 0, 2 * Math.PI);
      ctx.fill();
      ctx.fillStyle = fg;
      ctx.font = "11px system-ui";
      ctx.fillText(k, x + 8, y + 4);
    }
    if (turn !== undefined && entries.length) {
      const last = entries[entries.length - 1][1];
      const r = ((last - turn) * Math.PI) / 180;
      ctx.strokeStyle = "#f2b84b";
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.arc(c + Math.cos(r) * w * 0.38, c - Math.sin(r) * w * 0.38, 9, 0, 2 * Math.PI);
      ctx.stroke();
      ctx.setLineDash([]);
    }
  }

  return {
    el,
    update(s: Status) {
      phase.textContent = `${PHASES[s.phase] ?? s.phase}${s.view ? ` · ${s.view}` : ""}${s.paused ? " (paused)" : ""}`;
      bar.style.width = s.total ? `${(100 * s.done) / s.total}%` : s.phase === "done" ? "100%" : "0%";
      message.textContent = s.message || "";
      meta.textContent = `${s.camera ? "camera connected" : "camera not connected"}${s.wait_ms ? ` · wait ${s.wait_ms} ms` : ""}`;
      warnings.replaceChildren(...(s.warnings || []).map((w) => h("div", { class: "notice warn small" }, w)));
      const g = s.guidance;
      guidance.replaceChildren(g?.message ? h("div", { class: `notice ${g.done ? "ok" : "warn"}` }, g.message,
        g.measured !== undefined ? h("div", { class: "small" }, `${g.measured} of ${g.total} LEDs measured so far.`) : "") : "");
      drawSketch(g?.azimuths, g?.turn_deg);
    },
  };
}
