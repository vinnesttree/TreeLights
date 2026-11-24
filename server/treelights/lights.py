"""The tree as one light that can be switched on and off (used by the timers and Home Assistant).

- **On** runs an effect from the library: the one asked for, else the one used last (with its
  sliders, palette, colors and brightness), else Twinkle in Classic Bulbs colors.
- **Off** stops the effect and turns WLED itself off. Releasing the LEDs alone would hand
  them back to WLED's own effect, which isn't off.
- While a scan or calibration holds the LEDs, switching is refused (`LightsBusy`): the capture
  isn't interrupted, and a timer that fires meanwhile is skipped.
- The state is kept in <data>/lights.json, so lights that were on are on again after a restart.
- The effect's output lease is renewed while it runs, so it runs until switched off.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from . import storage
from .effects import LIBRARY, PALETTE_IDS, Params

if TYPE_CHECKING:
    from .app import AppState

log = logging.getLogger(__name__)

DEFAULT_EFFECT = "twinkle"
DEFAULT_PARAMS = {"palette": "bulbs"}
DEFAULT_BRIGHTNESS = 0.5
LEASE_TTL_S = 3600.0  # renewed every SUPERVISE_S while the effect runs
SUPERVISE_S = 30.0


class LightsBusy(RuntimeError):
    """The LEDs are in use by a scan or calibration."""


def _detail(e: Exception) -> str:
    return str(getattr(e, "detail", None) or e)


class Lights:
    FILE = "lights.json"

    def __init__(self, app: "AppState"):
        self.app = app
        self.path = Path(app.data_dir) / self.FILE
        saved = storage.read_json(self.path, default=None) or {}
        self.on = bool(saved.get("on"))
        self.effect = saved.get("effect") if saved.get("effect") in LIBRARY else DEFAULT_EFFECT
        self.params: dict = saved.get("params") if isinstance(saved.get("params"), dict) else dict(DEFAULT_PARAMS)
        self.brightness = float(saved.get("brightness", DEFAULT_BRIGHTNESS))
        self.changed = float(saved.get("changed", 0.0))  # time.time() of the last on/off change
        self.source: str | None = saved.get("source")
        self.error: str | None = None  # last problem switching WLED itself
        self._lock = threading.RLock()
        self._listeners: list[Callable[[], None]] = []
        self._task: asyncio.Task | None = None

    # ---- state ---------------------------------------------------------------------------
    def subscribe(self, fn: Callable[[], None]) -> Callable[[], None]:
        """`fn` is called (from any thread) after every change."""
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None

    def _notify(self) -> None:
        for fn in list(self._listeners):
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("lights listener failed")

    def _save(self) -> None:
        storage.atomic_write_json(self.path, {"on": self.on, "effect": self.effect, "params": self.params,
                                              "brightness": self.brightness, "changed": self.changed,
                                              "source": self.source})

    def sync(self, source: str | None = None) -> None:
        """Take over what's running now: called after any effect is started, changed or stopped."""
        with self._lock:
            cur = self.app.effect_state()
            on = cur is not None
            if cur:
                self.effect, self.params, self.brightness = cur["effect"], cur["params"], cur["brightness"]
            if on != self.on:
                self.on, self.changed = on, time.time()
                if source:
                    self.source = source
            elif source and on:
                self.source = source
            self._save()
        self._notify()

    def busy(self) -> str | None:
        """What holds the LEDs if it isn't the effect (a scan, a calibration...), else None."""
        holder = self.app.output.holder()
        return holder.kind if holder and holder.owner != "test" else None

    def status(self) -> dict:
        cur = self.app.effect_state()
        with self._lock:
            effect = cur["effect"] if cur else self.effect
            return {
                "on": cur is not None,
                "effect": effect,
                "label": LIBRARY[effect].label or effect if effect in LIBRARY else effect,
                "params": dict(cur["params"] if cur else Params().merged(LIBRARY[effect].defaults).merged(self.params).to_dict()),
                "brightness": cur["brightness"] if cur else self.brightness,
                "changed": self.changed or None,
                "source": self.source,
                "busy": self.busy(),
                "error": self.error,
            }

    # ---- switching ---------------------------------------------------------------------------
    def _check_free(self) -> None:
        kind = self.busy()
        if kind:
            raise LightsBusy(f"The LEDs are in use ({kind}): a scan or calibration is running.")

    async def turn_on(self, effect: str | None = None, params: dict | None = None, brightness: float | None = None,
                      source: str = "app") -> dict:
        """Switch on, or change what runs while on. `params` are merged into the effect's
        current (or remembered) parameters. Raises ValueError for an unknown effect or palette,
        LightsBusy while a capture runs."""
        if effect is not None and effect not in LIBRARY:
            raise ValueError(f"unknown effect {effect!r}")
        if params and params.get("palette") is not None and params["palette"] not in PALETTE_IDS:
            raise ValueError(f"unknown palette {params['palette']!r}")
        self._check_free()
        cur = self.app.effect_state()
        if cur and (effect is None or effect == cur["effect"]):
            self.app.update_effect(params, brightness)
        else:
            name = effect or self.effect
            if LIBRARY[name].needs_map and not self.app.has_map():
                if effect is not None:
                    raise ValueError(f"{LIBRARY[name].label or name} needs a map of the tree. Map the tree first.")
                name = DEFAULT_EFFECT  # the remembered effect needs a map that's gone
            remembered = cur["params"] if cur and name == cur["effect"] else self.params if name == self.effect else None
            if remembered is not None:
                base = dict(remembered)
            else:  # another effect: its own slider defaults, keeping the colors and palette (as in WLED)
                prev = cur["params"] if cur else self.params
                keep = {k: prev[k] for k in ("colors", "palette") if prev.get(k) is not None}
                base = {**Params().merged(LIBRARY[name].defaults).to_dict(), **keep}
            b = self.brightness if brightness is None else brightness
            self.app.start_effect(name, float(min(max(b, 0.0), 1.0)), params={**base, **(params or {})})
        if cur is None:
            await self._wled_power(True)
        self.sync(source)
        return self.status()

    async def turn_off(self, source: str = "app") -> dict:
        self._check_free()
        self.app.stop_effect()
        await self._wled_power(False)
        self.sync(source)
        return self.status()

    async def set_params(self, params: dict | None = None, brightness: float | None = None, source: str = "app") -> dict:
        """Change the effect's parameters or brightness; while off, they're kept for the next "on"."""
        if params and params.get("palette") is not None and params["palette"] not in PALETTE_IDS:
            raise ValueError(f"unknown palette {params['palette']!r}")
        if self.app.effect_state() is not None:
            self.app.update_effect(params, brightness)
            self.sync(source)
            return self.status()
        with self._lock:
            merged = Params().merged(LIBRARY[self.effect].defaults).merged(self.params).merged(params)
            self.params = merged.to_dict()
            if brightness is not None:
                self.brightness = float(min(max(brightness, 0.0), 1.0))
            self._save()
        self._notify()
        return self.status()

    async def _wled_power(self, on: bool) -> None:
        """Turn WLED itself on or off. Not being able to is reported, not raised: the
        effect runs (or has stopped) either way."""
        link = self.app.link
        if not link.host:
            return
        try:
            if on:
                # WLED shows realtime data even while off, but its own "on" keeps the UI and
                # other integrations consistent with what the tree shows.
                st = await link.client.state()
                if st.get("on") is False:
                    await link.client.set_state({"on": True})
            else:
                await link.client.set_state({"on": False})
            self.error = None
        except Exception as e:  # noqa: BLE001
            self.error = f"Couldn't turn WLED {'on' if on else 'off'} at {link.host}: {e}"
            log.warning("%s", self.error)

    # ---- lifecycle ---------------------------------------------------------------------------
    async def restore(self) -> None:
        """At start-up: lights that were on when the server stopped are switched on again."""
        if not self.on:
            return
        try:
            await self.turn_on(source=self.source or "restart")
            log.info("lights on again after restart (%s)", self.effect)
        except Exception as e:  # noqa: BLE001
            log.warning("couldn't switch the lights on again after restart: %s", _detail(e))

    def start(self) -> None:
        if not self._task:
            self._task = asyncio.ensure_future(self._supervise())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    def supervise_once(self) -> None:
        """Keep the effect's lease, and notice when something else took over the LEDs."""
        running = self.app.effect_state() is not None
        if running:
            self.app.output.renew("test", ttl=LEASE_TTL_S)
        if running != self.on:  # e.g. a scan took over the LEDs
            self.sync("scan" if self.busy() else None)

    async def _supervise(self) -> None:
        while True:
            await asyncio.sleep(SUPERVISE_S)
            try:
                self.supervise_once()
            except Exception:  # noqa: BLE001
                log.exception("lights supervisor failed")
