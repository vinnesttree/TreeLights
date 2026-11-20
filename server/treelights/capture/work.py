"""Functions run in worker processes (architecture §6.2).

Frames and references are passed as file paths; references are cached per
process so they aren't re-read for every frame.
"""

from __future__ import annotations

import functools
from pathlib import Path

import numpy as np

from ..detection import binary, constellation
from ..detection.detector import Reference, build_reference, decode_image, detect_single


@functools.lru_cache(maxsize=8)
def _load_ref(path: str, mtime: float) -> Reference:
    z = np.load(path)
    return Reference(z["image"].astype(np.float32), z["sigma"].astype(np.float32))


def load_ref(path: str | Path) -> Reference:
    p = Path(path)
    return _load_ref(str(p), p.stat().st_mtime)


def _img(path: str | Path) -> np.ndarray:
    return decode_image(Path(path).read_bytes())


def make_reference(frame_paths: list[str], out_path: str) -> dict:
    ref = build_reference([_img(p) for p in frame_paths])
    tmp = out_path + ".tmp.npz"
    np.savez(tmp, image=ref.image.astype(np.float16), sigma=ref.sigma)
    Path(tmp).replace(out_path)
    return {"sigma": ref.sigma.tolist(), "shape": list(ref.shape)}


def dark_changed_fraction(a_path: str, b_path: str) -> float:
    """Fraction of pixels that changed between two dark frames (scene settling)."""
    a, b = _img(a_path).astype(np.float32), _img(b_path).astype(np.float32)
    d = (a - b).mean(axis=2)
    from ..detection.detector import robust_sigma
    s = max(robust_sigma(d), 0.5)
    return float((np.abs(d) > 8 * s + 4).mean())


def detect_frame(frame_path: str, ref_path: str, expected_color: list[float] | None) -> dict:
    det = detect_single(_img(frame_path), load_ref(ref_path), None if expected_color is None else np.array(expected_color))
    return {"valid": det.valid, "reason": det.reason, "noise": det.noise,
            "candidates": [c.to_dict() for c in det.candidates]}


def constellation_frame(frame_path: str, ref_path: str) -> list[list[float]]:
    return constellation.constellation_points(_img(frame_path), load_ref(ref_path)).tolist()


def blob_colors(frame_path: str, ref_path: str, points: list[list[float]], r: int = 2) -> dict:
    """Mean dark-subtracted RGB around each point, plus the peak value."""
    img = _img(frame_path).astype(np.float32)
    ref = load_ref(ref_path)
    diff = img - ref.image
    h, w = diff.shape[:2]
    cols, peaks, sats = [], [], []
    for x, y in points:
        xi, yi = int(round(x)), int(round(y))
        win = diff[max(0, yi - r) : min(h, yi + r + 1), max(0, xi - r) : min(w, xi + r + 1)]
        raw = img[max(0, yi - r) : min(h, yi + r + 1), max(0, xi - r) : min(w, xi + r + 1)]
        cols.append(win.reshape(-1, 3).mean(axis=0).tolist())
        peaks.append(float(win.max()))
        sats.append(bool((raw >= 250).any()))
    return {"colors": cols, "peaks": peaks, "saturated": sats, "sigma": float(np.mean(ref.sigma))}


@functools.lru_cache(maxsize=2)
def _decoder(ref_path: str, ref_mtime: float, allon: tuple[str, ...], n_leds: int) -> binary.BinaryDecoder:
    dec = binary.BinaryDecoder(load_ref(ref_path), n_leds)
    dec.set_allon([_img(p) for p in allon])
    return dec


def binary_pair(ref_path: str, allon: list[str], n_leds: int, bit: int, pattern: str, complement: str) -> dict:
    dec = _decoder(ref_path, Path(ref_path).stat().st_mtime, tuple(allon), n_leds)
    chk = dec.add_pair(bit, _img(pattern), _img(complement))
    return {"ok": chk.ok, "reason": chk.reason, "shift": [float(v) for v in chk.shift], "fraction": chk.fraction_exclusive, "peaks": len(dec.peaks)}


def binary_decode(ref_path: str, allon: list[str], n_leds: int, pairs: dict[int, tuple[str, str]]) -> list[dict]:
    """Decode from scratch (deterministic, independent of the incremental cache)."""
    dec = binary.BinaryDecoder(load_ref(ref_path), n_leds)
    dec.set_allon([_img(p) for p in allon])
    for bit, (p, c) in sorted(pairs.items()):
        chk = dec.add_pair(bit, _img(p), _img(c))
        if not chk.ok:
            raise RuntimeError(f"bit {bit} pair no longer consistent: {chk.reason}")
    return dec.result()
