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

Run:  python -m glintbar   (or the installed `glintbar` command)
"""
import csv
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import winsound
from collections import deque
from datetime import datetime

import psutil
import webview

_T0 = time.monotonic()   # module init start, reported by --diag

HERE = os.path.dirname(os.path.abspath(__file__))   # package dir; the bundled HTML lives here
# Config and logs live in a per-user data folder, not next to the code, so this
# works the same whether GlintBar is run from source or pip-installed (an
# installed package must never write into site-packages).
DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "GlintBar")
os.makedirs(DATA_DIR, exist_ok=True)
LOG_DIR = os.path.join(DATA_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

SAMPLE_INTERVAL = 1.0        # seconds
HISTORY_LEN = 60             # sparkline window (seconds)

# Window titles: brand-cased for display, and also the keys used to find each
# window (FindWindow), so always reference these constants, never a bare string.
TITLE_BAR = "GlintBar"
TITLE_SETTINGS = "GlintBar settings"
TITLE_DETAIL = "GlintBar detail"
TITLE_AWAY = "GlintBar away"

METRIC_IDS = [
    # grouped for readability: system vitals -> GPU cluster -> I/O
    "cpu", "cpu_temp", "ram_pct", "sys_temp", "fan_rpm",
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


def _nv_num(x):
    """nvidia-smi prints [N/A] for fields a GPU doesn't expose (power.draw and
    clocks.gr are commonly [N/A] on laptop/older GPUs). Treat those as missing
    rather than letting one bad field blank the whole GPU cluster."""
    try:
        return float(x.strip())
    except (ValueError, AttributeError):
        return None


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
    _INTERVAL = 2.0         # only spawn nvidia-smi this often; cache in between
    _FAIL_INTERVAL = 30.0   # ...and back well off once it clearly isn't answering
    _MAX_FAILS = 3          # ~6s of nothing before the tiles go blank rather than lie

    def __init__(self):
        self._cache = {}
        self._t = 0.0
        self._fails = 0

    def sample(self):
        now = time.monotonic()
        # Throttle on elapsed time alone. Requiring a non-empty cache here meant a
        # failing GPU — whose cache is deliberately emptied so the tiles don't show
        # stale numbers as live — spawned nvidia-smi on every 1s sampler tick, and a
        # wedged driver that hangs rather than fails would then stall that thread on
        # the 5s timeout, taking CPU/RAM/disk/net down with it.
        interval = self._INTERVAL if self._fails < self._MAX_FAILS else self._FAIL_INTERVAL
        if now - self._t < interval:
            return self._cache      # reuse recent reading, no subprocess
        self._t = now
        try:
            o = subprocess.run(["nvidia-smi", "--query-gpu=" + ",".join(_NV_QUERY),
                                "--format=csv,noheader,nounits"],
                               capture_output=True, text=True, timeout=5, creationflags=_NOWIN)
            line = o.stdout.strip().splitlines()[0]        # first GPU only
            p = [_nv_num(x) for x in line.split(",")]
            temp, util, mu, mt, power, clock = (p + [None] * 6)[:6]
            d = {}
            if temp is not None:
                d["gpu_temp"] = temp
            if util is not None:
                d["gpu_util"] = util
            if mu is not None and mt:
                d["gpu_mem_pct"] = mu / mt * 100.0
                d["gpu_mem_used"], d["gpu_mem_total"] = mu, mt
            if power is not None:
                d["gpu_power"] = power
            if clock is not None:
                d["gpu_clock"] = clock
            if d:
                self._cache, self._fails = d, 0
                return self._cache
            raise ValueError("no usable fields")
        except Exception:
            # A driver reset or a disabled GPU must not leave the last good reading
            # on screen looking current — blank tiles are honest, stale ones aren't.
            self._fails += 1
            if self._fails >= self._MAX_FAILS:
                self._cache = {}
        return self._cache


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


LHM_PORT = int(os.environ.get("GLINTBAR_LHM_PORT", "8085"))


def _num(s):
    if not isinstance(s, str):
        try:
            return float(s)
        except (TypeError, ValueError):
            return None
    m = re.search(r"-?\d+(?:[.,]\d+)?", s)
    if not m:
        return None
    t = m.group()
    if "," in t:
        # A comma means different things in different locales. Exactly three digits
        # after it is thousands grouping ("1,234 RPM" is 1234, not 1.234); one or two
        # is a decimal comma. Three decimal places on a temperature or a fan speed
        # doesn't happen, four-figure RPM does.
        t = t.replace(",", "") if len(t.split(",")[1]) == 3 else t.replace(",", ".")
    return float(t)


class LhmProvider:
    """Real CPU temperature and fan RPM from a running LibreHardwareMonitor.

    LibreHardwareMonitor (open source) reads the CPU's on-die sensors and the
    fan controller through a signed kernel driver, which needs admin. GlintBar
    stays no-admin and just reads LHM's local web server (Options -> Remote Web
    Server, default port 8085). Tiles appear only when LHM is running."""
    metrics = []

    def __init__(self):
        self.url = "http://127.0.0.1:" + str(LHM_PORT) + "/data.json"
        self.ok = False
        vals = self.sample()
        self.metrics = [k for k in ("cpu_temp", "fan_rpm") if vals.get(k) is not None]
        self.ok = bool(self.metrics)

    def _fetch(self):
        try:
            # self.url is a fixed http://127.0.0.1 loopback URL, no user input
            with urllib.request.urlopen(self.url, timeout=1.0) as r:  # nosec B310
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            return None

    def sample(self):
        data = self._fetch()
        if not data:
            return {}
        temps, fans = [], []

        def walk(node):
            t = node.get("Type")
            if t == "Temperature":
                v = _num(node.get("Value"))
                if v is not None:
                    temps.append((node.get("Text", "").lower(), v))
            elif t == "Fan":
                v = _num(node.get("Value"))
                if v is not None:
                    fans.append(v)
            for c in node.get("Children", []):
                walk(c)

        walk(data)
        out = {}
        cpu_t = None
        for keys in (("cpu package", "package"), ("tctl", "tdie")):
            cand = [v for name, v in temps if any(k in name for k in keys)]
            if cand:
                cpu_t = max(cand)
                break
        if cpu_t is None:
            cand = [v for name, v in temps if "core" in name and "gpu" not in name]
            cpu_t = max(cand) if cand else None
        if cpu_t is not None:
            out["cpu_temp"] = round(cpu_t, 1)
        fan = max([f for f in fans if f > 0], default=None)
        if fan is not None:
            out["fan_rpm"] = round(fan)
        return out


class _HWI_SM2(ctypes.Structure):
    _fields_ = [("sig", ctypes.c_uint32), ("ver", ctypes.c_uint32),
                ("rev", ctypes.c_uint32), ("poll", ctypes.c_int64),
                ("sOff", ctypes.c_uint32), ("sSize", ctypes.c_uint32),
                ("sCount", ctypes.c_uint32), ("rOff", ctypes.c_uint32),
                ("rSize", ctypes.c_uint32), ("rCount", ctypes.c_uint32)]


class _HWI_READ(ctypes.Structure):
    _fields_ = [("t", ctypes.c_uint32), ("si", ctypes.c_uint32), ("rid", ctypes.c_uint32),
                ("lo", ctypes.c_char * 128), ("lu", ctypes.c_char * 128),
                ("unit", ctypes.c_char * 16), ("val", ctypes.c_double),
                ("vmin", ctypes.c_double), ("vmax", ctypes.c_double), ("vavg", ctypes.c_double)]


class HwinfoProvider:
    """Real CPU temperature and fan RPM from a running HWiNFO.

    HWiNFO reads the hardware sensors (with admin for its driver) and publishes
    them in a shared-memory block. GlintBar stays no-admin and just reads it.
    Enable it in HWiNFO: Settings -> 'Shared Memory Support'. Tiles appear only
    when HWiNFO is running with that on."""
    metrics = []

    def __init__(self):
        self.ok = False
        self._p = None
        try:
            k = ctypes.windll.kernel32
            k.OpenFileMappingW.restype = wt.HANDLE
            k.OpenFileMappingW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
            k.MapViewOfFile.restype = ctypes.c_void_p
            k.MapViewOfFile.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_size_t]
            h = k.OpenFileMappingW(0x0004, False, "Global\\HWiNFO_SENS_SM2")  # FILE_MAP_READ
            if h:
                self._p = k.MapViewOfFile(h, 0x0004, 0, 0, 0)
        except Exception:
            self._p = None
        if self._p:
            vals = self.sample()
            self.metrics = [m for m in ("cpu_temp", "fan_rpm") if vals.get(m) is not None]
            self.ok = bool(self.metrics)

    def sample(self):
        if not self._p:
            return {}
        try:
            hdr = _HWI_SM2.from_address(self._p)
            if not (0 < hdr.rCount < 100000 and 0 < hdr.rSize < 4096):
                return {}
            cpu_temps, fans = [], []
            for i in range(hdr.rCount):
                r = _HWI_READ.from_address(self._p + hdr.rOff + i * hdr.rSize)
                if r.t == 1:                                   # temperature
                    label = r.lu.decode("latin-1", "replace").lower()
                    if 0 < r.val < 130 and "cpu" in label:
                        cpu_temps.append((label, r.val))
                elif r.t == 3:                                 # fan
                    if 0 < r.val < 30000:
                        fans.append(r.val)
            out = {}
            pkg = [v for lbl, v in cpu_temps
                   if any(k in lbl for k in ("package", "tctl", "tdie"))]
            cpu_t = max(pkg) if pkg else (max(v for _, v in cpu_temps) if cpu_temps else None)
            if cpu_t is not None:
                out["cpu_temp"] = round(cpu_t, 1)
            if fans:
                out["fan_rpm"] = round(max(fans))
            return out
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
# real CPU temp + fan RPM: prefer HWiNFO (shared memory), else LibreHardwareMonitor
SENSOR = HwinfoProvider()
if not SENSOR.ok:
    SENSOR = LhmProvider()
SENSOR_METRICS = SENSOR.metrics if SENSOR.ok else []
AVAILABLE = [m for m in METRIC_IDS
             if m in BASE_METRICS or m in GPU_METRICS
             or m in THERMAL_METRICS or m in SENSOR_METRICS]

CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
DEFAULT_CONFIG = {
    "metrics": list(AVAILABLE),    # which tiles to show, in order ("top picks")
    "size": "M",                   # S / M / L
    "align": "right",              # left / center / right within the taskbar gap
    "sparklines": True,
    "alerts": True,                # additive tile appears on a critical breach
    "sound": True,                 # soft beep on a new critical breach
    "away_watch": True,            # watch for busy processes while you're away
    "away_after_min": 5,           # idle minutes before "away" starts
    "away_cpu_pct": 25,            # only report if CPU stayed above this while away
    "away_hot_c": 85,              # ...or if it ran at least this hot for a while
    "temp_unit": "C",              # "C" or "F"; display only, edit in config.json (no UI toggle)
    "hotkey": "ctrl+alt+g",        # global hide/show hotkey; "" disables it
}


def _valid_metrics(metrics):
    m = [x for x in (metrics or []) if x in AVAILABLE]
    return m or list(AVAILABLE)


def _coerce_config(cfg):
    """Numeric keys are hand-editable in config.json; a stringly "5" would raise
    deep inside the away loop (whose bare except would then silently stop it).
    Coerce them back to the default's type, falling back to the default."""
    for k, v in DEFAULT_CONFIG.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        try:
            cfg[k] = type(v)(cfg.get(k, v))
        except (TypeError, ValueError):
            cfg[k] = v
    return cfg


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    cfg["metrics"] = _valid_metrics(cfg.get("metrics"))
    return _coerce_config(cfg)


CONFIG = load_config()
CONFIG_VERSION = 0


def store_config(cfg):
    global CONFIG, CONFIG_VERSION
    merged = {**DEFAULT_CONFIG, **(cfg or {})}
    merged["metrics"] = _valid_metrics(merged.get("metrics"))
    CONFIG = _coerce_config(merged)
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
            self._csv_fh = open(self.logfile, "w", newline="", encoding="utf-8")
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
            self.logfile = None      # or snapshot() keeps naming a file nobody writes
            self.logging = False

    # ---- sampling --------------------------------------------------------
    def sample(self):
        now = time.monotonic()
        dt = max(now - self._t, 1e-3)
        self._t = now

        gpu = GPU.sample() if GPU else {}
        therm = THERMAL.sample() if THERMAL.ok else {}
        lhm = SENSOR.sample() if SENSOR.ok else {}

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
            "cpu_temp": lhm.get("cpu_temp"),
            "fan_rpm": lhm.get("fan_rpm"),
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

    def snapshot_metric(self, mid):
        """Just the one metric the hover popup is showing. It polls faster than the
        1s sample rate to paint quickly on open, so copying all twelve histories
        each time — and holding the lock against the sampler to do it — is waste."""
        with self.lock:
            s = self.stats.get(mid)
            avg = (s["sum"] / s["n"]) if s and s["n"] else None
            return {"latest": self.latest.get(mid),
                    "hist": list(self.hist[mid]) if mid in self.hist else [],
                    "stats": {"min": s["min"], "max": s["max"], "avg": avg} if s else None,
                    "extra": dict(self.extra)}


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

    def request_size(self, css_width):
        """Fit the embedded bar to its content width (CSS px). Returns True once placed."""
        return _place(css_width)

    def open_settings(self):
        _open_settings()

    def show_detail(self, metric_id, chip_center_css):
        return _show_detail(metric_id, chip_center_css)

    def beep(self):
        if CONFIG.get("sound", True):
            try:
                winsound.MessageBeep(0x30)   # MB_ICONWARNING
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
        _close_settings()

    def save_config(self, cfg):
        store_config(cfg)
        self._close()
        return True

    def cancel(self):
        self._close()
        return True

    # CSV logging now lives here instead of on the bar (the bar just shows a
    # flashing dot while recording).
    def logging_state(self):
        return {"logging": collector.logging,
                "logfile": os.path.basename(collector.logfile) if collector.logfile else None}

    def toggle_log(self):
        if collector.logging:
            collector.stop_log()
            return {"logging": False, "logfile": None}
        f = collector.start_log()
        return {"logging": True, "logfile": os.path.basename(f)}

    def open_logs(self):
        os.startfile(LOG_DIR)
        return True

    def fit(self, css_h):
        return _fit_settings(css_h)


with open(os.path.join(HERE, "ui.html"), encoding="utf-8") as _f:
    HTML = _f.read()
with open(os.path.join(HERE, "settings.html"), encoding="utf-8") as _f:
    SETTINGS_HTML = _f.read()
with open(os.path.join(HERE, "detail.html"), encoding="utf-8") as _f:
    DETAIL_HTML = _f.read()
with open(os.path.join(HERE, "away.html"), encoding="utf-8") as _f:
    AWAY_HTML = _f.read()


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
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.GetWindowThreadProcessId.restype = wt.DWORD
    user32.IsWindowVisible.argtypes = [wt.HWND]
    user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]


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


# Windows shell UI whose host windows can momentarily cover the whole monitor
_SHELL_PROCS = {
    "startmenuexperiencehost.exe", "searchhost.exe", "searchapp.exe",
    "shellexperiencehost.exe", "textinputhost.exe",
}


_ENUM_PROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def _fullscreen_on_bar_monitor(user32):
    """True when any window covers the whole monitor the bar sits on (fullscreen
    video, game, or slideshow) - even if it isn't the foreground window. This is
    what catches a fullscreen app on the bar's screen while you're working on a
    second monitor, which a foreground-only check misses."""
    our = EMBED_STATE.get("hwnd")
    if not our:
        return False
    our_mon = user32.MonitorFromWindow(our, 2)     # MONITOR_DEFAULTTONEAREST
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(our_mon, ctypes.byref(mi)):
        return False
    mon = mi.rcMonitor
    hit = [False]

    def _cb(hwnd, _):
        if hit[0]:
            return False
        if hwnd == our or not user32.IsWindowVisible(hwnd):
            return True
        if user32.MonitorFromWindow(hwnd, 2) != our_mon:
            return True                            # on another screen
        buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, buf, 64)
        if buf.value in ("Progman", "WorkerW", "Shell_TrayWnd"):
            return True                            # desktop / taskbar
        r = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        if not (r.left <= mon.left and r.top <= mon.top
                and r.right >= mon.right and r.bottom >= mon.bottom):
            return True                            # doesn't cover the whole monitor (e.g. maximised)
        try:                                       # ignore shell surfaces (Start, Search, touch keyboard)
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if psutil.Process(pid.value).name().lower() in _SHELL_PROCS:
                return True
        except Exception:
            pass
        hit[0] = True
        return False                               # found one - stop enumerating

    user32.EnumWindows(_ENUM_PROC(_cb), 0)
    return hit[0]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


def _idle_seconds():
    """Seconds since the last keyboard or mouse input (how long you've been away)."""
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
    return (ctypes.windll.kernel32.GetTickCount() - lii.dwTime) / 1000.0


_IDLE_NAMES = {"System Idle Process", "Idle"}


def _top_cpu_processes(n=5):
    """(name, cpu%% of total) for the busiest real processes since the last call."""
    ncpu = psutil.cpu_count() or 1
    out = []
    for p in psutil.process_iter(["name"]):
        name = p.info["name"]
        if name in _IDLE_NAMES:      # the idle process is unused CPU, not a culprit
            continue
        try:
            c = p.cpu_percent(None) / ncpu
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if c > 0.5:
            out.append((name or ("pid " + str(p.pid)), c))
    out.sort(key=lambda x: -x[1])
    return out[:n]


# Windows' own memory bookkeeping routinely holds the largest RSS on the box.
# Naming it tells you nothing you can act on, the same reason the idle process is
# excluded from CPU attribution.
_MEM_SKIP = {"memory compression", "memcompression", "system", "registry"}


def _top_mem_process():
    """(name, MB) of the largest resident process right now. Sampled once, when you
    get back: whatever is still holding memory then is the one worth naming. If it
    had already exited the memory would have come back and there'd be nothing to
    report. Returns ("", 0) if nothing can be read."""
    name, rss = "", 0
    for p in psutil.process_iter(["name", "memory_info"]):
        n = p.info["name"] or ""
        if n.lower() in _MEM_SKIP:
            continue
        try:
            r = p.info["memory_info"].rss
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError, TypeError):
            continue
        if r > rss:
            name, rss = n, r
    return name, round(rss / (1024 * 1024))


EMBED_STATE = {}
HWND_TOPMOST = -1
SWP_NOMOVE_SIZE_ACT = 0x0001 | 0x0002 | 0x0010   # NOSIZE|NOMOVE|NOACTIVATE
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80, 0x08000000
SW_HIDE, SW_SHOW, SW_SHOWNA = 0, 5, 8


def _place_topmost(u, hwnd, x, y, w, h, show, ex_add=0):
    """Move a window, pin it to the topmost band, and show it. Optionally OR in
    extra ex-styles first (tool-window / no-activate). Shared by the bar, the
    hover popup, and the away report so the placement rule lives in one place."""
    if ex_add:
        cur = u.GetWindowLongW(hwnd, -20)          # GWL_EXSTYLE
        u.SetWindowLongW(hwnd, -20, cur | ex_add)
    u.MoveWindow(hwnd, x, y, w, h, True)
    u.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE_SIZE_ACT)
    u.ShowWindow(hwnd, show)


def _place(css_width):
    """Resize/anchor the floating bar to fit `css_width` within the taskbar gap.
    If the gap is too narrow to fit the bar, it yields (hides) rather than cover
    the taskbar buttons, and returns on its own once there's room again."""
    st = EMBED_STATE
    if not st.get("hwnd"):
        return False
    # Hidden by the hotkey, or by a fullscreen app owning the screen. This runs from
    # the bar's own JS (an alert appearing, the logging dot, a width drift), which
    # keeps ticking while the window is hidden — without this it would re-show the
    # bar over the fullscreen app.
    if st.get("user_hidden") or st.get("hidden_fs"):
        return True
    u, scale = st["user32"], st["scale"]
    gap_l, gap_r, top, h = st["gap_l"], st["gap_r"], st["top"], st["height"]
    avail = gap_r - gap_l - 24
    if avail < 160:                  # not enough room; get out of the way of the buttons
        u.ShowWindow(st["hwnd"], SW_HIDE)
        st["yielded"] = True
        return True
    st["yielded"] = False
    w = min(int(round(css_width * scale)) + 6, avail) if css_width else avail
    w = max(w, 160)                  # avail >= 160 here, so w <= avail: never overlaps the buttons
    align = CONFIG.get("align", "right")
    if align == "left":
        x1 = gap_l + 12
    elif align == "center":
        x1 = gap_l + ((gap_r - gap_l) - w) // 2
    else:
        x1 = gap_r - 12 - w
    _place_topmost(u, st["hwnd"], x1, top, w, h, SW_SHOW)   # absolute screen px
    st["x1"], st["w"] = x1, w
    if css_width:
        st["last_css"] = css_width
    return True


def _overlay(user32, gap_l, gap_r, top, height, scale):
    """Float our window over the empty taskbar gap as a topmost, input-owning bar."""
    hwnd = 0
    for _ in range(60):
        hwnd = user32.FindWindowW(None, TITLE_BAR)
        if hwnd:
            break
        time.sleep(0.1)
    if not hwnd:
        return
    # tool window: no taskbar button, no Alt-Tab entry, always topmost
    GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW, WS_EX_TOPMOST = -20, 0x80, 0x40000, 0x08
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    ex = (ex | WS_EX_TOOLWINDOW | WS_EX_TOPMOST) & ~WS_EX_APPWINDOW
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)
    user32.ShowWindow(hwnd, 0)     # hide/show so Windows drops the taskbar/Alt-Tab entry
    EMBED_STATE.update(hwnd=hwnd, user32=user32, gap_l=gap_l, gap_r=gap_r,
                       top=top, height=height, scale=scale)
    _place(None)   # re-shows it (SW_SHOW) as a tool window and fits it to the gap


def _watcher():
    """Keep the bar correctly placed and on top as the taskbar changes over time."""
    while True:
        time.sleep(1)
        st = EMBED_STATE
        if not st.get("hwnd"):
            continue
        if st.get("user_hidden"):     # hidden by the hotkey; don't fight it
            continue
        try:
            u = st["user32"]
            # hide while a fullscreen app (video, game, slideshow) owns the screen
            fs = _fullscreen_on_bar_monitor(u)
            if fs:
                # Gate on whether the bar is actually visible rather than on our own
                # flag: if anything else re-showed it, the flag alone would say
                # "already handled" and leave it stranded over the fullscreen app.
                if u.IsWindowVisible(st["hwnd"]):
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


def _parse_hotkey(spec):
    """Turn 'ctrl+alt+g' into the virtual-key codes that must all be held."""
    if not spec:
        return None
    mods = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10,
            "win": 0x5B, "super": 0x5B}
    vks, key = [], None
    for part in str(spec).lower().split("+"):
        part = part.strip()
        if part in mods:
            vks.append(mods[part])
        elif len(part) == 1 and part.isalnum():
            key = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit():
            key = 0x70 + int(part[1:]) - 1     # F1..F24
    return (vks + [key]) if key is not None else None


def _toggle_hidden():
    """Hide/show the bar on demand (hotkey), so the taskbar underneath is reachable."""
    st = EMBED_STATE
    if not st.get("hwnd"):
        return
    u = st["user32"]
    hide = not st.get("user_hidden", False)
    st["user_hidden"] = hide
    if hide:
        u.ShowWindow(st["hwnd"], SW_HIDE)
        _hide_detail()
    elif not st.get("hidden_fs"):     # a fullscreen app still owns the screen;
        u.ShowWindow(st["hwnd"], SW_SHOW)   # the watcher brings it back on exit
        _place(st.get("last_css"))


def _hotkey_loop():
    """Poll the global hide/show hotkey (default Ctrl+Alt+G) and toggle on a fresh press."""
    keys = _parse_hotkey(CONFIG.get("hotkey", "ctrl+alt+g"))
    if not keys:
        return
    u = ctypes.windll.user32
    was_down = False
    while True:
        time.sleep(0.05)
        try:
            down = all(u.GetAsyncKeyState(k) & 0x8000 for k in keys)
            if down and not was_down:
                _toggle_hidden()
            was_down = down
        except Exception:
            pass


DETAIL = {"metric": None, "rect": None}


class DetailApi:
    def get(self):
        mid = DETAIL.get("metric")
        if not mid:
            return {"metric": None}
        snap = collector.snapshot_metric(mid)
        return {"metric": mid,
                "latest": snap["latest"],
                "hist": snap["hist"],
                "stats": snap["stats"],
                "extra": snap["extra"],
                "temp_unit": CONFIG.get("temp_unit", "C")}


_DETAIL_HWND = None


def _detail_hwnd(user32):
    # The detail window is created once and lives for the whole process, so its
    # handle never changes. Resolve it a single time and reuse — the watchdog
    # asks for it every 80ms. A zero result isn't cached, so it retries until the
    # window exists.
    global _DETAIL_HWND
    if not _DETAIL_HWND:
        _DETAIL_HWND = user32.FindWindowW(None, TITLE_DETAIL)
    return _DETAIL_HWND


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
    # tool window (no taskbar button) + no-activate so it never steals focus
    _place_topmost(u, hwnd, x, y, W, H, SW_SHOWNA, WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
    DETAIL["rect"] = (x, y, x + W, y + H)   # physical px, for the cursor watchdog
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
    DETAIL["rect"] = None
    return True


def _bar_rect(u):
    hwnd = EMBED_STATE.get("hwnd")
    if not hwnd:
        return None
    r = wt.RECT()
    if u.GetWindowRect(hwnd, ctypes.byref(r)):
        return (r.left, r.top, r.right, r.bottom)
    return None


def _pt_in_rect(x, y, rect, margin=0):
    if not rect:
        return False
    left, top, right, bottom = rect
    return (left - margin) <= x <= (right + margin) and (top - margin) <= y <= (bottom + margin)


def _detail_watchdog():
    """Hide the hover popup once the cursor leaves both the bar and the popup.

    The bar's DOM mouseleave is not reliable when the pointer exits onto the
    adjacent topmost popup or off a screen edge, which could leave the popup
    stuck open. Polling the real cursor position is robust, and counting the
    popup's own rect as "inside" lets you move onto it to read it. A 12px
    margin bridges the small gap between the bar and the popup.

    The gate is the popup's *actual* visibility, not our own metric flag: if a
    hide ever no-ops (a missed FindWindow, or the DOM path clearing state while
    the window is still up) the next tick still sees a visible window and retries,
    so the popup can never be stranded on screen.
    """
    u = ctypes.windll.user32
    pt = wt.POINT()
    outside = 0
    while True:
        time.sleep(0.08)
        hwnd = _detail_hwnd(u)
        if not hwnd or not u.IsWindowVisible(hwnd):
            outside = 0
            continue
        try:
            u.GetCursorPos(ctypes.byref(pt))
            inside = (_pt_in_rect(pt.x, pt.y, _bar_rect(u), 12)
                      or _pt_in_rect(pt.x, pt.y, DETAIL.get("rect"), 12))
            if inside:
                outside = 0
            else:
                outside += 1
                if outside >= 2:     # ~160ms of grace before hiding
                    _hide_detail()
                    outside = 0
        except Exception:
            pass


AWAY_POLL = int(os.environ.get("GLINTBAR_AWAY_POLL", "15"))   # seconds between checks
AWAY_WINDOW = min(AWAY_POLL, HISTORY_LEN)   # the collector only keeps this many seconds
AWAY_RAM_RISE = 25           # points of RAM growth that, left high, look like a leak
AWAY_RAM_HIGH = 80           # ...and only if it ended up above this
AWAY = {"report": None}


def _median(vals):
    """The typical level. A peak says only how bad one second got; a mean is
    dragged around by that same second. The middle value is neither."""
    s = sorted(v for v in vals if v is not None)
    if not s:
        return 0.0
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


def _recent(hist, key):
    """The one-second samples the collector already recorded since the last away
    poll. Reading only the latest value saw one second in every AWAY_POLL — about
    7% of the away period — so brief spikes were missed and the peak understated.

    The collector keeps HISTORY_LEN seconds, so with a poll interval longer than
    that (GLINTBAR_AWAY_POLL is a user knob) only the most recent HISTORY_LEN
    seconds of each interval survive to be read — hence AWAY_WINDOW."""
    return [v for v in (hist.get(key) or [])[-AWAY_WINDOW:] if v is not None]


def _away_hwnd(user32):
    return user32.FindWindowW(None, TITLE_AWAY)


AWAY_COLS = ["when", "away_min", "observed_min", "busy_min", "busy_pct", "typical_cpu",
             "peak_cpu", "peak_temp", "temp_src",
             "ram_from", "ram_to", "ram_peak", "mem_top", "mem_top_mb",
             "proc1", "proc1_pct", "proc1_min",
             "proc2", "proc2_pct", "proc2_min",
             "proc3", "proc3_pct", "proc3_min"]


def _log_away(rep):
    try:
        path = os.path.join(LOG_DIR, "away.csv")
        # Appending the wider row under an older header would quietly corrupt the
        # file, so retire that one and start a fresh log beside it.
        if os.path.exists(path):
            with open(path, newline="", encoding="utf-8") as f:
                head = next(csv.reader(f), [])
            if head and head != AWAY_COLS:      # an empty file needs no rescuing
                os.replace(path, os.path.join(
                    LOG_DIR, "away-" + datetime.now().strftime("%Y%m%d%H%M%S") + ".csv"))
        # an existing-but-empty file still needs the header written
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(AWAY_COLS)
            row = [rep["when"], rep["duration_min"], rep["observed_min"],
                   rep["busy_min"], rep["busy_pct"],
                   rep["typical_cpu"], rep["peak_cpu"], rep["peak_temp"], rep["temp_src"],
                   rep["ram_from"], rep["ram_to"], rep["ram_peak"],
                   rep["mem_top"], rep["mem_top_mb"]]
            for i in range(3):
                n, pct, mins = rep["offenders"][i] if i < len(rep["offenders"]) else ("", "", "")
                row += [n, pct, mins]
            w.writerow(row)
    except Exception:
        pass


def _show_away():
    st = EMBED_STATE
    u = st.get("user32") or ctypes.windll.user32
    hwnd = _away_hwnd(u)
    if not hwnd:
        return
    scale = st.get("scale", 1.0)
    # ~283px of content with the "busy" and "RAM" rows; the rest is slack so a font
    # fallback or DPI rounding can't clip the buttons (actions sit at margin-top:auto)
    W, H = int(380 * scale), int(300 * scale)
    sw = u.GetSystemMetrics(0)
    x, y = (sw - W) // 2, int(70 * scale)
    _place_topmost(u, hwnd, x, y, W, H, SW_SHOWNA, WS_EX_TOOLWINDOW)


def _finalize_away(stats):
    if not stats or stats["samples"] == 0:
        return
    peak_cpu, peak_temp = stats["peak_cpu"], stats["peak_temp"]
    # Report a sustained spell, not a momentary spike — of load or of heat. Both
    # criteria are durations now, so one noisy sensor reading can't force a report.
    min_busy = max(3, round(60 / AWAY_POLL))
    ram_first = stats["ram_first"] if stats["ram_first"] is not None else stats["ram_last"]
    ram_last = stats["ram_last"]
    # A machine can be quiet and cool all night and still be in trouble: memory that
    # climbs and stays up is the one soak-test symptom this window is placed to catch.
    leaky = (ram_last - ram_first) >= AWAY_RAM_RISE and ram_last >= AWAY_RAM_HIGH
    if stats["busy_samples"] < min_busy and stats["hot_samples"] < min_busy and not leaky:
        return
    # Rank by CPU time used (share x intervals), not by best moment: a process that
    # sat at 8% for hours matters more than one that touched 10% once.
    top = sorted(stats["proc_acc"].items(), key=lambda kv: -kv[1][0])[:3]
    offenders = [(name, round(share / n, 1), round(n * AWAY_POLL / 60.0, 1))
                 for name, (share, n) in top]
    # Only name a memory holder when memory actually went somewhere, so the line stays
    # quiet on a normal night instead of always accusing whatever is simply biggest.
    mem_name, mem_mb = _top_mem_process() if (ram_last - ram_first) >= 10 else ("", 0)
    AWAY["report"] = {
        "when": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "duration_min": round((time.time() - stats["start"]) / 60, 1),
        # Time actually watched. The loop sleeps between polls, so if the machine
        # suspends mid-away the wall clock keeps running while sampling stops —
        # busy_pct is a share of what was observed, and saying so keeps the two
        # from contradicting each other.
        "observed_min": round(stats["samples"] * AWAY_POLL / 60.0, 1),
        "busy_min": round(stats["busy_samples"] * AWAY_POLL / 60.0, 1),
        "busy_pct": round(100.0 * stats["busy_samples"] / stats["samples"], 1),
        "temp_src": "cpu" if stats["temp_key"] == "cpu_temp" else "sys",
        "typical_cpu": round(_median(stats["cpu_avgs"]), 1),
        "peak_cpu": round(peak_cpu, 1),
        "peak_temp": round(peak_temp, 1),
        "ram_from": round(ram_first, 1),
        "ram_to": round(ram_last, 1),
        "ram_peak": round(stats["ram_peak"], 1),
        "mem_top": mem_name,
        "mem_top_mb": mem_mb,
        "offenders": offenders,
    }
    _log_away(AWAY["report"])
    if CONFIG.get("sound", True):
        try:
            winsound.MessageBeep(0x30)
        except Exception:
            pass
    _show_away()


def _is_locked():
    """True if the workstation is locked (secure desktop can't be opened)."""
    u = ctypes.windll.user32
    u.OpenInputDesktop.restype = wt.HANDLE
    h = u.OpenInputDesktop(0, False, 0x0100)   # DESKTOP_SWITCHDESKTOP
    if h:
        u.CloseDesktop(h)
        return False
    return True


def away_loop():
    """While you're away, watch for a busy machine and note the process behind it.
    'Away' means the screen is locked, or no keyboard/mouse for away_after_min."""
    away, stats = False, None
    _top_cpu_processes()             # prime per-process CPU baselines
    while True:
        time.sleep(AWAY_POLL)
        try:
            tops = _top_cpu_processes()   # ONE measurement per interval (~AWAY_POLL window)
            if not CONFIG.get("away_watch", True):
                away, stats = False, None
                continue
            idle = _idle_seconds()
            threshold = CONFIG.get("away_after_min", 5) * 60
            if _is_locked() or idle >= threshold:
                if not away:
                    away = True
                    stats = {"start": time.time(), "peak_cpu": 0.0, "peak_temp": 0.0,
                             "proc_acc": {}, "cpu_avgs": [], "hot_samples": 0,
                             "ram_first": None, "ram_last": 0.0, "ram_peak": 0.0,
                             "temp_key": None,
                             "busy_samples": 0, "samples": 0}
                snap = collector.snapshot()
                latest, hist = snap["latest"], snap["hist"]
                cpu_win = _recent(hist, "cpu")
                # Headline peak: the highest of every second in the interval.
                cpu_peak = max(cpu_win) if cpu_win else (latest.get("cpu") or 0.0)
                # "Busy" means sustained, so gate on the interval average — which is
                # also the basis the per-process numbers use, so they line up.
                cpu_avg = sum(cpu_win) / len(cpu_win) if cpu_win else cpu_peak
                if stats["temp_key"] is None:
                    # Fix the sensor for the whole away period. If one drops out midway
                    # (LHM closed, an HWiNFO read fails) the peak would otherwise be a
                    # max across two scales — CPU package vs the cooler ACPI zone —
                    # counted against one threshold.
                    stats["temp_key"] = "cpu_temp" if latest.get("cpu_temp") else "sys_temp"
                temp_key = stats["temp_key"]
                temp_win = _recent(hist, temp_key)
                temp = max(temp_win) if temp_win else (latest.get(temp_key) or 0.0)
                stats["peak_cpu"] = max(stats["peak_cpu"], cpu_peak)
                stats["peak_temp"] = max(stats["peak_temp"], temp)
                stats["cpu_avgs"].append(cpu_avg)     # for the typical level
                # RAM is a level, not a spike: keep where it started and where it got
                # to, so an overnight climb that never comes back is visible at all.
                ram = latest.get("ram_pct")
                if ram is not None:
                    if stats["ram_first"] is None:
                        stats["ram_first"] = ram
                    stats["ram_last"] = ram
                    ram_win = _recent(hist, "ram_pct")
                    stats["ram_peak"] = max(stats["ram_peak"], max(ram_win) if ram_win else ram)
                stats["samples"] += 1
                if temp >= CONFIG.get("away_hot_c", 85):
                    stats["hot_samples"] += 1         # how long it ran hot, not just how hot
                if cpu_avg >= CONFIG.get("away_cpu_pct", 25):
                    stats["busy_samples"] += 1
                    by_name = {}                      # sum same-named procs this sample
                    for name, c in tops:
                        by_name[name] = by_name.get(name, 0.0) + c
                    for name, c in by_name.items():
                        # Accumulate share and intervals, so a process can be ranked by
                        # the CPU time it actually used rather than by its best moment.
                        acc = stats["proc_acc"].setdefault(name, [0.0, 0])
                        acc[0] += c
                        acc[1] += 1
            elif away:
                away = False
                _finalize_away(stats)
                stats = None
        except Exception:
            pass


class AwayApi:
    def get(self):
        return AWAY.get("report")

    def dismiss(self):
        u = ctypes.windll.user32
        hwnd = _away_hwnd(u)
        if hwnd:
            u.ShowWindow(hwnd, 0)
        return True

    def open_logs(self):
        os.startfile(LOG_DIR)
        return True


def _settings_x(u, W):
    """Right-align the settings window near the bar, clamped on-screen (physical px)."""
    st = EMBED_STATE
    bx1 = st.get("x1", 0) + st.get("w", 0)
    x = (bx1 - W) if bx1 else ((u.GetSystemMetrics(0) - W) // 2)   # SM_CXSCREEN
    gap_r = st.get("gap_r")
    if gap_r:
        x = min(x, gap_r - W)
    return max(x, 8)


# The panel is born off screen so its fit-to-content pass isn't a visible stutter:
# it would otherwise appear at a placeholder height and immediately jump to its
# real one, moving as well as resizing (the y depends on the height).
SETTINGS_OFFSCREEN = -32000


def _place_settings(u, hwnd, W, H):
    """Float the settings panel just above the bar, growing upward."""
    screen_h = u.GetSystemMetrics(1)                 # SM_CYSCREEN
    top = EMBED_STATE.get("top", screen_h)
    u.MoveWindow(hwnd, _settings_x(u, W), max(top - H - 8, 8), W, H, True)


def _fit_settings(css_h):
    """Size the settings window to its content and bring it on screen, capped so
    tall content stays visible (never full-screen)."""
    st = EMBED_STATE
    u = st.get("user32") or ctypes.windll.user32
    hwnd = u.FindWindowW(None, TITLE_SETTINGS)
    if not hwnd:
        return False
    scale = st.get("scale", 1.0)
    wr, cr = wt.RECT(), wt.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(wr))
    u.GetClientRect(hwnd, ctypes.byref(cr))
    chrome_h = (wr.bottom - wr.top) - cr.bottom      # title bar + borders
    W = wr.right - wr.left
    screen_h = u.GetSystemMetrics(1)
    H = min(int(round(css_h * scale)) + chrome_h + 2, int(screen_h * 0.92))
    _place_settings(u, hwnd, W, H)
    return True


def _settings_failsafe():
    """If the fit never reports back — a script error, a webview that didn't run it
    — bring the panel on screen at its default size rather than leaving it parked
    off screen where it can't be found."""
    time.sleep(1.5)
    u = EMBED_STATE.get("user32") or ctypes.windll.user32
    hwnd = u.FindWindowW(None, TITLE_SETTINGS)
    if not hwnd:
        return
    r = wt.RECT()
    if u.GetWindowRect(hwnd, ctypes.byref(r)) and r.left <= SETTINGS_OFFSCREEN // 2:
        _place_settings(u, hwnd, r.right - r.left, r.bottom - r.top)


def _close_settings():
    """Shut the settings panel if it's open. True if there was one to shut."""
    found = False
    for w in list(webview.windows):
        if w.title == TITLE_SETTINGS:
            found = True
            try:
                w.destroy()
            except Exception:
                pass
    return found


def _open_settings():
    # The gear toggles. Clicking it while the panel is up closes it, rather than
    # looking broken by doing nothing.
    if _close_settings():
        return
    W, H = 360, 480                       # logical (DIP); JS calls fit() to trim to content
    # Off screen, not hidden: a hidden webview may not lay out, and then the
    # scrollHeight the fit relies on comes back wrong. _fit_settings brings it in.
    webview.create_window(
        TITLE_SETTINGS, html=SETTINGS_HTML, js_api=SettingsApi(),
        width=W, height=H, x=SETTINGS_OFFSCREEN, y=0, resizable=True, on_top=True,
        background_color="#12161c",
    )
    threading.Thread(target=_settings_failsafe, daemon=True).start()


_INSTANCE_MUTEX = None


def _single_instance():
    """False if another GlintBar is already running (keeps the mutex for our lifetime)."""
    global _INSTANCE_MUTEX
    # use_last_error so ctypes captures GetLastError right at the CreateMutexW call,
    # before any intervening FFI can clobber the thread's last-error.
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    _INSTANCE_MUTEX = k.CreateMutexW(None, False, "GlintBar_singleton_mutex")
    return ctypes.get_last_error() != 183   # ERROR_ALREADY_EXISTS


def _diag():
    """Print the facts that decide how GlintBar renders on this machine
    (taskbar detection, providers, timings). Run:  python -m glintbar --diag"""
    import platform
    print("GlintBar diagnostics")
    print("  python    :", platform.python_version())
    print("  windows   :", platform.platform())
    try:
        from importlib.metadata import version
        print("  pywebview :", version("pywebview"), " psutil:", version("psutil"))
    except Exception:
        pass
    print("  module init (imports + provider detection): %.2fs" % (time.monotonic() - _T0))
    u = ctypes.windll.user32
    try:
        if not u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            u.SetProcessDPIAware()
    except Exception:
        pass
    _win32_setup(u)
    try:
        print("  dpi scale :", u.GetDpiForSystem() / 96.0,
              " monitors:", u.GetSystemMetrics(80))       # SM_CMONITORS
    except Exception:
        pass
    print("providers")
    print("  gpu       :", GPU_KIND, GPU_METRICS)
    print("  thermal   :", THERMAL_METRICS or "none")
    print("  sensor    :", (type(SENSOR).__name__ if SENSOR.ok else "none"), SENSOR_METRICS)
    print("  tiles     :", AVAILABLE)
    t0 = time.perf_counter()
    _nvidia_ok()
    print("  nvidia-smi probe: %.2fs" % (time.perf_counter() - t0))
    print("taskbar")
    tray = u.FindWindowW("Shell_TrayWnd", None)
    print("  Shell_TrayWnd :", ("found " + str(_rect(u, tray))) if tray else "NOT FOUND")
    if tray:
        notify = u.FindWindowExW(tray, None, "TrayNotifyWnd", None)
        rebar = u.FindWindowExW(tray, None, "ReBarWindow32", None)
        print("  TrayNotifyWnd :", _rect(u, notify) if notify else "not found (use taskbar right edge)")
        print("  ReBarWindow32 :", _rect(u, rebar) if rebar else "not found (Win11: 30% heuristic)")
    region = _taskbar_region(u)
    if region:
        (tbl, tbt, tbr, tbb, _t), (gl, gr) = region
        print("  gap           : x %d..%d (%d px wide) at y=%d" % (gl, gr, gr - gl, tbt))
        print("  verdict       : would EMBED in the taskbar gap")
    else:
        print("  verdict       : would FALL BACK to a floating bar (no gap found)")


def _say(msg):
    """Console feedback when run via python.exe; no-op under pythonw (stdout=None)."""
    try:
        if sys.stdout:
            print(msg, flush=True)
    except Exception:
        pass


def main():
    if "--diag" in sys.argv:
        _diag()
        return
    if not _single_instance():
        _say("GlintBar is already running (this launch did nothing). "
             "Close it from the bar's X button first, or just use the running one.")
        return
    user32 = ctypes.windll.user32
    # Per-monitor-v2 awareness so coordinates and scale stay correct across monitors
    # with different scaling (matches how WebView2 renders). Fall back on old Windows.
    ok = False
    try:
        ok = bool(user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)))
    except Exception:
        ok = False
    if not ok:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
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
    bar = webview.create_window(
        TITLE_BAR, html=HTML, js_api=api,
        width=width, height=height, x=x, y=y,
        min_size=(100, 1),      # allow a very thin bar (default min is 100 tall)
        frameless=True, on_top=True, resizable=True, easy_drag=True,
        background_color="#0b0f14",
    )
    # hover-to-expand popup, created hidden; shown above the bar on chip hover
    webview.create_window(
        TITLE_DETAIL, html=DETAIL_HTML, js_api=DetailApi(),
        width=320, height=180, min_size=(80, 1), hidden=True,
        frameless=True, on_top=True, resizable=False,
        background_color="#12161c",
    )
    # "while you were away" report popup, created hidden; shown on your return
    webview.create_window(
        TITLE_AWAY, html=AWAY_HTML, js_api=AwayApi(),
        width=380, height=240, min_size=(80, 1), hidden=True,
        frameless=True, on_top=True, resizable=False,
        background_color="#12161c",
    )
    if EMBED and embed_args is not None:
        # embed once the window actually exists (pywebview 'shown' event), so a
        # slow machine (AV-scanned cold start) can't lose a startup race
        bar.events.shown += lambda *a: threading.Thread(
            target=_overlay, args=(user32, *embed_args), daemon=True).start()
        threading.Thread(target=_watcher, daemon=True).start()
        threading.Thread(target=_detail_watchdog, daemon=True).start()
        threading.Thread(target=_hotkey_loop, daemon=True).start()
    threading.Thread(target=away_loop, daemon=True).start()
    t = threading.Thread(target=sampler_loop, daemon=True)
    t.start()
    _say("GlintBar starting (first launch can take a while on managed machines).\n"
         "Running attached to this console: closing it closes the bar. For a\n"
         "detached bar, use the installed `glintbar` command or start_glintbar.cmd.")
    webview.start()


if __name__ == "__main__":
    main()
