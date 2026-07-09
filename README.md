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

### Temperatures and fans

The SYS Temp tile reads the ACPI thermal zone through a Windows performance
counter, so it needs no admin. It's a generic zone though, so treat it as a rough
system-heat reading rather than the exact CPU-package sensor, and some machines
block it. Getting true per-core CPU temperatures and fan RPM needs kernel-level
access, which in practice means running a helper like
[LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)
(with admin). That integration is on the [roadmap](ROADMAP.md).

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
