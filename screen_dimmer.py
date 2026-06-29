"""
LumaWatch — Intelligent Adaptive Brightness Engine
Temporal smoothing · Anti-flicker · Auto-learning baselines
System tray support · Persistent config · Low-resource worker queue
"""

import os
import json
import time
import queue
import threading
import pathlib
import tkinter as tk
from collections import deque

# ─── OPTIONAL DEPENDENCY GUARDS ───────────────────────────────────────────────
try:
    import screen_brightness_control as sbc
    SBC_AVAILABLE = True
except ImportError:
    SBC_AVAILABLE = False

try:
    from mss import mss as MSS
    import cv2
    import numpy as np
    CAP_AVAILABLE = True
except ImportError:
    CAP_AVAILABLE = False

try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False

# ─── CONFIG FILE ──────────────────────────────────────────────────────────────
CONFIG_PATH = pathlib.Path.home() / ".lumawatch.json"

def load_config():
    """Load persisted baselines from disk, returning defaults if absent."""
    if CONFIG_PATH.exists():
        try:
            d = json.loads(CONFIG_PATH.read_text())
            return float(d.get("normal", 70.0)), float(d.get("dim", 30.0))
        except Exception:
            pass
    return 70.0, 30.0

def save_config():
    """Persist current baselines to disk (called from analysis thread)."""
    try:
        CONFIG_PATH.write_text(json.dumps({
            "normal": normal_brightness,
            "dim":    dim_brightness,
        }))
    except Exception:
        pass

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
WHITE_THRESH      = 160    # pixel brightness (0-255) counted as "white"
WHITE_RATIO_LOW   = 0.10   # below this → full normal brightness
WHITE_RATIO_HIGH  = 0.50   # above this → full dim brightness
CHECK_INTERVAL    = 1.0    # seconds between samples
IDLE_INTERVAL     = 2.0    # extended sleep when nothing changed last cycle
CAPTURE_SIZE      = 48     # downsample to 48×48
SMOOTHING_WINDOW  = 5
TURBULENCE_THRESH = 15
LOCKOUT_DURATION  = 1.5
MIN_DELTA         = 4      # minimum brightness change to bother fading

# ─── ADAPTIVE BASELINES (loaded from config) ──────────────────────────────────
normal_brightness, dim_brightness   = load_config()
normal_brightness_confirmed         = False
dim_brightness_confirmed            = False

# ─── ENGINE STATE ─────────────────────────────────────────────────────────────
current_hw_brightness = 70.0
last_target           = 70.0
last_change_time      = 0.0
last_raw_target       = 70.0
lockout_until         = 0.0
engine_paused         = False
is_fading             = False
fade_lock             = threading.Lock()
state_lock            = threading.Lock()
luminance_history     = deque(maxlen=SMOOTHING_WINDOW)

# Single persistent fade-worker queue
_fade_queue: "queue.Queue[float | None]" = queue.Queue(maxsize=1)

# ─── DESIGN TOKENS ────────────────────────────────────────────────────────────
BG_DEEP    = "#0A0A0A"
BG_PANEL   = "#111111"
BG_TRACK   = "#1E1E1E"
ACCENT     = "#D92B2B"
ACCENT_DIM = "#7A1515"
ACCENT_GRN = "#2BD97A"
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
ARC_DIAM = S(180)
ARC_PAD  = ARC_W + S(4)
ARC_SIZE = ARC_DIAM + ARC_PAD * 2

TOPBAR_H  = S(44)
STATUS_H  = S(30)
BODY_PAD  = S(16)
WIN_H     = TOPBAR_H + 1 + ARC_SIZE + BODY_PAD * 2 + 1 + STATUS_H
WIN_W     = S(680)

# ─── BRIGHTNESS HELPERS ───────────────────────────────────────────────────────
_sbc_fail_count   = 0
_SBC_FAIL_LIMIT   = 5      
_sbc_warned       = False  

def _safe_get_brightness():
    """Read hardware brightness. Returns cached value on any failure."""
    global _sbc_fail_count, _sbc_warned
    if not SBC_AVAILABLE:
        return current_hw_brightness
    try:
        r = sbc.get_brightness()
        if r and r[0] is not None:
            _sbc_fail_count = 0          
            return float(r[0])
        raise ValueError("sbc returned empty result")
    except Exception as exc:
        _sbc_fail_count += 1
        if _sbc_fail_count >= _SBC_FAIL_LIMIT and not _sbc_warned:
            _sbc_warned = True
            root.after(0, _ui_status,
                       "⚠ HW brightness unavailable — check DDC/CI in monitor settings", ACCENT)
        print(f"[SBC GET] failure #{_sbc_fail_count}: {exc}")
    return current_hw_brightness

def _safe_set_brightness(value):
    """Set hardware brightness. Logs and counts failures; never raises."""
    global _sbc_fail_count, _sbc_warned
    if not SBC_AVAILABLE:
        return
    try:
        sbc.set_brightness(int(value))
        _sbc_fail_count = 0              
        if _sbc_warned:
            _sbc_warned = False
            root.after(0, _ui_status, "● ACTIVE", ACCENT_DIM)
    except Exception as exc:
        _sbc_fail_count += 1
        if _sbc_fail_count >= _SBC_FAIL_LIMIT and not _sbc_warned:
            _sbc_warned = True
            root.after(0, _ui_status,
                       "⚠ HW brightness unavailable — check DDC/CI in monitor settings", ACCENT)
        print(f"[SBC SET] failure #{_sbc_fail_count}: {exc}")

# ─── PERSISTENT FADE WORKER ───────────────────────────────────────────────────
def _fade_worker():
    """Long-lived daemon thread. Reads fade targets from _fade_queue."""
    global current_hw_brightness, last_target, is_fading
    while True:
        target = _fade_queue.get()        
        if target is None:                
            break

        if not fade_lock.acquire(blocking=False):
            _fade_queue.task_done()
            continue
        try:
            with state_lock:
                is_fading = True

            cur = _safe_get_brightness()
            with state_lock:
                current_hw_brightness = cur
                last_target = float(target)

            if abs(cur - target) < 2:
                continue

            total_range = abs(normal_brightness - dim_brightness)
            if total_range > 0 and abs(target - cur) >= total_range * 0.85:
                _safe_set_brightness(target)
                with state_lock:
                    current_hw_brightness = float(target)
                root.after(0, _ui_brightness, float(target))
                continue

            step = 5 if target > cur else -5
            b = int(cur)
            while (step > 0 and b < target) or (step < 0 and b > target):
                b = min(target, b + step) if step > 0 else max(target, b + step)
                _safe_set_brightness(b)
                with state_lock:
                    current_hw_brightness = float(b)
                root.after(0, _ui_brightness, float(b))
                time.sleep(0.005)

            _safe_set_brightness(target)
            with state_lock:
                current_hw_brightness = float(target)
            root.after(0, _ui_brightness, float(target))

        finally:
            with state_lock:
                is_fading = False
            fade_lock.release()
            _fade_queue.task_done()

def fade_to_target(target):
    """Enqueue a fade target. Drops silently if a fade is already queued."""
    try:
        _fade_queue.put_nowait(target)
    except queue.Full:
        pass  

# ─── ANALYSIS LOOP ────────────────────────────────────────────────────────────
def analyze_loop():
    global normal_brightness, dim_brightness, last_change_time
    global current_hw_brightness, last_target, last_raw_target
    global lockout_until, engine_paused, is_fading
    global normal_brightness_confirmed, dim_brightness_confirmed

    initial = _safe_get_brightness()
    with state_lock:
        current_hw_brightness = initial
        last_target = initial
    root.after(0, _ui_brightness, initial)

    if not CAP_AVAILABLE:
        root.after(0, _ui_status, "⚠ mss / cv2 / numpy missing — capture unavailable", ACCENT)
        return

    nothing_changed_last = False   

    with MSS() as sct:
        monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]

        while True:
            sleep_for = IDLE_INTERVAL if nothing_changed_last else CHECK_INTERVAL
            time.sleep(sleep_for)

            if engine_paused:
                nothing_changed_last = True
                continue

            try:
                now = time.time()
                actual = _safe_get_brightness()
                root.after(0, _ui_brightness, actual)

                shot  = sct.grab(monitor)
                img   = np.frombuffer(shot.raw, dtype=np.uint8).reshape(
                            shot.height, shot.width, 4)
                small = cv2.resize(img, (CAPTURE_SIZE, CAPTURE_SIZE),
                                   interpolation=cv2.INTER_AREA)
                gray  = cv2.cvtColor(small, cv2.COLOR_BGRA2GRAY)

                white_ratio     = float(np.count_nonzero(gray > WHITE_THRESH)) / gray.size
                is_white_screen = white_ratio > 0.30

                with state_lock:
                    hw, lt, fading = current_hw_brightness, last_target, is_fading

                if not fading and abs(actual - hw) > 2 and abs(actual - lt) > 2:
                    with state_lock:
                        if is_white_screen:
                            dim_brightness            = actual
                            dim_brightness_confirmed  = True
                        else:
                            normal_brightness           = actual
                            normal_brightness_confirmed = True
                        current_hw_brightness = actual
                        last_target           = actual
                        last_change_time      = now
                    save_config()   
                    root.after(0, _ui_memory_labels)
                else:
                    with state_lock:
                        current_hw_brightness = actual

                with state_lock:
                    max_b, min_b = normal_brightness, dim_brightness

                if abs(max_b - min_b) < 5:
                    nothing_changed_last = True
                    continue

                if white_ratio <= WHITE_RATIO_LOW:
                    raw = max_b
                elif white_ratio >= WHITE_RATIO_HIGH:
                    raw = min_b
                else:
                    t   = (white_ratio - WHITE_RATIO_LOW) / (WHITE_RATIO_HIGH - WHITE_RATIO_LOW)
                    raw = max_b - t * (max_b - min_b)
                raw = max(0.0, min(100.0, raw))

                with state_lock:
                    lrt = last_raw_target
                if abs(raw - lrt) >= TURBULENCE_THRESH:
                    with state_lock:
                        lockout_until = now + LOCKOUT_DURATION
                    root.after(0, _ui_status, "⚡ TAB ACTIVITY — brightness frozen", ACCENT)
                    nothing_changed_last = False
                else:
                    root.after(0, _ui_status, "● ACTIVE", ACCENT_DIM)
                with state_lock:
                    last_raw_target = raw

                luminance_history.append(raw)
                smoothed = int(round(sum(luminance_history) / len(luminance_history)))

                with state_lock:
                    lu, lct, chw = lockout_until, last_change_time, current_hw_brightness

                if now > lu and (now - lct > LOCKOUT_DURATION) and abs(smoothed - chw) >= MIN_DELTA:
                    with state_lock:
                        last_change_time = now
                    fade_to_target(smoothed)
                    nothing_changed_last = False
                else:
                    nothing_changed_last = True   

            except Exception as exc:
                print(f"[ERROR] {exc}")
                nothing_changed_last = False

# ─── UI CALLBACKS ────────────────────────────────────────────────────────────
def _ui_brightness(val):
    try:
        lbl_num.config(text=str(int(round(val))))
        _draw_arc(int(round(val)))
    except Exception:
        pass

def _ui_memory_labels():
    try:
        # Fixed label mapping assignment: Target values swapped to properly mirror their meanings
        lbl_bright_val.config(
            text=f"{int(normal_brightness)}%" + ("" if normal_brightness_confirmed else "  (default)"),
            fg=ACCENT if normal_brightness_confirmed else TEXT_MID
        )
        lbl_dim_val.config(
            text=f"{int(dim_brightness)}%" + ("" if dim_brightness_confirmed else "  (default)"),
            fg=ACCENT if dim_brightness_confirmed else TEXT_MID
        )
    except Exception:
        pass

def _ui_status(text, color=ACCENT_DIM):
    try:
        lbl_status.config(text=text, fg=color)
    except Exception:
        pass

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
    d   = ImageDraw.Draw(img)
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
        pystray.MenuItem("Quit",           lambda: root.after(0, quit_app)),
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
    try:
        _fade_queue.put_nowait(None)
    except queue.Full:
        pass
    try:
        root.destroy()
    finally:
        os._exit(0)

# ─── BUILD UI ─────────────────────────────────────────────────────────────────
root = tk.Tk()
root.title("LumaWatch")
root.configure(bg=BG_DEEP)

# Enabled dynamic user scaling: Changed from False to True
root.resizable(True, True)
root.minsize(WIN_W, WIN_H) # Prevent shrinking smaller than design elements
root.protocol("WM_DELETE_WINDOW", _hide_to_tray if TRAY_AVAILABLE else quit_app)

sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
root.geometry(f"{WIN_W}x{WIN_H}+{(sw-WIN_W)//2}+{(sh-WIN_H)//2}")

# ── TOP BAR ───────────────────────────────────────────────────────────────────
topbar = tk.Frame(root, bg=BG_DEEP, height=TOPBAR_H)
topbar.pack(fill="x")
topbar.pack_propagate(False)

tk.Label(topbar, text="◈", font=(FONT_UI, 11), fg=ACCENT, bg=BG_DEEP).pack(
    side="left", padx=(S(20), S(6)))
tk.Label(topbar, text="LUMAWATCH", font=(FONT_UI, 10, "bold"), fg=TEXT_HI, bg=BG_DEEP).pack(
    side="left")
tk.Label(topbar, text="  —  ADAPTIVE BRIGHTNESS", font=(FONT_UI, 8), fg=TEXT_MID, bg=BG_DEEP).pack(
    side="left")

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

if TRAY_AVAILABLE:
    tk.Label(topbar, text="× closes to tray", font=(FONT_UI, 7), fg=TEXT_MID, bg=BG_DEEP).pack(
        side="left", padx=(S(12), 0))

tk.Frame(root, bg=BORDER, height=1).pack(fill="x")

# ── BODY CONTAINER ────────────────────────────────────────────────────────────
# Configured container structure to center perfectly during scaling changes
body_container = tk.Frame(root, bg=BG_DEEP)
body_container.pack(expand=True, fill="both")

body = tk.Frame(body_container, bg=BG_DEEP, width=WIN_W - BODY_PAD*2, height=ARC_SIZE)
body.pack(expand=True)
body.pack_propagate(False)

left = tk.Frame(body, bg=BG_DEEP)
left.place(x=0, y=0, width=WIN_W - BODY_PAD * 2 - ARC_SIZE - S(20), height=ARC_SIZE)

def _info_row(parent, title, desc, val_text, y_offset):
    h = ARC_SIZE // 2 - S(6)
    fr = tk.Frame(parent, bg=BG_PANEL, highlightbackground=BORDER, highlightthickness=1)
    fr.place(x=0, y=y_offset, relwidth=1.0, height=h)

    tk.Label(fr, text=title, font=(FONT_UI, 10, "bold"), fg=TEXT_HI,
             bg=BG_PANEL, anchor="w").place(x=S(14), y=S(10))
    tk.Label(fr, text=desc, font=(FONT_UI, 8), fg=TEXT_MID,
             bg=BG_PANEL, anchor="w").place(x=S(14), y=S(28))

    val = tk.Label(fr, text=val_text, font=(FONT_UI, 15, "bold"),
                   fg=TEXT_MID, bg=BG_PANEL, anchor="e")
    val.place(relx=1.0, rely=0.5, x=-S(16), anchor="e")
    return val

# Swapped rows: "BRIGHT SCREEN" uses normal_brightness, "DARK SCREEN" uses dim_brightness
lbl_bright_val = _info_row(left, "BRIGHT SCREEN TARGET",
                             "Auto-learned — bright / white content",
                             f"{int(normal_brightness)}%" + ("" if normal_brightness_confirmed else "  (default)"),
                             0)
lbl_dim_val    = _info_row(left, "DARK SCREEN TARGET",
                             "Auto-learned — dark / low-light content",
                             f"{int(dim_brightness)}%" + ("" if dim_brightness_confirmed else "  (default)"),
                             ARC_SIZE // 2 + S(4))

arc_x = WIN_W - BODY_PAD * 2 - ARC_SIZE
arc_canvas = tk.Canvas(body, width=ARC_SIZE, height=ARC_SIZE,
                        bg=BG_DEEP, highlightthickness=0)
arc_canvas.place(x=arc_x, y=0)

initial_b = int(_safe_get_brightness())
_draw_arc(initial_b)

lbl_num = tk.Label(arc_canvas, text=str(initial_b),
                    font=(FONT_UI, 40, "bold"), fg=TEXT_HI, bg=BG_DEEP)
lbl_num.place(relx=0.5, rely=0.42, anchor="center")

tk.Label(arc_canvas, text="BRIGHTNESS", font=(FONT_UI, 7, "bold"),
         fg=TEXT_MID, bg=BG_DEEP).place(relx=0.5, rely=0.68, anchor="center")

# ── STATUS BAR ────────────────────────────────────────────────────────────────
tk.Frame(root, bg=BORDER, height=1).pack(fill="x")
status_row = tk.Frame(root, bg=BG_DEEP, height=STATUS_H)
status_row.pack(fill="x")
status_row.pack_propagate(False)

lbl_status = tk.Label(status_row, text="● ACTIVE",
                        font=(FONT_UI, 8, "bold"), fg=ACCENT_DIM, bg=BG_DEEP)
lbl_status.pack(side="left", padx=S(20))

if not SBC_AVAILABLE:
    tk.Label(status_row, text="⚠ screen_brightness_control missing",
             font=(FONT_UI, 8), fg=ACCENT, bg=BG_DEEP).pack(side="right", padx=S(12))
if not CAP_AVAILABLE:
    tk.Label(status_row, text="⚠ mss / cv2 / numpy missing",
             font=(FONT_UI, 8), fg=ACCENT, bg=BG_DEEP).pack(side="right", padx=S(12))
if not TRAY_AVAILABLE:
    tk.Label(status_row, text="⚠ pystray / Pillow missing — no tray icon",
             font=(FONT_UI, 8), fg=ACCENT, bg=BG_DEEP).pack(side="right", padx=S(12))

# ─── LAUNCH ───────────────────────────────────────────────────────────────────
threading.Thread(target=_fade_worker,  daemon=True).start()   
threading.Thread(target=analyze_loop,  daemon=True).start()   
_setup_tray()                                                  
root.mainloop()