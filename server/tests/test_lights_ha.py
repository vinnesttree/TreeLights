"""Lights on/off (with WLED's own power), restart, lease renewal, and the Home Assistant MQTT bridge."""

import asyncio
import concurrent.futures
import json
import shutil
import socket
import subprocess
import time

import httpx
import numpy as np
import pytest
import uvicorn

from treelights.app import AppState, create_app
from treelights.lights import LightsBusy
from treelights.sim.virtual_wled import VirtualWled


def _small_map(n):
    t = np.linspace(0, 6 * np.pi, n)
    h = np.linspace(0, 100, n)
    r = 40 * (1 - h / 110)
    leds = [{"i": i, "p": [float(r[i] * np.cos(t[i])), float(h[i]), float(r[i] * np.sin(t[i]))],
             "status": "measured", "segments": 2, "err_px": 0.5, "sigma": 0.5} for i in range(n)]
    return {"id": "m1", "scan": "s1", "leds": leds, "surface": {"h": [0, 50, 100], "r": [40, 22, 4]},
            "quality": {"activation_blocked": None, "warnings": []}, "wled": {}}


async def _state_with_wled(path, n=40):
    wled = VirtualWled(n)
    await wled.start()
    state = AppState(path, executor=concurrent.futures.ThreadPoolExecutor(1))
    state.config.update({"wled": {"host": "127.0.0.1", "http_port": wled.http_port, "udp_port": wled.udp_port,
                                  "led_count": n}})
    state.link.set_host("127.0.0.1", port=wled.http_port)
    state._apply_sender()
    return state, wled


async def test_on_off_switches_wled_and_survives_restart(tmp_path):
    state, wled = await _state_with_wled(tmp_path / "data")
    try:
        wled.state["on"] = False
        st = await state.lights.turn_on(source="test")
        assert st["on"] and st["effect"] == "twinkle" and st["params"]["palette"] == "bulbs"
        assert state.output.holds("test") and wled.state["on"] is True
        # Change while on: brightness and colors apply live, and are remembered.
        await state.lights.turn_on(params={"colors": ["#00ff00"]}, brightness=0.2)
        assert state.effect_state()["brightness"] == 0.2
        assert state.effect_state()["params"]["colors"][0] == "#00ff00"
        # Another effect keeps the colors and palette (as in WLED) and takes its own sliders.
        await state.lights.turn_on(effect="palette")
        cur = state.effect_state()
        assert cur["effect"] == "palette" and cur["params"]["colors"][0] == "#00ff00" and cur["params"]["intensity"] == 40
        assert cur["params"]["palette"] == "bulbs"
        # A restart: the new instance switches on again with the same effect.
        state.output.stop()
        state2 = AppState(tmp_path / "data", executor=concurrent.futures.ThreadPoolExecutor(1))
        state2.link.set_host("127.0.0.1", port=wled.http_port)
        await state2.lights.restore()
        cur = state2.effect_state()
        assert cur and cur["effect"] == "palette" and cur["brightness"] == 0.2
        # Off: the effect stops and WLED itself is turned off (not back to its own effect).
        st = await state2.lights.turn_off(source="test")
        assert not st["on"] and state2.output.holder() is None and wled.state["on"] is False
        state2.output.stop()
        state3 = AppState(tmp_path / "data", executor=concurrent.futures.ThreadPoolExecutor(1))
        await state3.lights.restore()
        assert state3.effect_state() is None
        # While off, parameter changes are kept for the next "on".
        await state3.lights.set_params({"speed": 10}, brightness=0.7)
        assert state3.lights.status()["params"]["speed"] == 10 and state3.lights.brightness == 0.7
        state3.output.stop()
    finally:
        await state.link.close()
        await wled.stop()


async def test_busy_map_effects_and_lease_renewal(tmp_path):
    state = AppState(tmp_path / "data", executor=concurrent.futures.ThreadPoolExecutor(1))
    try:
        with pytest.raises(ValueError, match="needs a map"):
            await state.lights.turn_on(effect="helix")
        with pytest.raises(ValueError):
            await state.lights.turn_on(effect="no-such-effect")
        # A scan holds the LEDs: switching is refused, the capture keeps them.
        ok, _ = state.output.acquire("walk", "calibration", ttl=30)
        assert ok
        with pytest.raises(LightsBusy):
            await state.lights.turn_on()
        with pytest.raises(LightsBusy):
            await state.lights.turn_off()
        state.output.release("walk")
        await state.lights.turn_on()
        # The lease is renewed while the effect runs (it used to stop after an hour).
        state.output._lease.expires = time.monotonic() + 5
        state.lights.supervise_once()
        assert state.output._lease.expires > time.monotonic() + 3000
        # A capture takes over: the lights report off.
        ok, _ = state.output.acquire("walk", "calibration", ttl=30)
        state.lights.supervise_once()
        assert not state.lights.status()["on"] and not state.lights.on
    finally:
        state.output.stop()


async def test_lights_and_schedule_api(tmp_path):
    state = AppState(tmp_path / "data", executor=concurrent.futures.ThreadPoolExecutor(1))
    app = create_app(state)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
            r = await http.post("/api/lights", json={"on": True, "effect": "solid", "brightness": 0.4,
                                                     "params": {"colors": ["#ff0000"]}})
            assert r.status_code == 200 and r.json()["on"] and r.json()["effect"] == "solid"
            assert (await http.get("/api/state")).json()["lights"]["on"]
            assert (await http.post("/api/lights", json={"on": True, "effect": "helix"})).status_code == 422
            assert (await http.post("/api/lights", json={"on": False})).json()["on"] is False
            r = await http.put("/api/schedule", json={"timezone": "Europe/Berlin", "latitude": 52.52, "longitude": 13.4,
                                                      "timers": [{"action": "on", "trigger": "sunset", "offset_min": 15},
                                                                 {"action": "off", "time": "23:30", "days": [4, 5]}]})
            body = r.json()
            assert r.status_code == 200 and body["timezone"] == "Europe/Berlin" and body["sun"]["today"]["sunset"]
            assert len(body["upcoming"]) >= 7 and not body["problems"]
            ids = [t["id"] for t in body["settings"]["timers"]]
            assert len(set(ids)) == 2 and all(ids)
            assert (await http.put("/api/schedule", json={"timezone": "Nowhere/Land"})).status_code == 422
            assert (await http.put("/api/schedule", json={"timers": [{"time": "7pm"}]})).status_code == 422
            # MQTT password: written, never read back; the masked value leaves it unchanged.
            await http.put("/api/config", json={"mqtt": {"password": "s3cret"}})
            assert (await http.get("/api/config")).json()["mqtt"]["password"] is True
            await http.put("/api/config", json={"mqtt": {"password": True}})
            assert state.config.settings.mqtt.password == "s3cret"
    finally:
        state.output.stop()


# ---- Home Assistant over a real broker ------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _mosquitto(tmp_path, anonymous=True):
    exe = shutil.which("mosquitto") or (shutil.which("mosquitto", path="/usr/sbin:/usr/local/sbin"))
    if not exe:
        pytest.skip("mosquitto not installed")
    port = _free_port()
    conf = tmp_path / "mosquitto.conf"
    conf.write_text(f"listener {port} 127.0.0.1\nallow_anonymous {'true' if anonymous else 'false'}\npersistence false\n")
    proc = subprocess.Popen([exe, "-c", str(conf)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    return proc, port


@pytest.fixture
def broker(tmp_path):
    proc, port = _mosquitto(tmp_path)
    yield port
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
def broker_with_login(tmp_path):
    proc, port = _mosquitto(tmp_path, anonymous=False)
    yield port
    proc.terminate()
    proc.wait(timeout=5)


class Watcher:
    """A second MQTT client standing in for Home Assistant."""

    def __init__(self, port: int):
        import paho.mqtt.client as mqtt
        self.msgs: dict[str, str] = {}
        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ha-test")
        self.c.on_message = lambda c, u, m: self.msgs.__setitem__(m.topic, m.payload.decode())
        self.c.connect("127.0.0.1", port)
        self.c.subscribe("#", qos=1)
        self.c.loop_start()

    async def wait(self, pred, timeout=5.0):
        t = time.monotonic()
        while time.monotonic() - t < timeout:
            if pred(self.msgs):
                return True
            await asyncio.sleep(0.05)
        return False

    def send(self, topic, payload):
        self.c.publish(topic, payload, qos=1).wait_for_publish(2)

    def close(self):
        self.c.loop_stop()
        self.c.disconnect()


async def test_home_assistant_discovery_and_commands(tmp_path, broker):
    state = AppState(tmp_path / "data", executor=concurrent.futures.ThreadPoolExecutor(1))
    state.maps.activate(state.maps.save(_small_map(state.output.led_count)), None, force=True)
    state.config.update({"mqtt": {"enabled": True, "host": "127.0.0.1", "port": broker, "base_topic": "tree1"},
                         "schedule": {"timezone": "UTC", "timers": [{"action": "on", "time": "17:00"}]}})
    ha = Watcher(broker)
    state.lights.changed = time.time()  # switched just now: no timer event to catch up at start
    try:
        state.scheduler.start()
        await state.ha.start()
        light_cfg = "homeassistant/light/tree1/light/config"
        assert await ha.wait(lambda m: light_cfg in m and m.get("tree1/status") == "online"
                             and "tree1/light/state" in m and "tree1/next_timer/state" in m)
        cfg = json.loads(ha.msgs[light_cfg])
        assert cfg["schema"] == "json" and cfg["unique_id"] == "tree1_light" and "Helix 3D" in cfg["effect_list"]
        assert cfg["command_topic"] == "tree1/light/set" and cfg["device"]["identifiers"] == ["tree1"]
        assert json.loads(ha.msgs["tree1/light/state"])["state"] == "OFF"
        assert ha.msgs["tree1/next_timer/state"].endswith("17:00:00+00:00")
        st = state.ha.status()
        assert st["connected"] and st["announced_at"] and st["entities"] == 6
        assert st["device_topic"] == light_cfg
        # "Announce again" sends the discovery configs once more.
        ha.msgs.pop(light_cfg)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(state)), base_url="http://t") as http:
            assert (await http.post("/api/homeassistant/announce")).status_code == 200
        assert await ha.wait(lambda m: light_cfg in m)

        # On, with an effect (by its label), a color and brightness.
        ha.send("tree1/light/set", json.dumps({"state": "ON", "effect": "Helix 3D", "brightness": 51,
                                               "color": {"r": 0, "g": 0, "b": 255}}))
        assert await ha.wait(lambda m: json.loads(m["tree1/light/state"])["state"] == "ON")
        cur = state.effect_state()
        assert cur["effect"] == "helix" and abs(cur["brightness"] - 0.2) < 1e-6 and cur["params"]["colors"][0] == "#0000ff"
        st = json.loads(ha.msgs["tree1/light/state"])
        assert st == {"state": "ON", "brightness": 51, "effect": "Helix 3D", "color_mode": "rgb",
                      "color": {"r": 0, "g": 0, "b": 255}}
        # Palette, speed, schedule switch.
        ha.send("tree1/palette/set", "Ocean")
        ha.send("tree1/speed/set", "200")
        assert await ha.wait(lambda m: m.get("tree1/palette/state") == "Ocean" and m.get("tree1/speed/state") == "200")
        assert state.effect_state()["params"]["palette"] == "ocean"
        ha.send("tree1/schedule/set", "OFF")
        assert await ha.wait(lambda m: m.get("tree1/schedule/state") == "OFF" and m.get("tree1/next_timer/state") == "None")
        assert state.config.settings.schedule.enabled is False
        # Changes made in the app reach Home Assistant too.
        await state.lights.turn_off(source="app")
        assert await ha.wait(lambda m: json.loads(m["tree1/light/state"])["state"] == "OFF")
        # A bad command is refused, and the true state is sent again.
        ha.msgs.pop("tree1/light/state")
        ha.send("tree1/light/set", json.dumps({"state": "ON", "effect": "Nope"}))
        assert await ha.wait(lambda m: "tree1/light/state" in m)
        assert json.loads(ha.msgs["tree1/light/state"])["state"] == "OFF"
        # Home Assistant restarts: everything is announced again.
        ha.msgs.clear()
        ha.send("homeassistant/status", "online")
        assert await ha.wait(lambda m: light_cfg in m and "tree1/palette/state" in m)
        # Turning the integration off removes the entities from Home Assistant.
        state.config.update({"mqtt": {"enabled": False}})
        await state.ha.apply()
        assert await ha.wait(lambda m: m.get(light_cfg) == "" and m.get("tree1/status") == "offline")
        assert not state.ha.status()["connected"]
    finally:
        ha.close()
        await state.ha.stop()
        await state.scheduler.stop()
        state.output.stop()


async def test_home_assistant_reports_an_unreachable_broker(tmp_path):
    state = AppState(tmp_path / "data", executor=concurrent.futures.ThreadPoolExecutor(1))
    state.config.update({"mqtt": {"enabled": True, "host": "127.0.0.1", "port": _free_port()}})
    try:
        await state.ha.start()
        for _ in range(100):
            if state.ha.status()["error"]:
                break
            await asyncio.sleep(0.05)
        st = state.ha.status()
        assert not st["connected"] and "Can't reach" in st["error"]
    finally:
        await state.ha.stop()
        state.output.stop()


async def test_home_assistant_explains_a_refused_login(tmp_path, broker_with_login):
    state = AppState(tmp_path / "data", executor=concurrent.futures.ThreadPoolExecutor(1))
    state.config.update({"mqtt": {"enabled": True, "host": "127.0.0.1", "port": broker_with_login}})
    try:
        await state.ha.start()
        for _ in range(100):
            if state.ha.status()["error"]:
                break
            await asyncio.sleep(0.05)
        st = state.ha.status()
        assert not st["connected"] and "refused" in st["error"] and "username and password" in st["error"]
    finally:
        await state.ha.stop()
        state.output.stop()
