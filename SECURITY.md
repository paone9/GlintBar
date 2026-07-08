# Security

GlintBar is a local, read-only hardware monitor. This page spells out what it
does and doesn't do, so you or your security team can review it quickly. The
whole thing is plain Python that you can read start to finish. No binaries, no
obfuscation.

## What it reads

- Windows performance counters (PDH): GPU utilization and the ACPI thermal zone.
- `nvidia-smi`: read-only GPU queries (`--query-gpu=...`) when an NVIDIA driver is
  present. It never changes GPU state.
- psutil: CPU, RAM, disk, and network counters.
- user32 (ctypes): it creates and positions its own window and reads taskbar
  geometry (`FindWindow`, `GetWindowRect`, `SetWindowPos`, `MoveWindow`). It does
  not touch or inject into other processes.
- WebView2 (through pywebview): renders a local HTML UI that's fully
  self-contained (inline CSS and JS, no external resources, no remote content).

## What it writes

Just `config.json` and `logs/*.csv`, both inside its own folder. Nothing else: no
registry keys, no system files, no auto-start entries. If you want it to launch on
login you add a shortcut yourself.

## What it never does

- No network connections. No telemetry, analytics, update checks, or remote
  content. It works fully offline.
- No administrator rights or elevation. It runs as a normal user.
- No drivers and no kernel access. That's also why true per-core CPU temps and fan
  RPM aren't available without a separate helper (see the README).
- No access to your documents or other apps' data.

## Dependencies

Two, both widely used and open source:

- [`psutil`](https://github.com/giampaolo/psutil) for system metrics.
- [`pywebview`](https://github.com/r0x0r/pywebview) for the window and UI (it uses
  the WebView2 runtime that ships with Windows).

## How to check it yourself

- Read the source. It's small: about 600 lines in `monitor.py` plus three HTML
  files.
- CI runs [`ruff`](https://github.com/astral-sh/ruff) and
  [`bandit`](https://github.com/PyCQA/bandit) on every push, and GitHub's default
  [CodeQL](https://codeql.github.com/) scanning runs semantic analysis. The
  results are on the Actions and Security tabs.
- To confirm there's no network use, read the code (there are no HTTP or socket
  calls) or run it behind a network monitor.

## Reporting a problem

Open a private Security Advisory on the repo (Security, then "Report a
vulnerability"), or a normal issue for anything non-sensitive.
