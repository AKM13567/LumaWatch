"""
lumawatch.learn — turns the local raw history (lumawatch.history) into a
per-monitor, per-hour "what brightness do you actually like at this hour"
curve. Purely local: reads ~/.lumawatch_history.jsonl, writes
~/.lumawatch_learned.json. No network access, ever.

Design choices worth knowing about:

- Manual signals (you moving the slider, pressing a hotkey, or using a
  physical monitor button) are weighted far higher than auto-engine
  settle events, since those are your actual stated preference rather
  than a guess the content-heuristics made. Auto events are still
  included at low weight (per your choice) so the curve fills in faster
  in the areas you never touch manually -- but they can never dominate.
- Recent observations count more than old ones (exponential half-life),
  so the curve tracks a change in your habits instead of being anchored
  to how you behaved months ago.
- An hour is only "trusted" once it has enough *effective* (recency- and
  source-weighted) samples. Untrusted hours have zero effect on
  brightness -- they just don't feed back yet.
- learned_ceiling_factor() is capped to the range [0.4, 1.0]. It can only
  ever pull the brightness *down* toward your historical habit for that
  hour, never push it above the monitor's own "normal" baseline. That's
  deliberate: bad or sparse early data should never be able to make the
  screen brighter than you configured it to safely go.
"""

import json
import datetime
from pathlib import Path

from . import history

LEARNED_PATH = Path.home() / ".lumawatch_learned.json"

SOURCE_WEIGHTS = {
    "manual_ui": 1.0,
    "manual_hw": 1.0,
    "auto": 0.15,
}
DEFAULT_SOURCE_WEIGHT = 0.15
RECENCY_HALF_LIFE_DAYS = 14.0
MIN_EFFECTIVE_WEIGHT = 3.0  # roughly "a few real manual data points", recency-adjusted


def _load_learned():
    if not LEARNED_PATH.exists():
        return {}
    try:
        with open(LEARNED_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        print(f"[learn] failed to load learned curve: {exc}")
        return {}


def _save_learned(data):
    try:
        with open(LEARNED_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        print(f"[learn] failed to save learned curve: {exc}")


def recompute(monitor_ids=None, half_life_days=RECENCY_HALF_LIFE_DAYS,
              min_effective_weight=MIN_EFFECTIVE_WEIGHT):
    """Rebuilds the learned curve from scratch off the current local
    history. Cheap enough to call every 20-30 minutes; not cheap enough
    to call every analysis tick.

    monitor_ids: optional iterable to restrict which monitors get
    (re)computed; None means "all monitors present in the history file".
    """
    events = history.load_events()
    now = datetime.datetime.now(datetime.timezone.utc)
    buckets = {}  # monitor_id -> hour(int) -> [weighted_sum, weight_total]

    for ev in events:
        mid = ev.get("monitor_id")
        if monitor_ids is not None and mid not in monitor_ids:
            continue
        try:
            ts = datetime.datetime.fromisoformat(ev["ts"])
        except Exception:
            continue

        age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
        recency_weight = 0.5 ** (age_days / half_life_days)
        source_weight = SOURCE_WEIGHTS.get(ev.get("source"), DEFAULT_SOURCE_WEIGHT)
        w = recency_weight * source_weight
        if w <= 0:
            continue

        hour = ev.get("hour")
        if hour is None:
            hour = ts.hour
        hour = int(hour)

        bucket = buckets.setdefault(mid, {}).setdefault(hour, [0.0, 0.0])
        bucket[0] += w * float(ev.get("brightness", 0.0))
        bucket[1] += w

    result = {}
    for mid, hours in buckets.items():
        result[mid] = {}
        for hour, (weighted_sum, weight_total) in hours.items():
            if weight_total <= 0:
                continue
            result[mid][str(hour)] = {
                "value": round(weighted_sum / weight_total, 1),
                "weight": round(weight_total, 2),
                "trusted": weight_total >= min_effective_weight,
            }

    _save_learned(result)
    return result


def get_curve(monitor_id):
    """{'0': {'value':.., 'weight':.., 'trusted':..}, ..., '23': {...}}
    for one monitor, or {} if nothing has been learned for it yet."""
    return _load_learned().get(monitor_id, {})


def learned_ceiling_factor(monitor_id, normal_baseline, hour=None, now=None):
    """A multiplicative factor in [0.4, 1.0] to layer on top of the
    existing fixed time-of-day ceiling. Returns 1.0 (no effect) if this
    hour isn't trusted yet, or if normal_baseline is invalid."""
    if hour is None:
        hour = (now or datetime.datetime.now()).hour
    bucket = get_curve(monitor_id).get(str(hour))
    if not bucket or not bucket.get("trusted") or normal_baseline <= 0:
        return 1.0
    factor = bucket["value"] / normal_baseline
    return max(0.4, min(1.0, factor))


def clear_learned():
    """Deletes the learned-curve file (does not touch raw history)."""
    try:
        if LEARNED_PATH.exists():
            LEARNED_PATH.unlink()
    except Exception as exc:
        print(f"[learn] failed to clear learned curve: {exc}")
