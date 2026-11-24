"""Home Assistant integration over MQTT, with MQTT discovery: the tree appears in Home
Assistant as a device without any YAML.

Entities (topics under `<base>` = the `base_topic` setting, default "treelights"):

| Entity | Topics | |
|---|---|---|
| light | `<base>/light/state`, `<base>/light/set` | JSON schema: on/off, brightness, effect, RGB color (Color 1) |
| select "Palette" | `<base>/palette/state`, `<base>/palette/set` | |
| number "Speed", "Intensity" | `<base>/speed/...`, `<base>/intensity/...` | 0-255 |
| switch "Schedule" | `<base>/schedule/state`, `<base>/schedule/set` | turns all timers on or off |
| sensor "Next timer" | `<base>/next_timer/state`, `.../attributes` | timestamp of the next timer event |

Availability is `<base>/status` ("online"/"offline", the broker sends "offline" if the
server disappears). Discovery configs and states are retained, and everything is sent
again when Home Assistant announces itself on `<discovery_prefix>/status`.

paho-mqtt runs its own network thread, reconnecting by itself; its callbacks hand work to
the asyncio loop. Turning the integration off, or changing its topics, removes the entities
from Home Assistant.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import ssl
import uuid
from typing import TYPE_CHECKING, Any

import paho.mqtt.client as mqtt

from . import __version__
from .config import MqttSettings
from .effects import LIBRARY, catalogue
from .lights import LightsBusy

if TYPE_CHECKING:
    from .app import AppState

log = logging.getLogger(__name__)

REFRESH_S = 15.0  # states that change without a notification (a scan taking over, the next timer)
SOURCE = "Home Assistant"


def node_id(base_topic: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", base_topic)


def _hex(c: dict) -> str:
    return "#%02x%02x%02x" % tuple(int(min(max(float(c.get(k, 0)), 0), 255)) for k in "rgb")


def _rgb(hex_color: str) -> dict:
    s = hex_color.lstrip("#")
    return {"r": int(s[0:2], 16), "g": int(s[2:4], 16), "b": int(s[4:6], 16)}


class HomeAssistant:
    def __init__(self, app: "AppState"):
        self.app = app
        self.client: mqtt.Client | None = None
        self.settings: MqttSettings | None = None  # the settings the client was started with
        self.connected = False
        self.error: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sent: dict[str, str] = {}  # topic -> payload last published (retained)
        self._task: asyncio.Task | None = None
        self._apply_lock = asyncio.Lock()

    # ---- topics and payloads -----------------------------------------------------------------
    def _effects(self) -> list[tuple[str, str]]:
        """(name, label) of the effects that can run now (those needing a map only with one)."""
        has_map = self.app.has_map()
        return [(name, cls.label or name) for name, cls in LIBRARY.items() if has_map or not cls.needs_map]

    @staticmethod
    def _palettes() -> list[tuple[str, str]]:
        return [(p["id"], p["name"]) for p in catalogue()]

    def _discovery(self, s: MqttSettings) -> dict[str, dict]:
        b, node = s.base_topic, node_id(s.base_topic)
        device = {"identifiers": [node], "name": "TreeLights", "manufacturer": "TreeLights",
                  "model": "Mapped LED tree (WLED)", "sw_version": __version__}
        common = {"availability_topic": f"{b}/status", "device": device,
                  "origin": {"name": "TreeLights", "sw_version": __version__}}
        p = s.discovery_prefix
        return {
            f"{p}/light/{node}/light/config": {
                **common, "name": None, "unique_id": f"{node}_light", "schema": "json", "icon": "mdi:pine-tree",
                "command_topic": f"{b}/light/set", "state_topic": f"{b}/light/state",
                "brightness": True, "brightness_scale": 255, "supported_color_modes": ["rgb"],
                "effect": True, "effect_list": [label for _, label in self._effects()],
            },
            f"{p}/select/{node}/palette/config": {
                **common, "name": "Palette", "unique_id": f"{node}_palette", "icon": "mdi:palette",
                "command_topic": f"{b}/palette/set", "state_topic": f"{b}/palette/state",
                "options": [label for _, label in self._palettes()],
            },
            f"{p}/number/{node}/speed/config": {
                **common, "name": "Speed", "unique_id": f"{node}_speed", "icon": "mdi:speedometer",
                "command_topic": f"{b}/speed/set", "state_topic": f"{b}/speed/state",
                "min": 0, "max": 255, "step": 1, "mode": "slider",
            },
            f"{p}/number/{node}/intensity/config": {
                **common, "name": "Intensity", "unique_id": f"{node}_intensity", "icon": "mdi:tune-vertical",
                "command_topic": f"{b}/intensity/set", "state_topic": f"{b}/intensity/state",
                "min": 0, "max": 255, "step": 1, "mode": "slider",
            },
            f"{p}/switch/{node}/schedule/config": {
                **common, "name": "Schedule", "unique_id": f"{node}_schedule", "icon": "mdi:timer-outline",
                "command_topic": f"{b}/schedule/set", "state_topic": f"{b}/schedule/state",
                "payload_on": "ON", "payload_off": "OFF", "entity_category": "config",
            },
            f"{p}/sensor/{node}/next_timer/config": {
                **common, "name": "Next timer", "unique_id": f"{node}_next_timer", "icon": "mdi:timer-play-outline",
                "device_class": "timestamp", "state_topic": f"{b}/next_timer/state",
                "json_attributes_topic": f"{b}/next_timer/attributes",
            },
        }

    def _states(self, s: MqttSettings) -> dict[str, str]:
        b = s.base_topic
        st = self.app.lights.status()
        params = st["params"]
        labels = dict(self._palettes())
        light = {"state": "ON" if st["on"] else "OFF", "brightness": max(1, round(st["brightness"] * 255)),
                 "effect": st["label"], "color_mode": "rgb", "color": _rgb(params["colors"][0])}
        nxt = self.app.scheduler.next_event()
        return {
            f"{b}/light/state": json.dumps(light),
            f"{b}/palette/state": labels.get(params["palette"], params["palette"]),
            f"{b}/speed/state": str(params["speed"]),
            f"{b}/intensity/state": str(params["intensity"]),
            f"{b}/schedule/state": "ON" if self.app.config.settings.schedule.enabled else "OFF",
            # "None" is Home Assistant's "unknown" for an MQTT sensor.
            f"{b}/next_timer/state": nxt["at"] if nxt else "None",
            f"{b}/next_timer/attributes": json.dumps({"action": nxt["action"], "description": nxt["description"]}
                                                     if nxt else {"action": None, "description": None}),
        }

    # ---- publishing --------------------------------------------------------------------------
    def _publish(self, force: bool = False) -> None:
        """Send discovery configs and states that changed (all of them with `force`)."""
        c, s = self.client, self.settings
        if c is None or s is None or not self.connected:
            return
        try:
            msgs = {t: json.dumps(v) for t, v in self._discovery(s).items()}
            msgs.update(self._states(s))
        except Exception:  # noqa: BLE001
            log.exception("building Home Assistant states failed")
            return
        for topic, payload in msgs.items():
            if force or self._sent.get(topic) != payload:
                c.publish(topic, payload, qos=1, retain=True)
                self._sent[topic] = payload

    def changed(self) -> None:
        """Something changed: publish (callable from any thread)."""
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._publish)

    # ---- commands ----------------------------------------------------------------------------
    def _subscriptions(self, s: MqttSettings) -> list[str]:
        b = s.base_topic
        return [f"{b}/{e}/set" for e in ("light", "palette", "speed", "intensity", "schedule")] + [f"{s.discovery_prefix}/status"]

    async def handle(self, topic: str, payload: str) -> None:
        s = self.settings
        if s is None:
            return
        b = s.base_topic
        lights = self.app.lights
        try:
            if topic == f"{s.discovery_prefix}/status":
                if payload.strip().lower() == "online":  # Home Assistant (re)started
                    self._publish(force=True)
                return
            if topic == f"{b}/light/set":
                cmd = json.loads(payload)
                if not isinstance(cmd, dict):
                    raise ValueError("expected a JSON object")
                if str(cmd.get("state", "ON")).upper() == "OFF":
                    await lights.turn_off(source=SOURCE)
                    return
                effect = None
                if cmd.get("effect") is not None:
                    by_label = {label: name for name, label in self._effects()}
                    effect = by_label.get(cmd["effect"]) or (cmd["effect"] if cmd["effect"] in LIBRARY else None)
                    if effect is None:
                        raise ValueError(f"unknown effect {cmd['effect']!r}")
                params: dict[str, Any] = {}
                if isinstance(cmd.get("color"), dict):
                    colors = list(lights.status()["params"]["colors"])
                    colors[0] = _hex(cmd["color"])
                    params["colors"] = colors
                bri = cmd.get("brightness")
                await lights.turn_on(effect, params or None, None if bri is None else float(bri) / 255.0, source=SOURCE)
            elif topic == f"{b}/palette/set":
                by_label = {label: pid for pid, label in self._palettes()}
                pid = by_label.get(payload) or (payload if payload in dict(self._palettes()) else None)
                if pid is None:
                    raise ValueError(f"unknown palette {payload!r}")
                await lights.set_params({"palette": pid}, source=SOURCE)
            elif topic in (f"{b}/speed/set", f"{b}/intensity/set"):
                key = topic.split("/")[-2]
                await lights.set_params({key: int(round(float(payload)))}, source=SOURCE)
            elif topic == f"{b}/schedule/set":
                on = payload.strip().upper() in ("ON", "TRUE", "1")
                self.app.config.update({"schedule": {"enabled": on}})
                self.app.scheduler.poke()
        except (LightsBusy, ValueError, KeyError, TypeError) as e:
            log.warning("Home Assistant command on %s refused: %s", topic, e)
        except Exception as e:  # noqa: BLE001 - HTTPException from the effect code, WLED errors
            log.warning("Home Assistant command on %s failed: %s", topic, getattr(e, "detail", None) or e)
        finally:
            self._publish(force=topic.endswith("/set"))  # put Home Assistant's optimistic state right

    # ---- paho callbacks (network thread) -------------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:  # noqa: ANN001
        if client is not self.client:
            return
        if reason_code.is_failure:
            self.connected = False
            self.error = f"The broker refused the connection: {reason_code}"
            log.warning("MQTT: %s", self.error)
            return
        s = self.settings
        assert s is not None
        self.connected, self.error = True, None
        log.info("MQTT: connected to %s:%s", s.host, s.port)
        client.publish(f"{s.base_topic}/status", "online", qos=1, retain=True)
        for t in self._subscriptions(s):
            client.subscribe(t, qos=1)
        if self._loop:
            self._loop.call_soon_threadsafe(self._publish, True)

    def _on_connect_fail(self, client, userdata) -> None:  # noqa: ANN001
        if client is self.client:
            s = self.settings
            self.connected = False
            self.error = f"Can't reach the MQTT broker at {s.host}:{s.port}" if s else "Can't reach the MQTT broker"

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:  # noqa: ANN001
        if client is self.client and self.connected:
            self.connected = False
            if reason_code.is_failure:
                self.error = f"Disconnected from the broker: {reason_code}"
            log.warning("MQTT: disconnected (%s); reconnecting", reason_code)

    def _on_message(self, client, userdata, msg) -> None:  # noqa: ANN001
        if client is not self.client or not self._loop:
            return
        payload = msg.payload.decode("utf-8", "replace")
        asyncio.run_coroutine_threadsafe(self.handle(msg.topic, payload), self._loop)

    # ---- lifecycle ---------------------------------------------------------------------------
    def status(self) -> dict:
        s = self.app.config.settings.mqtt
        return {"enabled": s.enabled, "configured": bool(s.host), "connected": self.connected,
                "broker": f"{s.host}:{s.port}" if s.host else None, "error": self.error,
                "base_topic": s.base_topic, "discovery_prefix": s.discovery_prefix}

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.app.lights.subscribe(self.changed)
        self.app.scheduler.subscribe(self.changed)
        await self.apply()
        self._task = asyncio.ensure_future(self._refresh())

    async def _refresh(self) -> None:
        while True:
            await asyncio.sleep(REFRESH_S)
            self._publish()

    async def apply(self) -> None:
        """(Re)connect with the current settings; call after they change."""
        async with self._apply_lock:
            new = self.app.config.settings.mqtt
            if self.client is not None and self.settings == new:
                return
            old = self.settings
            removed = old is not None and (not new.enabled or old.base_topic != new.base_topic
                                           or old.discovery_prefix != new.discovery_prefix)
            await self._disconnect(remove_entities=removed)
            self.settings = None
            if not new.enabled or not new.host:
                self.error = None if not new.enabled else "No broker address set."
                return
            self.settings = new
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                            client_id=f"treelights-{node_id(new.base_topic)}-{uuid.uuid4().hex[:6]}",
                            protocol=mqtt.MQTTv311)
            if new.username:
                c.username_pw_set(new.username, new.password or None)
            if new.tls:
                c.tls_set(cert_reqs=ssl.CERT_REQUIRED)
            c.will_set(f"{new.base_topic}/status", "offline", qos=1, retain=True)
            c.reconnect_delay_set(1, 30)
            c.on_connect, c.on_connect_fail = self._on_connect, self._on_connect_fail
            c.on_disconnect, c.on_message = self._on_disconnect, self._on_message
            self.client, self.connected, self.error, self._sent = c, False, None, {}
            try:
                c.connect_async(new.host, new.port, keepalive=30)
                c.loop_start()
            except Exception as e:  # noqa: BLE001
                self.error = f"Can't connect to the MQTT broker: {e}"
                log.warning("MQTT: %s", self.error)

    async def _disconnect(self, remove_entities: bool) -> None:
        c, s = self.client, self.settings
        self.client = None
        if c is None:
            return
        was_connected, self.connected = self.connected, False

        def finish() -> None:
            infos = []
            if was_connected and s is not None:
                if remove_entities:  # an empty retained config deletes the entity in Home Assistant
                    infos += [c.publish(t, "", qos=1, retain=True) for t in self._discovery(s)]
                    infos += [c.publish(t, "", qos=1, retain=True) for t in self._sent if not t.endswith("/config")]
                infos.append(c.publish(f"{s.base_topic}/status", "offline", qos=1, retain=True))
                for i in infos:
                    try:
                        i.wait_for_publish(timeout=2)
                    except (RuntimeError, ValueError):
                        pass
            c.disconnect()
            c.loop_stop()

        await asyncio.get_running_loop().run_in_executor(None, finish)
        self._sent = {}

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        async with self._apply_lock:
            await self._disconnect(remove_entities=False)
            self.settings = None
