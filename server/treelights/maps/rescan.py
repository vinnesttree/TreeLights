"""Re-scan and maintenance (architecture §11.5).

Choosing what to capture again after a map has been built:
- the LEDs to re-scan: picked by you, or "all low-confidence" (not measured from two
  or more views, or measured with an uncertainty well above the map's typical one);
- anchors: well-measured LEDs spread around and up the tree, captured in the same
  short session so the new camera can be registered to the existing ones.

The new capture is added to the same scan and solved with everything else, which
gives a new map version (the active one is unchanged until you activate it).
"""

from __future__ import annotations

import re

import numpy as np

HIGH_SIGMA_FACTOR = 2.5  # measured LEDs this far above the median uncertainty count as low confidence
ANCHOR_SECTORS = 12
ANCHOR_BANDS = 5
MAX_ANCHORS = 60


def low_confidence(m: dict) -> list[int]:
    """LEDs worth capturing again: anything not measured from 2+ views, and measured
    LEDs whose uncertainty is far above the map's typical one. Manual positions are
    your own edits and are left alone."""
    leds = m["leds"]
    sig = np.array([l["sigma"] for l in leds if l["status"] == "measured" and l.get("sigma") is not None], float)
    med = float(np.median(sig)) if len(sig) else None
    out = []
    for i, l in enumerate(leds):
        st = l["status"]
        if st == "manual":
            continue
        if st != "measured":
            out.append(i)
        elif med is not None and l.get("sigma") is not None and l["sigma"] > HIGH_SIGMA_FACTOR * max(med, 1e-6):
            out.append(i)
    return out


def anchors(m: dict, exclude: set[int] | None = None, max_n: int = MAX_ANCHORS) -> list[int]:
    """Well-measured LEDs spread around and up the tree: in each azimuth sector and
    height band, the one with the smallest uncertainty. Whichever side the camera is
    on, a good share of them face it."""
    exclude = exclude or set()
    cand = []
    for i, l in enumerate(m["leds"]):
        if i in exclude or l["status"] != "measured" or l.get("p") is None:
            continue
        x, y, z = l["p"]
        cand.append((i, float(np.arctan2(x, z)), float(y), float(l.get("sigma") or 0.0)))
    if not cand:
        return []
    ys = np.array([c[2] for c in cand])
    lo, hi = float(ys.min()), float(ys.max())
    best: dict[tuple[int, int], tuple[float, int]] = {}
    for i, az, y, s in cand:
        sec = int(((az + np.pi) / (2 * np.pi)) * ANCHOR_SECTORS) % ANCHOR_SECTORS
        band = min(ANCHOR_BANDS - 1, int((y - lo) / max(hi - lo, 1e-6) * ANCHOR_BANDS))
        key = (sec, band)
        if key not in best or s < best[key][0]:
            best[key] = (s, i)
    chosen = sorted(i for _s, i in best.values())
    if len(chosen) > max_n:
        idx = np.linspace(0, len(chosen) - 1, max_n).round().astype(int)
        chosen = [chosen[k] for k in idx]
    return chosen


def parse_selection(text: str, n: int) -> list[int]:
    """'12, 40-45, 300' -> [12, 40, 41, ..., 45, 300] (out-of-range entries dropped)."""
    out: set[int] = set()
    for part in re.split(r"[,\s]+", text.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", part)
        if m:
            a, b = sorted((int(m.group(1)), int(m.group(2))))
            out.update(range(a, b + 1))
        elif part.isdigit():
            out.add(int(part))
        else:
            raise ValueError(f"not an LED number or range: {part!r}")
    return sorted(i for i in out if 0 <= i < n)
