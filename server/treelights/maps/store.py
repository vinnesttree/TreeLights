"""Map versions and activation (architecture §11)."""

from __future__ import annotations

import csv
import io
from pathlib import Path

from .. import storage


class ActivationBlocked(Exception):
    pass


class MapStore:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir) / "maps"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.active_path = self.dir / "active.json"

    def save(self, m: dict) -> str:
        mid = m["id"]
        k = 1
        while (self.dir / mid).exists():  # never overwrite a version
            mid = f"{m['id']}-{k}"
            k += 1
        m = dict(m, id=mid)
        storage.atomic_write_json(self.dir / mid / "map.json", m)
        return mid

    def get(self, mid: str) -> dict:
        if "/" in mid or mid.startswith("."):
            raise FileNotFoundError(mid)
        m = storage.read_json(self.dir / mid / "map.json")
        if m is None:
            raise FileNotFoundError(mid)
        return m

    def list(self) -> list[dict]:
        active = self.active_id()
        out = []
        for p in sorted(self.dir.iterdir(), reverse=True):
            m = storage.read_json(p / "map.json") if p.is_dir() else None
            if m:
                q = m.get("quality", {})
                out.append({"id": m["id"], "created": m.get("created"), "scan": m.get("scan"), "active": m["id"] == active,
                            "measured": q.get("measured"), "warnings": len(q.get("warnings", [])),
                            "blocked": q.get("activation_blocked")})
        return out

    def active_id(self) -> str | None:
        ptr = storage.read_json(self.active_path)
        return ptr.get("id") if ptr else None

    def active(self) -> dict | None:
        mid = self.active_id()
        if not mid:
            return None
        try:
            return self.get(mid)
        except FileNotFoundError:
            return None

    def activate(self, mid: str, current_fingerprint: str | None = None, force: bool = False) -> dict:
        m = self.get(mid)
        blocked = m.get("quality", {}).get("activation_blocked")
        if blocked:
            raise ActivationBlocked(blocked)
        fp = (m.get("wled") or {}).get("config_hash")
        warning = None
        if fp and current_fingerprint and fp != current_fingerprint:
            warning = "WLED's LED configuration changed since this map was made."
            if not force:
                raise ActivationBlocked(warning + " Activate anyway to confirm.")
        storage.atomic_write_json(self.active_path, {"id": mid, "previous": self.active_id()})
        return {"id": mid, "warning": warning}

    def rollback(self) -> str | None:
        ptr = storage.read_json(self.active_path) or {}
        prev = ptr.get("previous")
        if prev:
            storage.atomic_write_json(self.active_path, {"id": prev, "previous": ptr.get("id")})
        return prev


def export_csv(m: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["i", "x", "y", "z", "status", "sigma"])
    for l in m["leds"]:
        p = l["p"] or [None, None, None]
        w.writerow([l["i"], *p, l["status"], l["sigma"]])
    return buf.getvalue()
