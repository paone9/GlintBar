"""
GlintBar - a slim, always-on-top hardware monitor that lives in the taskbar gap.

Data sources (all first-party / open-source, no admin needed):
  - GPU (NVIDIA)      : nvidia-smi  -> temp, util, VRAM, power, clock
  - GPU (AMD/Intel)   : Windows PDH perf counters -> utilization
  - CPU/RAM/net/disk  : psutil
GPU support is auto-detected; unsupported tiles are hidden.

Features:
  - Live 60s sparklines + session min/max/avg per metric
  - Colour-coded thresholds, additive critical alerts with optional sound
  - Hover a metric for an expanded live graph
  - Per-second CSV logging for offline analysis

Run:  python monitor.py
"""
import csv
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import threading
import time
import winsound
from collections import deque
from datetime import datetime

import psutil
import webview

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

SAMPLE_INTERVAL = 1.0        # seconds
HISTORY_LEN = 60             # sparkline window (seconds)

METRIC_IDS = [
    # grouped for readability: system vitals -> GPU cluster -> I/O
    "cpu", "ram_pct", "sys_temp",
    "gpu_temp", "gpu_util", "gpu_mem_pct", "gpu_clock", "gpu_power",
    "disk_mbps", "net_mbps",
]
BASE_METRICS = ["cpu", "ram_pct", "net_mbps", "disk_mbps"]   # always available (psutil)
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---- GPU providers (vendor-detected) ---------------------------------------
# NVIDIA  -> full telemetry via nvidia-smi
# any GPU -> utilization via Windows PDH perf counters (AMD/Intel/NVIDIA), no admin
# none    -> GPU tiles are simply not shown

_NV_QUERY = ["temperature.gpu", "utilization.gpu", "memory.used",
             "memory.total", "power.draw", "clocks.gr"]


def _nvidia_ok():
    try:
        o = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=5, creationflags=_NOWIN)
        return o.returncode == 0 and o.stdout.strip() != ""
    except Exception:
        return False


class NvidiaProvider:
    kind = "nvidia"
    metrics = ["gpu_temp", "gpu_util", "gpu_mem_pct", "gpu_power", "gpu_clock"]

    def sample(self):
        try:
            o = subprocess.run(["nvidia-smi", "--query-gpu=" + ",".join(_NV_QUERY),
                                "--format=csv,noheader,nounits"],
                               capture_output=True, text=True, timeout=5, creationflags=_NOWIN)
            p = [x.strip() for x in o.stdout.strip().split(",")]
            temp, util, mu, mt, power, clock = (float(x) for x in p)
            return {"gpu_temp": temp, "gpu_util": util,
                    "gpu_mem_pct": (mu / mt * 100.0 if mt else 0.0),
                    "gpu_power": power, "gpu_clock": clock,
                    "gpu_mem_used": mu, "gpu_mem_total": mt}
        except Exception:
            return {}


class _PDH_VAL(ctypes.Structure):
    _fields_ = [("CStatus", wt.DWORD), ("doubleValue", ctypes.c_double)]


class _PDH_ITEM(ctypes.Structure):
    _fields_ = [("szName", wt.LPWSTR), ("FmtValue", _PDH_VAL)]


_PDH_MORE, _PDH_DBL = 0x800007D2, 0x00000200
_PDH = None


def _pdh_lib():
    global _PDH
    if _PDH is None:
        p = ctypes.windll.pdh
        for fn in ("PdhOpenQueryW", "PdhAddEnglishCounterW",
                   "PdhCollectQueryData", "PdhGetFormattedCounterArrayW"):
            getattr(p, fn).restype = ctypes.c_uint32   # PDH_STATUS is unsigned
        _PDH = p
    return _PDH


def _pdh_values(p, counter):
    """Return [(instanceName, value), ...] for a wildcard PDH counter, or None."""
    for _ in range(5):
        size, count = wt.DWORD(0), wt.DWORD(0)
        if p.PdhGetFormattedCounterArrayW(counter, _PDH_DBL, ctypes.byref(size),
                                          ctypes.byref(count), None) != _PDH_MORE:
            return None
        buf = (ctypes.c_byte * (size.value + 8192))()   # margin for volatile counts
        size = wt.DWORD(size.value + 8192)
        if p.PdhGetFormattedCounterArrayW(counter, _PDH_DBL, ctypes.byref(size),
                                          ctypes.byref(count), buf) == 0:
            items = ctypes.cast(buf, ctypes.POINTER(_PDH_ITEM))
            return [(items[i].szName or "", items[i].FmtValue.doubleValue)
                    for i in range(count.value)]
    return None


class _PdhCounter:
    """A single persistent PDH wildcard counter query (no admin needed)."""
    def __init__(self, path):
        self.ok = False
        try:
            self.p = _pdh_lib()
            self.q = wt.HANDLE()
            if self.p.PdhOpenQueryW(None, 0, ctypes.byref(self.q)) != 0:
                return
            self.c = wt.HANDLE()
            if self.p.PdhAddEnglishCounterW(self.q, path, 0, ctypes.byref(self.c)) != 0:
                return
            self.p.PdhCollectQueryData(self.q)   # baseline
            self.ok = True
        except Exception:
            self.ok = False

    def values(self):
        self.p.PdhCollectQueryData(self.q)
        return _pdh_values(self.p, self.c)


class PdhGpuProvider:
    """Cross-vendor GPU utilization via Windows performance counters."""
    kind = "generic"
    metrics = ["gpu_util"]

    def __init__(self):
        self._c = _PdhCounter(r"\GPU Engine(*)\Utilization Percentage")
        self.ok = self._c.ok

    def sample(self):
        if not self.ok:
            return {}
        try:
            vals = self._c.values()
            if vals is None:
                return {}
            util = sum(v for n, v in vals if "engtype_3D" in n)
            return {"gpu_util": min(util, 100.0)}
        except Exception:
            return {}


class ThermalProvider:
    """System temperature from the ACPI thermal zone (no admin). A generic zone,
    not the exact CPU package sensor. For that, use LibreHardwareMonitor."""
    metrics = ["sys_temp"]

    def __init__(self):
        self._c = _PdhCounter(r"\Thermal Zone Information(*)\Temperature")
        self.ok = self._c.ok
        if self.ok:                      # confirm it yields a plausible reading
            v = self.sample().get("sys_temp")
            self.ok = v is not None and 0 < v < 150

    def sample(self):
        try:
            vals = self._c.values()
            if not vals:
                return {}
            temps = [k - 273.15 for _, k in vals if 200 < k < 500]   # Kelvin -> C
            return {"sys_temp": round(max(temps), 1)} if temps else {}
        except Exception:
            return {}


def _detect_gpu():
    if _nvidia_ok():
        return NvidiaProvider()
    g = PdhGpuProvider()
    if g.ok:
        return g
    return None


GPU = _detect_gpu()
GPU_KIND = GPU.kind if GPU else "none"
GPU_METRICS = GPU.metrics if GPU else []
THERMAL = ThermalProvider()
THERMAL_METRICS = THERMAL.metrics if THERMAL.ok else []
AVAILABLE = [m for m in METRIC_IDS
             if m in BASE_METRICS or m in GPU_METRICS or m in THERMAL_METRICS]

CONFIG_PATH = os.path.join(HERE, "config.json")
DEFAULT_CONFIG = {
    "metrics": list(AVAILABLE),    # which tiles to show, in order ("top picks")
    "size": "M",                   # S / M / L
    "align": "right",              # left / center / right within the taskbar gap
    "sparklines": True,
    "alerts": True,                # additive tile appears on a critical breach
    "sound": True,                 # soft beep on a new critical breach
}


def _valid_metrics(metrics):
    m = [x for x in (metrics or []) if x in AVAILABLE]
    return m or list(AVAILABLE)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    cfg["metrics"] = _valid_metrics(cfg.get("metrics"))
    return cfg


CONFIG = load_config()
CONFIG_VERSION = 0


def store_config(cfg):
    global CONFIG, CONFIG_VERSION
    merged = {**DEFAULT_CONFIG, **(cfg or {})}
    merged["metrics"] = _valid_metrics(merged.get("metrics"))
    CONFIG = merged
    CONFIG_VERSION += 1
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(CONFIG, f, indent=2)
    except Exception:
        pass


class Collector:
    def __init__(self):
        self.lock = threading.Lock()
        self.hist = {mid: deque(maxlen=HISTORY_LEN) for mid in METRIC_IDS}
        self.stats = {mid: {"min": None, "max": None, "sum": 0.0, "n": 0}
                      for mid in METRIC_IDS}
        self.latest = {}
        self.extra = {}          # non-graphed display values (mem MB, ram GB)
        # rate baselines
        self._net = psutil.net_io_counters()
        self._disk = psutil.disk_io_counters()
        self._t = time.monotonic()
        psutil.cpu_percent(interval=None)   # prime
        # logging
        self.logging = False
        self.logfile = None
        self._csv = None
        self._csv_fh = None

    # ---- logging control -------------------------------------------------
    def start_log(self):
        with self.lock:
            if self.logging:
                return self.logfile
            name = "glintbar_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv"
            self.logfile = os.path.join(LOG_DIR, name)
            self._csv_fh = open(self.logfile, "w", newline="")
            self._csv = csv.writer(self._csv_fh)
            self._csv.writerow(["timestamp"] + METRIC_IDS +
                               ["gpu_mem_used_mb", "gpu_mem_total_mb", "ram_used_gb"])
            self.logging = True
            return self.logfile

    def stop_log(self):
        with self.lock:
            if self._csv_fh:
                self._csv_fh.close()
            self._csv = self._csv_fh = None
            self.logging = False

    # ---- sampling --------------------------------------------------------
    def sample(self):
        now = time.monotonic()
        dt = max(now - self._t, 1e-3)
        self._t = now

        gpu = GPU.sample() if GPU else {}
        therm = THERMAL.sample() if THERMAL.ok else {}

        cpu = psutil.cpu_percent(interval=None)
        vm = psutil.virtual_memory()

        net = psutil.net_io_counters()
        net_mbps = ((net.bytes_sent + net.bytes_recv)
                    - (self._net.bytes_sent + self._net.bytes_recv)) / dt / 1e6
        self._net = net

        disk = psutil.disk_io_counters()
        disk_mbps = ((disk.read_bytes + disk.write_bytes)
                     - (self._disk.read_bytes + self._disk.write_bytes)) / dt / 1e6
        self._disk = disk

        vals = {
            "gpu_temp": gpu.get("gpu_temp"),
            "gpu_util": gpu.get("gpu_util"),
            "gpu_mem_pct": gpu.get("gpu_mem_pct"),
            "gpu_power": gpu.get("gpu_power"),
            "gpu_clock": gpu.get("gpu_clock"),
            "sys_temp": therm.get("sys_temp"),
            "cpu": cpu,
            "ram_pct": vm.percent,
            "net_mbps": max(net_mbps, 0.0),
            "disk_mbps": max(disk_mbps, 0.0),
        }

        with self.lock:
            for mid, v in vals.items():
                self.hist[mid].append(v)
                if v is not None:
                    s = self.stats[mid]
                    s["min"] = v if s["min"] is None else min(s["min"], v)
                    s["max"] = v if s["max"] is None else max(s["max"], v)
                    s["sum"] += v
                    s["n"] += 1
            self.latest = vals
            self.extra = {
                "gpu_mem_used": gpu.get("gpu_mem_used"),
                "gpu_mem_total": gpu.get("gpu_mem_total"),
                "ram_used_gb": vm.used / 1e9,
                "ram_total_gb": vm.total / 1e9,
            }
            if self.logging and self._csv:
                def _r(x):
                    return round(x, 3) if isinstance(x, float) else x
                row = [datetime.now().isoformat(timespec="seconds")]
                row += [_r(vals[m]) for m in METRIC_IDS]
                row += [gpu.get("gpu_mem_used"), gpu.get("gpu_mem_total"),
                        _r(vm.used / 1e9)]
                self._csv.writerow(row)
                self._csv_fh.flush()

    def snapshot(self):
        with self.lock:
            out = {"latest": dict(self.latest), "extra": dict(self.extra),
                   "hist": {m: list(self.hist[m]) for m in METRIC_IDS},
                   "stats": {}, "logging": self.logging,
                   "logfile": os.path.basename(self.logfile) if self.logfile else None}
            for mid, s in self.stats.items():
                avg = (s["sum"] / s["n"]) if s["n"] else None
                out["stats"][mid] = {"min": s["min"], "max": s["max"], "avg": avg}
            return out


collector = Collector()


def sampler_loop():
    while True:
        try:
            collector.sample()
        except Exception:
            pass
        time.sleep(SAMPLE_INTERVAL)


class Api:
    def get_state(self):
        s = collector.snapshot()
        s["config"] = CONFIG
        s["cfgv"] = CONFIG_VERSION
        s["embedded"] = bool(EMBED_STATE.get("hwnd"))
        s["available"] = AVAILABLE
        s["gpu_kind"] = GPU_KIND
        return s

    def toggle_log(self):
        if collector.logging:
            collector.stop_log()
            return {"logging": False}
        f = collector.start_log()
        return {"logging": True, "logfile": os.path.basename(f)}

    def open_logs(self):
        os.startfile(LOG_DIR)

    def request_size(self, css_width):
        """Fit the embedded bar to its content width (CSS px). Returns True once placed."""
        return _place(css_width)

    def open_settings(self):
        _open_settings()

    def show_detail(self, metric_id, chip_center_css):
        return _show_detail(metric_id, chip_center_css)

    def hide_detail(self):
        return _hide_detail()

    def beep(self):
        if CONFIG.get("sound", True):
            try:
                winsound.MessageBeep(0x30)   # MB_ICONWARNING
            except Exception:
                pass
        return True

    def get_config(self):
        return {"config": CONFIG, "metric_ids": METRIC_IDS,
                "available": AVAILABLE, "gpu_kind": GPU_KIND}

    def save_config(self, cfg):
        store_config(cfg)
        for w in list(webview.windows):
            if w.title == "glintbar settings":
                try:
                    w.destroy()
                except Exception:
                    pass
        return True

    def close(self):
        for w in list(webview.windows):
            try:
                w.destroy()
            except Exception:
                pass


class SettingsApi:
    def get_config(self):
        return {"config": CONFIG, "metric_ids": METRIC_IDS,
                "available": AVAILABLE, "gpu_kind": GPU_KIND}

    def _close(self):
        for w in list(webview.windows):
            if w.title == "glintbar settings":
                try:
                    w.destroy()
                except Exception:
                    pass

    def save_config(self, cfg):
        store_config(cfg)
        self._close()
        return True

    def cancel(self):
        self._close()
        return True


with open(os.path.join(HERE, "ui.html"), encoding="utf-8") as _f:
    HTML = _f.read()
with open(os.path.join(HERE, "settings.html"), encoding="utf-8") as _f:
    SETTINGS_HTML = _f.read()
with open(os.path.join(HERE, "detail.html"), encoding="utf-8") as _f:
    DETAIL_HTML = _f.read()


DOCK = "taskbar"      # "taskbar" (in the empty taskbar area), "bottom", or "top"
BAR_HEIGHT = 52       # physical px, used for "bottom"/"top" docks
EMBED = True          # float over the taskbar gap as a topmost window


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


def _win32_setup(user32):
    user32.FindWindowW.restype = wt.HWND
    user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
    user32.FindWindowExW.restype = wt.HWND
    user32.FindWindowExW.argtypes = [wt.HWND, wt.HWND, wt.LPCWSTR, wt.LPCWSTR]
    user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    user32.SetParent.restype = wt.HWND
    user32.SetParent.argtypes = [wt.HWND, wt.HWND]
    user32.MoveWindow.argtypes = [wt.HWND, wt.INT, wt.INT, wt.INT, wt.INT, wt.BOOL]
    user32.ShowWindow.argtypes = [wt.HWND, wt.INT]
    user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, wt.INT, wt.INT, wt.INT, wt.INT, wt.UINT]
    user32.GetForegroundWindow.restype = wt.HWND
    user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, wt.INT]
    user32.MonitorFromWindow.restype = wt.HMONITOR
    user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
    user32.GetMonitorInfoW.argtypes = [wt.HMONITOR, ctypes.POINTER(MONITORINFO)]


def _rect(user32, hwnd):
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def _work_area(user32):
    r = wt.RECT()
    user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)  # SPI_GETWORKAREA
    return r.left, r.top, r.right, r.bottom


def _taskbar_region(user32):
    """Return (taskbar_rect, empty_gap) between app buttons and the tray, physical px."""
    tray = user32.FindWindowW("Shell_TrayWnd", None)
    if not tray:
        return None
    tbl, tbt, tbr, tbb = _rect(user32, tray)
    # right bound = left edge of the notification/clock cluster
    notify = user32.FindWindowExW(tray, None, "TrayNotifyWnd", None)
    right = _rect(user32, notify)[0] if notify else tbr
    # left bound = right edge of the app-button strip (Win10 rebar; may be absent on Win11)
    rebar = user32.FindWindowExW(tray, None, "ReBarWindow32", None)
    left = _rect(user32, rebar)[2] if rebar else tbl + int((tbr - tbl) * 0.30)
    return (tbl, tbt, tbr, tbb, tray), (left, right)


def _foreground_is_fullscreen(user32):
    """True when a real app covers the whole monitor the bar sits on
    (fullscreen video, games, presentations), so the bar should get out of the way."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    our = EMBED_STATE.get("hwnd")
    if our and hwnd == our:
        return False
    buf = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, buf, 64)
    if buf.value in ("Progman", "WorkerW", "Shell_TrayWnd"):
        return False   # the desktop / shell, not a fullscreen app
    MONITOR_DEFAULTTONEAREST = 2
    fg_mon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    if our and fg_mon != user32.MonitorFromWindow(our, MONITOR_DEFAULTTONEAREST):
        return False   # fullscreen app is on another screen; leave the bar alone
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(fg_mon, ctypes.byref(mi)):
        return False
    m, r = mi.rcMonitor, wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left <= m.left and r.top <= m.top
            and r.right >= m.right and r.bottom >= m.bottom)


EMBED_STATE = {}
HWND_TOPMOST = -1
SWP_NOMOVE_SIZE_ACT = 0x0001 | 0x0002 | 0x0010   # NOSIZE|NOMOVE|NOACTIVATE


def _place(css_width):
    """Resize/anchor the floating bar to fit `css_width` within the taskbar gap."""
    st = EMBED_STATE
    if not st.get("hwnd"):
        return False
    u, scale = st["user32"], st["scale"]
    gap_l, gap_r, top, h = st["gap_l"], st["gap_r"], st["top"], st["height"]
    avail = gap_r - gap_l - 24
    w = min(int(round(css_width * scale)) + 6, avail) if css_width else avail
    w = max(w, 160)
    align = CONFIG.get("align", "right")
    if align == "left":
        x1 = gap_l + 12
    elif align == "center":
        x1 = gap_l + ((gap_r - gap_l) - w) // 2
    else:
        x1 = gap_r - 12 - w
    u.MoveWindow(st["hwnd"], x1, top, w, h, True)          # absolute screen px
    u.SetWindowPos(st["hwnd"], HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE_SIZE_ACT)
    u.ShowWindow(st["hwnd"], 5)                            # SW_SHOW
    st["x1"], st["w"] = x1, w
    if css_width:
        st["last_css"] = css_width
    return True


def _overlay(user32, gap_l, gap_r, top, height, scale):
    """Float our window over the empty taskbar gap as a topmost, input-owning bar."""
    hwnd = 0
    for _ in range(60):
        hwnd = user32.FindWindowW(None, "glintbar")
        if hwnd:
            break
        time.sleep(0.1)
    if not hwnd:
        return
    # tool window: no taskbar button / alt-tab entry, always topmost
    GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_TOPMOST = -20, 0x00000080, 0x00000008
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_TOOLWINDOW | WS_EX_TOPMOST)
    EMBED_STATE.update(hwnd=hwnd, user32=user32, gap_l=gap_l, gap_r=gap_r,
                       top=top, height=height, scale=scale)
    _place(None)   # start spanning the whole gap; the UI shrinks it to fit


def _watcher():
    """Keep the bar correctly placed and on top as the taskbar changes over time."""
    while True:
        time.sleep(1)
        st = EMBED_STATE
        if not st.get("hwnd"):
            continue
        try:
            u = st["user32"]
            # hide while a fullscreen app (video, game, slideshow) owns the screen
            fs = _foreground_is_fullscreen(u)
            if fs:
                if not st.get("hidden_fs"):
                    u.ShowWindow(st["hwnd"], 0)   # SW_HIDE
                    _hide_detail()
                    st["hidden_fs"] = True
                continue
            if st.get("hidden_fs"):
                st["hidden_fs"] = False
                u.ShowWindow(st["hwnd"], 5)       # SW_SHOW
                _place(st.get("last_css"))        # re-fit and re-assert topmost
            # normal upkeep: re-assert topmost and re-fit if the taskbar gap moved
            u.SetWindowPos(st["hwnd"], HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE_SIZE_ACT)
            region = _taskbar_region(u)
            if not region:
                continue
            (tbl, tbt, tbr, tbb, tray), (gl, gr) = region
            if abs(gl - st["gap_l"]) > 8 or abs(gr - st["gap_r"]) > 8 or tbt != st["top"]:
                st["gap_l"], st["gap_r"], st["top"], st["height"] = gl, gr, tbt, tbb - tbt
                _place(st.get("last_css"))
        except Exception:
            pass


DETAIL = {"metric": None}


class DetailApi:
    def get(self):
        mid = DETAIL.get("metric")
        if not mid:
            return {"metric": None}
        snap = collector.snapshot()
        return {"metric": mid,
                "latest": snap["latest"].get(mid),
                "hist": snap["hist"].get(mid, []),
                "stats": snap["stats"].get(mid),
                "extra": snap["extra"]}


def _detail_hwnd(user32):
    return user32.FindWindowW(None, "glintbar detail")


def _show_detail(metric_id, chip_center_css):
    st = EMBED_STATE
    if not st.get("hwnd"):
        return False
    DETAIL["metric"] = metric_id
    u, scale = st["user32"], st["scale"]
    hwnd = _detail_hwnd(u)
    if not hwnd:
        return False
    W, H = int(320 * scale), int(180 * scale)
    cx = st.get("x1", st["gap_l"]) + int(chip_center_css * scale)
    x = max(6, min(cx - W // 2, st["gap_r"] - W))
    y = st["top"] - H - 8            # float just above the taskbar
    # tool window so it never grabs a taskbar button
    GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = -20, 0x80, 0x08000000
    ex = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
    u.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
    u.MoveWindow(hwnd, x, y, W, H, True)
    u.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE_SIZE_ACT)
    u.ShowWindow(hwnd, 8)            # SW_SHOWNA, show without stealing focus
    return True


def _hide_detail():
    st = EMBED_STATE
    if not st.get("hwnd"):
        return False
    u = st["user32"]
    hwnd = _detail_hwnd(u)
    if hwnd:
        u.ShowWindow(hwnd, 0)        # SW_HIDE
    DETAIL["metric"] = None
    return True


def _open_settings():
    if any(w.title == "glintbar settings" for w in webview.windows):
        return
    webview.create_window(
        "glintbar settings", html=SETTINGS_HTML, js_api=SettingsApi(),
        width=360, height=460, resizable=False, on_top=True,
        background_color="#12161c",
    )


def main():
    user32 = ctypes.windll.user32
    user32.SetProcessDPIAware()
    _win32_setup(user32)
    try:
        scale = user32.GetDpiForSystem() / 96.0
    except Exception:
        scale = 1.0

    embed_args = None
    if DOCK == "taskbar":
        region = _taskbar_region(user32)
        if region:
            (tbl, tbt, tbr, tbb, tray), (gap_l, gap_r) = region
            height = tbb - tbt                       # fill the taskbar row
            x_phys = gap_l + 12
            width = max(600, (gap_r - 20) - x_phys)  # span the empty gap
            y_phys = tbt
            embed_args = (gap_l, gap_r, tbt, height, scale)
    if DOCK != "taskbar" or embed_args is None:
        left, top, right, bottom = _work_area(user32)
        width, height = right - left, BAR_HEIGHT
        x_phys, y_phys = (left, bottom - height) if DOCK == "bottom" else (left, top)

    # pywebview/WinForms scales window POSITION by the DPI factor, so pass DIPs
    x, y = round(x_phys / scale), round(y_phys / scale)
    api = Api()
    webview.create_window(
        "glintbar", html=HTML, js_api=api,
        width=width, height=height, x=x, y=y,
        min_size=(100, 1),      # allow a very thin bar (default min is 100 tall)
        frameless=True, on_top=True, resizable=True, easy_drag=True,
        background_color="#0b0f14",
    )
    # hover-to-expand popup, created hidden; shown above the bar on chip hover
    webview.create_window(
        "glintbar detail", html=DETAIL_HTML, js_api=DetailApi(),
        width=320, height=180, min_size=(80, 1), hidden=True,
        frameless=True, on_top=True, resizable=False,
        background_color="#12161c",
    )
    if EMBED and embed_args is not None:
        threading.Thread(target=_overlay, args=(user32, *embed_args), daemon=True).start()
        threading.Thread(target=_watcher, daemon=True).start()
    t = threading.Thread(target=sampler_loop, daemon=True)
    t.start()
    webview.start()


if __name__ == "__main__":
    main()
