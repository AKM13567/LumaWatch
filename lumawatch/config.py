"""
LumaWatch — Configuration & persistence

Holds all user-tunable settings and per-monitor learned baselines.
Config lives at ~/.lumawatch.json and is forward-compatible: old
single-monitor configs are migrated automatically on load.
"""

import json
import pathlib
import threading

CONFIG_PATH = pathlib.Path.home() / ".lumawatch.json"
CONFIG_VERSION = 2

DEFAULT_MONITOR_PROFILE = {
    "normal": 70.0,
    "dim": 30.0,
    "normal_confirmed": False,
    "dim_confirmed": False,
}

DEFAULTS = {
    "version": CONFIG_VERSION,
    "monitors": {},                 # monitor_id -> profile dict
    "manual_override": False,       # if True, engine never auto-adjusts
    "manual_brightness": 70.0,
    "ambient_enabled": True,        # time-of-day brightness ceiling + warmth
    "night_light_enabled": True,    # warm color temp shift after dusk
    "night_light_strength": 45,     # 0-100, how strong the warm shift gets
    "autostart_enabled": False,
    "hotkeys_enabled": True,
    "hotkey_brightness_up": "ctrl+alt+up",
    "hotkey_brightness_down": "ctrl+alt+down",
    "hotkey_toggle_pause": "ctrl+alt+p",
}

_lock = threading.Lock()
_state = None  # type: dict | None


def _migrate(d: dict) -> dict:
    """Bring an on-disk config up to the current schema."""
    version = d.get("version", 1)

    if version < 2:
        # v1 stored a single global "normal"/"dim" pair with no monitor id.
        legacy_profile = {
            "normal": float(d.get("normal", 70.0)),
            "dim": float(d.get("dim", 30.0)),
            "normal_confirmed": bool(d.get("normal_confirmed", False)),
            "dim_confirmed": bool(d.get("dim_confirmed", False)),
        }
        d = {
            **DEFAULTS,
            "monitors": {"legacy-primary": legacy_profile},
        }

    merged = {**DEFAULTS, **d}
    merged["version"] = CONFIG_VERSION
    return merged


def load():
    """Load config from disk once per process, migrating if necessary."""
    global _state
    with _lock:
        if _state is not None:
            return _state
        if CONFIG_PATH.exists():
            try:
                raw = json.loads(CONFIG_PATH.read_text())
                _state = _migrate(raw)
            except Exception:
                _state = dict(DEFAULTS)
        else:
            _state = dict(DEFAULTS)
        return _state


def save():
    """Persist current in-memory config to disk. Best-effort, never raises."""
    with _lock:
        if _state is None:
            return
        try:
            CONFIG_PATH.write_text(json.dumps(_state, indent=2))
        except Exception:
            pass


def get_monitor_profile(monitor_id: str) -> dict:
    """Return (creating if needed) the learned baseline profile for a monitor."""
    cfg = load()
    with _lock:
        if monitor_id not in cfg["monitors"]:
            cfg["monitors"][monitor_id] = dict(DEFAULT_MONITOR_PROFILE)
        return cfg["monitors"][monitor_id]


def set_monitor_profile(monitor_id: str, **kwargs):
    profile = get_monitor_profile(monitor_id)
    with _lock:
        profile.update(kwargs)
    save()


def get(key, default=None):
    cfg = load()
    return cfg.get(key, default)


def set(key, value):
    cfg = load()
    with _lock:
        cfg[key] = value
    save()
