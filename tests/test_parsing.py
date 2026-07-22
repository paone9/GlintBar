"""Unit tests for GlintBar's pure parsing / config / sensor-selection seams.

These are the branchy, hand-fed bits where a silent regression would hide:
the nvidia-smi field parse (#1), the general number scrape, config coercion (#6),
LibreHardwareMonitor sensor selection, the hotkey parser, and metric validation.

They import glintbar.monitor, which probes hardware at import time and pulls in
Windows-only modules (winsound, ctypes.wintypes) -> this suite runs on Windows CI.
"""
import glintbar.monitor as gm


# --- _nv_num: nvidia-smi field parse, tolerant of [N/A] (regression guard, #1) ---

def test_nv_num_plain_and_whitespace():
    assert gm._nv_num("55") == 55.0
    assert gm._nv_num("  85.3 ") == 85.3


def test_nv_num_na_and_empty_are_none():
    assert gm._nv_num("[N/A]") is None
    assert gm._nv_num("") is None
    assert gm._nv_num("N/A") is None


def test_nv_num_non_string_is_none():
    # nvidia-smi only ever hands us strings; anything else is missing, not a crash
    assert gm._nv_num(None) is None


# --- _num: general number scrape (LHM values, comma decimals, units) ---

def test_num_extracts_from_unit_string():
    assert gm._num("55.0 °C") == 55.0
    assert gm._num("3000 RPM") == 3000.0


def test_num_comma_decimal():
    assert gm._num("1,5") == 1.5
    assert gm._num("12,75") == 12.75


def test_num_comma_thousands_is_not_a_decimal():
    # "1,234 RPM" is a four-figure fan, not 1.234 — three digits after the comma
    # is grouping, one or two is a decimal comma
    assert gm._num("1,234 RPM") == 1234.0
    assert gm._num("12,345") == 12345.0


def test_num_negative():
    assert gm._num("-10") == -10.0


def test_num_non_numeric_is_none():
    assert gm._num("abc") is None
    assert gm._num("") is None
    assert gm._num(None) is None


def test_num_passthrough_number():
    assert gm._num(42) == 42.0


# --- _coerce_config: valid-JSON-but-wrong-type inputs (the reason it exists, #6) ---

def test_coerce_stringly_numbers():
    c = gm._coerce_config({"away_after_min": "5", "away_cpu_pct": "25"})
    assert c["away_after_min"] == 5 and isinstance(c["away_after_min"], int)
    assert c["away_cpu_pct"] == 25 and isinstance(c["away_cpu_pct"], int)


def test_coerce_garbage_falls_back_to_default():
    c = gm._coerce_config({"away_after_min": "garbage"})
    assert c["away_after_min"] == gm.DEFAULT_CONFIG["away_after_min"]


def test_coerce_float_number_to_int():
    # a JSON number 7.9 is truncated to the default's int type
    assert gm._coerce_config({"away_after_min": 7.9})["away_after_min"] == 7


def test_coerce_non_integer_string_falls_back():
    # int("7.9") raises, so a decimal *string* is treated as bad input -> default
    assert (gm._coerce_config({"away_after_min": "7.9"})["away_after_min"]
            == gm.DEFAULT_CONFIG["away_after_min"])


def test_coerce_leaves_bools_and_strings_untouched():
    c = gm._coerce_config({"sound": True, "align": "left", "hotkey": "ctrl+alt+g"})
    assert c["sound"] is True
    assert c["align"] == "left"
    assert c["hotkey"] == "ctrl+alt+g"


def test_coerce_missing_key_uses_default():
    c = gm._coerce_config({})
    assert c["away_after_min"] == gm.DEFAULT_CONFIG["away_after_min"]


# --- LhmProvider.sample: sensor selection over a fixture data.json ---

def _lhm(fixture):
    """An LhmProvider whose network fetch is replaced by a fixture (no __init__)."""
    p = gm.LhmProvider.__new__(gm.LhmProvider)
    p._fetch = lambda: fixture
    return p


def test_lhm_prefers_cpu_package_over_cores():
    data = {"Type": "Root", "Children": [
        {"Type": "Temperature", "Text": "CPU Core #1", "Value": "48.0 °C", "Children": []},
        {"Type": "Temperature", "Text": "CPU Package", "Value": "61.5 °C", "Children": []},
        {"Type": "Temperature", "Text": "GPU Core", "Value": "70.0 °C", "Children": []},
        {"Type": "Fan", "Text": "CPU Fan", "Value": "1200 RPM", "Children": []},
        {"Type": "Fan", "Text": "Idle Fan", "Value": "0 RPM", "Children": []},
    ]}
    out = _lhm(data).sample()
    assert out["cpu_temp"] == 61.5
    assert out["fan_rpm"] == 1200


def test_lhm_core_fallback_excludes_gpu():
    data = {"Type": "Root", "Children": [
        {"Type": "Temperature", "Text": "CPU Core #1", "Value": "50", "Children": []},
        {"Type": "Temperature", "Text": "CPU Core #2", "Value": "55", "Children": []},
        {"Type": "Temperature", "Text": "GPU Core", "Value": "80", "Children": []},
    ]}
    # no package/tctl -> falls back to hottest CPU core, never the GPU's 80
    assert _lhm(data).sample()["cpu_temp"] == 55


def test_lhm_amd_tctl_tdie():
    data = {"Type": "Root", "Children": [
        {"Type": "Temperature", "Text": "Core (Tctl/Tdie)", "Value": "63.2", "Children": []},
        {"Type": "Temperature", "Text": "CPU Core #1", "Value": "58", "Children": []},
    ]}
    assert _lhm(data).sample()["cpu_temp"] == 63.2


def test_lhm_nested_children_are_walked():
    data = {"Type": "Root", "Children": [
        {"Type": "Hardware", "Text": "CPU", "Children": [
            {"Type": "Temperature", "Text": "CPU Package", "Value": "59", "Children": []},
        ]},
    ]}
    assert _lhm(data).sample()["cpu_temp"] == 59


def test_lhm_no_data_is_empty():
    assert _lhm(None).sample() == {}


# --- _median: the typical level, used for the away report ---

def test_median_odd_and_even():
    assert gm._median([3.0, 1.0, 2.0]) == 2.0
    assert gm._median([1.0, 2.0, 3.0, 4.0]) == 2.5


def test_median_ignores_a_single_spike():
    # the whole reason it isn't a mean or a max
    quiet = [1.0, 1.0, 1.0, 1.0, 1.0]
    assert gm._median(quiet) == 1.0
    assert gm._median(quiet + [99.0]) == 1.0


def test_median_empty_and_none():
    assert gm._median([]) == 0.0
    assert gm._median([None, None]) == 0.0
    assert gm._median([None, 5.0]) == 5.0


# --- _recent: the away poll's one-second sample window ---

def test_recent_returns_the_whole_window_not_one_sample():
    n = gm.AWAY_POLL
    hist = {"cpu": [float(i) for i in range(n * 3)]}
    w = gm._recent(hist, "cpu")
    assert len(w) == n                    # the interval, not a single reading
    assert max(w) == float(n * 3 - 1)     # so a spike in the window is caught


def test_recent_never_exceeds_the_collector_history():
    # the collector keeps only HISTORY_LEN seconds, so a longer poll interval must
    # not silently claim to have seen more than that
    assert gm.AWAY_WINDOW <= gm.HISTORY_LEN
    hist = {"cpu": [float(i) for i in range(gm.HISTORY_LEN * 3)]}
    assert len(gm._recent(hist, "cpu")) <= gm.HISTORY_LEN


def test_recent_filters_none_values():
    assert gm._recent({"cpu": [1.0, None, 3.0]}, "cpu") == [1.0, 3.0]


def test_recent_missing_or_empty_key():
    assert gm._recent({}, "cpu") == []
    assert gm._recent({"cpu": None}, "cpu") == []


def test_recent_shorter_history_than_window():
    assert gm._recent({"cpu": [5.0, 7.0]}, "cpu") == [5.0, 7.0]


# --- _parse_hotkey: modifier + key -> virtual-key codes ---

def test_hotkey_ctrl_alt_g():
    assert gm._parse_hotkey("ctrl+alt+g") == [0x11, 0x12, ord("G")]


def test_hotkey_case_insensitive():
    assert gm._parse_hotkey("CTRL+ALT+G") == [0x11, 0x12, ord("G")]


def test_hotkey_function_key():
    assert gm._parse_hotkey("f5") == [0x70 + 4]


def test_hotkey_shift_and_win():
    assert gm._parse_hotkey("shift+a") == [0x10, ord("A")]
    assert gm._parse_hotkey("win+d") == [0x5B, ord("D")]


def test_hotkey_empty_or_none():
    assert gm._parse_hotkey("") is None
    assert gm._parse_hotkey(None) is None


def test_hotkey_modifiers_only_is_none():
    # no actual key -> nothing to trigger on
    assert gm._parse_hotkey("ctrl+alt") is None


# --- _valid_metrics: drop unknown ids, never return an empty list ---

def test_valid_metrics_keeps_known():
    # "cpu" is a base metric, always available
    assert gm._valid_metrics(["cpu"]) == ["cpu"]


def test_valid_metrics_drops_unknown():
    assert "bogus" not in gm._valid_metrics(["cpu", "bogus"])


def test_valid_metrics_empty_falls_back_to_all():
    assert gm._valid_metrics([]) == list(gm.AVAILABLE)
    assert gm._valid_metrics(None) == list(gm.AVAILABLE)
