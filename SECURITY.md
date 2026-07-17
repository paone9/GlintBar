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
- user32 (ctypes): it creates and positions its own window, reads taskbar geometry
  (`FindWindow`, `GetWindowRect`, `SetWindowPos`, `MoveWindow`), and reads idle time
  and lock state for the away watch (`GetLastInputInfo`, `OpenInputDesktop`). It
  does not touch or inject into other processes.
- psutil per-process CPU, but only while you're away, to name the process behind a
  busy machine. Names and CPU only, no memory contents.
- WebView2 (through pywebview): renders a local HTML UI that's fully
  self-contained (inline CSS and JS, no external resources, no remote content).
- Optional sensor helpers, if you enable one: HWiNFO via its shared-memory block
  (a local memory read, no network), or LibreHardwareMonitor via
  `http://127.0.0.1:8085` on your own machine (loopback only, never the internet).

## What it writes

Just `config.json` and `logs/*.csv`, both inside its own folder. Nothing else: no
registry keys, no system files, no auto-start entries. If you want it to launch on
login you add a shortcut yourself.

## What it never does

- No internet connections. No telemetry, analytics, update checks, or remote
  content. The only network use is optional and local: if you enable the
  LibreHardwareMonitor integration, it reads from `127.0.0.1` (your own machine).
  Leave that off and it makes no network calls at all.
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

- Read the source. It's small: about 1,300 lines in `monitor.py` plus a handful
  of self-contained HTML files.
- CI runs [`ruff`](https://github.com/astral-sh/ruff) and
  [`bandit`](https://github.com/PyCQA/bandit) on every push, and a
  [CodeQL](https://codeql.github.com/) workflow runs semantic analysis of the
  Python, the UI JavaScript, and the workflow files. The results are on the
  Actions and Security tabs.
- Dependencies are hash-locked: `requirements.txt` is compiled from
  `requirements.in` with `pip-compile --generate-hashes`, so `pip` refuses any
  package whose contents don't match the recorded hash.
- To confirm there's no network use, read the code (there are no HTTP or socket
  calls) or run it behind a network monitor.

## Reporting a problem

Open a private Security Advisory on the repo (Security, then "Report a
vulnerability"), or a normal issue for anything non-sensitive.
