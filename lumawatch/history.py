"""
lumawatch.history — local-only raw event log for the personal-brightness-curve
feature.

This module never makes a network call and never should. It appends one
JSON line per brightness observation to a plain text file in the user's
home directory (~/.lumawatch_history.jsonl), so the data is human-readable
and trivially exportable/deletable by the user at any time -- that's the
"completely transparent, nothing uploaded" requirement made concrete.

Each line looks like:
    {"ts": "2026-07-20T23:14:02.331+00:00", "hour": 23,
     "monitor_id": "DISPLAY1", "brightness": 42.0, "source": "manual_ui"}

`source` is one of:
    "manual_ui"  -- you moved the slider or pressed a brightness hotkey
    "manual_hw"  -- LumaWatch detected a physical monitor button change
    "auto"       -- the content-adaptive engine settled on this value itself
"""

import json
import shutil
import datetime
from pathlib import Path

HISTORY_PATH = Path.home() / ".lumawatch_history.jsonl"
MAX_AGE_DAYS = 90
MAX_LINES = 200_000  # hard safety cap regardless of age, so a stuck loop can't grow this unbounded


def record_event(monitor_id, brightness, source, ts=None):
    """Append one observation. Safe to call from any thread; swallows and
    prints errors rather than raising, since a logging failure should never
    take down the brightness engine itself."""
    ts = ts or datetime.datetime.now(datetime.timezone.utc)
    entry = {
        "ts": ts.isoformat(),
        "hour": ts.astimezone().hour,  # local hour -- "night" means local night, not UTC
        "monitor_id": monitor_id,
        "brightness": round(float(brightness), 1),
        "source": source,
    }
    try:
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as exc:
        print(f"[history] failed to record event: {exc}")


def load_events():
    """Returns a list of dicts, oldest first. Corrupt lines are skipped
    rather than aborting the whole read."""
    if not HISTORY_PATH.exists():
        return []
    events = []
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as exc:
        print(f"[history] failed to load events: {exc}")
    return events


def prune_old(max_age_days=MAX_AGE_DAYS, max_lines=MAX_LINES):
    """Drops events older than max_age_days and hard-caps total line count.
    Call this occasionally (e.g. once per app start) -- it's a full
    read+rewrite, not something to run every tick."""
    events = load_events()
    if not events:
        return

    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=max_age_days)
    kept = []
    for ev in events:
        try:
            ts = datetime.datetime.fromisoformat(ev["ts"])
        except Exception:
            continue
        if ts >= cutoff:
            kept.append(ev)

    if len(kept) > max_lines:
        kept = kept[-max_lines:]

    if len(kept) == len(events):
        return  # nothing to prune, don't bother rewriting

    try:
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            for ev in kept:
                f.write(json.dumps(ev) + "\n")
    except Exception as exc:
        print(f"[history] failed to prune: {exc}")


def export_copy(dest_path):
    """Copies the raw log to a user-chosen location, unmodified. This is
    the entire 'export' feature -- it's a plain file copy, nothing more."""
    if not HISTORY_PATH.exists():
        open(HISTORY_PATH, "a", encoding="utf-8").close()
    shutil.copyfile(HISTORY_PATH, dest_path)


def clear_all():
    """Deletes the raw local history file."""
    try:
        if HISTORY_PATH.exists():
            HISTORY_PATH.unlink()
    except Exception as exc:
        print(f"[history] failed to clear: {exc}")
