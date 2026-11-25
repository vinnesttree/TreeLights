"""Temporary changes to WLED's state, always undone (architecture §5.4, §7.3).

When LEDs are too dim for the camera even at full level, calibration may raise
WLED's master brightness (which WLED applies to realtime data unless its "force
max brightness" option is on). The original state is written to disk *before*
anything is changed, and put back at the end of the capture, or at the next
start-up if the server stopped in between.

A tree with several controllers has one guard per controller, each with its own file:
`wled_restore.json` for the first, `wled_restore_<k>.json` for controller k (k >= 2).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Sequence

from .. import storage
from .link import WledLink

log = logging.getLogger(__name__)

RESTORE_FILE = "wled_restore.json"
BOOST_BRI = 255


def restore_file(k: int) -> str:
    """The file controller k (0-based) saves its state in."""
    return RESTORE_FILE if k == 0 else f"wled_restore_{k + 1}.json"


class BrightnessGuard:
    def __init__(self, data_dir: Path, link: WledLink, filename: str = RESTORE_FILE):
        self.path = Path(data_dir) / filename
        self.link = link

    @property
    def boosted(self) -> bool:
        return self.path.exists()

    def forces_max(self) -> bool:
        """WLED ignores its master brightness for realtime data when this option is on."""
        live = (self.link.cfg.live if self.link.cfg else None) or {}
        return bool(live.get("maxbri"))

    async def boost(self) -> bool:
        """Raise master brightness to full. True if that should make the LEDs brighter."""
        if self.forces_max():
            return False
        st = await self.link.client.state()
        bri = int(st.get("bri", BOOST_BRI))
        if bri >= BOOST_BRI:
            return False
        if not self.boosted:  # keep the first original if boosted twice
            storage.atomic_write_json(self.path, {"mac": self.link.mac, "host": self.link.host,
                                                  "bri": bri, "on": bool(st.get("on", True))})
        await self.link.client.set_state({"bri": BOOST_BRI})
        log.info("raised WLED master brightness %d -> %d for calibration", bri, BOOST_BRI)
        return True

    async def restore(self) -> bool:
        """Put back the saved state; True if something was restored. The file is only
        removed once WLED has accepted the change, so a failure is retried later."""
        saved = storage.read_json(self.path, default=None)
        if not saved:
            return False
        if saved.get("mac") and self.link.mac and saved["mac"].lower() != self.link.mac.lower():
            log.warning("saved WLED state belongs to controller %s, not %s; not restoring", saved["mac"], self.link.mac)
            self.path.unlink(missing_ok=True)
            return False
        await self.link.client.set_state({"bri": int(saved["bri"]), "on": bool(saved.get("on", True))})
        self.path.unlink(missing_ok=True)
        log.info("restored WLED master brightness to %d", int(saved["bri"]))
        return True


class GroupBrightness:
    """The guards of all controllers of a tree. `links` is read at each call, so it follows
    changes to the tree's controllers."""

    def __init__(self, data_dir: Path, links: Callable[[], Sequence[WledLink]]):
        self.data_dir = Path(data_dir)
        self._links = links

    def guards(self) -> list[BrightnessGuard]:
        return [BrightnessGuard(self.data_dir, link, restore_file(k)) for k, link in enumerate(self._links())]

    @property
    def boosted(self) -> bool:
        return any(g.boosted for g in self.guards())

    async def boost(self) -> bool:
        """Raise every reachable controller's master brightness. True if any was raised.
        A controller that fails doesn't stop the others; the first error is raised after."""
        raised, err = False, None
        for g in self.guards():
            if not g.link.host:
                continue
            try:
                raised |= await g.boost()
            except Exception as e:  # noqa: BLE001
                log.warning("couldn't raise the master brightness of %s: %s", g.link.host, e)
                err = err or e
        if err and not raised:
            raise err
        return raised

    async def restore(self) -> bool:
        """Restore every controller that was raised. Errors are raised after trying all."""
        restored, err = False, None
        for g in self.guards():
            if not g.boosted:
                continue
            try:
                restored |= await g.restore()
            except Exception as e:  # noqa: BLE001
                err = err or e
        if err:
            raise err
        return restored
