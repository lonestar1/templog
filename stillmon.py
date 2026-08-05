#!/usr/bin/env python3
"""
stillmon.py -- Still Monitor service.

One always-on web service: tuning, logging, notes and run health in a single
control panel. It replaced two separate scripts, tuner.py and logger.py, which
were removed once superseded -- they remain in git history.

Run on the Pi:
    python3 stillmon.py
    # then open http://<pi-address>:8001/  (or http://<hostname>.local:8001/)

Run on a Mac for UI work (no camera, no ImageMagick, no ssocr needed):
    python3 stillmon.py --sim
    # then open http://localhost:8001/

Legacy-Python 3.5 compatible: no f-strings, no ThreadingHTTPServer (3.7+), no
subprocess capture_output. The threading server is built from ThreadingMixIn,
which 3.5 does have -- and it matters: a capture takes ~1.4s, and on a
single-threaded server that blocks every other request and the UI looks dead.
"""

import argparse
import csv
import datetime
import json
import os
import socketserver
import sys
import threading
import time

from http import server
from urllib.parse import urlparse, parse_qs

import capture
import chart
import config
import runner


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PORT = 8001

# The status endpoint reads in-memory state only -- no camera, no disk -- so
# polling it often is cheap even on a Pi 3. settings.refresh_seconds governs
# the heavier chart refresh instead.
STATUS_POLL_SECONDS = 10


class Service(object):
    """Shared state for the whole process.

    The camera, the working image files and the config are all shared mutable
    state. `camera_lock` serialises every operation that touches them -- which
    is also what stops the tuner and the logger fighting over the camera.
    """

    def __init__(self, backend):
        self.backend = backend
        self.pipeline = capture.Pipeline(BASE_DIR, backend)
        self.camera_lock = threading.Lock()
        self.crop = config.load_crop()
        self.settings = config.load_settings()
        self.runner = runner.Runner(self)

    # -- tuning ------------------------------------------------------------

    def merge_crop(self, params):
        """Overlay slider values onto the saved tuning.

        Resolution is NOT taken from the request -- it only changes through
        set_resolution, which rescales the crop box to match.
        """
        merged = dict(self.crop)
        for key in ("x", "y", "w", "h", "top_trim",
                    "threshold", "brightness", "contrast"):
            if key in params:
                try:
                    merged[key] = int(float(params[key][0]))
                except (ValueError, IndexError):
                    pass
        if "invert" in params:
            # arrives as a query string ("0"/"1") or a JSON bool
            raw = params["invert"][0]
            merged["invert"] = str(raw).lower() not in ("0", "false", "")
        return merged

    def apply_preset(self, name):
        """Load a named tuning preset and make it current.

        Presets can come from a different capture resolution, so the crop box
        is rescaled rather than landing in the wrong part of the frame.
        """
        if self.runner.running:
            raise RuntimeError("stop logging before switching preset")
        presets = config.load_presets()
        if name not in presets:
            raise ValueError("no preset named {0}".format(name))
        entry = dict(presets[name])
        crop = dict(config.CROP_DEFAULTS)
        for key in config.CROP_INT_KEYS:
            if key in entry:
                crop[key] = int(entry[key])
        for key in config.CROP_BOOL_KEYS:
            if key in entry:
                crop[key] = bool(entry[key])
        crop = config.rescale_crop(crop, self.crop["cap_w"], self.crop["cap_h"])
        with self.camera_lock:
            self.crop = config.save_crop(crop)
        return self.crop

    def set_resolution(self, width, height):
        """Switch capture resolution, carrying the tuning across.

        The crop box is rescaled proportionally so the tuning survives -- see
        config.rescale_crop. The frame on disk is now the wrong size, so it is
        dropped and the caller should grab a fresh one.
        """
        if (int(width), int(height)) not in config.RESOLUTIONS:
            raise ValueError("unsupported resolution")
        if self.runner.running:
            raise RuntimeError("stop logging before changing resolution")
        with self.camera_lock:
            self.crop = config.rescale_crop(self.crop, width, height)
            config.save_crop(self.crop)
            if os.path.exists(self.pipeline.frame):
                os.unlink(self.pipeline.frame)
        return self.crop

    # -- camera ------------------------------------------------------------

    def grab(self):
        if self.runner.running:
            raise RuntimeError("logging in progress")
        with self.camera_lock:
            return self.pipeline.grab(self.crop)

    def read(self, crop, build_panels=True):
        """Tuner path: re-process the current frame with candidate settings."""
        if self.runner.running:
            raise RuntimeError("logging in progress")
        with self.camera_lock:
            if not self.pipeline.have_frame():
                self.pipeline.grab(self.crop)
            return self.pipeline.read(crop, self.settings,
                                      build_panels=build_panels)

    def capture_reading(self):
        """Logger path: fresh frame, decode, no preview images.

        Skipping the three preview `convert` calls is what keeps a logged
        reading cheap.
        """
        with self.camera_lock:
            self.pipeline.grab(self.crop)
            return self.pipeline.read(self.crop, self.settings,
                                      build_panels=False)

    # -- samples -----------------------------------------------------------

    def mark_sample(self):
        """Record that a sample was drawn NOW; the readings follow later.

        The run's CSV is captured at marking time, so a sample drawn at 14:00
        still lands in the right file even if it is only measured at 14:40,
        after the run has ended.
        """
        if not self.runner.csv_path:
            raise RuntimeError("start a run before marking samples")

        # Capture the still temperature -- the vapour temperature at the probe,
        # which is what the camera reads -- at the moment the sample is drawn.
        # NOT the wash/boiler temperature, which is a different measurement and
        # is recorded by hand in notes. Reading it later would be wrong: by then
        # the sample has cooled and the still has moved on. This is the number
        # that gives a sample meaning, since 95% ABV off a 78 C plateau is a
        # different thing from 95% off a still running at 92 C.
        still_raw, still_value = "", ""
        try:
            reading = self.capture_reading()
            still_raw, still_value = reading["raw"], reading["value"]
        except Exception as exc:          # never lose the mark over a misread
            print("stillmon: could not read the still while marking a "
                  "sample: {0}".format(exc))

        pending = config.load_pending_samples()
        entry = {
            "id": datetime.datetime.now().isoformat(),
            "time": datetime.datetime.now().isoformat(),
            "csv": os.path.basename(self.runner.csv_path),
            "still_raw": still_raw,
            "still_value": still_value,
        }
        pending.append(entry)
        config.save_pending_samples(pending)
        return pending

    def log_sample(self, sample_id, sample_temp="", sample_abv="", note="",
                   sample_volume=""):
        """Fill in a marked sample and write it at its ORIGINAL timestamp."""
        pending = config.load_pending_samples()
        match = None
        for entry in pending:
            if entry.get("id") == sample_id:
                match = entry
                break
        if match is None:
            raise ValueError("no pending sample with that id")
        if (sample_temp == "" and sample_abv == "" and sample_volume == ""
                and not note):
            raise ValueError("enter a volume, temperature, ABV, or a note")

        path = self.run_path(match.get("csv"), (".csv",))
        if not os.path.exists(path):
            raise ValueError("the run file for that sample is gone")

        logged = runner.log_sample(path, match["time"], sample_temp,
                                   sample_abv, note,
                                   still_raw=match.get("still_raw", ""),
                                   still_value=match.get("still_value", ""),
                                   sample_volume=sample_volume)
        pending = [e for e in pending if e.get("id") != sample_id]
        config.save_pending_samples(pending)

        # if that run is still the live one, show it in the notes list now
        if self.runner.running and self.runner.csv_path == path:
            # the still reading, not blank -- the CSV has it, so the panel
            # showing "--" while the chart shows a temperature is just this
            # list disagreeing with the file
            self.runner.notes.append({"time": logged["time"],
                                      "value": logged.get("still_value", ""),
                                      "text": logged["text"]})
        return {"pending": pending, "logged": logged}

    def cancel_sample(self, sample_id):
        pending = [e for e in config.load_pending_samples()
                   if e.get("id") != sample_id]
        config.save_pending_samples(pending)
        return pending

    # -- run files ---------------------------------------------------------

    def run_path(self, name, extensions=(".csv", ".html")):
        """Resolve a run filename to a path, refusing anything else.

        These names arrive from the browser, so they get treated as hostile:
        basename only, a required prefix, and an allowed extension. That rules
        out traversal and stops the delete endpoint being pointed at, say,
        crop.json.
        """
        base = os.path.basename(name or "")
        if not base.startswith("temps_") or not base.endswith(extensions):
            raise ValueError("not a run file: {0}".format(base))
        return os.path.join(BASE_DIR, base)

    def list_runs(self):
        """Every run CSV, newest first, with the numbers needed to decide
        whether to keep it."""
        runs = []
        for name in os.listdir(BASE_DIR):
            if not (name.startswith("temps_") and name.endswith(".csv")):
                continue
            path = os.path.join(BASE_DIR, name)
            try:
                stat = os.stat(path)
                readings = 0
                notes = 0
                with open(path) as fh:
                    for row in csv.DictReader(fh):
                        if (row.get("value") or "").strip():
                            readings += 1
                        if (row.get("note") or "").strip():
                            notes += 1
            except OSError:
                continue
            html = chart.html_path_for(path)
            runs.append({
                "csv": name,
                "readings": readings,
                "notes": notes,
                "bytes": stat.st_size,
                "modified": datetime.datetime.fromtimestamp(
                    stat.st_mtime).isoformat(),
                "has_chart": os.path.exists(html),
                "active": (self.runner.running
                           and self.runner.csv_path == path),
            })
        runs.sort(key=lambda r: r["csv"], reverse=True)
        return runs

    # -- reporting ---------------------------------------------------------

    def capture_info(self):
        seconds = self.pipeline.last_capture_seconds
        return {
            "seconds": round(seconds, 2) if seconds is not None else None,
            "resolution": [int(self.crop["cap_w"]), int(self.crop["cap_h"])],
            "simulated": self.backend.simulated,
        }


# --------------------------------------------------------------------- page
# Built with __TOKEN__ placeholders rather than str.format, so CSS and JS
# braces don't all have to be doubled. That doubling was a standing bug magnet
# in the original tuner.

# RAW string, deliberately. This is JavaScript and CSS, not Python, so escape
# sequences in it are meant for the browser. As a normal string Python eats
# them first: "?\nThis cannot be undone." becomes a real newline inside a JS
# string literal (an unterminated string), and onclick="f(\'x\')" collapses to
# onclick="f('x')" which breaks the attribute. Either one is a single
# SyntaxError that stops the whole script parsing, so every handler vanishes
# and every button silently does nothing. Keep the r.
PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Still Monitor</title>
<style>
  body{font-family:system-ui,sans-serif;background:#111;color:#eee;margin:0;
       padding:8px 14px 40px;font-size:13px;}
  h2{margin:0;font-size:16px;}
  h3{margin:16px 0 6px;font-size:13px;color:#9ab;
     text-transform:uppercase;letter-spacing:.6px;}
  .top{display:flex;align-items:baseline;gap:16px;margin-bottom:6px;
       flex-wrap:wrap;}
  #readout{font-size:30px;font-variant-numeric:tabular-nums;color:#6f6;}
  #raw{font-size:12px;color:#888;}
  button{font-size:13px;padding:5px 12px;cursor:pointer;}
  button:disabled{opacity:.4;cursor:not-allowed;}
  input[type=text],input[type=number]{background:#222;color:#eee;
       border:1px solid #444;padding:4px 6px;font-size:13px;}
  select{font-size:13px;padding:4px;background:#222;color:#eee;
         border:1px solid #444;}
  #status{color:#8f8;font-size:12px;}
  #simbadge{background:#a4632a;color:#fff;padding:2px 8px;border-radius:3px;
            font-size:11px;letter-spacing:.5px;}
  .views{display:flex;gap:12px;align-items:flex-start;}
  .view{flex:1;}
  .imgwrap{position:relative;display:block;}
  .view img{width:100%;height:auto;max-height:26vh;object-fit:contain;
            border:1px solid #333;background:#000;display:block;}
  #cropbox{position:absolute;border:2px solid #33aaff;
           background:rgba(51,170,255,.10);pointer-events:none;display:none;}
  .cap{font-size:11px;color:#9ab;margin:2px 0;}
  .ctrls{margin-top:12px;max-width:1100px;}
  .ctrl{display:flex;align-items:center;gap:12px;margin:7px 0;}
  .ctrl label{font-size:12px;color:#bbb;width:210px;flex:none;}
  .ctrl input[type=range]{flex:1;min-width:250px;}
  .v{display:inline-block;width:48px;text-align:right;color:#8cf;
     font-variant-numeric:tabular-nums;flex:none;}
  .meta{margin-top:10px;font-size:11px;color:#778;}

  /* tabs: monitoring stays uncluttered, setup lives out of the way */
  .tabbar{display:flex;gap:2px;align-items:center;margin:10px 0 0;
          border-bottom:1px solid #333;}
  .tabbtn{background:#1a1a1a;color:#9ab;border:1px solid #333;
          border-bottom:none;padding:7px 16px;font-size:13px;
          border-radius:3px 3px 0 0;margin-bottom:-1px;}
  .tabbtn.active{background:#111;color:#eee;border-bottom:1px solid #111;}
  .tabbtn:disabled{opacity:.35;cursor:not-allowed;}
  #setuplocked{display:none;color:#e8a33d;font-size:11px;margin-left:10px;}
  #unsaved{display:none;color:#e8a33d;font-size:12px;}
  .tabbtn.dirty::after{content:" •";color:#e8a33d;}
  .tab{display:none;}
  .tab.active{display:block;}

  /* the tuner is inert while logging -- the camera can't do both */
  #tuner.frozen{opacity:.35;pointer-events:none;filter:grayscale(.6);}
  #frozenmsg{display:none;color:#e8a33d;font-size:12px;margin:6px 0;}
  #tuner.frozen ~ #frozenmsg{display:block;}

  .panel{border:1px solid #333;padding:10px 12px;margin-top:12px;
         background:#171717;}
  .row{display:flex;gap:14px;align-items:center;flex-wrap:wrap;}
  .stat{display:flex;flex-direction:column;gap:2px;min-width:92px;}
  .stat .k{font-size:10px;color:#889;text-transform:uppercase;
           letter-spacing:.5px;}
  .stat .val{font-size:16px;font-variant-numeric:tabular-nums;}
  .alert{padding:7px 10px;margin:6px 0;border-radius:3px;font-size:13px;}
  .alert.warn{background:#5a4415;border:1px solid #a67c1e;color:#ffd98a;}
  .alert.error{background:#5c1f1f;border:1px solid #b03636;color:#ffb3b3;}
  .notebtns{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0;}
  .notelist{margin-top:8px;max-height:150px;overflow-y:auto;font-size:12px;}
  .notelist div{padding:2px 0;border-bottom:1px solid #262626;}
  .notelist .t{color:#8cf;font-variant-numeric:tabular-nums;}
  .notelist .v{color:#6f6;width:auto;display:inline;text-align:left;}
  .settingsrow{display:flex;gap:8px;align-items:center;margin:4px 0;}
  .settingsrow input[type=text]{width:150px;}
  details summary{cursor:pointer;color:#9ab;margin-top:14px;}
  table.runs{border-collapse:collapse;font-size:12px;width:100%;
             max-width:780px;}
  table.runs td,table.runs th{text-align:left;padding:4px 10px 4px 0;
             border-bottom:1px solid #262626;}
  table.runs th{color:#889;font-size:10px;text-transform:uppercase;
             letter-spacing:.5px;}
  table.runs a{color:#8cf;}
  table.runs .live{color:#6f6;}
  table.runs button{font-size:11px;padding:2px 8px;}

  /* ---- phone layout -------------------------------------------------
     The panel is checked from a phone mid-run, so Monitor has to be usable
     one-handed: the three tuner previews stop sitting side by side, controls
     go full width, and tap targets grow. The runs table scrolls sideways
     rather than squashing its columns. */
  @media (max-width: 760px){
    body{padding:6px 10px 40px;font-size:14px;}
    .top{gap:10px;}
    #readout{font-size:38px;}
    .views{flex-direction:column;}
    .view img{max-height:34vh;}
    .ctrl{flex-wrap:wrap;gap:6px;}
    .ctrl label{width:100%;}
    .ctrl input[type=range]{min-width:0;width:100%;}
    .stat{min-width:74px;}
    .stat .val{font-size:15px;}
    button{padding:9px 14px;font-size:14px;}
    .tabbtn{flex:1;padding:11px 8px;}
    #setuplocked{display:none !important;}
    .notebtns button{flex:1 1 44%;}
    input[type=text],input[type=number],select{font-size:16px;}
    #notetext{width:100% !important;}
    .runswrap{overflow-x:auto;}
    table.runs{min-width:520px;}
    .row{gap:8px;}
  }
</style></head><body>

<div class="top">
  <h2>Still Monitor</h2>
  <span id="readout">--</span>
  <span id="raw">raw: --</span>
  <span id="status"></span>
  __SIMBADGE__
</div>

<div class="tabbar">
  <button id="tab_monitor_btn" class="tabbtn active"
    onclick="showTab('monitor')">Monitor</button>
  <button id="tab_tuning_btn" class="tabbtn"
    onclick="showTab('tuning')">Tuning</button>
  <button id="tab_settings_btn" class="tabbtn"
    onclick="showTab('settings')">Settings</button>
  <span id="setuplocked">tuning locked while logging</span>
</div>

<div id="alerts"></div>

<!-- ============================================================ MONITOR == -->
<div id="tab-monitor" class="tab active">

<div class="panel">
  <div class="row">
    <button id="startbtn" onclick="startRun()">Start logging</button>
    <button id="stopbtn" onclick="stopRun()">Stop</button>
    <span id="runplan" style="color:#778;font-size:12px"></span>
  </div>
  <div class="row" style="margin-top:10px" id="stats">
    <div class="stat"><span class="k">state</span>
      <span class="val" id="s_state">idle</span></div>
    <div class="stat"><span class="k">file</span>
      <span class="val" id="s_file" style="font-size:12px">--</span></div>
    <div class="stat"><span class="k">readings</span>
      <span class="val" id="s_count">0</span></div>
    <div class="stat"><span class="k">last</span>
      <span class="val" id="s_last">--</span></div>
    <div class="stat"><span class="k">rate</span>
      <span class="val" id="s_rate">--</span></div>
    <div class="stat"><span class="k">elapsed</span>
      <span class="val" id="s_elapsed">--</span></div>
    <div class="stat"><span class="k">next in</span>
      <span class="val" id="s_next">--</span></div>
    <div class="stat"><span class="k">misreads</span>
      <span class="val" id="s_misreads">0</span></div>
  </div>
</div>

<!-- --------------------------------------------------------------- notes -->
<div class="panel">
  <h3 style="margin-top:0">Run notes</h3>
  <div class="notebtns" id="notebtns"></div>
  <div class="row">
    <input id="notetext" type="text" placeholder="note text…" style="width:320px"
      onkeydown="if(event.key==='Enter')addNote()">
    <button id="notebtn" onclick="addNote()">Add note</button>
    <span style="color:#778;font-size:11px">writes a reading immediately,
      not at the next interval</span>
  </div>
  <div class="notelist" id="notelist"></div>
</div>

<!-- ------------------------------------------------------------- samples -->
<div class="panel">
  <h3 style="margin-top:0">Samples</h3>
  <div class="row">
    <button id="samplebtn" onclick="markSample()">Sample drawn now</button>
    <span style="color:#778;font-size:11px">marks the time; fill in the
      readings once it has cooled and it is logged at the marked time</span>
  </div>
  <div id="pendingsamples"></div>
</div>

<!-- --------------------------------------------------------------- runs -->
<div class="panel">
  <h3 style="margin-top:0">Runs
    <button style="margin-left:10px" onclick="viewChart()">View live chart</button>
    <button onclick="saveChart()">Save chart</button>
    <label style="font-size:12px;color:#bbb;font-weight:normal">
      <input id="showsystem" type="checkbox"> include service markers</label>
    <span id="chartstatus" style="color:#8f8;font-size:12px"></span>
  </h3>
  <div class="runswrap"><table class="runs" id="runs"></table></div>
</div>

</div><!-- end MONITOR -->

<!-- ============================================================= TUNING == -->
<div id="tab-tuning" class="tab">

<div class="panel">
  <h3 style="margin-top:0">Run setup</h3>
  <div class="row">
    <label>Interval <input id="interval" type="number" min="1" max="3600"
      step="1" style="width:70px" onchange="saveRunSetup()"> s</label>
    <label>Duration <input id="duration" type="number" min="0" max="72"
      step="0.5" style="width:70px" onchange="saveRunSetup()"> h
      <span style="color:#778">(0 = until stopped)</span></label>
    <label>Decimals
      <select id="decimals" onchange="setDecimals()"><option>0</option>
        <option>1</option><option>2</option></select></label>
    <label>Resolution
      <select id="resolution" onchange="setResolution()">__RESOPTIONS__</select>
    </label>
  </div>
</div>

<!-- --------------------------------------------------------------- tuner -->
<h3>Tuning</h3>
<div id="tuner">
  <div class="row" style="margin-bottom:6px">
    <button onclick="grab()">Grab new frame</button>
    <button onclick="save()">Save tuning</button>
    <span id="unsaved">unsaved changes — the logger uses the SAVED tuning</span>
    <label title="ssocr wants dark digits on a light background">
      <input id="invert" type="checkbox" onchange="setInvert()">
      Invert <span style="color:#778">(on = LED, off = LCD)</span></label>
  </div>
  <div class="row" style="margin-bottom:8px">
    <label>Preset <select id="presetsel"></select></label>
    <button onclick="loadPreset()">Load</button>
    <button onclick="savePreset()">Save as…</button>
    <button onclick="deletePreset()">Delete</button>
    <span id="presetstatus" style="color:#8f8;font-size:12px"></span>
  </div>
  <div class="views">
    <div class="view"><div class="cap">Camera frame + crop box (live)</div>
      <div class="imgwrap"><img id="disp" src="/disp.png">
        <div id="cropbox"></div></div></div>
    <div class="view"><div class="cap">Cropped + brightness/contrast (pre-threshold)</div>
      <div class="imgwrap"><img id="mid" src="/mid.png"></div></div>
    <div class="view"><div class="cap">What ssocr sees (thresholded)</div>
      <div class="imgwrap"><img id="proc" src="/proc.png"></div></div>
  </div>

  <div class="ctrls">
    <div class="ctrl"><label>Threshold % (high = drop glare)</label>
      <input id="threshold" type="range" min="0" max="100" step="1">
      <span class="v" id="threshold_v"></span></div>
    <div class="ctrl"><label>Contrast</label>
      <input id="contrast" type="range" min="-100" max="100" step="1">
      <span class="v" id="contrast_v"></span></div>
    <div class="ctrl"><label>Brightness</label>
      <input id="brightness" type="range" min="-100" max="100" step="1">
      <span class="v" id="brightness_v"></span></div>
    <div class="ctrl"><label>Top-trim (shave glare bridge)</label>
      <input id="top_trim" type="range" min="0" max="120" step="1">
      <span class="v" id="top_trim_v"></span></div>
    <div class="ctrl"><label>Crop X</label>
      <input id="x" type="range" min="0" step="1">
      <span class="v" id="x_v"></span></div>
    <div class="ctrl"><label>Crop Y</label>
      <input id="y" type="range" min="0" step="1">
      <span class="v" id="y_v"></span></div>
    <div class="ctrl"><label>Crop W</label>
      <input id="w" type="range" min="20" step="1">
      <span class="v" id="w_v"></span></div>
    <div class="ctrl"><label>Crop H</label>
      <input id="h" type="range" min="20" step="1">
      <span class="v" id="h_v"></span></div>
  </div>
</div>
<div id="frozenmsg">Tuning is frozen while logging — the camera can't do both.
  Stop the run to re-tune.</div>

<div class="meta" id="meta"></div>

</div><!-- end TUNING -->

<!-- =========================================================== SETTINGS == -->
<div id="tab-settings" class="tab">

<div class="panel">
  <h3 style="margin-top:0">Quick-note buttons</h3>
  <div id="btnsettings"></div>

  <h3>Reading validation</h3>
  <div class="row">
    <label>Reject readings changing faster than
      <input id="max_rate_per_min" type="number" step="1" style="width:70px">
      °C/min <span style="color:#778">(0 = off)</span></label>
  </div>
  <div style="color:#778;font-size:11px;margin-top:4px">
    A glare-induced misread can be perfectly plausible — a 1 read as a 7 turns
    16.2 into 76.2, which passes every other check. Physically impossible rates
    are logged as misreads instead of data.
  </div>

  <h3>Alerts</h3>
  <div class="row">
    <label>Jump warning <input id="alert_delta" type="number" step="0.1"
      style="width:70px"> °C between readings</label>
    <label>Misread warning after <input id="alert_misreads" type="number"
      step="1" style="width:60px"> in a row</label>
    <label>Rise above plateau <input id="alert_rise" type="number" step="0.1"
      style="width:60px"> °C</label>
  </div>
  <div class="row" style="margin-top:6px">
    <label>Plateau window <input id="plateau_window" type="number" step="1"
      style="width:60px"> readings</label>
    <label>Plateau tolerance <input id="plateau_tolerance" type="number"
      step="0.1" style="width:60px"> °C</label>
    <label>Target line <input id="target_temp" type="number" step="0.1"
      style="width:70px"> °C</label>
  </div>
  <div class="row" style="margin-top:10px">
    <button onclick="saveSettings()">Save settings</button>
    <span id="settingsstatus" style="color:#8f8;font-size:12px"></span>
    <span style="color:#778;font-size:11px">these stay editable during a run</span>
  </div>
</div>

</div><!-- end SETTINGS -->

<script>
var CROP = __CROP__;
var SETTINGS = __SETTINGS__;
var CAPTURE = __CAPTURE__;
var POLL_MS = __POLLMS__;
var IDS = ["threshold","contrast","brightness","top_trim","x","y","w","h"];
var RUNNING = false;

function el(id){ return document.getElementById(id); }

/* ------------------------------------------------------------------ tabs */
var TAB = "monitor";

var TABS = ["monitor","tuning","settings"];

function showTab(name){
  // Tuning is unavailable during a run: the camera can't tune and log at once,
  // and an interval or resolution change mid-run would invalidate the data.
  // Settings deliberately stays open -- alert thresholds and note buttons are
  // exactly the things you want to adjust while watching a run.
  if(name === "tuning" && RUNNING) return;
  TAB = name;
  TABS.forEach(function(t){
    el("tab-" + t).className = "tab" + (t === name ? " active" : "");
    el("tab_" + t + "_btn").className = "tabbtn" + (t === name ? " active" : "");
  });
  if(name === "tuning" && !RUNNING){ drawBox(); grab(); }
}

/* ------------------------------------------------------------ formatting */
function fmtDuration(seconds){
  if(seconds === null || seconds === undefined) return "--";
  seconds = Math.max(0, Math.round(seconds));
  var h = Math.floor(seconds/3600), m = Math.floor((seconds%3600)/60),
      s = seconds%60;
  if(h) return h + "h " + (m<10?"0":"") + m + "m";
  if(m) return m + "m " + (s<10?"0":"") + s + "s";
  return s + "s";
}

/* ----------------------------------------------------------------- tuner */
// Slider ranges depend on capture resolution, so they're set at runtime.
function applyRanges(){
  el("x").max = CROP.cap_w; el("w").max = CROP.cap_w;
  el("y").max = CROP.cap_h; el("h").max = CROP.cap_h;
  el("top_trim").max = Math.max(1, CROP.h - 1);
}

function loadValues(){
  applyRanges();
  IDS.forEach(function(id){
    el(id).value = CROP[id];
    el(id + "_v").textContent = CROP[id];
  });
  el("invert").checked = (CROP.invert !== false);
}

IDS.forEach(function(id){
  el(id).addEventListener("input", function(){
    el(id + "_v").textContent = el(id).value;
    CROP[id] = +el(id).value;
    markDirty();
    drawBox();
    schedule();
  });
});

function params(){
  var p = {};
  IDS.forEach(function(id){ p[id] = el(id).value; });
  p.invert = el("invert").checked ? 1 : 0;
  return p;
}
function qs(){
  var p = params();
  return Object.keys(p).map(function(k){ return k + "=" + p[k]; }).join("&");
}

// Draw the crop rectangle over the left image. Accounts for
// object-fit:contain letterboxing, so the box lands where the crop actually is.
function drawBox(){
  var img = el("disp"), box = el("cropbox");
  var natW = img.naturalWidth, natH = img.naturalHeight;
  if(!img.clientWidth || !natW) return;
  var cW = img.clientWidth, cH = img.clientHeight;
  var scale = Math.min(cW/natW, cH/natH);
  var offX = (cW - natW*scale)/2, offY = (cH - natH*scale)/2;
  var nx = natW/CROP.cap_w, ny = natH/CROP.cap_h;
  var p = params();
  var yTop = (+p.y) + (+p.top_trim);
  var hEff = Math.max(1, (+p.h) - (+p.top_trim));
  box.style.display = "block";
  box.style.left   = (offX + (+p.x)*nx*scale) + "px";
  box.style.top    = (offY + yTop*ny*scale) + "px";
  box.style.width  = ((+p.w)*nx*scale) + "px";
  box.style.height = (hEff*ny*scale) + "px";
}
window.addEventListener("resize", drawBox);
el("disp").addEventListener("load", drawBox);

var busy = false, pending = false, deb = null;
function schedule(){
  clearTimeout(deb);
  deb = setTimeout(function(){
    if(busy){ pending = true; return; }
    refresh();
  }, 120);
}

function showMeta(){
  var bits = ["capture " + CROP.cap_w + "x" + CROP.cap_h];
  if(CAPTURE.seconds !== null && CAPTURE.seconds !== undefined){
    bits.push("last capture " + CAPTURE.seconds.toFixed(2) + "s");
  }
  el("meta").textContent = bits.join(" · ");
}

function refresh(){
  if(RUNNING) return;              // tuner is inert while logging
  busy = true;
  el("status").textContent = "processing…";
  fetch("/read?" + qs()).then(function(r){ return r.json(); }).then(function(d){
    el("readout").textContent = d.value || "??";
    el("raw").textContent = "raw: " + (d.raw || "(none)") +
      (d.err ? ("   [" + d.err + "]") : "");
    var t = Date.now();
    el("disp").src = "/disp.png?t=" + t;
    el("mid").src  = "/mid.png?t=" + t;
    el("proc").src = "/proc.png?t=" + t;
    if(d.capture){ CAPTURE = d.capture; showMeta(); }
    el("status").textContent = "";
    busy = false;
    if(pending){ pending = false; refresh(); }
  }).catch(function(e){
    busy = false;
    el("status").textContent = "err " + e;
  });
}

function grab(){
  el("status").textContent = "grabbing frame…";
  fetch("/grab").then(function(r){ return r.json(); }).then(function(d){
    if(d.capture){ CAPTURE = d.capture; showMeta(); }
    if(d.error){ el("status").textContent = d.error; return; }
    refresh();
  }).catch(function(e){ el("status").textContent = "grab failed: " + e; });
}

function setResolution(){
  var parts = el("resolution").value.split("x");
  el("status").textContent = "switching resolution…";
  post("/resolution", {w:+parts[0], h:+parts[1]}, function(d){
    if(d.error){ el("status").textContent = d.error; return; }
    CROP = d.crop;
    SAVED_CROP = JSON.parse(JSON.stringify(d.crop));
    loadValues();
    markDirty();
    el("status").textContent = "crop rescaled — re-check the reading";
    grab();
  });
}

// Interval and duration live on the Setup tab but are used by Start on the
// Monitor tab, so they persist on change rather than at Start.
function saveRunSetup(){
  post("/settings", {interval:+el("interval").value,
                     duration_hours:+el("duration").value}, function(d){
    if(d.error){ el("settingsstatus").textContent = d.error; return; }
    SETTINGS = d.settings;
    el("interval").value = SETTINGS.interval;
    el("duration").value = SETTINGS.duration_hours;
    poll();                          // refresh the plan line on Monitor
  });
}

// Decimals is applied server-side when the digits are turned into a value, so
// the change has to reach the service before the readout can reflect it.
function setDecimals(){
  post("/settings", {decimals:+el("decimals").value}, function(d){
    if(d.error){ el("status").textContent = d.error; return; }
    SETTINGS = d.settings;
    if(!RUNNING){ refresh(); }      // re-decode the current frame
  });
}

// Invert is part of the tuning, so it re-decodes the current frame straight
// away rather than waiting for a save -- an LCD goes from garbage to a reading
// the moment it is switched off.
function setInvert(){
  CROP.invert = el("invert").checked;
  markDirty();
  refresh();
}

/* --------------------------------------------------------------- presets */
function renderPresets(names){
  var sel = el("presetsel");
  sel.innerHTML = "";
  if(!names || !names.length){
    var o = document.createElement("option");
    o.textContent = "(none saved)";
    o.value = "";
    sel.appendChild(o);
    return;
  }
  names.forEach(function(n){
    var o = document.createElement("option");
    o.textContent = n; o.value = n;
    sel.appendChild(o);
  });
}

function loadPresets(){
  fetch("/presets").then(function(r){ return r.json(); })
    .then(function(d){ renderPresets(d.presets); })
    .catch(function(){ /* transient */ });
}

function savePreset(){
  var name = prompt("Save this tuning as:", "");
  if(!name) return;
  var body = params();
  body.name = name;
  body.invert = el("invert").checked;
  post("/preset_save", body, function(d){
    if(d.error){ el("presetstatus").textContent = d.error; return; }
    renderPresets(d.presets);
    el("presetsel").value = name;
    el("presetstatus").textContent = "saved " + name;
    setTimeout(function(){ el("presetstatus").textContent = ""; }, 3000);
  });
}

function loadPreset(){
  var name = el("presetsel").value;
  if(!name) return;
  post("/preset_load", {name:name}, function(d){
    if(d.error){ el("presetstatus").textContent = d.error; return; }
    CROP = d.crop;
    SAVED_CROP = JSON.parse(JSON.stringify(d.crop));
    loadValues();
    markDirty();
    el("presetstatus").textContent = "loaded " + name;
    grab();
  });
}

function deletePreset(){
  var name = el("presetsel").value;
  if(!name) return;
  if(!confirm("Delete preset " + name + "?")) return;
  post("/preset_delete", {name:name}, function(d){
    renderPresets(d.presets);
    el("presetstatus").textContent = "deleted " + name;
  });
}

function save(){
  fetch("/save_crop", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify(params())})
    .then(function(r){ return r.text(); })
    .then(function(t){
      el("status").textContent = t;
      SAVED_CROP = JSON.parse(JSON.stringify(CROP));
      SAVED_CROP.invert = el("invert").checked;
      markDirty();
    })
    .catch(function(e){ el("status").textContent = "save failed: " + e; });
}

/* --------------------------------------------------------------- logging */
function post(path, body, done){
  fetch(path, {method:"POST", headers:{"Content-Type":"application/json"},
               body: JSON.stringify(body || {})})
    .then(function(r){ return r.json(); })
    .then(done)
    .catch(function(e){ el("status").textContent = "failed: " + e; });
}

function startRun(){
  if(tuningDirty() && !confirm(
      "The tuning on screen has not been saved.\n\n" +
      "The logger reads the saved tuning, not the sliders, so this run will " +
      "use the OLD values and may misread every reading.\n\n" +
      "Start anyway?")){
    showTab("tuning");
    return;
  }
  el("status").textContent = "starting…";
  post("/start", {interval:+el("interval").value,
                  duration_hours:+el("duration").value,
                  decimals:+el("decimals").value}, function(d){
    if(d.error){ el("status").textContent = d.error; return; }
    el("status").textContent = "";
    applyStatus(d);
    loadRuns();
  });
}

function stopRun(){
  el("status").textContent = "stopping…";
  post("/stop", {}, function(d){
    el("status").textContent = "";
    applyStatus(d);
    loadRuns();                    // the chart is auto-saved on stop
    if(TAB === "tuning"){ grab(); } // only touch the camera if it's on screen
  });
}

// A note captures a fresh reading first, which takes about 1.4s. Without a
// guard the panel looks inert for that whole time, so the obvious thing to do
// is press Enter again -- and the run gets the same note twice, seconds apart.
// Clear the field and lock the controls IMMEDIATELY, restoring them only if the
// request actually fails.
var NOTE_BUSY = false;

function addNote(text){
  if(NOTE_BUSY) return;
  var typed = (typeof text !== "string");
  var value = typed ? el("notetext").value : text;
  if(!value) return;

  NOTE_BUSY = true;
  if(typed){ el("notetext").value = ""; }
  el("notebtn").disabled = true;
  setNoteButtonsEnabled(false);
  el("status").textContent = "capturing…";

  post("/note", {text:value}, function(d){
    NOTE_BUSY = false;
    el("notebtn").disabled = false;
    setNoteButtonsEnabled(true);
    if(d.error){
      el("status").textContent = d.error;
      if(typed){ el("notetext").value = value; }   // give it back to retype
      return;
    }
    el("status").textContent = "";
    applyStatus(d);
  });
}

function setNoteButtonsEnabled(on){
  var host = el("notebtns");
  for(var i = 0; i < host.children.length; i++){
    if(host.children[i].tagName === "BUTTON"){
      host.children[i].disabled = !on;
    }
  }
}

function renderNoteButtons(){
  var host = el("notebtns");
  host.innerHTML = "";
  (SETTINGS.note_buttons || []).forEach(function(b){
    if(!b.enabled) return;
    var btn = document.createElement("button");
    btn.textContent = b.label;
    btn.title = "logs: " + b.text;
    btn.disabled = !RUNNING || NOTE_BUSY;
    btn.onclick = function(){ addNote(b.text); };
    host.appendChild(btn);
  });
  if(!host.children.length){
    host.innerHTML = '<span style="color:#778;font-size:11px">' +
      'no quick-note buttons enabled — see Settings below</span>';
  }
}

/* --------------------------------------------------------------- samples */
// A sample's readings arrive long after the moment they describe, so the time
// is captured on drawing and the row is written against it later.
function markSample(){
  post("/sample_mark", {}, function(d){
    if(d.error){ el("status").textContent = d.error; return; }
    renderPending(d.pending_samples);
  });
}

var SAMPLE_BUSY = false;

function logSample(id){
  if(SAMPLE_BUSY) return;
  SAMPLE_BUSY = true;
  var button = el("sb_" + cssId(id));
  if(button){ button.disabled = true; }
  post("/sample_log", {id:id,
                       sample_volume: el("sv_" + cssId(id)).value,
                       sample_temp: el("st_" + cssId(id)).value,
                       sample_abv:  el("sa_" + cssId(id)).value,
                       note:        el("sn_" + cssId(id)).value},
    function(d){
      SAMPLE_BUSY = false;
      if(button){ button.disabled = false; }
      if(d.error){ el("status").textContent = d.error; return; }
      el("status").textContent = "logged at " + d.logged.time.substr(11, 8);
      renderPending(d.pending_samples);
      poll();
    });
}

function cancelSample(id){
  if(!confirm("Discard this marked sample?")) return;
  post("/sample_cancel", {id:id}, function(d){
    renderPending(d.pending_samples);
  });
}

// ISO timestamps contain characters that are awkward in element ids
function cssId(id){ return id.replace(/[^a-zA-Z0-9]/g, ""); }

// The tuner preview reflects the LIVE sliders, but the logger reads the SAVED
// crop.json. When they differ the panel looks perfect while every reading
// misreads, which is invisible until the run produces nothing. Track the saved
// state and say so plainly.
var SAVED_CROP = JSON.parse(JSON.stringify(CROP));

function tuningDirty(){
  var p = params();
  var keys = IDS.concat(["invert"]);
  for(var i = 0; i < keys.length; i++){
    var k = keys[i];
    var live = (k === "invert") ? (el("invert").checked ? 1 : 0) : +p[k];
    var saved = (k === "invert") ? (SAVED_CROP.invert !== false ? 1 : 0)
                                 : +SAVED_CROP[k];
    if(live !== saved) return true;
  }
  return false;
}

function markDirty(){
  var dirty = tuningDirty();
  el("unsaved").style.display = dirty ? "inline" : "none";
  el("tab_tuning_btn").className =
    (el("tab_tuning_btn").className.replace(" dirty", "")) +
    (dirty ? " dirty" : "");
}

var PENDING_KEY = null;          // which samples are currently on screen
var SAMPLE_FIELDS = ["sv_", "st_", "sa_", "sn_"];

function ageText(iso){
  var mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  return (mins < 1 ? "just now" : mins + " min ago");
}

// The status poll runs every 10s, and rebuilding this list on every poll wiped
// whatever was half-typed into the fields -- you would enter a temperature,
// reach for the ABV, and watch it clear. The list is therefore only rebuilt
// when the set of pending samples actually changes; otherwise just the ages
// are refreshed, and the inputs are never touched.
function renderPending(pending){
  var host = el("pendingsamples");
  pending = pending || [];
  var key = pending.map(function(s){ return s.id; }).join("|");

  if(key === PENDING_KEY){
    pending.forEach(function(s){
      var age = el("sage_" + cssId(s.id));
      if(age){ age.textContent = ageText(s.time); }
    });
    return;
  }

  // A rebuild is genuinely needed (one was added, logged or discarded). Carry
  // across anything already typed for the samples that survive it, and the
  // cursor with it -- a sample marked on the phone would otherwise interrupt
  // someone typing on the laptop.
  var kept = {};
  var active = document.activeElement;
  var activeId = (active && active.id) ? active.id : null;
  var caret = null;
  if(activeId && active.type === "text"){
    try { caret = [active.selectionStart, active.selectionEnd]; } catch(e){}
  }
  pending.forEach(function(s){
    var k = cssId(s.id);
    SAMPLE_FIELDS.forEach(function(p){
      var field = el(p + k);
      if(field){ kept[p + k] = field.value; }
    });
  });

  PENDING_KEY = key;
  host.innerHTML = "";
  if(!pending.length){
    host.innerHTML = '<div style="color:#778;font-size:11px;margin-top:6px">' +
      'no samples waiting</div>';
    return;
  }

  pending.forEach(function(s){
    var k = cssId(s.id);
    var row = document.createElement("div");
    row.className = "row";
    row.style.marginTop = "8px";
    row.innerHTML =
      '<span style="color:#8cf;font-variant-numeric:tabular-nums">' +
        s.time.substr(11, 8) + '</span>' +
      '<span id="sage_' + k + '" style="color:#778;font-size:11px">' +
        ageText(s.time) + '</span>' +
      '<span style="color:#6f6;font-size:11px">still ' +
        (s.still_value ? s.still_value + " °C" : "?") + '</span>' +
      '<label>vol <input id="sv_' + k + '" type="number" step="1" ' +
        'style="width:70px"> ml</label>' +
      '<label>temp <input id="st_' + k + '" type="number" step="0.1" ' +
        'style="width:70px"> °C</label>' +
      '<label>ABV <input id="sa_' + k + '" type="number" step="0.1" ' +
        'style="width:70px"> %</label>' +
      '<input id="sn_' + k + '" type="text" placeholder="note (optional)" ' +
        'style="width:170px">' +
      '<button id="sb_' + k + '">Log</button>' +
      '<button id="sc_' + k + '">Discard</button>';
    host.appendChild(row);
    SAMPLE_FIELDS.forEach(function(p){
      if(kept[p + k] !== undefined){ el(p + k).value = kept[p + k]; }
      el(p + k).addEventListener("keydown", function(e){
        if(e.key === "Enter"){ logSample(s.id); }
      });
    });
    el("sb_" + k).addEventListener("click", function(){ logSample(s.id); });
    el("sc_" + k).addEventListener("click", function(){ cancelSample(s.id); });
  });

  if(activeId && el(activeId)){
    el(activeId).focus();
    if(caret){
      try { el(activeId).setSelectionRange(caret[0], caret[1]); } catch(e){}
    }
  }
}

function renderNotes(notes){
  var host = el("notelist");
  host.innerHTML = "";
  (notes || []).slice().reverse().forEach(function(n){
    var d = document.createElement("div");
    var t = (n.time || "").split("T")[1] || "";
    d.innerHTML = '<span class="t">' + t.split(".")[0] + '</span> ' +
      '<span class="v">' + (n.value || "--") + '</span> ' +
      escapeHtml(n.text);
    host.appendChild(d);
  });
}

function escapeHtml(s){
  return String(s).replace(/[&<>"']/g, function(c){
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
  });
}

function renderAlerts(alerts){
  var host = el("alerts");
  host.innerHTML = "";
  (alerts || []).forEach(function(a){
    var d = document.createElement("div");
    d.className = "alert " + (a.level === "error" ? "error" : "warn");
    d.textContent = a.text;
    host.appendChild(d);
  });
}

function applyStatus(s){
  if(!s || s.running === undefined) return;
  RUNNING = s.running;

  el("s_state").textContent = s.running ? "LOGGING" : "idle";
  el("s_state").style.color = s.running ? "#6f6" : "#889";
  el("s_file").textContent = s.csv || "--";
  el("s_count").textContent = s.count;
  el("s_last").textContent = s.last_value || "--";
  el("s_rate").textContent = (s.rate_per_min === null ||
                              s.rate_per_min === undefined)
    ? "--" : (s.rate_per_min >= 0 ? "+" : "") +
             s.rate_per_min.toFixed(2) + " °/min";
  el("s_elapsed").textContent = s.running ? fmtDuration(s.elapsed) : "--";
  el("s_next").textContent = s.running ? fmtDuration(s.next_in) : "--";
  el("s_misreads").textContent = s.misreads +
    (s.misread_streak ? (" (" + s.misread_streak + " in a row)") : "") +
    (s.rate_rejects ? (" · " + s.rate_rejects + " rejected") : "");

  if(s.running && s.last_value){ el("readout").textContent = s.last_value; }
  if(s.running && s.last_raw){ el("raw").textContent = "raw: " + s.last_raw; }

  el("startbtn").disabled = s.running;
  el("stopbtn").disabled = !s.running;
  el("notebtn").disabled = !s.running;
  el("tuner").className = s.running ? "frozen" : "";

  // The Tuning tab is locked while logging. If the run started while the user
  // was sitting on it, move them off rather than leaving dead controls on
  // screen. Settings stays available throughout.
  el("tab_tuning_btn").disabled = s.running;
  el("setuplocked").style.display = s.running ? "inline" : "none";
  if(s.running && TAB === "tuning"){ showTab("monitor"); }

  el("runplan").textContent = "every " + s.interval + "s" +
    (s.duration_hours > 0 ? (", for " + s.duration_hours + "h")
                          : ", until stopped");

  renderAlerts(s.alerts);
  renderNotes(s.notes);
  renderNoteButtons();
  // marking needs a run (it has to know which file the sample belongs to),
  // but FILLING IN a marked sample stays available afterwards -- the cooling
  // delay routinely outlasts the run
  el("samplebtn").disabled = !s.running;
  renderPending(s.pending_samples);
}

function poll(){
  fetch("/status").then(function(r){ return r.json(); })
    .then(applyStatus)
    .catch(function(){ /* transient; the next poll will catch up */ });
}

/* ----------------------------------------------------------------- runs */
function viewChart(file){
  // Stop/resume and restart markers are a record of interruptions, not of the
  // process. Off by default; the checkbox brings them back when you are
  // debugging rather than reading the run.
  var q = [];
  if(file){ q.push("file=" + encodeURIComponent(file)); }
  if(el("showsystem").checked){ q.push("system=1"); }
  window.open("/chart" + (q.length ? ("?" + q.join("&")) : ""), "_blank");
}

function saveChart(file){
  el("chartstatus").textContent = "saving…";
  var body = file ? {file: file} : {};
  body.system = el("showsystem").checked;
  post("/save_chart", body, function(d){
    el("chartstatus").textContent = d.error ? d.error : ("saved " + d.saved);
    loadRuns();
    setTimeout(function(){ el("chartstatus").textContent = ""; }, 4000);
  });
}

// Continue logging into an existing run rather than opening a new file.
// Offered on the newest run only -- resuming an older one would interleave
// readings into a file whose chart has already been written.
function resumeRun(file){
  if(!confirm("Resume logging into " + file + "?"))
    return;
  el("chartstatus").textContent = "resuming…";
  post("/resume_run", {file:file}, function(d){
    if(d.error){ el("chartstatus").textContent = d.error; return; }
    el("chartstatus").textContent = "resumed " + d.resumed;
    applyStatus(d);
    loadRuns();
  });
}

function deleteRun(file){
  if(!confirm("Delete " + file + " and its chart?\nThis cannot be undone."))
    return;
  post("/delete_run", {file:file}, function(d){
    if(d.error){ el("chartstatus").textContent = d.error; return; }
    renderRuns(d.runs);
  });
}

function renderRuns(runs){
  var host = el("runs");
  if(!runs || !runs.length){
    host.innerHTML = '<tr><td style="color:#778">no runs yet</td></tr>';
    return;
  }
  var html = "<tr><th>run</th><th>readings</th><th>notes</th><th>size</th>" +
             "<th>chart</th><th></th></tr>";
  runs.forEach(function(r, index){
    // newest run only, and only when nothing is logging
    var canResume = (index === 0) && !RUNNING && r.readings > 0;
    var kb = (r.bytes/1024).toFixed(1) + " kB";
    html += "<tr>" +
      "<td>" + r.csv + (r.active ? ' <span class="live">● logging</span>' : "") + "</td>" +
      "<td>" + r.readings + "</td>" +
      "<td>" + r.notes + "</td>" +
      "<td>" + kb + "</td>" +
      "<td>" + (r.has_chart
        ? '<a href="/download?file=' + encodeURIComponent(r.csv.replace(/\.csv$/, ".html")) + '">saved</a>'
        : '<span style="color:#778">—</span>') + "</td>" +
      '<td><button onclick="viewChart(\'' + r.csv + '\')">chart</button> ' +
      '<button onclick="saveChart(\'' + r.csv + '\')">save</button> ' +
      '<a href="/download?file=' + encodeURIComponent(r.csv) + '">csv</a> ' +
      (canResume ?
        '<button onclick="resumeRun(\'' + r.csv + '\')">resume</button> ' : "") +
      (r.active ? "" :
        '<button onclick="deleteRun(\'' + r.csv + '\')">delete</button>') +
      "</td></tr>";
  });
  host.innerHTML = html;
}

function loadRuns(){
  fetch("/runs").then(function(r){ return r.json(); })
    .then(function(d){ renderRuns(d.runs); })
    .catch(function(){ /* transient */ });
}

/* -------------------------------------------------------------- settings */
function renderSettingsForm(){
  var host = el("btnsettings");
  host.innerHTML = "";
  (SETTINGS.note_buttons || []).forEach(function(b, i){
    var row = document.createElement("div");
    row.className = "settingsrow";
    row.innerHTML =
      '<input type="checkbox" id="nb_en_' + i + '"' + (b.enabled?" checked":"") + '>' +
      '<input type="text" id="nb_lb_' + i + '" placeholder="button label">' +
      '<input type="text" id="nb_tx_' + i + '" placeholder="text written to the log" style="width:260px">';
    host.appendChild(row);
    el("nb_lb_" + i).value = b.label;
    el("nb_tx_" + i).value = b.text;
  });

  ["alert_delta","alert_misreads","alert_rise","plateau_window",
   "plateau_tolerance","target_temp","max_rate_per_min"].forEach(function(k){
    el(k).value = SETTINGS[k];
  });
  el("interval").value = SETTINGS.interval;
  el("duration").value = SETTINGS.duration_hours;
  el("decimals").value = SETTINGS.decimals;
}

function saveSettings(){
  var buttons = (SETTINGS.note_buttons || []).map(function(b, i){
    return {label: el("nb_lb_" + i).value,
            text:  el("nb_tx_" + i).value,
            enabled: el("nb_en_" + i).checked};
  });
  var payload = {note_buttons: buttons,
                 interval: +el("interval").value,
                 duration_hours: +el("duration").value,
                 decimals: +el("decimals").value};
  ["alert_delta","alert_misreads","alert_rise","plateau_window",
   "plateau_tolerance","target_temp","max_rate_per_min"].forEach(function(k){
    payload[k] = +el(k).value;
  });
  post("/settings", payload, function(d){
    if(d.error){ el("settingsstatus").textContent = d.error; return; }
    SETTINGS = d.settings;
    renderSettingsForm();
    renderNoteButtons();
    el("settingsstatus").textContent = "saved";
    setTimeout(function(){ el("settingsstatus").textContent = ""; }, 2500);
  });
}

/* ------------------------------------------------------------------ init */
loadValues();
markDirty();
renderSettingsForm();
renderNoteButtons();
showMeta();
drawBox();
poll();
loadRuns();
loadPresets();
setInterval(poll, POLL_MS);
// The runs list re-reads every CSV to count readings, so it refreshes on the
// slower settings.refresh_seconds cadence rather than with the status poll.
setInterval(loadRuns, Math.max(15, SETTINGS.refresh_seconds) * 1000);
// No frame is grabbed at load: the page opens on Monitor, and the camera is
// only worth waking when the Setup tab is actually shown.
</script>
</body></html>"""


def render_page(service):
    options = []
    current = (int(service.crop["cap_w"]), int(service.crop["cap_h"]))
    for width, height in config.RESOLUTIONS:
        selected = " selected" if (width, height) == current else ""
        label = "{0}x{1}".format(width, height)
        if (width, height) == (820, 616):
            label += " (proven)"
        options.append('<option value="{0}x{1}"{2}>{3}</option>'.format(
            width, height, selected, label))

    badge = ('<span id="simbadge">SIMULATED &mdash; no camera</span>'
             if service.backend.simulated else "")

    html = PAGE
    html = html.replace("__RESOPTIONS__", "".join(options))
    html = html.replace("__SIMBADGE__", badge)
    html = html.replace("__CROP__", json.dumps(service.crop))
    html = html.replace("__SETTINGS__", json.dumps(service.settings))
    html = html.replace("__CAPTURE__", json.dumps(service.capture_info()))
    html = html.replace("__POLLMS__", str(STATUS_POLL_SECONDS * 1000))
    return html


# ------------------------------------------------------------------ handler

class Handler(server.BaseHTTPRequestHandler):

    service = None       # set in main()
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass             # journald gets our own messages, not a request log

    # -- plumbing ----------------------------------------------------------

    def _send(self, code, ctype, body):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # every image is regenerated in place, so caching would show stale ones
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, code=200):
        self._send(code, "application/json", json.dumps(payload))

    def _file(self, path, ctype):
        if not os.path.exists(path):
            self._send(404, "text/plain", "not ready")
            return
        with open(path, "rb") as fh:
            self._send(200, ctype, fh.read())

    def _body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        # json.loads on 3.5 needs str, not bytes
        return json.loads((raw or b"{}").decode("utf-8"))

    # -- routes ------------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        svc = self.service

        if path == "/":
            self._send(200, "text/html; charset=utf-8", render_page(svc))

        elif path == "/status":
            status = svc.runner.status()
            status["capture"] = svc.capture_info()
            status["pending_samples"] = config.load_pending_samples()
            self._json(status)

        elif path == "/grab":
            try:
                svc.grab()
                self._json({"ok": True, "capture": svc.capture_info()})
            except (capture.CaptureError, RuntimeError) as exc:
                self._json({"ok": False, "error": str(exc),
                            "capture": svc.capture_info()})

        elif path == "/read":
            crop = svc.merge_crop(query)
            try:
                result = svc.read(crop, build_panels=True)
            except (capture.CaptureError, RuntimeError) as exc:
                self._json({"value": "ERR", "raw": "", "err": str(exc),
                            "capture": svc.capture_info()})
                return
            self._json({"value": result["value"], "raw": result["raw"],
                        "err": result["err"], "capture": svc.capture_info()})

        elif path == "/config":
            self._json({"crop": svc.crop, "settings": svc.settings,
                        "capture": svc.capture_info(),
                        "resolutions": [list(r) for r in config.RESOLUTIONS]})

        elif path == "/runs":
            self._json({"runs": svc.list_runs()})

        elif path == "/presets":
            self._json({"presets": sorted(config.load_presets().keys()),
                        "crop": svc.crop})

        elif path == "/chart":
            # Rendered on the fly and never written to disk -- the live view
            # can be opened as often as you like without touching the SD card.
            name = (query.get("file") or [None])[0]
            try:
                target = (svc.run_path(name, (".csv",)) if name
                          else svc.runner.csv_path)
            except ValueError as exc:
                self._send(400, "text/plain", str(exc))
                return
            if not target or not os.path.exists(target):
                self._send(404, "text/html; charset=utf-8",
                           "<p>No run to chart yet.</p>")
                return
            plateau = (svc.runner.plateau_value
                       if target == svc.runner.csv_path else None)
            run = chart.load_run(target)
            show_system = (query.get("system") or ["0"])[0] not in ("0", "")
            self._send(200, "text/html; charset=utf-8",
                       chart.render_html(run, svc.settings, plateau,
                                         show_system=show_system))

        elif path == "/download":
            name = (query.get("file") or [None])[0]
            try:
                target = svc.run_path(name)
            except ValueError as exc:
                self._send(400, "text/plain", str(exc))
                return
            if not os.path.exists(target):
                self._send(404, "text/plain", "not found")
                return
            ctype = ("text/csv" if target.endswith(".csv")
                     else "text/html; charset=utf-8")
            with open(target, "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition",
                             'attachment; filename="{0}"'.format(
                                 os.path.basename(target)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/disp.png":
            self._file(svc.pipeline.disp_img, "image/png")
        elif path == "/mid.png":
            self._file(svc.pipeline.mid_img, "image/png")
        elif path == "/proc.png":
            self._file(svc.pipeline.proc_img, "image/png")
        else:
            self._send(404, "text/plain", "not found")

    def do_POST(self):
        path = urlparse(self.path).path
        svc = self.service

        # Always drain the request body, even on routes that ignore it and on
        # early returns. Under HTTP/1.1 keep-alive, unread body bytes stay in
        # the socket and get parsed as the start of the NEXT request on that
        # connection. That request is then malformed, and the server answers it
        # with BaseHTTPRequestHandler's HTML error page -- which surfaces in the
        # browser as "Unexpected token '<', "<!DOCTYPE "... is not valid JSON"
        # against whatever fetch() happened to be next in line.
        try:
            data = self._body()
        except ValueError:
            data = {}

        if path == "/save_crop":
            if svc.runner.running:
                self._send(409, "text/plain", "stop logging before re-tuning")
                return
            crop = svc.merge_crop({k: [str(v)] for k, v in data.items()})
            svc.crop = crop
            saved = config.save_crop(crop)
            self._send(200, "text/plain",
                       "saved: crop={0} thr={1} b/c={2}/{3} trim={4}".format(
                           saved["geometry"], saved["threshold"],
                           saved["brightness"], saved["contrast"],
                           saved["top_trim"]))

        elif path == "/resolution":
            try:
                crop = svc.set_resolution(int(data["w"]), int(data["h"]))
            except (KeyError, ValueError, RuntimeError) as exc:
                self._json({"error": str(exc) or "bad resolution"}, code=400)
                return
            self._json({"crop": crop, "capture": svc.capture_info()})

        elif path == "/start":
            try:
                # persist whatever the user chose, so a restart uses it too
                svc.settings["interval"] = int(data.get(
                    "interval", svc.settings["interval"]))
                svc.settings["duration_hours"] = float(data.get(
                    "duration_hours", svc.settings["duration_hours"]))
                svc.settings["decimals"] = int(data.get(
                    "decimals", svc.settings["decimals"]))
                svc.settings = config.save_settings(svc.settings)
                status = svc.runner.start(
                    interval=svc.settings["interval"],
                    duration_hours=svc.settings["duration_hours"])
            except (ValueError, RuntimeError) as exc:
                self._json({"error": str(exc)}, code=409)
                return
            self._json(status)

        elif path == "/stop":
            self._json(svc.runner.stop())

        elif path == "/resume_run":
            # Continue logging into an existing CSV instead of starting a new
            # file -- for when a run was stopped and shouldn't have been.
            try:
                target = svc.run_path(data.get("file"), (".csv",))
            except ValueError as exc:
                self._json({"error": str(exc)}, code=400)
                return
            if svc.runner.running:
                self._json({"error": "already logging"}, code=409)
                return
            if not os.path.exists(target):
                self._json({"error": "no such run"}, code=404)
                return
            started_at = runner.first_timestamp(target)
            if started_at is None:
                self._json({"error": "that run has no readings to resume from"},
                           code=409)
                return
            # Duration is measured from the ORIGINAL start. If it has already
            # elapsed, resuming under it would stop again immediately, so fall
            # back to running until stopped rather than silently doing nothing.
            duration = float(svc.settings["duration_hours"])
            note = ""
            if duration > 0 and (time.time() - started_at) >= duration * 3600.0:
                duration = 0
                note = " (duration already elapsed; running until stopped)"
            try:
                status = svc.runner.start(resume_state={
                    "csv_path": target,
                    "started_at": started_at,
                    "interval": svc.settings["interval"],
                    "duration_hours": duration,
                    "marker": "logging resumed",
                })
            except RuntimeError as exc:
                self._json({"error": str(exc)}, code=409)
                return
            status["resumed"] = os.path.basename(target) + note
            self._json(status)

        elif path == "/note":
            try:
                svc.runner.add_note(data.get("text", ""))
            except (ValueError, RuntimeError) as exc:
                self._json({"error": str(exc)}, code=409)
                return
            self._json(svc.runner.status())

        elif path == "/settings":
            merged = dict(svc.settings)
            merged.update(data)
            svc.settings = config.save_settings(merged)
            self._json({"settings": svc.settings})

        elif path == "/sample_mark":
            try:
                pending = svc.mark_sample()
            except RuntimeError as exc:
                self._json({"error": str(exc)}, code=409)
                return
            self._json({"pending_samples": pending})

        elif path == "/sample_log":
            try:
                result = svc.log_sample(
                    data.get("id"),
                    str(data.get("sample_temp", "")).strip(),
                    str(data.get("sample_abv", "")).strip(),
                    str(data.get("note", "")).strip(),
                    str(data.get("sample_volume", "")).strip())
            except ValueError as exc:
                self._json({"error": str(exc)}, code=400)
                return
            self._json({"pending_samples": result["pending"],
                        "logged": result["logged"]})

        elif path == "/sample_cancel":
            self._json({"pending_samples":
                        svc.cancel_sample(data.get("id"))})

        elif path == "/preset_save":
            try:
                presets = config.save_preset(data.get("name"),
                                             svc.merge_crop(
                                                 {k: [str(v)] for k, v
                                                  in data.items()
                                                  if k != "name"}))
            except ValueError as exc:
                self._json({"error": str(exc)}, code=400)
                return
            self._json({"presets": sorted(presets.keys())})

        elif path == "/preset_load":
            try:
                crop = svc.apply_preset(data.get("name"))
            except (ValueError, RuntimeError) as exc:
                self._json({"error": str(exc)}, code=409)
                return
            self._json({"crop": crop, "capture": svc.capture_info()})

        elif path == "/preset_delete":
            presets = config.delete_preset(data.get("name"))
            self._json({"presets": sorted(presets.keys())})

        elif path == "/save_chart":
            name = data.get("file")
            try:
                target = (svc.run_path(name, (".csv",)) if name
                          else svc.runner.csv_path)
            except ValueError as exc:
                self._json({"error": str(exc)}, code=400)
                return
            if not target or not os.path.exists(target):
                self._json({"error": "no run to chart"}, code=404)
                return
            plateau = (svc.runner.plateau_value
                       if target == svc.runner.csv_path else None)
            try:
                written = chart.save_html(target, svc.settings, plateau,
                                          show_system=bool(data.get("system")))
            except OSError as exc:
                self._json({"error": str(exc)}, code=500)
                return
            self._json({"saved": os.path.basename(written)})

        elif path == "/delete_run":
            try:
                target = svc.run_path(data.get("file"), (".csv",))
            except ValueError as exc:
                self._json({"error": str(exc)}, code=400)
                return
            if svc.runner.running and svc.runner.csv_path == target:
                self._json({"error": "that run is still logging"}, code=409)
                return
            removed = []
            for candidate in (target, chart.html_path_for(target)):
                if os.path.exists(candidate):
                    try:
                        os.unlink(candidate)
                        removed.append(os.path.basename(candidate))
                    except OSError as exc:
                        self._json({"error": str(exc)}, code=500)
                        return
            self._json({"deleted": removed, "runs": svc.list_runs()})

        else:
            self._send(404, "text/plain", "not found")


class ThreadedHTTPServer(socketserver.ThreadingMixIn, server.HTTPServer):
    """ThreadingHTTPServer is 3.7+, but it's only this mixin plus a flag.

    Without threading, a ~1.4s capture blocks every other request and the whole
    UI appears frozen.
    """
    daemon_threads = True
    allow_reuse_address = True


# --------------------------------------------------------------------- main

def main(argv=None):
    parser = argparse.ArgumentParser(description="Still Monitor service")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--sim", action="store_true",
                        help="fake camera for UI work on a machine with no "
                             "raspistill/ImageMagick/ssocr")
    parser.add_argument("--sim-speed", type=float, default=60.0,
                        help="virtual minutes per real minute (default 60)")
    parser.add_argument("--sim-misread-every", type=int, default=0,
                        help="inject a misread every Nth reading (0 = never)")
    parser.add_argument("--no-resume", action="store_true",
                        help="don't continue a run interrupted by a power cut")
    args = parser.parse_args(argv)

    backend = capture.make_backend(sim=args.sim, sim_speed=args.sim_speed,
                                   sim_misread_every=args.sim_misread_every,
                                   outdir=BASE_DIR)
    service = Service(backend)
    Handler.service = service

    if not args.no_resume:
        runner.resume_if_interrupted(service)

    httpd = ThreadedHTTPServer(("", args.port), Handler)
    print("Still Monitor on port {0}  [backend: {1}]".format(
        args.port, backend.name))
    if backend.simulated:
        print("SIMULATED camera -- tuning behaviour must be verified on the Pi")
    print("Config: {0}".format(config.BASE_DIR))
    sys.stdout.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping…")
        if service.runner.running:
            service.runner.stop("interrupted")


if __name__ == "__main__":
    main()
