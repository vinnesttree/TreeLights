"""Coverage guidance after each view (architecture §7.8).

Viewing directions are the camera azimuths around the tree axis in the map
frame (y up). Positive rotation about +y is counter-clockwise seen from above.
When the tree turns counter-clockwise by a, the camera's azimuth relative to
the tree decreases by a.
"""

from __future__ import annotations

import numpy as np


def _azimuth(C: np.ndarray) -> float:
    return float(np.degrees(np.arctan2(-C[2], C[0])))


def camera_azimuths(m: dict) -> dict[str, float]:
    out = {}
    for c in m.get("cameras", []):
        R, t = np.array(c["R"]), np.array(c["t"])
        out[c["segment"]] = _azimuth(-R.T @ t)
    return out


def guidance(m: dict, current_segment: str | None = None) -> dict:
    az = camera_azimuths(m)
    leds = m.get("leds", [])
    n = len(leds)
    measured = sum(1 for l in leds if l["status"] == "measured")
    weak = [l for l in leds if l["status"] in ("estimated", "unknown", "single_view") and l["p"]]
    angles = sorted(az.values())
    if not angles:
        return {"measured": measured, "total": n, "azimuths": az, "turn_deg": 45.0, "message": "Turn the tree about 45°."}
    # Largest gap in viewing directions.
    ext = angles + [angles[0] + 360]
    gaps = [(ext[k + 1] - ext[k], (ext[k] + ext[k + 1]) / 2) for k in range(len(angles))]
    width, center = max(gaps)
    # Pull the target towards where weak LEDs are, if there are many.
    if len(weak) > 0.05 * n:
        P = np.array([l["p"] for l in weak])
        weak_az = np.degrees(np.arctan2(-P[:, 2], P[:, 0]))
        mean_dir = np.degrees(np.arctan2(np.sin(np.radians(weak_az)).mean(), np.cos(np.radians(weak_az)).mean()))
        d = (mean_dir - center + 180) % 360 - 180
        if abs(d) < width / 2:
            center += 0.5 * d
    cur = az.get(current_segment) if current_segment else None
    if cur is None and az:
        cur = az[sorted(az)[-1]]
    turn = (cur - center + 180) % 360 - 180  # positive: turn the tree counter-clockwise (seen from above)
    done = width <= 60 and measured >= 0.9 * n
    if done:
        msg = f"Coverage looks good: {measured} of {n} LEDs measured. You can solve now, or add views for weak areas."
    else:
        way = "counter-clockwise" if turn > 0 else "clockwise"
        msg = f"Turn the tree about {abs(round(turn / 5) * 5)}° {way} (seen from above). Largest unviewed gap: {round(width)}°."
    return {"measured": measured, "total": n, "azimuths": az, "largest_gap_deg": width, "target_azimuth": center,
            "turn_deg": turn, "done": done, "message": msg}


def predicted_visible(m: dict, target_azimuth: float) -> set[int]:
    """LEDs on the side of the tree a camera at target_azimuth would face."""
    a = np.radians(target_azimuth)
    d = np.array([np.cos(a), -np.sin(a)])  # (x, z) direction of the camera from the axis
    out = set()
    for l in m.get("leds", []):
        if l["p"]:
            v = np.array([l["p"][0], l["p"][2]])
            if v @ d > 0:
                out.add(l["i"])
    return out
