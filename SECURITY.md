# Security

GlintBar is a **local, read-only** hardware monitor. This document states exactly
what it does and does not do, so you (or your security team) can evaluate it
quickly. The whole program is plain, auditable Python — no binaries, no obfuscation.

## What it accesses (all read-only)

- **Windows performance counters** (PDH) — GPU utilization and the ACPI thermal
  zone. Read-only.
- **`nvidia-smi`** — read-only GPU queries (`--query-gpu=...`) when an NVIDIA
  driver is present. It never changes GPU state.
- **psutil** — CPU, RAM, disk, and network counters. Read-only.
- **user32 (ctypes)** — creates and positions *its own* window and reads taskbar
  geometry (`FindWindow`/`GetWindowRect`/`SetWindowPos`/`MoveWindow`). It does not
  inject into or modify other processes.
- **WebView2 (via pywebview)** — renders a bundled, fully self-contained local
  HTML UI (inline CSS/JS, **no external resources, no remote content**).

## What it writes

- `config.json` and `logs/*.csv` **inside its own folder**. Nothing else — no
  registry, no system files, no auto-start entries (you create a shortcut yourself
  if you want it to launch on login).

## What it never does

- **No network connections.** No telemetry, analytics, update checks, or remote
  content. It runs fully offline.
- **No administrator / elevation.** Runs as a normal user.
- **No drivers, no kernel access.** (That's why true per-core CPU temps and fan
  RPM aren't available without a separate helper — see the README.)
- **No access to your documents or other applications' data.**

## Dependencies

Two, both widely used and open source:

- [`psutil`](https://github.com/giampaolo/psutil) — system metrics.
- [`pywebview`](https://github.com/r0x0r/pywebview) — the window/UI host
  (uses the OS-provided WebView2 runtime).

## How to verify

- **Read the source** — it's small (~600 lines in `monitor.py`, plus three HTML
  files).
- **Automated scanning:** CI runs [`ruff`](https://github.com/astral-sh/ruff)
  (lint) and [`bandit`](https://github.com/PyCQA/bandit) (Python security scanner)
  on every push, and GitHub's [CodeQL](https://codeql.github.com/) default code
  scanning performs semantic analysis. See the **Actions** and **Security** tabs.
- **Confirm no network use** — read the code (there are no HTTP/socket calls), or
  run it behind a network monitor.

## Reporting a vulnerability

Please open a private **Security Advisory** on this repository (Security →
Report a vulnerability), or a regular issue for non-sensitive reports.
