# GlintBar

[![CI](https://github.com/paone9/GlintBar/actions/workflows/ci.yml/badge.svg)](https://github.com/paone9/GlintBar/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A small always-on-top hardware monitor that sits in the empty part of your
Windows taskbar (or docked at the top/bottom). It shows live CPU, RAM, GPU, disk
and network so you can keep half an eye on them, and it flags thermal, clock or
power spikes early, before they turn into throttling or a crash like a
`VIDEO_TDR_FAILURE` GPU hang.

## What it shows

A single row of up to nine metrics, each with a value and a 60-second sparkline.
Hover a metric to pop out a bigger live graph with the session min, max and
average.

| Tile | Source | Watch for |
|------|--------|-----------|
| GPU Temp | nvidia-smi | amber >80 °C, red >87 °C |
| GPU Load | nvidia-smi / perf counters | sustained 100% |
| VRAM | nvidia-smi | red >95 % |
| GPU Power | nvidia-smi | sudden dropouts under load |
| GPU Clock | nvidia-smi | dropping to idle mid-load can mean a TDR hang |
| SYS Temp | ACPI thermal zone | red >97 °C |
| CPU Temp | LibreHardwareMonitor | real CPU package temp (needs LHM, see below) |
| Fan | LibreHardwareMonitor | fan RPM (needs LHM, see below) |
| CPU | psutil | red >95 % |
| RAM | psutil | red >95 % |
| Network | psutil | MB/s in+out |
| Disk | psutil | MB/s read+write |

Everything comes from first-party or open-source sources and runs without admin
rights. The bar only shows tiles your machine can actually provide (details
below).

## Supported hardware and platforms

Windows 10/11 only. It uses Win32 APIs, so there's no macOS or Linux build.

CPU, RAM, disk and network work on any Windows PC, via `psutil`.

GPU support is detected at launch:

| GPU | Load | VRAM | Temp | Clock | Power | Source |
|-----|:----:|:----:|:----:|:-----:|:-----:|--------|
| NVIDIA | ✅ | ✅ | ✅ | ✅ | ✅ | `nvidia-smi` (driver) |
| AMD / Intel / other | ✅ | — | — | — | — | Windows GPU perf counters |
| No GPU / headless | — | — | — | — | — | GPU tiles hidden |

AMD and Intel GPUs get load only. Temp, clock, power and VRAM need NVIDIA. The
settings panel greys out anything your hardware can't provide.

### Privilege levels: it works without admin, and does more with it

GlintBar itself never needs administrator rights, and it runs fine on its own. At
normal privilege you get everything except real CPU temperature and fan speed:
CPU, RAM, disk, network, GPU (per the table above), and the SYS Temp tile.

The SYS Temp tile reads the ACPI thermal zone through a Windows performance
counter, so it needs no admin. It's a generic zone though, so treat it as a rough
system-heat reading, not the exact CPU-package sensor, and some machines block it.

Real per-core CPU temperature and fan RPM need kernel-level access, which no
no-admin tool can do. For those, run
[LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)
(open source). It loads a signed driver with admin and does the low-level reads;
GlintBar stays at normal privilege and just reads its data. So GlintBar runs fully
at normal privilege with a reduced sensor set, and picks up the extra CPU Temp and
Fan tiles automatically when LibreHardwareMonitor is present.

To enable it:

1. Install and run LibreHardwareMonitor (run it as admin so it can read the
   sensors).
2. In its Options menu, turn on **Remote Web Server** (default port 8085).
3. Start LibreHardwareMonitor before GlintBar, or restart GlintBar. The CPU Temp
   and Fan tiles appear on their own.

GlintBar reads only from `http://127.0.0.1:8085` on your own machine, never the
internet. If you run the web server on a different port, set the
`GLINTBAR_LHM_PORT` environment variable to match.

### Requirements

Python 3.8+, `pip install -r requirements.txt` (`pywebview` and `psutil`), and
the WebView2 runtime, which already ships with Windows 11. No admin, no compiled
binaries, just Python source.

## Run

```
pip install -r requirements.txt
python monitor.py
```

For a launch with no console window, double-click `start_glintbar.vbs`, or
`start_glintbar.cmd` if Windows Script Host is blocked. Both find `pythonw` on
your PATH, so there are no hardcoded paths to fix.

Controls sit on the right: the gear opens settings, ● toggles CSV logging (it
turns red while recording), ▤ opens the log folder, and ✕ closes.

To start it on login, drop a shortcut to `start_glintbar.vbs` into
`shell:startup` (Win+R, then `shell:startup`).

## Settings

Open the gear to change:

- Metrics: pick the ones you want, with presets for Essentials, GPU focus, or
  All. The bar rebuilds and resizes to fit only what you keep.
- Size: Compact, Normal, or Large.
- Align: Left, Center, or Right within the taskbar gap.
- Sparklines, critical alerts, and alert sound: on or off.

Your choices are saved to `config.json`.

## Placement

Set `DOCK` at the top of `monitor.py`:

- `"taskbar"` (default) floats a topmost bar over the empty part of the taskbar,
  between your app buttons and the system tray. It's a normal top-level window
  that sits above the Windows 11 taskbar's input layer, so its hovers and clicks
  work and it uses no extra screen space. A small background thread keeps it
  fitted to the gap and on top as apps open and close.
- `"bottom"` is a thin full-width strip just above the taskbar.
- `"top"` is a thin full-width strip at the top of the screen.

It assumes a single primary monitor with a bottom taskbar.

When a video, game, or slideshow goes fullscreen on the same screen, the bar
hides itself so it's not in the way, and it comes back when you exit fullscreen.

## Hover to expand

Hover a metric and a bigger popup appears just above it with a full 60-second
graph, the current value, and min/max/avg. It disappears when you move off.

## Critical alerts

When a metric crosses a critical threshold (GPU temp over 87 °C, or CPU, RAM or
VRAM over 95%), an extra tile appears at the end of the bar. It's added rather
than swapped in, so your other tiles don't move, and it stays until things settle
(about two seconds to appear, with a short cooldown before it clears). There's an
optional beep on a new alert so you notice without watching. Both are toggles in
settings.

## Away watch

If you step away and the machine keeps working hard, GlintBar figures out what was
responsible and tells you when you get back. It counts you as away as soon as the
screen is locked, or after a few minutes with no keyboard or mouse activity. While
you're away and the CPU stays high, it records which processes are behind it. When
you return it shows a short summary (how long you were away, the peak CPU and
temperature, and the busiest processes) and appends a line to `logs/away.csv`. If
nothing unusual happened, it stays quiet.

Toggle it in settings. The idle delay and CPU threshold are in `config.json`
(`away_after_min`, `away_cpu_pct`), defaulting to 5 minutes and 25%.

## CSV logging

Press ● to write one row per second to `logs/glintbar_<timestamp>.csv` with every
metric. Open it in pandas or Excel and line it up against a workload timeline to
see how throttling, thermal drift or power dropouts track with load.

```python
import pandas as pd
df = pd.read_csv("logs/glintbar_<timestamp>.csv", parse_dates=["timestamp"])
df.set_index("timestamp")[["gpu_temp", "gpu_clock", "gpu_power"]].plot()
```

## Notes

- It adapts to any resolution and display scaling; DPI is read at runtime.
- Thresholds live in the `META` map at the top of `ui.html`, so they're easy to
  change.
- Ideas that aren't built yet (attention rotation, anomaly detection, toast
  notifications, AMD/Intel temperatures) are written up in [ROADMAP.md](ROADMAP.md).

## Security

GlintBar is meant to be easy to review. It's plain Python with no binaries and no
obfuscation, it needs no admin, and it makes no network connections at all: no
telemetry, no update checks, nothing. It only reads system metrics and writes
`config.json` and logs inside its own folder.

CI runs `ruff` and `bandit` on every push, and GitHub's default CodeQL scanning
runs semantic analysis. See the CI badge above and the repo's Security tab.
[SECURITY.md](SECURITY.md) lists exactly what it touches.

## License

MIT. See [LICENSE](LICENSE).
