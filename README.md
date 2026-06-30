\# LumaWatch 🌙



LumaWatch is a lightweight, intelligent adaptive brightness engine for Windows. It samples your screen's content in real time and smoothly adjusts your monitor's hardware brightness via native DDC/CI, so you're not blinded switching from a dark IDE to a white browser tab — the same idea phones use for adaptive brightness, brought to the desktop.



\---



\## ✨ Features



\* \*\*Real-time screen sampling\*\* — efficient downsampled luminance analysis of on-screen content.

\* \*\*Native hardware control\*\* — DDC/CI for external monitors, with automatic fallback (WMI/ACPI via `screen\_brightness\_control`) for laptop internal panels that don't speak DDC/CI.

\* \*\*Multi-monitor support\*\* — every connected display gets its own learned brightness profile and is adjusted independently. Switch between them with the tabs at the top of the window.

\* \*\*Time-of-day brightness ceiling\*\* — like a phone capping max brightness late at night, LumaWatch tapers the highest brightness it will use in the evening and overnight, easing back up at dawn.

\* \*\*Night Light warmth\*\* — an optional warm color-temperature shift after sunset (similar to Windows Night Light / iOS Night Shift), applied via the display gamma ramp.

\* \*\*Manual override\*\* — a slider and global hotkeys (`Ctrl+Alt+↑` / `Ctrl+Alt+↓`) to take direct control any time; auto-adjustment resumes when you toggle back to Auto.

\* \*\*Auto-learning baselines\*\* — LumaWatch watches your manual brightness changes and remembers what "bright content" and "dark content" should look like for you, per monitor.

\* \*\*Anti-flicker smoothing\*\* — a rolling average and a turbulence/lockout system stop brief flashes (like a loading spinner) from triggering a brightness swing.

\* \*\*Auto-start on login\*\* — one checkbox adds/removes LumaWatch from Windows startup (no admin rights required).

\* \*\*System tray support\*\* — closes to tray instead of quitting; pause, override, and quit are all available from the tray menu.



\---



\## 🚀 Getting Started



\### Method 1: Installer (recommended)



1\. Clone or download this repository.

2\. Run `install.bat`. It creates a local virtual environment and installs all dependencies.

3\. Run `run\_lumawatch.bat` to launch LumaWatch (no console window).

4\. In the app, check \*\*"Start with Windows"\*\* if you want it to launch automatically at login.



\### Method 2: Standalone Executable



If a `LumaWatch.exe` is available on the \*\*\[Releases](https://github.com/AKM13567/LumaWatch/releases)\*\* page, download it and run it directly — no Python install required.



\### Method 3: Manual setup from source



```bash

git clone https://github.com/AKM13567/LumaWatch.git

cd LumaWatch

python -m venv .venv

.venv\\Scripts\\activate

pip install -r requirements.txt

python screen\_dimmer.py

```



\---



\## 🖥️ Multi-monitor notes



Each display gets its own tab and its own learned `normal`/`dim` baselines, stored under a stable per-monitor key in `\~/.lumawatch.json`. A monitor without DDC/CI support (common for laptop internal panels) is labeled \*\*"(no DDC)"\*\* in its tab — LumaWatch still samples and tracks it, but brightness changes route through `screen\_brightness\_control`'s OS-level fallback instead of DDC/CI.



> Monitor↔display matching between screen capture (`mss`) and brightness control (`screen\_brightness\_control`) is done by enumeration order, which is reliable on most systems but not guaranteed by Windows. If your tabs seem to control the wrong screen, please open an issue with your `mss`/`sbc` versions and display setup.



\## 🌒 Time-of-day \& Night Light



Both are independent toggles in the app:



\* \*\*Time-of-day ceiling\*\* multiplies the content-derived target brightness by a factor that's `1.0` during the day, ramps down starting 2 hours before your "night" hour, and holds at a configurable floor (default 55%) overnight before ramping back up at dawn.

\* \*\*Night Light warmth\*\* is a separate, optional gamma-ramp tint that warms the whole desktop after sunset, independent of brightness. Strength is configurable (`night\_light\_strength`, 0–100) in `\~/.lumawatch.json`.



Both use a fixed sunset/sunrise approximation rather than your actual geographic location — adjust `night\_start\_hour`, `day\_start\_hour` in `lumawatch/ambient.py` if the defaults (7am–9:30pm "day") don't fit your schedule.



\## ⌨️ Hotkeys



| Action | Default hotkey |

|---|---|

| Brightness up (+5, active monitor) | `Ctrl+Alt+↑` |

| Brightness down (−5, active monitor) | `Ctrl+Alt+↓` |

| Pause / resume engine | `Ctrl+Alt+P` |



Hotkeys are global (work even when LumaWatch isn't focused) and require the `keyboard` package. Pressing a brightness hotkey automatically switches that monitor into manual override mode, the same way moving a phone's brightness slider locks out auto-brightness until you toggle it back.



Hotkey bindings can be changed in `\~/.lumawatch.json` (`hotkey\_brightness\_up`, etc.) using \[`keyboard`'s hotkey syntax](https://github.com/boppreh/keyboard#api).



\---



\## 🧩 Project layout



```

LumaWatch/

├── screen\_dimmer.py      # entry point: UI + per-monitor engine orchestration

├── lumawatch/

│   ├── config.py          # persisted settings \& per-monitor learned baselines

│   ├── monitors.py        # multi-monitor enumeration, capture, DDC control

│   ├── ambient.py         # time-of-day ceiling + night light math (pure, testable)

│   ├── gamma.py            # Windows gamma-ramp control for night light warmth

│   ├── hotkeys.py          # global hotkey registration

│   └── autostart.py        # Windows Run-key autostart toggle

├── install.bat

├── run\_lumawatch.bat

└── requirements.txt

```



\## ⚠️ Known limitations



\* Windows only (DDC/CI, gamma ramp, and registry autostart are all Windows APIs).

\* `keyboard`'s global hook can require running as administrator on some systems/policies — if hotkeys silently don't fire, try running once from an elevated terminal to confirm.

\* Night Light gamma tinting is a single global ramp; it can't be applied per-monitor independently of each other.

\* DDC/CI over some USB-C docks / KVMs is flaky regardless of software — this is a hardware/firmware limitation, not specific to LumaWatch.



\---



\## License



MIT

