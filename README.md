# TreeLights

TreeLights finds the 3D position of every addressable LED on a Christmas tree using an Android phone's camera in Chrome. The LEDs are driven through stock WLED, and everything runs in one Docker container on a Linux computer on your network.

The result is a 3D map of the tree. The map stores the wiring order, a per-LED uncertainty and a surface model, so 3D effects can be built on it. For now there is one test animation that sweeps planes and a sphere through the tree.

The design, including a failure analysis and how each failure is handled, is in [`architecture.md`](architecture.md).

> **Status:** everything is built and tested in a closed-loop simulator. The simulator includes a virtual WLED reached over real DDP/UDP, a virtual phone over WebSockets, packet loss, camera bumps, reflections and recorded videos. It has **not yet been run against a real tree, ESP32 or phone**. Expect some tuning on first use (§18 step 11 in the architecture).

## What you need

- **Linux host with Docker.** It runs the container with host networking.
- **ESP32 running WLED 0.14 or newer**, driving the LED string. TreeLights doesn't modify the firmware.
- **Android phone with Chrome** on a tripod or stand.
- **Optional:** a second device (phone or computer) to start each view without touching the tripod phone.

## Install

```sh
git clone <this repository> treelights && cd treelights
docker compose -f docker/docker-compose.yml up -d --build
docker compose -f docker/docker-compose.yml logs treelights   # shows the URL and a QR code
```

- The app listens on **https://&lt;host&gt;:8443**. A plain-HTTP setup page with a QR code and help is on **http://&lt;host&gt;:8080**.
- Data is kept in `docker/data/`: config, certificates, scans and maps.
- **Firewall:** if the host runs one, allow TCP 8443 and 8080 from the LAN, and UDP 5353 for mDNS discovery.
- **Certificate warning:** Chrome warns about the certificate because it comes from a local authority the container creates.
  - Tap **Advanced → Proceed**. Chrome asks when the page is opened, never per camera frame.
  - To get rid of the warning entirely, install the CA certificate from the setup page on the phone. This is optional.

## WLED settings

- **Sync settings:** turn on *Receive UDP realtime*. TreeLights sends DDP to port 4048. Keep the realtime timeout at the default (about 2.5 s).
- **LED count:** set it to match the string. The Setup page compares it with TreeLights' setting.
- **Color order and outputs:** TreeLights checks the color order with the camera and compensates. It reads the GPIO outputs from WLED and treats output boundaries as breaks in the chain.

## Mapping the tree

1. **Setup.**
   - Find or enter the controller and run the checks.
   - Enter the **tree height in cm, from the lowest light to the highest light**. It only sets the map's units.
   - Optionally run *Index chase* to confirm the whole string lights.
2. **Camera calibration (optional, once per phone).** Show the board on a computer screen and capture about 15 views with the phone.
3. **Calibrate (tripod phone).** Open the Calibrate page on the phone on the tripod.
   1. Start the camera, light the tree, then tap *Lock camera*.
   2. Keep the screen on. Fullscreen also locks the orientation.
4. **Each view.**
   1. Turn the tree by the suggested angle (about 45°) and step out of the picture.
   2. Start the view from the **Control** page on another device, or from the tripod phone with its 5 s countdown.
   3. The server waits until the tree stops swaying, then captures for about 30–60 s.
   4. Repeat for about 8 views (one full turn), until the guidance says coverage looks good.
5. **Solve.** Use *Solve map* on the Control page.
6. **Map.** Review the 3D map and its quality report.
   - Tap an LED to blink it on the real tree.
   - Fix problems by marking an LED bad, moving it, or adding or removing a chain break, then re-solve.
   - *Activate* the version you want. The previous one stays available for roll-back.
7. **Test.** Run the sweep test and check it looks clean on the tree. *Status colors* shows which LEDs were measured (green), placed from one view (yellow), estimated (orange) or are unknown (off).

**Video mode (fallback).** If live capture doesn't work, use *Play sequence* on the Calibrate page and record it with the phone's own camera app (Pro mode with fixed focus and exposure is best). Then upload the recording. The upload resumes if interrupted.

## How it stays robust (short version)

- **Captures check themselves.** A rotating color per step, exactly one new blob, never where a recent LED was, and a sanity check on the frame. A lost UDP packet or a slow camera gives a rejected frame, never a wrong LED identity.
- **Fast first pass.** All LEDs blink Hamming-coded indices, and every decoded index is spot-checked. One-at-a-time capture fills the gaps.
- **Branches are expected.** An LED behind needles, the light it scatters onto nearby branches, and glints off needles are all handled.
  - Each blob is classed as a point source or diffuse, and carries its own uncertainty.
  - Dim reflections next to a bright LED are dropped.
  - A glint brighter than a mostly hidden LED is kept *alongside* the LED. Glints move between views, so the multi-view fit rejects them.
  - LEDs the fast pass only saw as scattered light get a clean one-LED frame.
- **Movement is caught.** Phone or tree movement is detected by the motion sensors and a reference constellation of LEDs. It starts a new camera segment instead of corrupting data.
- **Positions are solved as a whole.** Camera positions, focal length and distortion are solved from the LEDs themselves. Each LED keeps several candidate positions until the chain decides.
- **The distance limit learns itself.** The maximum distance between consecutive LEDs is learned from the data: a normal bound, longer connector links (found even when they repeat every *n* LEDs), and breaks between outputs. It is solved exactly with a shortest path along the chain.
- **Nothing is invented.** Unmeasured LEDs are placed using the wiring and the parts of the tree the camera couldn't see, and are labelled as such. LEDs that can't be bounded are left `unknown`.

## Map format

`GET /api/maps/<id>/export?fmt=json|csv`. The JSON is described in architecture §14. Positions are in cm (or tree heights if no height was entered). `y` is up, the origin is on the tree axis at the lowest LED, and `+z` points toward the first view.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -r server/requirements.lock pytest==8.3.4 pytest-asyncio==0.25.0 pytest-timeout==2.3.1
cd server && ../.venv/bin/python -m pytest -q            # ~6 min: unit, closed-loop and end-to-end tests
cd web && npm ci && npm run build                         # web app -> web/dist

# Run locally without TLS, then drive it with the simulated rig (virtual WLED + phone + turning tree):
cd server && ../.venv/bin/python -m treelights --data /tmp/tl --web ../web/dist --no-tls --https-port 18443
../.venv/bin/python -m treelights.sim.rig --server http://127.0.0.1:18443 --n 600 --views 8

# Detection and full-pipeline accuracy with branch occlusion, scattered light and needle glints:
PYTHONPATH=. ../.venv/bin/python tools/occlusion_report.py      # per-LED detection vs ground truth
PYTHONPATH=. ../.venv/bin/python tools/foliage_e2e.py 1         # capture + solve, errors in cm
```

Layout:

- `server/treelights/`
  - `wled/`: DDP, JSON API, discovery, monitor
  - `output/`: lease and keepalive thread
  - `capture/`: live session, scan store, video mode
  - `detection/`
  - `reconstruction/`
  - `maps/`
  - `effects/`
  - `sim/`: simulator and rig
- `web/`: Vite + TypeScript + three.js
- `docker/`

## Troubleshooting

| Symptom | What to check |
|---|---|
| Camera page says it needs HTTPS | Open the `https://…:8443` address, not `http://`. |
| Realtime check fails | WLED Sync settings: *Receive UDP realtime* on. Nothing else (Home Assistant, xLights, another sender) should be streaming to WLED. |
| Many rejected frames, wait time keeps growing | Weak WiFi. Put the phone on 5 GHz if possible, and move the router or the ESP32 closer. |
| "The picture keeps moving" | Check the tripod. Let the tree settle after turning it. Keep people and pets out of the picture. |
| Few LEDs measured | Add views, especially from directions the guidance suggests. Dim the room. Make sure the whole tree is in the picture. LEDs deep inside the branches may only be seen from one side; they're placed from the wiring and marked *single view* or *estimated*. |
| "LEDs N–M were never seen" | WLED's LED count may be larger than the string, or those LEDs aren't connected. |
| Map looks upside down or tilted | The report lists which "up" estimates disagree. Re-solve after adding views. |
