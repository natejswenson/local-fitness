"""Tests for agent/tools.py — the MCP tool handlers that query the DB.

The handlers are async and return ``{"content": [{"type": "text", "text": ...}]}``.
We call them directly against a seeded tmp DB (no SDK runtime, no network).
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pdfplumber
import pytest

from local_fitness import db, plans
from local_fitness.agent import branding, interpret, report_card, tools, visuals


def test_text_emits_compact_json():
    """Tool payloads are compact JSON (no indent) — fewer whitespace tokens
    across the multi-turn loop; the model parses either format (design #3)."""
    res = tools._text({"a": 1, "b": [1, 2], "c": {"d": 3}})
    txt = res["content"][0]["text"]
    assert "\n" not in txt and "  " not in txt
    assert json.loads(txt) == {"a": 1, "b": [1, 2], "c": {"d": 3}}


def call(tool, args):
    """Run a tool handler and return its decoded JSON payload."""
    result = asyncio.run(tool.handler(args))
    text = result["content"][0]["text"]
    try:
        return json.loads(text), result.get("is_error", False)
    except json.JSONDecodeError:
        return text, result.get("is_error", False)


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    db.init_schema(p)
    today = date.today()
    with db.connect(p) as conn:
        for i in range(40):
            d = (today - timedelta(days=i)).isoformat()
            conn.execute(
                "INSERT INTO daily_metrics (date, rhr, sleep_seconds, sleep_score, "
                "avg_stress, body_battery_min, body_battery_max, steps, "
                "intensity_minutes_moderate, intensity_minutes_vigorous) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (d, 50 + (i % 4), 27000 + i * 10, 80, 30, 20, 90, 9000, 20, 5),
            )
            conn.execute(
                "INSERT INTO baselines (date, rhr_60day_mean, rhr_60day_sd, "
                "body_battery_max_60day_mean, ctl, atl, tsb) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (d, 52.0, 2.0, 88.0, 40.0, 45.0, -5.0),
            )
        # Activities incl. one fully-detailed workout.
        conn.execute(
            "INSERT INTO activities (activity_id, date, start_time, activity_type, "
            "activity_name, duration_seconds, distance_meters, avg_hr, max_hr, "
            "training_load, aerobic_te) VALUES "
            "(1, ?, ?, 'running', 'Morning Run', 3600, 10000, 150, 170, 80.0, 3.5)",
            (today.isoformat(), today.isoformat() + "T07:00:00"),
        )
        conn.execute(
            "INSERT INTO activity_hr_zones (activity_id, zone, seconds_in_zone) VALUES (1, 2, 1800)"
        )
        conn.execute(
            "INSERT INTO activity_splits (activity_id, split_index, distance_meters, "
            "duration_seconds, avg_hr) VALUES (1, 0, 1000, 360, 148)"
        )
    return p


def test_get_today_status(seeded):
    # Fix B (2026-07-10 doc): get_today_status now delegates to
    # status.assemble_status() — the old {today, recent_days,
    # current_baseline} raw shape is gone, replaced by assemble_status()'s
    # richer payload (metrics with baseline deltas, training_load, etc).
    payload, err = call(tools.get_today_status, {})
    assert not err
    assert "recent_days" not in payload
    assert "current_baseline" not in payload
    assert payload["training_load"]["ctl"] == 40.0
    assert payload["metrics"]
    assert payload["date"] == date.today().isoformat()


def test_get_today_status_matches_daily_snapshot(seeded):
    # Fix B's convergence invariant: identical payload for identical DB state.
    today_payload, err1 = call(tools.get_today_status, {})
    snapshot_payload, err2 = call(tools.daily_snapshot, {})
    assert not err1 and not err2
    assert today_payload == snapshot_payload


def test_get_today_status_description_mirrors_daily_snapshot():
    today_tool = next(t for t in tools.ALL_TOOLS if t.name == "get_today_status")
    snapshot_tool = next(t for t in tools.ALL_TOOLS if t.name == "daily_snapshot")
    # Must no longer be the stale raw-shape description.
    assert today_tool.description != (
        "Today's metrics + last 7 days alongside the latest 60-day baselines. "
        "Call this first when assessing recovery or making 'should I train "
        "hard' decisions."
    )
    assert today_tool.description == snapshot_tool.description


def test_get_metric_valid(seeded):
    payload, err = call(tools.get_metric, {"metric": "rhr", "days": 14})
    assert not err
    assert all("value" in row for row in payload)


def test_get_metric_unknown(seeded):
    payload, err = call(tools.get_metric, {"metric": "bogus", "days": 14})
    assert err
    assert "unknown metric" in payload["error"]


def test_get_metric_trend(seeded):
    payload, err = call(tools.get_metric_trend, {"metric": "rhr", "days": 14})
    assert not err
    assert payload["n_samples"] > 0
    assert "current_vs_baseline_sd" in payload  # rhr is baseline-tracked


def test_get_metric_trend_unknown(seeded):
    _payload, err = call(tools.get_metric_trend, {"metric": "nope", "days": 14})
    assert err


def test_get_metric_trend_no_data(seeded):
    _payload, err = call(tools.get_metric_trend, {"metric": "vo2_max", "days": 14})
    assert err  # vo2_max never seeded → no rows in window


def test_chart_default_is_compact_calendar(seeded):
    # No style -> calendar (the default). It must be the week-stacked grid (its
    # "Mon→Sun" legend is the signature) and COMPACT: a 30-day window is a handful
    # of week-rows, never one row per day (the truncation bug fix).
    text, err = call(tools.chart, {"metric": "rhr", "days": 30})
    assert not err
    assert "rhr · last 30d" in text
    assert "Mon→Sun" in text          # calendar legend signature
    assert any(sq in text for sq in tools.charts._HEAT)
    assert len(text.splitlines()) <= 10           # ~30 days -> <=5 weeks + headers


def test_chart_bar_style_is_one_row_per_day(seeded):
    # Explicit bar style is still available and is one row per day (best for
    # short windows) — distinctly taller than the calendar for the same window.
    text, err = call(tools.chart, {"metric": "rhr", "days": 14, "style": "bar"})
    assert not err
    assert any(sq in text for sq in tools.charts._HEAT)
    assert "Mon→Sun" not in text                  # NOT the calendar
    assert len([ln for ln in text.splitlines() if any(s in ln for s in tools.charts._HEAT)]) >= 10


def test_chart_calendar_cumulative_steps_weekly_sum(seeded):
    # steps is an additive metric -> the calendar's weekly column is a SUM, not a
    # mean. The fixture seeds 9000 steps/day and any 14-day window contains a full
    # Mon-Sun week, so a 7*9000 = 63000 weekly total must appear (proves the tool
    # routes steps with cumulative=True).
    text, err = call(tools.chart, {"metric": "steps", "days": 14})
    assert not err
    assert "Mon→Sun" in text
    assert "63000" in text


def test_chart_line_style(seeded):
    # A clean box-drawing line (mono) — not braille, not the calendar grid, not emoji.
    text, err = call(tools.chart, {"metric": "rhr", "days": 14, "style": "line"})
    assert not err
    assert "rhr · last 14d" in text
    assert any(g in text for g in "─╭╮╰╯│")       # box-drawing line
    assert not any(0x2800 <= ord(c) <= 0x28FF for c in text)  # NOT braille
    assert "Mon→Sun" not in text                  # NOT the calendar
    assert all(sq not in text for sq in tools.charts._HEAT)   # mono — no emoji


def test_chart_combo_has_trendline(seeded):
    # sleep_seconds varies across the window (steps is flat in the fixture), so
    # bars and the overlaid trend line are both visible.
    text, err = call(tools.chart, {"metric": "sleep_seconds", "days": 14, "style": "combo"})
    assert not err
    assert "█" in text and "•" in text and "┤" in text
    assert "h" in text  # seconds formatted as hours on the axis


def test_chart_spark(seeded):
    text, err = call(tools.chart, {"metric": "rhr", "days": 14, "style": "spark"})
    assert not err
    assert any(b in text for b in tools.charts._BLOCKS)


def test_chart_derived_weighted_intensity(seeded):
    # mod(20) + 2×vig(5) = 30 for every seeded day; the tool must accept the
    # derived metric name and not 500 on the computed column.
    text, err = call(tools.chart, {"metric": "intensity_minutes_weighted", "days": 7})
    assert not err
    assert "intensity_minutes_weighted" in text


def test_chart_baseline_metric_tsb(seeded):
    text, err = call(tools.chart, {"metric": "tsb", "days": 14, "style": "combo"})
    assert not err
    assert "tsb" in text  # pulled from baselines, not daily_metrics
    # The fixture's tsb is a flat -5.0 window: bars must still render, and the
    # axis must show the real value, not a fabricated -4.5 / -4.0 spread.
    assert "█" in text
    assert "-5.0" in text
    assert "-4.5" not in text


@pytest.mark.parametrize("metric", ["ctl", "atl"])
def test_chart_baseline_metrics_ctl_atl(seeded, metric):
    # ctl/atl ride the same whitelisted f-string path against the baselines
    # table as tsb; exercise both so the branch isn't covered by tsb alone.
    text, err = call(tools.chart, {"metric": metric, "days": 14, "style": "spark"})
    assert not err
    assert metric in text


def test_chart_combo_trend_footer_is_unit_consistent(seeded):
    # Significant 1: the combo trend footer reports formatted endpoints (same
    # value_fmt as the axis), never a raw-unit "/step" slope. sleep_seconds shows
    # an "h" axis, so the footer endpoints must read in hours too — not raw seconds.
    text, err = call(tools.chart, {"metric": "sleep_seconds", "days": 14, "style": "combo"})
    assert not err
    footer = [ln for ln in text.split("\n") if "trend" in ln][0]
    assert "→" in footer            # endpoint form, not a per-step number
    assert "/step" not in footer    # no raw-unit slope
    assert "h" in footer            # formatted in hours, matching the axis
    # A unitless integer metric still gets clean integer endpoints (no "/step").
    text2, err2 = call(tools.chart, {"metric": "rhr", "days": 14, "style": "combo"})
    assert not err2
    footer2 = [ln for ln in text2.split("\n") if "trend" in ln][0]
    assert "→" in footer2
    assert "/step" not in footer2
    assert "bpm" not in footer2     # the dead unit param is gone


def test_chart_unknown_metric(seeded):
    payload, err = call(tools.chart, {"metric": "bogus", "days": 14})
    assert err
    assert "unknown metric" in payload["error"]


def test_chart_unknown_style(seeded):
    payload, err = call(tools.chart, {"metric": "rhr", "days": 14, "style": "pie"})
    assert err
    assert "unknown style" in payload["error"]


def test_chart_no_data(seeded):
    payload, err = call(tools.chart, {"metric": "vo2_max", "days": 14})
    assert err  # vo2_max never seeded → no rows in window


def test_chart_value_fmt_vo2_max_keeps_a_decimal():
    # Minor: a realistic vo2_max window (47.9→48.4) must not collapse to "48".
    fmt = tools._chart_value_fmt("vo2_max")
    assert fmt(47.9) == "47.9"
    assert fmt(48.4) == "48.4"
    assert fmt(47.9) != fmt(48.4)  # distinct axis labels


def test_chart_value_fmt_integer_metrics_stay_integer():
    # Genuinely-integer metrics keep integer formatting (no spurious decimals).
    assert tools._chart_value_fmt("steps")(8123.4) == "8123"
    assert tools._chart_value_fmt("rhr")(52.6) == "53"


def test_chart_excluded_from_brief_toolset(seeded):
    # The brief renders its own UI cards; terminal ASCII has no place there.
    # chart is callable (it's in ALL_TOOLS) but deliberately NOT in the brief's
    # read-only allow-list — mirrors the daily_snapshot precedent.
    read_only = tools.read_only_tool_names()
    assert "mcp__fitness__chart" not in read_only
    assert "mcp__fitness__chart" in tools.allowed_tool_names()


def test_query_workouts_filters(seeded):
    payload, err = call(
        tools.query_workouts,
        {"activity_type": "run", "days": 30, "min_distance_km": 5, "min_duration_min": 10, "limit": 10},
    )
    assert not err
    assert len(payload) == 1
    assert payload[0]["activity_id"] == 1


def test_query_workouts_no_filters(seeded):
    payload, err = call(tools.query_workouts, {})
    assert not err
    assert len(payload) >= 1


def test_get_workout_detail_found(seeded):
    payload, err = call(tools.get_workout_detail, {"activity_id": 1})
    assert not err
    assert payload["activity"]["activity_name"] == "Morning Run"
    assert "raw_json" not in payload["activity"]
    assert payload["hr_zones"] and payload["splits"]


def test_get_workout_detail_missing(seeded):
    _payload, err = call(tools.get_workout_detail, {"activity_id": 999})
    assert err


def test_compare_periods_daily(seeded):
    today = date.today()
    a0 = (today - timedelta(days=10)).isoformat()
    a1 = today.isoformat()
    b0 = (today - timedelta(days=30)).isoformat()
    b1 = (today - timedelta(days=20)).isoformat()
    payload, err = call(
        tools.compare_periods,
        {"metric": "rhr", "period_a_start": a0, "period_a_end": a1,
         "period_b_start": b0, "period_b_end": b1},
    )
    assert not err
    assert payload["period_a"]["n"] > 0
    assert payload["delta_mean_a_minus_b"] is not None


def test_compare_periods_training_load(seeded):
    today = date.today()
    payload, err = call(
        tools.compare_periods,
        {"metric": "training_load",
         "period_a_start": (today - timedelta(days=5)).isoformat(),
         "period_a_end": today.isoformat(),
         "period_b_start": (today - timedelta(days=40)).isoformat(),
         "period_b_end": (today - timedelta(days=35)).isoformat()},
    )
    assert not err
    assert payload["period_a"]["n"] >= 1
    assert payload["period_b"]["n"] == 0  # no activities that far back


def test_compare_periods_unknown(seeded):
    _payload, err = call(
        tools.compare_periods,
        {"metric": "xyz", "period_a_start": "2026-01-01", "period_a_end": "2026-01-02",
         "period_b_start": "2026-01-03", "period_b_end": "2026-01-04"},
    )
    assert err


def test_find_anomalies(seeded):
    payload, err = call(tools.find_anomalies, {"metric": "rhr", "sd_threshold": 0.1})
    assert not err
    assert payload["metric"] == "rhr"
    assert isinstance(payload["anomalies"], list)


def test_find_anomalies_unsupported_metric(seeded):
    _payload, err = call(tools.find_anomalies, {"metric": "steps"})
    assert err


def test_training_load_status(seeded):
    payload, err = call(tools.training_load_status, {})
    assert not err
    assert payload["current"]["ctl"] == 40.0


def test_training_load_status_empty(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    _payload, err = call(tools.training_load_status, {})
    assert err


def test_correlate(seeded):
    payload, err = call(tools.correlate, {"metric_a": "sleep_seconds", "metric_b": "rhr", "days": 30})
    assert not err
    assert payload["n_pairs"] >= 5
    assert "pearson_r" in payload


def test_correlate_with_lag(seeded):
    payload, err = call(
        tools.correlate, {"metric_a": "sleep_seconds", "metric_b": "rhr", "days": 30, "lag_days": 1}
    )
    assert not err


def test_correlate_bad_metric(seeded):
    _payload, err = call(tools.correlate, {"metric_a": "foo", "metric_b": "rhr", "days": 30})
    assert err


def test_correlate_insufficient(seeded):
    _payload, err = call(tools.correlate, {"metric_a": "sleep_seconds", "metric_b": "rhr", "days": 2})
    assert err  # < 5 paired points


def test_recovery_pattern(seeded):
    payload, err = call(tools.recovery_pattern, {"activity_type": "run", "min_distance_km": 5})
    assert not err
    assert payload["n_workouts_matched"] >= 0
    assert "recent_workouts" in payload


# === WS1 — interpretation-parity payload attachments ========================
# (docs/plans/2026-07-12-deterministic-intelligence-and-ux-design.md, WS1)

def _decimal_places(x: float) -> int:
    s = repr(float(x))
    return len(s.split(".", 1)[1]) if "." in s else 0


def test_get_metric_trend_slope_direction_present(seeded):
    payload, err = call(tools.get_metric_trend, {"metric": "rhr", "days": 14})
    assert not err
    assert payload["slope_direction"] in ("rising", "falling", "flat", "no data")


def test_get_metric_trend_vs_baseline_strict_boundary_for_rhr(seeded):
    # seeded's rhr baseline is mean=52.0, sd=2.0 constant; today's (most
    # recent) rhr is 50 -> current_vs_baseline_sd == (50-52)/2 == -1.0
    # exactly — the strict-band boundary (baseline_position's exactly -1.0
    # is "normal", not "suppressed").
    payload, err = call(tools.get_metric_trend, {"metric": "rhr", "days": 14})
    assert not err
    assert payload["current_vs_baseline_sd"] == -1.0
    assert payload["vs_baseline"] == "normal"


def test_get_metric_trend_vs_baseline_no_data_for_non_baselined_metric(seeded):
    payload, err = call(tools.get_metric_trend, {"metric": "steps", "days": 14})
    assert not err
    assert "current_vs_baseline_sd" not in payload
    assert payload["vs_baseline"] == "no data"


def test_get_metric_trend_single_sample_yields_null_slope_and_no_data(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    db.init_schema(p)
    today = date.today().isoformat()
    with db.connect(p) as conn:
        conn.execute("INSERT INTO daily_metrics (date, vo2_max) VALUES (?, ?)", (today, 45.0))
    payload, err = call(tools.get_metric_trend, {"metric": "vo2_max", "days": 5})
    assert not err
    assert payload["n_samples"] == 1
    assert payload["slope_per_day"] is None  # not the guarded 0.0
    assert payload["slope_direction"] == "no data"


def test_get_metric_trend_dp_budget(seeded):
    payload, err = call(tools.get_metric_trend, {"metric": "rhr", "days": 14})
    assert not err
    assert _decimal_places(payload["mean"]) <= 2
    assert _decimal_places(payload["slope_per_day"]) <= 3
    assert _decimal_places(payload["baseline_60day_mean"]) <= 2
    assert _decimal_places(payload["baseline_60day_sd"]) <= 2
    assert _decimal_places(payload["current_vs_baseline_sd"]) <= 2


def test_compare_periods_effect_size_fields(seeded):
    today = date.today()
    payload, err = call(
        tools.compare_periods,
        {"metric": "rhr",
         "period_a_start": (today - timedelta(days=10)).isoformat(),
         "period_a_end": today.isoformat(),
         "period_b_start": (today - timedelta(days=30)).isoformat(),
         "period_b_end": (today - timedelta(days=20)).isoformat()},
    )
    assert not err
    assert "delta_pct" in payload
    assert "cohens_d" in payload
    assert "magnitude" in payload
    assert _decimal_places(payload["period_a"]["mean"]) <= 2
    assert _decimal_places(payload["period_a"]["sd"]) <= 2
    assert _decimal_places(payload["delta_mean_a_minus_b"]) <= 2
    if payload["delta_pct"] is not None:
        assert _decimal_places(payload["delta_pct"]) <= 1
    if payload["cohens_d"] is not None:
        assert _decimal_places(payload["cohens_d"]) <= 3


def test_compare_periods_two_one_day_periods_degrades_per_field(seeded):
    # The designated realistic case for effect_size's per-field degradation:
    # each period has n=1 (sd forced to 0 via max(len-1, 1)) -> delta_pct is
    # still computed from the means; cohens_d/magnitude are None.
    today = date.today()
    a_day = today.isoformat()
    b_day = (today - timedelta(days=20)).isoformat()
    payload, err = call(
        tools.compare_periods,
        {"metric": "rhr", "period_a_start": a_day, "period_a_end": a_day,
         "period_b_start": b_day, "period_b_end": b_day},
    )
    assert not err
    assert payload["period_a"]["n"] == 1
    assert payload["period_b"]["n"] == 1
    assert payload["delta_pct"] is not None
    assert payload["cohens_d"] is None
    assert payload["magnitude"] is None


def test_correlate_has_computed_fields_no_legend(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    db.init_schema(p)
    today = date.today()
    with db.connect(p) as conn:
        for i in range(10):
            d = (today - timedelta(days=i)).isoformat()
            # steps and rhr move in perfect lockstep -> pearson_r == 1.0
            conn.execute(
                "INSERT INTO daily_metrics (date, steps, rhr) VALUES (?, ?, ?)",
                (d, 1000 * i, 50 + i),
            )
    payload, err = call(tools.correlate, {"metric_a": "steps", "metric_b": "rhr", "days": 10})
    assert not err
    assert payload["pearson_r"] == 1.0
    assert payload["strength"] == "strong"
    assert payload["direction"] == "positive"
    assert "interpretation" not in payload


def test_correlate_pearson_r_none_when_zero_variance_skips_rounding(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    db.init_schema(p)
    today = date.today()
    with db.connect(p) as conn:
        for i in range(10):
            d = (today - timedelta(days=i)).isoformat()
            conn.execute(  # steps constant -> zero variance -> denom == 0
                "INSERT INTO daily_metrics (date, steps, rhr) VALUES (?, ?, ?)",
                (d, 5000, 50 + i),
            )
    payload, err = call(tools.correlate, {"metric_a": "steps", "metric_b": "rhr", "days": 10})
    assert not err
    assert payload["pearson_r"] is None  # round(None, 3) must not raise
    assert payload["strength"] is None
    assert payload["direction"] is None


def test_find_anomalies_sd_distance_and_direction(seeded):
    payload, err = call(tools.find_anomalies, {"metric": "rhr", "sd_threshold": 0.1})
    assert not err
    assert payload["anomalies"]
    for row in payload["anomalies"]:
        expected = round((row["value"] - row["baseline_mean"]) / row["baseline_sd"], 2)
        assert row["sd_distance"] == expected
        assert row["direction"] in ("above", "below")


def test_training_load_status_tsb_zone_matches_interpret(seeded):
    payload, err = call(tools.training_load_status, {})
    assert not err
    assert payload["tsb_zone"] == interpret.tsb_zone(payload["current"]["tsb"])


def test_training_load_status_ctl_direction_matches_delta_direction(seeded):
    payload, err = call(tools.training_load_status, {})
    assert not err
    assert "ctl_pct_change_14d" in payload
    assert payload["ctl_direction"] == interpret.delta_direction(payload["ctl_pct_change_14d"])


def test_training_load_status_dp_budget(seeded):
    payload, err = call(tools.training_load_status, {})
    assert not err
    for field in ("ctl", "atl", "tsb"):
        assert _decimal_places(payload["current"][field]) <= 2
    if payload["ctl_pct_change_14d"] is not None:
        assert _decimal_places(payload["ctl_pct_change_14d"]) <= 1


def test_training_load_status_ctl_pct_change_matches_brief_on_gappy_baselines(tmp_path, monkeypatch):
    """Gappy-baselines agreement test (WS1): no baselines row exactly at
    today - 14d, so a window-derived "then" point would differ from the
    at-or-before lookup — both paths must call the shared
    brief_planner.ctl_at_or_before and therefore agree."""
    from local_fitness.agent import brief_planner as bp

    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    db.init_schema(p)
    today = date.today()
    with db.connect(p) as conn:
        for i in range(0, 30, 3):  # gappy: 0, 3, 6, 9, 12, 15, ... — never 14
            d = (today - timedelta(days=i)).isoformat()
            ctl = 40.0 - i * 0.1
            conn.execute(
                "INSERT INTO baselines (date, ctl, atl, tsb) VALUES (?, ?, ?, ?)",
                (d, ctl, 20.0, ctl - 20.0),
            )
    payload, err = call(tools.training_load_status, {})
    assert not err

    with db.connect(p) as conn:
        baseline = bp.status_mod._baseline_row(conn, today.isoformat())
        sig = bp._compute_signals(conn, today.isoformat(), baseline, 10000, None, None)
    assert payload["ctl_pct_change_14d"] == sig.ctl_pct_change_14d


def test_recovery_pattern_rounds_avg_recovery_days(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    db.init_schema(p)
    today = date.today()
    with db.connect(p) as conn:
        # Three matched workouts recovering to baseline in 1, 2, and 4 days
        # respectively -> avg = 7/3 = 2.3333... rounds to 2.33.
        for idx, offset in enumerate((1, 2, 4)):
            wdate = today - timedelta(days=30 - idx * 8)
            conn.execute(
                "INSERT INTO activities (activity_id, date, start_time, activity_type, "
                "distance_meters, training_load) VALUES (?, ?, ?, 'running', 8000, 50.0)",
                (idx + 1, wdate.isoformat(), wdate.isoformat() + "T07:00:00"),
            )
            conn.execute(
                "INSERT INTO baselines (date, body_battery_max_60day_mean) VALUES (?, 80.0)",
                (wdate.isoformat(),),
            )
            recovered_date = wdate + timedelta(days=offset)
            conn.execute(
                "INSERT INTO daily_metrics (date, body_battery_max) VALUES (?, 80.0)",
                (recovered_date.isoformat(),),
            )
    payload, err = call(tools.recovery_pattern, {"activity_type": "running", "lookback_days": 60})
    assert not err
    assert payload["avg_recovery_days_body_battery"] == pytest.approx(2.33)
    assert _decimal_places(payload["avg_recovery_days_body_battery"]) <= 2


# === WS2 2f — remaining miles-convention gaps ===============================
# (docs/plans/2026-07-12-deterministic-intelligence-and-ux-design.md, WS2 2f)

def test_get_workout_detail_splits_carry_mile_pace_fields(seeded):
    payload, err = call(tools.get_workout_detail, {"activity_id": 1})
    assert not err
    split = payload["splits"][0]
    # seeded's split: distance_meters=1000, duration_seconds=360, no pace.
    assert split["distance_mi"] == 0.62
    assert split["duration_formatted"] == "6:00"


def test_get_workout_detail_splits_omit_distance_mi_in_km_mode(seeded, monkeypatch):
    monkeypatch.setenv("LOCAL_FITNESS_DISPLAY_UNITS", "km")
    payload, err = call(tools.get_workout_detail, {"activity_id": 1})
    assert not err
    split = payload["splits"][0]
    assert "distance_mi" not in split
    assert split["duration_formatted"] == "6:00"  # unconditional, not units-gated


def test_recovery_pattern_matched_workouts_carry_mile_pace_fields(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    db.init_schema(p)
    today = date.today()
    wdate = today - timedelta(days=10)
    with db.connect(p) as conn:
        conn.execute(
            "INSERT INTO activities (activity_id, date, start_time, activity_type, "
            "distance_meters, training_load, avg_pace_sec_per_km, duration_seconds) "
            "VALUES (1, ?, ?, 'running', 8000, 50.0, 300.0, 2400)",
            (wdate.isoformat(), wdate.isoformat() + "T07:00:00"),
        )
        conn.execute(
            "INSERT INTO baselines (date, body_battery_max_60day_mean) VALUES (?, 80.0)",
            (wdate.isoformat(),),
        )
    payload, err = call(tools.recovery_pattern, {"activity_type": "running", "lookback_days": 30})
    assert not err
    assert payload["recent_workouts"]
    w = payload["recent_workouts"][0]
    assert w["distance_mi"] == 4.97
    assert w["pace_min_per_mi"] == "8:03"
    assert w["duration_formatted"] == "40:00"


def test_recovery_pattern_matched_workouts_omit_distance_mi_in_km_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_FITNESS_DISPLAY_UNITS", "km")
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    db.init_schema(p)
    today = date.today()
    wdate = today - timedelta(days=10)
    with db.connect(p) as conn:
        conn.execute(
            "INSERT INTO activities (activity_id, date, start_time, activity_type, "
            "distance_meters, training_load, avg_pace_sec_per_km, duration_seconds) "
            "VALUES (1, ?, ?, 'running', 8000, 50.0, 300.0, 2400)",
            (wdate.isoformat(), wdate.isoformat() + "T07:00:00"),
        )
        conn.execute(
            "INSERT INTO baselines (date, body_battery_max_60day_mean) VALUES (?, 80.0)",
            (wdate.isoformat(),),
        )
    payload, err = call(tools.recovery_pattern, {"activity_type": "running", "lookback_days": 30})
    assert not err
    w = payload["recent_workouts"][0]
    assert "distance_mi" not in w
    assert w["pace_min_per_mi"] == "8:03"  # unconditional, not units-gated


# === WS2 2g — compare_periods distance_meters SUM branch ====================
# (docs/plans/2026-07-12-deterministic-intelligence-and-ux-design.md, WS2 2g)

@pytest.fixture
def distance_seeded(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    db.init_schema(p)
    today = date.today()
    with db.connect(p) as conn:
        # period A (last 5 days): two runs, 5000m + 3000m = 8000m total.
        conn.execute(
            "INSERT INTO activities (activity_id, date, activity_type, distance_meters) "
            "VALUES (1, ?, 'running', 5000.0)", (today.isoformat(),),
        )
        conn.execute(
            "INSERT INTO activities (activity_id, date, activity_type, distance_meters) "
            "VALUES (2, ?, 'running', 3000.0)",
            ((today - timedelta(days=2)).isoformat(),),
        )
        # period B (30-40 days back): one run, 4000m total.
        conn.execute(
            "INSERT INTO activities (activity_id, date, activity_type, distance_meters) "
            "VALUES (3, ?, 'running', 4000.0)",
            ((today - timedelta(days=35)).isoformat(),),
        )
    return p


def test_compare_periods_distance_meters_sum_shape(distance_seeded):
    today = date.today()
    payload, err = call(
        tools.compare_periods,
        {"metric": "distance_meters",
         "period_a_start": (today - timedelta(days=5)).isoformat(),
         "period_a_end": today.isoformat(),
         "period_b_start": (today - timedelta(days=40)).isoformat(),
         "period_b_end": (today - timedelta(days=30)).isoformat()},
    )
    assert not err
    assert payload["period_a"] == {"n": 2, "total": 8000.0, "total_mi": 4.97}
    assert payload["period_b"] == {"n": 1, "total": 4000.0, "total_mi": 2.49}
    assert payload["delta"] == 4000.0  # a-minus-b
    assert payload["delta_pct"] == 100.0
    assert "mean" not in payload["period_a"] and "sd" not in payload["period_a"]
    assert "cohens_d" not in payload and "magnitude" not in payload


def test_compare_periods_distance_meters_omits_total_mi_in_km_mode(distance_seeded, monkeypatch):
    monkeypatch.setenv("LOCAL_FITNESS_DISPLAY_UNITS", "km")
    today = date.today()
    payload, err = call(
        tools.compare_periods,
        {"metric": "distance_meters",
         "period_a_start": (today - timedelta(days=5)).isoformat(),
         "period_a_end": today.isoformat(),
         "period_b_start": (today - timedelta(days=40)).isoformat(),
         "period_b_end": (today - timedelta(days=30)).isoformat()},
    )
    assert not err
    assert "total_mi" not in payload["period_a"]
    assert "total_mi" not in payload["period_b"]
    assert payload["period_a"]["total"] == 8000.0


def test_compare_periods_distance_meters_empty_period_yields_none_total(distance_seeded):
    today = date.today()
    payload, err = call(
        tools.compare_periods,
        {"metric": "distance_meters",
         "period_a_start": "2020-01-01", "period_a_end": "2020-01-02",  # nothing here
         "period_b_start": (today - timedelta(days=40)).isoformat(),
         "period_b_end": (today - timedelta(days=30)).isoformat()},
    )
    assert not err
    assert payload["period_a"] == {"n": 0, "total": None}
    assert payload["delta"] is None
    assert payload["delta_pct"] is None


def test_run_sql_select(seeded):
    payload, err = call(tools.run_sql, {"query": "SELECT COUNT(*) AS c FROM daily_metrics"})
    assert not err
    assert payload["count"] == 1


def test_run_sql_rejects_non_select(seeded):
    _payload, err = call(tools.run_sql, {"query": "DELETE FROM daily_metrics"})
    assert err


def test_run_sql_rejects_forbidden_keyword(seeded):
    _payload, err = call(tools.run_sql, {"query": "WITH x AS (SELECT 1) UPDATE settings SET value='x'"})
    assert err


def test_run_sql_bad_query(seeded):
    _payload, err = call(tools.run_sql, {"query": "SELECT * FROM does_not_exist"})
    assert err


def test_run_sql_bad_table_points_at_schema_resource(seeded):
    # Regression: a mistyped table/column raises sqlite3.OperationalError —
    # the schema-resource pointer must fire on that REAL path, not only on
    # the exotic sqlite3.Error branch (Phase-5 live gate caught the pointer
    # living solely in the unreachable branch).
    payload, err = call(tools.run_sql, {"query": "SELECT * FROM does_not_exist"})
    assert err
    assert "fitness://schema" in payload["error"]
    assert "operational error" not in payload["error"]


# --- day-window robustness: over-large N must be a clean _err, not OverflowError ---

_BIG = 10**9  # timedelta(days=N) raises OverflowError around here


def test_get_metric_rejects_huge_days(seeded):
    payload, err = call(tools.get_metric, {"metric": "rhr", "days": _BIG})
    assert err
    assert "days must be between" in payload["error"]


def test_get_metric_trend_rejects_huge_days(seeded):
    payload, err = call(tools.get_metric_trend, {"metric": "rhr", "days": _BIG})
    assert err
    assert "days must be between" in payload["error"]


def test_get_metric_trend_rejects_single_point_window(seeded):
    # days:0/1 yields a degenerate single-sample trend; lo=2 rejects it cleanly.
    for bad in (0, 1):
        payload, err = call(tools.get_metric_trend, {"metric": "rhr", "days": bad})
        assert err
        assert "days must be between" in payload["error"]


def test_query_workouts_rejects_huge_days(seeded):
    payload, err = call(tools.query_workouts, {"days": _BIG})
    assert err
    assert "days must be between" in payload["error"]


def test_find_anomalies_rejects_huge_lookback(seeded):
    payload, err = call(tools.find_anomalies, {"metric": "rhr", "lookback_days": _BIG})
    assert err
    assert "lookback_days must be between" in payload["error"]


def test_recovery_pattern_rejects_huge_lookback(seeded):
    payload, err = call(tools.recovery_pattern, {"lookback_days": _BIG})
    assert err
    assert "lookback_days must be between" in payload["error"]


def test_correlate_rejects_huge_days(seeded):
    payload, err = call(
        tools.correlate,
        {"metric_a": "sleep_seconds", "metric_b": "rhr", "days": _BIG},
    )
    assert err
    assert "days must be between" in payload["error"]


def test_correlate_rejects_huge_lag(seeded):
    payload, err = call(
        tools.correlate,
        {"metric_a": "sleep_seconds", "metric_b": "rhr", "days": 30, "lag_days": _BIG},
    )
    assert err
    assert "lag_days must be between" in payload["error"]


def test_correlate_allows_negative_lag(seeded):
    # A small negative lag is legitimate (sign flips which metric leads) and
    # must not be rejected by the bounds check.
    _payload, err = call(
        tools.correlate,
        {"metric_a": "sleep_seconds", "metric_b": "rhr", "days": 30, "lag_days": -1},
    )
    assert not err


# --- notes tools (use LOCAL_FITNESS_NOTES_PATH from the fixture) ---

def test_save_and_list_user_notes(seeded):
    saved, err = call(tools.save_user_note, {"note": "lead with the workout card"})
    assert not err and saved["saved"]
    listed, err = call(tools.list_user_notes, {})
    assert not err
    assert listed["count"] == 1
    assert listed["notes"][0]["text"] == "lead with the workout card"


def test_save_user_note_empty(seeded):
    _payload, err = call(tools.save_user_note, {"note": "   "})
    assert err


def test_update_user_note(seeded):
    call(tools.save_user_note, {"note": "old"})
    updated, err = call(tools.update_user_note, {"line": 0, "note": "new"})
    assert not err
    assert updated["text"] == "new"


def test_update_user_note_bad_line(seeded):
    _payload, err = call(tools.update_user_note, {"line": None, "note": "x"})
    assert err
    _payload, err = call(tools.update_user_note, {"line": 0, "note": ""})
    assert err
    _payload, err = call(tools.update_user_note, {"line": 99, "note": "x"})
    assert err  # no note at that line


def test_delete_user_note(seeded):
    call(tools.save_user_note, {"note": "drop me"})
    deleted, err = call(tools.delete_user_note, {"line": 0})
    assert not err and deleted["deleted"]


def test_delete_user_note_bad_line(seeded):
    _payload, err = call(tools.delete_user_note, {"line": None})
    assert err
    _payload, err = call(tools.delete_user_note, {"line": 42})
    assert err


def test_server_and_tool_names():
    server = tools.make_server()
    assert server is not None
    names = tools.allowed_tool_names()
    assert len(names) == len(tools.ALL_TOOLS)
    assert all(n.startswith("mcp__fitness__") for n in names)


# --- W4-T2: observation + manual-workout round-trip -----------------------

def _obs_rows(db_path):
    with db.connect(db_path) as conn:
        return conn.execute(
            "SELECT * FROM observations ORDER BY observation_id"
        ).fetchall()


def _activity_rows(db_path):
    with db.connect(db_path) as conn:
        return conn.execute(
            "SELECT * FROM activities ORDER BY activity_id"
        ).fetchall()


def test_log_observation_numeric_and_text_roundtrip(seeded):
    saved, err = call(tools.log_observation, {"obs_type": "weight", "value": 165})
    assert not err and saved["logged"]
    assert saved["observation"]["value_num"] == 165
    assert saved["observation"]["value_text"] is None

    saved2, err = call(tools.log_observation, {"obs_type": "note", "text": "felt flat"})
    assert not err and saved2["logged"]
    assert saved2["observation"]["value_text"] == "felt flat"
    assert saved2["observation"]["value_num"] is None

    listed, err = call(tools.list_observations, {})
    assert not err
    assert listed["count"] == 2
    texts = {o["obs_type"] for o in listed["observations"]}
    assert texts == {"weight", "note"}


def test_list_observations_rejects_huge_days(seeded):
    # Same finding class as the date-window analysis tools: a huge `days` must
    # be a clean _err, not a raw OverflowError out of timedelta().
    payload, err = call(tools.list_observations, {"days": _BIG})
    assert err
    assert "days must be between" in payload["error"]


def test_log_observation_invalid_obs_type(seeded):
    _payload, err = call(tools.log_observation, {"obs_type": "bogus", "value": 1})
    assert err
    assert not _obs_rows(seeded)  # nothing inserted


def test_log_observation_numeric_missing_value(seeded):
    _payload, err = call(tools.log_observation, {"obs_type": "weight"})
    assert err
    assert not _obs_rows(seeded)


def test_log_observation_text_missing_text(seeded):
    _payload, err = call(tools.log_observation, {"obs_type": "note"})
    assert err
    _payload, err = call(tools.log_observation, {"obs_type": "note", "text": "   "})
    assert err
    assert not _obs_rows(seeded)  # no empty rows


def test_log_observation_bad_activity_id(seeded):
    # Non-null activity_id that doesn't exist → _err, nothing inserted.
    _payload, err = call(
        tools.log_observation, {"obs_type": "rpe", "value": 8, "activity_id": 999999}
    )
    assert err
    assert not _obs_rows(seeded)


def test_log_observation_malformed_date(seeded):
    # A malformed observed_on must be rejected before any write — mirrors
    # log_manual_workout's guard so bad dates never poison the sort order.
    _payload, err = call(
        tools.log_observation,
        {"obs_type": "weight", "value": 165, "date": "not-a-date"},
    )
    assert err
    assert "invalid date" in _payload["error"]
    assert not _obs_rows(seeded)  # nothing inserted


def test_log_observation_rejects_future_date(seeded):
    # A future-dated observation is silently excluded from the days-filtered
    # list_observations lookback, so reject it before any write.
    future = (date.today() + timedelta(days=3)).isoformat()
    _payload, err = call(
        tools.log_observation,
        {"obs_type": "weight", "value": 165, "date": future},
    )
    assert err
    assert "future" in _payload["error"]
    assert not _obs_rows(seeded)


def test_log_observation_valid_activity_id(seeded):
    # activity_id 1 exists in the seeded fixture.
    saved, err = call(
        tools.log_observation, {"obs_type": "rpe", "value": 8, "activity_id": 1}
    )
    assert not err and saved["logged"]
    assert saved["observation"]["activity_id"] == 1


def test_delete_observation_absent_and_present(seeded):
    _payload, err = call(tools.delete_observation, {"observation_id": 4242})
    assert err  # absent id

    saved, _ = call(tools.log_observation, {"obs_type": "mood", "value": 7})
    obs_id = saved["observation"]["observation_id"]
    deleted, err = call(tools.delete_observation, {"observation_id": obs_id})
    assert not err and deleted["deleted"]
    assert not _obs_rows(seeded)


def test_log_manual_workout_negative_ids_and_source(seeded):
    today = date.today().isoformat()
    first, err = call(
        tools.log_manual_workout, {"activity_type": "strength", "duration_min": 45}
    )
    assert not err and first["logged"]
    assert first["activity"]["activity_id"] == -1
    assert first["activity"]["source"] == "manual"
    assert first["activity"]["date"] == today  # date defaults to today

    second, err = call(
        tools.log_manual_workout, {"activity_type": "yoga", "duration_min": 30}
    )
    assert not err
    assert second["activity"]["activity_id"] == -2


def test_log_manual_workout_malformed_date(seeded):
    before = len(_activity_rows(seeded))
    _payload, err = call(
        tools.log_manual_workout,
        {"activity_type": "strength", "duration_min": 45, "date": "nope"},
    )
    assert err
    assert len(_activity_rows(seeded)) == before  # no activities row written


def test_log_manual_workout_rejects_nonpositive_duration(seeded):
    before = len(_activity_rows(seeded))
    for bad in (0, -15):
        _payload, err = call(
            tools.log_manual_workout,
            {"activity_type": "strength", "duration_min": bad},
        )
        assert err
        assert "duration_min must be positive" in _payload["error"]
    assert len(_activity_rows(seeded)) == before  # no activities row written


def test_log_manual_workout_rejects_future_date(seeded):
    before = len(_activity_rows(seeded))
    future = (date.today() + timedelta(days=3)).isoformat()
    _payload, err = call(
        tools.log_manual_workout,
        {"activity_type": "strength", "duration_min": 45, "date": future},
    )
    assert err
    assert "future" in _payload["error"]
    assert len(_activity_rows(seeded)) == before  # no activities row written


def test_log_manual_workout_recompute_failure_persists_row(seeded, monkeypatch):
    """If recompute() raises AFTER the row commits, the tool must NOT re-raise:
    it returns logged=True/recompute_failed=True so the caller knows the row
    landed and does NOT retry (which would duplicate the workout)."""
    from local_fitness.ingest import baselines

    before = len(_activity_rows(seeded))

    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(baselines, "recompute", boom)

    payload, err = call(
        tools.log_manual_workout,
        {"activity_type": "strength", "duration_min": 45},
    )
    # Partial-success: not an error, row persisted, recompute flagged failed.
    assert not err
    assert payload["logged"] is True
    assert payload["recompute_failed"] is True
    assert "recompute failed" in payload["warning"]
    assert "database is locked" in payload["error_detail"]
    # Exactly one new row — no duplicate, and it really persisted.
    assert len(_activity_rows(seeded)) == before + 1


def test_delete_manual_workout_recompute_failure_reports_deleted(seeded, monkeypatch):
    from local_fitness.ingest import baselines

    saved, err = call(
        tools.log_manual_workout, {"activity_type": "strength", "duration_min": 45}
    )
    assert not err
    aid = saved["activity"]["activity_id"]

    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(baselines, "recompute", boom)

    payload, err = call(tools.delete_manual_workout, {"activity_id": aid})
    assert not err
    assert payload["deleted"] is True
    assert payload["recompute_failed"] is True
    # The row really is gone despite the recompute failure.
    with db.connect(seeded) as conn:
        row = conn.execute(
            "SELECT * FROM activities WHERE activity_id = ?", (aid,)
        ).fetchone()
    assert row is None


def test_delete_manual_workout_guardrails(seeded):
    # Refuses non-negative ids (Garmin data protection).
    _payload, err = call(tools.delete_manual_workout, {"activity_id": 1})
    assert err
    _payload, err = call(tools.delete_manual_workout, {"activity_id": 0})
    assert err
    # Absent negative id → _err.
    _payload, err = call(tools.delete_manual_workout, {"activity_id": -99})
    assert err


def test_delete_manual_workout_detaches_observation(seeded):
    saved, err = call(
        tools.log_manual_workout, {"activity_type": "strength", "duration_min": 45}
    )
    assert not err
    aid = saved["activity"]["activity_id"]
    assert aid == -1

    obs, err = call(
        tools.log_observation, {"obs_type": "soreness", "value": 3, "activity_id": aid}
    )
    assert not err
    obs_id = obs["observation"]["observation_id"]

    deleted, err = call(tools.delete_manual_workout, {"activity_id": aid})
    assert not err and deleted["deleted"]

    with db.connect(seeded) as conn:
        row = conn.execute(
            "SELECT * FROM observations WHERE observation_id = ?", (obs_id,)
        ).fetchone()
    assert row is not None  # observation still exists
    assert row["activity_id"] is None  # ...but its activity_id is NULLed


# --- W4-T3: recompute integration -----------------------------------------

def _baseline_for(db_path, d):
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT ctl, atl, tsb FROM baselines WHERE date = ?", (d,)
        ).fetchone()
    return dict(row) if row else None


def test_manual_workout_recompute_reflects_load(seeded):
    today = date.today().isoformat()
    before = _baseline_for(seeded, today)
    assert before is not None  # fixture seeded a baselines row for today

    saved, err = call(
        tools.log_manual_workout,
        {"activity_type": "strength", "duration_min": 60, "training_load": 120},
    )
    assert not err

    after = _baseline_for(seeded, today)
    assert after is not None
    assert after["ctl"] is not None and after["atl"] is not None and after["tsb"] is not None
    # The fixture wrote a fixed (ctl=40, atl=45) row; recompute overwrote it
    # from real activity training_load, so the values must have changed.
    assert (after["ctl"], after["atl"], after["tsb"]) != (
        before["ctl"], before["atl"], before["tsb"]
    )
    assert after["ctl"] > 0


def test_backdated_manual_workout_rewrites_own_date(seeded):
    from local_fitness.ingest import baselines

    backdate = (date.today() - timedelta(days=baselines.RECOMPUTE_LOOKBACK_DAYS + 10)).isoformat()
    saved, err = call(
        tools.log_manual_workout,
        {
            "activity_type": "cycling",
            "duration_min": 90,
            "date": backdate,
            "training_load": 150,
        },
    )
    assert not err
    # The widened lookback must have written a baselines row for the backdated
    # date, with the load reflected (CTL nonzero on/after that date).
    row = _baseline_for(seeded, backdate)
    assert row is not None
    assert row["ctl"] is not None and row["ctl"] > 0


def test_garmin_reingest_leaves_manual_row_untouched(seeded):
    saved, err = call(
        tools.log_manual_workout, {"activity_type": "strength", "duration_min": 45}
    )
    assert not err
    manual_id = saved["activity"]["activity_id"]
    assert manual_id == -1

    # Simulate a Garmin re-ingest: INSERT OR REPLACE a positive activity_id.
    today = date.today().isoformat()
    with db.connect(seeded) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO activities "
            "(activity_id, date, activity_type, activity_name, duration_seconds, "
            "training_load, source) VALUES (2, ?, 'running', 'Re-ingest Run', 3600, 90.0, 'garmin')",
            (today,),
        )

    with db.connect(seeded) as conn:
        manual = conn.execute(
            "SELECT * FROM activities WHERE activity_id = ?", (manual_id,)
        ).fetchone()
    assert manual is not None  # negative-id manual row survives the upsert
    assert manual["source"] == "manual"


# --- save_brief tool -------------------------------------------------------


def _valid_takeaway():
    return {
        "headline": "Easy 5k on tap",
        "summary": "RHR steady, TSB positive — green light to run.",
        "tone": "positive",
        "details": "Full markdown deep-dive.",
    }


@pytest.fixture
def briefs_tmp(tmp_path, monkeypatch):
    """Point the briefs gate + DB at a tmp dir so save_brief never touches
    the real briefings/ or dev DB."""
    from local_fitness.agent import briefs

    out = tmp_path / "briefings"
    monkeypatch.setattr(briefs, "DEFAULT_BRIEFINGS_DIR", out)
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    return out


def test_save_brief_tool_valid_writes_file_and_no_brief_key(briefs_tmp):
    payload, err = call(tools.save_brief, {"brief": {"takeaways": [_valid_takeaway()]}})
    assert not err
    today = date.today().isoformat()
    # The tool returns ONLY scalars — the pydantic Brief object is dropped so
    # json.dumps can't raise (and no model leaks across the wire).
    assert set(payload.keys()) == {"saved", "date", "path"}
    assert "brief" not in payload
    assert payload["saved"] is True
    assert payload["date"] == today
    # A file really landed for the valid case.
    assert (briefs_tmp / f"{today}.json").exists()
    assert payload["path"] == str(briefs_tmp / f"{today}.json")


def test_save_brief_tool_invalid_is_error(briefs_tmp):
    # Empty takeaways → schema validation failure → is_error with a message.
    payload, err = call(tools.save_brief, {"brief": {"takeaways": []}})
    assert err
    assert "validation" in payload["error"].lower()
    # Nothing written on rejection.
    assert list(briefs_tmp.glob("*.json")) == []


def test_save_brief_advertises_the_full_takeaway_schema():
    """The tool's inputSchema must expose the real Brief/Takeaway shape — the
    tone enum and the {metric, days} sub-object — so a client (Claude Desktop, a
    phone with no filesystem) can build a valid brief from the contract alone,
    instead of grepping schemas.py the way the agent had to on 2026-07-22."""
    s = tools._SAVE_BRIEF_INPUT_SCHEMA
    # A full JSON Schema, not the opaque {"brief": dict} shorthand.
    assert s["type"] == "object"
    assert set(s["properties"]) == {"brief"}
    brief = s["properties"]["brief"]
    # Only takeaways is required of the CALLER — date/user_name/generated_at are
    # stamped server-side by briefs.save_brief and must not be demanded here.
    assert brief["required"] == ["takeaways"]
    # The nested takeaway shape is reachable via hoisted $defs (so its $ref
    # resolves at the schema root).
    props = s["$defs"]["Takeaway"]["properties"]
    assert {"headline", "summary", "tone"} <= set(props)
    assert set(props["tone"]["enum"]) == {"positive", "caution", "critical", "neutral"}
    assert "TakeawayMetric" in s["$defs"]


def test_save_brief_schema_meets_the_sdk_passthrough_condition():
    """The Agent SDK forwards a dict schema verbatim ONLY when it has a
    top-level string `type` plus `properties`; otherwise it reinterprets the
    dict as a {name: python-type} shorthand and would silently discard our
    Brief schema. Guard the pass-through condition, and that the registered
    tool actually carries this exact schema object."""
    s = tools._SAVE_BRIEF_INPUT_SCHEMA
    assert isinstance(s.get("type"), str) and "properties" in s
    assert tools.save_brief.input_schema is s


def test_brief_loop_excludes_write_tools():
    """Contract invariant: the brief loop's allow-list (read_only_tool_names)
    is a strict subset of all tools and never includes a write or the
    snapshot/list-observations tools, so brief generation cannot mutate data."""
    ro = set(tools.read_only_tool_names())
    for w in (
        "log_manual_workout", "delete_manual_workout", "log_observation",
        "delete_observation", "save_user_note", "update_user_note",
        "delete_user_note", "daily_snapshot", "list_observations",
        "sync_garmin_data",
    ):
        assert f"mcp__{tools.SERVER_NAME}__{w}" not in ro
    assert ro < set(tools.allowed_tool_names())


# --- sync_garmin_data --------------------------------------------------------

def test_sync_garmin_data_success_recomputes_baselines(monkeypatch):
    calls = {}

    def fake_pull(*, max_days):
        calls["max_days"] = max_days
        return {
            "status": "success", "days_pulled": 2, "activities_loaded": 1,
            "last_date": "2026-07-06", "error": None,
        }

    def fake_recompute(*, lookback_days):
        calls["lookback_days"] = lookback_days
        return 90

    monkeypatch.setattr(tools.daily_ingest, "pull", fake_pull)
    monkeypatch.setattr(tools.baselines_mod, "recompute", fake_recompute)

    payload, err = call(tools.sync_garmin_data, {})
    assert not err
    assert payload["status"] == "success"
    assert payload["days_pulled"] == 2
    # Bite-sized cap wired through, and baselines only recomputed because new
    # days actually landed.
    assert calls["max_days"] == tools.SYNC_MAX_DAYS
    assert calls["lookback_days"] == 90


def test_sync_garmin_data_skipped_does_not_recompute(monkeypatch):
    monkeypatch.setattr(
        tools.daily_ingest, "pull",
        lambda **_: {
            "status": "skipped", "days_pulled": 0, "activities_loaded": 0,
            "last_date": "2026-07-06", "error": None,
        },
    )
    monkeypatch.setattr(
        tools.baselines_mod, "recompute",
        lambda **_: pytest.fail("recompute should not run when no new days pulled"),
    )
    payload, err = call(tools.sync_garmin_data, {})
    assert not err
    assert payload["status"] == "skipped"
    assert payload["days_pulled"] == 0


def test_sync_garmin_data_partial_success_skips_recompute(monkeypatch):
    # Mirrors _run_sync in web/server.py: only a clean "success" status
    # triggers a baseline recompute, not "partial".
    monkeypatch.setattr(
        tools.daily_ingest, "pull",
        lambda **_: {
            "status": "partial", "days_pulled": 1, "activities_loaded": 0,
            "last_date": "2026-07-05", "error": "1 day(s) still missing",
        },
    )
    monkeypatch.setattr(
        tools.baselines_mod, "recompute",
        lambda **_: pytest.fail("recompute should not run on a partial pull"),
    )
    payload, err = call(tools.sync_garmin_data, {})
    assert err
    assert payload["status"] == "partial"
    assert "still missing" in payload["error"]


def test_sync_garmin_data_auth_failure_is_error(monkeypatch):
    monkeypatch.setattr(
        tools.daily_ingest, "pull",
        lambda **_: {
            "status": "auth_failure", "days_pulled": 0, "activities_loaded": 0,
            "last_date": None, "error": "mfa_required: verification needed",
        },
    )
    monkeypatch.setattr(
        tools.baselines_mod, "recompute",
        lambda **_: pytest.fail("recompute should not run on auth failure"),
    )
    payload, err = call(tools.sync_garmin_data, {})
    assert err
    assert payload["status"] == "auth_failure"
    assert "mfa_required" in payload["error"]


def test_sync_garmin_data_is_in_full_tool_set():
    assert f"mcp__{tools.SERVER_NAME}__sync_garmin_data" in tools.allowed_tool_names()


# --- LOCAL_ONLY_TOOLS: generate_brief_report / generate_chart --------------


def test_fetch_metric_series_matches_chart_tool_output(seeded):
    # Regression guard for the chart()/generate_chart() extraction: the shared
    # helper's fetched values must be the exact same numbers chart()'s ASCII
    # rendering displays for the same (metric, days) window.
    dates, values = tools._fetch_metric_series("rhr", 14)
    assert len(dates) == len(values) > 0
    assert dates == sorted(dates)  # ascending, matching the ORDER BY date SQL

    text, err = call(tools.chart, {"metric": "rhr", "days": 14, "style": "spark"})
    assert not err
    fmt = tools._chart_value_fmt("rhr")
    assert fmt(min(values)) in text
    assert fmt(max(values)) in text


def test_chart_description_echoes_reply_rendering_rule():
    # 3b: the chart tool's description gets a one-line echo of the
    # system-prompt Charts bullet (reproduce full output in a fenced code
    # block, then the coach read).
    tool = next(t for t in tools.ALL_TOOLS if t.name == "chart")
    assert "fenced code block" in tool.description


def test_fetch_metric_series_unknown_metric_raises(seeded):
    with pytest.raises(ValueError, match="unknown metric"):
        tools._fetch_metric_series("bogus", 14)


def test_write_atomic_rejects_escaping_final_name(tmp_path):
    # INV-T4: the CLAUDE.md-mandated .resolve().relative_to() containment
    # pattern — a final_name that resolves outside reports_dir must raise
    # before write.
    reports_dir = tmp_path / "reports"
    with pytest.raises(ValueError):
        tools._write_atomic(reports_dir, "../escaped.pdf", b"data")
    # Nothing escaped: the parent of reports_dir has no new file.
    assert not (tmp_path / "escaped.pdf").exists()


def test_write_atomic_writes_final_bytes_and_no_tmp_left_behind(tmp_path):
    reports_dir = tmp_path / "reports"
    path = tools._write_atomic(reports_dir, "out.png", b"hello")
    assert path == reports_dir / "out.png"
    assert path.read_bytes() == b"hello"
    assert list(reports_dir.glob("*.tmp")) == []


def test_content_tag_is_deterministic_and_content_sensitive():
    # Same bytes -> same 8-hex tag (idempotent re-render reuses one filename);
    # different bytes -> different tag (a changed render lands on a fresh name,
    # which is what makes macOS `open` show a new window instead of refocusing
    # the stale one). Both facts are load-bearing for the stale-PDF fix.
    tag = tools._content_tag(b"the pdf bytes")
    assert re.fullmatch(r"[0-9a-f]{8}", tag)
    assert tools._content_tag(b"the pdf bytes") == tag
    assert tools._content_tag(b"the pdf bytes!") != tag
    # It is exactly the sha256 prefix, not some other hash.
    import hashlib
    assert tag == hashlib.sha256(b"the pdf bytes").hexdigest()[:8]


def test_brief_pdf_filename_is_content_addressed(seeded, reports_tmp, monkeypatch):
    # Regenerating the SAME brief content reuses one filename (idempotent), but
    # a content change moves to a NEW filename so the viewer opens fresh bytes
    # rather than refocusing a stale Preview window (the 2026-07-22 "you used
    # old data" bug). We drive the content change through the rendered PDF.
    _reports_dir, briefs_dir = reports_tmp
    d = date.today().isoformat()
    _write_brief_json(briefs_dir, d, [
        {"headline": "First", "summary": "s", "tone": "neutral", "details": "one"},
    ])
    p1, err1 = call(tools.generate_brief_report, {"date": d})
    p2, err2 = call(tools.generate_brief_report, {"date": d})
    assert not err1 and not err2
    # Identical input twice -> identical content-addressed filename.
    assert p1["path"] == p2["path"]

    # Change the brief, re-render: the filename must change (different bytes).
    _write_brief_json(briefs_dir, d, [
        {"headline": "Totally different headline now", "summary": "s2",
         "tone": "critical", "details": "two two two"},
    ])
    p3, err3 = call(tools.generate_brief_report, {"date": d})
    assert not err3
    assert p3["path"] != p1["path"]
    # Both still carry today's date and land in the reports dir.
    assert Path(p3["path"]).name.startswith(f"brief-{d}-")


@pytest.fixture(autouse=True)
def no_real_open(monkeypatch):
    """Stub subprocess.run and asyncio.sleep so generate_brief_report/
    generate_chart's auto-open never pops a real Preview window or incurs
    the real 1.5s grace-period sleep during tests. subprocess.run is a
    fresh Mock() per test -- tests that care about its call args just
    inspect tools.subprocess.run directly, no re-patching needed.

    Rebinds tools.subprocess to a stand-in module rather than mutating the
    real subprocess module's .run in place -- tools.subprocess IS the real
    stdlib subprocess module object (a plain `import subprocess`), so
    patching .run on it directly leaked into every other subprocess.run
    caller in the same test process, including matplotlib's font_manager
    (subprocess.check_output delegates to subprocess.run internally),
    which broke real chart-rendering happy-path tests on a fresh CI
    runner with no cached font list."""
    fake_subprocess = types.ModuleType("subprocess")
    fake_subprocess.__dict__.update(subprocess.__dict__)
    fake_subprocess.run = Mock()
    monkeypatch.setattr(tools, "subprocess", fake_subprocess)
    monkeypatch.setattr(tools.asyncio, "sleep", AsyncMock())


@pytest.fixture(autouse=True)
def _reset_ephemeral_dir():
    """Guarantee a clean _EPHEMERAL_DIR slate before and after every test --
    several tests below assert on first-call-vs-memoized behavior, which is
    only meaningful if no prior test left a real ephemeral dir cached."""
    tools._EPHEMERAL_DIR = None
    yield
    if tools._EPHEMERAL_DIR is not None:
        shutil.rmtree(tools._EPHEMERAL_DIR, ignore_errors=True)
    tools._EPHEMERAL_DIR = None


@pytest.fixture
def fake_tempdir(tmp_path, monkeypatch):
    """Redirect tempfile.gettempdir() to a throwaway directory under
    tmp_path, so tests exercising the real _default_reports_dir() path
    never touch the actual OS temp dir or interact with real concurrent
    fitness-mcp-stdio sessions on this machine."""
    fake_root = tmp_path / "faketmp"
    fake_root.mkdir()
    monkeypatch.setattr(tools.tempfile, "gettempdir", lambda: str(fake_root))
    return fake_root


@pytest.fixture
def reports_tmp(tmp_path, monkeypatch):
    """Point _default_reports_dir and DEFAULT_BRIEFINGS_DIR at tmp dirs so
    generate_brief_report/generate_chart tests never touch the real
    reports/ or briefings/ directories."""
    from local_fitness.agent import briefs as briefs_mod

    reports_dir = tmp_path / "reports"
    monkeypatch.setattr(tools, "_default_reports_dir", lambda: reports_dir)
    briefs_dir = tmp_path / "briefings"
    monkeypatch.setattr(briefs_mod, "DEFAULT_BRIEFINGS_DIR", briefs_dir)
    return reports_dir, briefs_dir


def test_default_reports_dir_ephemeral_when_env_unset(monkeypatch, fake_tempdir):
    monkeypatch.delenv("LOCAL_FITNESS_REPORTS_DIR", raising=False)
    register_calls = []
    monkeypatch.setattr(
        tools.atexit, "register", lambda fn, arg: register_calls.append((fn, arg))
    )
    result = tools._default_reports_dir()
    assert result.exists()
    assert result.is_dir()
    assert result.parent == fake_tempdir
    assert register_calls == [(tools._rmtree_ignore_errors, result)]


def test_default_reports_dir_memoized(monkeypatch, fake_tempdir):
    monkeypatch.delenv("LOCAL_FITNESS_REPORTS_DIR", raising=False)
    mkdtemp_spy = Mock(side_effect=tempfile.mkdtemp)
    monkeypatch.setattr(tools.tempfile, "mkdtemp", mkdtemp_spy)
    first = tools._default_reports_dir()
    second = tools._default_reports_dir()
    assert first == second
    assert mkdtemp_spy.call_count == 1


def test_default_reports_dir_honors_env_override(monkeypatch, tmp_path):
    override_dir = tmp_path / "persistent-reports"
    monkeypatch.setenv("LOCAL_FITNESS_REPORTS_DIR", str(override_dir))
    mkdtemp_spy = Mock()
    monkeypatch.setattr(tools.tempfile, "mkdtemp", mkdtemp_spy)
    register_spy = Mock()
    monkeypatch.setattr(tools.atexit, "register", register_spy)
    result = tools._default_reports_dir()
    assert result == override_dir
    mkdtemp_spy.assert_not_called()
    register_spy.assert_not_called()


def test_rmtree_ignore_errors_removes_dir(tmp_path):
    d = tmp_path / "to_remove"
    d.mkdir()
    (d / "file.txt").write_text("x")
    tools._rmtree_ignore_errors(d)
    assert not d.exists()


def test_rmtree_ignore_errors_missing_path_is_noop(tmp_path):
    d = tmp_path / "does_not_exist"
    tools._rmtree_ignore_errors(d)  # must not raise


def _fake_pid_dir(fake_tempdir, pid, age_seconds=0):
    d = fake_tempdir / f"local-fitness-reports-{pid}-abcd1234"
    d.mkdir()
    if age_seconds:
        old_time = time.time() - age_seconds
        os.utime(d, (old_time, old_time))
    return d


def test_sweep_removes_stale_and_dead_dir(monkeypatch, fake_tempdir):
    stale = _fake_pid_dir(fake_tempdir, 99999, age_seconds=25 * 60 * 60)
    monkeypatch.setattr(tools.os, "kill", Mock(side_effect=ProcessLookupError))
    monkeypatch.delenv("LOCAL_FITNESS_REPORTS_DIR", raising=False)
    tools._default_reports_dir()
    assert not stale.exists()


def test_sweep_leaves_fresh_dir_alone(monkeypatch, fake_tempdir):
    fresh = _fake_pid_dir(fake_tempdir, 99999)  # recent mtime
    monkeypatch.setattr(tools.os, "kill", Mock(side_effect=ProcessLookupError))
    monkeypatch.delenv("LOCAL_FITNESS_REPORTS_DIR", raising=False)
    tools._default_reports_dir()
    assert fresh.exists()


def test_sweep_leaves_alive_pid_dir_alone(monkeypatch, fake_tempdir):
    alive = _fake_pid_dir(fake_tempdir, 12345, age_seconds=25 * 60 * 60)
    monkeypatch.setattr(tools.os, "kill", Mock(return_value=None))  # simulates alive
    monkeypatch.delenv("LOCAL_FITNESS_REPORTS_DIR", raising=False)
    tools._default_reports_dir()
    assert alive.exists()


def test_sweep_leaves_unrelated_dir_alone(monkeypatch, fake_tempdir):
    unrelated = fake_tempdir / "some-other-apps-tmpdir"
    unrelated.mkdir()
    old_time = time.time() - 25 * 60 * 60
    os.utime(unrelated, (old_time, old_time))
    monkeypatch.delenv("LOCAL_FITNESS_REPORTS_DIR", raising=False)
    tools._default_reports_dir()
    assert unrelated.exists()


def test_reports_dir_constant_removed():
    assert not hasattr(tools, "REPORTS_DIR")


def test_default_reports_dir_concurrent_calls_create_only_one_dir(monkeypatch, fake_tempdir):
    # Pins the threading.Lock's actual regression-prevention value: unlike
    # sequential calls (test_default_reports_dir_memoized above), this uses
    # genuinely concurrent threads racing the critical section -- it would
    # fail if _EPHEMERAL_DIR_LOCK were removed or narrowed.
    monkeypatch.delenv("LOCAL_FITNESS_REPORTS_DIR", raising=False)
    mkdtemp_spy = Mock(side_effect=tempfile.mkdtemp)
    monkeypatch.setattr(tools.tempfile, "mkdtemp", mkdtemp_spy)

    n_workers = 4
    barrier = threading.Barrier(n_workers)

    def worker():
        barrier.wait()
        return tools._default_reports_dir()

    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        results = [f.result() for f in [executor.submit(worker) for _ in range(n_workers)]]

    assert len(set(results)) == 1
    assert mkdtemp_spy.call_count == 1


def _write_brief_json(briefs_dir, d, takeaways):
    briefs_dir.mkdir(parents=True, exist_ok=True)
    payload = {"date": d, "user_name": "Nate", "takeaways": takeaways}
    (briefs_dir / f"{d}.json").write_text(json.dumps(payload), encoding="utf-8")


class _NoTouch:
    """Sentinel standing in for DEFAULT_BRIEFINGS_DIR: raises if ANY path is
    built from it, proving a malformed date is rejected before file I/O."""

    def __truediv__(self, other):
        raise AssertionError("must not touch briefings dir on malformed date")


def test_generate_brief_report_malformed_date_no_file_io(monkeypatch):
    # INV-T2: malformed date rejected before any file I/O is attempted.
    from local_fitness.agent import briefs as briefs_mod

    monkeypatch.setattr(briefs_mod, "DEFAULT_BRIEFINGS_DIR", _NoTouch())
    for bad in ("2026/07/08", "../../../etc/passwd", "20260708", "2026-7-8", ""):
        payload, err = call(tools.generate_brief_report, {"date": bad})
        assert err
        assert "malformed date" in payload["error"]
    assert not tools.subprocess.run.called


def test_generate_brief_report_missing_brief_is_error(reports_tmp):
    # INV-T1: well-formed date, but no saved brief for it -> clean error.
    payload, err = call(tools.generate_brief_report, {"date": "2026-07-08"})
    assert err
    assert "no saved brief" in payload["error"]
    assert not tools.subprocess.run.called


def test_generate_brief_report_invalid_brief_schema_is_error(reports_tmp):
    _reports_dir, briefs_dir = reports_tmp
    d = date.today().isoformat()
    briefs_dir.mkdir(parents=True, exist_ok=True)
    (briefs_dir / f"{d}.json").write_text(json.dumps({"date": d}), encoding="utf-8")
    payload, err = call(tools.generate_brief_report, {"date": d})
    assert err
    assert "schema validation" in payload["error"]
    assert not tools.subprocess.run.called


def test_generate_brief_report_chartless_takeaway_still_completes(seeded, reports_tmp):
    # INV-T11: a takeaway with no metric renders a complete PDF (no image).
    reports_dir, briefs_dir = reports_tmp
    d = date.today().isoformat()
    _write_brief_json(briefs_dir, d, [
        {"headline": "h", "summary": "s", "tone": "neutral", "details": "d"},
    ])
    payload, err = call(tools.generate_brief_report, {"date": d})
    assert not err
    path = Path(payload["path"])
    assert path.parent == reports_dir
    assert path.read_bytes()[:5] == b"%PDF-"


def test_generate_brief_report_per_takeaway_render_failure_degrades_gracefully(
    seeded, reports_tmp, monkeypatch
):
    # INV-T12: a per-takeaway fetch/render exception is caught and logged;
    # that takeaway's chart is omitted but the whole report still completes.
    _reports_dir, briefs_dir = reports_tmp
    d = date.today().isoformat()
    _write_brief_json(briefs_dir, d, [
        {
            "headline": "h", "summary": "s", "tone": "neutral", "details": "d",
            "metric": {"metric": "rhr", "days": 14},
        },
    ])

    def boom(*_a, **_k):
        raise RuntimeError("degenerate series")

    monkeypatch.setattr(tools, "_fetch_metric_series", boom)
    payload, err = call(tools.generate_brief_report, {"date": d})
    assert not err
    path = Path(payload["path"])
    pdf_bytes = path.read_bytes()
    assert pdf_bytes[:5] == b"%PDF-"
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as doc:
        total_images = sum(len(p.images) for p in doc.pages)
    assert total_images == 0  # the failed takeaway's chart was skipped


def test_generate_brief_report_happy_path_embeds_chart(seeded, reports_tmp):
    reports_dir, briefs_dir = reports_tmp
    d = date.today().isoformat()
    _write_brief_json(briefs_dir, d, [
        {
            "headline": "Easy day", "summary": "RHR steady", "tone": "positive",
            "details": "Deep dive.", "metric": {"metric": "rhr", "days": 14},
        },
    ])
    payload, err = call(tools.generate_brief_report, {"date": d})
    assert not err
    path = Path(payload["path"])
    # The filename carries an 8-hex content tag between the date and .pdf so a
    # re-render with changed content lands on a fresh Preview window (see
    # tools._content_tag / test_brief_pdf_filename_is_content_addressed).
    assert re.fullmatch(rf"brief-{re.escape(d)}-[0-9a-f]{{8}}\.pdf", path.name)
    assert path.parent == reports_dir
    pdf_bytes = path.read_bytes()
    assert pdf_bytes[:5] == b"%PDF-"
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as doc:
        total_images = sum(len(p.images) for p in doc.pages)
    assert total_images == 1


def test_generate_chart_unknown_metric_no_sql(seeded, monkeypatch):
    # INV-T3: an unwhitelisted metric is rejected before any SQL executes.
    def boom(*_a, **_k):
        raise AssertionError("must not query DB for an unwhitelisted metric")

    monkeypatch.setattr(db, "connect", boom)
    payload, err = call(
        tools.generate_chart, {"metric": "bogus", "days": 14, "chart_type": "line"}
    )
    assert err
    assert "unknown metric" in payload["error"]
    assert not tools.subprocess.run.called


def test_generate_chart_unknown_chart_type(seeded):
    payload, err = call(
        tools.generate_chart, {"metric": "rhr", "days": 14, "chart_type": "pie"}
    )
    assert err
    assert "unknown chart_type" in payload["error"]
    assert not tools.subprocess.run.called


def test_generate_chart_rejects_huge_days(seeded):
    payload, err = call(
        tools.generate_chart, {"metric": "rhr", "days": _BIG, "chart_type": "line"}
    )
    assert err
    assert "days must be between" in payload["error"]
    assert not tools.subprocess.run.called


def test_generate_chart_no_data_in_window(seeded):
    payload, err = call(
        tools.generate_chart, {"metric": "vo2_max", "days": 14, "chart_type": "line"}
    )
    assert err
    assert "no data in window" in payload["error"]
    assert not tools.subprocess.run.called


def test_generate_brief_report_pdf_render_failure_is_error(seeded, reports_tmp, monkeypatch):
    # A whole-PDF render failure (distinct from a per-takeaway degradation)
    # is a hard error -- there's no partial PDF to fall back to.
    from local_fitness.agent import visuals

    _reports_dir, briefs_dir = reports_tmp
    d = date.today().isoformat()
    _write_brief_json(briefs_dir, d, [
        {"headline": "h", "summary": "s", "tone": "neutral", "details": "d"},
    ])

    def boom(*_a, **_k):
        raise RuntimeError("weasyprint exploded")

    monkeypatch.setattr(visuals, "render_brief_pdf", boom)
    payload, err = call(tools.generate_brief_report, {"date": d})
    assert err
    assert "PDF render failed" in payload["error"]
    assert not tools.subprocess.run.called


def test_generate_brief_report_path_escape_is_error(seeded, reports_tmp, monkeypatch):
    _reports_dir, briefs_dir = reports_tmp
    d = date.today().isoformat()
    _write_brief_json(briefs_dir, d, [
        {"headline": "h", "summary": "s", "tone": "neutral", "details": "d"},
    ])

    def boom(*_a, **_k):
        raise ValueError("escaped")

    monkeypatch.setattr(tools, "_write_atomic", boom)
    payload, err = call(tools.generate_brief_report, {"date": d})
    assert err
    assert "escaped reports directory" in payload["error"]
    assert not tools.subprocess.run.called


def test_generate_chart_path_escape_is_error(seeded, reports_tmp, monkeypatch):
    def boom(*_a, **_k):
        raise ValueError("escaped")

    monkeypatch.setattr(tools, "_write_atomic", boom)
    payload, err = call(
        tools.generate_chart, {"metric": "rhr", "days": 14, "chart_type": "line"}
    )
    assert err
    assert "escaped reports directory" in payload["error"]
    assert not tools.subprocess.run.called


def test_generate_chart_render_failure_is_error(seeded, monkeypatch):
    # Unlike generate_brief_report, generate_chart has no takeaway to fall
    # back to -- a render failure is a hard error, not a graceful skip.
    from local_fitness.agent import visuals

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(visuals, "render_chart_png", boom)
    payload, err = call(
        tools.generate_chart, {"metric": "rhr", "days": 14, "chart_type": "line"}
    )
    assert err
    assert "chart render failed" in payload["error"]
    assert not tools.subprocess.run.called


def test_generate_chart_happy_path_writes_expected_png(seeded, reports_tmp):
    # INV-T8 + INV-9: valid PNG at the filename format metric-chart_type-Nd-date.
    reports_dir, _briefs_dir = reports_tmp
    payload, err = call(
        tools.generate_chart, {"metric": "rhr", "days": 14, "chart_type": "line"}
    )
    assert not err
    path = Path(payload["path"])
    today = date.today().isoformat()
    assert path.name == f"chart-rhr-line-14d-{today}.png"
    assert path.parent == reports_dir
    assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_generate_chart_response_carries_inline_image_block(seeded, reports_tmp):
    # Fix A (2026-07-10 doc): the response gains a SECOND content block —
    # an image, base64-decodable, matching the same PNG bytes written to disk.
    import base64

    result = asyncio.run(
        tools.generate_chart.handler({"metric": "rhr", "days": 14, "chart_type": "line"})
    )
    assert result.get("is_error") is not True
    content = result["content"]
    assert content[0]["type"] == "text"
    image_blocks = [c for c in content if c["type"] == "image"]
    assert len(image_blocks) == 1
    image = image_blocks[0]
    assert image["mimeType"] == "image/png"
    decoded = base64.b64decode(image["data"])
    assert decoded[:8] == b"\x89PNG\r\n\x1a\n"
    path = Path(json.loads(content[0]["text"])["path"])
    assert decoded == path.read_bytes()


def test_generate_chart_description_no_longer_local_only(seeded):
    # generate_chart's registered description must no longer claim it's
    # unreachable over the network — Fix A falsifies that claim outright.
    tool = next(t for t in tools.ALL_TOOLS if t.name == "generate_chart")
    lowered = tool.description.lower()
    assert "local-only" not in lowered
    assert "never over the network" not in lowered


def _spy_to_thread(monkeypatch, recorded):
    real_to_thread = asyncio.to_thread

    async def spy(func, *args, **kwargs):
        recorded.append(func)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(tools.asyncio, "to_thread", spy)


def test_generate_brief_report_auto_opens_and_dispatches_via_to_thread(
    seeded, reports_tmp, monkeypatch
):
    _reports_dir, briefs_dir = reports_tmp
    d = date.today().isoformat()
    _write_brief_json(briefs_dir, d, [
        {"headline": "h", "summary": "s", "tone": "neutral", "details": "d"},
    ])
    recorded = []
    _spy_to_thread(monkeypatch, recorded)

    payload, err = call(tools.generate_brief_report, {"date": d})
    assert not err
    final_path = Path(payload["path"])
    tools.subprocess.run.assert_called_once_with(
        ["open", str(final_path)],
        check=False,
        timeout=10,
        stdout=tools.subprocess.DEVNULL,
        stderr=tools.subprocess.DEVNULL,
    )
    assert tools._default_reports_dir in recorded


def test_generate_chart_auto_opens_and_dispatches_via_to_thread(
    seeded, reports_tmp, monkeypatch
):
    recorded = []
    _spy_to_thread(monkeypatch, recorded)

    payload, err = call(
        tools.generate_chart, {"metric": "rhr", "days": 14, "chart_type": "line"}
    )
    assert not err
    final_path = Path(payload["path"])
    tools.subprocess.run.assert_called_once_with(
        ["open", str(final_path)],
        check=False,
        timeout=10,
        stdout=tools.subprocess.DEVNULL,
        stderr=tools.subprocess.DEVNULL,
    )
    assert tools._default_reports_dir in recorded


def test_generate_brief_report_auto_open_failure_does_not_fail_tool(
    seeded, reports_tmp, monkeypatch
):
    _reports_dir, briefs_dir = reports_tmp
    d = date.today().isoformat()
    _write_brief_json(briefs_dir, d, [
        {"headline": "h", "summary": "s", "tone": "neutral", "details": "d"},
    ])
    monkeypatch.setattr(tools.subprocess, "run", Mock(side_effect=OSError("no open binary")))
    payload, err = call(tools.generate_brief_report, {"date": d})
    assert not err
    assert Path(payload["path"]).exists()


def test_generate_chart_auto_open_failure_does_not_fail_tool(seeded, reports_tmp, monkeypatch):
    monkeypatch.setattr(tools.subprocess, "run", Mock(side_effect=OSError("no open binary")))
    payload, err = call(
        tools.generate_chart, {"metric": "rhr", "days": 14, "chart_type": "line"}
    )
    assert not err
    assert Path(payload["path"]).exists()


def test_generate_brief_report_reports_dir_error_is_clean(seeded, reports_tmp, monkeypatch):
    _reports_dir, briefs_dir = reports_tmp
    d = date.today().isoformat()
    _write_brief_json(briefs_dir, d, [
        {"headline": "h", "summary": "s", "tone": "neutral", "details": "d"},
    ])

    def boom():
        raise OSError("disk full")

    monkeypatch.setattr(tools, "_default_reports_dir", boom)
    payload, err = call(tools.generate_brief_report, {"date": d})
    assert err
    assert "could not prepare reports directory" in payload["error"]
    assert not tools.subprocess.run.called


def test_generate_chart_reports_dir_error_is_clean(seeded, monkeypatch):
    def boom():
        raise OSError("disk full")

    monkeypatch.setattr(tools, "_default_reports_dir", boom)
    payload, err = call(
        tools.generate_chart, {"metric": "rhr", "days": 14, "chart_type": "line"}
    )
    assert err
    assert "could not prepare reports directory" in payload["error"]
    assert not tools.subprocess.run.called


def test_pdf_writing_tools_are_local_only_generate_chart_is_not():
    # INV-4 (rewritten per Fix A, 2026-07-10 doc; extended for
    # workout_report_card): a tool that hands back a *filesystem path* is
    # local-only, because a remote /mcp/ caller gets a container-internal path
    # with no way to retrieve the file. Both PDF writers qualify. generate_chart
    # does NOT — its inline ImageContent block sidesteps the retrieval problem,
    # which is exactly why it moved into ALL_TOOLS.
    all_names = {t.name for t in tools.ALL_TOOLS}
    local_only_names = {t.name for t in tools.LOCAL_ONLY_TOOLS}
    assert local_only_names == {"generate_brief_report", "workout_report_card"}
    assert all_names.isdisjoint(local_only_names)
    assert "generate_chart" in all_names


# --- 2026-07-09: Training Plan section (_build_plan_section + wiring) ------

@pytest.fixture
def plan_seeded(tmp_path, monkeypatch):
    """A DB with an ACTIVE training plan spanning the trailing 7 days
    through today, with real activities producing a deliberate mix of
    verdicts (done/partial/missed/compliant/pending).

    Independent of the `seeded` fixture, which pre-seeds an unrelated 10km
    activity on today's date — that would collide with this fixture's
    intentionally-empty (pending) workout for today."""
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    db.init_schema(p)
    today = date.today()

    def d(offset):
        return (today - timedelta(days=offset)).isoformat()

    with db.connect(p) as conn:
        for i in range(7):
            conn.execute("INSERT INTO daily_metrics (date, rhr) VALUES (?, 50)", (d(i),))

    workouts = [
        {"date": d(6), "seq": 1, "week_index": 1, "type": "long",
         "target_distance_m": 6000.0, "description": ""},
        {"date": d(5), "seq": 1, "week_index": 1, "type": "rest",
         "target_distance_m": None, "description": ""},
        {"date": d(4), "seq": 1, "week_index": 1, "type": "easy",
         "target_distance_m": 5000.0, "description": ""},
        {"date": d(3), "seq": 1, "week_index": 1, "type": "tempo",
         "target_distance_m": 3000.0, "description": ""},
        {"date": d(2), "seq": 1, "week_index": 1, "type": "rest",
         "target_distance_m": None, "description": ""},
        {"date": d(1), "seq": 1, "week_index": 2, "type": "easy",
         "target_distance_m": 4000.0, "target_pace_sec_per_km": 350.0,
         "description": "keep HR under 140"},
        {"date": d(0), "seq": 1, "week_index": 2, "type": "easy",
         "target_distance_m": 4000.0, "target_pace_sec_per_km": 350.0,
         "description": "keep HR under 140"},
    ]
    plan_id = plans.insert_draft(
        {
            "goal_type": "10k",
            "race_date": (today + timedelta(days=71)).isoformat(),
            "target_time_seconds": 3000,
            "created_at": today.isoformat(),
        },
        workouts,
        db_path=p,
    )
    plans.commit_plan(plan_id, now="t", db_path=p)

    with db.connect(p) as conn:
        conn.execute(
            "INSERT INTO activities (activity_id, date, activity_type, distance_meters, duration_seconds) "
            "VALUES (1, ?, 'running', 5100.0, 1800)", (d(4),),  # done: easy 5000m
        )
        conn.execute(
            "INSERT INTO activities (activity_id, date, activity_type, distance_meters, duration_seconds) "
            "VALUES (2, ?, 'running', 3050.0, 900)", (d(3),),  # done: tempo 3000m
        )
        conn.execute(
            "INSERT INTO activities (activity_id, date, activity_type, distance_meters, duration_seconds) "
            "VALUES (3, ?, 'running', 2960.0, 1080)", (d(1),),  # partial: easy 4000m
        )
        # d(6) long 6000m: no activity -> missed. d(0) today easy 4000m: no
        # activity -> pending (date >= frontier and raw classify is missed).
    return p


# === WS2 — plan-tool payload quality =========================================
# (docs/plans/2026-07-12-deterministic-intelligence-and-ux-design.md, WS2)

# --- 2a: weekly_rollup — direct import from tools, pure, no DB ---------------

def test_weekly_rollup_empty_workouts_yields_zero_totals():
    rollup = tools.weekly_rollup([], "2026-07-12")
    assert rollup == {
        "week_planned_mi": 0.0, "week_actual_mi": 0.0,
        "week_run_mi": 0.0, "week_walk_mi": 0.0, "slips": 0, "days": [],
    }


def test_weekly_rollup_single_workout():
    workouts = [
        {"date": "2026-07-12", "verdict": "done", "type": "easy",
         "target_distance_m": 5000.0, "actual_distance_m": 5100.0},
    ]
    rollup = tools.weekly_rollup(workouts, "2026-07-12")
    # No actual_run/walk keys on the input, so run_mi falls back to actual_mi
    # (a pre-pace-gating caller must not silently lose its mileage) and walk
    # is zero.
    assert rollup["days"] == [
        {"date": "2026-07-12", "verdict": "done", "type": "easy",
         "planned_mi": 3.11, "actual_mi": 3.17, "run_mi": 3.17, "walk_mi": 0.0},
    ]
    assert rollup["week_planned_mi"] == 3.1
    assert rollup["week_actual_mi"] == 3.2
    assert rollup["week_run_mi"] == 3.2
    assert rollup["week_walk_mi"] == 0.0
    assert rollup["slips"] == 0


def test_weekly_rollup_days_reverse_chronological():
    workouts = [
        {"date": "2026-07-06", "verdict": "done", "type": "easy",
         "target_distance_m": 5000.0, "actual_distance_m": 5100.0},
        {"date": "2026-07-10", "verdict": "done", "type": "tempo",
         "target_distance_m": 3000.0, "actual_distance_m": 3050.0},
        {"date": "2026-07-08", "verdict": "missed", "type": "long",
         "target_distance_m": 6000.0, "actual_distance_m": None},
    ]
    rollup = tools.weekly_rollup(workouts, "2026-07-12")
    dates = [d["date"] for d in rollup["days"]]
    assert dates == ["2026-07-10", "2026-07-08", "2026-07-06"]  # most recent first


def test_weekly_rollup_suppresses_actual_for_pending_and_compliant():
    workouts = [
        {"date": "2026-07-12", "verdict": "pending", "type": "easy",
         "target_distance_m": 4000.0, "actual_distance_m": None},
        # compliant rest day carrying a stray real actual_distance_m (e.g. an
        # untracked walk) — still suppressed; suppression is verdict-keyed,
        # not presence-of-data-keyed.
        {"date": "2026-07-11", "verdict": "compliant", "type": "rest",
         "target_distance_m": None, "actual_distance_m": 2000.0},
        {"date": "2026-07-10", "verdict": "done", "type": "easy",
         "target_distance_m": 5000.0, "actual_distance_m": 5100.0},
    ]
    rollup = tools.weekly_rollup(workouts, "2026-07-12")
    by_date = {d["date"]: d for d in rollup["days"]}
    assert by_date["2026-07-12"]["actual_mi"] is None
    assert by_date["2026-07-11"]["actual_mi"] is None
    assert by_date["2026-07-10"]["actual_mi"] == 3.17


def test_weekly_rollup_missing_target_distance_yields_planned_mi_none():
    workouts = [
        {"date": "2026-07-12", "verdict": "compliant", "type": "rest",
         "target_distance_m": None, "actual_distance_m": None},
    ]
    rollup = tools.weekly_rollup(workouts, "2026-07-12")
    assert rollup["days"][0]["planned_mi"] is None


def test_weekly_rollup_zero_target_distance_yields_planned_mi_zero_not_none():
    # units.to_miles's `is not None` convention (not truthy) — a synthetic
    # 0-meter target yields 0.0, not None. On real data this never occurs
    # (no 0-distance prescribed target; rest days carry None), but the
    # convention is pinned here per the 07-10 doc's deferred truthy-vs-None
    # flag, now picked up.
    workouts = [
        {"date": "2026-07-12", "verdict": "missed", "type": "easy",
         "target_distance_m": 0.0, "actual_distance_m": None},
    ]
    rollup = tools.weekly_rollup(workouts, "2026-07-12")
    assert rollup["days"][0]["planned_mi"] == 0.0


def test_weekly_rollup_window_excludes_outside_trailing_7_days():
    workouts = [
        {"date": "2026-07-05", "verdict": "done", "type": "easy",  # 7d back -> outside
         "target_distance_m": 5000.0, "actual_distance_m": 5000.0},
        {"date": "2026-07-06", "verdict": "done", "type": "easy",  # 6d back -> boundary, inside
         "target_distance_m": 5000.0, "actual_distance_m": 5000.0},
        {"date": "2026-07-13", "verdict": "pending", "type": "easy",  # after target_date -> outside
         "target_distance_m": 5000.0, "actual_distance_m": None},
    ]
    rollup = tools.weekly_rollup(workouts, "2026-07-12")
    dates = [d["date"] for d in rollup["days"]]
    assert dates == ["2026-07-06"]


def test_weekly_rollup_rounding_order_per_day_then_sum():
    # Divergence case: per-day-round(2dp)-then-sum-then-round(1dp) yields
    # 2.6; summing raw meters first and converting once yields 2.5 — proving
    # the rounding ORDER is load-bearing, not incidental (2a).
    workouts = [
        {"date": "2026-07-12", "verdict": "done", "type": "easy",
         "target_distance_m": None, "actual_distance_m": 2730.0},
        {"date": "2026-07-11", "verdict": "done", "type": "easy",
         "target_distance_m": None, "actual_distance_m": 509.0},
        {"date": "2026-07-10", "verdict": "done", "type": "easy",
         "target_distance_m": None, "actual_distance_m": 861.0},
    ]
    rollup = tools.weekly_rollup(workouts, "2026-07-12")
    assert rollup["week_actual_mi"] == 2.6  # not 2.5 (raw-sum-then-convert)


def test_weekly_rollup_slips_counts_partial_and_missed_only():
    workouts = [
        {"date": "2026-07-12", "verdict": "done", "type": "easy",
         "target_distance_m": 5000.0, "actual_distance_m": 5000.0},
        {"date": "2026-07-11", "verdict": "partial", "type": "easy",
         "target_distance_m": 5000.0, "actual_distance_m": 2000.0},
        {"date": "2026-07-10", "verdict": "missed", "type": "long",
         "target_distance_m": 8000.0, "actual_distance_m": None},
        {"date": "2026-07-09", "verdict": "compliant", "type": "rest",
         "target_distance_m": None, "actual_distance_m": None},
        {"date": "2026-07-08", "verdict": "pending", "type": "easy",
         "target_distance_m": 5000.0, "actual_distance_m": None},
    ]
    rollup = tools.weekly_rollup(workouts, "2026-07-12")
    assert rollup["slips"] == 2


def test_build_plan_section_no_active_plan_is_none(seeded):
    assert tools._build_plan_section(date.today().isoformat()) is None


def test_build_plan_section_active_plan_full_values(plan_seeded):
    today = date.today()
    section = tools._build_plan_section(today.isoformat())
    assert section is not None
    assert section["adherence_pct"] == 75  # 4.5/6 non-pending graded workouts
    assert section["goal_type"] == "10k"
    assert section["days_to_race"] == 71
    assert section["week_planned_mi"] == 13.7
    assert section["week_actual_mi"] == 6.9
    assert section["slips"] == 2  # d(6) missed + d(1) partial

    assert section["today"] == {
        "type": "easy",
        "distance_mi": 2.49,
        "pace_min_per_mi": "9:23",
        "description": "keep HR under 140",
    }

    dates = [w["date"] for w in section["last_7_days"]]
    assert dates == sorted(dates, reverse=True)  # most recent first
    by_date = {w["date"]: w for w in section["last_7_days"]}
    assert by_date[today.isoformat()]["verdict"] == "pending"
    assert by_date[today.isoformat()]["actual_mi"] is None
    assert by_date[(today - timedelta(days=1)).isoformat()]["verdict"] == "partial"
    assert by_date[(today - timedelta(days=1)).isoformat()]["actual_mi"] == 1.84
    assert by_date[(today - timedelta(days=2)).isoformat()]["verdict"] == "compliant"
    assert by_date[(today - timedelta(days=2)).isoformat()]["planned_mi"] is None
    assert by_date[(today - timedelta(days=3)).isoformat()]["verdict"] == "done"
    assert by_date[(today - timedelta(days=4)).isoformat()]["verdict"] == "done"
    assert by_date[(today - timedelta(days=5)).isoformat()]["verdict"] == "compliant"
    assert by_date[(today - timedelta(days=6)).isoformat()]["verdict"] == "missed"
    assert by_date[(today - timedelta(days=6)).isoformat()]["actual_mi"] == 0.0


def test_build_plan_section_no_workouts_in_window_is_none(plan_seeded):
    # Plan's workouts only span today-6..today; a target_date far outside
    # that range has nothing in its trailing-7-day window.
    far_future = (date.today() + timedelta(days=100)).isoformat()
    assert tools._build_plan_section(far_future) is None


def test_generate_brief_report_wires_plan_section_and_calls_coaching_line(
    plan_seeded, reports_tmp, monkeypatch
):
    reports_dir, briefs_dir = reports_tmp
    d = date.today().isoformat()
    _write_brief_json(briefs_dir, d, [
        {"headline": "h", "summary": "s", "tone": "neutral", "details": "d"},
    ])

    calls = []

    async def fake_generate(
        profile, today_workout, last_7_days, adherence_pct, days_to_race, goal_type,
        *, model=None, timeout=30.0, notes_text=None, **_kw,
    ):
        calls.append((today_workout, adherence_pct, days_to_race, goal_type, notes_text))
        return "Go hit today's easy 4 clean."

    monkeypatch.setattr(tools.plan_coach, "generate_coaching_line", fake_generate)

    payload, err = call(tools.generate_brief_report, {"date": d})
    assert not err
    pdf_bytes = Path(payload["path"]).read_bytes()
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as doc:
        text = "\n".join(p.extract_text() or "" for p in doc.pages)
    assert "TRAINING PLAN" in text
    assert "Go hit today's easy 4 clean." in text
    assert len(calls) == 1
    assert calls[0][1] == 75  # adherence_pct threaded through correctly
    assert calls[0][2] == 71  # days_to_race
    assert calls[0][3] == "10k"


def test_generate_brief_report_coaching_line_failure_falls_back(
    plan_seeded, reports_tmp, monkeypatch
):
    reports_dir, briefs_dir = reports_tmp
    d = date.today().isoformat()
    _write_brief_json(briefs_dir, d, [
        {"headline": "h", "summary": "s", "tone": "neutral", "details": "d"},
    ])

    async def boom(*_a, **_k):
        raise RuntimeError("no credential")

    monkeypatch.setattr(tools.plan_coach, "generate_coaching_line", boom)

    payload, err = call(tools.generate_brief_report, {"date": d})
    assert not err  # a coaching-line failure must never fail the whole report
    pdf_bytes = Path(payload["path"]).read_bytes()
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as doc:
        text = "\n".join(p.extract_text() or "" for p in doc.pages)
        # The 2026-07-09 layout puts signal cards and the Training Plan
        # side by side as two real page columns — plain extract_text()
        # reads left-to-right across the FULL page width per visual row,
        # interleaving words from both columns and breaking up a phrase
        # that lives entirely in the (right) plan column. Crop to the
        # right half before extracting to read that phrase intact.
        plan_text = "\n".join(
            p.crop((p.width * 0.45, 0, p.width, p.height)).extract_text() or ""
            for p in doc.pages
        )
    assert "TRAINING PLAN" in text
    # The narrow right rail can word-wrap mid phrase (e.g. "9:23/" then
    # "mi." on the next PDF line, with no real space between them) — strip
    # ALL whitespace from both sides so a wrap-induced newline can't be
    # mistaken for (or masked by) a real space.
    squashed = "".join(plan_text.split())
    # fallback_coaching_line's deterministic phrasing for a partial prior day.
    # It deliberately does NOT restate the prescription — the Today callout
    # prints that directly above it (see plan_coach).
    assert "".join("Yesterday came up short of the prescription.".split()) in squashed
    assert "".join("Today: easy 2.49 mi @ 9:23/mi.".split()) not in squashed


def test_generate_brief_report_no_active_plan_has_no_plan_section(seeded, reports_tmp):
    reports_dir, briefs_dir = reports_tmp
    d = date.today().isoformat()
    _write_brief_json(briefs_dir, d, [
        {"headline": "h", "summary": "s", "tone": "neutral", "details": "d"},
    ])
    payload, err = call(tools.generate_brief_report, {"date": d})
    assert not err
    pdf_bytes = Path(payload["path"]).read_bytes()
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as doc:
        text = "\n".join(p.extract_text() or "" for p in doc.pages)
    assert "TRAINING PLAN" not in text


# === WS3 3a — plan_coach notes parity ========================================

def test_generate_brief_report_threads_saved_notes_into_coaching_line(
    plan_seeded, reports_tmp, monkeypatch
):
    """3a: a saved preference must reach generate_coaching_line's
    notes_text, exactly like it already reaches chat/brief via
    notes.render_for_prompt()."""
    reports_dir, briefs_dir = reports_tmp
    d = date.today().isoformat()
    _write_brief_json(briefs_dir, d, [
        {"headline": "h", "summary": "s", "tone": "neutral", "details": "d"},
    ])
    from local_fitness import notes as notes_mod
    notes_mod.append_note("stop roasting my steps")

    calls = []

    async def fake_generate(
        profile, today_workout, last_7_days, adherence_pct, days_to_race, goal_type,
        *, model=None, timeout=30.0, notes_text=None, **_kw,
    ):
        calls.append(notes_text)
        return "Go hit today's easy 4 clean."

    monkeypatch.setattr(tools.plan_coach, "generate_coaching_line", fake_generate)

    payload, err = call(tools.generate_brief_report, {"date": d})
    assert not err
    assert len(calls) == 1
    assert "stop roasting my steps" in calls[0]


# === WS4 4a — ground the PDF coaching line ===================================

def test_generate_brief_report_logs_coaching_line_grounding(
    plan_seeded, reports_tmp, monkeypatch, caplog
):
    """4a: the coaching line is checked against the deterministic plan
    section — advisory-only (logged, never gates the PDF). An invented
    adherence number close to but not equal to the real 75% (plan_seeded's
    fixture value) must produce a logged flag while the PDF still renders
    normally, mirroring grounding.log_grounding's log-only pattern."""
    reports_dir, briefs_dir = reports_tmp
    d = date.today().isoformat()
    _write_brief_json(briefs_dir, d, [
        {"headline": "h", "summary": "s", "tone": "neutral", "details": "d"},
    ])

    async def fake_generate(*_a, **_k):
        # 80% is a subtle corruption of the real 75% adherence — close enough
        # to look like the same metric (within grounding's NEARBY band) but
        # not equal (outside the EXACT band) -> a "flag" verdict, not silent.
        return "You're running at 80% adherence this week — keep it up."

    monkeypatch.setattr(tools.plan_coach, "generate_coaching_line", fake_generate)

    with caplog.at_level(logging.INFO, logger="local_fitness.agent.tools"):
        payload, err = call(tools.generate_brief_report, {"date": d})
    assert not err  # advisory grounding must never fail the PDF

    pdf_bytes = Path(payload["path"]).read_bytes()
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as doc:
        text = "\n".join(p.extract_text() or "" for p in doc.pages)
    assert "TRAINING PLAN" in text
    # The invented line still renders verbatim -- grounding never alters the PDF.
    assert "80% adherence" in text

    flag_logs = [r for r in caplog.records if "plan_coach_grounding" in r.message]
    assert len(flag_logs) == 1
    assert "flags=1" in flag_logs[0].message
    assert "adherence_pct" in flag_logs[0].message


def test_generate_brief_report_coaching_line_grounding_clean_line_logs_zero_flags(
    plan_seeded, reports_tmp, monkeypatch, caplog
):
    """A coaching line that only cites faithful numbers (or none at all)
    logs flags=0 -- the signal doesn't manufacture false positives on a
    clean line."""
    reports_dir, briefs_dir = reports_tmp
    d = date.today().isoformat()
    _write_brief_json(briefs_dir, d, [
        {"headline": "h", "summary": "s", "tone": "neutral", "details": "d"},
    ])

    async def fake_generate(*_a, **_k):
        return "Solid week. Keep showing up and trust the process."

    monkeypatch.setattr(tools.plan_coach, "generate_coaching_line", fake_generate)

    with caplog.at_level(logging.INFO, logger="local_fitness.agent.tools"):
        payload, err = call(tools.generate_brief_report, {"date": d})
    assert not err

    flag_logs = [r for r in caplog.records if "plan_coach_grounding" in r.message]
    assert len(flag_logs) == 1
    assert "flags=0" in flag_logs[0].message


# === WS3 3d — MCP-appropriate error strings ==================================

def test_training_load_status_empty_db_error_points_at_sync_tool(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    payload, err = call(tools.training_load_status, {})
    assert err
    assert "sync_garmin_data" in payload["error"]
    assert "recompute-baselines" not in payload["error"]
    assert "fitness " not in payload["error"]  # no bare CLI-command wording


def test_run_sql_invalid_query_points_at_schema_resource(seeded, monkeypatch):
    # Both except clauses carry the schema pointer now; this forces the
    # rarer non-OperationalError sqlite3.Error branch to keep it covered.
    import sqlite3

    def boom(_q):
        raise sqlite3.IntegrityError("boom")

    monkeypatch.setattr(tools, "_run_sql_blocking", boom)
    payload, err = call(tools.run_sql, {"query": "SELECT 1"})
    assert err
    assert "query failed: invalid query" in payload["error"]
    assert "fitness://schema" in payload["error"]


def test_log_manual_workout_recompute_failure_warning_no_cli_wording(seeded, monkeypatch):
    from local_fitness.ingest import baselines

    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(baselines, "recompute", boom)

    payload, err = call(
        tools.log_manual_workout,
        {"activity_type": "strength", "duration_min": 45},
    )
    assert not err
    assert payload["recompute_failed"] is True
    assert "recompute failed" in payload["warning"]  # pinned literal, tests/test_tools.py:731
    assert "fitness baselines" not in payload["warning"]
    assert "sync_garmin_data" in payload["warning"]


def test_delete_manual_workout_recompute_failure_warning_no_cli_wording(seeded, monkeypatch):
    from local_fitness.ingest import baselines

    saved, err = call(
        tools.log_manual_workout, {"activity_type": "strength", "duration_min": 45}
    )
    assert not err
    aid = saved["activity"]["activity_id"]

    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(baselines, "recompute", boom)

    payload, err = call(tools.delete_manual_workout, {"activity_id": aid})
    assert not err
    assert payload["recompute_failed"] is True
    assert "recompute failed" in payload["warning"]  # pinned literal, tests/test_tools.py:754
    assert "fitness baselines" not in payload["warning"]
    assert "sync_garmin_data" in payload["warning"]


# === WS3 3e — "when NOT to use" description lines ===========================

def test_daily_snapshot_description_names_get_brief_context():
    tool = next(t for t in tools.ALL_TOOLS if t.name == "daily_snapshot")
    assert "get_brief_context" in tool.description


def test_get_brief_context_description_names_get_metric_trend():
    tool = next(t for t in tools.ALL_TOOLS if t.name == "get_brief_context")
    assert "get_metric" in tool.description
    assert "get_metric_trend" in tool.description


# --- long-window bar/combo weekly bucketing (round-2 facet review) -----------

def test_bucket_weekly_mean_and_sum():
    dates = ["2026-07-06", "2026-07-07", "2026-07-13"]  # Mon, Tue, next Mon
    weeks, means = tools._bucket_weekly(dates, [50.0, 54.0, 60.0], cumulative=False)
    assert weeks == ["2026-07-06", "2026-07-13"]
    assert means == [52.0, 60.0]
    _weeks, sums = tools._bucket_weekly(dates, [50.0, 54.0, 60.0], cumulative=True)
    assert sums == [104.0, 60.0]


def test_bucket_weekly_anchors_to_monday():
    # 2026-07-09 is a Thursday → its week anchors to Monday 2026-07-06.
    weeks, _ = tools._bucket_weekly(["2026-07-09"], [1.0], cumulative=False)
    assert weeks == ["2026-07-06"]


def test_chart_bar_long_window_buckets_weekly(seeded):
    text, err = call(tools.chart, {"metric": "rhr", "days": 35, "style": "bar"})
    assert not err
    assert "weekly avg" in text
    # ~6 ISO weeks cover a 35-day window with 40 seeded days — never 35 rows.
    data_rows = [ln for ln in text.split("\n") if ln and "·" not in ln]
    assert 4 <= len(data_rows) <= 7


def test_chart_bar_long_window_cumulative_metric_sums(seeded):
    text, err = call(tools.chart, {"metric": "steps", "days": 35, "style": "bar"})
    assert not err
    assert "weekly sum" in text


def test_chart_combo_long_window_buckets_weekly(seeded):
    text, err = call(tools.chart, {"metric": "rhr", "days": 35, "style": "combo"})
    assert not err
    assert "weekly avg" in text


def test_chart_bar_short_window_stays_daily(seeded):
    text, err = call(tools.chart, {"metric": "rhr", "days": 14, "style": "bar"})
    assert not err
    assert "weekly" not in text


# --- workout_report_card ---------------------------------------------------

@pytest.fixture
def rc_seeded(tmp_path, monkeypatch):
    """A DB with enough comparable running history for a real reference.

    The shared `seeded` fixture holds a single activity, which every report
    card would grade as `insufficient_data` — useful for exactly one test, not
    for the handler's happy path.
    """
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    today = date.today()
    with db.connect(p) as conn:
        for i in range(1, 13):
            d = (today - timedelta(days=i)).isoformat()
            conn.execute(
                "INSERT INTO activities (activity_id, date, start_time, activity_type, "
                "activity_name, duration_seconds, distance_meters, avg_hr, "
                "avg_pace_sec_per_km, training_load) VALUES "
                "(?, ?, ?, 'running', 'Run', 3000, 10000, 150, 300, 100)",
                (100 + i, d, f"{d} 07:00:00"),
            )
        conn.execute(
            "INSERT INTO activities (activity_id, date, start_time, activity_type, "
            "activity_name, duration_seconds, distance_meters, avg_hr, "
            "avg_pace_sec_per_km, training_load) VALUES "
            "(1, ?, ?, 'running', 'Morning Run', 3000, 10000, 150, 300, 100)",
            (today.isoformat(), today.isoformat() + " 07:00:00"),
        )
        for idx, hr in enumerate((140, 145, 150, 155)):
            conn.execute(
                "INSERT INTO activity_splits (activity_id, split_index, distance_meters, "
                "duration_seconds, avg_hr) VALUES (1, ?, 1609.34, 600, ?)",
                (idx, hr),
            )
    return p


def test_report_card_defaults_to_the_most_recent_activity(rc_seeded, reports_tmp):
    payload, err = call(tools.workout_report_card, {"format": "table"})
    assert not err
    assert payload["activity_id"] == 1
    assert payload["date"] == date.today().isoformat()
    assert payload["markdown"].startswith("# Report Card")


def test_report_card_activity_id_overrides_date(rc_seeded, reports_tmp):
    payload, err = call(tools.workout_report_card, {
        "activity_id": 105, "date": date.today().isoformat(), "format": "table"})
    assert not err
    assert payload["activity_id"] == 105


def test_report_card_by_date(rc_seeded, reports_tmp):
    d = (date.today() - timedelta(days=3)).isoformat()
    payload, err = call(tools.workout_report_card, {"date": d, "format": "table"})
    assert not err
    assert payload["date"] == d


def test_report_card_no_matching_activity_is_an_error(rc_seeded, reports_tmp):
    payload, err = call(tools.workout_report_card, {"date": "1999-01-01"})
    assert err
    assert "no matching activity" in payload["error"]


def test_report_card_malformed_date_never_touches_the_db(rc_seeded, monkeypatch):
    """Validation happens before any query, mirroring generate_chart's
    unknown-metric guard."""
    def boom(*_a, **_k):
        raise AssertionError("must not open the DB on a malformed date")

    monkeypatch.setattr(tools.db, "connect", boom)
    payload, err = call(tools.workout_report_card, {"date": "07-19-2026"})
    assert err
    assert "malformed date" in payload["error"]


def test_report_card_bad_format_is_an_error(rc_seeded, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("must not open the DB on a bad format")

    monkeypatch.setattr(tools.db, "connect", boom)
    payload, err = call(tools.workout_report_card, {"format": "csv"})
    assert err
    assert payload["allowed"] == ["both", "pdf", "table"]


def test_report_card_table_format_writes_no_file(rc_seeded, reports_tmp):
    reports_dir, _ = reports_tmp
    payload, err = call(tools.workout_report_card, {"format": "table"})
    assert not err
    assert "path" not in payload
    assert not reports_dir.exists() or list(reports_dir.glob("*.pdf")) == []


def test_report_card_writes_a_pdf(rc_seeded, reports_tmp):
    reports_dir, _ = reports_tmp
    payload, err = call(tools.workout_report_card, {})
    assert not err
    path = Path(payload["path"])
    assert re.fullmatch(r"report-card-1-[0-9a-f]{8}\.pdf", path.name)
    assert path.parent == reports_dir
    assert path.read_bytes()[:5] == b"%PDF-"


def test_report_card_pdf_states_the_rolling_reference(rc_seeded, reports_tmp):
    """The 'which yardstick' requirement is asserted in the rendered page, not
    merely in the payload."""
    payload, err = call(tools.workout_report_card, {})
    assert not err
    with pdfplumber.open(io.BytesIO(Path(payload["path"]).read_bytes())) as doc:
        text = "".join(page.extract_text() or "" for page in doc.pages)
    # The standalone yardstick sentence was dropped (0.25.0) — Expected states
    # the target per metric. The disclosure now rides the hero's meta line, so
    # the page must still say WHICH reference produced the grades.
    assert "60d median" in text
    assert "**" not in text          # markdown emphasis must not print literally


def test_report_card_pdf_states_the_plan_reference(rc_seeded, reports_tmp):
    today = date.today().isoformat()
    with db.connect(rc_seeded) as conn:
        conn.execute(
            "INSERT INTO training_plans (plan_id, status, goal_type, race_date, "
            "title, created_at) VALUES (7, 'active', '10k', ?, 'Plan', ?)",
            ((date.today() + timedelta(days=30)).isoformat(), today),
        )
        conn.execute(
            "INSERT INTO plan_workouts (plan_id, date, seq, week_index, type, "
            "target_distance_m, target_pace_sec_per_km, description) "
            "VALUES (7, ?, 1, 1, 'easy', 10000, 330, 'Easy 10k')",
            (today,),
        )
    payload, err = call(tools.workout_report_card, {})
    assert not err
    assert payload["reference"] == "rolling_60d"     # the HR/load pool
    assert payload["intent_source"] == "plan"
    with pdfplumber.open(io.BytesIO(Path(payload["path"]).read_bytes())) as doc:
        text = "".join(page.extract_text() or "" for page in doc.pages)
    # Graded against the plan, and the meta line says so.
    assert "(plan)" in text
    assert "60d median" not in text


def test_report_card_without_splits_still_grades_and_still_renders(rc_seeded, reports_tmp):
    """~88% of the history is backfilled and carries no splits — the common
    case, not an edge case."""
    with db.connect(rc_seeded) as conn:
        conn.execute("DELETE FROM activity_splits WHERE activity_id = 1")
    payload, err = call(tools.workout_report_card, {})
    assert not err
    assert payload["splits_available"] is False
    assert payload["overall"]["grade"] != "n/a"      # grades are unaffected
    assert Path(payload["path"]).read_bytes()[:5] == b"%PDF-"
    assert "No per-mile splits recorded" in payload["markdown"]


def test_report_card_insufficient_history_grades_nothing(seeded, reports_tmp):
    """The shared fixture's single activity has no comparable history."""
    payload, err = call(tools.workout_report_card, {"format": "table"})
    assert not err
    assert payload["reference"] == "insufficient_data"
    assert payload["overall"]["grade"] == "n/a"
    assert set(payload["grades"].values()) == {None}


def test_report_card_reports_a_double_day(rc_seeded, reports_tmp):
    with db.connect(rc_seeded) as conn:
        conn.execute(
            "INSERT INTO activities (activity_id, date, start_time, activity_type, "
            "duration_seconds, distance_meters) VALUES (555, ?, ?, 'running', 1200, 5000)",
            (date.today().isoformat(), date.today().isoformat() + " 05:00:00"),
        )
    payload, err = call(tools.workout_report_card, {"format": "table"})
    assert not err
    assert 555 in payload["other_activities_on_date"]


def test_report_card_pdf_render_failure_is_an_error(rc_seeded, reports_tmp, monkeypatch):
    from local_fitness.agent import visuals

    def boom(*_a, **_k):
        raise RuntimeError("weasyprint exploded")

    monkeypatch.setattr(visuals, "render_report_card_pdf", boom)
    payload, err = call(tools.workout_report_card, {})
    assert err
    assert "PDF render failed" in payload["error"]


def test_report_card_split_chart_failure_never_sinks_the_card(
    rc_seeded, reports_tmp, monkeypatch
):
    """One section's problem must never fail the whole report."""
    from local_fitness.agent import visuals

    def boom(*_a, **_k):
        raise RuntimeError("matplotlib exploded")

    monkeypatch.setattr(visuals, "render_split_hr_png", boom)
    payload, err = call(tools.workout_report_card, {})
    assert not err
    assert Path(payload["path"]).read_bytes()[:5] == b"%PDF-"


def test_report_card_path_escape_is_error(rc_seeded, reports_tmp, monkeypatch):
    def boom(*_a, **_k):
        raise ValueError("escaped")

    monkeypatch.setattr(tools, "_write_atomic", boom)
    payload, err = call(tools.workout_report_card, {})
    assert err
    assert "escaped reports directory" in payload["error"]


def test_report_card_table_format_never_reaches_the_network(rc_seeded, monkeypatch):
    """format='table' must stay a purely local read. The HR trace is the only
    thing on this path that can hit Garmin, and a markdown card has nowhere to
    plot it — so it must not even be resolved."""
    from local_fitness.ingest import details

    monkeypatch.setattr(
        details, "fetch_hr_samples",
        lambda *a, **k: pytest.fail("format='table' attempted a Garmin fetch"))
    payload, err = call(tools.workout_report_card, {"format": "table"})
    assert not err
    assert payload["markdown"]


def test_report_card_pdf_resolves_the_hr_trace(rc_seeded, reports_tmp, monkeypatch):
    """The PDF path opts into the trace, and a fetched trace is cached so the
    second render makes no second call."""
    from local_fitness.ingest import details

    calls = []

    def _fetch(activity_id):
        calls.append(activity_id)
        # ~0.3 mi of samples at a steady 40m per 15s: enough for three
        # tenth-mile buckets, each with a computable pace.
        return [details.HrSample(float(i) * 40.0, 120 + i, float(i) * 15.0)
                for i in range(13)]

    monkeypatch.setattr(details, "fetch_hr_samples", _fetch)
    payload, err = call(tools.workout_report_card, {})
    assert not err
    assert calls == [1]
    _, err2 = call(tools.workout_report_card, {})
    assert not err2
    assert calls == [1]  # served from the SQLite cache the first render wrote


def test_report_card_coach_read_failure_falls_back_and_still_renders(
    rc_seeded, reports_tmp, monkeypatch, caplog
):
    """A dead SDK stream costs the phrasing, never the card — every grade on it
    was computed in Python before the model was ever asked."""
    async def _boom(*a, **k):
        raise RuntimeError("stream died")

    monkeypatch.setattr(tools.workout_coach, "generate_read_cached", _boom)
    with caplog.at_level(logging.WARNING):
        payload, err = call(tools.workout_report_card, {})
    assert not err
    assert Path(payload["path"]).read_bytes()[:5] == b"%PDF-"
    assert any("workout read generation failed" in r.message for r in caplog.records)


def test_report_card_pdf_leads_with_the_coach_read(rc_seeded, reports_tmp, monkeypatch):
    async def _read(*a, **k):
        return {"distance": "You covered the ground.", "pace": "Too quick.",
                "hr": "Stayed low.", "load": "Banked what it should."}

    monkeypatch.setattr(tools.workout_coach, "generate_read_cached", _read)
    payload, err = call(tools.workout_report_card, {})
    assert not err
    with pdfplumber.open(io.BytesIO(Path(payload["path"]).read_bytes())) as doc:
        text = "\n".join(p.extract_text() or "" for p in doc.pages)
    # All four paragraphs render, each under its metric label.
    for label in ("DISTANCE", "PACE", "HEART RATE", "TRAINING LOAD"):
        assert label in text
    for para in ("You covered the ground.", "Too quick.", "Stayed low.",
                 "Banked what it should."):
        assert para in text
    # They sit under the GPA/distance/pace line inside the hero, in table order.
    assert text.index("4.00 GPA") < text.index("You covered the ground.")
    assert text.index("You covered the ground.") < text.index("Too quick.")
    # The GPA explainer was removed — the number stands on its own.
    assert "weighted 4.0 scale" not in text


def test_report_card_pdf_splits_table_has_no_distance_column(rc_seeded, reports_tmp):
    """Dropped as duplicative: the row label already IS the distance, so a
    Distance column printed '1.00 mi' beside a column headed 'Mile'."""
    payload, err = call(tools.workout_report_card, {})
    assert not err
    html_out = visuals._render_splits_html(
        report_card.build_card(
            {"activity_id": 1, "date": "2026-07-19", "activity_type": "running",
             "distance_meters": 10000, "duration_seconds": 3000,
             "avg_pace_sec_per_km": 300, "avg_hr": 150, "training_load": 100},
            [{"activity_id": 1, "split_index": 0, "distance_meters": 1609.344,
              "duration_seconds": 480, "avg_hr": 148, "avg_pace_sec_per_km": 298,
              "elevation_gain_meters": 10}],
            None, {"mode": "insufficient_data", "n": 0},
        ),
        None,
    )
    headers = re.findall(r"<th>(.*?)</th>", html_out)
    assert "Distance" not in headers
    assert headers == ["Mile", "Pace", "Avg HR", "vs run", "Elev"]


def test_report_card_grade_column_is_left_aligned_like_the_others():
    """Every column in both tables shares one alignment; the grade is already
    the loudest cell by weight and size and doesn't also need a different one."""
    css = visuals._report_card_css(branding.load_theme())
    assert "td.metric-grade {{" not in css  # not an unformatted f-string
    grade_rule = [ln for ln in css.splitlines() if ln.startswith("td.metric-grade")]
    assert grade_rule and "text-align: left" in grade_rule[0]


def test_report_card_is_local_only():
    """Regression guard for web/mcp_server.py's transport contract: a
    PDF-writing tool must never reach the networked /mcp/ surface, which has
    no way to retrieve a local path."""
    assert "workout_report_card" in [t.name for t in tools.LOCAL_ONLY_TOOLS]
    assert "workout_report_card" not in [t.name for t in tools.ALL_TOOLS]
    assert "mcp__fitness__workout_report_card" not in tools.allowed_tool_names()


# --- the one-page guarantee (end-to-end) ------------------------------------
# render_brief_pdf shrinks; generate_brief_report shrinks THEN truncates. The
# guarantee that a saved report is exactly one page lives here, at the tool,
# because only the tool is allowed to drop content.

_FAT_TAKEAWAY = {
    "headline": "A headline of the length the generator actually emits daily",
    "summary": "A standfirst of roughly twenty-five words, which is what the "
               "brief generator writes in practice, so the measured card "
               "height is honest rather than optimistic.",
    "tone": "neutral",
    "metric": {"metric": "rhr", "days": 14},
    "details": "Four or five sentences of deep-dive prose. It cites a number, "
               "explains what that number means against baseline, and then "
               "says what to do about it today. That is the realistic worst "
               "case for how tall one signal card gets.",
}


@pytest.mark.parametrize("n_takeaways", [1, 2, 3, 4, 5])
def test_generate_brief_report_is_always_exactly_one_page(
    seeded, reports_tmp, n_takeaways, monkeypatch
):
    reports_dir, briefs_dir = reports_tmp
    d = date.today().isoformat()
    _write_brief_json(briefs_dir, d, [dict(_FAT_TAKEAWAY) for _ in range(n_takeaways)])

    async def fake_line(*a, **k):
        return "Coaching line."
    monkeypatch.setattr(tools.plan_coach, "generate_coaching_line_cached", fake_line)

    payload, err = call(tools.generate_brief_report, {"date": d})
    assert not err, payload
    with pdfplumber.open(io.BytesIO(Path(payload["path"]).read_bytes())) as doc:
        assert len(doc.pages) == 1


def test_generate_brief_report_states_what_it_dropped(seeded, reports_tmp, monkeypatch):
    """When the density ladder is exhausted the report truncates — and says so
    on the page. A silently shortened brief is the failure mode this exists to
    prevent."""
    reports_dir, briefs_dir = reports_tmp
    d = date.today().isoformat()
    _write_brief_json(briefs_dir, d, [dict(_FAT_TAKEAWAY) for _ in range(5)])

    async def fake_line(*a, **k):
        return "Coaching line."
    monkeypatch.setattr(tools.plan_coach, "generate_coaching_line_cached", fake_line)

    payload, err = call(tools.generate_brief_report, {"date": d})
    assert not err, payload
    with pdfplumber.open(io.BytesIO(Path(payload["path"]).read_bytes())) as doc:
        text = "\n".join(p.extract_text() or "" for p in doc.pages)
    assert "omitted for space" in text
    # The count must match reality: headlines present + omitted == 5.
    stated = int(re.search(r"(\d+) further signals? omitted", text).group(1))
    assert stated == 5 - text.count("A headline of the length")


# --- chart window anchoring -------------------------------------------------

def test_fetch_metric_series_window_ends_on_the_given_date(seeded):
    """Regression: the window was anchored to date.today() with NO upper
    bound, so re-rendering an OLD brief drew charts running to today and could
    show data the brief's own prose never saw."""
    dates, _values = tools._fetch_metric_series("rhr", 3650, end="2026-07-10")
    assert dates, "fixture should have rhr rows in range"
    assert max(dates) <= "2026-07-10"


def test_fetch_metric_series_window_starts_days_before_end(seeded):
    dates, _values = tools._fetch_metric_series("rhr", 7, end="2026-07-10")
    assert min(dates) >= "2026-07-03"
    assert max(dates) <= "2026-07-10"


def test_fetch_metric_series_defaults_to_today_for_live_callers(seeded):
    """chart()/generate_chart() must keep their existing behavior."""
    dated = tools._fetch_metric_series("rhr", 3650, end=date.today().isoformat())
    default = tools._fetch_metric_series("rhr", 3650)
    assert dated == default
