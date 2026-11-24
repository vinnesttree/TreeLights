# TreeLights

TreeLights finds the 3D position of every addressable LED on a Christmas tree using an Android phone's camera in Chrome. You walk once around the tree with the camera running while every LED blinks its own index; the phone's orientation sensor tells which side of the tree each frame sees. The LEDs are driven through stock WLED, and everything runs in one Docker container on a Linux computer on your network.

The result is a 3D map of the tree. The map stores the wiring order, a per-LED uncertainty and a surface model. The **Effects** page uses it: 3D volumetric effects that fill the tree's volume, and patterns painted on its surface. Colors, palettes and sliders work much like WLED's.

The design, including a failure analysis and how each failure is handled, is in [`architecture.md`](architecture.md).

> **Status:** everything is built and tested in a closed-loop simulator. The simulator includes a virtual WLED reached over real DDP/UDP, a virtual phone walking around the tree over WebSockets (with hand jitter, motion blur, sensor drift and noise, dropped frames), packet loss, camera bumps, reflections and recorded videos. It has **not yet been run against a real tree, ESP32 or phone**. Expect some tuning on first use (§18 step 11 in the architecture).

## What you need

- **Linux host with Docker.** It runs the container with host networking.
- **ESP32 running WLED 0.14 or newer**, driving the LED string. TreeLights doesn't modify the firmware.
- **Android phone with Chrome.** Room to walk around the tree, about 1.5–2 m away from it.
- **Optional, for the tripod mode only:** a tripod, and a second device to start each view.

## Install

```sh
git clone <this repository> treelights && cd treelights
docker compose -f docker/docker-compose.yml up -d --build
docker compose -f docker/docker-compose.yml logs treelights   # shows the URL and a QR code
```

- **Raspberry Pi 4/5 (arm64):** the same commands build natively on the Pi. To build both architectures elsewhere, use `docker/build-multiarch.sh` (needs Docker buildx with QEMU emulation).
- The app listens on **https://&lt;host&gt;:8443**. A plain-HTTP setup page with a QR code and help is on **http://&lt;host&gt;:8080**.
- **Other ports:** set `HTTPS_PORT` and `HTTP_PORT`, in a `.env` file next to the compose file or in the environment when running `docker compose`. Without Docker, use `--https-port` / `--http-port`, or `TREELIGHTS_HTTPS_PORT` / `TREELIGHTS_HTTP_PORT`.
- Data is kept in `docker/data/`: config, certificates, scans and maps.
- **Firewall:** if the host runs one, allow TCP 8443 and 8080 from the LAN, and UDP 5353 for mDNS discovery.
- **Certificate warning:** Chrome warns about the certificate because it comes from a local authority the container creates.
  - Tap **Advanced → Proceed**. Chrome asks when the page is opened, never per camera frame.
  - To get rid of the warning entirely, install the CA certificate from the setup page on the phone. This is optional.

### Running with Dockge

[`deploy/dockge/compose.yaml`](deploy/dockge/compose.yaml) is a complete stack. It pulls the ready-built image `ghcr.io/vinnesttree/treelights:latest`, which GitHub builds from `main` on every push (for amd64 and arm64), so nothing needs cloning or building on the server.

1. **Install Dockge** if you don't have it (from Dockge's README):
   ```sh
   mkdir -p /opt/stacks /opt/dockge && cd /opt/dockge
   curl https://raw.githubusercontent.com/louislam/dockge/master/compose.yaml --output compose.yaml
   docker compose up -d
   ```
   Then open `http://<host>:5001`.
2. **Create the stack.** In Dockge, click **+ Compose**, name it `treelights`, and replace the editor's contents with [`deploy/dockge/compose.yaml`](deploy/dockge/compose.yaml).
   - **Choose the ports** before deploying: in the file, change the numbers in `${HTTPS_PORT:-8443}` (the app) and `${HTTP_PORT:-8080}` (the setup page), for example to `${HTTPS_PORT:-9443}` and `${HTTP_PORT:-9080}`. Or set `HTTPS_PORT=9443` and `HTTP_PORT=9080` in a `.env` file next to the compose file.
   - Alternatively, save the file as `/opt/stacks/treelights/compose.yaml` and choose **Scan Stacks Folder** from the menu at the top right.
3. **Deploy.** Dockge pulls the image (a few hundred MB the first time), then shows the container's health.
4. **Open it.** The setup page is at **http://&lt;host&gt;:&lt;HTTP_PORT&gt;** (QR code and help), and the app is at **https://&lt;host&gt;:&lt;HTTPS_PORT&gt;** (8080 and 8443 unless changed). Continue with *WLED settings* and *Mapping the tree* below.

**Notes:**
- **Data** is stored next to the stack's compose file, in `/opt/stacks/treelights/data`: config, certificates, scans and maps. Back up that folder to keep your maps.
- **Updating:** redeploy the stack. Because of `pull_policy: always`, every `docker compose up` fetches the latest image. GitHub builds a new one a few minutes after each push to `main` (*Actions* tab, *Container image*).
  - To stay on a release, use its tag instead of `latest`, for example `ghcr.io/vinnesttree/treelights:0.1.0`.
  - To build on the server instead, for example to try a branch, the file has the lines to swap in.
- **"denied" or "unauthorized" when pulling:** GitHub publishes a new image as private at first. Make it public once: on GitHub, open the repository's *Packages* → *treelights* → *Package settings* → *Change visibility* → *Public*.
- **No `ports:` section.** The stack uses host networking, which mDNS discovery of WLED needs, so the app listens directly on the host's `HTTPS_PORT` and `HTTP_PORT`. Pick ports nothing else on the host uses: if one is taken, the container log names the port and how to change it. Allow both through the firewall from the LAN, plus UDP 5353. Host networking needs a Linux Docker host.
- **Raspberry Pi 4/5:** the image is published for arm64 too; the same stack works.
- **Phone can't connect** (several network interfaces or a VPN on the host): uncomment `TREELIGHTS_HOST_IPS` in the file, set it to the host's LAN address, and redeploy.

## WLED settings

- **Sync settings:** turn on *Receive UDP realtime*. TreeLights sends DDP to port 4048. Keep the realtime timeout at the default (about 2.5 s).
- **LED count:** set it to match the string. The Setup page compares it with TreeLights' setting.
- **Color order and outputs:** TreeLights checks the color order with the camera and compensates. It reads the GPIO outputs from WLED and treats output boundaries as breaks in the chain.

## Mapping the tree

1. **Setup.**
   - Find or enter the controller and run the checks.
   - Enter the **tree height in cm, from the lowest light to the highest light**. It only sets the map's units.
   - Optionally run *Index chase* to confirm the whole string lights.
2. **Scan.** Open the **Scan** page on the phone.
   1. Dim the room. Stand far enough back that the whole tree fits in the picture.
   2. Tap *Start* and allow the camera and motion sensors. All LEDs start blinking.
   3. Walk slowly, at an even pace, once around the tree (about a minute), holding the phone upright with the tree in the middle of the picture. The ring fills in as you go and turns green where enough LEDs were identified. Heed the warnings: *slow down*, *step back*, *point at the tree*.
   4. Back at the start, tap *Stop*. The recording uploads (it resumes if interrupted) and the map is built from the recording together with the frames streamed live.
3. **Map.** Review the 3D map and its quality report.
   - Tap an LED to blink it on the real tree.
   - Fix problems by marking an LED bad, moving it, or adding or removing a chain break, then re-solve.
   - **Link limits:** tap an LED to see its link to the next LED (length, allowed maximum, kind) and set your own bound for it. The *Link limits* card shows the learned spacing and sets a cap that no link may exceed.
   - *Activate* the version you want. The previous one stays available for roll-back.
   - **Recordings:** run old walks through the current version. *Decode again* re-reads a walk's stored recording and live frames; *Solve again* re-runs the 3D solve on the LEDs decoded before. Both make a new map version. Raw recordings are kept for the 2 most recent scans and the active map's scan; to keep more, raise `raw_frame_retention_scans` (`curl -k -X PUT https://<host>:<port>/api/config -H 'Content-Type: application/json' -d '{"raw_frame_retention_scans": 10}'`).
   - **Re-scan** weak spots: choose LEDs (or *All low-confidence*), *Show on tree* to see where they are, then *Set up re-scan*. For a walk scan, walk again past them; for a tripod scan, start the re-scan view from Control. The result is solved with the original capture into a new version.
4. **Test.** Run the sweep test and check it looks clean on the tree. *Status colors* shows which LEDs were measured (green), placed from one view (yellow), estimated (orange) or are unknown (off).

## Effects

The **Effects** page runs effects on the tree. It works like WLED's: choose an effect, set its sliders, pick up to three colors and a palette. Changes apply live while the effect runs, and the 3D preview shows what the LEDs show.

- **3D volumetric** (needs a map). Shapes and fields fill the tree's volume, using every LED's 3D position, inner LEDs included: Plane Sweep, Lighthouse, Ripples 3D, Plasma 3D, Noise 3D, Fire 3D, Snowfall, Fireworks 3D, Orbs, Helix 3D, Vortex, Rotating Cube.
- **Surface patterns** (needs a map). Patterns are painted on the tree's outer surface, from the map's surface model: Candy Stripes, Rings, Waves, Zig-Zag, Checkerboard, Polka Dots, Argyle, Garland, Marble.
  - The patterns join up seamlessly all the way round the tree.
  - Checks and dots keep their shape from the bottom of the tree to the top.
  - *Depth fade* dims LEDs inside the tree, so only the outer shell shows the pattern.
- **Inspired by Matt Parker's tree** (Stand-up Maths, [2020](https://github.com/standupmaths/xmastree2020) and [2021](https://github.com/standupmaths/xmastree2021)).
  - The animations made for his tree were shared as pre-rendered frames for his own 500 LEDs, so they can't play on another tree. These are new versions of the same ideas, computed live from your map and adjustable like the others.
  - Effects (needs a map): Two-Tone Spin (his 2020 animation: a plane spinning through the tree, a different color on each side), Bouncing Balls (balls drop, fall under gravity and bounce to rest), Explosions, Rainbow Explosion, Rainbow Implosion, Rainbow Ripple, Rainbow Spin / Corkscrew, Equalizer, Color Lerp, Particle Accelerator, Pulsing Heart, Santa Hat.
  - Morse Xmas (flashes MERRY XMAS in Morse code) needs no map.
- **Basic** (no map needed): Solid, Palette Flow, Twinkle.
- **Sliders:** *Speed*, plus up to four effect-specific sliders (0–255, as in WLED). Choosing an effect loads its own slider defaults and keeps your colors. Changing the speed never makes the animation jump.
- **Colors:** three slots (*Color 1*, *Background*, *Color 3*; some effects name them differently), set with a color picker or the quick colors.
- **Palettes:**
  - *Default*: the effect's own palette.
  - Gradients: the FastLED/WLED classics (Party, Cloud, Lava, Ocean, Forest, Rainbow, Rainbow Bands) and others, such as Fire, Candy Cane, Christmas, Classic Bulbs, Aurora and Ice.
  - Palettes built from your colors: *\* Color 1*, *\* Colors 1&2*, *\* Color Gradient*, *\* Colors Only*.
  - *\* Random Cycle*: blends to a new random palette every few seconds.
- **API:**
  - `GET /api/effects` lists the effects (with their sliders and defaults), the palettes and the running effect.
  - `POST /api/test/start` with `{"effect": "helix", "brightness": 0.5, "params": {"speed": 128, "intensity": 90, "c1": 70, "c2": 100, "c3": 0, "palette": "ocean", "colors": ["#ff0000", "#000000", "#000000"]}}` starts an effect.
  - `POST /api/effects/params` with `{"params": {...}, "brightness": 0.7}` changes the running effect without restarting it.

**Advanced: tripod capture.** For a tree that can be turned, or one you can't walk around (see *Advanced* on the home page):
1. *Camera calibration* (optional, once per phone): show the board on a computer screen and capture about 15 views.
2. *Calibrate* on the tripod phone: start the camera, light the tree, tap *Lock camera*.
3. For each view, turn the tree by the suggested angle (about 45°), step out of the picture and start the view from *Control* on another device (or with the 5 s countdown). Repeat for about 8 views, then *Solve map*.
4. *Video mode*: if live capture doesn't work, use *Play sequence* on the Calibrate page, record it with the phone's camera app and upload it.

## How it stays robust (short version)

- **Walking needs no clock sync.** The blinking pattern's timing is read from the frames themselves, so phone, server and WLED clocks and the camera's latency don't matter. LEDs are never off while their bits are sent, so each can be followed from frame to frame while you move.
- **The orientation sensor is checked, not trusted.** How the phone's sensor axes map to the camera is worked out from the video (every mapping is tried). The sensor is used only if one mapping clearly fits; otherwise the walk is mapped from the images alone.
- **LED identities are checked cycle by cycle.** A track that jumps to a neighbouring LED is split there, and each part must agree with its code in every 7.2 s cycle it covers.
- **Two sources.** The recording gives full-rate frames; the live stream gives feedback during the walk and is a complete fallback if the upload fails.

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
../.venv/bin/python -m treelights.sim.virtual_walker --server http://127.0.0.1:18443 --n 150    # walk around (add --ideal for no noise)
../.venv/bin/python -m treelights.sim.rig --server http://127.0.0.1:18443 --n 600 --views 8      # tripod: turning tree

# Detection and full-pipeline accuracy with branch occlusion, scattered light and needle glints:
PYTHONPATH=. ../.venv/bin/python tools/occlusion_report.py      # per-LED detection vs ground truth
PYTHONPATH=. ../.venv/bin/python tools/foliage_e2e.py 1         # capture + solve, errors in cm
PYTHONPATH=. ../.venv/bin/python tools/walk_report.py noisy 150 50   # walk-around: every stage vs ground truth (or: ideal)
```

Layout:

- `server/treelights/`
  - `wled/`: DDP, JSON API, discovery, monitor
  - `output/`: lease and keepalive thread
  - `walk/`: walk-around capture (pattern, timing, tracking, gyro calibration, decoding, session, job)
  - `capture/`: tripod live session, scan store, video mode
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
| Camera page says it needs HTTPS | Open the `https://…` address on the app port (8443 unless changed), not `http://`. |
| Container restarts; log says a port "is already in use" | Another service on the host uses that port. Set `HTTPS_PORT` / `HTTP_PORT` to free ports and redeploy. |
| Scan: "the blinking pattern isn't being picked up" | Point the camera at the tree and dim other lights. Check the Setup page's realtime check. |
| Scan: few LEDs identified on one side | Walk that side again more slowly, or start a new scan. LEDs hidden from every side are placed from the wiring and marked *estimated*. |
| "The orientation sensor didn't match the camera motion" | The map is still built from the images. Hold the phone upright and don't rotate the screen during the walk. |
| "Find controllers" finds nothing | The search uses mDNS and also sweeps the server's own /24 network for WLED. A controller on another subnet or VLAN isn't found: enter its IP address instead (shown in WLED's Wi-Fi settings or your router). |
| Realtime check fails | WLED Sync settings: *Receive UDP realtime* on. Nothing else (Home Assistant, xLights, another sender) should be streaming to WLED. |
| Many rejected frames, wait time keeps growing | Weak WiFi. Put the phone on 5 GHz if possible, and move the router or the ESP32 closer. |
| "The picture keeps moving" | Check the tripod. Let the tree settle after turning it. Keep people and pets out of the picture. |
| Few LEDs measured | Add views, especially from directions the guidance suggests. Dim the room. Make sure the whole tree is in the picture. LEDs deep inside the branches may only be seen from one side; they're placed from the wiring and marked *single view* or *estimated*. |
| "LEDs N–M were never seen" | WLED's LED count may be larger than the string, or those LEDs aren't connected. |
| "The active map was made with another WLED controller" or "LED configuration changed" | WLED's LED count, outputs or realtime settings differ from when the map was made. Change them back, or re-scan if the LEDs are now wired differently. |
| Map looks upside down or tilted | The report lists which "up" estimates disagree. Re-solve after adding views. |
