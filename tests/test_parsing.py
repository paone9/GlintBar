"""Unit tests for GlintBar's pure parsing / config / sensor-selection seams.

These are the branchy, hand-fed bits where a silent regression would hide:
the nvidia-smi field parse (#1), the general number scrape, config coercion (#6),
LibreHardwareMonitor sensor selection, the hotkey parser, and metric validation.

They import glintbar.monitor, which probes hardware at import time and pulls in
Windows-only modules (winsound, ctypes.wintypes) -> this suite runs on Windows CI.
"""
import types

import glintbar.monitor as gm

# --- NvidiaProvider.sample: the poll throttle must survive a failing GPU ---

def test_nvidia_keeps_throttling_when_the_gpu_is_failing(monkeypatch):
    """A failing GPU empties the cache so the tiles blank instead of showing stale
    numbers as live. The throttle must not be conditional on that cache being
    non-empty, or every 1s sampler tick spawns nvidia-smi — and a wedged driver
    that hangs would then stall the sampler thread on each 5s timeout.
    """
    clock = {"t": 1000.0}
    spawns = []

    class _Sub:
        CREATE_NO_WINDOW = 0

        @staticmethod
        def run(*a, **k):
            spawns.append(1)
            raise OSError("nvidia-smi is not answering")

    monkeypatch.setattr(gm, "time", types.SimpleNamespace(
        monotonic=lambda: clock["t"], sleep=lambda _s: None))
    monkeypatch.setattr(gm, "subprocess", _Sub)

    p = gm.NvidiaProvider()
    p._cache = {"gpu_temp": 61.0}          # a good reading to start from
    for _ in range(10):                    # ten sampler ticks, one second apart
        clock["t"] += 1.0
        p.sample()

    # two seconds apart while it still hopes, then the long backoff — not one per tick
    assert len(spawns) == 3
    assert p._fails >= p._MAX_FAILS
    assert p.sample() == {}                # blank, rather than the stale 61.0


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


# --- _effective_align: don't trust an unmeasured left edge (taskbargap integration) ---

def test_effective_align_honours_configured_align_when_measured():
    assert gm._effective_align("left", True) == "left"
    assert gm._effective_align("center", True) == "center"
    assert gm._effective_align("right", True) == "right"


def test_effective_align_overrides_to_right_when_unmeasured():
    # gap_l is a 30% heuristic here and can sit on top of real taskbar buttons;
    # gap_r is independently sanity-checked, so right is the only safe anchor
    assert gm._effective_align("left", False) == "right"
    assert gm._effective_align("center", False) == "right"


def test_effective_align_right_is_unaffected_by_measured():
    # already anchored off the trustworthy edge, so measured is moot
    assert gm._effective_align("right", False) == "right"


# --- _fs_debounce: don't hide over a transient shell surface's brief false
# positive - and symmetrically, don't un-hide over one either. _watcher calls
# this generic function twice per tick with independent streak state: once with
# the raw verdict (debouncing hide) and once with its negation (debouncing
# un-hide), so a spurious single-tick misread can't flip either direction.

def test_fs_debounce_false_never_starts_a_streak():
    assert gm._fs_debounce(False, None, 100.0) == (False, None)
    assert gm._fs_debounce(False, 90.0, 100.0) == (False, None)


def test_fs_debounce_first_true_tick_starts_the_clock_but_does_not_hide():
    # a ~1s shell toast is exactly this: one True tick, then gone
    should_hide, fs_since = gm._fs_debounce(True, None, 100.0)
    assert should_hide is False
    assert fs_since == 100.0


def test_fs_debounce_not_hidden_before_hold_elapses():
    should_hide, fs_since = gm._fs_debounce(True, 100.0, 100.0 + gm.FS_HIDE_HOLD_S - 0.1)
    assert should_hide is False
    assert fs_since == 100.0     # streak start is preserved, not reset


def test_fs_debounce_hides_once_hold_elapses():
    should_hide, fs_since = gm._fs_debounce(True, 100.0, 100.0 + gm.FS_HIDE_HOLD_S)
    assert should_hide is True
    assert fs_since == 100.0


def test_fs_debounce_clearing_resets_the_streak():
    # verdict flips back to False mid-streak (the shell surface disappeared)
    assert gm._fs_debounce(False, 100.0, 100.5) == (False, None)


# --- _fs_streaks: one tick of both directions at once, including the reset
# between them - the state transition _watcher relies on but can't unit-test
# on its own, since it only exists as a line inside the Win32-coupled loop.

def test_fs_streaks_covering_builds_hide_streak_and_clears_clear_streak():
    # mid-way through a clear streak when fullscreen resumes: the clear streak
    # must reset to None, not keep counting toward an un-hide that's now wrong
    should_hide, should_show, fs_since, clear_since = gm._fs_streaks(
        True, None, 90.0, 100.0)
    assert (should_hide, should_show) == (False, False)
    assert fs_since == 100.0      # hide streak just started
    assert clear_since is None    # clear streak reset, not left at 90.0


def test_fs_streaks_hides_once_hide_hold_elapses():
    should_hide, _should_show, _fs_since, clear_since = gm._fs_streaks(
        True, 100.0, None, 100.0 + gm.FS_HIDE_HOLD_S)
    assert should_hide is True
    assert clear_since is None


def test_fs_streaks_clearing_builds_clear_streak_and_resets_hide_streak():
    # mid-way through a hide streak when the screen clears: the hide streak
    # must reset, matching _fs_debounce's own reset-on-False behaviour
    should_hide, should_show, fs_since, clear_since = gm._fs_streaks(
        False, 90.0, None, 100.0)
    assert (should_hide, should_show) == (False, False)
    assert fs_since is None
    assert clear_since == 100.0   # clear streak just started


def test_fs_streaks_shows_after_one_confirming_tick_not_the_full_hide_hold():
    # FS_CLEAR_HOLD_S is one tick, not FS_HIDE_HOLD_S's three - this is the
    # asymmetry the un-hide side is supposed to have
    assert gm.FS_CLEAR_HOLD_S < 1.0 < gm.FS_HIDE_HOLD_S
    _should_hide, should_show, _fs_since, _clear_since = gm._fs_streaks(
        False, None, 100.0, 100.0 + gm.FS_CLEAR_HOLD_S)
    assert should_show is True


# --- _bar_geometry: the one rule that keeps the bar off the taskbar's buttons ---

# The live Windows 11 layout this was built against: 3840 wide at 175%, gap
# 1405..3075 between the last app button and the Widgets button (taskbargap 0.2,
# measured through UI Automation).
GAP_L, GAP_R, SCALE = 1405, 3075, 1.75


def _inside(geo, left=GAP_L, right=GAP_R):
    x, w = geo
    return left <= x and x + w <= right


def test_bar_geometry_fits_content_inside_the_gap():
    geo = gm._bar_geometry(GAP_L, GAP_R, SCALE, True, 400, "right")
    assert geo is not None and _inside(geo)
    x, w = geo
    assert w == round(400 * SCALE) + 6
    assert x + w == GAP_R - round(gm.BAR_EDGE_MARGIN_PX * SCALE)   # right-aligned, with margin


def test_bar_geometry_never_widens_past_the_gap_however_much_the_page_asks():
    for css in (900, 1200, 5000):
        geo = gm._bar_geometry(GAP_L, GAP_R, SCALE, True, css, "right")
        assert geo is not None and _inside(geo), css


def test_bar_geometry_fills_the_gap_when_the_page_has_not_reported_yet():
    geo = gm._bar_geometry(GAP_L, GAP_R, SCALE, True, None, "right")
    assert geo is not None and _inside(geo)
    assert geo[1] == (GAP_R - GAP_L) - 2 * round(gm.BAR_EDGE_MARGIN_PX * SCALE)


def test_bar_geometry_yields_rather_than_squeeze_below_the_minimum():
    assert gm._bar_geometry(1000, 1000 + gm.BAR_MIN_PX + 13, 1.75, True, 400, "right") is None
    geo = gm._bar_geometry(1000, 1000 + gm.BAR_MIN_PX + 14, 1.75, True, 400, "right")
    assert geo is not None and geo[1] == gm.BAR_MIN_PX


def test_bar_geometry_claims_less_of_an_unmeasured_gap_and_anchors_right():
    measured = gm._bar_geometry(GAP_L, GAP_R, SCALE, True, 2000, "left")
    guessed = gm._bar_geometry(GAP_L, GAP_R, SCALE, False, 2000, "left")
    assert measured[0] == GAP_L + round(gm.BAR_EDGE_MARGIN_PX * SCALE)     # left, as configured
    assert guessed[0] + guessed[1] == GAP_R - round(gm.BAR_EDGE_MARGIN_PX * SCALE)   # forced right
    assert guessed[1] == measured[1] - round(gm.UNMEASURED_GAP_MARGIN_PX * SCALE)


def test_bar_geometry_centres_inside_the_gap():
    x, w = gm._bar_geometry(GAP_L, GAP_R, SCALE, True, 300, "center")
    assert abs((x - GAP_L) - (GAP_R - (x + w))) <= 1
