"""
lumawatch.schedule_learn — learns YOUR actual "night starts around HH:MM" /
"day starts around HH:MM" times from your own manual brightness adjustments,
instead of requiring the fixed night_start_hour/day_start_hour constants in
lumawatch/ambient.py to be hand-edited in source code.

Reuses the same raw log as lumawatch.learn (~/.lumawatch_history.jsonl) --
no new raw data collection needed, since every manual brightness change
(slider, hotkey, physical monitor button) is already being recorded there.
This module just analyzes that data differently: instead of an hour-by-hour
average (lumawatch.learn), it fits two robust (Theil-Sen) regressions -- one
over a candidate "evening decline" window, one over a candidate "morning
rise" window -- and finds where each line crosses the midpoint between your
typical day and night brightness. That crossing point is reported back as
an actual clock time.

Why two windows instead of one 24-hour fit: hour-of-day isn't a normal
number line for regression -- 11pm and 1am are 2 hours apart in reality but
22 apart on a raw 0-23 scale, and the true day -> night -> day pattern is a
smooth dip, not a straight line, so a single linear regression across all
24 hours would badly misfit it. Splitting into two known-monotonic windows
(one declining, one rising) sidesteps that without needing a fancier
periodic/harmonic regression model.

Local-only, as with every other learning module here: reads the local
history log, writes ~/.lumawatch_schedule_fit.json. No network access.

This needs meaningfully more real-world time to become trusted than
lumawatch.calibration does -- you can fill calibration's 30 samples in a
few minutes by dragging a slider around, but this needs genuine samples
spread across your actual evening/morning/day/night hours, which only
accumulate as you really use your computer at those times.
"""

import json
import datetime
import statistics
from pathlib import Path

from . import history
from ._robust import theil_sen

FIT_PATH = Path.home() / ".lumawatch_schedule_fit.json"

DAY_WINDOW = (12, 16)       # steady "day" reference hours (noon-4pm)
NIGHT_WINDOW = (2, 4)       # steady "night" reference hours (2am-4am)
EVENING_START = 16          # evening window: 16:00 through next 02:00 (10 hours)
EVENING_SPAN = 10
MORNING_START = 4           # morning window: 04:00 through 12:00 (8 hours)
MORNING_SPAN = 8

MIN_STEADY_SAMPLES = 8       # manual samples needed in the day/night reference windows
MIN_TRANSITION_SAMPLES = 10  # manual samples needed in each transition window
MIN_SIDE_SAMPLES = 4         # samples needed on EACH side of a candidate breakpoint to try it
MIN_DECLINE_SLOPE = -0.5     # brightness %/hour; must decline at least this much to trust an evening fit
MIN_RISE_SLOPE = 0.5         # brightness %/hour; must rise at least this much to trust a morning fit
FLOOR_FACTOR = 0.4           # never dim the ceiling factor below this -- matches lumawatch.learn


def _find_breakpoint(points, anchor_level, span, min_side_samples=MIN_SIDE_SAMPLES):
    """Grid-searches candidate breakpoints splitting a window [0, span)
    into a 'still near anchor_level' side and a 'transitioning' side, fits
    Theil-Sen only on the transitioning side, and keeps whichever
    breakpoint minimizes total squared error across both sides.

    This exists because a single straight line across the *whole* window
    gets diluted by however much of it is actually still flat -- e.g. if
    you don't start winding down until 9pm, a line fit across the full
    4pm-2am window would badly underestimate how sharp (and how late) the
    real decline is. Searching for where the flat part actually ends fixes
    that. Returns None if no candidate has enough points on both sides.
    """
    paired = sorted(points)
    best = None
    best_error = None
    for x_b in range(1, span):
        before = [(x, y) for x, y in paired if x < x_b]
        after = [(x, y) for x, y in paired if x >= x_b]
        if len(before) < min_side_samples or len(after) < min_side_samples:
            continue
        after_xs = [p[0] for p in after]
        after_ys = [p[1] for p in after]
        fit = theil_sen(after_xs, after_ys)
        if fit is None:
            continue
        slope, intercept = fit
        error = sum((y - anchor_level) ** 2 for _, y in before)
        error += sum((y - (slope * x + intercept)) ** 2 for x, y in after)
        if best_error is None or error < best_error:
            best_error = error
            best = {
                "breakpoint_x": x_b,
                "slope": slope,
                "intercept": intercept,
                "x_min": min(after_xs),
                "x_max": max(after_xs),
                "n": len(points),
            }
    return best


def _manual_samples(monitor_id):
    events = history.load_events()
    return [e for e in events
            if e.get("monitor_id") == monitor_id and e.get("source") in ("manual_ui", "manual_hw")]


def _hour_in(hour, window):
    lo, hi = window
    return lo <= hour < hi


def _in_evening_window(hour):
    """True for 16:00 through next-day 02:00 -- the one window that wraps
    past midnight, so it can't use the plain _hour_in() range test."""
    wrap_end = (EVENING_START + EVENING_SPAN) % 24
    return hour >= EVENING_START or hour < wrap_end


def _evening_x(hour):
    """Hours since 16:00, continuing past midnight as 24, 25, ... instead
    of wrapping back to 0 -- this is what keeps the evening window a
    genuine monotonic axis for regression."""
    return hour - EVENING_START if hour >= EVENING_START else hour + 24 - EVENING_START


def _morning_x(hour):
    return hour - MORNING_START


def _load_fits():
    if not FIT_PATH.exists():
        return {}
    try:
        with open(FIT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        print(f"[schedule_learn] failed to load fits: {exc}")
        return {}


def _save_fits(data):
    try:
        with open(FIT_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        print(f"[schedule_learn] failed to save fits: {exc}")


def _fit_one(monitor_id):
    samples = _manual_samples(monitor_id)
    if not samples:
        return None

    day_vals = [s["brightness"] for s in samples if _hour_in(s["hour"], DAY_WINDOW)]
    night_vals = [s["brightness"] for s in samples if _hour_in(s["hour"], NIGHT_WINDOW)]
    evening_pts = [(_evening_x(s["hour"]), s["brightness"]) for s in samples
                   if _in_evening_window(s["hour"])]
    morning_pts = [(_morning_x(s["hour"]), s["brightness"]) for s in samples
                   if _hour_in(s["hour"], (MORNING_START, MORNING_START + MORNING_SPAN))]

    if (len(day_vals) < MIN_STEADY_SAMPLES or len(night_vals) < MIN_STEADY_SAMPLES
            or len(evening_pts) < MIN_TRANSITION_SAMPLES or len(morning_pts) < MIN_TRANSITION_SAMPLES):
        return None

    day_level = statistics.median(day_vals)
    night_level = statistics.median(night_vals)
    if day_level <= night_level:
        # No real day/night contrast in your habits yet -- nothing sensible
        # to learn a transition time from.
        return None

    eve_break = _find_breakpoint(evening_pts, day_level, EVENING_SPAN)
    morn_break = _find_breakpoint(morning_pts, night_level, MORNING_SPAN)

    night_start_hour = None
    evening_info = None
    if eve_break is not None and eve_break["slope"] <= MIN_DECLINE_SLOPE:
        night_start_hour = round((EVENING_START + eve_break["breakpoint_x"]) % 24, 2)
        evening_info = eve_break

    day_start_hour = None
    morning_info = None
    if morn_break is not None and morn_break["slope"] >= MIN_RISE_SLOPE:
        day_start_hour = round((MORNING_START + morn_break["breakpoint_x"]) % 24, 2)
        morning_info = morn_break

    return {
        "day_level": round(day_level, 1),
        "night_level": round(night_level, 1),
        "night_start_hour": night_start_hour,
        "day_start_hour": day_start_hour,
        "evening": evening_info,
        "morning": morning_info,
        "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def recompute(monitor_ids):
    """Recomputes the learned schedule for each given monitor id. Purely
    local; reads history, writes the schedule fit file. Cheap enough to
    call every 20-30 minutes alongside lumawatch.learn's recompute."""
    fits = _load_fits()
    for monitor_id in monitor_ids:
        fit = _fit_one(monitor_id)
        if fit is None:
            fits.pop(monitor_id, None)
        else:
            fits[monitor_id] = fit
    _save_fits(fits)
    return fits


def get_fit(monitor_id):
    return _load_fits().get(monitor_id)


def predicted_brightness(monitor_id, hour):
    """Continuous predicted 'natural' brightness at a given hour (float
    hours, e.g. 22.75), using the learned piecewise day/evening/night/
    morning curve. Returns None if not trusted yet for this monitor."""
    fit = get_fit(monitor_id)
    if fit is None:
        return None

    day_level, night_level = fit["day_level"], fit["night_level"]

    if _hour_in(hour, DAY_WINDOW):
        return day_level
    if _hour_in(hour, NIGHT_WINDOW):
        return night_level
    if _in_evening_window(hour):
        e = fit["evening"]
        x = _evening_x(hour)
        if e is None or x < e["breakpoint_x"]:
            return day_level  # still in the flat part, before your decline actually starts
        x = min(x, e["x_max"])
        return min(day_level, max(night_level, e["slope"] * x + e["intercept"]))
    if _hour_in(hour, (MORNING_START, MORNING_START + MORNING_SPAN)):
        m = fit["morning"]
        x = _morning_x(hour)
        if m is None or x < m["breakpoint_x"]:
            return night_level  # still in the flat part, before your rise actually starts
        x = min(x, m["x_max"])
        return min(day_level, max(night_level, m["slope"] * x + m["intercept"]))

    return day_level  # the four windows cover all 24 hours; this is just a safe fallback


def learned_schedule_ceiling_factor(monitor_id, hour=None, now=None):
    """A ceiling multiplier in [FLOOR_FACTOR, 1.0], derived from your
    learned personal schedule rather than a fixed clock-based one. Returns
    1.0 (no effect) if not trusted yet -- callers should treat that as
    'this signal has nothing to say', not 'definitely daytime'."""
    if hour is None:
        n = now or datetime.datetime.now()
        hour = n.hour + n.minute / 60.0
    predicted = predicted_brightness(monitor_id, hour)
    fit = get_fit(monitor_id)
    if predicted is None or fit is None or fit["day_level"] <= 0:
        return 1.0
    factor = predicted / fit["day_level"]
    return max(FLOOR_FACTOR, min(1.0, factor))


def get_learned_times(monitor_id):
    """(night_start_hour, day_start_hour) as floats or None each -- for
    display purposes, e.g. 22.75 means 10:45 PM. Use format_hour() to
    render them."""
    fit = get_fit(monitor_id)
    if fit is None:
        return None, None
    return fit.get("night_start_hour"), fit.get("day_start_hour")


def format_hour(hour_float):
    """22.75 -> '10:45 PM'. For UI display; returns a plain-English
    placeholder if nothing's been learned yet."""
    if hour_float is None:
        return "not learned yet"
    total_minutes = round(hour_float * 60) % (24 * 60)
    h, m = divmod(total_minutes, 60)
    period = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {period}"


def clear_schedule(monitor_id=None):
    """Clears learned schedule data. monitor_id=None clears every monitor;
    otherwise clears just that one. Does not touch the raw history log --
    that's shared with lumawatch.learn and cleared via history.clear_all()."""
    if monitor_id is None:
        _save_fits({})
        return
    fits = _load_fits()
    fits.pop(monitor_id, None)
    _save_fits(fits)
