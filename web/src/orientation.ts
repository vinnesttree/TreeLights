// Phone orientation for the walk-around scan (architecture §7.9).
//
// Preferred: RelativeOrientationSensor (gyroscope + accelerometer: z up from
// gravity, arbitrary heading; no magnetometer, which LED strings and their power
// supplies disturb). Fallback: the deviceorientation event, converted to a
// quaternion. The server works out how the device axes and the quaternion's
// convention map onto the camera (it tries every mapping against the video), so
// nothing here depends on the platform's conventions being what the spec says.
//
// Times are phone-clock seconds: (performance.timeOrigin + timestamp) / 1000, the
// same clock as the frame times and the recording start.

export type Quat = [number, number, number, number]; // x, y, z, w

export function phoneNow(): number {
  return (performance.timeOrigin + performance.now()) / 1000;
}

export function phoneTime(domTs: number): number {
  return (performance.timeOrigin + domTs) / 1000;
}

/** deviceorientation (alpha, beta, gamma in degrees; intrinsic Z-X'-Y'') -> quaternion device -> world. */
export function eulerToQuat(alpha: number, beta: number, gamma: number): Quat {
  const d = Math.PI / 180;
  const [_x, _y, _z] = [beta * d, gamma * d, alpha * d];
  const cX = Math.cos(_x / 2), cY = Math.cos(_y / 2), cZ = Math.cos(_z / 2);
  const sX = Math.sin(_x / 2), sY = Math.sin(_y / 2), sZ = Math.sin(_z / 2);
  // ZXY order.
  return [
    sX * cY * cZ - cX * sY * sZ,
    cX * sY * cZ + sX * cY * sZ,
    cX * cY * sZ + sX * sY * cZ,
    cX * cY * cZ - sX * sY * sZ,
  ];
}

/** Rotate vector v by unit quaternion q. */
export function rotate(q: Quat, v: [number, number, number]): [number, number, number] {
  const [x, y, z, w] = q;
  const [vx, vy, vz] = v;
  const tx = 2 * (y * vz - z * vy), ty = 2 * (z * vx - x * vz), tz = 2 * (x * vy - y * vx);
  return [vx + w * tx + (y * tz - z * ty), vy + w * ty + (z * tx - x * tz), vz + w * tz + (x * ty - y * tx)];
}

/** Angle (degrees) between two orientations. */
export function quatAngle(a: Quat, b: Quat): number {
  const dot = Math.abs(a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3]);
  return (2 * Math.acos(Math.min(1, dot)) * 180) / Math.PI;
}

export class Orientation {
  source: "sensor" | "deviceorientation" | null = null;
  latest: Quat | null = null;
  latestT = 0;
  samples: { t: number; q: Quat }[] = [];
  /** Angular speed (deg/s), smoothed. */
  rate = 0;
  private sensor: any = null;
  private onEvent = (e: DeviceOrientationEvent) => {
    if (e.alpha === null || e.beta === null || e.gamma === null) return;
    this.push(phoneTime(e.timeStamp), eulerToQuat(e.alpha, e.beta, e.gamma));
  };

  async start(): Promise<string> {
    const w = window as any;
    if (typeof w.RelativeOrientationSensor === "function") {
      try {
        if (navigator.permissions) {
          const names = ["accelerometer", "gyroscope"];
          const res = await Promise.all(names.map((n) => navigator.permissions.query({ name: n as PermissionName }).catch(() => null)));
          if (res.some((r) => r && r.state === "denied")) throw new Error("motion sensors denied");
        }
        const s = new w.RelativeOrientationSensor({ frequency: 60, referenceFrame: "device" });
        await new Promise<void>((resolve, reject) => {
          const timer = setTimeout(() => reject(new Error("sensor did not start")), 2000);
          s.addEventListener("reading", () => {
            clearTimeout(timer);
            resolve();
          }, { once: true });
          s.addEventListener("error", (e: any) => {
            clearTimeout(timer);
            reject(e.error ?? new Error("sensor error"));
          }, { once: true });
          s.start();
        });
        s.addEventListener("reading", () => {
          const q = s.quaternion as number[];
          if (q) this.push(phoneTime(s.timestamp), [q[0], q[1], q[2], q[3]]);
        });
        this.sensor = s;
        this.source = "sensor";
        return this.source;
      } catch {
        this.sensor?.stop?.();
        this.sensor = null;
      }
    }
    // iOS-style permission prompt (harmless elsewhere).
    const DOE = (window as any).DeviceOrientationEvent;
    if (DOE?.requestPermission) {
      try {
        await DOE.requestPermission();
      } catch {
        /* the page still works without the sensor */
      }
    }
    window.addEventListener("deviceorientation", this.onEvent);
    this.source = "deviceorientation";
    return this.source;
  }

  stop(): void {
    this.sensor?.stop?.();
    this.sensor = null;
    window.removeEventListener("deviceorientation", this.onEvent);
  }

  private push(t: number, q: Quat): void {
    const n = Math.hypot(...q) || 1;
    q = q.map((c) => c / n) as Quat;
    if (this.latest && t > this.latestT) {
      const r = quatAngle(this.latest, q) / (t - this.latestT);
      if (Number.isFinite(r)) this.rate = 0.85 * this.rate + 0.15 * r;
    }
    if (t <= this.latestT) return;
    this.latest = q;
    this.latestT = t;
    this.samples.push({ t, q });
  }

  /** Samples since the last call. */
  drain(): { t: number[]; q: Quat[] } {
    const s = this.samples;
    this.samples = [];
    return { t: s.map((x) => x.t), q: s.map((x) => x.q) };
  }

  /**
   * Heading (degrees) of the back camera's viewing direction around the vertical.
   * Only used for the coverage ring on the phone; if the platform's convention is
   * the inverse, the heading turns the other way, which the ring doesn't mind.
   */
  heading(): number | null {
    if (!this.latest) return null;
    const f = rotate(this.latest, [0, 0, -1]);
    return (Math.atan2(f[1], f[0]) * 180) / Math.PI;
  }
}
