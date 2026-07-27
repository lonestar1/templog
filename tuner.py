#!/usr/bin/env python3
"""
tuner.py — interactive live OCR tuner for the LED thermometer logger.
(Legacy-Python 3.5 compatible: no f-strings, no capture_output.)

Run on the Pi over SSH:
    python3 tuner.py
Open  http://<pi-ip>:8001/  on your Mac.

You get, live and side by side:
  - LEFT: the current camera frame with your crop box drawn on it
  - RIGHT: the thresholded black/white image ssocr actually sees
  - the decoded NUMBER, updating as you drag

Sliders (all applied in software, so they work regardless of camera quirks):
  - Threshold      : the ssocr luminance cut. HIGH drops dim glare, keeps hot digits.
  - Contrast       : stretch before thresholding
  - Brightness     : shift before thresholding
  - Crop X/Y/W/H   : position and size the crop box over the digits
  - Top-trim       : shave pixels off the TOP of the crop to cut a glare bridge
                     across the digit tops without losing the digits themselves

The camera re-captures every ~2s (auto). Everything else re-processes instantly
against the latest frame. When the number reads true, click "Save settings".
The logger reads crop.json and reproduces exactly what you tuned.
"""

import os, json, subprocess, datetime, threading, time
from http import server
from urllib.parse import urlparse, parse_qs

OUTDIR   = os.path.dirname(os.path.abspath(__file__))
FRAME    = os.path.join(OUTDIR, "framing.jpg")
FRAME_TMP= os.path.join(OUTDIR, "framing_tmp.jpg")
DISP_IMG = os.path.join(OUTDIR, "_display.png")   # left: frame + crop box
MID_IMG  = os.path.join(OUTDIR, "_mid.png")       # middle: cropped + b/c, pre-threshold
PROC_IMG = os.path.join(OUTDIR, "_proc.png")      # right: thresholded view
CROPJSON = os.path.join(OUTDIR, "crop.json")
CAP_W, CAP_H = 820, 616   # half-res: fast on Pi 3, still ample for big digits
PORT = 8001

# sensible starting values (tune live). Coords are in the half-res frame.
DEFAULTS = {
    "x": 330, "y": 300, "w": 200, "h": 120,
    "top_trim": 0,          # px shaved off top of crop
    "threshold": 130,
    "brightness": 0,        # -100..100
    "contrast": 0,          # -100..100
}

def current_settings():
    """Start from DEFAULTS, then overlay anything saved in crop.json so the
    page reflects (and continues from) the last saved tuning after a refresh."""
    s = dict(DEFAULTS)
    if os.path.exists(CROPJSON):
        try:
            with open(CROPJSON) as fh:
                saved = json.load(fh)
            for k in ("x", "y", "w", "h", "top_trim",
                      "threshold", "brightness", "contrast"):
                if k in saved:
                    s[k] = int(saved[k])
        except Exception as e:
            print("could not load saved crop.json:", e)
    return s

os.makedirs(OUTDIR, exist_ok=True)
_lock = threading.Lock()

def grab_frame():
    """Capture ONE fresh frame on demand (button-triggered). Auto-exposure ON.
    Captures at reduced resolution for speed on the Pi 3 — still plenty for
    reading big digits, and much faster to process."""
    try:
        subprocess.run(["raspistill", "-o", FRAME_TMP,
                        "-w", str(CAP_W), "-h", str(CAP_H),
                        "-t", "600", "-n"], check=True)
        os.replace(FRAME_TMP, FRAME)   # atomic swap
        return True
    except Exception as e:
        print("capture warning:", e)
        return False

def effective_crop(p):
    """Apply top_trim to the crop: shrink height from the top."""
    x = int(p["x"]); y = int(p["y"]); w = int(p["w"]); h = int(p["h"])
    trim = max(0, int(p.get("top_trim", 0)))
    y2 = y + trim
    h2 = max(1, h - trim)
    return "{0}x{1}+{2}+{3}".format(w, h2, x, y2), (x, y2, w, h2)

def build_images(p):
    """Emit three on-disk PNGs and return the ssocr decode:
      DISP_IMG  = full frame + crop box (left)
      MID_IMG   = cropped + brightness/contrast, BEFORE threshold (middle)
      PROC_IMG  = thresholded view ssocr actually reads (right)"""
    geom, (x, y, w, h) = effective_crop(p)

    # LEFT: plain downscaled frame. The crop box is drawn live client-side
    # as an overlay, so we don't burn it into the image here.
    subprocess.run(["convert", FRAME, "-resize", "640x", DISP_IMG], check=True)

    # MIDDLE: cropped + brightness/contrast, pre-threshold. This is the
    # intermediate the threshold then operates on — shows what b/c is doing.
    proc_crop = os.path.join(OUTDIR, "_proccrop.png")
    subprocess.run(["convert", FRAME, "-crop", geom, "+repage",
                    "-brightness-contrast",
                    "{0}x{1}".format(int(p["brightness"]), int(p["contrast"])),
                    proc_crop], check=True)
    subprocess.run(["convert", proc_crop, "-resize", "640x", MID_IMG], check=True)

    # RIGHT: thresholded view ssocr reads. Use -d 3 to match the logger.
    testbild = os.path.join(OUTDIR, "testbild.png")
    cmd = ["ssocr", "-d", "3", "-t", str(int(p["threshold"])), "-D",
           "-o", testbild, "make_mono", "invert", proc_crop]
    out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         universal_newlines=True)
    decoded = out.stdout.strip()
    err = out.stderr.strip()

    if os.path.exists(testbild):
        subprocess.run(["convert", testbild, "-resize", "640x", PROC_IMG], check=True)
    else:
        subprocess.run(["convert", proc_crop, "-resize", "640x", PROC_IMG], check=True)
    return decoded, err

def parse_temp(raw, decimals=1):
    digits = raw.replace(".", "").replace(" ", "")
    neg = digits.startswith("-"); digits = digits.lstrip("-")
    if not digits.isdigit() or len(digits) < decimals + 1:
        return "??"
    val = int(digits) / (10 ** decimals)
    if neg: val = -val
    return ("%." + str(decimals) + "f") % val

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>OCR tuner</title>
<style>
  body{{font-family:system-ui,sans-serif;background:#111;color:#eee;margin:0;
       padding:8px 14px;font-size:13px;}}
  .top{{display:flex;align-items:baseline;gap:16px;margin-bottom:6px;}}
  h2{{margin:0;font-size:16px;}}
  #readout{{font-size:30px;font-variant-numeric:tabular-nums;color:#6f6;}}
  #raw{{font-size:12px;color:#888;}}
  button{{font-size:13px;padding:5px 12px;cursor:pointer;}}
  #status{{color:#8f8;font-size:12px;}}
  .views{{display:flex;gap:12px;align-items:flex-start;}}
  .view{{flex:1;}}
  .imgwrap{{position:relative;display:block;}}
  .view img{{width:100%;height:auto;max-height:26vh;object-fit:contain;
            border:1px solid #333;background:#000;display:block;}}
  #cropbox{{position:absolute;border:2px solid #33aaff;
           background:rgba(51,170,255,.10);pointer-events:none;display:none;}}
  .cap{{font-size:11px;color:#9ab;margin:2px 0;}}
  .ctrls{{margin-top:12px;max-width:1100px;}}
  .ctrl{{display:flex;align-items:center;gap:12px;margin:7px 0;}}
  .ctrl label{{font-size:12px;color:#bbb;width:210px;flex:none;}}
  .ctrl input[type=range]{{flex:1;min-width:250px;}}
  .v{{display:inline-block;width:48px;text-align:right;color:#8cf;
     font-variant-numeric:tabular-nums;flex:none;}}
</style></head><body>
<div class="top">
  <h2>Live OCR tuner</h2>
  <span id="readout">--</span>
  <span id="raw">raw: --</span>
  <button onclick="grab()">Grab new frame</button>
  <button onclick="save()">Save settings</button>
  <span id="status"></span>
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
    <span class="v" id="tv"></span></div>
  <div class="ctrl"><label>Contrast</label>
    <input id="contrast" type="range" min="-100" max="100" step="1">
    <span class="v" id="cv"></span></div>
  <div class="ctrl"><label>Brightness</label>
    <input id="brightness" type="range" min="-100" max="100" step="1">
    <span class="v" id="bv"></span></div>
  <div class="ctrl"><label>Top-trim (shave glare bridge)</label>
    <input id="top_trim" type="range" min="0" max="120" step="1">
    <span class="v" id="ttv"></span></div>
  <div class="ctrl"><label>Crop X</label>
    <input id="x" type="range" min="0" max="{CAPW}" step="1">
    <span class="v" id="xv"></span></div>
  <div class="ctrl"><label>Crop Y</label>
    <input id="y" type="range" min="0" max="{CAPH}" step="1">
    <span class="v" id="yv"></span></div>
  <div class="ctrl"><label>Crop W</label>
    <input id="w" type="range" min="20" max="{CAPW}" step="1">
    <span class="v" id="wv"></span></div>
  <div class="ctrl"><label>Crop H</label>
    <input id="h" type="range" min="20" max="{CAPH}" step="1">
    <span class="v" id="hv"></span></div>
</div>

<script>
const D={DEFAULTS};
const CAPW={CAPW}, CAPH={CAPH};
const ids=["threshold","contrast","brightness","top_trim","x","y","w","h"];
const vmap={{threshold:"tv",contrast:"cv",brightness:"bv",top_trim:"ttv",
            x:"xv",y:"yv",w:"wv",h:"hv"}};
ids.forEach(id=>{{const el=document.getElementById(id);el.value=D[id];
  document.getElementById(vmap[id]).textContent=D[id];
  el.addEventListener('input',()=>{{document.getElementById(vmap[id]).textContent=el.value;
    drawBox();      // instant client-side crop rectangle
    schedule();     // debounced server re-process
  }});}});

function params(){{const p={{}};ids.forEach(id=>p[id]=document.getElementById(id).value);return p;}}
function qs(){{const p=params();return Object.keys(p).map(k=>k+'='+p[k]).join('&');}}

// Draw the crop rectangle over the left image live. Accounts for
// object-fit:contain letterboxing so the box matches the actual crop.
function drawBox(){{
  const img=document.getElementById('disp'), box=document.getElementById('cropbox');
  const natW=img.naturalWidth, natH=img.naturalHeight;
  if(!img.clientWidth || !natW) return;
  // the rendered image size the browser actually paints (contain = fit inside)
  const cW=img.clientWidth, cH=img.clientHeight;
  const scale=Math.min(cW/natW, cH/natH);
  const dispW=natW*scale, dispH=natH*scale;
  const offX=(cW-dispW)/2, offY=(cH-dispH)/2;   // letterbox offsets
  // natural image is the 640-wide preview; map capture coords -> natural -> displayed
  const nx=natW/CAPW, ny=natH/CAPH;
  const p=params();
  const x=+p.x, y=+p.y, w=+p.w, h=+p.h, trim=+p.top_trim;
  const yTop=(y+trim), hEff=Math.max(1,h-trim);
  box.style.display='block';
  box.style.left =(offX + x*nx*scale)+'px';
  box.style.top  =(offY + yTop*ny*scale)+'px';
  box.style.width =(w*nx*scale)+'px';
  box.style.height=(hEff*ny*scale)+'px';
}}
window.addEventListener('resize', drawBox);
document.getElementById('disp').addEventListener('load', drawBox);

let busy=false, pending=false, tdeb=null;
function schedule(){{
  clearTimeout(tdeb);
  tdeb=setTimeout(()=>{{if(busy){{pending=true;return;}}refresh();}}, 120);
}}

function refresh(){{busy=true;
  document.getElementById('status').textContent='processing…';
  fetch('/read?'+qs()).then(r=>r.json()).then(d=>{{
    document.getElementById('readout').textContent=d.value;
    document.getElementById('raw').textContent='raw: '+d.raw+(d.err?('   ['+d.err+']'):'');
    const t=Date.now();
    document.getElementById('disp').src='/disp.png?t='+t;
    document.getElementById('mid').src='/mid.png?t='+t;
    document.getElementById('proc').src='/proc.png?t='+t;
    document.getElementById('status').textContent='';
    busy=false;if(pending){{pending=false;refresh();}}
  }}).catch(e=>{{busy=false;document.getElementById('status').textContent='err '+e;}});}}

function grab(){{
  document.getElementById('status').textContent='grabbing frame…';
  fetch('/grab').then(r=>r.json()).then(()=>{{refresh();}})
    .catch(e=>document.getElementById('status').textContent='grab failed: '+e);
}}

// initial: grab one frame + process. No auto-loop — you control captures.
drawBox();
grab();

function save(){{
  fetch('/save',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify(params())}}).then(r=>r.text()).then(t=>{{
      document.getElementById('status').textContent=t;}})
    .catch(e=>document.getElementById('status').textContent='save failed: '+e);}}
</script>
</body></html>"""

def merged(q):
    p = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in q:
            try: p[k] = int(float(q[k][0]))
            except Exception: pass
    return p

class Handler(server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        if os.path.exists(path):
            with open(path, 'rb') as fh:
                self._send(200, ctype, fh.read())
        else:
            self._send(404, 'text/plain', b'not ready')

    def do_GET(self):
        u = urlparse(self.path); path = u.path; q = parse_qs(u.query)
        if path == '/':
            html = PAGE.format(CAPW=CAP_W, CAPH=CAP_H,
                               DEFAULTS=json.dumps(current_settings()))
            self._send(200, 'text/html; charset=utf-8', html.encode())
        elif path == '/grab':
            # capture a fresh frame ONLY (button-triggered)
            with _lock:
                ok = grab_frame()
            self._send(200, 'application/json',
                       json.dumps({"ok": bool(ok)}).encode())
        elif path == '/read':
            # process the EXISTING frame with current slider values (no capture)
            p = merged(q)
            if not os.path.exists(FRAME):
                with _lock:
                    grab_frame()   # first time: get an initial frame
            with _lock:
                try:
                    raw, err = build_images(p)
                except Exception as e:
                    self._send(500, 'application/json',
                               json.dumps({"value":"ERR","raw":"","err":str(e)}).encode())
                    return
            resp = {"value": parse_temp(raw), "raw": raw or "(none)", "err": err}
            self._send(200, 'application/json', json.dumps(resp).encode())
        elif path == '/disp.png':
            self._file(DISP_IMG, 'image/png')
        elif path == '/mid.png':
            self._file(MID_IMG, 'image/png')
        elif path == '/proc.png':
            self._file(PROC_IMG, 'image/png')
        else:
            self._send(404, 'text/plain', b'not found')

    def do_POST(self):
        if urlparse(self.path).path == '/save':
            n = int(self.headers.get('Content-Length', 0))
            data = json.loads((self.rfile.read(n) or b'{}').decode('utf-8'))
            p = merged({k: [str(v)] for k, v in data.items()})
            geom, _ = effective_crop(p)   # geometry has top_trim baked in (for logger)
            # store the RAW slider x/y/w/h (untrimmed) so reloading the tuner
            # and re-applying top_trim doesn't double-trim.
            payload = {"geometry": geom, "cap_w": CAP_W, "cap_h": CAP_H,
                       "x": int(p["x"]), "y": int(p["y"]),
                       "w": int(p["w"]), "h": int(p["h"]),
                       "top_trim": int(p["top_trim"]),
                       "threshold": int(p["threshold"]),
                       "brightness": int(p["brightness"]),
                       "contrast": int(p["contrast"]),
                       "saved": datetime.datetime.now().isoformat()}
            with open(CROPJSON, 'w') as fh:
                json.dump(payload, fh, indent=2)
            msg = "saved: crop={0} thr={1} b/c={2}/{3} trim={4}".format(
                geom, p["threshold"], p["brightness"], p["contrast"], p["top_trim"])
            self._send(200, 'text/plain', msg.encode())
        else:
            self._send(404, 'text/plain', b'not found')

if __name__ == '__main__':
    print("Live OCR tuner running.  Open  http://<pi-ip>:%d/  on your Mac." % PORT)
    print("Saves tuned settings to " + CROPJSON)
    server.HTTPServer(('', PORT), Handler).serve_forever()

