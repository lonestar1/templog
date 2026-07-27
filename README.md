# Still Monitor

Reads the temperature off a 7-segment LED thermostat display **optically** — a Raspberry Pi
camera points at the readout, the image is cropped and thresholded, and
[`ssocr`](https://github.com/auerswal/ssocr) decodes the digits. Readings are logged to a
timestamped CSV and served through a browser control panel, for monitoring a still boiler run
over several hours.

The display can't be tapped electrically, hence the camera. It turns out to be a perfectly good
sensor: a validated run produced 216 readings with zero misreads, and later runs have held an
exact 30.00 s cadence across service restarts.

<!-- Photos: drop them in images/ and link them here, e.g.
     ![The camera rig](images/rig.jpg)
     ![The control panel](images/panel.png) -->

## What it does

- **Tunes** the crop box, threshold and brightness/contrast live in the browser, with a
  three-panel view of what the camera sees, what gets cropped, and what `ssocr` actually reads
- **Logs** on an absolute-time schedule to `temps_YYYYMMDDHHMM.csv`
- **Annotates** runs with notes that capture a reading *immediately* rather than waiting for the
  next interval, including six configurable one-tap buttons
- **Charts** each run as a self-contained HTML file with note markers, a target line and the
  detected plateau
- **Watches** run health: consecutive-misread alerts, sudden-jump alerts, rate of change in
  °C/min, and a "process likely finished" alert when the temperature rises off a plateau
- **Survives** power cuts — systemd restarts the service and the run resumes into the same CSV,
  on the original schedule grid

## How it works

Each reading:

1. `raspistill -o frame.jpg -w 820 -h 616 -t 500 -n` — auto-exposure, half resolution
2. `convert frame.jpg -crop {geometry} +repage -brightness-contrast {b}x{c} crop.png`
3. `ssocr -d 3 -t {threshold} make_mono invert crop.png` → e.g. `165`
4. Strip dots, insert the decimal point manually (`165` → `16.5`), then range-gate the result

Crop coordinates live in capture-resolution space. Changing the resolution from the panel
rescales the crop box automatically so the tuning survives, but the reading is worth
re-checking afterwards — sharpness and noise differ between resolutions.

## Requirements

Built against a deliberately old stack, because that's what the hardware is:

| | |
|---|---|
| Board | Raspberry Pi 3 (armv7l) |
| Camera | Raspberry Pi Camera Module v2 (Sony IMX219) |
| OS | Raspbian Stretch (Debian 9) |
| Python | **3.5** — no f-strings, no `subprocess.run(capture_output=…)`, no `ThreadingHTTPServer` |
| Camera stack | legacy `raspistill` (`start_x=1`, `gpu_mem=128`), not `libcamera` |
| Image tools | ImageMagick 6 (`convert`), `ssocr` ≥ 2.25 |

No Python packages are required beyond the standard library — no matplotlib, no web framework,
no JavaScript dependencies. That's deliberate: the target is an end-of-life OS where installing
anything is a small adventure.

## Configuring the Pi

All of this is done once, before installing the service.

**Enable the camera and SSH.** `sudo raspi-config` → *Interface Options* → enable **Camera** and
**SSH**, then reboot. On older OS versions that camera setting writes these to `/boot/config.txt`:

```
start_x=1
gpu_mem=128
```

Both are required for the legacy `raspistill` stack. Verify the camera works before going
further:

```sh
raspistill -o test.jpg -t 1000 -n
```

If that produces a black or empty file, fix it before touching this project — nothing downstream
can work without it.

**Set the timezone and confirm NTP.** A Pi has no real-time clock, so every timestamp in your
data comes from network time. If it boots without a network, timestamps will be wrong until NTP
syncs.

```sh
sudo raspi-config     # Localisation Options -> Timezone
timedatectl           # expect "NTP synchronized: yes"
```

**Give it a stable address.** The panel is reached over the LAN, so either set a DHCP
reservation on your router or rely on mDNS (`<hostname>.local`, provided by `avahi-daemon`,
which is installed and running by default). Set a memorable hostname with
`sudo hostnamectl set-hostname <name>`.

**Point the camera.** Fixed focus, so distance matters more than anything: fill a good part of
the frame with the digits, keep the sensor square to the display, and control reflections — a
glare streak across the digit tops is the single most common cause of misreads. Getting the rig
physically stable matters more than any software setting, because the crop box is tied to where
the display sits in the frame. Nudge the camera and you re-tune.

## Running on newer Raspberry Pi OS

This was built on Raspbian Stretch with Python 3.5 because that's the hardware it runs on, and
the code is held to 3.5 syntax throughout. **Nothing here depends on Python being old** — it's
all standard library, so it should run unmodified on any Python 3.5+, including current
versions.

The one genuine incompatibility is the camera. Bullseye and later replace `raspistill` with
`libcamera-still` / `rpicam-still`, and Bookworm drops the legacy stack entirely. The capture
calls are deliberately isolated in **`PiBackend` in `capture.py`** — three small methods — so a
port means changing that class and nothing else:

```python
# roughly, for rpicam-still / libcamera-still
["rpicam-still", "-o", dest, "--width", str(w), "--height", str(h),
 "-t", "500", "-n"]
```

Also watch for **ImageMagick 7**, where `convert` is deprecated in favour of `magick` — that
affects the two `convert` calls in the same file.

This is untested on newer versions; I have no newer Pi to try it on. If you port it, the sim
mode (`--sim`) lets you check everything except the camera path on any machine.

## Installing

### 1. Dependencies

```sh
sudo apt-get install -y imagemagick build-essential libimlib2-dev git
```

On end-of-life Raspbian Stretch the default apt repos return 404. Point `/etc/apt/sources.list`
at the archive first:

```
deb http://legacy.raspbian.org/raspbian/ stretch main contrib non-free rpi
```

If apt complains about "valid until", add `-o Acquire::Check-Valid-Until=false`.

### 2. Build ssocr

`ssocr` isn't packaged; it's built from source, so it won't survive an SD card reflash unless
you repeat this:

```sh
git clone https://github.com/auerswal/ssocr.git
cd ssocr
make
sudo make install          # installs to /usr/local/bin/ssocr
ssocr --version            # expect 2.25.1 or newer
```

### 3. The service

```sh
git clone <this-repo> ~/Documents/templog
cd ~/Documents/templog
python3 stillmon.py                     # test it in the foreground first
```

Open `http://<pi-address>:8001/`. Once it works, install the systemd unit so it starts at boot
and restarts on crash:

```sh
sudo cp stillmon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now stillmon
journalctl -u stillmon -f
```

The unit assumes the project lives at `/home/pi/Documents/templog` and runs as user `pi`. Edit
`WorkingDirectory`, `ExecStart` and `User` if yours differs.

If `avahi-daemon` is running (it is by default on Raspberry Pi OS), the panel is reachable at
`http://<hostname>.local:8001/` — no IP needed. `sudo hostnamectl set-hostname <name>` changes
that name.

## Using it

The panel has two tabs. **Monitor** is what you watch during a run; **Setup & tuning** holds
everything you configure beforehand and is locked while logging, because the camera can't tune
and log at the same time.

**Tune first.** On Setup, grab a frame and drag the crop box over the digits. Raise the
threshold until only the digits survive — glare is dimmer than the LEDs, so a high cut drops it.
Use top-trim if a reflection bridges the tops of the digits. Save when the reading is right.

**Then run.** Set interval and duration on Setup (duration 0 = until stopped), switch to
Monitor, press Start. Add notes as events happen — "first drops", "hearts", "tails" ship as
one-tap buttons and every button is editable, including what text it writes to the log.

**Afterwards.** Each run appears in the runs list with its reading and note counts. View the
chart live at any time, save it to disk, download the CSV, resume a run that was stopped too
early, or delete it. The chart is auto-saved once when a run stops.

## Configuration

| File | Holds | Written by |
|---|---|---|
| `crop.json` | Tuning: crop box, top-trim, threshold, brightness, contrast, capture resolution | Save on the tuning panel |
| `settings.json` | Interval, duration, decimals, alert thresholds, note buttons, target line | Save on the settings panel |
| `run_state.json` | Present only while a run is active; drives crash recovery | The service, on start/stop |

`settings.json` and `run_state.json` are gitignored — they're per-machine state. `crop.json` is
committed as an example, but it belongs to one specific camera position and **you will need to
re-tune**. Deploying never overwrites either file on the Pi.

Writes are kept light: the CSV append is the only frequent one. Charts render in memory and only
touch the disk when you ask, or once when a run ends.

## Data format

`timestamp,raw,value,note`

- `timestamp` — ISO 8601, e.g. `2026-07-26T19:49:14.948236`
- `raw` — exact `ssocr` output, kept for debugging; empty on a misread
- `value` — parsed float as a string, e.g. `16.5`; empty on a misread or an out-of-range read
- `note` — free text, empty on normal readings

A reading is valid only if it has exactly the expected digit count and falls inside
`temp_min`…`temp_max` (default −40…120 °C). Anything else is written with a blank `value`, so
misreads are visible in the data rather than silently dropped. Rows with a blank `value` are
excluded from the chart.

Note that `decimals` is applied *before* the range gate: on a display reading `16.5`, choosing 0
decimals yields `165 °C`, which is out of range and logged as a misread.

## Development

The capture path needs `raspistill`, ImageMagick and `ssocr`, none of which exist on a typical
dev machine. There's a simulator for everything else:

```sh
python3 stillmon.py --sim              # http://localhost:8001/
python3 stillmon.py --sim --sim-speed 60 --sim-misread-every 20
```

It replays a stored frame and synthesises a scripted still run — ambient, ramp, plateau, then
the rise that means the process is finished — which is what exercises plateau detection and the
alert thresholds. **Tuning behaviour is not simulated**: all three panels show the same
unprocessed frame, because there's no image tooling to run. Camera and OCR behaviour must always
be verified on the Pi.

Deploying:

```sh
./deploy.sh                    # syntax gate, sanity checks, copy, restart
./deploy.sh --check            # checks only
./deploy.sh --install-service  # also install/refresh the systemd unit
./deploy.sh --force            # deploy under a live run; it resumes afterwards
```

Set `STILLMON_PI` to point at your Pi (default `pi@raspberrypi.local`). The script copies code
only — never tuning, settings or run data — refuses to interrupt a live run unless forced, gates
every deploy on the Pi's own Python 3.5 (a modern Python accepts f-strings that die there), and
checks the control panel's inline handlers resolve.

## Gotchas

Hard-won, and worth knowing before changing anything:

- **`raspistill -ex off` produces a black frame** on this firmware — it disables auto-exposure
  and zeroes sensor gain. `-ev` and `-ISO` are overridden by auto-exposure too. Rely on
  auto-exposure and do *all* image adjustment in software.
- **Use `ssocr -d 3`, not `-d -1`.** Auto-count invents phantom `1`s from noise and returns long
  garbage strings; a fixed digit count fails cleanly instead.
- **`invert` is required** — the display is bright-on-dark and `ssocr` wants dark-on-light.
- **The decimal point is inserted by us, not read by `ssocr`** — its dot detection is unreliable
  on a multiplexed display.
- **Glare can bridge the tops of the digits** and merge them into one. Fix it with a high
  threshold plus top-trim, both in software.
- **Anything at the edge of the crop counts as a digit** — a status LED or a `°C` symbol will be
  read as a fourth digit and fail the whole reading. Crop tight.
- **Resolution barely affects capture time** (measured 1.38 / 1.37 / 1.50 s at 820×616,
  1640×1232 and 3280×2464). The cost is the settle time, not the pixel count.
- **The panel's JavaScript lives inside a Python string.** It must stay a raw string (`r"""`) or
  Python eats escapes meant for the browser, and one bad escape silently kills every handler on
  the page.

## Repo layout

```
stillmon.py    the service: HTTP server, control panel, routing
capture.py     capture + OCR pipeline, behind a backend interface (real / simulated)
runner.py      the logging engine: scheduling, notes, alerts, crash recovery
chart.py       hand-built SVG chart and self-contained HTML output
config.py      the three config files, with atomic writes
deploy.sh      deploy to the Pi, with a Python 3.5 gate and sanity checks
stillmon.service   systemd unit
sim/           a real captured frame, used by --sim
tuner.py       superseded by stillmon.py — kept for reference
logger.py      superseded by runner.py — kept for reference
```
