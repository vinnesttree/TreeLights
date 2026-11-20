"""Settings, persisted to <data>/config.json."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from . import storage


def data_dir() -> Path:
    return Path(os.environ.get("TREELIGHTS_DATA", "/data"))


class WledSettings(BaseModel):
    host: str | None = None  # IP or hostname; refreshed by MAC if it stops answering
    mac: str | None = None
    http_port: int = 80
    led_count: int = 600
    protocol: Literal["ddp", "dnrgb"] = "ddp"
    udp_port: int | None = None  # override for testing; default 4048 (DDP) / 21324 (DNRGB)


class PowerSettings(BaseModel):
    # None: use WLED's configured limit (/json/info leds.maxpwr). 0 disables the cap.
    max_current_ma: int | None = None
    ma_per_channel: float = 20.0  # full-scale current per color channel per LED
    idle_ma_per_led: float = 1.0


class CaptureSettings(BaseModel):
    width: int = 1920
    height: int = 1080
    jpeg_quality: float = 0.9
    # Sequential pass: undecoded runs up to this length between decoded neighbours are retried.
    short_run: int = 8
    thorough: bool = False
    constellation_every: int = 25
    retries: int = 2


class Settings(BaseModel):
    tree_height_cm: float | None = None
    pin: str | None = None
    https_port: int = 8443
    http_port: int = 8080
    raw_frame_retention_scans: int = 2
    wled: WledSettings = Field(default_factory=WledSettings)
    power: PowerSettings = Field(default_factory=PowerSettings)
    capture: CaptureSettings = Field(default_factory=CaptureSettings)


class ConfigStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else data_dir()
        self.path = self.root / "config.json"
        self._lock = threading.Lock()
        raw = storage.read_json(self.path, default=None)
        self._settings = Settings.model_validate(raw) if raw else Settings()

    @property
    def settings(self) -> Settings:
        with self._lock:
            return self._settings.model_copy(deep=True)

    def update(self, patch: dict) -> Settings:
        with self._lock:
            merged = _deep_merge(self._settings.model_dump(), patch)
            self._settings = Settings.model_validate(merged)
            storage.atomic_write_json(self.path, self._settings.model_dump())
            return self._settings.model_copy(deep=True)


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out
