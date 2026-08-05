#!/usr/bin/env python3
"""
runner.py -- the logging engine.

Replaces the run loop of the original logger.py (removed once superseded; see
git history) with one that can be driven from the web UI and survives a power
cut.

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
import chart
import config


# Three different temperatures meet in this file, so be precise about them:
#   value        the STILL temperature -- the vapour temperature at the probe,
#                which is what the camera reads. This is the curve.
#   sample_temp  the temperature of a COLLECTED SAMPLE when measured, used to
#                correct its hydrometer reading. Nothing to do with the curve.
#   (the wash/boiler temperature is a third thing again, recorded by hand in
#    notes, and never read by this software)
# sample_temp is deliberately kept out of `value`: a cooled sample sitting at
# 20 C would put a meaningless spike in the curve.
CSV_HEADER = ["timestamp", "raw", "value", "note",
              "sample_volume", "sample_temp", "sample_abv"]

# Runs started before samples existed have a 4-column header. Appending
# 6-column rows to those would produce a ragged file, so every append matches
# whatever header the file actually has.
_HEADER_CACHE = {}
_APPEND_LOCK = threading.Lock()


def header_of(path):
    """The column names an existing CSV was created with."""
    cached = _HEADER_CACHE.get(path)
    if cached:
        return cached
    header = list(CSV_HEADER)
    try:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path) as fh:
                first = csv.reader(fh).__next__()
            if first:
                header = [c.strip() for c in first]
    except (OSError, StopIteration, ValueError):
        pass
    _HEADER_CACHE[path] = header
    return header


def append_row(path, fields):
    """Append a row to a run CSV, matching its existing columns.

    `fields` is a dict keyed by column name. Columns the file does not have are
    dropped; columns it has that aren't supplied are written empty.
    """
    header = header_of(path)
    row = [fields.get(name, "") for name in header]
    with _APPEND_LOCK:
        with open(path, "a", newline="") as fh:
            csv.writer(fh).writerow(row)
            fh.flush()
    return header


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
        self.rate_rejects = 0         # readings dropped by the rate gate
        self.rate_reject_streak = 0
        self.last_reject = None       # {"value", "rate"} of the most recent
        self.seen_ramp = False        # has the temperature actually climbed?

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
            self._derive_seen_ramp()
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
            self._write_row(_iso(), "", "", resume_state.get(
                "marker", "service restarted -- logging resumed"))

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
        self.save_chart()
        return self.status()

    def save_chart(self):
        """Write the run's HTML chart.

        Called on stop and on duration-complete -- the only automatic writes.
        Everything else renders on the fly and never touches the disk, to keep
        SD wear down.
        """
        if not self.csv_path or not os.path.exists(self.csv_path):
            return None
        try:
            return chart.save_html(self.csv_path, self.service.settings,
                                   self.plateau_value)
        except Exception as exc:      # a chart failure must not lose the run
            print("runner: could not write chart: {0}".format(exc))
            return None

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
        self.rate_rejects = 0
        self.rate_reject_streak = 0
        self.last_reject = None
        self.seen_ramp = False

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
        append_row(self.csv_path, {"timestamp": timestamp, "raw": raw,
                                   "value": value, "note": note})

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

    def _derive_seen_ramp(self):
        """Recover the "has it ramped?" flag from a resumed run's history.

        Without this, restarting DURING a plateau is unrecoverable: the flag
        starts false, the rate at a plateau is zero by definition, so it can
        never become true again -- and the plateau is therefore never detected
        and the process-finished alert never fires for the rest of the run.
        A restart in the middle of a four-hour distillation would silently cost
        exactly the signal the run is being watched for.
        """
        settings = self.service.settings
        ramp_rate = float(settings.get("plateau_ramp_rate", 0.5))
        window = max(2, int(settings.get("rate_window", 5)))

        # Measured over the SAME window as rate_per_minute(), not between
        # consecutive readings. OCR jitter of 0.2 C across a 15s interval
        # computes as 0.8 deg/min on a pair, which is enough to call a dead
        # flat ambient run a ramp -- the exact false positive this flag exists
        # to prevent.
        for index in range(window, len(self.readings) + 1):
            points = self.readings[index - window:index]
            span = points[-1][0] - points[0][0]
            if span <= 0:
                continue
            rate = abs(points[-1][1] - points[0][1]) / span * 60.0
            if rate >= ramp_rate:
                self.seen_ramp = True
                return

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
            self.save_chart()

    def _next_index(self):
        """Which slot on the schedule grid to aim at next.

        No `+ 1`. A fresh start has an elapsed of a few milliseconds -- enough
        to be positive -- so adding one pushed the first target a whole
        interval into the future and the run captured nothing until then. At a
        30s interval that is a visibly dead panel right after pressing Start.

        Flooring instead gives slot 0 on a fresh start, so the first capture is
        immediate. On a resume it gives a slot already in the past, so the
        delay is negative and a reading is taken straight away -- also what you
        want after a restart -- and the loop's skip-ahead then puts it back on
        the grid.
        """
        elapsed = time.time() - self.started_at
        if elapsed <= 0:
            return 0
        return int(elapsed // self.interval)

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

        value = self._rate_gate(value)
        self._record(timestamp, raw, value)
        self._write_row(timestamp, raw, value, note)
        return {"timestamp": timestamp, "raw": raw, "value": value}

    # -- rate gate ---------------------------------------------------------

    RATE_REJECT_LIMIT = 3   # consecutive rejections before we believe the display

    def _rate_gate(self, value):
        """Blank a reading that changed faster than is physically possible.

        The range gate cannot catch this: glare turning a 1 into a 7 reads 76.2
        instead of 16.2, and 76.2 is a perfectly ordinary temperature. What
        gives it away is the RATE -- 120 deg/min, when a still manages single
        digits.

        Rejected readings keep their raw ssocr string and lose only the value,
        so they appear as misreads in the data rather than vanishing.

        Rejecting cannot be unconditional: if the display genuinely jumps (a
        sensor swapped, a setpoint changed) a strict gate would reject every
        subsequent reading forever, since the baseline never moves. After
        RATE_REJECT_LIMIT consecutive rejections we accept the reading and
        re-baseline -- a sustained new level is evidence, a single spike isn't.
        """
        limit = float(self.service.settings.get("max_rate_per_min", 0) or 0)
        if not value or limit <= 0 or not self.readings:
            return value

        last_time, last_value = self.readings[-1]
        minutes = (time.time() - last_time) / 60.0

        # Never divide by a near-zero gap. A note captures a reading
        # immediately, which can land milliseconds after a scheduled one --
        # against a 40ms gap any change at all computes as thousands of
        # deg/min, and a perfectly good note reading would be thrown away.
        # The scheduled interval is the meaningful sampling period, so use it
        # as the floor: a real glare misread still fails the test, an ordinary
        # note passes it.
        minutes = max(minutes, self.interval / 60.0)
        if minutes <= 0:
            return value

        rate = abs(float(value) - last_value) / minutes
        if rate <= limit:
            # a good reading clears the banner; the count in status keeps the
            # permanent record
            self.rate_reject_streak = 0
            self.last_reject = None
            return value

        self.rate_reject_streak += 1
        if self.rate_reject_streak > self.RATE_REJECT_LIMIT:
            # persistent, so it is probably real -- accept and start over
            print("runner: rate gate accepting {0} after {1} rejections "
                  "(sustained change)".format(value, self.rate_reject_streak))
            self.rate_reject_streak = 0
            self.last_reject = None
            return value

        self.rate_rejects += 1
        self.last_reject = {"value": value, "rate": round(rate, 1)}
        print("runner: rejected {0} -- {1:.0f} deg/min exceeds {2:.0f}".format(
            value, rate, limit))
        return ""

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

        # 3. rate gate fired -- the reading was impossible, not just surprising
        if self.last_reject:
            alerts.append({
                "level": "error",
                "text": "reading {0} rejected: {1:.0f} deg/min is not "
                        "physically possible -- check for glare".format(
                            self.last_reject["value"],
                            self.last_reject["rate"])})

        # 4. plateau, then a rise -- the process-finished signal
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

        A PLATEAU ONLY COUNTS IF A RAMP CAME FIRST. Without that, the first
        flat stretch of any run is the ambient temperature before the heat is
        even on -- it is flat for as long as you like, gets locked in, and then
        an ordinary heat-up ramp clears the rise threshold and reports the
        process as finished at 24 C. Worse, the real plateau is then never
        detected, so the signal that actually matters is lost for the whole
        run.

        Requiring a preceding ramp encodes what the plateau means: the process
        settling after heating, not the room it started in.
        """
        settings = self.service.settings
        rate = self.rate_per_minute()
        ramp_rate = float(settings.get("plateau_ramp_rate", 0.5))
        if rate is not None and abs(rate) >= ramp_rate:
            self.seen_ramp = True

        if self.plateau_value is not None:
            # A plateau left far behind was never the plateau -- it was a
            # shoulder in the ramp. Discard it and keep looking, rather than
            # measuring the rest of the run against a wrong baseline.
            margin = max(3.0 * float(settings.get("alert_rise", 1.0)), 3.0)
            if self.readings and self.readings[-1][1] - self.plateau_value >= margin:
                self.plateau_value = None
            return

        if not self.seen_ramp:
            return

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
            "rate_rejects": self.rate_rejects,
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


def log_sample(csv_path, timestamp, sample_temp="", sample_abv="", note="",
               still_raw="", still_value="", sample_volume=""):
    """Write a collected-sample row at the time it was DRAWN, not now.

    A distillate sample has to cool before a hydrometer reading means anything,
    so the numbers arrive long after the moment they describe. Writing them at
    the current time would put every sample in the wrong place on the chart.

    The row lands out of chronological order in the file, which is harmless:
    the still curve is built only from rows with a `value`, and sample rows
    have none. Markers are positioned by their timestamp, not by file order.
    """
    parts = []
    if sample_volume != "":
        parts.append("{0} ml".format(sample_volume))
    if sample_temp != "":
        parts.append("{0} C".format(sample_temp))
    if sample_abv != "":
        parts.append("{0}% ABV".format(sample_abv))
    # `still_value` is the still temperature -- the vapour temperature the
    # camera reads -- at the moment the sample was drawn. Named explicitly
    # explicitly, because the row now carries two temperatures and confusing
    # them would make the record useless.
    # The still temperature is NOT repeated here: it goes in the row's value
    # column, which is what the chart's temperature column shows. Putting it in
    # the description too just says the same number twice.
    summary = "sample: " + ", ".join(parts) if parts else "sample"
    if note:
        summary += " -- " + note

    # The still reading goes in raw/value, so the sample also appears as an
    # ordinary point on the temperature curve rather than a gap in it.
    append_row(csv_path, {"timestamp": timestamp,
                          "raw": still_raw, "value": still_value,
                          "note": summary,
                          "sample_volume": sample_volume,
                          "sample_temp": sample_temp,
                          "sample_abv": sample_abv})
    return {"time": timestamp, "text": summary, "still_value": still_value,
            "sample_volume": sample_volume,
            "sample_temp": sample_temp, "sample_abv": sample_abv}


def first_timestamp(csv_path):
    """Epoch seconds of the first real reading in a run, or None.

    Manual resume needs the ORIGINAL start, not now: the schedule grid and the
    elapsed/duration figures are all measured from it.
    """
    try:
        with open(csv_path) as fh:
            for row in csv.DictReader(fh):
                stamp = row.get("timestamp") or ""
                try:
                    return datetime.datetime.strptime(
                        stamp, "%Y-%m-%dT%H:%M:%S.%f").timestamp()
                except (ValueError, OverflowError):
                    continue
    except OSError:
        return None
    return None


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
