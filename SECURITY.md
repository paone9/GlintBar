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
- user32 (ctypes), for its own windows: it creates, positions, shows and hides
  them (`FindWindow`/`FindWindowEx` limited to its own process, `MoveWindow`,
  `SetWindowPos`, `ShowWindow`, `SetWindowLong`), reads the mouse position for the
  hover popup (`GetCursorPos`), and reads idle time and lock state for the away
  watch (`GetLastInputInfo`, `OpenInputDesktop`).
- The taskbar's geometry: the taskbar windows' rectangles (`FindWindow`,
  `GetWindowRect`), and on Windows 11 the positions of the buttons it draws, read
  from the taskbar's accessibility tree through UI Automation (via `taskbargap`).
  Read only; it never clicks, moves or changes the taskbar.
- Other apps' top-level windows, to hide the bar under a fullscreen app: it
  enumerates them (`EnumWindows`) and reads each one's visibility, rectangle,
  monitor, class name, extended style, cloaked state (`DwmGetWindowAttribute`) and
  owning process ID and name. To estimate the taskbar's button count where it
  can't be measured, `taskbargap` also checks whether each window has a title
  (`GetWindowTextLength`), never what it says. No window titles or contents are
  read, and it never moves, resizes, or sends input to another app's window.
- Window events: two out-of-context WinEvent hooks (`SetWinEventHook`, foreground
  change and window shown) tell it when to re-check and repaint. They deliver
  notifications only; nothing is injected into other processes.
- The hide/show hotkey: it polls the state of only the configured key combination
  (`GetAsyncKeyState`, default Ctrl+Alt+G) every 50 ms. No other keys are read and
  no keystrokes are recorded.
- psutil per-process CPU, but only while you're away, to name the process behind a
  busy machine, and when RAM rose noticeably during an absence, the name and
  memory size (RSS) of the largest process. Names and numbers only, no memory
  contents.
- WebView2 (through pywebview): renders a local HTML UI that's fully
  self-contained (inline CSS and JS, no external resources, no remote content).
- Sensor helpers: HWiNFO via its shared-memory block (a local memory read, no
  network) if HWiNFO is running with shared memory on. If it isn't, GlintBar checks
  once at startup for LibreHardwareMonitor at `http://127.0.0.1:8085` (or the port
  in `GLINTBAR_LHM_PORT`), loopback only, never the internet, and keeps reading it
  only if it answers with CPU temperature or fan data.

## What it writes

`config.json`, `logs/*.csv` (including `away.csv`; an older `away.csv` whose
columns differ is renamed to `away-<timestamp>.csv` rather than appended to), and
`webview2\`, all in `%LOCALAPPDATA%\GlintBar` (a
per-user data folder), not the install directory. `webview2\` is WebView2's own
browser profile (cache, preferences); it's kept there deliberately so it's reused
across launches instead of leaking a fresh multi-megabyte copy into `%TEMP%` on
every single one, which is what happens without a persistent profile path. Nothing
else: no registry keys, no system files, no auto-start entries. If you want it to
launch on login you add a shortcut yourself.

## What it never does

- No internet connections. No telemetry, analytics, update checks, or remote
  content. The only network use is local: the LibreHardwareMonitor check on
  `127.0.0.1` (your own machine) described above, which happens once at startup
  whenever HWiNFO's shared memory isn't available. There is no switch to turn it
  off; running HWiNFO with shared memory on skips it.
- No administrator rights or elevation. It runs as a normal user.
- No drivers and no kernel access. That's also why true per-core CPU temps and fan
  RPM aren't available without a separate helper (see the README).
- No access to your documents or other apps' data. It reads other apps' window
  geometry and process names and sizes, as listed above, never their contents.

## Dependencies

Three, all open source:

- [`psutil`](https://github.com/giampaolo/psutil) for system metrics.
- [`pywebview`](https://github.com/r0x0r/pywebview) for the window and UI. It uses
  the WebView2 runtime, which ships with Windows 11 and is usually present on
  Windows 10 via Edge, and on Windows it pulls in `pythonnet`.
- [`taskbargap`](https://github.com/paone9/taskbargap) for finding the free gap in
  the taskbar, extracted from this project (same author).

GlintBar's own code is plain Python; `psutil` and `pythonnet` include compiled
code, as published on PyPI.

## How to check it yourself

- Read the source. It's small: about 2,500 lines in `glintbar/monitor.py` plus a
  handful of self-contained HTML files.
- CI runs [`ruff`](https://github.com/astral-sh/ruff) and
  [`bandit`](https://github.com/PyCQA/bandit), and the unit tests on Windows, on
  every push to `main` and every pull request, and a
  [CodeQL](https://codeql.github.com/) workflow runs semantic analysis of the
  Python, the UI JavaScript, and the workflow files. The results are on the
  Actions and Security tabs.
- GitHub Actions are pinned to commit SHAs. Dependabot bumps those and the pinned
  dev tools (ruff, bandit, pytest). In `pyproject.toml`, `pywebview` and `psutil`
  sit at minimum-tested floors and `taskbargap` is pinned to its 0.2 minor
  (`~=0.2.0`).
- To confirm there's no internet use, read the code: the only network call is the
  LibreHardwareMonitor read on `127.0.0.1` (`LhmProvider` in `monitor.py`). Or run
  it behind a network monitor.

## Reporting a problem

Open a private Security Advisory on the repo (Security, then "Report a
vulnerability"), or a normal issue for anything non-sensitive.
