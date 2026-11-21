"""Vectorised 3D value noise for effects (smooth, deterministic, about 0..1)."""

from __future__ import annotations

import numpy as np

_M = 0xFFFFFFFF


def _hash(ix: np.ndarray, iy: np.ndarray, iz: np.ndarray, seed: int) -> np.ndarray:
    h = (ix * 0x8DA6B343 + iy * 0xD8163841 + iz * 0xCB1AB31F + seed * 0x9E3779B1) & _M
    h ^= h >> 13
    h = (h * 0x5BD1E995) & _M
    h ^= h >> 15
    return h.astype(np.float64) / _M


def noise3(x: np.ndarray, y: np.ndarray, z: np.ndarray, seed: int = 0) -> np.ndarray:
    fx, fy, fz = np.floor(x), np.floor(y), np.floor(z)
    ix, iy, iz = fx.astype(np.int64), fy.astype(np.int64), fz.astype(np.int64)
    tx, ty, tz = x - fx, y - fy, z - fz
    ux, uy, uz = (t * t * t * (t * (t * 6 - 15) + 10) for t in (tx, ty, tz))

    def c(dx: int, dy: int, dz: int) -> np.ndarray:
        return _hash(ix + dx, iy + dy, iz + dz, seed)

    x00 = c(0, 0, 0) * (1 - ux) + c(1, 0, 0) * ux
    x10 = c(0, 1, 0) * (1 - ux) + c(1, 1, 0) * ux
    x01 = c(0, 0, 1) * (1 - ux) + c(1, 0, 1) * ux
    x11 = c(0, 1, 1) * (1 - ux) + c(1, 1, 1) * ux
    y0 = x00 * (1 - uy) + x10 * uy
    y1 = x01 * (1 - uy) + x11 * uy
    return y0 * (1 - uz) + y1 * uz


def fbm(x: np.ndarray, y: np.ndarray, z: np.ndarray, octaves: int = 3, seed: int = 0) -> np.ndarray:
    """Fractal noise, rescaled so its spread is close to 0..1."""
    total = np.zeros_like(np.asarray(x, float))
    amp, norm, f = 1.0, 0.0, 1.0
    for o in range(octaves):
        total += amp * noise3(x * f, y * f, z * f, seed + o * 17)
        norm += amp
        amp *= 0.5
        f *= 2.03
    v = total / norm
    # Summed octaves bunch up around 0.5; stretch back out.
    return np.clip((v - 0.5) * (1.6 + 0.25 * octaves) + 0.5, 0, 1)
