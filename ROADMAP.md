# glintbar — roadmap / deferred ideas

Shipped small-and-solid first (see README). This file captures the richer ideas
we discussed but intentionally *didn't* build yet, with the reasoning, so future
work (or contributors) can pick them up.

## Shipped
- Taskbar-gap floating bar (topmost overlay, owns its input on Win11).
- Configurable metrics / size / align, hover-to-expand live graph, CSV logging.
- Cross-vendor GPU: NVIDIA (full) / AMD·Intel (utilization) / none.
- No-admin **SYS Temp** tile (ACPI thermal zone via perf counter).
- **Additive critical alert**: a tile pops in on a sustained critical breach
  (2s to enter, hysteresis + 8s cooldown to clear), never displacing your tiles,
  with an optional soft beep. Toggle in ⚙.
- **Placement watcher**: keeps the bar fitted to the taskbar gap and re-asserts
  topmost as things change (apps open/close, Explorer restart).

## Deferred (and why)

### 1. Attention rotation — "Option B" (pinned + auto slots)
A few slots that rotate to show whichever *warm* (elevated, sub-critical)
metric is most noteworthy, with anti-flap: debounced entry, long dwell,
incumbency bonus, in-place swap. **Deferred** because motion in the taskbar is a
cognitive tax and colour-coding already draws the eye; the additive critical
alert covers the high-value case with zero churn. Add only if static+alerts
proves insufficient in real use.

### 2. Full tier model — "A + B combined"
Escalation across three zones by severity: **pinned** (static anchors) →
**auto** (rotating warm metrics) → **alert** (additive critical). One model that
degrades to today's behaviour (auto-slots = 0) or full rotation (pinned = 0).
Build on top of #1 once rotation exists.

### 3. Anomaly detection (beyond static thresholds)
Rolling baseline (EWMA / z-score over the 60s window) to catch *sudden* changes,
not just absolute levels. Key target: **GPU clock collapsing to idle while util
is high** — the `VIDEO_TDR_FAILURE` signature — and power dropouts. Needs care:
disk/network are naturally spiky, so tuning to avoid false positives is the hard
part. Would feed the alert/score engine.

### 4. Real toast notifications
Currently critical events use a sticky tile + `winsound` beep (zero-dep). A true
Windows toast (actionable, appears even with the bar hidden) needs either an
optional dep (`windows-toasts`) or a WinRT/`Shell_NotifyIcon` path. Keep it
optional to preserve the no-dependency, corp-friendly footprint.

### 5. LibreHardwareMonitor provider — real CPU temps, fans, AMD/Intel GPU
The no-admin sources cover a lot, but three things need kernel-level access:
**true per-core CPU package temperature**, **fan RPM**, and **AMD/Intel GPU
temp/clock/power**. LibreHardwareMonitor (open source) exposes all of these.
Add an optional provider that reads a running LHM instance via its local web-server
JSON (`http://localhost:8085/data.json`, zero extra deps — `urllib`), auto-detected
and hidden when absent. LHM itself runs with admin; GlintBar stays no-admin. The
current **SYS Temp** tile (ACPI thermal zone) is the no-admin fallback for temp.

### 6. Multi-monitor & taskbar orientation
Currently assumes a single primary monitor with a bottom taskbar. Handle
top/left/right taskbars and let the user pick which monitor.

### 7. "Recently surfaced" log
A small timestamped history of what alerted/surfaced, so short-lived events can
be reviewed. Partly covered by the CSV log today.

### 8. Cross-platform
Windows-only today (Win32 + taskbar embedding). macOS/Linux would need different
placement strategies and metric providers — a large effort, likely a separate
front end over the same collector.

## Project
- Source-first distribution (no `.exe`): corporate environments block unsigned
  binaries; plain Python is auditable and needs no signing.
