#!/usr/bin/env python3
"""
runner.py -- the logging engine.

Replaces logger.py's run loop with one that can be driven from the web UI and
survives a power cut.

Three things differ from the original:

1. ABSOLUTE-TIME SCHEDULING. logger.py slept `interval` seconds *after* each
   capture, so the real cadence was interval + capture time -- measured at
   21.61s against a nominal 20s, which is ~19 minutes of drift over a 4-hour
   run. Here each reading targets `start + n * interval`, so readings land on
   the grid and elapsed time stays exact.

2. NOTES. A note captures a reading immediately and writes it now, rather than
   waiting for the next interval -- when you mark "first drops" you want the
   temperature at that moment, not up to 20 seconds later.

3. RESUME. run_state.json is written on start and stop only (never per
   reading, to spare the SD card). If the service comes back up mid-run, the
   run continues appending to the same CSV with a note marking the gap.

Legacy-Python 3.5 compatible.
"""

import csv
import datetime
import os
import threading
import time

import capture
import config


CSV_HEADER = ["timestamp", "raw", "value", "note"]


def _now():
    return datetime.datetime.now()


def _iso(dt=None):
    return (dt or _now()).isoformat()


class Runner(object):
    """Owns a logging run. One at a time; the camera can't do two things."""

    def __init__(self, service):
        self.service = service
        self.thread = None
        self.stop_event = threading.Event()
        self.csv_lock = threading.Lock()

        self.running = False
        self.csv_path = None
        self.started_at = None        # epoch seconds
        self.interval = 20
        self.duration_hours = 0

        self.readings = []            # (epoch, value_float) for valid readings
        self.notes = []               # dicts: time, value, text
        self.count = 0
        self.misreads = 0
        self.misread_streak = 0
        self.last_raw = ""
        self.last_value = ""
        self.last_good_at = None
        self.last_error = ""
        self.overruns = 0

        self.plateau_value = None     # established plateau, degrees
        self.alerts = []              # list of {level, text}

    # ------------------------------------------------------------ lifecycle

    def start(self, interval=None, duration_hours=None, resume_state=None):
        if self.running:
            raise RuntimeError("a run is already in progress")

        settings = self.service.settings
        self.interval = int(interval if interval is not None
                            else settings["interval"])
        self.duration_hours = float(duration_hours if duration_hours is not None
                                    else settings["duration_hours"])
        self.interval = max(1, self.interval)

        self._reset_run_state()

        if resume_state:
            self.csv_path = resume_state["csv_path"]
            self.started_at = float(resume_state["started_at"])
            self.interval = int(resume_state.get("interval", self.interval))
            self.duration_hours = float(
                resume_state.get("duration_hours", self.duration_hours))
            self._load_existing(self.csv_path)
        else:
            # Minute-granular names collide if a run is stopped and restarted
            # within the same minute -- which silently merges two runs into one
            # CSV and one chart. Suffix instead of appending.
            stamp = _now().strftime("%Y%m%d%H%M")
            path = os.path.join(config.BASE_DIR, "temps_{0}.csv".format(stamp))
            suffix = 2
            while os.path.exists(path):
                path = os.path.join(
                    config.BASE_DIR,
                    "temps_{0}_{1}.csv".format(stamp, suffix))
                suffix += 1
            self.csv_path = path
            self.started_at = time.time()

        self._ensure_header()
        if resume_state:
            self._write_row(_iso(), "", "",
                            "service restarted -- logging resumed")

        self.running = True
        self.stop_event.clear()
        config.save_run_state({
            "csv_path": self.csv_path,
            "started_at": self.started_at,
            "interval": self.interval,
            "duration_hours": self.duration_hours,
            "written_at": _iso(),
        })

        self.thread = threading.Thread(target=self._loop)
        self.thread.daemon = True
        self.thread.start()
        return self.status()

    def stop(self, reason="stopped"):
        if not self.running:
            return self.status()
        self.stop_event.set()
        thread = self.thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=30)
        self.running = False
        self.thread = None
        config.clear_run_state()
        self._write_row(_iso(), "", "", "run {0}".format(reason))
        return self.status()

    def _reset_run_state(self):
        self.readings = []
        self.notes = []
        self.count = 0
        self.misreads = 0
        self.misread_streak = 0
        self.last_raw = ""
        self.last_value = ""
        self.last_good_at = None
        self.last_error = ""
        self.overruns = 0
        self.plateau_value = None
        self.alerts = []

    # ----------------------------------------------------------------- csv

    def _ensure_header(self):
        exists = (os.path.exists(self.csv_path)
                  and os.path.getsize(self.csv_path) > 0)
        if exists:
            return
        with self.csv_lock:
            with open(self.csv_path, "a", newline="") as fh:
                csv.writer(fh).writerow(CSV_HEADER)
                fh.flush()

    def _write_row(self, timestamp, raw, value, note=""):
        """The only frequent disk write in the whole service. Explicitly
        flushed -- the CSV is the source of truth, console output is not."""
        with self.csv_lock:
            with open(self.csv_path, "a", newline="") as fh:
                csv.writer(fh).writerow([timestamp, raw, value, note])
                fh.flush()

    def _load_existing(self, path):
        """Rebuild in-memory state from a CSV we're resuming into."""
        if not os.path.exists(path):
            return
        try:
            with open(path) as fh:
                for row in csv.DictReader(fh):
                    note = (row.get("note") or "").strip()
                    value = (row.get("value") or "").strip()
                    stamp = row.get("timestamp") or ""
                    if value:
                        self.count += 1
                        try:
                            epoch = datetime.datetime.strptime(
                                stamp, "%Y-%m-%dT%H:%M:%S.%f").timestamp()
                            self.readings.append((epoch, float(value)))
                            self.last_good_at = epoch
                        except (ValueError, OverflowError):
                            pass
                        self.last_value = value
                    elif not note:
                        self.misreads += 1
                    if note:
                        self.notes.append({"time": stamp, "value": value,
                                           "text": note})
        except (OSError, ValueError) as exc:
            print("runner: could not reload {0}: {1}".format(path, exc))

    # ---------------------------------------------------------------- loop

    def _loop(self):
        """Absolute-time schedule: reading n targets started_at + n*interval."""
        index = self._next_index()
        completed = False        # True only if the duration ran out
        while not self.stop_event.is_set():
            target = self.started_at + index * self.interval
            delay = target - time.time()
            if delay > 0:
                # wait() rather than sleep() so Stop is immediate
                if self.stop_event.wait(delay):
                    break

            if self._duration_elapsed():
                completed = True
                break

            self._take_reading()
            index += 1

            # If a capture overran the interval, skip the slots we missed
            # instead of silently falling behind the grid.
            now = time.time()
            skipped = 0
            while self.started_at + index * self.interval <= now:
                index += 1
                skipped += 1
            if skipped:
                self.overruns += skipped
                print("runner: capture overran the interval, skipped {0} "
                      "slot(s)".format(skipped))

        # Only self-finalise when the duration ran out. A manual Stop is still
        # blocked in join() at this point and has not yet cleared self.running,
        # so testing that flag alone would log a bogus "duration reached" row
        # on every manual stop.
        if completed and self.running:
            self.running = False
            self.thread = None
            config.clear_run_state()
            self._write_row(_iso(), "", "", "run complete (duration reached)")

    def _next_index(self):
        """Where on the grid we are -- non-zero when resuming."""
        elapsed = time.time() - self.started_at
        if elapsed <= 0:
            return 0
        return int(elapsed // self.interval) + 1

    def _duration_elapsed(self):
        if self.duration_hours <= 0:
            return False
        return (time.time() - self.started_at) >= self.duration_hours * 3600.0

    # ------------------------------------------------------------- reading

    def _take_reading(self, note=""):
        timestamp = _iso()
        try:
            result = self.service.capture_reading()
            raw, value, err = result["raw"], result["value"], result["err"]
            self.last_error = err or ""
        except capture.CaptureError as exc:
            self.last_error = str(exc)
            raw, value = "ERROR", ""
        except Exception as exc:                     # never kill the run
            self.last_error = "unexpected: {0}".format(exc)
            raw, value = "ERROR", ""

        self._record(timestamp, raw, value)
        self._write_row(timestamp, raw, value, note)
        return {"timestamp": timestamp, "raw": raw, "value": value}

    def _record(self, timestamp, raw, value):
        self.last_raw = raw
        if value:
            self.count += 1
            self.misread_streak = 0
            self.last_value = value
            self.last_good_at = time.time()
            self.readings.append((time.time(), float(value)))
        else:
            self.misreads += 1
            self.misread_streak += 1
        self._update_health()

    # -------------------------------------------------------------- health

    def rate_per_minute(self):
        """Degrees per minute over a short window.

        Windowed rather than last-two-readings, so OCR jitter doesn't make the
        figure jump around.
        """
        window = int(self.service.settings.get("rate_window", 5))
        points = self.readings[-window:]
        if len(points) < 2:
            return None
        span = points[-1][0] - points[0][0]
        if span <= 0:
            return None
        return (points[-1][1] - points[0][1]) / span * 60.0

    def _update_health(self):
        settings = self.service.settings
        alerts = []

        # 1. consecutive misreads -- a run can otherwise die silently
        threshold = int(settings.get("alert_misreads", 3))
        if self.misread_streak >= threshold:
            alerts.append({
                "level": "error",
                "text": "{0} consecutive misreads -- check glare or framing"
                        .format(self.misread_streak)})

        # 2. sudden jump between consecutive readings
        delta_limit = float(settings.get("alert_delta", 5.0))
        if len(self.readings) >= 2 and delta_limit > 0:
            delta = self.readings[-1][1] - self.readings[-2][1]
            if abs(delta) > delta_limit:
                alerts.append({
                    "level": "warn",
                    "text": "temperature jumped {0:+.1f} deg between readings"
                            .format(delta)})

        # 3. plateau, then a rise -- the process-finished signal
        self._update_plateau()
        rise_limit = float(settings.get("alert_rise", 1.0))
        if self.plateau_value is not None and self.readings:
            rise = self.readings[-1][1] - self.plateau_value
            if rise >= rise_limit:
                alerts.append({
                    "level": "warn",
                    "text": "risen {0:.1f} deg above the {1:.1f} plateau -- "
                            "process likely finished"
                            .format(rise, self.plateau_value)})

        self.alerts = alerts

    def _update_plateau(self):
        """Call a plateau once a window of readings sits inside a tolerance.

        Once established it is kept, because the whole point is to detect the
        later rise away from it.
        """
        if self.plateau_value is not None:
            return
        settings = self.service.settings
        window = int(settings.get("plateau_window", 10))
        tolerance = float(settings.get("plateau_tolerance", 0.3))
        if len(self.readings) < window:
            return
        values = [v for _, v in self.readings[-window:]]
        if max(values) - min(values) <= tolerance:
            self.plateau_value = sum(values) / len(values)

    # --------------------------------------------------------------- notes

    def add_note(self, text):
        """Capture a reading right now and write it with the note attached."""
        text = (text or "").strip()
        if not text:
            raise ValueError("note text is empty")
        if not self.running:
            raise RuntimeError("not logging")
        result = self._take_reading(note=text)
        entry = {"time": result["timestamp"], "value": result["value"],
                 "text": text}
        self.notes.append(entry)
        return entry

    # -------------------------------------------------------------- status

    def status(self):
        now = time.time()
        elapsed = (now - self.started_at) if self.started_at else 0
        remaining = None
        if self.running and self.duration_hours > 0:
            remaining = max(0.0, self.duration_hours * 3600.0 - elapsed)

        next_in = None
        if self.running and self.started_at:
            index = int((now - self.started_at) // self.interval) + 1
            next_in = max(0.0, self.started_at + index * self.interval - now)

        rate = self.rate_per_minute()
        return {
            "running": self.running,
            "csv": os.path.basename(self.csv_path) if self.csv_path else None,
            "count": self.count,
            "misreads": self.misreads,
            "misread_streak": self.misread_streak,
            "last_value": self.last_value,
            "last_raw": self.last_raw,
            "last_good_age": (now - self.last_good_at)
                             if self.last_good_at else None,
            "last_error": self.last_error,
            "elapsed": elapsed if self.started_at else 0,
            "remaining": remaining,
            "next_in": next_in,
            "interval": self.interval,
            "duration_hours": self.duration_hours,
            "overruns": self.overruns,
            "rate_per_min": round(rate, 3) if rate is not None else None,
            "plateau": (round(self.plateau_value, 2)
                        if self.plateau_value is not None else None),
            "alerts": self.alerts,
            "notes": self.notes[-20:],
        }


def resume_if_interrupted(service):
    """Called at service start. Continues a run that a power cut interrupted.

    If the duration already elapsed while the machine was down, the run is
    finalised rather than resumed.
    """
    state = config.load_run_state()
    if not state or not state.get("csv_path"):
        return None
    if not os.path.exists(state["csv_path"]):
        config.clear_run_state()
        return None

    duration = float(state.get("duration_hours", 0) or 0)
    started_at = float(state.get("started_at", 0) or 0)
    if duration > 0 and (time.time() - started_at) >= duration * 3600.0:
        config.clear_run_state()
        print("runner: interrupted run had already reached its duration; "
              "not resuming")
        return None

    print("runner: resuming interrupted run {0}".format(state["csv_path"]))
    return service.runner.start(resume_state=state)
