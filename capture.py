#!/usr/bin/env python3
"""
capture.py -- shared camera capture + seven-segment OCR decode.

Extracted from the proven tuner.py / logger.py pipeline. The image path is
byte-faithful to what produced a 216-reading run with zero misreads:

    raspistill -o frame.jpg -w W -h H -t 500 -n
    convert frame.jpg -crop GEOM +repage -brightness-contrast BxC crop.png
    ssocr -d 3 -t THR make_mono invert crop.png

Do not "improve" those invocations without re-validating on the Pi. In
particular: `invert` is required (the display is bright-on-dark, ssocr wants
dark-on-light), `-d 3` must stay fixed (auto-count invents phantom 1s from
noise), and the decimal point is inserted by us -- ssocr's dot detection is
unreliable on this multiplexed display.

Legacy-Python 3.5 compatible: no f-strings, no subprocess capture_output.
"""

import math
import os
import shutil
import subprocess
import time


class CaptureError(Exception):
    """Raised when the camera or an image tool fails."""


# ---------------------------------------------------------------- geometry

def effective_crop(crop):
    """Apply top_trim to the crop box: shrink the height from the top.

    Returns (imagemagick_geometry, (x, y, w, h)) with the trim baked in.
    """
    x = int(crop["x"])
    y = int(crop["y"])
    w = int(crop["w"])
    h = int(crop["h"])
    trim = max(0, int(crop.get("top_trim", 0)))
    y2 = y + trim
    h2 = max(1, h - trim)
    return "{0}x{1}+{2}+{3}".format(w, h2, x, y2), (x, y2, w, h2)


# ------------------------------------------------------------ value decode

def to_temp(raw, decimals=1, num_digits=3, temp_min=-40.0, temp_max=120.0):
    """Turn an ssocr digit string into a formatted temperature string.

    Returns "" for anything that isn't a plausible reading -- the caller logs
    that as a misread. Ported unchanged from logger.py, which validated over a
    216-reading run.
    """
    digits = raw.replace(".", "").replace(" ", "")
    neg = digits.startswith("-")
    digits = digits.lstrip("-")
    if not digits.isdigit():
        return ""
    # a valid reading has the expected digit count (e.g. 3 -> 16.3)
    if num_digits > 0:
        if len(digits) != num_digits:
            return ""
    else:
        if len(digits) < decimals + 1:
            return ""
    val = int(digits) / (10 ** decimals)
    if neg:
        val = -val
    # reject physically impossible values (noise / misreads)
    if val < temp_min or val > temp_max:
        return ""
    return ("%." + str(decimals) + "f") % val


def temp_to_raw(value, decimals=1, num_digits=3):
    """Inverse of to_temp -- render a float as the digit string the display
    would show. Used by the simulator."""
    scaled = int(round(abs(value) * (10 ** decimals)))
    digits = str(scaled)
    if num_digits > 0:
        digits = digits[-num_digits:].rjust(num_digits, "0")
    return ("-" + digits) if value < 0 else digits


# ----------------------------------------------------------------- backends

class PiBackend(object):
    """The real thing: raspistill + ImageMagick + ssocr on the Pi."""

    name = "pi"
    simulated = False

    def __init__(self, capture_timeout_ms=500):
        # 500ms matches the validated logger.py run. Longer settling times were
        # not needed; auto-exposure handles the display fine.
        self.capture_timeout_ms = capture_timeout_ms

    def grab(self, dest, cap_w, cap_h):
        """Capture one frame to `dest`. Auto-exposure ON -- deliberately.

        raspistill -ex off produces a BLACK frame on this firmware (it zeroes
        sensor gain), and -ev / -ISO are overridden by auto-exposure. All image
        adjustment happens in software instead.
        """
        tmp = dest + ".tmp.jpg"
        try:
            subprocess.run(
                ["raspistill", "-o", tmp,
                 "-w", str(int(cap_w)), "-h", str(int(cap_h)),
                 "-t", str(int(self.capture_timeout_ms)), "-n"],
                check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True)
        except OSError as exc:
            raise CaptureError("raspistill not available: {0}".format(exc))
        except subprocess.CalledProcessError as exc:
            raise CaptureError("raspistill failed: {0}".format(
                (exc.stderr or "").strip() or exc))
        os.replace(tmp, dest)   # atomic swap, so readers never see a part file

    def crop_adjust(self, frame, dest, geometry, brightness, contrast):
        """Crop the frame and apply brightness/contrast, pre-threshold."""
        try:
            subprocess.run(
                ["convert", frame, "-crop", geometry, "+repage",
                 "-brightness-contrast",
                 "{0}x{1}".format(int(brightness), int(contrast)),
                 dest],
                check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True)
        except OSError as exc:
            raise CaptureError("ImageMagick not available: {0}".format(exc))
        except subprocess.CalledProcessError as exc:
            raise CaptureError("convert failed: {0}".format(
                (exc.stderr or "").strip() or exc))

    def resize(self, src, dest, width=640):
        try:
            subprocess.run(["convert", src, "-resize", "{0}x".format(width), dest],
                           check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise CaptureError("resize failed: {0}".format(exc))

    def ssocr(self, src, threshold, num_digits=3, debug_image=None):
        """Decode digits. Returns (raw_string, stderr_string).

        `invert` is required and `-d N` must stay fixed -- see module docstring.
        """
        cmd = ["ssocr", "-d", str(int(num_digits)), "-t", str(int(threshold))]
        if debug_image:
            cmd += ["-D", "-o", debug_image]
        cmd += ["make_mono", "invert", src]
        try:
            out = subprocess.run(cmd,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 universal_newlines=True)
        except OSError as exc:
            raise CaptureError("ssocr not available: {0}".format(exc))
        return out.stdout.strip(), out.stderr.strip()


class SimBackend(object):
    """Fake camera for developing on the Mac, where raspistill, ImageMagick and
    ssocr do not exist.

    Serves a real frame captured from the Pi for every image panel, and
    synthesises readings from a scripted still-run curve: ambient, ramp,
    plateau, then the ~1 degree rise that means the process is finished. That
    curve is what exercises the plateau detection and the alert thresholds.

    LIMITATION: the crop/threshold panels all show the same unprocessed frame,
    because there is no image tooling on the Mac. Tuning behaviour must always
    be verified on the Pi.
    """

    name = "sim"
    simulated = True

    # scripted curve, in virtual minutes
    AMBIENT = 16.5
    PLATEAU = 78.3
    RAMP_END = 60.0       # ambient -> plateau over this many virtual minutes
    PLATEAU_END = 120.0   # holds until here, then climbs (process finished)
    FINAL_RISE = 1.6      # degrees gained after the plateau breaks

    def __init__(self, sample_frame, speed=60.0, misread_every=0):
        """speed: virtual minutes per real minute (60 = an hour a minute).
        misread_every: inject a misread every Nth reading (0 = never)."""
        self.sample_frame = sample_frame
        self.speed = float(speed)
        self.misread_every = int(misread_every)
        self.started = time.time()
        self.reading_count = 0

    # -- the fake camera ---------------------------------------------------

    def grab(self, dest, cap_w, cap_h):
        if not os.path.exists(self.sample_frame):
            raise CaptureError(
                "sim sample frame missing: {0}".format(self.sample_frame))
        time.sleep(0.15)   # stand in for real capture latency
        shutil.copyfile(self.sample_frame, dest)

    def crop_adjust(self, frame, dest, geometry, brightness, contrast):
        shutil.copyfile(frame, dest)

    def resize(self, src, dest, width=640):
        shutil.copyfile(src, dest)

    def ssocr(self, src, threshold, num_digits=3, debug_image=None):
        if debug_image:
            shutil.copyfile(src, debug_image)
        self.reading_count += 1
        if self.misread_every and self.reading_count % self.misread_every == 0:
            return "", "sim: injected misread"
        return temp_to_raw(self.temperature(), 1, num_digits), ""

    # -- the scripted curve ------------------------------------------------

    def virtual_minutes(self):
        return (time.time() - self.started) / 60.0 * self.speed

    def temperature(self):
        t = self.virtual_minutes()
        if t < self.RAMP_END:
            # ease into the plateau rather than hitting it with a corner
            frac = t / self.RAMP_END
            base = self.AMBIENT + (self.PLATEAU - self.AMBIENT) * math.sin(
                frac * math.pi / 2)
        elif t < self.PLATEAU_END:
            base = self.PLATEAU
        else:
            over = min(1.0, (t - self.PLATEAU_END) / 20.0)
            base = self.PLATEAU + self.FINAL_RISE * over
        # deterministic jitter, so runs are reproducible
        return base + 0.05 * math.sin(t * 3.1)


# ------------------------------------------------------------------ pipeline

class Pipeline(object):
    """Owns the working-image paths and runs frames through capture -> OCR.

    NOT thread-safe by itself. The camera and the working files are shared
    state; the service serialises access with a single lock.
    """

    def __init__(self, outdir, backend):
        self.outdir = outdir
        self.backend = backend
        self.frame = os.path.join(outdir, "frame.jpg")
        self.crop_img = os.path.join(outdir, "crop.png")
        self.disp_img = os.path.join(outdir, "_display.png")
        self.mid_img = os.path.join(outdir, "_mid.png")
        self.proc_img = os.path.join(outdir, "_proc.png")
        self._proc_crop = os.path.join(outdir, "_proccrop.png")
        self._testbild = os.path.join(outdir, "testbild.png")
        self.last_capture_seconds = None
        self.last_grab_time = None

    # -- capture -----------------------------------------------------------

    def grab(self, crop):
        """Capture one fresh frame at the configured resolution."""
        started = time.time()
        self.backend.grab(self.frame,
                          int(crop.get("cap_w", 820)),
                          int(crop.get("cap_h", 616)))
        self.last_capture_seconds = time.time() - started
        self.last_grab_time = time.time()
        return self.last_capture_seconds

    def have_frame(self):
        return os.path.exists(self.frame)

    # -- decode ------------------------------------------------------------

    def read(self, crop, settings, build_panels=False):
        """Process the current frame and decode it.

        Returns a dict: raw, value, err, and the capture geometry used. When
        build_panels is set, also refreshes the three tuner preview images --
        that costs three extra `convert` calls, so the logger leaves it off.
        """
        geometry, box = effective_crop(crop)
        num_digits = int(settings.get("num_digits", 3))

        self.backend.crop_adjust(
            self.frame, self._proc_crop, geometry,
            int(crop.get("brightness", 0)), int(crop.get("contrast", 0)))

        debug_image = self._testbild if build_panels else None
        raw, err = self.backend.ssocr(
            self._proc_crop, int(crop.get("threshold", 130)),
            num_digits=num_digits, debug_image=debug_image)

        if build_panels:
            self._build_panels()
        else:
            # the logger still wants the cropped image on disk for debugging
            self.backend.crop_adjust(
                self.frame, self.crop_img, geometry,
                int(crop.get("brightness", 0)), int(crop.get("contrast", 0)))

        value = to_temp(raw,
                        decimals=int(settings.get("decimals", 1)),
                        num_digits=num_digits,
                        temp_min=float(settings.get("temp_min", -40.0)),
                        temp_max=float(settings.get("temp_max", 120.0)))
        return {"raw": raw, "value": value, "err": err,
                "geometry": geometry, "box": box}

    def _build_panels(self):
        """Refresh the three tuner previews.

        LEFT is the plain frame -- the crop box is drawn client-side as an
        overlay so dragging a slider is instant and doesn't need a round trip.
        """
        self.backend.resize(self.frame, self.disp_img)
        self.backend.resize(self._proc_crop, self.mid_img)
        if os.path.exists(self._testbild):
            self.backend.resize(self._testbild, self.proc_img)
        else:
            self.backend.resize(self._proc_crop, self.proc_img)


def make_backend(sim=False, sim_speed=60.0, sim_misread_every=0, outdir=None):
    """Pick a backend. `sim` is for Mac-side UI work only."""
    if sim:
        sample = os.path.join(outdir or ".", "sim", "sample_frame.jpg")
        return SimBackend(sample, speed=sim_speed,
                          misread_every=sim_misread_every)
    return PiBackend()
