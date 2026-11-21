// Camera handling for the Calibrate page (architecture §7.2).

export type Locks = { exposure: boolean; focus: boolean; whiteBalance: boolean };

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export class CameraCapture {
  video: HTMLVideoElement;
  stream: MediaStream | null = null;
  track: MediaStreamTrack | null = null;
  locks: Locks = { exposure: false, focus: false, whiteBalance: false };
  private canvas = document.createElement("canvas");
  private small = document.createElement("canvas");

  constructor(video: HTMLVideoElement) {
    this.video = video;
    this.video.muted = true;
    this.video.playsInline = true;
    this.small.width = 64;
    this.small.height = 36;
  }

  static async backCameras(): Promise<MediaDeviceInfo[]> {
    const all = await navigator.mediaDevices.enumerateDevices();
    return all.filter((d) => d.kind === "videoinput");
  }

  async start(deviceId: string | null, width: number, height: number): Promise<void> {
    this.stop();
    const video: MediaTrackConstraints = {
      width: { ideal: width },
      height: { ideal: height },
      frameRate: { ideal: 30 },
    };
    if (deviceId) video.deviceId = { exact: deviceId };
    else video.facingMode = { ideal: "environment" };
    this.stream = await navigator.mediaDevices.getUserMedia({ video, audio: false });
    this.track = this.stream.getVideoTracks()[0];
    // Fixed zoom: phones with several lenses may otherwise switch.
    await this.tryConstraints([{ zoom: 1 }]);
    this.video.srcObject = this.stream;
    await this.video.play();
    await this.nextFrame();
  }

  stop(): void {
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
    this.track = null;
  }

  get deviceId(): string | undefined {
    return this.track?.getSettings().deviceId;
  }

  get size(): { width: number; height: number } {
    return { width: this.video.videoWidth, height: this.video.videoHeight };
  }

  capabilities(): any {
    try {
      return (this.track as any)?.getCapabilities?.() ?? {};
    } catch {
      return {};
    }
  }

  private async tryConstraints(advanced: any[]): Promise<boolean> {
    if (!this.track) return false;
    try {
      await this.track.applyConstraints({ advanced } as any);
      return true;
    } catch {
      return false;
    }
  }

  /** Lock exposure, focus and white balance where Chrome offers it. */
  async lock(): Promise<Locks> {
    const caps = this.capabilities();
    const cur: any = this.track?.getSettings() ?? {};
    const locks: Locks = { exposure: false, focus: false, whiteBalance: false };
    if (caps.exposureMode?.includes("manual")) {
      const c: any = { exposureMode: "manual" };
      if (caps.exposureTime && cur.exposureTime) {
        // Keep the current exposure but no longer than one frame.
        c.exposureTime = Math.min(cur.exposureTime, Math.max(caps.exposureTime.min, 333));
      }
      locks.exposure = await this.tryConstraints([c]);
    }
    if (caps.focusMode?.includes("manual") && cur.focusDistance !== undefined) {
      locks.focus = await this.tryConstraints([{ focusMode: "manual", focusDistance: cur.focusDistance }]);
    } else if (caps.focusMode?.includes("single-shot")) {
      locks.focus = await this.tryConstraints([{ focusMode: "single-shot" }]);
    }
    if (caps.whiteBalanceMode?.includes("manual")) {
      const c: any = { whiteBalanceMode: "manual" };
      if (cur.colorTemperature) c.colorTemperature = cur.colorTemperature;
      locks.whiteBalance = await this.tryConstraints([c]);
    }
    this.locks = locks;
    return locks;
  }

  /** Resolves on the next frame presented by the video element. */
  nextFrame(): Promise<void> {
    const v: any = this.video;
    if (typeof v.requestVideoFrameCallback === "function") {
      return new Promise((r) => v.requestVideoFrameCallback(() => r()));
    }
    return new Promise((r) => requestAnimationFrame(() => r()));
  }

  /** Wait waitMs, then grab the first frame presented after that, as JPEG. */
  async grab(waitMs: number, quality: number): Promise<Blob> {
    await sleep(waitMs);
    await this.nextFrame();
    const { width, height } = this.size;
    this.canvas.width = width;
    this.canvas.height = height;
    const ctx = this.canvas.getContext("2d")!;
    ctx.drawImage(this.video, 0, 0, width, height);
    return new Promise((resolve, reject) =>
      this.canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("encode failed"))), "image/jpeg", quality));
  }

  /** Mean luminance of a downscaled frame (for the latency probe). */
  brightness(): number {
    const ctx = this.small.getContext("2d", { willReadFrequently: true })!;
    ctx.drawImage(this.video, 0, 0, this.small.width, this.small.height);
    const d = ctx.getImageData(0, 0, this.small.width, this.small.height).data;
    let s = 0;
    for (let i = 0; i < d.length; i += 4) s += 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2];
    return s / (d.length / 4);
  }
}

/**
 * Phone motion: world "up" in camera coordinates (OpenCV convention: x right,
 * y down, z out of the back camera) from accelerationIncludingGravity, and
 * rotation-rate spikes ("phone bumped").
 */
export class MotionSensor {
  up: [number, number, number] | null = null;
  onSpike: (magnitude: number) => void = () => {};
  private lastSpike = 0;
  private handler = (e: DeviceMotionEvent) => this.onMotion(e);

  start(): void {
    window.addEventListener("devicemotion", this.handler);
  }

  stop(): void {
    window.removeEventListener("devicemotion", this.handler);
  }

  private onMotion(e: DeviceMotionEvent): void {
    const g = e.accelerationIncludingGravity;
    if (g && g.x !== null && g.y !== null && g.z !== null) {
      // Device axes -> screen axes (the video frame follows the screen orientation).
      const a = ((screen.orientation?.angle ?? 0) * Math.PI) / 180;
      const sx = g.x * Math.cos(a) - g.y * Math.sin(a);
      const sy = g.x * Math.sin(a) + g.y * Math.cos(a);
      const v: [number, number, number] = [sx, -sy, -g.z];
      const n = Math.hypot(...v) || 1;
      const u = v.map((c) => c / n) as [number, number, number];
      this.up = this.up ? (this.up.map((c, i) => 0.9 * c + 0.1 * u[i]) as [number, number, number]) : u;
    }
    const r = e.rotationRate;
    if (r) {
      const mag = Math.hypot(r.alpha ?? 0, r.beta ?? 0, r.gamma ?? 0);
      const now = performance.now();
      if (mag > 10 && now - this.lastSpike > 1000) {
        this.lastSpike = now;
        this.onSpike(mag);
      }
    }
  }
}

/** Keep the screen on while capturing. */
export class WakeLock {
  private lock: any = null;
  private onVis = () => {
    if (document.visibilityState === "visible" && this.wanted) this.acquire();
  };
  private wanted = false;

  async acquire(): Promise<boolean> {
    this.wanted = true;
    document.addEventListener("visibilitychange", this.onVis);
    try {
      this.lock = await (navigator as any).wakeLock?.request("screen");
      return !!this.lock;
    } catch {
      return false;
    }
  }

  release(): void {
    this.wanted = false;
    document.removeEventListener("visibilitychange", this.onVis);
    this.lock?.release?.();
    this.lock = null;
  }
}
