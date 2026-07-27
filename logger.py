#!/usr/bin/env python3
"""
logger.py — capture the LED thermometer, decode digits, log to CSV.
(Legacy-Python 3.5 compatible.)

Reads crop geometry AND capture/processing settings from crop.json
(created by crop_tool.py), so exposure is reproduced identically every frame.

Run under tmux for the long unattended session:
    tmux new -s logger
    python3 logger.py
    # Ctrl-b then d to detach; reattach later with:  tmux attach -t logger
"""

import os, json, subprocess, time, csv, datetime, sys

# ---- settings ----
INTERVAL     = 20      # seconds between readings
DECIMALS     = 1       # display shows one decimal place. Set 0 if none.
NUM_DIGITS   = 3       # expected digits on display. Stops ssocr inventing
                       # phantom '1's from noise. Set -1 only if digit count varies.
# threshold/brightness/contrast come from crop.json (tuned in tuner.py)
# ------------------

OUTDIR   = os.path.dirname(os.path.abspath(__file__))
CROPJSON = os.path.join(OUTDIR, "crop.json")
# each run writes its own timestamped file: temps_YYYYMMDDHHMM.csv
RUN_STAMP = datetime.datetime.now().strftime("%Y%m%d%H%M")
CSV_PATH = os.path.join(OUTDIR, "temps_%s.csv" % RUN_STAMP)
FRAME    = os.path.join(OUTDIR, "frame.jpg")
CROP_IMG = os.path.join(OUTDIR, "crop.png")
CAP_W, CAP_H = 820, 616   # must match tuner.py's capture resolution

def load_cfg():
    if not os.path.exists(CROPJSON):
        sys.exit("No config set. Run tuner.py first (expected %s)." % CROPJSON)
    with open(CROPJSON) as fh:
        c = json.load(fh)
    c.setdefault("threshold", 130)
    c.setdefault("brightness", 0)
    c.setdefault("contrast", 0)
    return c

def capture():
    subprocess.run(["raspistill", "-o", FRAME,
                    "-w", str(CAP_W), "-h", str(CAP_H),
                    "-t", "500", "-n"], check=True)

def read_value(cfg):
    subprocess.run(["convert", FRAME, "-crop", cfg["geometry"], "+repage",
                    "-brightness-contrast",
                    "{0}x{1}".format(int(cfg["brightness"]), int(cfg["contrast"])),
                    CROP_IMG], check=True)
    out = subprocess.run(
        ["ssocr", "-d", str(NUM_DIGITS), "-t", str(int(cfg["threshold"])),
         "make_mono", "invert", CROP_IMG],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    return out.stdout.strip(), out.stderr.strip()

# plausible temperature range for your display (edit if needed)
TEMP_MIN = -40.0
TEMP_MAX = 120.0

def to_temp(raw):
    digits = raw.replace(".", "").replace(" ", "")
    neg = digits.startswith("-")
    digits = digits.lstrip("-")
    if not digits.isdigit():
        return ""
    # a valid reading has the expected number of digits (e.g. 3 -> 16.3)
    if NUM_DIGITS > 0:
        if len(digits) != NUM_DIGITS:
            return ""
    else:
        if len(digits) < DECIMALS + 1:
            return ""
    val = int(digits) / (10 ** DECIMALS)
    if neg:
        val = -val
    # reject physically impossible values (noise/misreads)
    if val < TEMP_MIN or val > TEMP_MAX:
        return ""
    return ("%." + str(DECIMALS) + "f") % val

def main():
    cfg = load_cfg()
    new_file = (not os.path.exists(CSV_PATH)) or os.stat(CSV_PATH).st_size == 0
    print("Logging every %ds  crop=%s  thr=%s b/c=%s/%s" % (
        INTERVAL, cfg["geometry"], cfg["threshold"],
        cfg["brightness"], cfg["contrast"]))
    print("Writing %s   (Ctrl-C to stop)" % CSV_PATH)
    with open(CSV_PATH, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["timestamp", "raw", "value"]); f.flush()
        while True:
            ts = datetime.datetime.now().isoformat()
            try:
                capture()
                raw, err = read_value(cfg)
                value = to_temp(raw)
                w.writerow([ts, raw, value]); f.flush()
                flag = "" if value else "  <-- MISREAD"
                print("%s  raw='%s'  value=%s%s" % (ts, raw, value or "??", flag))
            except subprocess.CalledProcessError as e:
                w.writerow([ts, "ERROR", ""]); f.flush()
                print("%s  capture/convert error: %s" % (ts, e))
            time.sleep(INTERVAL)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
