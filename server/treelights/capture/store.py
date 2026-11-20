"""Scan storage and observation aggregation (architecture §13).

scans/<scan>/
  scan.json          settings, views, segments (atomic writes)
  events.jsonl       append-only session log
  detections.jsonl   append-only per-frame results
  overrides.json     user edits
  refs/<seg>.npy     dark reference images
  frames/<seg>/...   raw frames
"""

from __future__ import annotations

import datetime as dt
import threading
import uuid
from pathlib import Path

import numpy as np

from .. import storage


def new_id(prefix: str = "") -> str:
    stamp = dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    return f"{prefix}{stamp}-{uuid.uuid4().hex[:4]}"


class Scan:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.id = self.root.name
        self._lock = threading.RLock()
        self.meta = storage.read_json(self.root / "scan.json", default=None)
        if self.meta is None:
            raise FileNotFoundError(f"no scan at {self.root}")
        self.events = storage.JsonlLog(self.root / "events.jsonl")
        self.detections = storage.JsonlLog(self.root / "detections.jsonl", fsync=False)

    @staticmethod
    def create(scans_dir: Path, led_count: int, extra: dict | None = None) -> "Scan":
        sid = new_id()
        root = Path(scans_dir) / sid
        root.mkdir(parents=True)
        seq = 1 + max([int((storage.read_json(p / "scan.json", {}) or {}).get("seq", 0)) for p in Path(scans_dir).iterdir() if p.is_dir()] + [0])
        meta = {"id": sid, "seq": seq, "created": dt.datetime.now().isoformat(timespec="seconds"), "led_count": led_count,
                "views": [], "segments": {}, **(extra or {})}
        storage.atomic_write_json(root / "scan.json", meta)
        return Scan(root)

    def save(self) -> None:
        with self._lock:
            storage.atomic_write_json(self.root / "scan.json", self.meta)

    # ---- views and segments ---------------------------------------------
    def add_view(self, info: dict | None = None) -> str:
        with self._lock:
            vid = f"v{len(self.meta['views']) + 1}"
            self.meta["views"].append({"id": vid, "segments": [], "status": "capturing", **(info or {})})
            self.save()
        self.events.append({"type": "view_started", "view": vid})
        return vid

    def view(self, vid: str) -> dict:
        return next(v for v in self.meta["views"] if v["id"] == vid)

    def set_view(self, vid: str, **fields) -> None:
        with self._lock:
            self.view(vid).update(fields)
            self.save()

    def add_segment(self, vid: str, camera: dict) -> str:
        with self._lock:
            v = self.view(vid)
            sid = f"{vid}{chr(ord('a') + len(v['segments']))}"
            v["segments"].append(sid)
            self.meta["segments"][sid] = {"view": vid, **camera}
            self.save()
        self.events.append({"type": "segment_started", "view": vid, "segment": sid})
        return sid

    def set_segment(self, sid: str, **fields) -> None:
        with self._lock:
            self.meta["segments"][sid].update(fields)
            self.save()

    # ---- files ------------------------------------------------------------
    def frame_path(self, seg: str, step: int, kind: str) -> Path:
        p = self.root / "frames" / seg / f"{step:06d}_{kind}.jpg"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def ref_path(self, seg: str, version: int = 0) -> Path:
        p = self.root / "refs" / f"{seg}_{version}.npz"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def write_frame(self, seg: str, step: int, kind: str, data: bytes) -> Path:
        p = self.frame_path(seg, step, kind)
        p.write_bytes(data)  # frames are not critical state; detections reference them
        return p

    # ---- observations ------------------------------------------------------
    def record(self, rec: dict) -> None:
        self.detections.append(rec)

    def invalidate(self, seg: str, after_step: int, reason: str) -> None:
        """Mark detections of a segment captured after a step as invalid (camera moved)."""
        self.events.append({"type": "invalidate", "segment": seg, "after_step": after_step, "reason": reason})

    def valid_records(self, segments: set[str] | None = None, kinds: tuple[str, ...] | None = None) -> list[dict]:
        """Detection records that are still valid: not invalidated by a later
        movement, not from a discarded binary pass, not unverified."""
        cut: dict[str, int] = {}
        drop_binary: set[str] = set()
        for ev in self.events.read():
            if ev.get("type") == "invalidate":
                s = ev["segment"]
                cut[s] = min(cut.get(s, 1 << 62), int(ev["after_step"]))
            elif ev.get("type") == "binary_discarded":
                drop_binary.add(ev["segment"])
        out = []
        for rec in self.detections.read():
            seg = rec.get("segment")
            if seg is None or rec.get("led") is None or rec.get("unverified"):
                continue
            if segments is not None and seg not in segments:
                continue
            if kinds is not None and rec.get("kind") not in kinds:
                continue
            if rec.get("step", 0) > cut.get(seg, 1 << 62):
                continue
            if rec.get("kind") == "binary" and seg in drop_binary:
                continue
            out.append(rec)
        return out

    def observations(self) -> dict[str, dict[int, list[dict]]]:
        """{segment: {led: [candidate, ...]}} from valid detections.

        - Spot-check captures only verify the binary pass and aren't used.
        - A clean one-LED frame ("seq") takes precedence over binary-pass blobs for the
          same LED; with many LEDs lit, a binary blob may be light scattered by the
          branches rather than the LED itself. Binary blobs are used when no one-LED
          frame found anything.
        - When an LED was captured several times in a segment, only candidates
          that repeat within 2 px are kept (architecture §7.3 step 8).
        - An LED captured in a valid frame with no candidates is recorded as
          seen-nothing (empty list): negative information for gap filling.
        """
        attempts: dict[str, dict[int, dict[str, list[list[dict]]]]] = {}
        for rec in self.valid_records(kinds=("seq", "binary")):
            per = attempts.setdefault(rec["segment"], {}).setdefault(int(rec["led"]), {"seq": [], "binary": []})
            per[rec["kind"]].append(rec.get("candidates", []))
        out: dict[str, dict[int, list[dict]]] = {}
        for seg, leds in attempts.items():
            o = out.setdefault(seg, {})
            for led, per in leds.items():
                seq_found = [t for t in per["seq"] if t]
                if seq_found:
                    o[led] = _consistent(seq_found)
                else:
                    binary_found = [t for t in per["binary"] if t]
                    o[led] = _consistent(binary_found) if binary_found else []
        return out

    def overrides(self) -> dict:
        return storage.read_json(self.root / "overrides.json", default={}) or {}

    def set_overrides(self, ov: dict) -> None:
        storage.atomic_write_json(self.root / "overrides.json", ov)


class ScanStore:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir) / "scans"
        self.dir.mkdir(parents=True, exist_ok=True)

    def create(self, led_count: int, extra: dict | None = None) -> Scan:
        return Scan.create(self.dir, led_count, extra)

    def get(self, sid: str) -> Scan:
        if "/" in sid or sid.startswith("."):
            raise FileNotFoundError(sid)
        return Scan(self.dir / sid)

    def list(self) -> list[dict]:
        out = []
        for p in sorted(self.dir.iterdir(), reverse=True):
            meta = storage.read_json(p / "scan.json", default=None)
            if meta:
                out.append({"id": meta["id"], "created": meta.get("created"), "views": len(meta.get("views", []))})
        return out

    def prune_frames(self, keep_ids: set[str], keep_recent: int) -> list[str]:
        """Delete raw frames of old scans (detections are kept). Returns pruned ids."""
        def order(p: Path) -> tuple:
            meta = storage.read_json(p / "scan.json", {}) or {}
            return (int(meta.get("seq", 0)), p.name)

        scans = sorted((p for p in self.dir.iterdir() if p.is_dir()), key=order, reverse=True)
        pruned = []
        for k, p in enumerate(scans):
            if k < keep_recent or p.name in keep_ids:
                continue
            if (p / "frames").exists():
                storage.remove_tree(p / "frames")
                pruned.append(p.name)
        return pruned


def _consistent(tries: list[list[dict]]) -> list[dict]:
    """One attempt: its candidates. Several: only candidates that repeat within 2 px."""
    if len(tries) == 1:
        return tries[0]
    kept: list[dict] = []
    for k, t in enumerate(tries):
        for c in t:
            others = [u for j, u in enumerate(tries) if j != k]
            if any(np.hypot(c["x"] - d["x"], c["y"] - d["y"]) <= 2.0 for u in others for d in u):
                if not any(np.hypot(c["x"] - d["x"], c["y"] - d["y"]) <= 1.0 for d in kept):
                    kept.append(c)
    return sorted(kept, key=lambda c: -c.get("score", 0))[:3]
