#!/usr/bin/env python3
"""
config.py -- configuration files for the Still Monitor service.

Three files with clear ownership, rather than overloading crop.json (which is
the proven tuner/logger contract):

    crop.json      tuning: crop box, top_trim, threshold, brightness,
                   contrast, and the capture resolution those coords belong to
    settings.json  run preferences: interval, duration, decimals, alert
                   thresholds, note buttons, target temperature
    secrets.json   salted password hash (gitignored, chmod 600)
    run_state.json active-run recovery state (gitignored)

All writes are atomic (temp file + os.replace) so a power cut can't leave a
truncated config behind -- which matters on a box whose whole point is
surviving power cuts.

Legacy-Python 3.5 compatible: no f-strings.
"""

import json
import os
import tempfile
import datetime


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CROP_PATH = os.path.join(BASE_DIR, "crop.json")
SETTINGS_PATH = os.path.join(BASE_DIR, "settings.json")
SECRETS_PATH = os.path.join(BASE_DIR, "secrets.json")
RUN_STATE_PATH = os.path.join(BASE_DIR, "run_state.json")


# Capture resolution presets. Crop coordinates live in this space, so changing
# resolution rescales the crop (see rescale_crop). Higher resolution means
# slower captures on a Pi 3, which raises the usable minimum interval -- the
# proven default is 820x616 and there is no known reason to exceed it.
RESOLUTIONS = [
    (410, 308),
    (820, 616),
    (1640, 1232),
    (3280, 2464),
]

# ssocr's -t is a PERCENTAGE, 0-100. Anything outside that range is rejected
# and ssocr silently falls back to its own default of 50 -- so a "threshold" of
# 130 or 227 was never higher than one of 50, it *was* 50. This cost real
# debugging time, because the control looked like it was doing something.
SSOCR_THRESHOLD_MAX = 100
SSOCR_THRESHOLD_DEFAULT = 50

CROP_DEFAULTS = {
    "x": 330, "y": 300, "w": 200, "h": 120,
    "top_trim": 0,        # px shaved off the top, to cut a glare bridge
    "threshold": SSOCR_THRESHOLD_DEFAULT,   # percent, see above
    "brightness": 0,      # -100..100, applied in software
    "contrast": 0,        # -100..100, applied in software
    "cap_w": 820,         # the resolution the coords above belong to
    "cap_h": 616,
}

CROP_INT_KEYS = ("x", "y", "w", "h", "top_trim",
                 "threshold", "brightness", "contrast", "cap_w", "cap_h")

# Starting presets for the six quick-note buttons. Every field is editable in
# the settings page -- these are only what ships.
DEFAULT_NOTE_BUTTONS = [
    {"label": "First drops", "text": "First drops", "enabled": True},
    {"label": "Hearts",      "text": "Hearts cut",  "enabled": True},
    {"label": "Tails",       "text": "Tails cut",   "enabled": True},
    {"label": "Button 4",    "text": "",            "enabled": False},
    {"label": "Button 5",    "text": "",            "enabled": False},
    {"label": "Button 6",    "text": "",            "enabled": False},
]

NOTE_BUTTON_COUNT = 6

SETTINGS_DEFAULTS = {
    # run
    "interval": 20,             # seconds between readings (a floor, not a sleep)
    "duration_hours": 0,        # 0 = run until stopped
    "decimals": 1,              # output formatting; we place the dot, not ssocr
    "num_digits": 3,            # display always shows 3; never use ssocr -d -1
    "temp_min": -40.0,          # range gate: outside this is a misread
    "temp_max": 120.0,

    # chart
    "target_temp": 78.3,        # horizontal marker line; 0 = off

    # alerts (see BUILD_DECISIONS.md section 9)
    "alert_delta": 5.0,         # |dT| between consecutive readings
    "alert_misreads": 3,        # consecutive misreads before warning
    "alert_rise": 1.0,          # rise above a plateau = process finished
    "plateau_window": 10,       # readings that must be flat to call a plateau
    "plateau_tolerance": 0.3,   # max spread across that window, degrees

    # ui
    "refresh_seconds": 60,      # control panel auto-refresh while logging
    "rate_window": 5,           # readings averaged for the degrees/min figure

    "note_buttons": DEFAULT_NOTE_BUTTONS,
}


# ------------------------------------------------------------------ helpers

def _atomic_write_json(path, payload):
    """Write JSON via a temp file in the same directory, then rename.

    sort_keys matters on 3.5: plain dicts are unordered there, so without it
    the file churns between writes for no reason.
    """
    directory = os.path.dirname(path) or "."
    handle, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(handle, "w") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _read_json(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path) as fh:
            return json.load(fh)
    except (ValueError, OSError) as exc:
        print("config: could not read {0}: {1}".format(path, exc))
        return None


def _coerce_int(value, fallback):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return fallback


def _coerce_float(value, fallback):
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


# --------------------------------------------------------------------- crop

def load_crop():
    """Tuning settings, defaults filled in for anything missing."""
    crop = dict(CROP_DEFAULTS)
    saved = _read_json(CROP_PATH)
    if saved:
        for key in CROP_INT_KEYS:
            if key in saved:
                crop[key] = _coerce_int(saved[key], crop[key])
    return normalise_threshold(crop)


def normalise_threshold(crop):
    """Bring an out-of-range threshold in line with what ssocr actually did.

    Existing tunings hold values like 130 or 227 because the slider used to
    allow them. ssocr rejected those and used 50, so rewriting them to 50 is
    behaviour-preserving -- the thresholded image is byte-identical. Clamping
    to 100 instead would silently CHANGE the tuning, and 100 is inside a band
    where this display fails.
    """
    value = int(crop.get("threshold", SSOCR_THRESHOLD_DEFAULT))
    if value > SSOCR_THRESHOLD_MAX or value < 0:
        crop["threshold"] = SSOCR_THRESHOLD_DEFAULT
    return crop


def save_crop(crop):
    """Persist tuning.

    Writes the derived `geometry` string (top_trim baked in) alongside the raw
    slider values. The geometry is what the capture pipeline consumes; the raw
    x/y/w/h are what the tuner reloads, so re-applying top_trim after a refresh
    doesn't double-trim.
    """
    from capture import effective_crop   # local import: avoids a cycle

    payload = {}
    for key in CROP_INT_KEYS:
        payload[key] = _coerce_int(crop.get(key), CROP_DEFAULTS[key])
    normalise_threshold(payload)
    geometry, _ = effective_crop(payload)
    payload["geometry"] = geometry
    payload["saved"] = datetime.datetime.now().isoformat()
    _atomic_write_json(CROP_PATH, payload)
    return payload


def rescale_crop(crop, new_w, new_h):
    """Move the crop box to a new capture resolution.

    Crop coordinates are in capture space, so changing resolution would
    otherwise invalidate the tuning entirely. Scaling by the resolution ratio
    keeps the box over the same part of the scene.

    The threshold is deliberately left alone -- it's a luminance cut and does
    not scale. Sharpness and noise do change between resolutions though, so the
    reading should be re-checked after a switch.
    """
    old_w = int(crop.get("cap_w", 820)) or 820
    old_h = int(crop.get("cap_h", 616)) or 616
    new_w = int(new_w)
    new_h = int(new_h)
    if (old_w, old_h) == (new_w, new_h):
        return dict(crop)

    sx = float(new_w) / old_w
    sy = float(new_h) / old_h
    scaled = dict(crop)
    scaled["x"] = int(round(crop["x"] * sx))
    scaled["y"] = int(round(crop["y"] * sy))
    scaled["w"] = max(20, int(round(crop["w"] * sx)))
    scaled["h"] = max(20, int(round(crop["h"] * sy)))
    scaled["top_trim"] = int(round(crop.get("top_trim", 0) * sy))
    scaled["cap_w"] = new_w
    scaled["cap_h"] = new_h

    # keep the box inside the frame after rounding
    scaled["w"] = min(scaled["w"], new_w)
    scaled["h"] = min(scaled["h"], new_h)
    scaled["x"] = max(0, min(scaled["x"], new_w - scaled["w"]))
    scaled["y"] = max(0, min(scaled["y"], new_h - scaled["h"]))
    scaled["top_trim"] = max(0, min(scaled["top_trim"], scaled["h"] - 1))
    return scaled


# ----------------------------------------------------------------- settings

def _clean_note_buttons(raw):
    """Always return exactly NOTE_BUTTON_COUNT well-formed buttons."""
    buttons = []
    raw = raw if isinstance(raw, list) else []
    for index in range(NOTE_BUTTON_COUNT):
        default = DEFAULT_NOTE_BUTTONS[index]
        item = raw[index] if index < len(raw) and isinstance(raw[index], dict) else {}
        label = str(item.get("label", default["label"]))[:24].strip()
        text = str(item.get("text", default["text"]))[:120].strip()
        enabled = bool(item.get("enabled", default["enabled"]))
        # a button with nothing to log is useless; treat it as off
        if not text:
            enabled = False
        buttons.append({"label": label or default["label"],
                        "text": text,
                        "enabled": enabled})
    return buttons


def load_settings():
    settings = dict(SETTINGS_DEFAULTS)
    settings["note_buttons"] = [dict(b) for b in DEFAULT_NOTE_BUTTONS]
    saved = _read_json(SETTINGS_PATH)
    if not saved:
        return settings

    for key in ("interval", "duration_hours", "decimals", "num_digits",
                "alert_misreads", "plateau_window", "refresh_seconds",
                "rate_window"):
        if key in saved:
            settings[key] = _coerce_int(saved[key], settings[key])
    for key in ("temp_min", "temp_max", "target_temp", "alert_delta",
                "alert_rise", "plateau_tolerance"):
        if key in saved:
            settings[key] = _coerce_float(saved[key], settings[key])
    settings["note_buttons"] = _clean_note_buttons(saved.get("note_buttons"))
    return validate_settings(settings)


def validate_settings(settings):
    """Clamp everything to a range that can't wedge the service."""
    settings["interval"] = max(1, min(3600, settings["interval"]))
    settings["duration_hours"] = max(0, min(72, settings["duration_hours"]))
    settings["decimals"] = max(0, min(2, settings["decimals"]))
    settings["num_digits"] = max(1, min(6, settings["num_digits"]))
    settings["alert_misreads"] = max(1, min(100, settings["alert_misreads"]))
    settings["plateau_window"] = max(3, min(200, settings["plateau_window"]))
    settings["rate_window"] = max(2, min(100, settings["rate_window"]))
    # a refresh faster than the capture interval just burns Pi 3 cycles
    settings["refresh_seconds"] = max(5, min(600, settings["refresh_seconds"]))
    if settings["temp_min"] >= settings["temp_max"]:
        settings["temp_min"] = SETTINGS_DEFAULTS["temp_min"]
        settings["temp_max"] = SETTINGS_DEFAULTS["temp_max"]
    settings["note_buttons"] = _clean_note_buttons(settings.get("note_buttons"))
    return settings


def save_settings(settings):
    payload = validate_settings(dict(settings))
    payload["saved"] = datetime.datetime.now().isoformat()
    _atomic_write_json(SETTINGS_PATH, payload)
    return payload


# ---------------------------------------------------------------- run state

def load_run_state():
    return _read_json(RUN_STATE_PATH)


def save_run_state(state):
    """Written on start and stop ONLY -- never per reading, to spare the SD
    card. This is what lets a run resume after a power cut."""
    _atomic_write_json(RUN_STATE_PATH, state)


def clear_run_state():
    if os.path.exists(RUN_STATE_PATH):
        os.unlink(RUN_STATE_PATH)


# ------------------------------------------------------------------ secrets

def load_secrets():
    return _read_json(SECRETS_PATH) or {}


def save_secrets(secrets):
    _atomic_write_json(SECRETS_PATH, secrets)
    try:
        os.chmod(SECRETS_PATH, 0o600)
    except OSError:
        pass
