# Still Monitor (templog)

Reads the temperature off a 7-segment LED thermostat display **optically** — a Raspberry Pi
camera points at the readout, the image is cropped and thresholded, and `ssocr` decodes the
digits. Readings are logged to a timestamped CSV for monitoring a still boiler run.

The display can't be tapped electrically, hence the camera + OCR approach.

## Status

- **Capture → OCR → log pipeline: working and validated.** A 1-hour test run produced 176
  readings with zero misreads.
- **Next phase:** collapse `tuner.py` + `logger.py` into a single always-on web service
  (`stillmon.py`) under systemd — start/stop logging, run notes, and charts all from the
  browser, no SSH.

## Hardware / environment

| | |
|---|---|
| Board | Raspberry Pi 3 (armv7l) |
| Camera | Raspberry Pi Camera Module v2 (Sony IMX219) |
| OS | Raspbian Stretch (Debian 9) — **EOL** |
| Python | **3.5.3** |
| Camera stack | legacy `raspistill` / `raspivid` (not `libcamera`) |
| Pi address | `192.168.113.98`, user `pi` |
| Path on Pi | `~/Documents/templog/` |

**All code must stay Python 3.5-compatible** — no f-strings, no
`subprocess.run(capture_output=...)`, no `ThreadingHTTPServer`.

## Files

| File | Purpose |
|---|---|
| `tuner.py` | Live OCR tuner web tool (port 8001) — 3-panel view, sliders for threshold / brightness / contrast / top-trim / crop, saves `crop.json` |
| `logger.py` | Run logger — reads `crop.json`, captures every 20 s, writes `temps_YYYYMMDDHHMM.csv` |
| `crop.json` | Saved tuning. **The state file** — written by the tuner, read by the logger |

## The pipeline

Per reading:

1. `raspistill -o frame.jpg -w 820 -h 616 -t 500 -n` — auto-exposure, half res
2. `convert frame.jpg -crop {geometry} +repage -brightness-contrast {b}x{c} crop.png`
3. `ssocr -d 3 -t {threshold} make_mono invert crop.png` → e.g. `165`
4. Strip dots, insert the decimal manually (`165` → `16.5`), range-gate the result

Capture resolution is **820×616 in both the tuner and the logger** — crop coordinates live in
that space, so they must match. Change the resolution and you have to re-tune the crop.

## Running it (interim, pre-service)

Tune:

```sh
cd ~/Documents/templog && python3 tuner.py
# open http://192.168.113.98:8001/ , tune, Save settings, Ctrl-C
```

Log (survives SSH disconnect, but not a reboot):

```sh
cd ~/Documents/templog
nohup python3 logger.py > logger.out 2>&1 &
tail -f temps_*.csv     # logger.out is buffered; the CSV is the source of truth
pkill -f logger.py      # stop
```

Pull data to the Mac:

```sh
scp pi@192.168.113.98:~/Documents/templog/temps_*.csv ~/Downloads/
```

## Data format

`timestamp,raw,value` (becoming `timestamp,raw,value,note`):

- `timestamp` — ISO 8601, e.g. `2026-07-26T19:49:14.948236`
- `raw` — exact `ssocr` output, kept for debugging; empty on a misread
- `value` — parsed float as a string, e.g. `16.5`; empty on a misread or out-of-range read
- `note` — free text, empty on normal readings

Validation: exactly 3 digits, and the value must fall within −40…120 °C, or the row is logged
as a misread with a blank `value`.

## Gotchas worth knowing before you touch anything

- **`raspistill -ex off` produces a black frame** on this firmware — it zeroes sensor gain.
  `-ev` and `-ISO` are overridden by auto-exposure. Rely on auto-exposure and do *all* image
  adjustment in software.
- **Use `ssocr -d 3`, not `-d -1`.** Auto-count invents phantom `1`s from noise and returns
  garbage; a fixed count fails cleanly instead.
- **The decimal point is inserted by us, not read by `ssocr`** — its dot detection is unreliable
  on this multiplexed display.
- **Glare can bridge the tops of the digits** and merge them. Fixed with a high threshold plus a
  top-trim, both software-side.
- **Raspbian Stretch's apt repos 404.** Point `/etc/apt/sources.list` at
  `http://legacy.raspbian.org/raspbian/`.

Full context, including the web-service spec, is in `../docs/PROJECT_CONTEXT.md` (kept outside
this repo).
