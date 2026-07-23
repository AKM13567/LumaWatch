"""
lumawatch.calibration — replaces "the last manual sample overwrites the
whole baseline" with a proper regression between screen content brightness
(light_level, the same 0-1 center-weighted white_ratio the engine already
computes each frame) and the brightness you actually set manually.

The fit is Theil-Sen (the median of all pairwise slopes between sample
points), not plain least-squares. It has a much higher breakdown point --
roughly 29% of points would need to be outliers before the slope gets
dragged off course, versus effectively 0% for ordinary least squares --
which directly targets "one weird manual adjustment shouldn't wreck the
whole model."

Local-only: reads/writes ~/.lumawatch_calibration.jsonl (raw samples) and
~/.lumawatch_calibration_fit.json (the fitted line per monitor). No network
access, ever. This is a distinct concern from lumawatch.learn (which learns
*time-of-day* preference) -- this module learns *content-brightness*
preference.

Only genuinely manual brightness-setting events should ever be recorded
here (slider, hotkey, physical monitor button) -- never auto-engine output.
That's what makes this a measurement of what you actually want, rather than
a reflection of the engine's own existing heuristics.
"""

import json
import datetime
from pathlib import Path

from ._robust import theil_sen

CALIBRATION_PATH = Path.home() / ".lumawatch_calibration.jsonl"
FIT_PATH = Path.home() / ".lumawatch_calibration_fit.json"

MIN_SAMPLES = 30
MIN_X_SPREAD = 0.15  # observed light-level range must span at least this much (0-1 scale)
                      # or the slope is numerically unstable / not a meaningful correlation
MAX_SAMPLES_PER_MONITOR = 2000  # safety cap so this can't grow unbounded
THEIL_SEN_MAX_N = 200  # cap on points actually used for the O(n^2) pairwise-slope
                        # computation -- keeps a single fit fast even after months of
                        # data, at the cost of only looking at the most recent 200
                        # manual samples for the slope/intercept itself. Fit quality
                        # (r2) below is still computed against the full history.


def record_sample(monitor_id, light_level, brightness, source, ts=None):
    """Append one (light_level, brightness) observation for one monitor.
    `source` should be 'manual_ui' or 'manual_hw' -- never 'auto'."""
    ts = ts or datetime.datetime.now(datetime.timezone.utc)
    entry = {
        "ts": ts.isoformat(),
        "monitor_id": monitor_id,
        "light_level": round(float(light_level), 4),
        "brightness": round(float(brightness), 1),
        "source": source,
    }
    try:
        with open(CALIBRATION_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as exc:
        print(f"[calibration] failed to record sample: {exc}")


def load_samples(monitor_id=None):
    if not CALIBRATION_PATH.exists():
        return []
    samples = []
    try:
        with open(CALIBRATION_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if monitor_id is None or ev.get("monitor_id") == monitor_id:
                    samples.append(ev)
    except Exception as exc:
        print(f"[calibration] failed to load samples: {exc}")
    return samples


def _prune_monitor_samples(monitor_id, max_samples=MAX_SAMPLES_PER_MONITOR):
    """Keeps the file from growing forever -- caps each monitor to its most
    recent max_samples points. Rewrites the whole file, so don't call this
    on every single sample; record_sample only triggers it occasionally."""
    all_samples = load_samples()
    by_monitor = {}
    for ev in all_samples:
        by_monitor.setdefault(ev.get("monitor_id"), []).append(ev)

    trimmed = by_monitor.get(monitor_id, [])
    if len(trimmed) <= max_samples:
        return
    by_monitor[monitor_id] = trimmed[-max_samples:]

    rebuilt = [ev for evs in by_monitor.values() for ev in evs]
    try:
        with open(CALIBRATION_PATH, "w", encoding="utf-8") as f:
            for ev in rebuilt:
                f.write(json.dumps(ev) + "\n")
    except Exception as exc:
        print(f"[calibration] failed to prune: {exc}")


def _load_fits():
    if not FIT_PATH.exists():
        return {}
    try:
        with open(FIT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        print(f"[calibration] failed to load fits: {exc}")
        return {}


def _save_fits(data):
    try:
        with open(FIT_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        print(f"[calibration] failed to save fits: {exc}")


def fit_calibration(monitor_id, min_samples=MIN_SAMPLES, min_x_spread=MIN_X_SPREAD):
    """(Re)fits the light_level -> brightness line for one monitor from its
    manual samples, via Theil-Sen (see module docstring for why). Returns
    the fit dict, or None if there isn't enough (or varied enough) data to
    trust yet -- in which case any previously-saved fit for this monitor is
    cleared, so a monitor can't stay "trusted" on stale grounds if you e.g.
    clear data."""
    samples = load_samples(monitor_id)
    fits = _load_fits()

    if len(samples) < min_samples:
        fits.pop(monitor_id, None)
        _save_fits(fits)
        return None

    xs = [s["light_level"] for s in samples]
    ys = [s["brightness"] for s in samples]
    n = len(xs)

    x_min, x_max = min(xs), max(xs)
    if (x_max - x_min) < min_x_spread:
        # You've only ever manually adjusted at a narrow range of content
        # brightness -- not enough spread to know how brightness *should*
        # change with light level. Don't fabricate a slope from that.
        fits.pop(monitor_id, None)
        _save_fits(fits)
        return None

    fitted = theil_sen(xs, ys, max_n=THEIL_SEN_MAX_N)
    if fitted is None:
        fits.pop(monitor_id, None)
        _save_fits(fits)
        return None
    slope, intercept = fitted

    # Fit quality (r2) is reported against the FULL sample set, even if the
    # slope/intercept above were computed from a recency-capped subsample.
    mean_y = sum(ys) / n
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    if ss_tot == 0:
        r2 = 0.0
    else:
        ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
        r2 = max(0.0, 1.0 - ss_res / ss_tot)

    fit = {
        "slope": slope,
        "intercept": intercept,
        "n": n,
        "x_min": x_min,
        "x_max": x_max,
        "r2": round(r2, 3),
        "method": "theil-sen",
        "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    fits[monitor_id] = fit
    _save_fits(fits)

    # Occasional light housekeeping -- cheap check, expensive-ish rewrite only when needed.
    if n > MAX_SAMPLES_PER_MONITOR:
        _prune_monitor_samples(monitor_id)

    return fit


def get_fit(monitor_id):
    """Returns the saved fit dict for a monitor, or None if it isn't
    trusted/available yet. Doesn't recompute anything -- just reads the
    last fit_calibration() result."""
    return _load_fits().get(monitor_id)


def predict(monitor_id, light_level):
    """Predicted brightness for a given light_level, using the trusted fit
    for this monitor. Returns None if there's no trusted fit yet -- callers
    should fall back to the old normal/dim two-point system in that case.
    Clamps light_level to the range you've actually manually calibrated
    across, so the line is never extrapolated into content-brightness
    levels you've never actually reacted to."""
    fit = get_fit(monitor_id)
    if fit is None:
        return None
    x = min(max(light_level, fit["x_min"]), fit["x_max"])
    y = fit["slope"] * x + fit["intercept"]
    return max(0.0, min(100.0, y))


def clear_calibration(monitor_id=None):
    """Clears calibration data. monitor_id=None clears everything (all
    monitors' raw samples and fits); otherwise clears just that monitor."""
    fits = _load_fits()
    if monitor_id is None:
        try:
            if CALIBRATION_PATH.exists():
                CALIBRATION_PATH.unlink()
        except Exception as exc:
            print(f"[calibration] failed to clear samples: {exc}")
        _save_fits({})
        return

    fits.pop(monitor_id, None)
    _save_fits(fits)
    remaining = [s for s in load_samples() if s.get("monitor_id") != monitor_id]
    try:
        with open(CALIBRATION_PATH, "w", encoding="utf-8") as f:
            for ev in remaining:
                f.write(json.dumps(ev) + "\n")
    except Exception as exc:
        print(f"[calibration] failed to clear monitor samples: {exc}")
