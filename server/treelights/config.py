"""Settings, persisted to <data>/config.json."""

from __future__ import annotations

import os
import re
import threading
import uuid
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator

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
    # Walk-around: record at 1920x1080 (instead of 1280x720) and find the LEDs at the
    # recording's full size. Helps dense trees (simulated 1600 LEDs on a 2.9 m tree: 67% instead
    # of 51% measured from one walk); bigger uploads, and finding LEDs takes ~2x longer.
    walk_full_resolution: bool = False


class Timer(BaseModel):
    """Turn the lights on or off at a time of day, or at sunrise / sunset with an offset."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    enabled: bool = True
    action: Literal["on", "off"] = "on"
    trigger: Literal["time", "sunrise", "sunset"] = "time"
    time: str = "18:00"  # HH:MM local time, for trigger "time"
    offset_min: int = Field(0, ge=-720, le=720)  # minutes after (negative: before) sunrise / sunset
    days: list[int] = Field(default_factory=lambda: list(range(7)))  # 0 = Monday ... 6 = Sunday
    effect: str | None = None  # "on" only: the effect to run; None keeps the last one used
    brightness: float | None = Field(None, ge=0.0, le=1.0)  # "on" only; None keeps the last one

    @field_validator("time")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", v.strip())
        if not m or int(m[1]) > 23 or int(m[2]) > 59:
            raise ValueError("time must be HH:MM (00:00-23:59)")
        return f"{int(m[1]):02d}:{m[2]}"

    @field_validator("days")
    @classmethod
    def _days(cls, v: list[int]) -> list[int]:
        if any(not 0 <= d <= 6 for d in v):
            raise ValueError("days are 0 (Monday) to 6 (Sunday)")
        return sorted(set(v))


class ScheduleSettings(BaseModel):
    enabled: bool = True
    # IANA name such as "Europe/Berlin". None: the TZ environment variable, else UTC.
    timezone: str | None = None
    latitude: float | None = Field(None, ge=-90, le=90)  # needed for sunrise / sunset
    longitude: float | None = Field(None, ge=-180, le=180)  # positive east
    timers: list[Timer] = Field(default_factory=list)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        try:
            ZoneInfo(v.strip())
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown time zone {v!r} (use a name like Europe/Berlin)")
        return v.strip()


class MqttSettings(BaseModel):
    """Home Assistant integration through an MQTT broker, with MQTT discovery."""

    enabled: bool = False
    host: str | None = None
    port: int = Field(1883, ge=1, le=65535)
    username: str | None = None
    password: str | None = None
    tls: bool = False
    discovery_prefix: str = "homeassistant"
    # Topics are <base_topic>/...; also names the device in Home Assistant's unique ids, so
    # give each TreeLights server its own.
    base_topic: str = "treelights"

    @field_validator("discovery_prefix", "base_topic")
    @classmethod
    def _topic(cls, v: str) -> str:
        v = v.strip().strip("/")
        if not v or any(c in v for c in "#+ ") or len(v) > 100:
            raise ValueError("a topic must be non-empty, without spaces, '#' or '+'")
        return v


class Settings(BaseModel):
    tree_height_cm: float | None = None
    pin: str | None = None
    https_port: int = 8443
    http_port: int = 8080
    raw_frame_retention_scans: int = 2
    wled: WledSettings = Field(default_factory=WledSettings)
    power: PowerSettings = Field(default_factory=PowerSettings)
    capture: CaptureSettings = Field(default_factory=CaptureSettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)
    mqtt: MqttSettings = Field(default_factory=MqttSettings)


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
