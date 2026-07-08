# GlintBar

A slim, always-on-top hardware monitor that lives in the **empty space of your
Windows taskbar** (or docked at the top/bottom). It shows live CPU, RAM, GPU,
disk and network at a glance, and surfaces trouble — thermal / clock / power
spikes — *before* it turns into throttling or a crash (e.g. a
`VIDEO_TDR_FAILURE` GPU hang).

## What it shows

A single-line bar of up to nine live metrics, each with a compact value + a
60-second micro-sparkline. Hover any metric for an expanded live graph with
session **min / max / avg**:

| Tile | Source | Watch for |
|------|--------|-----------|
| GPU Temp | nvidia-smi | amber >80 °C, red >87 °C |
| GPU Load | nvidia-smi / perf counters | sustained 100% |
| VRAM | nvidia-smi | red >95 % |
| GPU Power | nvidia-smi | sudden dropouts under load |
| GPU Clock | nvidia-smi | collapse to idle mid-load = a TDR hang |
| SYS Temp | ACPI thermal zone | red >97 °C |
| CPU | psutil | red >95 % |
| RAM | psutil | red >95 % |
| Network | psutil | MB/s in+out |
| Disk | psutil | MB/s read+write |

Data sources are all first-party / open-source and need **no admin rights**.
The bar only shows the tiles your machine actually supports (see below).

## Supported hardware & platforms

**OS:** Windows 10/11 only (uses Win32 APIs). Not macOS/Linux.

**CPU / RAM / disk / network:** every Windows PC (`psutil`).

**GPU** — auto-detected at launch, best source wins:

| GPU | Load | VRAM | Temp | Clock | Power | Source |
|-----|:----:|:----:|:----:|:-----:|:-----:|--------|
| **NVIDIA** | ✅ | ✅ | ✅ | ✅ | ✅ | `nvidia-smi` (driver) |
| **AMD / Intel / other** | ✅ | — | — | — | — | Windows GPU perf counters |
| **No GPU / headless** | — | — | — | — | — | GPU tiles hidden |

AMD/Intel machines get **GPU load** (temp/clock/power/VRAM need NVIDIA). The
settings panel greys out any tile your hardware can't provide.

**Temperatures & fans.** The **SYS Temp** tile reads the ACPI thermal zone via a
Windows performance counter — no admin, but it's a *generic* zone (a useful
system-heat proxy), not the exact CPU-package sensor, and some machines block it.
**True per-core CPU temperature and fan RPM aren't reachable without kernel-level
access** — they need a helper like [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)
running (with admin). An optional LibreHardwareMonitor integration is on the
[roadmap](ROADMAP.md).

**Requirements:** Python 3.8+, `pip install -r requirements.txt`
(`pywebview`, `psutil`), and the WebView2 runtime (preinstalled on Windows 11).
No admin, no code-signing, no binaries — it's plain Python source, the friendliest
form for locked-down / corporate machines.

## Run

```
pip install -r requirements.txt
python monitor.py
```

For a no-console launch, double-click **`start_glintbar.vbs`**, or
**`start_glintbar.cmd`** if Windows Script Host is blocked. Both find `pythonw`
on your PATH, so they work on any machine (no hardcoded paths).

Controls (right edge): **⚙** settings · **●** toggles CSV logging (turns red
while recording) · **▤** opens the log folder · **✕** closes.

To start on login, put a shortcut to `start_glintbar.vbs` in `shell:startup`
(Win+R → `shell:startup`).

## Customize (⚙ settings)

- **Metrics** — check the ones you want; presets for *Essentials*, *GPU focus*,
  or *All*. The bar rebuilds and auto-resizes to fit only what you keep.
- **Size** — Compact / Normal / Large.
- **Align** — Left / Center / Right within the taskbar gap.
- **Sparklines**, **Critical alerts**, and **Alert sound** — on/off.

Choices persist to `config.json`.

## Placement

Set `DOCK` at the top of `monitor.py`:

- `"taskbar"` (default) — floats as a **topmost bar over the taskbar's empty
  area** (between your app buttons and the system tray). It's a real top-level
  window sitting *above* the Windows 11 taskbar's XAML input layer, so it owns its
  own hovers and clicks. Uses zero extra screen space. A background watcher keeps
  it fitted to the gap and on top as apps open/close.
- `"bottom"` — a slim full-width strip just above the taskbar.
- `"top"` — a slim full-width strip at the top of the screen.

Assumes a single primary monitor with a bottom taskbar.

## Hover to expand

Hover any metric and a larger popup rises just above it with a big live
60-second graph, the current value, and min/max/avg — a quick detailed peek that
disappears when you move away.

## Critical alerts

When a metric crosses a critical threshold (GPU temp > 87 °C, CPU/RAM/VRAM >
95%), an **alert tile pops in** at the end of the bar — additive, so it never
displaces your chosen tiles — and stays until things calm down (2s to appear,
hysteresis + cooldown to clear). An optional soft **beep** fires on a new breach
so you catch it even when you're not looking. Both toggle in ⚙.

## CSV logging

Hit **●** to write one row per second to `logs/glintbar_<timestamp>.csv` with
every metric. Import it into pandas/Excel and align it to a workload timeline to
correlate throttling, thermal drift, or power dropouts with load.

```python
import pandas as pd
df = pd.read_csv("logs/glintbar_<timestamp>.csv", parse_dates=["timestamp"])
df.set_index("timestamp")[["gpu_temp", "gpu_clock", "gpu_power"]].plot()
```

## Notes

- Adapts to any resolution and display scaling (DPI is detected at runtime).
- Metric thresholds live in the `META` map at the top of `ui.html` — tweak freely.
- Deferred ideas (attention rotation, anomaly detection, toast notifications,
  AMD/Intel temperatures) are captured in [ROADMAP.md](ROADMAP.md).

## License

MIT — see [LICENSE](LICENSE).
