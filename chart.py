#!/usr/bin/env python3
"""
chart.py -- render a run CSV as a self-contained HTML chart.

SVG is built by hand rather than with matplotlib (not installed on this Pi, and
heavy on a Pi 3) or a vendored JS chart library (200 KB per saved file, and a
pinned blob to carry forever). A hand-built SVG is a few KB, renders instantly,
and will still open in a browser in ten years.

Hover tooltips use SVG <title> elements, which browsers render natively -- no
JavaScript at all, so a saved chart is a genuinely static artefact.

SD-wear note: rendering never touches the disk. The service serves the live
chart from memory; files are only written on an explicit "Save chart" or once
when a run stops.

Legacy-Python 3.5 compatible.
"""

import csv
import datetime
import math
import os


LINE_COLOUR = "#c0392b"        # from the proven test plot
GRID_COLOUR = "#d8d8d8"
AXIS_COLOUR = "#888888"
TEXT_COLOUR = "#333333"
NOTE_COLOUR = "#2c6fbb"
TARGET_COLOUR = "#2e8b57"
PLATEAU_COLOUR = "#b08000"

WIDTH = 940
HEIGHT = 400
MARGIN_LEFT = 58
MARGIN_RIGHT = 18
MARGIN_TOP = 18
MARGIN_BOTTOM = 44

# candidate axis steps, in degrees
Y_STEPS = [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100]


def _escape(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _parse_timestamp(value):
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.datetime.strptime(value, fmt)
        except (ValueError, TypeError):
            continue
    return None


def load_run(path):
    """Read a run CSV into points and notes.

    Rows with a blank value are dropped from the curve (they are misreads), but
    a row with a note is always kept as a note -- including the blank-value
    marker rows the runner writes on resume and stop.
    """
    points = []
    notes = []
    with open(path) as fh:
        for row in csv.DictReader(fh):
            stamp = _parse_timestamp(row.get("timestamp", ""))
            if stamp is None:
                continue
            raw_value = (row.get("value") or "").strip()
            note = (row.get("note") or "").strip()
            value = None
            if raw_value:
                try:
                    value = float(raw_value)
                except ValueError:
                    value = None
            if value is not None:
                points.append((stamp, value))
            if note:
                notes.append({"time": stamp, "value": value, "text": note})
    # Number the notes here, once. The marker numbers appear on the full
    # chart, on the zoomed detail chart and in the table, and the detail
    # chart draws a SUBSET -- so anything that renumbers per-chart makes
    # marker 1 in the detail panel mean note 5 everywhere else.
    for position, note in enumerate(notes, start=1):
        note["index"] = position
    return {"points": points, "notes": notes,
            "name": os.path.basename(path)}


# Markers the service writes about itself, rather than about the process.
SYSTEM_NOTES = ("run stopped", "logging resumed",
                "service restarted -- logging resumed",
                "run complete (duration reached)")


def filter_notes(run, show_system=False):
    """Drop the service's own markers, and renumber what is left.

    Renumbering matters: the numbers are shared by both charts and the table,
    so filtering without renumbering leaves visible gaps (1, 2, 4, 5) that look
    like missing notes.
    """
    if show_system:
        return run
    notes = [n for n in run["notes"]
             if (n.get("text") or "").strip() not in SYSTEM_NOTES]
    for position, note in enumerate(notes, start=1):
        note = dict(note)
        note["index"] = position
        notes[position - 1] = note
    trimmed = dict(run)
    trimmed["notes"] = notes
    return trimmed


def _nice_y_axis(low, high):
    """Pick a readable y range and tick step.

    A still holding steady is a near-flat line, so a zero span has to produce a
    sane axis rather than dividing by zero.
    """
    if low == high:
        low -= 0.5
        high += 0.5
    span = high - low
    target_ticks = 6
    raw_step = span / float(target_ticks)
    step = Y_STEPS[-1]
    for candidate in Y_STEPS:
        if candidate >= raw_step:
            step = candidate
            break
    # floor/ceil, not int(). int() truncates towards zero, so a minimum of
    # 16.1 with a step of 20 gave int(0.8) - 1 = -1, an axis starting at -20
    # on a chart that never goes below 16 -- a quarter of the height wasted.
    axis_low = step * math.floor(low / step)
    axis_high = step * math.ceil(high / step)
    if axis_high <= axis_low:
        axis_high = axis_low + step
    return axis_low, axis_high, step


def _format_tick(value, step):
    if step < 1:
        return "%.1f" % value
    return "%.0f" % value if float(value).is_integer() else "%.1f" % value


def render_svg(run, settings=None, plateau=None, y_range=None):
    """Return the chart as an SVG string.

    y_range forces the vertical extent; without it the readings set it.
    """
    settings = settings or {}
    points = run["points"]
    plot_w = WIDTH - MARGIN_LEFT - MARGIN_RIGHT
    plot_h = HEIGHT - MARGIN_TOP - MARGIN_BOTTOM

    if not points:
        return ('<svg xmlns="http://www.w3.org/2000/svg" width="{0}" '
                'height="{1}"><text x="{2}" y="{3}" text-anchor="middle" '
                'font-family="system-ui,sans-serif" font-size="14" '
                'fill="{4}">no readings yet</text></svg>').format(
                    WIDTH, HEIGHT, WIDTH // 2, HEIGHT // 2, TEXT_COLOUR)

    times = [p[0] for p in points]
    values = [p[1] for p in points]

    # The time axis has to cover the notes as well as the readings. A sample is
    # marked when it is drawn, which can be a moment before the first reading
    # or after the last -- and anything outside the range is not merely
    # mispositioned, it is dropped entirely and silently.
    note_times = [n["time"] for n in run["notes"] if n.get("time")]
    t0 = min([times[0]] + note_times)
    t1 = max([times[-1]] + note_times)
    span_seconds = (t1 - t0).total_seconds() or 1.0

    target = float(settings.get("target_temp", 0) or 0)
    low, high = min(values), max(values)

    # The data sets the scale. Forcing the target line into range wrecks the
    # chart whenever the two are far apart: an ambient run at 16 C with a
    # target of 78.3 produced an axis of -20..80, drawing the readings as a
    # flat line squashed into the bottom few percent. The target line appears
    # once the readings approach it, and the caption says so meanwhile.
    #
    # The plateau IS included, because it is derived from the readings and so
    # is always near them.
    if plateau is not None:
        low = min(low, plateau)
        high = max(high, plateau)
    if y_range:
        low, high = float(y_range[0]), float(y_range[1])
    axis_low, axis_high, step = _nice_y_axis(low, high)
    axis_span = axis_high - axis_low

    def sx(when):
        return MARGIN_LEFT + ((when - t0).total_seconds() / span_seconds) * plot_w

    def sy(value):
        return MARGIN_TOP + plot_h - ((value - axis_low) / axis_span) * plot_h

    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="{0}" height="{1}" '
           'viewBox="0 0 {0} {1}" font-family="system-ui,sans-serif">'
           .format(WIDTH, HEIGHT)]
    out.append('<rect width="{0}" height="{1}" fill="#ffffff"/>'
               .format(WIDTH, HEIGHT))

    # -- horizontal grid + y labels
    ticks = int(round(axis_span / step))
    for index in range(ticks + 1):
        value = axis_low + index * step
        y = sy(value)
        out.append('<line x1="{0}" y1="{1:.1f}" x2="{2}" y2="{1:.1f}" '
                   'stroke="{3}" stroke-width="1"/>'
                   .format(MARGIN_LEFT, y, WIDTH - MARGIN_RIGHT, GRID_COLOUR))
        out.append('<text x="{0}" y="{1:.1f}" text-anchor="end" font-size="11" '
                   'fill="{2}">{3}</text>'
                   .format(MARGIN_LEFT - 8, y + 4, TEXT_COLOUR,
                           _format_tick(value, step)))

    # -- x labels: aim for ~7, always on real reading times
    label_count = min(7, len(points))
    seen = set()
    for index in range(label_count):
        position = int(index * (len(points) - 1) / max(1, label_count - 1))
        when = times[position]
        label = when.strftime("%H:%M")
        if label in seen:
            continue
        seen.add(label)
        x = sx(when)
        out.append('<line x1="{0:.1f}" y1="{1}" x2="{0:.1f}" y2="{2}" '
                   'stroke="{3}" stroke-width="1"/>'
                   .format(x, MARGIN_TOP, MARGIN_TOP + plot_h, GRID_COLOUR))
        out.append('<text x="{0:.1f}" y="{1}" text-anchor="middle" '
                   'font-size="11" fill="{2}">{3}</text>'
                   .format(x, MARGIN_TOP + plot_h + 16, TEXT_COLOUR, label))

    # -- axes
    out.append('<line x1="{0}" y1="{1}" x2="{0}" y2="{2}" stroke="{3}"/>'
               .format(MARGIN_LEFT, MARGIN_TOP, MARGIN_TOP + plot_h,
                       AXIS_COLOUR))
    out.append('<line x1="{0}" y1="{1}" x2="{2}" y2="{1}" stroke="{3}"/>'
               .format(MARGIN_LEFT, MARGIN_TOP + plot_h, WIDTH - MARGIN_RIGHT,
                       AXIS_COLOUR))

    # -- target temperature line
    if target and axis_low <= target <= axis_high:
        y = sy(target)
        out.append('<line x1="{0}" y1="{1:.1f}" x2="{2}" y2="{1:.1f}" '
                   'stroke="{3}" stroke-width="1.5" stroke-dasharray="6 4"/>'
                   .format(MARGIN_LEFT, y, WIDTH - MARGIN_RIGHT,
                           TARGET_COLOUR))
        out.append('<text x="{0}" y="{1:.1f}" text-anchor="end" font-size="10" '
                   'fill="{2}">target {3:.1f}</text>'
                   .format(WIDTH - MARGIN_RIGHT - 4, y - 4, TARGET_COLOUR,
                           target))

    # -- plateau line
    if plateau is not None and axis_low <= plateau <= axis_high:
        y = sy(plateau)
        out.append('<line x1="{0}" y1="{1:.1f}" x2="{2}" y2="{1:.1f}" '
                   'stroke="{3}" stroke-width="1" stroke-dasharray="2 4"/>'
                   .format(MARGIN_LEFT, y, WIDTH - MARGIN_RIGHT,
                           PLATEAU_COLOUR))
        out.append('<text x="{0}" y="{1:.1f}" font-size="10" fill="{2}">'
                   'plateau {3:.1f}</text>'
                   .format(MARGIN_LEFT + 4, y - 4, PLATEAU_COLOUR, plateau))

    # -- the curve
    coords = " ".join("{0:.1f},{1:.1f}".format(sx(t), sy(v))
                      for t, v in points)
    out.append('<polyline points="{0}" fill="none" stroke="{1}" '
               'stroke-width="1.8" stroke-linejoin="round"/>'
               .format(coords, LINE_COLOUR))

    # Native tooltips via <title>. Only mark points on short runs -- a 4-hour
    # run at 20s is 720 points and a circle each would bloat the file for no
    # readability gain.
    if len(points) <= 240:
        for when, value in points:
            out.append('<circle cx="{0:.1f}" cy="{1:.1f}" r="2" fill="{2}">'
                       '<title>{3}  {4:.1f} &#176;C</title></circle>'
                       .format(sx(when), sy(value), LINE_COLOUR,
                               when.strftime("%H:%M:%S"), value))

    # -- note markers, numbered to match the list below
    for position, note in enumerate(run["notes"], start=1):
        index = note.get("index", position)
        when = note["time"]
        if when < t0 or when > t1:
            continue
        x = sx(when)
        y = sy(note["value"]) if note["value"] is not None else MARGIN_TOP + 10
        out.append('<line x1="{0:.1f}" y1="{1}" x2="{0:.1f}" y2="{2}" '
                   'stroke="{3}" stroke-width="1" stroke-dasharray="3 3" '
                   'opacity="0.7"/>'
                   .format(x, MARGIN_TOP, MARGIN_TOP + plot_h, NOTE_COLOUR))
        out.append('<circle cx="{0:.1f}" cy="{1:.1f}" r="7" fill="{2}">'
                   '<title>{3}  {4}</title></circle>'
                   .format(x, y, NOTE_COLOUR, when.strftime("%H:%M:%S"),
                           _escape(note["text"])))
        out.append('<text x="{0:.1f}" y="{1:.1f}" text-anchor="middle" '
                   'font-size="9" fill="#ffffff">{2}</text>'
                   .format(x, y + 3, index))

    # -- caption
    duration = (t1 - t0).total_seconds() / 60.0
    caption = ("{0} readings  ·  {1:.1f}–{2:.1f} °C  ·  "
               "{3:.0f} min").format(len(points), min(values), max(values),
                                     duration)
    # say where the target is when it is off the chart, so its absence from
    # the plot never reads as "no target set"
    if target and not (axis_low <= target <= axis_high):
        caption += "  ·  target {0:.1f} °C {1} this range".format(
            target, "above" if target > axis_high else "below")
    out.append('<text x="{0}" y="{1}" font-size="11" fill="{2}">{3}</text>'
               .format(MARGIN_LEFT, HEIGHT - 8, TEXT_COLOUR, caption))
    out.append('<text x="{0}" y="{1}" text-anchor="end" font-size="11" '
               'fill="{2}">°C</text>'
               .format(MARGIN_LEFT - 8, MARGIN_TOP - 4, TEXT_COLOUR))
    out.append("</svg>")
    return "".join(out)


def detail_run(run, settings=None, plateau=None):
    """A second chart covering only the readings near the plateau.

    On a distillation the ramp from ambient owns most of the vertical range --
    a run from 20 C to 79 C puts every reading that matters, the fluctuation
    around the plateau, inside the top few percent of the chart where it cannot
    be read. A logarithmic axis makes that worse rather than better: it expands
    the low end and compresses the high end, which is backwards here.

    So the full run is still drawn, and a zoomed panel is added underneath.
    Returns None when there is nothing worth zooming into.
    """
    settings = settings or {}
    points = run["points"]
    if len(points) < 20:
        return None

    values = [v for _, v in points]
    span = max(values) - min(values)
    band = float(settings.get("chart_detail_band", 3.0))
    if span <= band * 2:
        return None                       # already readable

    focus = plateau
    if focus is None:
        target = float(settings.get("target_temp", 0) or 0)
        focus = target if target and min(values) <= target <= max(values) \
            else max(values)

    near = [(t, v) for t, v in points if abs(v - focus) <= band]
    if len(near) < 10:
        return None

    # The band picks the PERIOD, not the points. Filtering by value would drop
    # any excursion outside it -- and an excursion is exactly what you want to
    # see in a zoomed view. Take every reading between the first and last time
    # the run was near the focus, so spikes stay on the curve and the axis
    # grows to fit them.
    t0, t1 = near[0][0], near[-1][0]
    window = [(t, v) for t, v in points if t0 <= t <= t1]
    notes = [n for n in run["notes"] if n.get("time") and t0 <= n["time"] <= t1]
    return {"points": window, "notes": notes, "name": run["name"],
            "focus": focus}


HTML_TEMPLATE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  body{font-family:system-ui,sans-serif;margin:24px;color:#222;background:#fff;}
  @media (max-width:760px){ body{margin:12px;} h1{font-size:15px;} }
  h1{font-size:17px;margin:0 0 2px;}
  .sub{color:#666;font-size:12px;margin-bottom:14px;}
  .chart{overflow-x:auto;}
  .detail{margin-top:22px;}
  .detail h2{font-size:13px;color:#444;margin:0 0 4px;font-weight:600;}
  .detail .why{color:#777;font-size:11px;margin-bottom:6px;}
  svg{border:1px solid #e0e0e0;max-width:100%;height:auto;}
  table{border-collapse:collapse;margin-top:18px;font-size:13px;}
  th,td{text-align:left;padding:4px 12px 4px 0;
        border-bottom:1px solid #eee;}
  th{color:#666;font-weight:600;font-size:11px;text-transform:uppercase;
     letter-spacing:.5px;}
  td.n{color:#fff;background:#2c6fbb;text-align:center;width:20px;
       border-radius:9px;padding:2px 0;font-size:11px;}
  td.v{font-variant-numeric:tabular-nums;}
  .empty{color:#888;font-style:italic;}
</style></head><body>
<h1>__TITLE__</h1>
<div class="sub">__SUBTITLE__</div>
<div class="chart">__SVG__</div>
__DETAIL__
__NOTES__
</body></html>"""


def render_html(run, settings=None, plateau=None, generated=None,
                show_system=False):
    """Self-contained HTML: inline SVG, notes listed below, no external files."""
    settings = settings or {}
    run = filter_notes(run, show_system)
    svg = render_svg(run, settings, plateau)
    points = run["points"]

    zoom = detail_run(run, settings, plateau)
    if zoom:
        band = float((settings or {}).get("chart_detail_band", 3.0))
        detail_html = (
            '<div class="detail"><h2>Detail near {0:.1f} &#176;C</h2>'
            '<div class="why">The full run above is dominated by the climb '
            'from ambient. This panel covers the {1} readings from when it '
            'first came within &#177;{2:.0f} &#176;C of that, so the '
            'fluctuation is readable -- excursions included.</div>'
            '<div class="chart">{3}</div></div>').format(
                zoom["focus"], len(zoom["points"]), band,
                render_svg(zoom, settings, plateau))
    else:
        detail_html = ""

    if points:
        subtitle = "{0} → {1}".format(
            points[0][0].strftime("%Y-%m-%d %H:%M:%S"),
            points[-1][0].strftime("%Y-%m-%d %H:%M:%S"))
    else:
        subtitle = "no readings"
    stamp = generated or datetime.datetime.now()
    subtitle += "  ·  generated {0}".format(
        stamp.strftime("%Y-%m-%d %H:%M"))

    if run["notes"]:
        rows = ["<table><tr><th></th><th>time</th><th>temp</th>"
                "<th>note</th></tr>"]
        for position, note in enumerate(run["notes"], start=1):
            index = note.get("index", position)
            rows.append(
                '<tr><td class="n">{0}</td><td class="v">{1}</td>'
                '<td class="v">{2}</td><td>{3}</td></tr>'.format(
                    index, note["time"].strftime("%H:%M:%S"),
                    ("%.1f °C" % note["value"])
                    if note["value"] is not None else "—",
                    _escape(note["text"])))
        rows.append("</table>")
        notes_html = "".join(rows)
    else:
        notes_html = '<p class="empty">No notes recorded.</p>'

    html = HTML_TEMPLATE
    html = html.replace("__TITLE__", _escape(run["name"]))
    html = html.replace("__SUBTITLE__", _escape(subtitle))
    html = html.replace("__SVG__", svg)
    html = html.replace("__DETAIL__", detail_html)
    html = html.replace("__NOTES__", notes_html)
    return html


def html_path_for(csv_path):
    """Charts share the CSV's basename, per the spec."""
    base, _ = os.path.splitext(csv_path)
    return base + ".html"


def save_html(csv_path, settings=None, plateau=None, show_system=False):
    """Write the chart next to its CSV. Deliberate, occasional writes only."""
    run = load_run(csv_path)
    html = render_html(run, settings, plateau, show_system=show_system)
    path = html_path_for(csv_path)
    with open(path, "w") as fh:
        fh.write(html)
    return path
