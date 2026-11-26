"""Drawing from the phone: the picture painted on the Draw page, and showing it on the tree.

- **The picture** is one color per LED (`effects.drawing.Canvas`), painted with a brush in map
  coordinates or on the unwrapped surface (see `effects/drawing.py`). It's kept in
  <data>/drawing.json, so it survives a restart.
- **On the tree** it runs as the library effect `drawing`, through the lights (`lights.py`): the
  same output lease as every other effect, so it replaces the running effect, it's renewed while
  it runs, and the timers and Home Assistant see it like any other effect. Scans and calibration
  take precedence: while they hold the LEDs, painting still changes the picture, but it isn't
  shown (`LightsBusy`).
- **Brightness and the power cap** are those of every effect: the effect's brightness, then the
  output thread's current limit.
- **Saved drawings** are still scenes in <data>/drawings/<id>.json, one color per LED.
- **Undo** keeps the picture as it was before each of the last strokes, fills, clears and loads.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import numpy as np

from . import storage
from .effects import TreeGeometry
from .effects.drawing import BrushSpace, Canvas, clamp_radius, parse_color, parse_points
from .lights import LightsBusy

if TYPE_CHECKING:
    from .app import AppState

log = logging.getLogger(__name__)

EFFECT = "drawing"
UNDO_DEPTH = 30
SAVE_DELAY_S = 1.0
_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


class NoMap(ValueError):
    """Drawing needs an active map."""


class Drawing:
    FILE = "drawing.json"
    DIR = "drawings"

    def __init__(self, app: "AppState"):
        self.app = app
        self.path = Path(app.data_dir) / self.FILE
        self.dir = Path(app.data_dir) / self.DIR
        self.canvas = Canvas(app.output.led_count)
        saved = storage.read_json(self.path, default=None) or {}
        if isinstance(saved.get("colors"), list):
            try:
                self.canvas.set_hex(saved["colors"])
            except ValueError:
                log.warning("ignoring a damaged %s", self.path)
        self._undo: list[np.ndarray] = []
        self._space: tuple | None = None  # (map id, LED count, BrushSpace)
        self._listeners: list[Callable[[dict], None]] = []
        self._save_handle: asyncio.TimerHandle | None = None

    # ---- listeners (the Draw page's WebSocket) ------------------------------------------------
    def subscribe(self, fn: Callable[[dict], None]) -> Callable[[], None]:
        """`fn(msg)` is called on the event loop after every change of the picture."""
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None

    def _emit(self, msg: dict) -> None:
        for fn in list(self._listeners):
            try:
                fn(msg)
            except Exception:  # noqa: BLE001
                log.exception("drawing listener failed")

    # ---- geometry --------------------------------------------------------------------------------
    def fit(self) -> None:
        """Keep the canvas the size of the LED string."""
        self.canvas.resize(self.app.output.led_count)

    def space(self) -> BrushSpace:
        m = self.app.maps.active()
        n = self.app.output.led_count
        if not (m and len(m["leds"]) == n and any(l.get("p") for l in m["leds"])):
            raise NoMap("Drawing needs a map of the tree. Map the tree first.")
        if not self._space or self._space[:2] != (m["id"], n):
            self._space = (m["id"], n, BrushSpace(TreeGeometry.from_map(m)))
        return self._space[2]

    # ---- editing ---------------------------------------------------------------------------------
    def _push_undo(self) -> None:
        self._undo.append(self.canvas.snapshot())
        del self._undo[:-UNDO_DEPTH]

    def stroke(self, points, space: str = "3d", radius=0.05, color="#ffffff", erase: bool = False,  # noqa: ANN001
               start: bool = True) -> int:
        """Paint a stroke (a polyline; one point is a dab). `start` marks the first message of a
        stroke: undo goes back to before it. Returns how many LEDs changed."""
        pts = parse_points(points, space)
        r = clamp_radius(radius)
        rgb = np.zeros(3) if erase else parse_color(color)
        brush = self.space()
        self.fit()
        if start or not self._undo:
            self._push_undo()
        idx = self.canvas.paint(brush.mask(pts, r, space), rgb)
        if len(idx):
            self._changed({"type": "update", "leds": idx.tolist(), "colors": self.canvas.to_hex(idx),
                           "undo": len(self._undo)})
        return len(idx)

    def fill(self, color="#000000") -> None:  # noqa: ANN001
        """Every LED with a position takes the color (black clears)."""
        rgb = parse_color(color)
        brush = self.space()
        self.fit()
        self._push_undo()
        self.canvas.paint(brush.has, rgb)
        self.canvas.paint(~brush.has, np.zeros(3))
        self._changed(self.canvas_msg())

    def clear(self) -> None:
        self.fit()
        self._push_undo()
        self.canvas.restore(np.zeros((self.canvas.n, 3)))
        self._changed(self.canvas_msg())

    def undo(self) -> bool:
        if not self._undo:
            return False
        self.canvas.restore(self._undo.pop())
        self._changed(self.canvas_msg())
        return True

    def canvas_msg(self) -> dict:
        return {"type": "canvas", "colors": self.canvas.to_hex(), "undo": len(self._undo)}

    def _changed(self, msg: dict) -> None:
        self._schedule_save()
        self._emit(msg)

    # ---- persistence ----------------------------------------------------------------------------
    def _schedule_save(self) -> None:
        """Save the picture at most once a second while painting."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.flush()
            return
        if self._save_handle is None:
            self._save_handle = loop.call_later(SAVE_DELAY_S, self.flush)

    def flush(self) -> None:
        if self._save_handle is not None:
            self._save_handle.cancel()
            self._save_handle = None
        storage.atomic_write_json(self.path, {"colors": self.canvas.to_hex(), "saved": time.time()}, indent=None)

    # ---- on the tree ----------------------------------------------------------------------------
    def live(self) -> bool:
        cur = self.app.effect_state()
        return bool(cur and cur["effect"] == EFFECT)

    async def show(self, source: str = "draw") -> dict:
        """Show the picture on the tree. Raises LightsBusy while a scan or calibration holds the
        LEDs, NoMap without a map."""
        self.space()
        await self.app.lights.turn_on(EFFECT, source=source)
        return self.status()

    async def show_if_free(self, source: str = "draw") -> None:
        """After painting: show the picture unless it's shown already or a capture holds the LEDs."""
        if self.live() or self.app.lights.busy():
            return
        try:
            await self.show(source)
        except LightsBusy:
            pass

    def status(self) -> dict:
        lights = self.app.lights.status()
        return {"live": self.live(), "busy": self.app.lights.busy(), "undo": len(self._undo),
                "lights_on": lights["on"], "brightness": lights["brightness"], "map": self.app.maps.active_id()}

    def state(self) -> dict:
        self.fit()
        return {**self.status(), "colors": self.canvas.to_hex()}

    # ---- saved drawings ---------------------------------------------------------------------------
    def _file(self, did: str) -> Path:
        if not _ID.match(did or ""):
            raise KeyError(did)
        return self.dir / f"{did}.json"

    def saved(self) -> list[dict]:
        out = []
        for p in sorted(self.dir.glob("*.json"), reverse=True):
            d = storage.read_json(p, default=None)
            if isinstance(d, dict) and isinstance(d.get("colors"), list):
                out.append({"id": p.stem, "name": d.get("name") or p.stem, "saved": d.get("saved"),
                            "map": d.get("map"), "led_count": len(d["colors"])})
        return out

    def save(self, name: str | None = None) -> dict:
        name = " ".join(str(name or "").split())[:60] or time.strftime("Drawing %Y-%m-%d %H:%M")
        base = time.strftime("%Y-%m-%dT%H-%M-%S")
        did, k = base, 1
        while (self.dir / f"{did}.json").exists():
            k += 1
            did = f"{base}-{k}"
        self.fit()
        storage.atomic_write_json(self.dir / f"{did}.json", {
            "name": name, "saved": time.time(), "map": self.app.maps.active_id(),
            "colors": self.canvas.to_hex()}, indent=None)
        return {"id": did, "name": name}

    def load(self, did: str) -> None:
        """Replace the picture with a saved drawing. Raises KeyError if there's no such drawing."""
        p = self._file(did)
        d = storage.read_json(p, default=None)
        if not isinstance(d, dict) or not isinstance(d.get("colors"), list):
            raise KeyError(did)
        self.fit()
        self._push_undo()
        self.canvas.set_hex(d["colors"])
        self._changed(self.canvas_msg())

    def delete(self, did: str) -> None:
        p = self._file(did)
        if not p.exists():
            raise KeyError(did)
        p.unlink()
