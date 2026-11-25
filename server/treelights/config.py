"""Settings, persisted to <data>/config.json.

Format 2 (several trees, each with one or more WLED controllers): server-wide settings
(PIN, ports, retention, the MQTT broker) and a list of trees. Each tree has its own
controllers, tree height, power limit, capture options, timers and Home Assistant topic.
Format 1, a single tree with a single controller, is migrated at start-up (`migrate`).
"""

from __future__ import annotations

import logging
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

from . import storage

log = logging.getLogger(__name__)

CONFIG_FORMAT = 2  # 1: one tree, one controller (no "format" key); 2: trees with controllers
DEFAULT_TREE = "tree1"
DEFAULT_TREE_NAME = "Tree"
TREES_DIR = "trees"
TREE_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")


def data_dir() -> Path:
    return Path(os.environ.get("TREELIGHTS_DATA", "/data"))


class WledSettings(BaseModel):
    """One WLED controller. A tree has one or more, in order: together they make one index
    space (the first controller drives the tree's first `led_count` LEDs, the second the
    next ones, and so on)."""

    host: str | None = None  # IP or hostname; refreshed by MAC if it stops answering
    mac: str | None = None
    http_port: int = Field(80, ge=1, le=65535)
    led_count: int = Field(600, ge=1)
    protocol: Literal["ddp", "dnrgb"] = "ddp"
    udp_port: int | None = None  # override for testing; default 4048 (DDP) / 21324 (DNRGB)
    name: str | None = None  # shown in the app; WLED's own name if not set
    # Current limit for this controller only (mA). None: the tree's limit, else WLED's own.
    max_current_ma: int | None = Field(None, ge=0)


ControllerSettings = WledSettings


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
    # give each TreeLights server its own. Stored per tree (TreeSettings.mqtt_topic); here it
    # is the topic of the tree being looked at.
    base_topic: str = "treelights"

    @field_validator("discovery_prefix", "base_topic")
    @classmethod
    def _topic(cls, v: str) -> str:
        return check_topic(v)


def check_topic(v: str) -> str:
    v = v.strip().strip("/")
    if not v or any(c in v for c in "#+ ") or len(v) > 100:
        raise ValueError("a topic must be non-empty, without spaces, '#' or '+'")
    return v


class Settings(BaseModel):
    """What one tree's code sees: the server-wide settings plus that tree's own.

    This is the shape config.json had before several trees and controllers existed, so
    code written for one tree (and API clients) keeps working. `wled` is the tree's first
    controller; `controllers` lists all of them, in index order."""

    tree_height_cm: float | None = None
    pin: str | None = None
    https_port: int = 8443
    http_port: int = 8080
    raw_frame_retention_scans: int = 2
    wled: WledSettings = Field(default_factory=WledSettings)
    controllers: list[WledSettings] = Field(default_factory=list)
    power: PowerSettings = Field(default_factory=PowerSettings)
    capture: CaptureSettings = Field(default_factory=CaptureSettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)
    mqtt: MqttSettings = Field(default_factory=MqttSettings)
    tree_id: str = DEFAULT_TREE
    tree_name: str = DEFAULT_TREE_NAME

    @property
    def led_count(self) -> int:
        """LEDs of the whole tree, over all its controllers."""
        return sum(c.led_count for c in self.controllers) if self.controllers else self.wled.led_count


class TreeSettings(BaseModel):
    """One tree: its controllers (in index order), map units, effect power limit, capture
    options, timers and Home Assistant topic."""

    id: str
    name: str = DEFAULT_TREE_NAME
    tree_height_cm: float | None = None
    controllers: list[WledSettings] = Field(default_factory=lambda: [WledSettings()], min_length=1, max_length=16)
    power: PowerSettings = Field(default_factory=PowerSettings)
    capture: CaptureSettings = Field(default_factory=CaptureSettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)
    mqtt_topic: str = "treelights"

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not TREE_ID.fullmatch(v):
            raise ValueError("a tree id is 1-32 letters, digits, '-' or '_'")
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = " ".join(str(v).split())
        if not v or len(v) > 60:
            raise ValueError("a tree name is 1-60 characters")
        return v

    @field_validator("mqtt_topic")
    @classmethod
    def _topic(cls, v: str) -> str:
        return check_topic(v)

    @property
    def led_count(self) -> int:
        return sum(c.led_count for c in self.controllers)


class ServerSettings(BaseModel):
    """config.json, format 2: server-wide settings and the list of trees."""

    format: int = CONFIG_FORMAT
    pin: str | None = None
    https_port: int = 8443
    http_port: int = 8080
    raw_frame_retention_scans: int = 2
    mqtt: MqttSettings = Field(default_factory=MqttSettings)  # the broker; each tree has its own topic
    default_tree: str = DEFAULT_TREE
    trees: list[TreeSettings] = Field(default_factory=lambda: [TreeSettings(id=DEFAULT_TREE)], min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> "ServerSettings":
        ids = [t.id for t in self.trees]
        if len(set(ids)) != len(ids):
            raise ValueError("tree ids must be unique")
        if self.default_tree not in ids:
            raise ValueError(f"the default tree {self.default_tree!r} doesn't exist")
        topics = [t.mqtt_topic for t in self.trees]
        if len(set(topics)) != len(topics):
            raise ValueError("each tree needs its own Home Assistant topic")
        # A controller drives one tree, once: two owners would fight over its LEDs.
        seen: dict[tuple, str] = {}
        for t in self.trees:
            for c in t.controllers:
                keys: list[tuple] = []
                if c.host:
                    keys.append(("host", c.host.lower(), c.http_port))
                if c.mac:
                    keys.append(("mac", c.mac.lower()))
                for k in keys:
                    if k in seen:
                        where = "twice in this tree" if seen[k] == t.id else f"in tree {seen[k]!r} too"
                        raise ValueError(f"controller {c.host or c.mac} is listed {where}; a controller can drive one tree only")
                    seen[k] = t.id
        return self

    def tree(self, tid: str) -> TreeSettings:
        for t in self.trees:
            if t.id == tid:
                return t
        raise KeyError(tid)


# Keys of a settings patch (in the single-tree shape) that are server-wide; TREE_KEYS
# belong to the tree; `wled` is its first controller, `controllers` all of them.
GLOBAL_KEYS = ("pin", "https_port", "http_port", "raw_frame_retention_scans")
TREE_KEYS = ("tree_height_cm", "power", "capture", "schedule")


def tree_dir(root: Path, tid: str) -> Path:
    """Where a tree's scans, maps and state live."""
    return Path(root) / TREES_DIR / tid


def find_tree(root: Path, tid: str | None = None) -> tuple[Path, dict]:
    """(folder, settings in the single-tree shape) of a tree in a data folder, without
    changing anything (for offline tools). A data folder the server hasn't migrated yet
    is the folder itself."""
    root = Path(root)
    raw = storage.read_json(root / "config.json", default=None) or {}
    if int(raw.get("format", 1)) < CONFIG_FORMAT:
        return root, Settings.model_validate(raw).model_dump()
    s = ServerSettings.model_validate(raw)
    tid = tid or s.default_tree
    return tree_dir(root, tid), _view(s, tid).model_dump()


def _view(s: ServerSettings, tid: str) -> Settings:
    t = s.tree(tid)
    mqtt = s.mqtt.model_copy(update={"base_topic": t.mqtt_topic})
    return Settings(tree_height_cm=t.tree_height_cm, pin=s.pin, https_port=s.https_port, http_port=s.http_port,
                    raw_frame_retention_scans=s.raw_frame_retention_scans, wled=t.controllers[0].model_copy(),
                    controllers=[c.model_copy() for c in t.controllers], power=t.power.model_copy(),
                    capture=t.capture.model_copy(), schedule=t.schedule.model_copy(deep=True), mqtt=mqtt,
                    tree_id=t.id, tree_name=t.name)


def _apply_view_patch(raw: dict, tid: str, patch: dict) -> dict:
    """Apply a patch in the single-tree shape (as `PUT /api/config` takes) to config.json's
    format-2 contents."""
    raw = _deep_merge(raw, {})
    raw["trees"] = [dict(t) for t in raw["trees"]]
    tree = next(t for t in raw["trees"] if t["id"] == tid)
    tree["controllers"] = [dict(c) for c in tree["controllers"]]
    for k, v in patch.items():
        if k in GLOBAL_KEYS:
            raw[k] = v
        elif k in TREE_KEYS:
            tree[k] = _deep_merge(tree[k], v) if isinstance(v, dict) and isinstance(tree.get(k), dict) else v
        elif k == "mqtt" and isinstance(v, dict):
            v = dict(v)
            if "base_topic" in v:
                tree["mqtt_topic"] = v.pop("base_topic")
            raw["mqtt"] = _deep_merge(raw.get("mqtt") or {}, v)
        elif k == "wled" and isinstance(v, dict):
            tree["controllers"][0] = _deep_merge(tree["controllers"][0], v)
        elif k == "controllers" and isinstance(v, list):
            tree["controllers"] = [dict(c) for c in v]
        elif k in ("tree_name", "name") and isinstance(v, str):
            tree["name"] = v
    return raw


def _legacy_to_v2(raw: dict, tid: str) -> dict:
    """A format-1 config.json (one tree, one controller) as format 2."""
    base = Settings.model_validate(raw).model_dump()  # fills in defaults, drops unknown keys
    tree = {"id": tid, "name": DEFAULT_TREE_NAME, "tree_height_cm": base["tree_height_cm"],
            "controllers": [base["wled"]], "power": base["power"], "capture": base["capture"],
            "schedule": base["schedule"], "mqtt_topic": base["mqtt"]["base_topic"]}
    out = {k: base[k] for k in GLOBAL_KEYS}
    out.update(format=CONFIG_FORMAT, mqtt=base["mqtt"], default_tree=tid, trees=[tree])
    return ServerSettings.model_validate(out).model_dump()


# What a single-tree install kept directly in <data>; it moves into the first tree's folder.
LEGACY_TREE_DATA = ("scans", "maps", "lights.json", "wled_restore.json")


def migrate(root: Path) -> dict:
    """Bring <root> to the format-2 layout, and return config.json's contents.

    Before there were several trees, config.json held one tree's settings, and its scans,
    maps and state lived directly in <root>. They move into <root>/trees/tree1/, the
    default tree. Every step is atomic, and the whole is safe to repeat:
    1. the old config.json is copied to config.v1.json;
    2. folders and files are moved by rename (one file system), never overwriting;
    3. config.json is rewritten in format 2, atomically, only after everything moved.
    A crash in between repeats the remaining steps at the next start."""
    root = Path(root)
    path = root / "config.json"
    raw = storage.read_json(path, default=None)
    if isinstance(raw, dict) and int(raw.get("format", 1)) >= CONFIG_FORMAT:
        return raw
    legacy_data = [n for n in LEGACY_TREE_DATA if (root / n).exists()]
    if raw is None and not legacy_data:  # a new install
        fresh = ServerSettings().model_dump()
        storage.atomic_write_json(path, fresh)
        return fresh
    tid = DEFAULT_TREE
    v2 = _legacy_to_v2(raw or {}, tid)  # validate before touching anything
    if raw is not None and not (root / "config.v1.json").exists():
        storage.atomic_write_json(root / "config.v1.json", raw)
    dest = tree_dir(root, tid)
    dest.mkdir(parents=True, exist_ok=True)
    for name in legacy_data:
        _move(root / name, dest / name)
    storage.fsync_dir(dest)
    storage.fsync_dir(root)
    storage.atomic_write_json(path, v2)
    log.info("moved the single-tree configuration and data into tree %r (%s)", tid, dest)
    return v2


def _move(src: Path, dst: Path) -> None:
    """Rename src to dst. If a folder already exists at dst (from an interrupted run), the
    entries of src that it lacks are moved into it; nothing in dst is replaced."""
    if not src.exists():
        return
    if not dst.exists():
        os.replace(src, dst)
        return
    if src.is_dir() and dst.is_dir():
        for child in sorted(src.iterdir()):
            _move(child, dst / child.name)
        try:
            src.rmdir()
        except OSError:
            log.warning("migration: %s has entries that also exist in %s; left in place", src, dst)
        return
    log.warning("migration: %s exists in both places; kept %s and left %s in place", src.name, dst, src)


class ConfigStore:
    """config.json: the server-wide settings and every tree's settings.

    `settings` and `update()` are the default tree's view, in the single-tree shape
    (`Settings`); `for_tree()` gives the same view of any tree."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else data_dir()
        self.path = self.root / "config.json"
        self._lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)
        self._server = ServerSettings.model_validate(migrate(self.root))

    # ---- server-wide -------------------------------------------------------------------
    @property
    def server(self) -> ServerSettings:
        with self._lock:
            return self._server.model_copy(deep=True)

    @property
    def default_tree(self) -> str:
        with self._lock:
            return self._server.default_tree

    def tree_ids(self) -> list[str]:
        with self._lock:
            return [t.id for t in self._server.trees]

    def _write(self, raw: dict) -> ServerSettings:
        s = ServerSettings.model_validate(raw)
        storage.atomic_write_json(self.path, s.model_dump())
        self._server = s
        return s

    # ---- one tree's view -------------------------------------------------------------------
    @property
    def settings(self) -> Settings:
        return self.view(self.default_tree)

    def update(self, patch: dict) -> Settings:
        return self.update_view(self.default_tree, patch)

    def view(self, tid: str) -> Settings:
        with self._lock:
            return _view(self._server, tid)

    def update_view(self, tid: str, patch: dict) -> Settings:
        with self._lock:
            self._server.tree(tid)  # KeyError for an unknown tree
            self._write(_apply_view_patch(self._server.model_dump(), tid, patch))
            return _view(self._server, tid)

    def for_tree(self, tid: str) -> "TreeConfig":
        return TreeConfig(self, tid)

    # ---- trees ----------------------------------------------------------------------------
    def add_tree(self, name: str | None = None) -> str:
        with self._lock:
            ids = {t.id for t in self._server.trees}
            k = len(ids) + 1
            while f"tree{k}" in ids or tree_dir(self.root, f"tree{k}").exists():
                k += 1
            tid = f"tree{k}"
            topics = {t.mqtt_topic for t in self._server.trees}
            topic = f"{self._server.mqtt.base_topic}_{tid}"
            while topic in topics:
                topic += "_"
            raw = self._server.model_dump()
            raw["trees"].append(TreeSettings(id=tid, name=name or f"Tree {k}", mqtt_topic=topic).model_dump())
            self._write(raw)
            return tid

    def rename_tree(self, tid: str, name: str) -> None:
        self.update_view(tid, {"name": name})

    def remove_tree(self, tid: str) -> None:
        with self._lock:
            if tid == self._server.default_tree:
                raise ValueError("The first tree can't be removed.")
            self._server.tree(tid)
            raw = self._server.model_dump()
            raw["trees"] = [t for t in raw["trees"] if t["id"] != tid]
            self._write(raw)


class TreeConfig:
    """One tree's settings in the single-tree shape (`Settings`). Code written for one
    tree (timers, Home Assistant, capture) takes this in place of a ConfigStore."""

    def __init__(self, store: ConfigStore, tid: str):
        self.store = store
        self.tree_id = tid

    @property
    def settings(self) -> Settings:
        return self.store.view(self.tree_id)

    def update(self, patch: dict) -> Settings:
        return self.store.update_view(self.tree_id, patch)


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out
