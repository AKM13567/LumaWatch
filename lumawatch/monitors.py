"""
LumaWatch — Multi-monitor support

Enumerates all attached displays, gives each a stable identifier (so
learned baselines survive reboots / reconnects), and wraps per-monitor
screen capture + DDC/CI brightness get/set.

Falls back gracefully: if a monitor doesn't support DDC/CI (very common
for laptop internal panels, which mostly use WMI/ACPI brightness instead
of DDC), we transparently use screen_brightness_control's "auto" method
which already tries WMI on Windows for the internal panel.
"""

import numpy as np

try:
    import screen_brightness_control as sbc
    SBC_AVAILABLE = True
except ImportError:
    SBC_AVAILABLE = False

try:
    from mss import mss as MSS
    import cv2
    CAP_AVAILABLE = True
except ImportError:
    CAP_AVAILABLE = False


class MonitorHandle:
    """One physical display: its capture region + brightness control."""

    def __init__(self, index, mss_monitor, sbc_index, name):
        self.index = index                  # 0-based order as enumerated
        self.mss_monitor = mss_monitor      # dict with left/top/width/height for mss
        self.sbc_index = sbc_index          # index into sbc.list_monitors(), or None
        self.name = name                    # human-readable label
        self._fail_count = 0
        self._fail_limit = 5
        self._last_known_brightness = 70.0

    @property
    def id(self) -> str:
        """Stable-ish identifier used as the config key for learned baselines."""
        return f"mon{self.index}-{self.name}".replace(" ", "_")

    @property
    def ddc_capable(self) -> bool:
        return self.sbc_index is not None

    def get_brightness(self) -> float:
        if not SBC_AVAILABLE or self.sbc_index is None:
            return self._last_known_brightness
        try:
            monitors = sbc.list_monitors()
            r = sbc.get_brightness(display=monitors[self.sbc_index])
            if r and r[0] is not None:
                self._fail_count = 0
                self._last_known_brightness = float(r[0])
                return self._last_known_brightness
            raise ValueError("empty result")
        except Exception:
            self._fail_count += 1
            return self._last_known_brightness

    def set_brightness(self, value: float):
        if not SBC_AVAILABLE or self.sbc_index is None:
            return False
        try:
            monitors = sbc.list_monitors()
            sbc.set_brightness(int(value), display=monitors[self.sbc_index])
            self._fail_count = 0
            self._last_known_brightness = float(value)
            return True
        except Exception:
            self._fail_count += 1
            return False

    @property
    def is_failing(self) -> bool:
        return self._fail_count >= self._fail_limit

    def grab_grayscale(self, sct, size=48):
        """Capture this monitor's region downsampled to a small grayscale array."""
        shot = sct.grab(self.mss_monitor)
        img = np.frombuffer(shot.raw, dtype=np.uint8).reshape(
            shot.height, shot.width, 4)
        small = cv2.resize(img, (size, size), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGRA2GRAY)
        return gray


def enumerate_monitors():
    """
    Build a MonitorHandle for every display mss can capture, paired up
    with screen_brightness_control monitors where possible (by index —
    sbc and mss generally enumerate displays in the same OS order, but
    this isn't guaranteed on every system; see README for caveats).
    """
    handles = []
    if not CAP_AVAILABLE:
        return handles

    sbc_names = []
    if SBC_AVAILABLE:
        try:
            sbc_names = [m for m in sbc.list_monitors()]
        except Exception:
            sbc_names = []

    with MSS() as sct:
        # monitors[0] is the virtual "all displays" bounding box; skip it.
        physical = sct.monitors[1:] if len(sct.monitors) > 1 else sct.monitors

        for i, mon in enumerate(physical):
            sbc_index = i if i < len(sbc_names) else None
            label = sbc_names[i] if sbc_index is not None else f"display{i}"
            handles.append(MonitorHandle(i, mon, sbc_index, label))

    return handles
