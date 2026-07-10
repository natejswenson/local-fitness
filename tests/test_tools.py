"""Tests for agent/tools.py — the MCP tool handlers that query the DB.

The handlers are async and return ``{"content": [{"type": "text", "text": ...}]}``.
We call them directly against a seeded tmp DB (no SDK runtime, no network).
"""
from __future__ import annotations

import asyncio
import io
import json
import os
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
from local_fitness.agent import tools


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
    payload, err = call(tools.get_today_status, {})
    assert not err
    assert payload["recent_days"]
    assert payload["current_baseline"]["ctl"] == 40.0


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
    assert path.name == f"brief-{d}.pdf"
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


def test_generate_brief_report_and_generate_chart_excluded_from_all_tools():
    # INV-4: the two local-only tools are never registered in ALL_TOOLS —
    # only in LOCAL_ONLY_TOOLS.
    all_names = {t.name for t in tools.ALL_TOOLS}
    assert "generate_brief_report" not in all_names
    assert "generate_chart" not in all_names
    local_only_names = {t.name for t in tools.LOCAL_ONLY_TOOLS}
    assert local_only_names == {"generate_brief_report", "generate_chart"}


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

    async def fake_generate(profile, today_workout, last_7_days, adherence_pct, days_to_race, goal_type):
        calls.append((today_workout, adherence_pct, days_to_race, goal_type))
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
    assert "".join("Today: easy 2.49 mi @ 9:23/mi.".split()) in squashed


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
