#!/usr/bin/env python3
"""
stillmon.py -- Still Monitor service.

One always-on web service replacing the separate tuner.py / logger.py scripts.
This is stage one: the tuner, rebuilt on the shared capture module, with the
config split and the capture-resolution control.

Run on the Pi:
    python3 stillmon.py
    # then open http://192.168.113.98:8001/

Run on a Mac for UI work (no camera, no ImageMagick, no ssocr needed):
    python3 stillmon.py --sim
    # then open http://localhost:8001/

Legacy-Python 3.5 compatible: no f-strings, no ThreadingHTTPServer (3.7+), no
subprocess capture_output. The threading server is built from ThreadingMixIn,
which 3.5 does have -- and it matters: a capture takes ~1.6s, and on a
single-threaded server that blocks every other request and the UI looks dead.
"""

import argparse
import json
import os
import socketserver
import sys
import threading

from http import server
from urllib.parse import urlparse, parse_qs

import capture
import config


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PORT = 8001


class Service(object):
    """Shared state for the whole process.

    The camera, the working image files and the config are all shared mutable
    state. `camera_lock` serialises every operation that touches them.
    """

    def __init__(self, backend):
        self.backend = backend
        self.pipeline = capture.Pipeline(BASE_DIR, backend)
        self.camera_lock = threading.Lock()
        self.crop = config.load_crop()
        self.settings = config.load_settings()

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
        return merged

    def set_resolution(self, width, height):
        """Switch capture resolution, carrying the tuning across.

        The crop box is rescaled proportionally so the tuning survives -- see
        config.rescale_crop. The frame on disk is now the wrong size, so it is
        dropped and the caller should grab a fresh one.
        """
        if (int(width), int(height)) not in config.RESOLUTIONS:
            raise ValueError("unsupported resolution")
        with self.camera_lock:
            self.crop = config.rescale_crop(self.crop, width, height)
            config.save_crop(self.crop)
            if os.path.exists(self.pipeline.frame):
                os.unlink(self.pipeline.frame)
        return self.crop

    # -- capture -----------------------------------------------------------

    def grab(self):
        with self.camera_lock:
            return self.pipeline.grab(self.crop)

    def read(self, crop, build_panels=True):
        with self.camera_lock:
            if not self.pipeline.have_frame():
                self.pipeline.grab(self.crop)
            return self.pipeline.read(crop, self.settings,
                                      build_panels=build_panels)

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
# braces don't all have to be doubled. That doubling was a standing bug
# magnet in the original tuner.

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Still Monitor</title>
<style>
  body{font-family:system-ui,sans-serif;background:#111;color:#eee;margin:0;
       padding:8px 14px;font-size:13px;}
  .top{display:flex;align-items:baseline;gap:16px;margin-bottom:6px;
       flex-wrap:wrap;}
  h2{margin:0;font-size:16px;}
  #readout{font-size:30px;font-variant-numeric:tabular-nums;color:#6f6;}
  #raw{font-size:12px;color:#888;}
  button{font-size:13px;padding:5px 12px;cursor:pointer;}
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
  .warn{color:#e8a33d;}
</style></head><body>
<div class="top">
  <h2>Still Monitor &mdash; tuning</h2>
  <span id="readout">--</span>
  <span id="raw">raw: --</span>
  <button onclick="grab()">Grab new frame</button>
  <button onclick="save()">Save settings</button>
  <label style="color:#bbb;font-size:12px;">Resolution
    <select id="resolution" onchange="setResolution()">__RESOPTIONS__</select>
  </label>
  <span id="status"></span>
  __SIMBADGE__
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
  <div class="ctrl"><label>Threshold (high = drop glare)</label>
    <input id="threshold" type="range" min="10" max="254" step="1">
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

<div class="meta" id="meta"></div>

<script>
var CROP = __CROP__;
var CAPTURE = __CAPTURE__;
var IDS = ["threshold","contrast","brightness","top_trim","x","y","w","h"];

function el(id){ return document.getElementById(id); }

// Slider ranges depend on the capture resolution, so they're set at runtime
// rather than baked into the HTML.
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
}

IDS.forEach(function(id){
  el(id).addEventListener("input", function(){
    el(id + "_v").textContent = el(id).value;
    CROP[id] = +el(id).value;
    drawBox();     // instant, client side
    schedule();    // debounced server re-process
  });
});

function params(){
  var p = {};
  IDS.forEach(function(id){ p[id] = el(id).value; });
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
  var bits = [];
  bits.push("capture " + CROP.cap_w + "x" + CROP.cap_h);
  if(CAPTURE.seconds !== null && CAPTURE.seconds !== undefined){
    bits.push("last capture " + CAPTURE.seconds.toFixed(2) + "s");
  }
  el("meta").innerHTML = bits.join(" &middot; ");
}

function refresh(){
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
    if(d.capture){ CAPTURE = d.capture; }
    showMeta();
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
    if(d.capture){ CAPTURE = d.capture; }
    if(d.error){ el("status").textContent = d.error; return; }
    refresh();
  }).catch(function(e){ el("status").textContent = "grab failed: " + e; });
}

function setResolution(){
  var parts = el("resolution").value.split("x");
  el("status").textContent = "switching resolution…";
  fetch("/resolution", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({w:+parts[0], h:+parts[1]})})
    .then(function(r){ return r.json(); }).then(function(d){
      if(d.error){ el("status").textContent = d.error; return; }
      CROP = d.crop;
      loadValues();
      // the crop box was rescaled to the new frame size; confirm the reading
      el("status").textContent = "crop rescaled — re-check the reading";
      grab();
    }).catch(function(e){ el("status").textContent = "failed: " + e; });
}

function save(){
  fetch("/save_crop", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify(params())})
    .then(function(r){ return r.text(); })
    .then(function(t){ el("status").textContent = t; })
    .catch(function(e){ el("status").textContent = "save failed: " + e; });
}

loadValues();
showMeta();
drawBox();
grab();
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
    html = html.replace("__CAPTURE__", json.dumps(service.capture_info()))
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

        elif path == "/grab":
            try:
                svc.grab()
                self._json({"ok": True, "capture": svc.capture_info()})
            except capture.CaptureError as exc:
                self._json({"ok": False, "error": str(exc),
                            "capture": svc.capture_info()})

        elif path == "/read":
            crop = svc.merge_crop(query)
            try:
                result = svc.read(crop, build_panels=True)
            except capture.CaptureError as exc:
                self._json({"value": "ERR", "raw": "", "err": str(exc),
                            "capture": svc.capture_info()})
                return
            self._json({"value": result["value"], "raw": result["raw"],
                        "err": result["err"], "capture": svc.capture_info()})

        elif path == "/config":
            self._json({"crop": svc.crop, "settings": svc.settings,
                        "capture": svc.capture_info(),
                        "resolutions": [list(r) for r in config.RESOLUTIONS]})

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

        if path == "/save_crop":
            data = self._body()
            crop = svc.merge_crop({k: [str(v)] for k, v in data.items()})
            svc.crop = crop
            saved = config.save_crop(crop)
            self._send(200, "text/plain",
                       "saved: crop={0} thr={1} b/c={2}/{3} trim={4}".format(
                           saved["geometry"], saved["threshold"],
                           saved["brightness"], saved["contrast"],
                           saved["top_trim"]))

        elif path == "/resolution":
            data = self._body()
            try:
                crop = svc.set_resolution(int(data["w"]), int(data["h"]))
            except (KeyError, ValueError) as exc:
                self._json({"error": str(exc) or "bad resolution"}, code=400)
                return
            self._json({"crop": crop, "capture": svc.capture_info()})

        else:
            self._send(404, "text/plain", "not found")


class ThreadedHTTPServer(socketserver.ThreadingMixIn, server.HTTPServer):
    """ThreadingHTTPServer is 3.7+, but it's only this mixin plus a flag.

    Without threading, a ~1.6s capture blocks every other request and the whole
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
    args = parser.parse_args(argv)

    backend = capture.make_backend(sim=args.sim, sim_speed=args.sim_speed,
                                   sim_misread_every=args.sim_misread_every,
                                   outdir=BASE_DIR)
    Handler.service = Service(backend)

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
        print("\nStopped.")


if __name__ == "__main__":
    main()
