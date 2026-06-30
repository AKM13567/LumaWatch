"""
LumaWatch — Global hotkeys

Thin wrapper around the `keyboard` library so the rest of the app
doesn't care whether it's installed. Hotkeys run in their own daemon
thread and just call back into the app via plain callables, so they
stay decoupled from the Tk main loop (callers should hop back via
root.after(0, ...) inside their callback if they touch widgets).
"""

try:
    import keyboard as _kb
    KEYBOARD_AVAILABLE = True
except ImportError:
    KEYBOARD_AVAILABLE = False

_registered = []


def register(hotkey_str: str, callback):
    """Register a global hotkey like 'ctrl+alt+up'. No-op if unavailable
    or if the hotkey string is invalid (logs to stdout, never raises)."""
    if not KEYBOARD_AVAILABLE or not hotkey_str:
        return False
    try:
        _kb.add_hotkey(hotkey_str, callback)
        _registered.append(hotkey_str)
        return True
    except Exception as exc:
        print(f"[HOTKEY] failed to register '{hotkey_str}': {exc}")
        return False


def unregister_all():
    if not KEYBOARD_AVAILABLE:
        return
    for hk in _registered:
        try:
            _kb.remove_hotkey(hk)
        except Exception:
            pass
    _registered.clear()
