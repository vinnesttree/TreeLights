# Home Assistant

TreeLights connects to Home Assistant through an **MQTT broker**. Once both are connected to the same broker, the tree appears in Home Assistant by itself (MQTT discovery), with no YAML and no custom integration.

Home Assistant can't find TreeLights on the network without a broker: discovery happens through the broker, so the broker and Home Assistant's MQTT integration are needed even if you have no other MQTT devices.

- [What you get](#what-you-get)
- [Setup](#setup), in 5 steps
- [Using it](#using-it): dashboards, automations, scripts
- [How it behaves](#how-it-behaves)
- [Several trees](#several-trees)
- [Turning it off](#turning-it-off)
- [MQTT reference](#mqtt-reference) (for Node-RED or other MQTT tools)
- [Troubleshooting](#troubleshooting)

## What you get

A device named **TreeLights** under *Settings → Devices & services → MQTT*, with:

| Entity | Type | What it does |
|---|---|---|
| `light.treelights` | Light | On/off, brightness, effect (the effect list), RGB color. The color sets the effect's *Color 1*. |
| `select.treelights_palette` | Select | The palette, as on the Effects page. |
| `number.treelights_speed` | Number (0–255) | The *Speed* slider. |
| `number.treelights_intensity` | Number (0–255) | The effect's first slider (*Intensity*, *Density*, *Repeats*…, depending on the effect). |
| `switch.treelights_schedule` | Switch (configuration) | Runs or pauses the timers on TreeLights' Schedule page. |
| `sensor.treelights_next_timer` | Timestamp sensor | When the next TreeLights timer fires. Attributes: `action` (`on`/`off`) and `description`. |

The entity ids are Home Assistant's defaults; Home Assistant adds a number (`light.treelights_2`) if the name is taken, and you can rename them.

## Setup

### 1. Run a TreeLights version with Home Assistant support

The Setup page of TreeLights must have a **Home Assistant** card. If it doesn't, the container runs an older image: update it (see *Updating* in the [README](../README.md#updating)).

### 2. An MQTT broker

Choose one:

- **Home Assistant OS or Supervised:** install the **Mosquitto broker** add-on (*Settings → Add-ons → Add-on store → Mosquitto broker → Install*, then *Start*; newer versions of Home Assistant may call add-ons *apps*). Turn on *Start on boot*.
- **Home Assistant Container or Core:** run a broker yourself, for example the `eclipse-mosquitto` image, on a machine both Home Assistant and TreeLights can reach. Set it up with a password file (see Mosquitto's documentation).
- **A broker you already use** (for Zigbee2MQTT, Tasmota, …): use that one.

### 3. Home Assistant's MQTT integration

*Settings → Devices & services*. With the Mosquitto add-on, Home Assistant usually offers **MQTT** as *Discovered*: click *Configure* and *Submit*. Otherwise *Add integration → MQTT* and enter the broker.

**Discovery must be on.** It is by default. To check, open the MQTT integration's options (*Settings → Devices & services → MQTT → Configure*; on the page that opens, the button to change the MQTT options). After the broker page comes a page with *Enable discovery*, which must be on, and the *Discovery prefix*, which must be `homeassistant` (or enter the same prefix in TreeLights).

### 4. A login for TreeLights

The Mosquitto add-on doesn't accept connections without a username and password. Either:

- create a Home Assistant user for TreeLights: *Settings → People → Users → Add user* (the *Users* tab needs *Advanced mode*, in your user profile). Name it e.g. `treelights`, with a password, and *Can only log in from the local network* on. The add-on accepts Home Assistant users. The names `homeassistant` and `addons` are reserved and won't work; or
- add a login under *logins* in the Mosquitto add-on's *Configuration* tab, then restart the add-on.

With another broker, use a user from its password file.

### 5. Connect TreeLights

On TreeLights' **Setup** page, in the **Home Assistant** card:

1. Tick **Connect to Home Assistant**.
2. **Broker address:** the IP address of the machine running the broker. For the Mosquitto add-on, that's Home Assistant's own IP address, e.g. `192.168.1.10`.
   - Use the IP address, not `homeassistant.local`: the container usually can't resolve `.local` names.
   - Not `127.0.0.1`, unless the broker runs on the TreeLights host itself.
3. **Port:** `1883` (or `8883` with *TLS*, if your broker is set up for it).
4. **Username** and **Password** from step 4.
5. Leave **Topic** (`treelights`) and **Discovery prefix** (`homeassistant`) unless you need to change them.
6. **Save.** The status turns **OK – Connected to …** and says *6 entities announced to Home Assistant*.

The device then appears within seconds under *Settings → Devices & services → MQTT → devices* as **TreeLights**. Add its light to a dashboard from the device page.

## Using it

### Dashboard

The light's more-info dialog has brightness, color and the effect list. Add the palette, speed and intensity entities as well to control the effect as on the Effects page.

### Automations

TreeLights has its own timers (the Schedule page), which work even when Home Assistant is down. You can use those, or Home Assistant automations, or both. To let Home Assistant decide, pause TreeLights' timers with `switch.treelights_schedule`.

On at sunset, with an effect and a palette:

```yaml
alias: Tree on at sunset
triggers:
  - trigger: sun
    event: sunset
    offset: "-00:15:00"
actions:
  - action: light.turn_on
    target:
      entity_id: light.treelights
    data:
      effect: Twinkle
      brightness_pct: 60
  - action: select.select_option
    target:
      entity_id: select.treelights_palette
    data:
      option: Christmas
```

Off at 23:00:

```yaml
alias: Tree off at night
triggers:
  - trigger: time
    at: "23:00:00"
actions:
  - action: light.turn_off
    target:
      entity_id: light.treelights
```

Solid warm white (the color sets *Color 1*, which *Solid* shows):

```yaml
action: light.turn_on
target:
  entity_id: light.treelights
data:
  effect: Solid
  rgb_color: [255, 180, 100]
```

**Order matters when you change the effect and its settings together:** choosing an effect loads that effect's own slider values (as on the Effects page), so set *Speed* and *Intensity* after `light.turn_on` with an `effect`, not before. The colors and the palette are kept when the effect changes.

Effects are named as on the Effects page (*Twinkle*, *Helix 3D*, *Candy Stripes*…). `light.treelights` lists them in its `effect_list` attribute (*Developer tools → States*).

## How it behaves

- **On** runs the chosen effect, or the last one used (with its colors, palette, sliders and brightness). **Off** stops it and turns WLED itself off, so the tree goes dark instead of showing WLED's own effect.
- **3D and surface effects** need an active map. Until there is one, the effect list has only the effects that don't (*Solid*, *Palette Flow*, *Twinkle*, *Morse Xmas*); it updates within about 15 seconds after a map is activated.
- **During a scan** the LEDs belong to the scan: commands from Home Assistant are refused, and Home Assistant shows the real state again.
- **Changes made elsewhere** (the Effects or Schedule page, a TreeLights timer) show up in Home Assistant at once.
- **Unavailable** in Home Assistant means TreeLights isn't connected to the broker (it's stopped, or can't reach it). It reconnects by itself.
- After Home Assistant or TreeLights restarts, everything is announced again. If the lights were on, they're on again after TreeLights restarts.

## Several trees

Give each TreeLights server its own **Topic** on the Setup page (e.g. `treelights-porch`). The topic also names the device's ids in Home Assistant, so each tree becomes its own device. Rename the devices in Home Assistant to tell them apart.

## Turning it off

Untick **Connect to Home Assistant** and **Save**: TreeLights removes its device and entities from Home Assistant. Changing the topic or the discovery prefix also removes the old ones before announcing the new ones.

## MQTT reference

`<topic>` is the Topic setting (default `treelights`); `<prefix>` the discovery prefix (default `homeassistant`). TreeLights sends states retained, at QoS 1.

| Topic | Direction | Payload |
|---|---|---|
| `<topic>/status` | TreeLights → | `online` / `offline` (the broker sends `offline` if TreeLights disappears) |
| `<topic>/light/state` | TreeLights → | JSON: `{"state": "ON", "brightness": 128, "effect": "Twinkle", "color_mode": "rgb", "color": {"r": 255, "g": 160, "b": 0}}` |
| `<topic>/light/set` | → TreeLights | JSON, any of: `{"state": "ON"/"OFF", "brightness": 0-255, "effect": "<effect name>", "color": {"r", "g", "b"}}` |
| `<topic>/palette/state`, `/palette/set` | both | palette name, e.g. `Christmas` |
| `<topic>/speed/state`, `/speed/set` | both | `0`–`255` |
| `<topic>/intensity/state`, `/intensity/set` | both | `0`–`255` |
| `<topic>/schedule/state`, `/schedule/set` | both | `ON` / `OFF` |
| `<topic>/next_timer/state` | TreeLights → | ISO time of the next timer, or `None` |
| `<topic>/next_timer/attributes` | TreeLights → | JSON: `{"action": "on", "description": "On at sunset −15 min, every day"}` |
| `<prefix>/<component>/<topic>/<entity>/config` | TreeLights → | Discovery configs for Home Assistant |
| `<prefix>/status` | → TreeLights | `online` from Home Assistant makes TreeLights announce everything again |

## Troubleshooting

First look at the status in the **Home Assistant** card on TreeLights' Setup page.

| Status or symptom | What to do |
|---|---|
| No *Home Assistant* card on the Setup page | The container runs an older image: update it. |
| *Can't reach the MQTT broker at …* | Wrong address or port, or a firewall. Use the broker's IP address (not a `.local` name, not `127.0.0.1` unless the broker is on the same host). The Mosquitto add-on listens on port 1883 of Home Assistant's address. |
| *The broker refused the connection: Not authorized* or *Bad user name or password* | Wrong or missing username and password. See [step 4](#4-a-login-for-treelights). After adding a login to the Mosquitto add-on's configuration, restart the add-on. |
| Stays on *Connecting…* | TreeLights retries every few seconds; check the address. The container log (`docker compose logs treelights`) has `MQTT:` lines. |
| **OK – Connected**, but no TreeLights device in Home Assistant | Home Assistant isn't listening on the same broker, or discovery is off, or the prefixes differ. Check [step 3](#3-home-assistants-mqtt-integration): the MQTT integration must use the same broker, with *Enable discovery* on and the same prefix as TreeLights. Then press **Announce again**. |
| Still no device | Check what reaches the broker: in Home Assistant, *Settings → Devices & services → MQTT → Configure* has a tool to listen to a topic. Listen to `homeassistant/+/treelights/#` (with the Topic you set in place of `treelights`), then press **Announce again** in TreeLights. Six messages should arrive. If they do, Home Assistant received them: look in *Settings → System → Logs* for MQTT errors. If none arrive, TreeLights and Home Assistant use different brokers. |
| The device is there but *unavailable* | TreeLights isn't connected right now (see its Setup page), or another TreeLights server uses the same Topic. |
| An effect is missing from the list | It needs a map; activate one on the Map page. |
| A command does nothing | During a scan, commands are refused. Otherwise the container log shows `Home Assistant command … refused` with the reason. |
| Old entities left behind after changing the Topic while TreeLights was disconnected | Delete the old device in Home Assistant (*device page → ⋮ → Delete*). |

From a computer with the Mosquitto clients installed, you can also watch everything TreeLights sends:

```sh
mosquitto_sub -h <broker IP> -u <user> -P <password> -v -t 'treelights/#' -t 'homeassistant/+/treelights/#'
```
