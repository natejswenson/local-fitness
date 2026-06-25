"""Tests for agent/charts.py — the pure terminal-chart renderers.

No DB, no SDK: these assert the deterministic structure of the rendered strings
and the edge cases that bit the prototype (empty windows, flat series, single
points, and negative series like TSB / freshness)."""
from __future__ import annotations

from local_fitness.agent import charts


# --- render_bar_chart ---------------------------------------------------------

def test_bar_chart_one_row_per_point_with_formatted_value():
    out = charts.render_bar_chart(["06-01", "06-02"], [10, 20], value_fmt=lambda v: f"{int(v)}")
    lines = out.split("\n")
    assert len(lines) == 2
    assert lines[0].startswith("06-01") and lines[0].endswith(" 10")
    assert lines[1].endswith(" 20")
    # Bigger value → at least as many emoji squares as the smaller one.
    assert lines[1].count("🟥") + lines[1].count("🟧") + lines[1].count("🟨") >= 1


def test_bar_chart_zero_value_is_an_empty_bar():
    # Non-negative series scales from zero, so a 0 renders no squares — honest
    # for steps / intensity minutes (a rest day reads as empty, not min-height).
    out = charts.render_bar_chart(["d1", "d2"], [0, 100])
    first = out.split("\n")[0]
    assert all(sq not in first for sq in charts._HEAT)


def test_bar_chart_empty_returns_no_data():
    assert charts.render_bar_chart([], []) == charts._NO_DATA
    assert charts.render_bar_chart([], [], title="rhr").startswith("rhr\n")


# --- render_combo_chart -------------------------------------------------------

def test_combo_chart_has_axis_bars_and_trendline():
    out = charts.render_combo_chart(["a", "b", "c"], [1, 2, 3])
    assert "┤" in out and "└" in out  # y-axis + baseline
    assert "█" in out                  # bars
    assert "•" in out                  # trend marker
    assert "rising" in out             # monotonic up → positive slope


def test_combo_chart_handles_negative_series():
    # TSB / freshness lives below zero; the renderer must not crash or clip.
    tsb = [-9.8, -17.4, -31.1, -13.1]
    out = charts.render_combo_chart(["a", "b", "c", "d"], tsb, value_fmt=lambda v: f"{v:.0f}")
    assert "-31" in out  # the trough appears as an axis label
    assert "█" in out and "•" in out


def test_combo_chart_flat_series_is_flat_not_crash():
    out = charts.render_combo_chart(["a", "b", "c"], [5, 5, 5])
    assert "flat" in out


# --- render_sparkline ---------------------------------------------------------

def test_sparkline_one_glyph_per_point():
    out = charts.render_sparkline([1, 2, 3, 4, 5])
    assert len(out) == 5
    assert all(ch in charts._BLOCKS for ch in out)


def test_sparkline_flat_and_single_and_empty():
    assert charts.render_sparkline([7, 7, 7]) == "▁▁▁"  # flat → lowest block, no div/0
    assert len(charts.render_sparkline([42])) == 1
    assert charts.render_sparkline([]) == charts._NO_DATA
