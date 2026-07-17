# GlintBar

![GlintBar sitting in the Windows taskbar: CPU, RAM, SYS, GPU, VRAM, clock, power, disk and network tiles with sparklines](https://raw.githubusercontent.com/paone9/GlintBar/main/docs/hero.png)

[![PyPI](https://img.shields.io/pypi/v/glintbar)](https://pypi.org/project/glintbar/)
[![CI](https://github.com/paone9/GlintBar/actions/workflows/ci.yml/badge.svg)](https://github.com/paone9/GlintBar/actions/workflows/ci.yml)
[![CodeQL](https://github.com/paone9/GlintBar/actions/workflows/codeql.yml/badge.svg)](https://github.com/paone9/GlintBar/actions/workflows/codeql.yml)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/paone9/GlintBar/badge)](https://scorecard.dev/viewer/?uri=github.com/paone9/GlintBar)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

GlintBar turns the empty gap in your Windows taskbar into a live hardware
monitor: no window to keep open, no screen space given up. CPU, RAM, GPU, disk,
and network sit there at a glance, each with a 60-second sparkline and a
hover-to-expand graph. It quietly flags thermal, clock, and power spikes as they
build, so throttling, driver hangs, and runaway background processes show up on
the bar before they cost you a stutter or a crash.

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
no-admin tool can do. For those, run a helper that already does the low-level
reads with admin, and GlintBar reads from it while staying at normal privilege.
It picks up the extra **CPU Temp** and **Fan** tiles automatically when one is
present. Two options, whichever you already use:

**HWiNFO** (via shared memory): in HWiNFO, open Settings and tick **Shared Memory
Support**. That's it. GlintBar reads the shared-memory block directly (no network
at all). This is easiest if you already run HWiNFO.

**LibreHardwareMonitor** (open source, via a local web server): in its Options
menu turn on **Remote Web Server** (default port 8085). GlintBar reads only from
`http://127.0.0.1:8085` on your own machine, never the internet. Use
`GLINTBAR_LHM_PORT` if you change the port.

Either way, start the helper before GlintBar (or restart GlintBar) and the tiles
appear on their own. HWiNFO is preferred if both are running.

### Requirements

Python 3.8+ and the WebView2 runtime, which already ships with Windows 11. No
admin, no compiled binaries, just Python source. The only dependencies are
`pywebview` and `psutil`, installed automatically.

## Install and run

The simplest install (adds a `glintbar` command):

```
pip install glintbar
glintbar
```

Or with `pipx` for a fully isolated install: `pipx install glintbar`.

From a clone, for hacking on it:

```
pip install -r requirements.txt
python -m glintbar
```

For a launch with no console window, double-click `start_glintbar.vbs` (or
`start_glintbar.cmd` if Windows Script Host is blocked). To start it on login,
drop a shortcut to the `glintbar` command (or to `start_glintbar.vbs`) into
`shell:startup` (Win+R, then `shell:startup`).

Controls sit on the right: the gear opens settings, ● toggles CSV logging (it
turns red while recording), ▤ opens the log folder, and ✕ closes.

Settings and logs live in `%LOCALAPPDATA%\GlintBar` (`config.json` and `logs\`),
so they survive reinstalls and stay out of the install folder.

## Settings

Open the gear to change:

- Metrics: pick the ones you want, with presets for Essentials, GPU focus, or
  All. The bar rebuilds and resizes to fit only what you keep.
- Size: Compact, Normal, or Large.
- Align: Left, Center, or Right within the taskbar gap.
- Sparklines, critical alerts, and alert sound: on or off.

Your choices are saved to `config.json` in `%LOCALAPPDATA%\GlintBar`. A few
options have no toggle and live only in that file, for example `temp_unit`
(`"C"` or `"F"`, default `"C"`) to show temperatures in Fahrenheit.

## Placement

Set `DOCK` at the top of `glintbar/monitor.py`:

- `"taskbar"` (default) floats a topmost bar over the empty part of the taskbar,
  between your app buttons and the system tray. It's a normal top-level window
  that sits above the Windows 11 taskbar's input layer, so its hovers and clicks
  work and it uses no extra screen space. A small background thread keeps it
  fitted to the gap and on top as apps open and close.
- `"bottom"` is a thin full-width strip just above the taskbar.
- `"top"` is a thin full-width strip at the top of the screen.

It lives on your **primary monitor's** taskbar (the one Windows treats as
primary), and expects that taskbar along the bottom. On a multi-monitor setup with
different scaling per monitor it stays correctly placed and sized, since the app is
per-monitor-DPI aware. Only one copy runs at a time; launching it again just hands
back to the running one. Letting you pick a different monitor is on the
[roadmap](ROADMAP.md).

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
- Light footprint: CPU, RAM, disk and network update every second, while the GPU
  (which means spawning `nvidia-smi`) is polled every two seconds and cached in
  between, so the monitor itself stays cheap.
- Thresholds live in the `META` map at the top of `glintbar/ui.html`, so they're
  easy to change.
- Ideas that aren't built yet (attention rotation, anomaly detection, toast
  notifications, AMD/Intel temperatures) are written up in [ROADMAP.md](ROADMAP.md).

## Security

GlintBar is meant to be easy to review. It's plain Python with no binaries and no
obfuscation, it needs no admin, and it makes no network connections at all: no
telemetry, no update checks, nothing. It only reads system metrics and writes
`config.json` and logs inside its own folder.

CI runs `ruff` and `bandit` on every push, and a `CodeQL` workflow runs semantic
static analysis of the Python, the UI JavaScript, and the workflows themselves.
GitHub Actions are pinned to commit SHAs and the two dependencies are pinned to
their tested versions, so the build can't shift under you. An
[OpenSSF Scorecard](https://scorecard.dev/viewer/?uri=github.com/paone9/GlintBar)
grades the repo's security posture (badge above). See the repo's Security tab, and
[SECURITY.md](SECURITY.md) for exactly what the app touches.

## License

MIT. See [LICENSE](LICENSE).
