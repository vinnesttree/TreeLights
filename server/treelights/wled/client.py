"""WLED JSON API client and helpers.

Field names differ between WLED versions, so every accessor is defensive: a
missing key yields None ("unknown"), never an exception (architecture §5.2).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

MIN_VERSION = (0, 14)


@dataclass
class WledInfo:
    raw: dict
    version: str | None
    name: str | None
    mac: str | None
    led_count: int | None
    max_current_ma: int | None
    live: bool | None
    live_ip: str | None
    live_mode: str | None
    uptime_s: int | None

    @staticmethod
    def parse(raw: dict) -> "WledInfo":
        leds = raw.get("leds") if isinstance(raw.get("leds"), dict) else {}
        return WledInfo(
            raw=raw,
            version=_str(raw.get("ver")),
            name=_str(raw.get("name")),
            mac=normalize_mac(raw.get("mac")),
            led_count=_int(leds.get("count")),
            max_current_ma=_int(leds.get("maxpwr")),
            live=raw.get("live") if isinstance(raw.get("live"), bool) else None,
            live_ip=_str(raw.get("lip")) or None,
            live_mode=_str(raw.get("lm")) or None,
            uptime_s=_int(raw.get("uptime")),
        )

    def version_tuple(self) -> tuple[int, ...] | None:
        if not self.version:
            return None
        nums = re.findall(r"\d+", self.version)
        return tuple(int(n) for n in nums[:3]) if nums else None

    def version_ok(self) -> bool | None:
        v = self.version_tuple()
        return None if v is None else v[: len(MIN_VERSION)] >= MIN_VERSION


@dataclass
class BusInfo:
    start: int
    length: int
    color_order: int | None = None
    reversed: bool | None = None
    skip: int | None = None


@dataclass
class WledConfigView:
    raw: dict | None
    buses: list[BusInfo] = field(default_factory=list)
    live: dict | None = None

    @staticmethod
    def parse(raw: dict | None) -> "WledConfigView":
        if not isinstance(raw, dict):
            return WledConfigView(raw=None)
        buses: list[BusInfo] = []
        ins = _dig(raw, "hw", "led", "ins")
        if isinstance(ins, list):
            for b in ins:
                if not isinstance(b, dict):
                    continue
                start, length = _int(b.get("start")), _int(b.get("len"))
                if start is None or length is None:
                    continue
                buses.append(BusInfo(start, length, _int(b.get("order")), b.get("rev") if isinstance(b.get("rev"), bool) else None, _int(b.get("skip"))))
        live = _dig(raw, "if", "live")
        return WledConfigView(raw=raw, buses=sorted(buses, key=lambda b: b.start), live=live if isinstance(live, dict) else None)

    def output_breaks(self) -> list[int]:
        """Indices i such that LEDs i and i+1 are on different outputs (no physical link)."""
        out = []
        for b in self.buses[1:]:
            if b.start > 0:
                out.append(b.start - 1)
        return out


def config_fingerprint(info: WledInfo | None, cfg: WledConfigView | None) -> str:
    """Hash of everything that changes which physical LED an index addresses."""
    parts: dict[str, Any] = {"count": info.led_count if info else None}
    if cfg and cfg.raw:
        parts["led"] = _dig(cfg.raw, "hw", "led")
        parts["live"] = cfg.live
    blob = json.dumps(parts, sort_keys=True, default=str).encode()
    return "sha256:" + hashlib.sha256(blob).hexdigest()


class WledClient:
    def __init__(self, host: str, port: int = 80, timeout: float = 3.0):
        self.host = host
        self.port = port
        self._client = httpx.AsyncClient(base_url=f"http://{host}:{port}", timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str) -> Any:
        r = await self._client.get(path)
        r.raise_for_status()
        return r.json()

    async def info(self) -> WledInfo:
        return WledInfo.parse(await self._get("/json/info"))

    async def cfg(self) -> WledConfigView:
        try:
            return WledConfigView.parse(await self._get("/json/cfg"))
        except (httpx.HTTPError, ValueError):
            return WledConfigView(raw=None)  # locked or unsupported: degrade, don't fail

    async def state(self) -> dict:
        return await self._get("/json/state")

    async def set_state(self, patch: dict) -> dict:
        r = await self._client.post("/json/state", json=patch)
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            return {}


def normalize_mac(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    hexd = re.sub(r"[^0-9a-fA-F]", "", v).lower()
    return hexd if len(hexd) == 12 else None


def _dig(d: Any, *keys: str) -> Any:
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    return None


def _str(v: Any) -> str | None:
    return v if isinstance(v, str) else None
