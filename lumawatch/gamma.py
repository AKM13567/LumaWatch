"""
LumaWatch — Windows gamma ramp control

Applies a warm color tint via the display gamma ramp (same low-level
mechanism f.lux and Windows' own Night Light use). This works per
top-level desktop, not per-monitor — Windows gamma ramps are a single
global table applied to the primary rendering device.
"""

import ctypes
import ctypes.wintypes as wt

_gdi32 = None
_user32 = None
_default_ramp_cache = None


def _ensure_handles():
    global _gdi32, _user32
    if _gdi32 is None:
        _gdi32 = ctypes.windll.gdi32
        _user32 = ctypes.windll.user32


def _get_dc():
    _ensure_handles()
    return _user32.GetDC(0)


def _release_dc(hdc):
    _user32.ReleaseDC(0, hdc)


WORD = wt.WORD
RampArray = WORD * 256


def _build_ramp(r_gain, g_gain, b_gain):
    """Build a 3x256 gamma ramp table tinted by the given per-channel gains."""
    ramp = (RampArray * 3)()
    for i in range(256):
        base = int(i * 65535 / 255)
        ramp[0][i] = min(65535, int(base * r_gain))
        ramp[1][i] = min(65535, int(base * g_gain))
        ramp[2][i] = min(65535, int(base * b_gain))
    return ramp


def apply_warmth(r_gain: float, g_gain: float, b_gain: float) -> bool:
    """Apply a tinted gamma ramp. Returns False silently on any failure
    (e.g. non-Windows, no GDI access, remote desktop session)."""
    try:
        _ensure_handles()
        hdc = _get_dc()
        if not hdc:
            return False
        try:
            ramp = _build_ramp(r_gain, g_gain, b_gain)
            ok = _gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(ramp))
            return bool(ok)
        finally:
            _release_dc(hdc)
    except Exception:
        return False


def reset_to_neutral() -> bool:
    """Restore an identity (neutral) gamma ramp."""
    return apply_warmth(1.0, 1.0, 1.0)
