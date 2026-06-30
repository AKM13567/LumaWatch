"""
LumaWatch — Auto-start on login

Uses the per-user HKCU Run registry key (no admin rights required,
unlike a Task Scheduler entry or Startup-folder shortcut with elevated
targets). This is the same mechanism most lightweight tray utilities use.
"""

import sys
import pathlib

try:
    import winreg
    WINREG_AVAILABLE = True
except ImportError:
    WINREG_AVAILABLE = False

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "LumaWatch"


def _launch_command() -> str:
    """
    Build the command line to store in the registry. If running as a
    frozen executable (PyInstaller), point straight at it. Otherwise
    point at pythonw.exe + this script so it launches without a console.
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'

    script = str(pathlib.Path(__file__).resolve().parent.parent / "screen_dimmer.py")
    pythonw = pathlib.Path(sys.executable).with_name("pythonw.exe")
    interpreter = str(pythonw) if pythonw.exists() else sys.executable
    return f'"{interpreter}" "{script}"'


def is_enabled() -> bool:
    if not WINREG_AVAILABLE:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


def set_enabled(enabled: bool) -> bool:
    """Returns True on success. Never raises."""
    if not WINREG_AVAILABLE:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _launch_command())
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        return True
    except Exception as exc:
        print(f"[AUTOSTART] failed: {exc}")
        return False
