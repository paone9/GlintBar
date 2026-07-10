# GlintBar roadmap

This is a running list of ideas that aren't built yet, with the reasoning, so
they're easy to pick up later. The goal has been to ship a small solid thing
first (see the README) rather than pile on features.

## Done

- Bar that floats in the taskbar gap (a topmost window that owns its own input on
  Windows 11).
- Configurable metrics, size and alignment, hover-to-expand graph, CSV logging.
- GPU support across vendors: NVIDIA full, AMD/Intel utilization, or none.
- SYS Temp tile from the ACPI thermal zone, no admin needed.
- Additive critical alert: a tile appears on a sustained critical reading (about
  2s to show, with hysteresis and an 8s cooldown before it clears), without
  moving your other tiles, plus an optional beep. Toggle in settings.
- Placement watcher that keeps the bar fitted to the gap and on top as apps open
  and close or Explorer restarts.
- Hides while a fullscreen app (video, game, slideshow) is on the same screen.
- Away watch: while you're idle, it records which process is behind a busy
  machine and shows a summary (plus `logs/away.csv`) when you return.

## Ideas, not built yet

### 1. Attention rotation (pinned plus auto slots)

A few slots that rotate to show whichever elevated-but-not-critical metric is
most interesting right now, with anti-flap: a delay before a metric can enter, a
long minimum dwell, a bias toward whatever's already showing, and in-place swaps.
Held off on this because motion in the taskbar is distracting and the colour
coding already pulls your eye, and the additive alert already covers the case
that matters most. Worth doing only if the static bar plus alerts turns out to
miss things in real use.

### 2. Full tiered layout (rotation plus alerts)

Metrics move through three zones by severity: pinned anchors that never move,
auto slots that rotate warm metrics, and additive tiles for critical ones. Set
auto slots to zero and you get today's behaviour; set pinned to zero and it's
full rotation. Build this on top of idea 1 once rotation exists.

### 3. Anomaly detection

A rolling baseline (EWMA or a z-score over the 60-second window) to catch sudden
changes rather than just absolute levels. The main target is a GPU clock
collapsing to idle while utilization is high, which is the fingerprint of a
`VIDEO_TDR_FAILURE`, plus power dropouts. The tricky part is disk and network,
which are naturally spiky, so avoiding false alarms takes some tuning. This would
feed the alert logic.

### 4. Toast notifications

Critical events currently use a sticky tile and a `winsound` beep, with no extra
dependencies. A real Windows toast (clickable, shows even when the bar is hidden)
needs either an extra dependency like `windows-toasts` or a WinRT / tray-icon
path. Keep it optional so the default install stays dependency-light.

### 5. LibreHardwareMonitor provider

The no-admin sources cover a lot, but three things need kernel-level access: true
per-core CPU temperature, fan RPM, and AMD/Intel GPU temp, clock and power.
LibreHardwareMonitor exposes all of these. The plan is an optional provider that
reads a running LibreHardwareMonitor instance over its local web server
(`http://localhost:8085/data.json`, which only needs `urllib`), detected
automatically and hidden when it isn't running. LibreHardwareMonitor runs with
admin; GlintBar itself stays no-admin. The SYS Temp tile is the no-admin fallback
in the meantime.

### 6. Multiple monitors and taskbar position

Right now it assumes one primary monitor with a taskbar along the bottom. It
should handle top and side taskbars and let you choose the monitor.

### 7. Recent-events log

A short timestamped history of what alerted or surfaced, so brief spikes can be
reviewed after the fact. The CSV log covers part of this already.

### 8. Other platforms

Windows only for now, since it leans on Win32 and the taskbar. macOS or Linux
would need different placement and metric code, probably a separate front end
over the same collector. That's a big job.

## Notes on the project

Distribution is source-first, no `.exe`. Corporate environments block unsigned
binaries, and plain Python is easy to audit and needs no signing.
