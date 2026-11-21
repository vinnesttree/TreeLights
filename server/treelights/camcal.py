"""Optional one-time camera calibration with a ChArUco board (architecture §9.2).

The board is shown full-screen on any monitor; the phone captures ~15 views of
it from different angles. The result (focal length, k1, k2) is stored per
camera device and resolution and used as a strong prior by later solves.
Board square size doesn't matter: intrinsics don't depend on the board's scale.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import cv2
import numpy as np

from . import storage

SQUARES = (11, 8)
MIN_CORNERS = 12
MIN_FRAMES = 8


def board() -> "cv2.aruco.CharucoBoard":
    d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_100)
    return cv2.aruco.CharucoBoard(SQUARES, 1.0, 0.72, d)


def board_png(width: int, height: int) -> bytes:
    width, height = int(np.clip(width, 320, 7680)), int(np.clip(height, 240, 4320))
    margin = int(0.04 * min(width, height))
    img = board().generateImage((width, height), marginSize=margin, borderBits=1)
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def detect(gray: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
    """(object points (N,3), image points (N,2)) or None if too few corners."""
    det = cv2.aruco.CharucoDetector(board())
    corners, ids, _mc, _mi = det.detectBoard(gray)
    if ids is None or len(ids) < MIN_CORNERS:
        return None
    obj, img = board().matchImagePoints(corners, ids)
    if obj is None or len(obj) < MIN_CORNERS:
        return None
    return obj.reshape(-1, 3).astype(np.float32), img.reshape(-1, 2).astype(np.float32)


def calibrate(views: list[tuple[np.ndarray, np.ndarray]], width: int, height: int) -> dict:
    """Same model as the reconstruction: one focal length, principal point at the
    image center, radial k1/k2, no tangential distortion."""
    if len(views) < MIN_FRAMES:
        raise ValueError(f"Need at least {MIN_FRAMES} good board views, have {len(views)}.")
    f0 = 0.5 * max(width, height) / np.tan(np.radians(70) / 2)
    K = np.array([[f0, 0, (width - 1) / 2], [0, f0, (height - 1) / 2], [0, 0, 1]], np.float64)
    flags = (cv2.CALIB_USE_INTRINSIC_GUESS | cv2.CALIB_FIX_PRINCIPAL_POINT | cv2.CALIB_FIX_ASPECT_RATIO
             | cv2.CALIB_ZERO_TANGENT_DIST | cv2.CALIB_FIX_K3)
    rms, K, dist, _r, _t = cv2.calibrateCamera([v[0] for v in views], [v[1] for v in views], (width, height), K,
                                               np.zeros(5), flags=flags)
    return {"f": float(K[0, 0]), "k1": float(dist.ravel()[0]), "k2": float(dist.ravel()[1]), "rms_px": float(rms),
            "views": len(views), "width": width, "height": height}


class ProfileStore:
    def __init__(self, data_dir: Path):
        self.root = Path(data_dir) / "camcal"
        self.path = Path(data_dir) / "camera_profiles.json"

    @staticmethod
    def key(device: str | None, width: int, height: int) -> str:
        dev = hashlib.sha1((device or "default").encode()).hexdigest()[:12]
        return f"{dev}_{max(width, height)}x{min(width, height)}"

    def profiles(self) -> dict:
        return storage.read_json(self.path, default={}) or {}

    def frames_dir(self, key: str) -> Path:
        p = self.root / key
        p.mkdir(parents=True, exist_ok=True)
        return p

    def add_frame(self, device: str | None, width: int, height: int, jpeg: bytes) -> dict:
        arr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_GRAYSCALE)
        if arr is None:
            raise ValueError("undecodable image")
        found = detect(arr)
        key = self.key(device, width, height)
        d = self.frames_dir(key)
        count = len(list(d.glob("*.npz")))
        if found:
            np.savez(d / f"{count:03d}.npz", obj=found[0], img=found[1], size=np.array(arr.shape[::-1]))
            count += 1
        return {"key": key, "corners": 0 if not found else len(found[0]), "frames": count}

    def solve(self, device: str | None, width: int, height: int) -> dict:
        key = self.key(device, width, height)
        views, size = [], None
        for p in sorted(self.frames_dir(key).glob("*.npz")):
            z = np.load(p)
            views.append((z["obj"], z["img"]))
            size = tuple(int(v) for v in z["size"])
        if size is None:
            raise ValueError("No board views captured yet.")
        prof = calibrate(views, size[0], size[1])
        prof["device"] = device
        allp = self.profiles()
        allp[key] = prof
        storage.atomic_write_json(self.path, allp)
        return prof

    def reset(self, device: str | None, width: int, height: int) -> None:
        storage.remove_tree(self.frames_dir(self.key(device, width, height)))

    def prior_for(self, segments: dict) -> float | None:
        """Focal prior (relative to the longer image side) for a scan's segments, if a
        profile matches the camera most of them used."""
        allp = self.profiles()
        votes: dict[str, int] = {}
        for m in segments.values():
            if m.get("device") and m.get("width"):
                k = self.key(m["device"], int(m["width"]), int(m["height"]))
                votes[k] = votes.get(k, 0) + 1
        if not votes:
            return None
        k = max(votes, key=votes.get)
        p = allp.get(k)
        return None if not p else p["f"] / max(p["width"], p["height"])
