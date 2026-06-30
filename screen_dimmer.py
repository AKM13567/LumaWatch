"""
LumaWatch — Intelligent Adaptive Brightness Engine
Temporal smoothing · Anti-flicker · Auto-learning baselines (per monitor)
Time-of-day brightness ceiling · Night Light warmth · Manual override
Global hotkeys · Auto-start on login · System tray support
"""

import os
import sys
import time
import queue
import datetime
import threading
import tkinter as tk
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lumawatch import config as cfg
from lumawatch import monitors as monlib
from lumawatch import ambient
from lumawatch import gamma
from lumawatch import hotkeys
from lumawatch import autostart

try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False

SBC_AVAILABLE = monlib.SBC_AVAILABLE
CAP_AVAILABLE = monlib.CAP_AVAILABLE

# ─── TUNABLES ─────────────────────────────────────────────────────────────────
WHITE_THRESH      = 160
WHITE_RATIO_LOW   = 0.10
WHITE_RATIO_HIGH  = 0.50
CHECK_INTERVAL    = 1.0
IDLE_INTERVAL     = 2.0
CAPTURE_SIZE      = 48
SMOOTHING_WINDOW  = 5
TURBULENCE_THRESH = 15
LOCKOUT_DURATION  = 1.5
MIN_DELTA         = 4
MANUAL_NUDGE      = 5     # brightness change per hotkey press

# ─── DESIGN TOKENS ────────────────────────────────────────────────────────────
BG_DEEP    = "#0A0A0A"
BG_PANEL   = "#111111"
BG_TRACK   = "#1E1E1E"
ACCENT     = "#D92B2B"
ACCENT_DIM = "#7A1515"
ACCENT_GRN = "#2BD97A"
ACCENT_AMB = "#D9A02B"
TEXT_HI    = "#F0F0F0"
TEXT_MID   = "#555555"
BORDER     = "#2A2A2A"
FONT_UI    = "Segoe UI"

# ─── DPI SCALING ──────────────────────────────────────────────────────────────
import ctypes
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

_probe = tk.Tk()
_probe.withdraw()
_DPI = _probe.winfo_fpixels("1i")
_probe.destroy()

SCALE = max(1.0, _DPI / 96.0)
def S(v): return max(1, int(round(v * SCALE)))

ARC_W    = S(14)
ARC_DIAM = S(150)
ARC_PAD  = ARC_W + S(4)
ARC_SIZE = ARC_DIAM + ARC_PAD * 2

TOPBAR_H  = S(44)
STATUS_H  = S(30)
TABBAR_H  = S(34)
BODY_PAD  = S(16)
WIN_H     = TOPBAR_H + 1 + TABBAR_H + ARC_SIZE + BODY_PAD * 2 + 1 + STATUS_H
WIN_W     = S(720)

# ─── PER-MONITOR ENGINE STATE ────────────────────────────────────────────────
class MonitorState:
    """Mutable runtime state for one monitor's adaptive engine."""
    def __init__(self, handle: monlib.MonitorHandle):
        self.handle = handle
        self.profile = cfg.get_monitor_profile(handle.id)
        self.current_hw_brightness = self.profile["normal"]
        self.last_target = self.current_hw_brightness
        self.last_raw_target = self.current_hw_brightness
        self.last_change_time = 0.0
        self.lockout_until = 0.0
        self.is_fading = False
        self.fade_lock = threading.Lock()
        self.luminance_history = deque(maxlen=SMOOTHING_WINDOW)
        self.fade_queue: "queue.Queue[float | None]" = queue.Queue(maxsize=1)

    @property
    def normal(self): return self.profile["normal"]
    @property
    def dim(self): return self.profile["dim"]


state_lock = threading.Lock()
engine_paused = False
manual_override = cfg.get("manual_override", False)
manual_brightness = cfg.get("manual_brightness", 70.0)
ambient_enabled = cfg.get("ambient_enabled", True)
night_light_enabled = cfg.get("night_light_enabled", True)

monitor_handles = monlib.enumerate_monitors()
monitor_states = [MonitorState(h) for h in monitor_handles]
active_monitor_idx = 0  # which monitor's panel is shown in the UI


def active_state():
    if not monitor_states:
        return None
    return monitor_states[active_monitor_idx]


# ─── FADE WORKER (one per monitor) ───────────────────────────────────────────
def _fade_worker(ms: MonitorState):
    while True:
        target = ms.fade_queue.get()
        if target is None:
            break
        if not ms.fade_lock.acquire(blocking=False):
            ms.fade_queue.task_done()
            continue
        try:
            with state_lock:
                ms.is_fading = True

            cur = ms.handle.get_brightness()
            with state_lock:
                ms.current_hw_brightness = cur
                ms.last_target = float(target)

            if abs(cur - target) < 2:
                continue

            total_range = abs(ms.normal - ms.dim)
            if total_range > 0 and abs(target - cur) >= total_range * 0.85:
                ms.handle.set_brightness(target)
                with state_lock:
                    ms.current_hw_brightness = float(target)
                _ui_refresh_if_active(ms)
                continue

            step = 5 if target > cur else -5
            b = int(cur)
            while (step > 0 and b < target) or (step < 0 and b > target):
                b = min(target, b + step) if step > 0 else max(target, b + step)
                ms.handle.set_brightness(b)
                with state_lock:
                    ms.current_hw_brightness = float(b)
                _ui_refresh_if_active(ms)
                time.sleep(0.005)

            ms.handle.set_brightness(target)
            with state_lock:
                ms.current_hw_brightness = float(target)
            _ui_refresh_if_active(ms)
        finally:
            with state_lock:
                ms.is_fading = False
            ms.fade_lock.release()
            ms.fade_queue.task_done()


def fade_to_target(ms: MonitorState, target):
    try:
        ms.fade_queue.put_nowait(target)
    except queue.Full:
        pass


def _ui_refresh_if_active(ms: MonitorState):
    if active_state() is ms:
        root.after(0, _ui_brightness, ms.current_hw_brightness)


# ─── AMBIENT / NIGHT LIGHT LOOP ───────────────────────────────────────────────
def ambient_loop():
    """
    Runs independently of the per-frame analysis loop (it only needs to
    tick once a minute). Computes the time-of-day brightness ceiling and
    applies night-light gamma warmth.
    """
    while True:
        now = datetime.datetime.now()

        if night_light_enabled:
            strength = cfg.get("night_light_strength", 45)
            warmth = ambient.night_light_warmth(now, strength=strength)
            r, g, b = ambient.warmth_to_rgb_gain(warmth)
            gamma.apply_warmth(r, g, b)
        else:
            gamma.reset_to_neutral()

        root.after(0, _ui_ambient_status, now)
        time.sleep(30)


def current_ceiling(now=None):
    if not ambient_enabled:
        return 1.0
    now = now or datetime.datetime.now()
    return ambient.brightness_ceiling(now)


# ─── ANALYSIS LOOP (one per monitor) ─────────────────────────────────────────
def analyze_loop(ms: MonitorState):
    initial = ms.handle.get_brightness()
    with state_lock:
        ms.current_hw_brightness = initial
        ms.last_target = initial
    _ui_refresh_if_active(ms)

    if not CAP_AVAILABLE:
        if active_state() is ms:
            root.after(0, _ui_status, "⚠ mss / cv2 / numpy missing — capture unavailable", ACCENT)
        return

    nothing_changed_last = False

    from mss import mss as MSS

    with MSS() as sct:
        while True:
            sleep_for = IDLE_INTERVAL if nothing_changed_last else CHECK_INTERVAL
            time.sleep(sleep_for)

            if engine_paused or manual_override:
                nothing_changed_last = True
                continue

            try:
                now = time.time()
                actual = ms.handle.get_brightness()
                _ui_refresh_if_active(ms)

                gray = ms.handle.grab_grayscale(sct, CAPTURE_SIZE)
                white_ratio = float((gray > WHITE_THRESH).sum()) / gray.size
                is_white_screen = white_ratio > 0.30

                with state_lock:
                    hw, lt, fading = ms.current_hw_brightness, ms.last_target, ms.is_fading

                if not fading and abs(actual - hw) > 2 and abs(actual - lt) > 2:
                    with state_lock:
                        if is_white_screen:
                            ms.profile["dim"] = actual
                            ms.profile["dim_confirmed"] = True
                        else:
                            ms.profile["normal"] = actual
                            ms.profile["normal_confirmed"] = True
                        ms.current_hw_brightness = actual
                        ms.last_target = actual
                        ms.last_change_time = now
                    cfg.save()
                    if active_state() is ms:
                        root.after(0, _ui_memory_labels)
                else:
                    with state_lock:
                        ms.current_hw_brightness = actual

                with state_lock:
                    max_b, min_b = ms.normal, ms.dim

                if abs(max_b - min_b) < 5:
                    nothing_changed_last = True
                    continue

                if white_ratio <= WHITE_RATIO_LOW:
                    raw = max_b
                elif white_ratio >= WHITE_RATIO_HIGH:
                    raw = min_b
                else:
                    t = (white_ratio - WHITE_RATIO_LOW) / (WHITE_RATIO_HIGH - WHITE_RATIO_LOW)
                    raw = max_b - t * (max_b - min_b)
                raw = max(0.0, min(100.0, raw))

                # Apply time-of-day ceiling on top of the content-derived target,
                # same idea as a phone capping max brightness at night.
                raw = raw * current_ceiling()

                with state_lock:
                    lrt = ms.last_raw_target
                if abs(raw - lrt) >= TURBULENCE_THRESH:
                    with state_lock:
                        ms.lockout_until = now + LOCKOUT_DURATION
                    if active_state() is ms:
                        root.after(0, _ui_status, "⚡ TAB ACTIVITY — brightness frozen", ACCENT)
                    nothing_changed_last = False
                else:
                    if active_state() is ms:
                        root.after(0, _ui_status, "● ACTIVE", ACCENT_DIM)
                with state_lock:
                    ms.last_raw_target = raw

                ms.luminance_history.append(raw)
                smoothed = int(round(sum(ms.luminance_history) / len(ms.luminance_history)))

                with state_lock:
                    lu, lct, chw = ms.lockout_until, ms.last_change_time, ms.current_hw_brightness

                if now > lu and (now - lct > LOCKOUT_DURATION) and abs(smoothed - chw) >= MIN_DELTA:
                    with state_lock:
                        ms.last_change_time = now
                    fade_to_target(ms, smoothed)
                    nothing_changed_last = False
                else:
                    nothing_changed_last = True

            except Exception as exc:
                print(f"[ERROR][{ms.handle.id}] {exc}")
                nothing_changed_last = False


# ─── MANUAL OVERRIDE ──────────────────────────────────────────────────────────
def apply_manual_brightness():
    """Push the manual_brightness value to whichever monitor is active in the UI."""
    ms = active_state()
    if ms is None:
        return
    fade_to_target(ms, manual_brightness)


def nudge_brightness(delta):
    """Hotkey handler: adjusts brightness, switching the active monitor into
    manual override automatically (mirrors how phones treat a manual slider
    drag as an override of auto-brightness until conditions change a lot)."""
    global manual_override, manual_brightness
    with state_lock:
        manual_override = True
        manual_brightness = max(0.0, min(100.0, manual_brightness + delta))
    cfg.set("manual_override", True)
    cfg.set("manual_brightness", manual_brightness)
    root.after(0, _sync_manual_ui)
    apply_manual_brightness()


def toggle_manual_override(force=None):
    global manual_override
    with state_lock:
        manual_override = (not manual_override) if force is None else force
    cfg.set("manual_override", manual_override)
    if manual_override:
        apply_manual_brightness()
    root.after(0, _sync_manual_ui)


# ─── UI CALLBACKS ────────────────────────────────────────────────────────────
def _ui_brightness(val):
    try:
        lbl_num.config(text=str(int(round(val))))
        _draw_arc(int(round(val)))
        if not manual_override:
            manual_slider.set(int(round(val)))
    except Exception:
        pass

def _ui_memory_labels():
    try:
        ms = active_state()
        if ms is None:
            return
        lbl_bright_val.config(
            text=f"{int(ms.normal)}%" + ("" if ms.profile["normal_confirmed"] else "  (default)"),
            fg=ACCENT if ms.profile["normal_confirmed"] else TEXT_MID
        )
        lbl_dim_val.config(
            text=f"{int(ms.dim)}%" + ("" if ms.profile["dim_confirmed"] else "  (default)"),
            fg=ACCENT if ms.profile["dim_confirmed"] else TEXT_MID
        )
    except Exception:
        pass

def _ui_status(text, color=ACCENT_DIM):
    try:
        lbl_status.config(text=text, fg=color)
    except Exception:
        pass

def _ui_ambient_status(now):
    try:
        ceiling = current_ceiling(now)
        pct = int(round(ceiling * 100))
        if ceiling < 0.99 and ambient_enabled:
            lbl_ambient.config(text=f"🌙 night ceiling {pct}%", fg=ACCENT_AMB)
        else:
            lbl_ambient.config(text="☀ day — no ceiling", fg=TEXT_MID)
    except Exception:
        pass

def _sync_manual_ui():
    try:
        if manual_override:
            btn_manual.config(text="🔓 AUTO MODE", fg=ACCENT_GRN, highlightbackground=ACCENT_GRN)
            manual_slider.config(state="normal")
        else:
            btn_manual.config(text="🔒 MANUAL", fg=TEXT_MID, highlightbackground=BORDER)
            manual_slider.config(state="normal")
    except Exception:
        pass

def _on_manual_slider(val):
    global manual_brightness
    if not manual_override:
        return
    manual_brightness = float(val)
    cfg.set("manual_brightness", manual_brightness)
    apply_manual_brightness()

def _on_monitor_tab(idx):
    global active_monitor_idx
    active_monitor_idx = idx
    ms = active_state()
    if ms:
        _ui_brightness(ms.current_hw_brightness)
        _ui_memory_labels()
    _refresh_tab_styles()

def _refresh_tab_styles():
    for i, btn in enumerate(monitor_tab_buttons):
        if i == active_monitor_idx:
            btn.config(bg=BG_PANEL, fg=ACCENT, highlightbackground=ACCENT)
        else:
            btn.config(bg=BG_DEEP, fg=TEXT_MID, highlightbackground=BORDER)

def _on_toggle_ambient():
    global ambient_enabled
    ambient_enabled = not ambient_enabled
    cfg.set("ambient_enabled", ambient_enabled)
    chk_ambient.config(text="☑ Time-of-day ceiling" if ambient_enabled else "☐ Time-of-day ceiling")

def _on_toggle_night_light():
    global night_light_enabled
    night_light_enabled = not night_light_enabled
    cfg.set("night_light_enabled", night_light_enabled)
    chk_night.config(text="☑ Night Light warmth" if night_light_enabled else "☐ Night Light warmth")
    if not night_light_enabled:
        gamma.reset_to_neutral()

def _on_toggle_autostart():
    enabled = not autostart.is_enabled()
    ok = autostart.set_enabled(enabled)
    if ok:
        cfg.set("autostart_enabled", enabled)
        chk_autostart.config(text="☑ Start with Windows" if enabled else "☐ Start with Windows")
    else:
        _ui_status("⚠ Could not update autostart registry key", ACCENT)

# ─── ARC DRAW ─────────────────────────────────────────────────────────────────
def _draw_arc(value):
    arc_canvas.delete("arc")
    extent = value / 100 * 270
    x0, y0 = ARC_PAD, ARC_PAD
    x1, y1 = ARC_PAD + ARC_DIAM, ARC_PAD + ARC_DIAM
    arc_canvas.create_arc(x0, y0, x1, y1, start=135, extent=270,
                           style="arc", outline=BG_TRACK, width=ARC_W, tags="arc")
    if extent > 0:
        arc_canvas.create_arc(x0, y0, x1, y1, start=135, extent=extent,
                               style="arc", outline=ACCENT, width=ARC_W, tags="arc")

# ─── SYSTEM TRAY ──────────────────────────────────────────────────────────────
def _make_tray_icon_image():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([8, 8, 56, 56], fill="#D92B2B")
    d.ellipse([20, 20, 44, 44], fill="#0A0A0A")
    return img

def _show_window():
    root.after(0, root.deiconify)
    root.after(0, root.lift)

def _setup_tray():
    if not TRAY_AVAILABLE:
        return
    menu = pystray.Menu(
        pystray.MenuItem("Show LumaWatch", _show_window, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Pause / Resume", lambda: root.after(0, toggle_pause)),
        pystray.MenuItem("Manual override", lambda: root.after(0, toggle_manual_override)),
        pystray.MenuItem("Quit", lambda: root.after(0, quit_app)),
    )
    icon = pystray.Icon("LumaWatch", _make_tray_icon_image(), "LumaWatch", menu)
    threading.Thread(target=icon.run, daemon=True).start()

# ─── APP CONTROLS ─────────────────────────────────────────────────────────────
def _hide_to_tray():
    root.withdraw()

def toggle_pause():
    global engine_paused
    engine_paused = not engine_paused
    if engine_paused:
        btn_pause.config(text="▶  RESUME", fg=ACCENT_GRN, highlightbackground=ACCENT_GRN)
        _ui_status("⏸  PAUSED", TEXT_MID)
    else:
        btn_pause.config(text="⏸  PAUSE", fg=TEXT_MID, highlightbackground=BORDER)
        _ui_status("● ACTIVE", ACCENT_DIM)

def quit_app():
    for ms in monitor_states:
        try:
            ms.fade_queue.put_nowait(None)
        except queue.Full:
            pass
    hotkeys.unregister_all()
    gamma.reset_to_neutral()
    try:
        root.destroy()
    finally:
        os._exit(0)

# ─── BUILD UI ─────────────────────────────────────────────────────────────────
root = tk.Tk()
root.title("LumaWatch")
root.configure(bg=BG_DEEP)
root.resizable(True, True)
root.minsize(WIN_W, WIN_H)
root.protocol("WM_DELETE_WINDOW", _hide_to_tray if TRAY_AVAILABLE else quit_app)

sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
root.geometry(f"{WIN_W}x{WIN_H}+{(sw-WIN_W)//2}+{(sh-WIN_H)//2}")

# ── TOP BAR ───────────────────────────────────────────────────────────────────
topbar = tk.Frame(root, bg=BG_DEEP, height=TOPBAR_H)
topbar.pack(fill="x")
topbar.pack_propagate(False)

tk.Label(topbar, text="◈", font=(FONT_UI, 11), fg=ACCENT, bg=BG_DEEP).pack(side="left", padx=(S(20), S(6)))
tk.Label(topbar, text="LUMAWATCH", font=(FONT_UI, 10, "bold"), fg=TEXT_HI, bg=BG_DEEP).pack(side="left")
tk.Label(topbar, text="  —  ADAPTIVE BRIGHTNESS", font=(FONT_UI, 8), fg=TEXT_MID, bg=BG_DEEP).pack(side="left")

btn_quit = tk.Button(topbar, text="✕  QUIT", font=(FONT_UI, 8, "bold"),
                      fg=TEXT_MID, bg=BG_PANEL, activebackground=ACCENT,
                      activeforeground=TEXT_HI, relief="solid", bd=1,
                      highlightbackground=BORDER, highlightthickness=1,
                      cursor="hand2", padx=S(10), pady=S(2), command=quit_app)
btn_quit.pack(side="right", padx=(0, S(12)))

btn_pause = tk.Button(topbar, text="⏸  PAUSE", font=(FONT_UI, 8, "bold"),
                       fg=TEXT_MID, bg=BG_PANEL, activebackground=BG_TRACK,
                       activeforeground=TEXT_HI, relief="solid", bd=1,
                       highlightbackground=BORDER, highlightthickness=1,
                       cursor="hand2", padx=S(10), pady=S(2), command=toggle_pause)
btn_pause.pack(side="right", padx=S(6))

btn_manual = tk.Button(topbar, text="🔒 MANUAL", font=(FONT_UI, 8, "bold"),
                        fg=TEXT_MID, bg=BG_PANEL, activebackground=BG_TRACK,
                        activeforeground=TEXT_HI, relief="solid", bd=1,
                        highlightbackground=BORDER, highlightthickness=1,
                        cursor="hand2", padx=S(10), pady=S(2),
                        command=lambda: toggle_manual_override())
btn_manual.pack(side="right", padx=S(6))

if TRAY_AVAILABLE:
    tk.Label(topbar, text="× closes to tray", font=(FONT_UI, 7), fg=TEXT_MID, bg=BG_DEEP).pack(side="left", padx=(S(12), 0))

tk.Frame(root, bg=BORDER, height=1).pack(fill="x")

# ── MONITOR TABS ──────────────────────────────────────────────────────────────
tabbar = tk.Frame(root, bg=BG_DEEP, height=TABBAR_H)
tabbar.pack(fill="x")
tabbar.pack_propagate(False)

monitor_tab_buttons = []
if monitor_handles:
    for h in monitor_handles:
        label = f"{h.name}" + ("" if h.ddc_capable else " (no DDC)")
        b = tk.Button(tabbar, text=label, font=(FONT_UI, 8, "bold"),
                      relief="solid", bd=1, cursor="hand2",
                      padx=S(12), pady=S(4),
                      command=lambda i=h.index: _on_monitor_tab(i))
        b.pack(side="left", padx=(S(16) if h.index == 0 else S(6), 0), pady=S(4))
        monitor_tab_buttons.append(b)
else:
    tk.Label(tabbar, text="No displays detected", font=(FONT_UI, 8),
              fg=ACCENT, bg=BG_DEEP).pack(side="left", padx=S(16))

tk.Frame(root, bg=BORDER, height=1).pack(fill="x")

# ── BODY CONTAINER ────────────────────────────────────────────────────────────
body_container = tk.Frame(root, bg=BG_DEEP)
body_container.pack(expand=True, fill="both")

body = tk.Frame(body_container, bg=BG_DEEP, width=WIN_W - BODY_PAD*2, height=ARC_SIZE)
body.pack(expand=True)
body.pack_propagate(False)

left = tk.Frame(body, bg=BG_DEEP)
left.place(x=0, y=0, width=WIN_W - BODY_PAD * 2 - ARC_SIZE - S(220), height=ARC_SIZE)

def _info_row(parent, title, desc, val_text, y_offset, h_frac=0.30):
    h = int(ARC_SIZE * h_frac)
    fr = tk.Frame(parent, bg=BG_PANEL, highlightbackground=BORDER, highlightthickness=1)
    fr.place(x=0, y=y_offset, relwidth=1.0, height=h)
    tk.Label(fr, text=title, font=(FONT_UI, 10, "bold"), fg=TEXT_HI, bg=BG_PANEL, anchor="w").place(x=S(14), y=S(8))
    tk.Label(fr, text=desc, font=(FONT_UI, 8), fg=TEXT_MID, bg=BG_PANEL, anchor="w").place(x=S(14), y=S(26))
    val = tk.Label(fr, text=val_text, font=(FONT_UI, 13, "bold"), fg=TEXT_MID, bg=BG_PANEL, anchor="e")
    val.place(relx=1.0, rely=0.5, x=-S(16), anchor="e")
    return val

_init_ms = active_state()
_n0 = _init_ms.normal if _init_ms else 70.0
_d0 = _init_ms.dim if _init_ms else 30.0
_nc0 = _init_ms.profile["normal_confirmed"] if _init_ms else False
_dc0 = _init_ms.profile["dim_confirmed"] if _init_ms else False

row_h = int(ARC_SIZE * 0.30)
lbl_bright_val = _info_row(left, "BRIGHT SCREEN TARGET", "Auto-learned — bright / white content",
                             f"{int(_n0)}%" + ("" if _nc0 else "  (default)"), 0, 0.30)
lbl_dim_val = _info_row(left, "DARK SCREEN TARGET", "Auto-learned — dark / low-light content",
                          f"{int(_d0)}%" + ("" if _dc0 else "  (default)"), row_h + S(6), 0.30)

# Ambient status + toggles
amb_y = row_h * 2 + S(12)
amb_h = ARC_SIZE - amb_y
amb_fr = tk.Frame(left, bg=BG_PANEL, highlightbackground=BORDER, highlightthickness=1)
amb_fr.place(x=0, y=amb_y, relwidth=1.0, height=max(amb_h, S(90)))

lbl_ambient = tk.Label(amb_fr, text="☀ day — no ceiling", font=(FONT_UI, 9, "bold"),
                        fg=TEXT_MID, bg=BG_PANEL, anchor="w")
lbl_ambient.place(x=S(14), y=S(8))

chk_ambient = tk.Checkbutton(amb_fr, text="☑ Time-of-day ceiling" if ambient_enabled else "☐ Time-of-day ceiling",
                              font=(FONT_UI, 8), fg=TEXT_MID, bg=BG_PANEL, selectcolor=BG_PANEL,
                              activebackground=BG_PANEL, bd=0, highlightthickness=0,
                              command=_on_toggle_ambient, indicatoron=False, relief="flat")
chk_ambient.place(x=S(14), y=S(32))

chk_night = tk.Checkbutton(amb_fr, text="☑ Night Light warmth" if night_light_enabled else "☐ Night Light warmth",
                            font=(FONT_UI, 8), fg=TEXT_MID, bg=BG_PANEL, selectcolor=BG_PANEL,
                            activebackground=BG_PANEL, bd=0, highlightthickness=0,
                            command=_on_toggle_night_light, indicatoron=False, relief="flat")
chk_night.place(x=S(14), y=S(54))

_autostart_initial = autostart.is_enabled()
chk_autostart = tk.Checkbutton(amb_fr, text="☑ Start with Windows" if _autostart_initial else "☐ Start with Windows",
                                font=(FONT_UI, 8), fg=TEXT_MID, bg=BG_PANEL, selectcolor=BG_PANEL,
                                activebackground=BG_PANEL, bd=0, highlightthickness=0,
                                command=_on_toggle_autostart, indicatoron=False, relief="flat")
chk_autostart.place(x=S(14), y=S(76))

# ── BRIGHTNESS ARC ────────────────────────────────────────────────────────────
arc_x = WIN_W - BODY_PAD * 2 - ARC_SIZE - S(190)
arc_canvas = tk.Canvas(body, width=ARC_SIZE, height=ARC_SIZE, bg=BG_DEEP, highlightthickness=0)
arc_canvas.place(x=arc_x, y=0)

initial_b = int(_init_ms.handle.get_brightness()) if _init_ms else 70
_draw_arc(initial_b)

lbl_num = tk.Label(arc_canvas, text=str(initial_b), font=(FONT_UI, 34, "bold"), fg=TEXT_HI, bg=BG_DEEP)
lbl_num.place(relx=0.5, rely=0.42, anchor="center")
tk.Label(arc_canvas, text="BRIGHTNESS", font=(FONT_UI, 7, "bold"), fg=TEXT_MID, bg=BG_DEEP).place(relx=0.5, rely=0.68, anchor="center")

# ── MANUAL SLIDER PANEL ───────────────────────────────────────────────────────
manual_x = arc_x + ARC_SIZE + S(20)
manual_fr = tk.Frame(body, bg=BG_PANEL, highlightbackground=BORDER, highlightthickness=1)
manual_fr.place(x=manual_x, y=0, width=S(170), height=ARC_SIZE)

tk.Label(manual_fr, text="MANUAL OVERRIDE", font=(FONT_UI, 9, "bold"), fg=TEXT_HI, bg=BG_PANEL).place(x=S(14), y=S(12))
tk.Label(manual_fr, text="Lock auto-brightness and\nset a fixed level, or use\nCtrl+Alt+\u2191 / \u2193 anywhere.",
         font=(FONT_UI, 7), fg=TEXT_MID, bg=BG_PANEL, justify="left").place(x=S(14), y=S(32))

manual_slider = tk.Scale(manual_fr, from_=0, to=100, orient="horizontal",
                          bg=BG_PANEL, fg=TEXT_HI, troughcolor=BG_TRACK,
                          highlightthickness=0, bd=0, activebackground=ACCENT,
                          command=_on_manual_slider, length=S(140))
manual_slider.set(int(manual_brightness))
manual_slider.place(x=S(14), y=S(92))

# ── STATUS BAR ────────────────────────────────────────────────────────────────
tk.Frame(root, bg=BORDER, height=1).pack(fill="x")
status_row = tk.Frame(root, bg=BG_DEEP, height=STATUS_H)
status_row.pack(fill="x")
status_row.pack_propagate(False)

lbl_status = tk.Label(status_row, text="● ACTIVE", font=(FONT_UI, 8, "bold"), fg=ACCENT_DIM, bg=BG_DEEP)
lbl_status.pack(side="left", padx=S(20))

if not SBC_AVAILABLE:
    tk.Label(status_row, text="⚠ screen_brightness_control missing", font=(FONT_UI, 8), fg=ACCENT, bg=BG_DEEP).pack(side="right", padx=S(12))
if not CAP_AVAILABLE:
    tk.Label(status_row, text="⚠ mss / cv2 / numpy missing", font=(FONT_UI, 8), fg=ACCENT, bg=BG_DEEP).pack(side="right", padx=S(12))
if not TRAY_AVAILABLE:
    tk.Label(status_row, text="⚠ pystray / Pillow missing — no tray icon", font=(FONT_UI, 8), fg=ACCENT, bg=BG_DEEP).pack(side="right", padx=S(12))
if not hotkeys.KEYBOARD_AVAILABLE:
    tk.Label(status_row, text="⚠ keyboard lib missing — no hotkeys", font=(FONT_UI, 8), fg=ACCENT, bg=BG_DEEP).pack(side="right", padx=S(12))

_refresh_tab_styles()
_sync_manual_ui()

# ─── HOTKEYS ──────────────────────────────────────────────────────────────────
if cfg.get("hotkeys_enabled", True):
    hotkeys.register(cfg.get("hotkey_brightness_up"), lambda: root.after(0, nudge_brightness, MANUAL_NUDGE))
    hotkeys.register(cfg.get("hotkey_brightness_down"), lambda: root.after(0, nudge_brightness, -MANUAL_NUDGE))
    hotkeys.register(cfg.get("hotkey_toggle_pause"), lambda: root.after(0, toggle_pause))

# ─── LAUNCH ───────────────────────────────────────────────────────────────────
for _ms in monitor_states:
    threading.Thread(target=_fade_worker, args=(_ms,), daemon=True).start()
    threading.Thread(target=analyze_loop, args=(_ms,), daemon=True).start()

threading.Thread(target=ambient_loop, daemon=True).start()
_setup_tray()
root.mainloop()
