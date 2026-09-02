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
import sqlite3
import subprocess
import tempfile
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pdfplumber
import pytest

from local_fitness import db, plans
from local_fitness.agent import (
    branding,
    card_store,
    interpret,
    journal,
    ledger,
    memory,
    report_card,
    tools,
    units,
    visuals,
    workout_coach,
)
from local_fitness.ingest import daily as daily_ingest_mod


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


def test_daily_snapshot_payload_shape(seeded):
    """Inherited from the removed get_today_status: the old raw
    {today, recent_days, current_baseline} shape is gone, replaced by
    assemble_status()'s richer payload."""
    payload, err = call(tools.daily_snapshot, {})
    assert not err
    assert "recent_days" not in payload
    assert "current_baseline" not in payload
    assert payload["training_load"]["ctl"] == 40.0
    assert payload["metrics"]
    assert payload["date"] == date.today().isoformat()


def test_get_today_status_is_gone(seeded):
    """0.48.0 removed it — byte-identical body to daily_snapshot, sharing one
    description constant, so the model coin-flipped between two names for one
    tool (16/5 across recorded sessions). Pinned so it can't drift back in."""
    assert not hasattr(tools, "get_today_status")
    assert "get_today_status" not in {t.name for t in tools.ALL_TOOLS}


def test_the_v1_brief_grant_still_matches_its_prompt(seeded):
    """The removal moved the V1 read-only grant from get_today_status to
    daily_snapshot. briefing_prompt names it as step 1, and a prompt that
    instructs a tool the loop was never granted fails SILENTLY — that exact
    mismatch went unnoticed for three weeks in 2026 (see _READ_ONLY_TOOL_NAMES).
    """
    from local_fitness.agent import prompts

    granted = set(tools.read_only_tool_names())
    assert "mcp__fitness__daily_snapshot" in granted
    assert "mcp__fitness__get_today_status" not in granted
    # Every tool the V1 prompt tells the loop to call must actually be granted.
    body = prompts.briefing_prompt("Nate")
    for name in ("daily_snapshot", "get_training_plan_status",
                 "training_load_status", "query_workouts", "get_metric_trend"):
        assert name in body, f"V1 prompt no longer names {name}"
        assert f"mcp__fitness__{name}" in granted, (
            f"V1 prompt instructs {name} but the loop is not granted it")


# 0.57.0: `get_metric` folded into `get_metric_trend` as include_values=true.
# These tests cover what the raw-series branch uniquely did: the rows, the
# *_seconds formatting, and the partial-day anchoring of the values list.


def test_trend_include_values_returns_the_raw_series(seeded):
    payload, err = call(tools.get_metric_trend,
                        {"metric": "rhr", "days": 14, "include_values": True})
    assert not err
    assert payload["metric"] == "rhr"
    assert payload["n_samples"] == len(payload["values"])
    assert all("value" in row and row["value"] is not None for row in payload["values"])
    # The trend stats still ride along — one call answers both questions.
    assert "slope_direction" in payload and "mean" in payload


def test_trend_without_include_values_has_no_series(seeded):
    payload, err = call(tools.get_metric_trend, {"metric": "rhr", "days": 14})
    assert not err
    assert "values" not in payload


def test_trend_values_cap_and_truncation_flag(seeded):
    # seeded has 40 daily rhr rows; a wide window with a tiny cap must keep
    # the MOST RECENT rows and say it cut the rest. rhr settles (0.59.0), so
    # a fresh covering pull is stamped to keep today's row in the series.
    _stamp_pull(seeded, completed_at=datetime.now().isoformat(),
                last_date_fetched=date.today().isoformat())
    real_cap = tools._TREND_MAX_VALUES
    try:
        tools._TREND_MAX_VALUES = 5
        payload, err = call(tools.get_metric_trend,
                            {"metric": "rhr", "days": 60, "include_values": True})
    finally:
        tools._TREND_MAX_VALUES = real_cap
    assert not err
    assert len(payload["values"]) == 5
    assert payload["values_truncated"] is True
    assert payload["values"][-1]["date"] == date.today().isoformat()


def test_trend_values_under_cap_carry_no_truncation_flag(seeded):
    payload, err = call(tools.get_metric_trend,
                        {"metric": "rhr", "days": 14, "include_values": True})
    assert not err
    assert "values_truncated" not in payload


def test_trend_values_format_seconds_metrics(seeded):
    # sleep_seconds must carry the "7h 33m" companion the coach voice speaks —
    # the model is explicitly forbidden from showing raw seconds.
    payload, err = call(tools.get_metric_trend,
                        {"metric": "sleep_seconds", "days": 5, "include_values": True})
    assert not err
    assert payload["values"]
    for row in payload["values"]:
        assert row["value_formatted"] == units.format_hm(row["value"])
        assert row["value_formatted"].endswith("m")


def test_trend_values_non_seconds_metric_has_no_formatted_field(seeded):
    payload, err = call(tools.get_metric_trend,
                        {"metric": "rhr", "days": 5, "include_values": True})
    assert not err
    assert all("value_formatted" not in row for row in payload["values"])


def test_get_metric_is_gone(seeded):
    """0.57.0 removed it — identical {metric, days} schema and anchor logic to
    get_metric_trend, 1 recorded call ever, and an unbounded raw dump (63 KB
    at days=3650). Pinned so it can't drift back in (the get_today_status
    pattern)."""
    assert not hasattr(tools, "get_metric")
    assert "get_metric" not in {t.name for t in tools.ALL_TOOLS}
    assert "get_metric" not in tools._READ_ONLY_TOOL_NAMES


def test_get_metric_trend(seeded):
    payload, err = call(tools.get_metric_trend, {"metric": "rhr", "days": 14})
    assert not err
    assert payload["n_samples"] > 0
    assert "current_vs_baseline_sd" in payload  # rhr is baseline-tracked


def test_get_metric_trend_unknown(seeded):
    payload, err = call(tools.get_metric_trend, {"metric": "nope", "days": 14})
    assert err
    assert "unknown metric 'nope'" in payload["error"]


def test_get_metric_trend_no_data(seeded):
    payload, err = call(tools.get_metric_trend, {"metric": "vo2_max", "days": 14})
    assert err
    # vo2_max is a REAL metric with no rows in the window — the message must
    # say "no data", not "unknown metric", or the two failures are the same
    # to a caller trying to work out what went wrong.
    assert payload["error"] == "no data in window"


# --------------------------------------------------------------------------- #
# Fix 8: get_metric / get_metric_trend anchor PARTIAL_DAY_METRICS windows on
# yesterday, not today — a same-day running tally (avg_stress, steps, ...) is
# partial all day, so a "trend" computed against it is misleading. Measured
# live: get_metric_trend("avg_stress", 7) returned current=17, slope_direction
# "flat" off a 50-sample overnight-only reading, when the honest read on 7
# COMPLETE days was rising (every complete day that week ran 24-32).
# --------------------------------------------------------------------------- #
def test_get_metric_trend_reads_rising_not_flat_when_today_is_a_partial_low_read(
    tmp_path, monkeypatch
):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    db.init_schema(p)
    today = date.today()
    with db.connect(p) as conn:
        # Today: partial overnight-only stress read (17). Prior 7 days: a
        # clean, real rising trend (24 -> 32... well within TREND_FLAT_SD).
        conn.execute(
            "INSERT INTO daily_metrics (date, avg_stress) VALUES (?, ?)",
            (today.isoformat(), 17),
        )
        for i, stress in enumerate((18, 20, 22, 24, 27, 30, 32)):
            d = (today - timedelta(days=7 - i)).isoformat()
            conn.execute("INSERT INTO daily_metrics (date, avg_stress) VALUES (?, ?)", (d, stress))
    payload, err = call(tools.get_metric_trend, {"metric": "avg_stress", "days": 7})
    assert not err
    assert payload["current"] == 32  # yesterday's real value, not today's partial 17
    assert payload["slope_direction"] == "rising"  # not "flat"
    assert payload["partial_today_excluded"] is True
    assert payload["n_samples"] == 7  # 7 COMPLETE days, today never counted


def test_get_metric_trend_non_partial_metric_unaffected(seeded):
    # rhr is not in PARTIAL_DAY_METRICS — no flag, today's row still counts.
    payload, err = call(tools.get_metric_trend, {"metric": "rhr", "days": 14})
    assert not err
    assert "partial_today_excluded" not in payload


def test_trend_values_exclude_todays_partial_reading_for_steps(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    db.init_schema(p)
    today = date.today()
    with db.connect(p) as conn:
        # A tiny partial today's-steps reading that would otherwise drag the
        # "recent values" list and its baseline comparison down.
        conn.execute(
            "INSERT INTO daily_metrics (date, steps) VALUES (?, ?)", (today.isoformat(), 400)
        )
        for i, steps in enumerate((10500, 11000)):
            conn.execute(
                "INSERT INTO daily_metrics (date, steps) VALUES (?, ?)",
                ((today - timedelta(days=2 - i)).isoformat(), steps),
            )
    payload, err = call(tools.get_metric_trend,
                        {"metric": "steps", "days": 5, "include_values": True})
    assert not err
    assert payload["partial_today_excluded"] is True
    dates = [v["date"] for v in payload["values"]]
    assert today.isoformat() not in dates  # today's partial 400 never listed
    assert payload["values"][-1]["value"] == 11000


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
    assert err
    assert payload["error"] == "no data in window"   # not "unknown metric"


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
    # 0.37.0 envelope: {workouts, count, truncated}, and the native filter
    # unit is MILES (min_distance_mi) — the km alias stays accepted below.
    payload, err = call(
        tools.query_workouts,
        {"activity_type": "run", "days": 30, "min_distance_mi": 3.1, "min_duration_min": 10, "limit": 10},
    )
    assert not err
    assert payload["count"] == 1
    assert payload["truncated"] is False
    assert payload["workouts"][0]["activity_id"] == 1


def test_query_workouts_no_filters(seeded):
    payload, err = call(tools.query_workouts, {})
    assert not err
    assert payload["count"] >= 1
    assert len(payload["workouts"]) == payload["count"]


def test_get_workout_detail_found(seeded):
    payload, err = call(tools.get_workout_detail, {"activity_id": 1})
    assert not err
    assert payload["activity"]["activity_name"] == "Morning Run"
    assert "raw_json" not in payload["activity"]
    assert payload["hr_zones"] and payload["splits"]


def test_get_workout_detail_missing(seeded):
    payload, err = call(tools.get_workout_detail, {"activity_id": 999})
    assert err
    assert payload["error"] == "activity not found"


# --------------------------------------------------------------------------- #
# Fix 3: measured `effort` (run/walk/null) on workout payloads.
#
# Garmin's activity_type label lies — walking-desk sessions log as
# treadmill_running (documented in CLAUDE.md) — so `effort` is derived from
# pace via interpret.is_running_effort, never from the label. Pinned against
# live-shaped values: 1090 sec/km ≈ 29:15/mi is a real walking-desk row.
# --------------------------------------------------------------------------- #
def test_augment_workout_effort_walk_for_slow_pace():
    w = tools._augment_workout({"avg_pace_sec_per_km": 1090.0})
    assert w["effort"] == "walk"


def test_augment_workout_effort_run_for_fast_pace():
    w = tools._augment_workout({"avg_pace_sec_per_km": 333.0})
    assert w["effort"] == "run"


def test_augment_workout_effort_null_when_paceless():
    w = tools._augment_workout({"avg_pace_sec_per_km": None})
    assert w["effort"] is None


def _insert_effort_activities(conn, today):
    """Three activities all labelled 'treadmill_running' — mislabeled per
    CLAUDE.md's documented Garmin quirk — distinguished only by measured
    pace: a real walk (1090 sec/km), a real run (333 sec/km), and a paceless
    row (pace column NULL)."""
    rows = [
        (1, (today - timedelta(days=1)).isoformat(), 1090.0),  # walk
        (2, (today - timedelta(days=2)).isoformat(), 333.0),   # run
        (3, (today - timedelta(days=3)).isoformat(), None),    # paceless
    ]
    for aid, d, pace in rows:
        conn.execute(
            "INSERT INTO activities (activity_id, date, start_time, activity_type, "
            "distance_meters, avg_pace_sec_per_km, duration_seconds) "
            "VALUES (?, ?, ?, 'treadmill_running', 5000, ?, 1800)",
            (aid, d, d + "T07:00:00", pace),
        )


def test_query_workouts_effort_field_pins_walk_run_null(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    db.init_schema(p)
    today = date.today()
    with db.connect(p) as conn:
        _insert_effort_activities(conn, today)

    payload, err = call(tools.query_workouts, {})
    assert not err
    by_id = {w["activity_id"]: w for w in payload["workouts"]}
    assert by_id[1]["effort"] == "walk"
    assert by_id[2]["effort"] == "run"
    assert by_id[3]["effort"] is None
    # Additive, never filtered: all three (including the walk) still present.
    assert by_id[1]["activity_type"] == "treadmill_running"


def test_get_workout_detail_effort_field_pins_walk_and_run(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    db.init_schema(p)
    today = date.today()
    with db.connect(p) as conn:
        _insert_effort_activities(conn, today)

    walk_payload, err1 = call(tools.get_workout_detail, {"activity_id": 1})
    run_payload, err2 = call(tools.get_workout_detail, {"activity_id": 2})
    paceless_payload, err3 = call(tools.get_workout_detail, {"activity_id": 3})
    assert not err1 and not err2 and not err3
    assert walk_payload["activity"]["effort"] == "walk"
    assert run_payload["activity"]["effort"] == "run"
    assert paceless_payload["activity"]["effort"] is None


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
    payload, err = call(
        tools.compare_periods,
        {"metric": "xyz", "period_a_start": "2026-01-01", "period_a_end": "2026-01-02",
         "period_b_start": "2026-01-03", "period_b_end": "2026-01-04"},
    )
    assert err
    assert "unknown metric 'xyz'" in payload["error"]


def test_find_anomalies(seeded):
    payload, err = call(tools.find_anomalies, {"metric": "rhr", "sd_threshold": 0.5})
    assert not err
    assert payload["metric"] == "rhr"
    assert isinstance(payload["anomalies"], list)


def test_find_anomalies_unsupported_metric(seeded):
    payload, err = call(tools.find_anomalies, {"metric": "steps"})
    assert err
    # steps is a REAL metric that simply isn't baseline-tracked — the message
    # must say that, not "unknown metric".
    assert "only baseline-tracked metrics supported" in payload["error"]


def test_training_load_status(seeded):
    payload, err = call(tools.training_load_status, {})
    assert not err
    assert payload["current"]["ctl"] == 40.0


def test_training_load_status_empty(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    payload, err = call(tools.training_load_status, {})
    assert err
    # An EMPTY db, not a broken one — the message has to send the user to the
    # fix (sync) rather than read as a generic failure.
    assert "no training-load data yet" in payload["error"]
    assert "sync_garmin_data" in payload["error"]


# --------------------------------------------------------------------------- #
# Fix 9: "current" TSB/CTL/ATL is the last COMPLETE day, never today's own
# same-day projection. baselines.recompute walks the EWMA forward assuming
# today's training_load is 0 until something posts, so reporting today's row
# as "current form" pre-credits a zero-load rest day that hasn't happened.
# Regression test per spec: insert a synthetic activity dated "today" — the
# reported CURRENT tsb must NOT change (this failed before the fix: logging
# an activity recomputes today's baselines row and the OLD code read it
# straight through as "current").
# --------------------------------------------------------------------------- #
def test_training_load_status_current_unaffected_by_logging_todays_activity(
    tmp_path, monkeypatch
):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    with db.connect(p) as conn:
        # Yesterday's row is "current form" — stable, complete-day CTL/ATL/TSB.
        conn.execute(
            "INSERT INTO baselines (date, ctl, atl, tsb) VALUES (?, 59.53, 72.27, -12.74)",
            (yesterday,),
        )
        # Today's row assumes ZERO load so far (typical morning-read shape) —
        # a materially different, lower TSB (more fatigued-looking).
        conn.execute(
            "INSERT INTO baselines (date, ctl, atl, tsb) VALUES (?, 59.10, 81.90, -22.80)",
            (today,),
        )
    before, err1 = call(tools.training_load_status, {})
    assert not err1
    assert before["current"]["tsb"] == -12.74  # yesterday's, not today's -22.80

    with db.connect(p) as conn:
        # Now "log" today's activity by recomputing today's row with real load
        # (what sync_garmin_data + baselines.recompute would do).
        conn.execute(
            "UPDATE baselines SET ctl = 59.80, atl = 76.40, tsb = -16.60 WHERE date = ?",
            (today,),
        )
    after, err2 = call(tools.training_load_status, {})
    assert not err2
    # The reported CURRENT tsb is unchanged — still yesterday's stable read,
    # completely unaffected by today's activity being logged or not.
    assert after["current"]["tsb"] == before["current"]["tsb"] == -12.74
    assert after["current"]["ctl"] == before["current"]["ctl"] == 59.53


def test_training_load_status_exposes_projected_end_of_day(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    with db.connect(p) as conn:
        conn.execute(
            "INSERT INTO baselines (date, ctl, atl, tsb) VALUES (?, 59.53, 72.27, -12.74)",
            (yesterday,),
        )
        conn.execute(
            "INSERT INTO baselines (date, ctl, atl, tsb) VALUES (?, 59.10, 81.90, -22.80)",
            (today,),
        )
    payload, err = call(tools.training_load_status, {})
    assert not err
    assert payload["current"]["tsb"] == -12.74
    assert payload["projected_end_of_day"]["tsb"] == -22.8
    assert payload["projected_end_of_day"]["ctl"] == 59.1
    assert payload["projected_end_of_day"]["interpretation"] == "very fatigued"


def test_training_load_status_no_projection_when_today_has_no_row(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    with db.connect(p) as conn:
        conn.execute(
            "INSERT INTO baselines (date, ctl, atl, tsb) VALUES (?, 40.0, 45.0, -5.0)",
            (yesterday,),
        )
    payload, err = call(tools.training_load_status, {})
    assert not err
    assert payload["current"]["tsb"] == -5.0
    assert payload["projected_end_of_day"] is None  # no baselines row for today


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
    payload, err = call(tools.correlate, {"metric_a": "foo", "metric_b": "rhr", "days": 30})
    assert err
    assert "metrics must be daily numeric" in payload["error"]


def test_correlate_insufficient(seeded):
    payload, err = call(tools.correlate, {"metric_a": "sleep_seconds", "metric_b": "rhr", "days": 2})
    assert err
    assert "insufficient paired data" in payload["error"]  # NOT 'metrics must be daily numeric' — both metrics are valid


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
    # is "normal", not "suppressed"). Fresh covering pull stamped so today's
    # settling rhr counts (0.59.0).
    _stamp_pull(seeded, completed_at=datetime.now().isoformat(),
                last_date_fetched=date.today().isoformat())
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
    payload, err = call(tools.find_anomalies, {"metric": "rhr", "sd_threshold": 0.5})
    assert not err
    assert payload["anomalies"]
    for row in payload["anomalies"]:
        expected = round((row["value"] - row["baseline_mean"]) / row["baseline_sd"], 2)
        assert row["sd_distance"] == expected
        assert row["direction"] in ("above", "below")


def test_find_anomalies_sleep_formats_and_rounds(seeded):
    # sleep_seconds anomalies must carry the "7h 33m" companions the coach
    # voice speaks, and the raw AVG()/SD baseline floats must be rounded at the
    # payload boundary (pre-fix they passed through as ~10-digit floats and the
    # value stayed raw seconds).
    # On yesterday, not today — today is always excluded from the scan
    # (0.59.0: sleep settles through the morning).
    target = (date.today() - timedelta(days=1)).isoformat()
    with db.connect(seeded) as conn:
        # A clear low-sleep anomaly against a fabricated non-round baseline.
        conn.execute("UPDATE daily_metrics SET sleep_seconds=? WHERE date=?",
                     (18000, target))
        conn.execute(
            "UPDATE baselines SET sleep_seconds_60day_mean=?, sleep_seconds_60day_sd=? "
            "WHERE date=?",
            (26784.333333333, 3600.6666666, target),
        )
    payload, err = call(tools.find_anomalies, {"metric": "sleep_seconds", "sd_threshold": 1.0})
    assert not err
    assert payload["anomalies"]
    row = next(r for r in payload["anomalies"] if r["date"] == target)
    assert row["value_formatted"] == units.format_hm(18000)      # "5h 00m"
    assert row["baseline_formatted"] == units.format_hm(26784.333333333)
    assert row["baseline_mean"] == 26784.33                       # rounded 2dp
    assert row["baseline_sd"] == 3600.67
    assert row["direction"] == "below"


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
        # Fix 9: "now" is current_form (last COMPLETE day), matching what
        # training_load_status's own "current" now anchors on.
        current_form = bp.status_mod._baseline_row_before(conn, today.isoformat())
        sig = bp._compute_signals(conn, today.isoformat(), baseline, current_form, 10000, None, None)
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


# These three guard the SQL boundary, and each must fail for its OWN reason.
# They asserted only `assert err` until 0.47.0 — so a run_sql that rejected
# EVERY query, including valid SELECTs, would have passed all three. Pinning
# the error text is what makes them distinguish "rejected correctly" from
# "rejected for the wrong reason" (and from "rejected everything").


def test_run_sql_rejects_non_select(seeded):
    payload, err = call(tools.run_sql, {"query": "DELETE FROM daily_metrics"})
    assert err
    assert "only SELECT/WITH queries permitted" in payload["error"]


def test_run_sql_rejects_forbidden_keyword(seeded):
    """A statement that STARTS with WITH clears the first gate, so the keyword
    denylist is what has to catch it."""
    payload, err = call(tools.run_sql,
                        {"query": "WITH x AS (SELECT 1) UPDATE settings SET value='x'"})
    assert err
    assert "forbidden keyword: update" in payload["error"]


def test_run_sql_bad_query(seeded):
    payload, err = call(tools.run_sql, {"query": "SELECT * FROM does_not_exist"})
    assert err
    # Reached sqlite and failed there — NOT stopped by either guard above.
    assert "no such table: does_not_exist" in payload["error"]


def test_run_sql_accepts_a_valid_select(seeded):
    """The other half of the boundary, and the one whose absence let the three
    tests above pass a reject-everything implementation."""
    payload, err = call(tools.run_sql, {"query": "SELECT 1 AS n"})
    assert not err, payload
    assert payload["rows"] == [{"n": 1}]


def test_run_sql_bad_table_points_at_schema_resource(seeded):
    # Regression: a mistyped table/column raises sqlite3.OperationalError —
    # the schema-resource pointer must fire on that REAL path, not only on
    # the exotic sqlite3.Error branch (Phase-5 live gate caught the pointer
    # living solely in the unreachable branch).
    payload, err = call(tools.run_sql, {"query": "SELECT * FROM does_not_exist"})
    assert err
    assert "fitness://schema" in payload["error"]
    assert "operational error" not in payload["error"]


def test_run_sql_truncates_and_flags_at_row_cap(seeded):
    # A cross join yields far more than the 500-row cap. The result must be
    # clipped to exactly 500 AND carry truncated + a hint, so the model never
    # reads a clipped set as complete (the pre-fix bug: silent fetchmany(500)).
    payload, err = call(tools.run_sql, {
        "query": "SELECT a.date FROM daily_metrics a, daily_metrics b LIMIT 501"
    })
    assert not err
    assert payload["count"] == 500
    assert len(payload["rows"]) == 500
    assert payload["truncated"] is True
    assert "LIMIT" in payload["hint"]


def test_run_sql_exactly_at_cap_is_not_flagged_truncated(seeded):
    # Exactly 500 matched rows is a COMPLETE result — fetching 501 lets the tool
    # tell that from a clipped larger set, so no truncated flag fires at the edge.
    payload, err = call(tools.run_sql, {
        "query": "SELECT a.date FROM daily_metrics a, daily_metrics b LIMIT 500"
    })
    assert not err
    assert payload["count"] == 500
    assert "truncated" not in payload


# --- day-window robustness: over-large N must be a clean _err, not OverflowError ---

_BIG = 10**9  # timedelta(days=N) raises OverflowError around here


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
    assert len(saved["handle"]) == 8
    assert "line" not in saved  # the old index field must not ride along the new one
    listed, err = call(tools.list_user_notes, {})
    assert not err
    assert listed["count"] == 1
    assert listed["notes"][0]["text"] == "lead with the workout card"
    assert listed["notes"][0]["handle"] == saved["handle"]
    assert "line" not in listed["notes"][0]


def test_save_user_note_empty(seeded):
    payload, err = call(tools.save_user_note, {"note": "   "})
    assert err
    assert "note text is required" in payload["error"]


def test_update_user_note(seeded):
    saved, _ = call(tools.save_user_note, {"note": "old"})
    updated, err = call(tools.update_user_note, {"handle": saved["handle"], "note": "new"})
    assert not err
    assert updated["text"] == "new"
    assert updated["duplicates"] == 1
    # The handle changes with the content — the caller's old handle is stale.
    assert updated["handle"] != saved["handle"]


def test_update_user_note_handle_is_normalised(seeded):
    saved, _ = call(tools.save_user_note, {"note": "old"})
    bracketed = f"  [{saved['handle'].upper()}]  "
    updated, err = call(tools.update_user_note, {"handle": bracketed, "note": "new"})
    assert not err
    assert updated["text"] == "new"


def test_update_user_note_bad_handle(seeded):
    """Three DIFFERENT rejections. Asserting only `assert err` three times
    could not tell them apart — nor tell any of them from an
    update_user_note that refused everything."""
    payload, err = call(tools.update_user_note, {"handle": "", "note": "x"})
    assert err
    assert "handle is required" in payload["error"]
    saved, _ = call(tools.save_user_note, {"note": "old"})
    payload, err = call(tools.update_user_note, {"handle": saved["handle"], "note": ""})
    assert err
    assert "new note text is required" in payload["error"]   # the TEXT, not the handle
    payload, err = call(tools.update_user_note, {"handle": "deadbeef", "note": "x"})
    assert err
    assert "no note with handle 'deadbeef'" in payload["error"]  # the HANDLE, not the text


def test_delete_user_note(seeded):
    saved, _ = call(tools.save_user_note, {"note": "drop me"})
    deleted, err = call(tools.delete_user_note, {"handle": saved["handle"]})
    assert not err and deleted["deleted"]
    assert deleted["duplicates"] == 1


def test_delete_user_note_bad_handle(seeded):
    payload, err = call(tools.delete_user_note, {"handle": ""})
    assert err
    assert "handle is required" in payload["error"]      # missing argument
    payload, err = call(tools.delete_user_note, {"handle": "deadbeef"})
    assert err
    assert "no note with handle 'deadbeef'" in payload["error"]  # argument fine, row absent


def test_a_delete_no_longer_redirects_a_later_update(seeded):
    """The issue's headline, exercised over the real tool handlers rather
    than notes.py directly. On dev this silently rewrote the fourth note
    when the caller's stale line index shifted after the delete."""
    handles = []
    for t in ("zero", "one", "two", "three"):
        saved, _ = call(tools.save_user_note, {"note": t})
        handles.append(saved["handle"])
    deleted, err = call(tools.delete_user_note, {"handle": handles[1]})
    assert not err and deleted["deleted"]
    updated, err = call(tools.update_user_note, {"handle": handles[2], "note": "REWRITTEN"})
    assert not err
    listed, _ = call(tools.list_user_notes, {})
    texts = {n["text"] for n in listed["notes"]}
    assert texts == {"zero", "REWRITTEN", "three"}


def test_update_user_note_stale_handle_is_a_loud_error_not_a_silent_edit(seeded, tmp_path):
    """Two-sided half of the above: a good handle writes, a dead one
    refuses — and the file is untouched by the refusal, byte for byte."""
    saved, _ = call(tools.save_user_note, {"note": "original"})
    call(tools.update_user_note, {"handle": saved["handle"], "note": "rewritten"})
    notes_path = tmp_path / "user_notes.md"
    before = notes_path.read_bytes()
    payload, err = call(tools.update_user_note, {"handle": saved["handle"], "note": "should not land"})
    assert err
    assert saved["handle"] in payload["error"]
    assert notes_path.read_bytes() == before  # the refusal must not touch the file at all
    listed, _ = call(tools.list_user_notes, {})
    assert listed["notes"][0]["text"] == "rewritten"


def test_duplicate_handle_converges_over_the_real_tools(seeded, tmp_path):
    """A hand-edited pair sharing timestamp and text — the tool reports
    the duplicate count and rewrites the first, and the surviving pair is
    uniquely addressable again right after."""
    notes_path = tmp_path / "user_notes.md"
    notes_path.write_text(
        "- 2026-01-01T00:00:00 — same text twice\n"
        "- 2026-01-01T00:00:00 — same text twice\n",
        encoding="utf-8",
    )
    listed, _ = call(tools.list_user_notes, {})
    dup_handle = listed["notes"][0]["handle"]
    updated, err = call(tools.update_user_note, {"handle": dup_handle, "note": "now distinct"})
    assert not err
    assert updated["duplicates"] == 2
    listed, _ = call(tools.list_user_notes, {})
    texts = [n["text"] for n in listed["notes"]]
    assert texts.count("same text twice") == 1
    assert texts.count("now distinct") == 1


def test_list_user_notes_ranks_a_refreshed_note_first(seeded, tmp_path):
    # Repro 6, over the real tool handlers: refining an older note must
    # not leave it stuck behind a newer, untouched one just because it
    # sits earlier in the file. The refreshed note is the MIDDLE bullet on
    # disk (file position 1 of 3), deliberately not position 0: at position
    # 0, on-disk (oldest-first) order and the newest-first ranking agree by
    # coincidence, so the test cannot tell a real ranking fix from a
    # reversion to a bare `notes.read_notes()` (round-1 review finding —
    # this exact fixture passed unchanged against that mutant). All three
    # bullets start with a real past timestamp so the update's now()
    # unambiguously outranks them, rather than depending on timing.
    notes_path = tmp_path / "user_notes.md"
    notes_path.write_text(
        "- 2026-01-01T08:00:00 — OLDEST note, untouched\n"
        "- 2026-01-15T08:00:00 — MIDDLE note, will be refreshed\n"
        "- 2026-02-01T08:00:00 — NEWEST conflicting note, untouched\n",
        encoding="utf-8",
    )
    listed, _ = call(tools.list_user_notes, {})
    middle_handle = next(
        n["handle"]
        for n in listed["notes"]
        if n["text"] == "MIDDLE note, will be refreshed"
    )
    updated, err = call(
        tools.update_user_note,
        {"handle": middle_handle, "note": "MIDDLE note, but just refreshed today"},
    )
    assert not err
    listed, _ = call(tools.list_user_notes, {})
    assert listed["notes"][0]["text"] == "MIDDLE note, but just refreshed today"
    assert listed["notes"][0]["handle"] == updated["handle"]
    # On-disk order still has the refreshed note in the middle — only the
    # returned ranking changed. A bare `notes.read_notes()` reversion would
    # return "OLDEST note, untouched" first instead.
    texts = [n["text"] for n in listed["notes"]]
    assert texts[1:] == [
        "NEWEST conflicting note, untouched",
        "OLDEST note, untouched",
    ]


def test_render_for_prompt_handles_resolve_through_the_real_tools(seeded):
    """Correction 2: the prompt path is a second live entry point — every
    handle rendered into the prompt must be one the tools can act on."""
    from local_fitness import notes as notes_mod
    for t in ("alpha", "beta", "gamma"):
        call(tools.save_user_note, {"note": t})
    rendered_handles = {
        line.split("]", 1)[0].lstrip("[")
        for line in notes_mod.render_for_prompt().splitlines()
    }
    listed, _ = call(tools.list_user_notes, {})
    assert rendered_handles == {n["handle"] for n in listed["notes"]}
    for h in rendered_handles:
        _, err = call(tools.update_user_note, {"handle": h, "note": "touched"})
        assert not err


def test_server_and_tool_names():
    server = tools.make_server()
    # Pin what the server IS, not merely that one was returned — an
    # `is not None` here would pass on any object at all.
    assert server["name"] == tools.SERVER_NAME == "fitness"
    names = tools.allowed_tool_names()
    assert len(names) == len(tools.ALL_TOOLS)
    assert all(n.startswith("mcp__fitness__") for n in names)


def test_server_version_tracks_pyproject():
    """Every MCP client reads this in `serverInfo` — it is the version you look
    at in Claude Desktop to decide whether a fix has shipped.

    It was a hardcoded "0.6.0" from the server's first commit, so by 0.44.0 it
    was 38 releases stale and silently wrong. Compare against pyproject.toml
    rather than against `importlib.metadata` (which is what the code reads —
    asserting one against itself would be a tautology that passes no matter
    what either says)."""
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]

    assert tools.server_version() == declared
    assert tools.server_version() != "0.6.0", "the old hardcoded literal is back"
    # The instance attribute is what the SDK actually puts in the `serverInfo`
    # of the initialize response — `make_server()`'s dict has no version key,
    # so asserting on the dict would have compared None to None-ish forever.
    assert tools.make_server()["instance"].version == declared


def test_package_dunder_version_tracks_pyproject():
    """`local_fitness.__version__` is the same trap server_version() escaped:
    it sat at a literal "0.4.0" while pyproject reached 0.55.0, because nothing
    imported it and nothing tested it. Same fix (importlib.metadata), same
    non-tautological check — compare against pyproject.toml, the declared
    source, not against the metadata call the code itself makes."""
    import tomllib
    from pathlib import Path

    import local_fitness

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]

    assert local_fitness.__version__ == declared
    assert local_fitness.__version__ != "0.4.0", "the old hardcoded literal is back"


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


def test_list_observations_caps_at_limit_and_flags_truncated(seeded):
    # The daily-logging surface is unbounded without a cap: log 3, ask for 2,
    # and the newest 2 come back flagged truncated (pre-fix: SELECT * dumped all
    # 3 with no LIMIT and no signal).
    for w in (160, 161, 162):
        call(tools.log_observation, {"obs_type": "weight", "value": w})
    listed, err = call(tools.list_observations, {"limit": 2})
    assert not err
    assert listed["count"] == 2
    assert listed["truncated"] is True
    # ORDER BY observation_id DESC — the newest two, not an arbitrary two.
    assert [o["value_num"] for o in listed["observations"]] == [162, 161]


def test_list_observations_under_cap_is_not_flagged(seeded):
    # A result that fits the cap is complete — no truncated flag, even at the
    # exact edge (fetch limit+1 distinguishes a full page from a clipped one).
    for w in (160, 161):
        call(tools.log_observation, {"obs_type": "weight", "value": w})
    listed, err = call(tools.list_observations, {"limit": 2})
    assert not err
    assert listed["count"] == 2
    assert "truncated" not in listed


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
    assert "must be a valid YYYY-MM-DD" in _payload["error"]
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
    """The guardrail rejection and the not-found rejection are different
    things, and this test could not distinguish them until 0.47.0 — so a
    delete_manual_workout that had DROPPED the Garmin-data protection and
    merely returned "not found" for everything would have passed.

    Manual workouts carry negative ids; anything >= 0 came from Garmin and
    must never be deletable through this tool."""
    for garmin_id in (1, 0):
        payload, err = call(tools.delete_manual_workout, {"activity_id": garmin_id})
        assert err
        assert "refusing to delete non-manual activity" in payload["error"], (
            f"id {garmin_id} was not refused BY THE GUARDRAIL: {payload['error']}")
    # A negative id clears the guardrail and fails for the other reason.
    payload, err = call(tools.delete_manual_workout, {"activity_id": -99})
    assert err
    assert payload["error"] == "no manual workout at id -99"


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
    is a strict subset of all tools and includes no WRITE tool, so brief
    generation cannot mutate data.

    `daily_snapshot` used to be excluded here too, but never because it writes
    — it doesn't. It was held out (0.47.0 and earlier) purely to keep the V1
    tool set byte-identical while it and `get_today_status` both existed. With
    the duplicate removed in 0.48.0 the V1 prompt names `daily_snapshot` as
    step 1, so it has to be granted; see
    test_the_v1_brief_grant_still_matches_its_prompt. `list_observations` stays
    out — it is still not part of the V1 brief's read set."""
    ro = set(tools.read_only_tool_names())
    for w in (
        "log_manual_workout", "delete_manual_workout", "log_observation",
        "delete_observation", "save_user_note", "update_user_note",
        "delete_user_note", "list_observations", "sync_garmin_data",
        "update_plan_workout", "update_plan_workouts", "propose_training_plan",
        "commit_training_plan", "abandon_active_plan", "save_brief",
        "save_coach_memory", "delete_coach_memory", "update_coach_personality",
    ):
        assert f"mcp__{tools.SERVER_NAME}__{w}" not in ro, f"{w} is a write"
    assert ro < set(tools.allowed_tool_names())


# --- sync_garmin_data --------------------------------------------------------

def test_sync_garmin_data_success_recomputes_baselines(seeded, monkeypatch):
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

    monkeypatch.setattr(daily_ingest_mod, "pull", fake_pull)
    monkeypatch.setattr(tools.baselines_mod, "recompute", fake_recompute)

    payload, err = call(tools.sync_garmin_data, {})
    assert not err
    assert payload["status"] == "success"
    assert payload["days_pulled"] == 2
    # Bite-sized cap wired through, and baselines recomputed because new data
    # actually landed.
    assert calls["max_days"] == tools.SYNC_MAX_DAYS
    assert calls["lookback_days"] == 90
    assert payload["sync_state"] == (
        "success — synced 2 day(s), 1 activity row(s) through 2026-07-06; "
        "baselines recomputed"
    )


def test_sync_garmin_data_skipped_does_not_recompute(seeded, monkeypatch):
    monkeypatch.setattr(
        daily_ingest_mod, "pull",
        lambda **_: {
            "status": "skipped", "days_pulled": 0, "activities_loaded": 0,
            "last_date": "2026-07-06", "error": None,
            "gap_days_remaining": 0, "deferred_count": 0, "days_failed": 0,
        },
    )
    monkeypatch.setattr(
        tools.baselines_mod, "recompute",
        lambda **_: pytest.fail("recompute should not run when nothing landed"),
    )
    payload, err = call(tools.sync_garmin_data, {})
    assert not err
    assert payload["status"] == "skipped"
    assert payload["days_pulled"] == 0
    assert payload["sync_state"] == "skipped — already up to date; baselines unchanged"


def test_sync_garmin_data_partial_is_not_an_error_and_recomputes(seeded, monkeypatch):
    """A pull that landed real days is a success for the caller's purposes.

    daily.pull reports `partial` whenever ANY gap remains back to
    EARLIEST_BACKFILL_DATE, so one missing historical day used to make every
    sync return is_error AND skip the recompute — fresh workouts in the DB,
    CTL/ATL/TSB frozen, downstream tools claiming currency.
    """
    calls = {}
    monkeypatch.setattr(
        daily_ingest_mod, "pull",
        lambda **_: {
            "status": "partial", "days_pulled": 3, "activities_loaded": 0,
            "last_date": "2026-07-25",
            "error": "5 day(s) still missing; 2 day(s) failed: 2024-01-02,2024-01-03",
            "gap_days_remaining": 5, "deferred_count": 3, "days_failed": 2,
        },
    )
    monkeypatch.setattr(
        tools.baselines_mod, "recompute",
        lambda **kw: calls.setdefault("recompute", []).append(kw["lookback_days"]),
    )

    payload, err = call(tools.sync_garmin_data, {})

    assert not err
    assert payload["status"] == "partial"
    assert payload["days_failed"] == 2
    assert payload["deferred_count"] == 3
    assert payload["gap_days_remaining"] == 5
    assert calls["recompute"] == [90]  # exactly once
    assert payload["sync_state"] == (
        "partial — synced 3 day(s) through 2026-07-25; "
        "3 day(s) deferred, 2 day(s) failed, 5 day(s) still missing; "
        "baselines recomputed"
    )


def test_sync_garmin_data_recomputes_when_only_activities_landed(seeded, monkeypatch):
    # A ride/run can be written by _ingest_activity_range without any new
    # wellness day, so days_pulled alone is the wrong recompute trigger.
    calls = {}
    monkeypatch.setattr(
        daily_ingest_mod, "pull",
        lambda **_: {
            "status": "partial", "days_pulled": 0, "activities_loaded": 2,
            "last_date": None, "error": "1 day(s) still missing",
            "gap_days_remaining": 1, "deferred_count": 0, "days_failed": 0,
        },
    )
    monkeypatch.setattr(
        tools.baselines_mod, "recompute",
        lambda **kw: calls.setdefault("recompute", []).append(kw["lookback_days"]),
    )

    payload, err = call(tools.sync_garmin_data, {})

    assert not err
    assert calls["recompute"] == [90]
    assert payload["sync_state"] == (
        "partial — synced 2 activity row(s); 1 day(s) still missing; "
        "baselines recomputed"
    )


@pytest.mark.parametrize("status", ["auth_failure", "not_configured", "failure", "interrupted"])
def test_sync_garmin_data_hard_failures_are_errors(seeded, monkeypatch, status):
    monkeypatch.setattr(
        daily_ingest_mod, "pull",
        lambda **_: {
            "status": status, "days_pulled": 0, "activities_loaded": 0,
            "last_date": None, "error": f"mfa_required: {status} detail",
            "gap_days_remaining": 4, "deferred_count": 0, "days_failed": 0,
        },
    )
    monkeypatch.setattr(
        tools.baselines_mod, "recompute",
        lambda **_: pytest.fail("recompute should not run when nothing landed"),
    )
    payload, err = call(tools.sync_garmin_data, {})
    assert err
    assert payload["status"] == status
    assert payload["error"] == f"mfa_required: {status} detail"
    assert payload["days_failed"] == 0
    assert payload["days_pulled"] == 0
    assert "sync_state" not in payload


def test_sync_garmin_data_hard_failure_without_error_string_still_errors(seeded, monkeypatch):
    # `interrupted` can close a run with error=None; the tool must not fall
    # through to a success payload just because the string is empty.
    monkeypatch.setattr(
        daily_ingest_mod, "pull",
        lambda **_: {
            "status": "interrupted", "days_pulled": 0, "activities_loaded": 0,
            "last_date": None, "error": None,
            "gap_days_remaining": 2, "deferred_count": 0, "days_failed": 0,
        },
    )
    monkeypatch.setattr(
        tools.baselines_mod, "recompute",
        lambda **_: pytest.fail("recompute should not run when nothing landed"),
    )
    payload, err = call(tools.sync_garmin_data, {})
    assert err
    assert payload["error"] == "Garmin sync failed (interrupted)"


def test_sync_garmin_data_is_in_full_tool_set():
    assert f"mcp__{tools.SERVER_NAME}__sync_garmin_data" in tools.allowed_tool_names()


# --- PDF/chart tools: generate_brief_report / chart's png format -----------
# NB: LOCAL_ONLY_TOOLS is generate_brief_report + workout_report_card; since
# Fix A (2026-07-13; folded into chart as format="png" at 0.57.0) — the png
# render lives in ALL_TOOLS (it returns an inline
# image block, so a remote caller no longer needs the local file path).


def test_fetch_metric_series_matches_chart_tool_output(seeded):
    # Regression guard for the shared-fetch extraction: the shared
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


def test_render_tag_hashes_inputs_not_renderer_output(monkeypatch):
    # PDFs tag on the render's logical INPUTS because WeasyPrint's byte
    # stream is not reproducible (2026-07-23: identical HTML diverged on
    # ~50% of paired Linux renders). The tag must be: stable across calls,
    # order-canonical for dicts, sensitive to every input part, and
    # sensitive to the brand theme and app version (a layout change must
    # not refocus a stale-looking window).
    tag = tools._render_tag({"a": 1, "b": 2}, None, 0, b"png")
    assert re.fullmatch(r"[0-9a-f]{8}", tag)
    assert tools._render_tag({"b": 2, "a": 1}, None, 0, b"png") == tag
    assert tools._render_tag({"a": 1, "b": 3}, None, 0, b"png") != tag
    assert tools._render_tag({"a": 1, "b": 2}, {"x": 1}, 0, b"png") != tag
    assert tools._render_tag({"a": 1, "b": 2}, None, 1, b"png") != tag
    assert tools._render_tag({"a": 1, "b": 2}, None, 0, b"png2") != tag
    # Part boundaries matter: shifting bytes between adjacent parts changes
    # the tag (no concatenation ambiguity).
    assert tools._render_tag(b"ab", b"c") != tools._render_tag(b"a", b"bc")
    monkeypatch.setattr(tools, "_APP_VERSION", "999.0.0")
    assert tools._render_tag({"a": 1, "b": 2}, None, 0, b"png") != tag


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
    the chart png auto-open never pops a real Preview window or incurs
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
    generate_brief_report / chart-png tests never touch the real
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
        assert "must be a valid YYYY-MM-DD" in payload["error"]
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


def test_chart_png_unknown_metric_no_sql(seeded, monkeypatch):
    # INV-T3: an unwhitelisted metric is rejected before any SQL executes.
    def boom(*_a, **_k):
        raise AssertionError("must not query DB for an unwhitelisted metric")

    monkeypatch.setattr(db, "connect", boom)
    payload, err = call(
        tools.chart, {"metric": "bogus", "days": 14, "format": "png", "style": "line"}
    )
    assert err
    assert "unknown metric" in payload["error"]
    assert not tools.subprocess.run.called


def test_chart_png_rejects_ascii_only_styles(seeded):
    # calendar/spark (and anything else) are not png-renderable — the error
    # names the allowed set (the get_metric_trend pattern) instead of
    # silently falling back to a different chart.
    for style in ("pie", "calendar", "spark"):
        payload, err = call(
            tools.chart, {"metric": "rhr", "days": 14, "format": "png", "style": style}
        )
        assert err
        assert f"style '{style}' is not available as png" in payload["error"]
        assert payload["allowed"] == ["bar", "combo", "line"]
    assert not tools.subprocess.run.called


def test_chart_png_defaults_to_line_style(seeded, reports_tmp):
    payload, err = call(tools.chart, {"metric": "rhr", "days": 14, "format": "png"})
    assert not err
    assert Path(payload["path"]).name.startswith("chart-rhr-line-14d-")


def test_chart_unknown_format_is_an_error(seeded):
    payload, err = call(tools.chart, {"metric": "rhr", "days": 14, "format": "svg"})
    assert err
    assert payload["allowed"] == ["ascii", "png"]


def test_chart_png_rejects_huge_days(seeded):
    payload, err = call(
        tools.chart, {"metric": "rhr", "days": _BIG, "format": "png", "style": "line"}
    )
    assert err
    assert "days must be between" in payload["error"]
    assert not tools.subprocess.run.called


def test_chart_png_no_data_in_window(seeded):
    payload, err = call(
        tools.chart, {"metric": "vo2_max", "days": 14, "format": "png", "style": "line"}
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


def test_chart_png_path_escape_is_error(seeded, reports_tmp, monkeypatch):
    def boom(*_a, **_k):
        raise ValueError("escaped")

    monkeypatch.setattr(tools, "_write_atomic", boom)
    payload, err = call(
        tools.chart, {"metric": "rhr", "days": 14, "format": "png", "style": "line"}
    )
    assert err
    assert "escaped reports directory" in payload["error"]
    assert not tools.subprocess.run.called


def test_chart_png_render_failure_is_error(seeded, monkeypatch):
    # Unlike generate_brief_report, a png chart has no takeaway to fall
    # back to -- a render failure is a hard error, not a graceful skip.
    from local_fitness.agent import visuals

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(visuals, "render_chart_png", boom)
    payload, err = call(
        tools.chart, {"metric": "rhr", "days": 14, "format": "png", "style": "line"}
    )
    assert err
    assert "chart render failed" in payload["error"]
    assert not tools.subprocess.run.called


def test_chart_png_happy_path_writes_expected_png(seeded, reports_tmp):
    # INV-T8 + INV-9: valid PNG at the content-addressed filename format
    # chart-metric-chart_type-Nd-<sha8>.png.
    reports_dir, _briefs_dir = reports_tmp
    payload, err = call(
        tools.chart, {"metric": "rhr", "days": 14, "format": "png", "style": "line"}
    )
    assert not err
    path = Path(payload["path"])
    assert re.fullmatch(r"chart-rhr-line-14d-[0-9a-f]{8}\.png", path.name)
    assert path.parent == reports_dir
    assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_chart_png_filename_is_content_addressed(seeded, reports_tmp):
    # Same stale-Preview-refocus fix the PDFs got in 0.28.2: identical chart
    # bytes reuse ONE filename (idempotent — refocus is correct), but changed
    # bytes must land on a NEW filename so macOS `open` shows the fresh render
    # instead of refocusing a stale window. A day-stamped name could not do
    # this (it was constant across an intra-day re-render).
    args = {"metric": "rhr", "days": 14, "format": "png", "style": "line"}
    p1, err1 = call(tools.chart, args)
    p2, err2 = call(tools.chart, args)
    assert not err1 and not err2
    # Identical data twice -> identical content-addressed filename.
    assert p1["path"] == p2["path"]

    # Change the underlying series, re-render the SAME request: because the
    # rendered bytes differ, the filename must change.
    with db.connect() as conn:
        conn.execute("UPDATE daily_metrics SET rhr = rhr + 7")
        conn.commit()
    p3, err3 = call(tools.chart, args)
    assert not err3
    assert p3["path"] != p1["path"]
    assert Path(p3["path"]).name.startswith("chart-rhr-line-14d-")


def test_chart_png_response_carries_inline_image_block(seeded, reports_tmp):
    # Fix A (2026-07-10 doc): the response gains a SECOND content block —
    # an image, base64-decodable, matching the same PNG bytes written to disk.
    import base64

    result = asyncio.run(
        tools.chart.handler({"metric": "rhr", "days": 14, "format": "png", "style": "line"})
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


def test_generate_chart_is_gone_and_chart_owns_both_formats(seeded):
    """0.57.0: generate_chart folded into chart as format="png" — the two
    shared _fetch_metric_series, _CHART_METRICS and overlapping style enums,
    i.e. two names for one job (the get_today_status ambiguity again).
    Pinned so it can't drift back in."""
    assert not hasattr(tools, "generate_chart")
    assert "generate_chart" not in {t.name for t in tools.ALL_TOOLS}
    tool = next(t for t in tools.ALL_TOOLS if t.name == "chart")
    lowered = tool.description.lower()
    assert "png" in lowered and "ascii" in lowered
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


def test_chart_png_auto_opens_and_dispatches_via_to_thread(
    seeded, reports_tmp, monkeypatch
):
    recorded = []
    _spy_to_thread(monkeypatch, recorded)

    payload, err = call(
        tools.chart, {"metric": "rhr", "days": 14, "format": "png", "style": "line"}
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


def test_chart_png_auto_open_failure_does_not_fail_tool(seeded, reports_tmp, monkeypatch):
    monkeypatch.setattr(tools.subprocess, "run", Mock(side_effect=OSError("no open binary")))
    payload, err = call(
        tools.chart, {"metric": "rhr", "days": 14, "format": "png", "style": "line"}
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


def test_chart_png_reports_dir_error_is_clean(seeded, monkeypatch):
    def boom():
        raise OSError("disk full")

    monkeypatch.setattr(tools, "_default_reports_dir", boom)
    payload, err = call(
        tools.chart, {"metric": "rhr", "days": 14, "format": "png", "style": "line"}
    )
    assert err
    assert "could not prepare reports directory" in payload["error"]
    assert not tools.subprocess.run.called


def test_pdf_writing_tools_are_local_only_chart_is_not():
    # INV-4 (rewritten per Fix A, 2026-07-10 doc; extended for
    # workout_report_card): a tool that hands back a *filesystem path* is
    # local-only, because a remote /mcp/ caller gets a container-internal path
    # with no way to retrieve the file. Both PDF writers qualify. chart —
    # including its png format, the former generate_chart — does NOT: the
    # inline ImageContent block sidesteps the retrieval problem.
    all_names = {t.name for t in tools.ALL_TOOLS}
    local_only_names = {t.name for t in tools.LOCAL_ONLY_TOOLS}
    assert local_only_names == {"generate_brief_report", "workout_report_card"}
    assert all_names.isdisjoint(local_only_names)
    assert "chart" in all_names


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

# --- pending_draft: a proposed-but-uncommitted plan must never be invisible --
# plans.insert_draft archives any prior draft, so a draft nobody surfaces can
# be destroyed by the next proposal without a word. Measured on the live DB: a
# 59-workout draft sat unnoticed for 12 days while the active plan was patched
# one day at a time.


def test_draft_summary_is_none_without_a_draft():
    assert plans.draft_summary(None) is None


def test_draft_summary_reports_the_countable_facts():
    """Pins the actual values, not just the keys — a summary that returned the
    wrong count or spanned the wrong dates would still have the right shape."""
    draft = {
        "plan_id": 7, "title": "10K rebuild", "created_at": "2026-07-22T08:05",
        "workouts": [
            {"date": "2026-07-30", "seq": 1},
            {"date": "2026-07-24", "seq": 1},
            {"date": "2026-07-28", "seq": 1},
        ],
    }
    assert plans.draft_summary(draft) == {
        "plan_id": 7,
        "title": "10K rebuild",
        "created_at": "2026-07-22T08:05",
        "workout_count": 3,
        "first_date": "2026-07-24",   # min, not list order
        "last_date": "2026-07-30",
    }


def test_draft_summary_handles_a_draft_with_no_workouts():
    summary = plans.draft_summary(
        {"plan_id": 2, "title": "empty", "created_at": "t", "workouts": []})
    assert summary["workout_count"] == 0
    assert summary["first_date"] is None and summary["last_date"] is None


def test_plan_status_reports_a_pending_draft_alongside_the_active_plan(plan_seeded):
    """The active plan governs; the draft is a loose end reported beside it.
    Both must be present — an earlier shape returned one OR the other."""
    plan_id = plans.insert_draft(
        {"goal_type": "10k", "race_date": "2026-12-01",
         "target_time_seconds": 2900, "created_at": "2026-07-22T08:05"},
        [{"date": "2026-11-01", "seq": 1, "week_index": 1, "type": "easy",
          "target_distance_m": 5000.0, "description": ""},
         {"date": "2026-11-02", "seq": 1, "week_index": 1, "type": "long",
          "target_distance_m": 9000.0, "description": ""}],
        db_path=plan_seeded,
    )
    payload, err = call(tools.get_training_plan_status, {})
    assert not err
    assert payload["active"] is True, "the active plan must still be reported"
    assert payload["pending_draft"] == {
        "plan_id": plan_id,
        "title": None,
        "created_at": "2026-07-22T08:05",
        "workout_count": 2,
        "first_date": "2026-11-01",
        "last_date": "2026-11-02",
    }


def test_plan_status_reports_a_pending_draft_when_there_is_no_active_plan(
        tmp_path, monkeypatch):
    """The case that matters most: nothing active, a draft waiting. A bare
    {active: false} used to hide it completely."""
    p = tmp_path / "f.db"
    db.init_schema(p)
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    plan_id = plans.insert_draft(
        {"goal_type": "10k", "race_date": "2026-12-01",
         "target_time_seconds": 2900, "created_at": "2026-07-22T08:05"},
        [{"date": "2026-11-01", "seq": 1, "week_index": 1, "type": "easy",
          "target_distance_m": 5000.0, "description": ""}],
        db_path=p,
    )
    payload, err = call(tools.get_training_plan_status, {})
    assert not err
    assert payload["active"] is False
    assert payload["pending_draft"]["plan_id"] == plan_id
    assert payload["pending_draft"]["workout_count"] == 1


def test_plan_status_pending_draft_is_none_when_none_exists(plan_seeded):
    """No draft → the key is present and null, so the agent can tell 'no draft'
    apart from 'this tool does not report drafts'."""
    payload, err = call(tools.get_training_plan_status, {})
    assert not err
    assert "pending_draft" in payload
    assert payload["pending_draft"] is None


def test_plan_status_still_opens_exactly_one_connection(plan_seeded, monkeypatch):
    """The draft read rides the connection already held. This tool is on the
    perf gate's hot path; a second open would be a real regression."""
    opens = []
    real_connect = db.connect

    def counting_connect(*a, **kw):
        opens.append(1)
        return real_connect(*a, **kw)

    monkeypatch.setattr(db, "connect", counting_connect)
    payload, err = call(tools.get_training_plan_status, {})
    assert not err
    assert len(opens) == 1, f"expected 1 db.connect(), got {len(opens)}"


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


def test_weekly_rollup_run_walk_split_reconciles_to_foot_total():
    # A pace-gated day: run + walking-pad session, both on foot. week_actual_mi
    # is the FOOT total (run + walk) — the tool's headline; week_run_mi is the
    # brief PDF strip's headline. The split must sum back to the foot total so
    # the two sibling surfaces reconcile and can't drift into contradiction.
    # Exact-mile meters keep the per-day-then-sum rounding clean.
    workouts = [{
        "date": "2026-07-10", "verdict": "done", "type": "interval",
        "target_distance_m": 9600.0,
        "actual_distance_m": 12874.752,      # 8.0 mi foot total (run + walk)
        "actual_run_distance_m": 8046.72,    # 5.0 mi run
        "actual_walk_distance_m": 4828.032,  # 3.0 mi walk
    }]
    rollup = tools.weekly_rollup(workouts, "2026-07-12")
    assert rollup["week_run_mi"] == 5.0
    assert rollup["week_walk_mi"] == 3.0
    assert rollup["week_actual_mi"] == 8.0   # foot total, NOT run-only
    assert round(rollup["week_run_mi"] + rollup["week_walk_mi"], 1) == rollup["week_actual_mi"]


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
    # Same window, rest days out of both halves of the fraction: 2.5/4 graded
    # SESSIONS (long missed, easy done, tempo done, easy partial) = 62%. The
    # 13-point gap is the two compliant rest days.
    assert section["sessions_adherence_pct"] == 62
    assert section["rest_days_counted"] == 2
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


def test_progress_this_week_reconciles_with_brief_plan_section(plan_seeded):
    # The two sibling mileage surfaces must agree: get_training_plan_progress's
    # this_week.week_actual_mi is the FOOT total split by week_run_mi/week_walk_mi,
    # and its week_run_mi is exactly what _build_plan_section (the brief PDF strip)
    # headlines as its own week_actual_mi. Pins the semantic so the two can't
    # silently diverge again (the pre-fix defect: same key, two different numbers).
    today = date.today().isoformat()
    body, err = call(tools.get_training_plan_progress, {})
    assert not err
    tw = body["this_week"]
    assert round(tw["week_run_mi"] + tw["week_walk_mi"], 1) == tw["week_actual_mi"]
    section = tools._build_plan_section(today)
    assert section["week_actual_mi"] == tw["week_run_mi"]


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
        *, model=None, timeout=30.0, notes_text=None, sessions_adherence_pct=None,
        **_kw,
    ):
        calls.append((today_workout, adherence_pct, days_to_race, goal_type,
                      notes_text, sessions_adherence_pct))
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
    # The rest-day-free companion reaches the coach too, or the prompt would
    # quote 75% while the strip beside it prints 62%.
    assert calls[0][5] == 62


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
    # 0.37.0: the sqlite message rides along (it names only the model's own
    # SQL identifiers — the read-only gate keeps anything else out), because
    # "invalid query" alone produced blind same-shape retries.
    assert "query failed: boom" in payload["error"]
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


def test_get_brief_context_populates_continuity_from_saved_briefs(tmp_path, monkeypatch):
    """The handler has to pass `recent_briefs` in — assemble_brief_context never
    reads the briefings dir itself, so `continuity` used to be permanently []
    over MCP while the in-process composer got the real headlines."""
    from local_fitness.agent import briefs as briefs_mod

    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    db.init_schema(p)
    today = date.today()
    with db.connect(p) as conn:
        conn.execute("INSERT INTO daily_metrics (date, rhr, steps) VALUES (?, 54, 9100)",
                     (today.isoformat(),))

    out = tmp_path / "briefings"
    out.mkdir()
    for offset, headline in ((1, "Yesterday: you skipped the tempo"),
                             (3, "Three days back: RHR climbing")):
        d = (today - timedelta(days=offset)).isoformat()
        (out / f"{d}.json").write_text(
            json.dumps({"date": d, "user_name": "t",
                        "takeaways": [{"headline": headline, "summary": "s",
                                       "tone": "caution"}]}),
            encoding="utf-8")
    monkeypatch.setattr(briefs_mod, "DEFAULT_BRIEFINGS_DIR", out)

    payload, err = call(tools.get_brief_context, {})
    assert not err
    assert payload["continuity"] == ["Yesterday: you skipped the tempo",
                                     "Three days back: RHR climbing"]
    # Same call now also carries the freshness fields: newest brief is 1 day
    # old, and the only daily row is today's, so the frontier is today.
    assert payload["brief_stale_days"] == 1
    assert payload["data_frontier"] == today.isoformat()


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


@pytest.fixture
def day_sensitive_memory(rc_seeded, monkeypatch):
    """Seed a ledger whose text genuinely changes from one day to the next.

    The step-streak line renders a counter computed as-of-yesterday, so with
    steps over goal on every recent day the block reads differently on each
    calendar day. That is the real-world driver of the bug the tests below
    pin; without it they would pass vacuously, which is why they assert the
    precondition rather than assume it.
    """
    with db.connect(rc_seeded) as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('daily_step_goal', ?)",
            ("8000",))
        for back in range(0, 40):
            conn.execute(
                "INSERT OR REPLACE INTO daily_metrics (date, steps) "
                "VALUES (?, ?)",
                ((date.today() - timedelta(days=back)).isoformat(), 8500))
    return rc_seeded


def _freeze_ledger_clock(monkeypatch, day):
    """Move the clock `ledger` reads when nobody hands it a date."""
    class _FakeDate(date):
        @classmethod
        def today(cls):
            return day

    monkeypatch.setattr(ledger, "date", _FakeDate)


def test_the_memory_block_used_here_really_does_move_with_the_calendar(
    day_sensitive_memory, monkeypatch
):
    """Precondition for the two tests below: unanchored, this block differs
    from one day to the next. If this ever stops being true the regression
    tests are no longer testing anything and must be re-seeded."""
    _freeze_ledger_clock(monkeypatch, date.today())
    first = memory.render_memory_for_prompt(user_name="Alex")
    _freeze_ledger_clock(monkeypatch, date.today() + timedelta(days=3))
    assert memory.render_memory_for_prompt(user_name="Alex") != first


def test_the_read_cache_key_does_not_move_with_the_calendar(
    day_sensitive_memory, reports_tmp, monkeypatch
):
    """A card's read is cached under a hash of its prompt, and the coach's
    memory is in that prompt. Resolved against the CLOCK, the block carried a
    daily-incrementing step streak, so every stored card's key rotated
    overnight and re-rendering yesterday's run paid a fresh SDK call — 14 of
    15 stored cards missed on the live corpus (2026-08-02). Anchoring the
    memory to the ACTIVITY's date is what makes a past card's prompt, and
    therefore its key, stand still.
    """
    generated = []

    async def fake_generate(*_a, **_k):
        generated.append(1)
        return ("DISTANCE: covered the ground.\n\nPACE: quick enough.\n\n"
                "HEART RATE: sat where it should.\n\n"
                "STIMULUS: banked what it was worth.")

    monkeypatch.setattr(workout_coach, "generate_read", fake_generate)
    # Reflect writes a journal entry of its own; silence it so this test
    # measures the calendar and nothing else. Memory itself stays ENABLED —
    # the ledger block is the whole subject.
    async def _no_reflect(_card):
        return None

    monkeypatch.setattr(tools.reflect, "reflect_after_report_card", _no_reflect)

    _freeze_ledger_clock(monkeypatch, date.today())
    payload, err = call(tools.workout_report_card, {"format": "table"})
    assert not err
    assert len(generated) == 1
    first_key, first_read = card_store.load_read(1)
    assert first_key is not None

    # Three days pass. Nothing about the run, the plan or the grades changed.
    _freeze_ledger_clock(monkeypatch, date.today() + timedelta(days=3))
    payload, err = call(tools.workout_report_card, {"format": "table"})
    assert not err

    assert len(generated) == 1, (
        "the read was regenerated for a card nothing had changed about")
    assert card_store.load_read(1) == (first_key, first_read)


def test_a_journal_entry_written_later_does_not_move_a_past_cards_key(
    day_sensitive_memory, reports_tmp, monkeypatch
):
    """The other half of the anchoring: the journal layer is capped at the
    card's date, so writing an entry about a LATER day cannot rewrite an older
    card's prompt. Without the cap the block is an unbounded latest-N list and
    any new entry — a brief reflection, another card's reflection — rotated
    every stored card's key at once."""
    generated = []

    async def fake_generate(*_a, **_k):
        generated.append(1)
        return ("DISTANCE: covered the ground.\n\nPACE: quick enough.\n\n"
                "HEART RATE: sat where it should.\n\n"
                "STIMULUS: banked what it was worth.")

    monkeypatch.setattr(workout_coach, "generate_read", fake_generate)

    async def _no_reflect(_card):
        return None

    monkeypatch.setattr(tools.reflect, "reflect_after_report_card", _no_reflect)

    older = (date.today() - timedelta(days=5)).isoformat()
    payload, err = call(
        tools.workout_report_card, {"date": older, "format": "table"})
    assert not err
    aid = payload["activity_id"]
    before = card_store.load_read(aid)
    assert len(generated) == 1

    journal.save_entry("wrote this days after that run", source="chat",
                       entry_date=date.today().isoformat())

    payload, err = call(
        tools.workout_report_card, {"date": older, "format": "table"})
    assert not err
    assert len(generated) == 1
    assert card_store.load_read(aid) == before


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
    """Validation happens before any query, mirroring the chart tool's
    unknown-metric guard."""
    def boom(*_a, **_k):
        raise AssertionError("must not open the DB on a malformed date")

    monkeypatch.setattr(tools.db, "connect", boom)
    payload, err = call(tools.workout_report_card, {"date": "07-19-2026"})
    assert err
    assert "must be a valid YYYY-MM-DD" in payload["error"]


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


def test_report_card_surfaces_page_overflow_instead_of_spilling_silently(
    rc_seeded, reports_tmp, monkeypatch, caplog
):
    """When the density ladder is exhausted (page_count > 1), the card has no
    droppable content, so the tool must SAY so — a `pages` field on the payload
    and a WARNING log — never emit a silent 2-page 'single-page' card. A ladder
    that fit (pages == 1) leaves the payload clean."""
    from local_fitness.agent import visuals

    monkeypatch.setattr(
        visuals, "render_report_card_pdf", lambda *_a, **_k: (b"%PDF-two-pages", 2))
    with caplog.at_level(logging.WARNING):
        payload, err = call(tools.workout_report_card, {})
    assert not err
    assert payload["pages"] == 2
    assert any("still 2 pages" in r.message for r in caplog.records)


def test_report_card_single_page_leaves_no_pages_field(rc_seeded, reports_tmp):
    """The overflow signal is present ONLY on overflow — a normal one-page card
    carries no `pages` key."""
    payload, err = call(tools.workout_report_card, {})
    assert not err
    assert "pages" not in payload


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
    assert payload["overall"]["stars"] is not None   # ratings are unaffected
    assert Path(payload["path"]).read_bytes()[:5] == b"%PDF-"
    assert "No per-mile splits recorded" in payload["markdown"]


def test_report_card_insufficient_history_grades_nothing(seeded, reports_tmp):
    """The shared fixture's single activity has no comparable history."""
    payload, err = call(tools.workout_report_card, {"format": "table"})
    assert not err
    assert payload["reference"] == "insufficient_data"
    assert payload["overall"]["stars"] is None
    assert set(payload["stars"].values()) == {None}
    # ...and the rendered strings say so rather than drawing an empty row.
    assert set(payload["ratings"].values()) == {"n/a"}
    assert payload["overall_rating"] == "n/a"


def test_report_card_reports_a_double_day(rc_seeded, reports_tmp):
    with db.connect(rc_seeded) as conn:
        conn.execute(
            "INSERT INTO activities (activity_id, date, start_time, activity_type, "
            "duration_seconds, distance_meters) VALUES (555, ?, ?, 'running', 1200, 5000)",
            (date.today().isoformat(), date.today().isoformat() + " 05:00:00"),
        )
    payload, err = call(tools.workout_report_card, {"format": "table"})
    assert not err
    # Enriched shape (A5): enough of the other session to say WHICH one the
    # card did not grade, not just that one exists.
    others = payload["other_activities_on_date"]
    assert [o["activity_id"] for o in others] == [555]
    assert others[0]["activity_type"] == "running"
    assert others[0]["distance_mi"] == 3.11
    assert others[0]["start_time"].endswith("05:00:00")


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
                "hr": "Stayed low.", "stimulus": "Banked what it should."}

    monkeypatch.setattr(tools.workout_coach, "generate_read_cached", _read)
    payload, err = call(tools.workout_report_card, {})
    assert not err
    with pdfplumber.open(io.BytesIO(Path(payload["path"]).read_bytes())) as doc:
        text = "\n".join(p.extract_text() or "" for p in doc.pages)
    # All four paragraphs render, each under its metric label.
    for label in ("DISTANCE", "PACE", "HEART RATE", "STIMULUS"):
        assert label in text
    for para in ("You covered the ground.", "Too quick.", "Stayed low.",
                 "Banked what it should."):
        assert para in text
    # They sit under the hero band — score, meta line, scale note — in table
    # order. The score is the first thing on the page after the run's name.
    assert text.index("/ 5") < text.index("You covered the ground.")
    assert text.index("You covered the ground.") < text.index("Too quick.")
    # The card states what a 5 means, on the page and not only in the payload.
    assert "compliance score" in text
    assert text.index("compliance score") < text.index("You covered the ground.")
    # No letter rubric survives on the page.
    assert "GPA" not in text


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
    # The end date is derived from today because the fixture seeds the 40 days
    # ending today — a hard-coded date died the day it aged out of that window.
    end = (date.today() - timedelta(days=5)).isoformat()
    dates, _values = tools._fetch_metric_series("rhr", 3650, end=end)
    assert dates, "fixture should have rhr rows in range"
    assert max(dates) <= end


def test_fetch_metric_series_window_starts_days_before_end(seeded):
    end = (date.today() - timedelta(days=5)).isoformat()
    start = (date.today() - timedelta(days=12)).isoformat()
    dates, _values = tools._fetch_metric_series("rhr", 7, end=end)
    assert min(dates) >= start
    assert max(dates) <= end


def test_fetch_metric_series_defaults_to_today_for_live_callers(seeded):
    """chart() (both formats) must keep its existing behavior."""
    dated = tools._fetch_metric_series("rhr", 3650, end=date.today().isoformat())
    default = tools._fetch_metric_series("rhr", 3650)
    assert dated == default


# --- coach-memory journal tools (0.30.0) -------------------------------------


def test_save_coach_memory_round_trip(seeded):
    saved, err = call(tools.save_coach_memory,
                      {"text": "Blamed the heat again — second time this month."})
    assert not err
    assert saved["saved"] is True
    assert saved["source"] == "chat"
    listed, err = call(tools.list_coach_memories, {})
    assert not err
    assert listed["count"] == 1
    assert listed["memories"][0]["text"] == (
        "Blamed the heat again — second time this month.")


def test_save_coach_memory_validation(seeded):
    _p, err = call(tools.save_coach_memory, {"text": "   "})
    assert err
    _p, err = call(tools.save_coach_memory, {"text": "x" * 241})
    assert err
    _p, err = call(tools.save_coach_memory,
                   {"text": "fine", "date": "not-a-date"})
    assert err
    payload, err = call(tools.save_coach_memory,
                        {"text": "dated", "date": "2026-07-20"})
    assert not err
    assert payload["entry_date"] == "2026-07-20"


def test_list_coach_memories_args_validated(seeded):
    _p, err = call(tools.list_coach_memories, {"days": -1})
    assert err
    _p, err = call(tools.list_coach_memories, {"limit": 0})
    assert err
    payload, err = call(tools.list_coach_memories, {"days": 30, "limit": 5})
    assert not err
    assert payload["memories"] == []


def test_delete_coach_memory(seeded):
    saved, _ = call(tools.save_coach_memory, {"text": "forget me"})
    deleted, err = call(tools.delete_coach_memory,
                        {"entry_id": saved["entry_id"]})
    assert not err and deleted["deleted"] is True
    _p, err = call(tools.delete_coach_memory, {"entry_id": saved["entry_id"]})
    assert err
    _p, err = call(tools.delete_coach_memory, {})
    assert err


def test_recall_finds_archived_memory(seeded):
    call(tools.save_coach_memory,
         {"text": "left achilles flared on the hill repeats",
          "date": "2026-01-05"})
    for i in range(journal.JOURNAL_CAP):
        call(tools.save_coach_memory,
             {"text": f"filler {i}", "date": "2026-07-01"})
    payload, err = call(tools.recall_coach_memories, {"query": "achilles"})
    assert not err
    assert payload["search"] == "fts"
    assert payload["count"] == 1
    match = payload["matches"][0]
    assert match["text"] == "left achilles flared on the hill repeats"
    assert match["archived"] is True


def test_recall_validates_input(seeded):
    call(tools.save_coach_memory, {"text": "one memory"})
    _p, err = call(tools.recall_coach_memories, {})
    assert err
    _p, err = call(tools.recall_coach_memories, {"query": "   "})
    assert err
    _p, err = call(tools.recall_coach_memories, {"query": "x" * 201})
    assert err
    _p, err = call(tools.recall_coach_memories, {"query": "ok", "limit": 0})
    assert err
    _p, err = call(tools.recall_coach_memories, {"query": "ok", "limit": "ten"})
    assert err
    _p, err = call(tools.recall_coach_memories, {"query": "()"})
    assert err  # nothing searchable after sanitization
    payload, err = call(tools.recall_coach_memories,
                        {"query": "memory", "limit": 999})
    assert not err
    assert payload["count"] <= 25


def test_list_coach_memories_include_archived(seeded):
    for i in range(journal.JOURNAL_CAP + 1):
        call(tools.save_coach_memory,
             {"text": f"memory {i}", "date": "2026-07-01"})
    hot, err = call(tools.list_coach_memories, {"limit": 200})
    assert not err
    assert hot["count"] == journal.JOURNAL_CAP  # clamp + archived excluded
    everything, err = call(tools.list_coach_memories,
                           {"limit": 200, "include_archived": True})
    assert not err
    assert everything["count"] == journal.JOURNAL_CAP + 1
    assert sum(1 for m in everything["memories"] if m["archived"]) == 1


def test_get_coach_personality_counts_active_and_archived(seeded):
    for i in range(journal.JOURNAL_CAP + 1):
        call(tools.save_coach_memory,
             {"text": f"memory {i}", "date": "2026-07-01"})
    payload, err = call(tools.get_coach_personality, {})
    assert not err
    assert payload["journal_entries"] == journal.JOURNAL_CAP
    assert payload["journal_archived"] == 1


def test_report_card_schedules_reflection_exactly_once(rc_seeded, monkeypatch):
    """First render schedules the fire-and-forget reflection; a re-render (the
    common case) must skip even task creation via the has_event pre-check."""
    from local_fitness.agent import journal as journal_mod
    from local_fitness.agent import reflect as reflect_mod

    scheduled = []

    def recording_reflect(card):
        scheduled.append(card["activity"]["activity_id"])

        async def _noop():
            return None

        return _noop()

    monkeypatch.setattr(reflect_mod, "reflect_after_report_card",
                        recording_reflect)
    _payload, err = call(tools.workout_report_card, {"format": "table"})
    assert not err
    assert scheduled == [1]

    # The reflection itself would have written the event row; simulate that,
    # then re-render: no second scheduling.
    journal_mod.save_entry("tempo collapsed late", source="report_card",
                           source_key="1")
    _payload, err = call(tools.workout_report_card, {"format": "table"})
    assert not err
    assert scheduled == [1]


def test_report_card_reflection_respects_the_kill_switch(rc_seeded, monkeypatch):
    from local_fitness.agent import reflect as reflect_mod

    scheduled = []

    def recording_reflect(card):
        scheduled.append(True)

        async def _noop():
            return None

        return _noop()

    monkeypatch.setattr(reflect_mod, "reflect_after_report_card",
                        recording_reflect)
    monkeypatch.setenv("LOCAL_FITNESS_COACH_MEMORY", "0")
    _payload, err = call(tools.workout_report_card, {"format": "table"})
    assert not err
    assert scheduled == []


# --- personality tuning tools (0.31.0) ---------------------------------------


def test_get_coach_personality_untuned_serves_the_seed(seeded):
    payload, err = call(tools.get_coach_personality, {})
    assert not err
    assert payload["profile"] == "hardass"  # the shipped default
    assert payload["customized"] is False
    assert payload["base_profile_mismatch"] is False
    from local_fitness.agent import coach as coach_mod
    assert payload["spec"]["identity"] == coach_mod.load_profile("hardass").persona
    assert payload["dials"]["harshness"] == 9
    assert "step_goal_nagging" in payload["known_topics"]


def test_update_coach_personality_materializes_and_applies(seeded):
    payload, err = call(tools.update_coach_personality, {
        "add_never_do": "Never lecture about sleep.",
        "set_intensity": {"step_goal_nagging": "low"},
    })
    assert not err
    assert payload["customized"] is True
    assert payload["spec"]["never_do"] == ["Never lecture about sleep."]
    assert payload["spec"]["intensity"] == {"step_goal_nagging": "low"}
    assert payload["spec"]["updated_at"]

    # The tuned voice is live on the next resolve — no restart.
    from local_fitness.agent import coach as coach_mod
    profile = coach_mod.resolve_coach_profile()
    assert profile.spec is not None
    assert "Never lecture about sleep." in profile.effective_persona
    assert "step_goal_nagging: low" in profile.effective_persona


def test_update_coach_personality_dials_write_the_existing_settings_keys(seeded):
    payload, err = call(tools.update_coach_personality,
                        {"harshness": 10, "roast_threshold": 1.1})
    assert not err
    assert payload["dials_changed"] == ["coach_harshness", "coach_roast_threshold"]
    assert db.get_setting("coach_harshness") == "10"
    assert db.get_setting("coach_roast_threshold") == "1.1"
    from local_fitness.agent import coach as coach_mod
    assert coach_mod.resolve_coach_profile().harshness == 10


def test_update_coach_personality_rejects_bad_input_with_the_whitelist(seeded):
    payload, err = call(tools.update_coach_personality,
                        {"harshness": 99, "sock_color": "red",
                         "set_intensity": {"sleep": "loud"}})
    assert err
    msg = payload.get("error", "") if isinstance(payload, dict) else str(payload)
    assert "harshness must be an integer 0-10" in msg
    assert "unknown field 'sock_color'" in msg
    assert "bad intensity level 'loud'" in msg
    assert "editable_fields" in payload
    _payload, err = call(tools.update_coach_personality, {})
    assert err  # nothing to update


def test_update_coach_personality_reset_returns_to_stock(seeded):
    call(tools.update_coach_personality, {"identity": "Custom voice."})
    from local_fitness.agent import personality as personality_mod
    assert db.get_setting(personality_mod.SPEC_KEY) is not None
    payload, err = call(tools.update_coach_personality, {"reset": True})
    assert not err
    assert payload["reset"] is True and payload["customized"] is False
    assert db.get_setting(personality_mod.SPEC_KEY) is None
    from local_fitness.agent import coach as coach_mod
    profile = coach_mod.resolve_coach_profile()
    assert profile.effective_persona == profile.persona


def test_update_coach_personality_spec_size_cap(seeded):
    payload, err = call(tools.update_coach_personality,
                        {"identity": "x" * 4001})
    assert err
    # Names the cap and the overage — a bare `assert err` would pass on a
    # tool that rejected every personality edit.
    assert "identity too long (4001 chars, max 4000)" in payload["error"]
    # One under the cap still writes, so the boundary is real, not a blanket no.
    _payload, err = call(tools.update_coach_personality,
                         {"identity": "y" * 4000})
    assert not err


# --- report-card persistence: fast path + query tools ------------------------

_RC_READ_TEXT = (
    "DISTANCE: covered the ground today.\n"
    "PACE: quick stuff throughout.\n"
    "HEART RATE: low and easy the whole way.\n"
    "STIMULUS: banked plenty for the week."
)


@pytest.fixture
def rc_cards(rc_seeded, tmp_path, monkeypatch):
    """rc_seeded plus tmp user-notes, so notes edits in tests never touch the
    repo's real data/user_notes.md."""
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    return rc_seeded


def _patch_generate(monkeypatch, text=_RC_READ_TEXT):
    calls = {"n": 0}

    async def _gen(profile, card, **kwargs):
        calls["n"] += 1
        return text

    monkeypatch.setattr(tools.workout_coach, "generate_read", _gen)
    return calls


def _patch_generate_raises(monkeypatch, message="stream died"):
    async def _gen(profile, card, **kwargs):
        raise RuntimeError(message)

    monkeypatch.setattr(tools.workout_coach, "generate_read", _gen)


def _rc_raw_row(activity_id=1):
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM report_cards WHERE activity_id = ?",
            (activity_id,)).fetchone()
    return dict(row) if row is not None else None


def test_report_card_render_persists_a_real_read_snapshot(
        rc_cards, reports_tmp, monkeypatch):
    _patch_generate(monkeypatch)
    payload, err = call(tools.workout_report_card, {"format": "table"})
    assert not err
    row = _rc_raw_row(1)
    assert row is not None
    assert row["read_cache_key"] is not None
    assert row["overall_stars"] == payload["overall"]["stars"]
    assert row["distance_stars"] == payload["stars"]["distance"]
    stored = json.loads(row["card_json"])
    assert stored["coach_read"]["distance"] == "covered the ground today."


def test_report_card_fast_path_skips_generation_and_keeps_the_row(
        rc_cards, reports_tmp, monkeypatch):
    """Second render: the stored card serves the read (no SDK call even with
    the single-entry file cache gone) and the row stays byte-identical —
    the keyed no-op end to end."""
    calls = _patch_generate(monkeypatch)
    payload, err = call(tools.workout_report_card, {"format": "table"})
    assert not err and calls["n"] == 1
    before = _rc_raw_row(1)
    # Remove the single-entry file cache so ONLY the store can avoid a call.
    (db.DEFAULT_DB_PATH.parent / "workout_coach_cache.json").unlink()

    async def _must_not_generate(profile, card, **kwargs):
        raise AssertionError("fast path should have reused the stored read")

    monkeypatch.setattr(tools.workout_coach, "generate_read", _must_not_generate)
    payload, err = call(tools.workout_report_card, {"format": "table"})
    assert not err
    # The stored read, not the fallback template, made it onto the card.
    assert "covered the ground today." in payload["markdown"]
    assert _rc_raw_row(1) == before


def test_report_card_first_fallback_persists_with_a_null_key(
        rc_cards, reports_tmp, monkeypatch):
    _patch_generate_raises(monkeypatch)
    payload, err = call(tools.workout_report_card, {
        "activity_id": 105, "format": "table"})
    assert not err
    row = _rc_raw_row(105)
    assert row is not None
    assert row["read_cache_key"] is None


def test_report_card_fallback_render_never_clobbers_the_stored_real_read(
        rc_cards, reports_tmp, monkeypatch, tmp_path):
    _patch_generate(monkeypatch)
    payload, err = call(tools.workout_report_card, {"format": "table"})
    assert not err
    before = _rc_raw_row(1)
    assert before["read_cache_key"] is not None
    # A notes edit changes the prompt key, so the fast path misses; the
    # generation then fails — the documented transient stream death.
    (tmp_path / "user_notes.md").write_text("- go easier on me\n")
    (db.DEFAULT_DB_PATH.parent / "workout_coach_cache.json").unlink()
    _patch_generate_raises(monkeypatch)
    payload, err = call(tools.workout_report_card, {"format": "table"})
    assert not err
    # This render showed the template, but the stored snapshot kept the
    # coach's real words AND that render's grades — whole-row no-op.
    assert "covered the ground today." not in payload["markdown"]
    assert _rc_raw_row(1) == before


def test_report_card_new_real_read_overwrites_the_whole_row(
        rc_cards, reports_tmp, monkeypatch, tmp_path):
    _patch_generate(monkeypatch)
    call(tools.workout_report_card, {"format": "table"})
    before = _rc_raw_row(1)
    (tmp_path / "user_notes.md").write_text("- new coaching note\n")
    (db.DEFAULT_DB_PATH.parent / "workout_coach_cache.json").unlink()
    _patch_generate(monkeypatch, text=_RC_READ_TEXT.replace(
        "covered the ground today.", "a whole new verdict."))
    payload, err = call(tools.workout_report_card, {"format": "table"})
    assert not err
    after = _rc_raw_row(1)
    assert after["read_cache_key"] != before["read_cache_key"]
    assert "a whole new verdict." in json.dumps(after["card_json"])


def test_report_card_save_failure_never_fails_the_render(
        rc_cards, reports_tmp, monkeypatch):
    """save_card's never-raises contract, exercised through the call site:
    a broken write drops the row, never the card."""
    _patch_generate(monkeypatch)
    monkeypatch.setattr(
        tools.card_store, "_UPSERT_SQL", "INSERT INTO no_such_table VALUES (1)")
    payload, err = call(tools.workout_report_card, {"format": "table"})
    assert not err
    assert payload["markdown"].startswith("# Report Card")
    assert _rc_raw_row(1) is None


def test_list_report_cards_empty_and_bad_args(seeded):
    payload, err = call(tools.list_report_cards, {})
    assert not err
    assert payload == {"cards": [], "count": 0, "truncated": False}
    payload, err = call(tools.list_report_cards, {"start_date": "07-01-2026"})
    assert err and "start_date must be a valid YYYY-MM-DD" in payload["error"]
    payload, err = call(tools.list_report_cards, {"intent_class": "tempo"})
    assert err
    assert payload["allowed"] == ["easy", "long", "quality", "steady"]
    payload, err = call(tools.list_report_cards, {"limit": 0})
    assert err and "limit" in payload["error"]


def test_list_report_cards_payload_filters_and_order(rc_cards, reports_tmp, monkeypatch):
    _patch_generate(monkeypatch)
    for activity_id in (103, 105):
        payload, err = call(tools.workout_report_card, {
            "activity_id": activity_id, "format": "table"})
        assert not err
        (db.DEFAULT_DB_PATH.parent / "workout_coach_cache.json").unlink()
    payload, err = call(tools.list_report_cards, {})
    assert not err
    assert payload["count"] == 2
    # activity 103 is 3 days ago, 105 is 5 days ago — newest run first.
    assert [c["activity_id"] for c in payload["cards"]] == [103, 105]
    top = payload["cards"][0]
    # Four compliance metrics. `load` is absent entirely — it carries no score
    # (0.40.0), and a key that is always null invites a reader to look for one.
    assert set(top["stars"]) == {"distance", "pace", "hr", "continuity"}
    assert top["overall"] is not None and top["graded_at"]
    # A card rated under the star rubric has no legacy letter.
    assert top["legacy_grade"] is None
    # The date filter pins actual rows, not just a count.
    cutoff = (date.today() - timedelta(days=4)).isoformat()
    payload, err = call(tools.list_report_cards, {"start_date": cutoff})
    assert [c["activity_id"] for c in payload["cards"]] == [103]


def test_get_report_card_missing_row_points_at_a_local_session(seeded):
    payload, err = call(tools.get_report_card, {"activity_id": 42})
    assert err
    assert "local session" in payload["error"]
    payload, err = call(tools.get_report_card, {})
    assert err and "activity_id is required" in payload["error"]


def test_get_report_card_returns_the_stored_snapshot_verbatim(
        rc_cards, reports_tmp, monkeypatch):
    _patch_generate(monkeypatch)
    rendered, err = call(tools.workout_report_card, {"format": "table"})
    assert not err
    payload, err = call(tools.get_report_card, {"activity_id": 1})
    assert not err
    assert payload["activity_id"] == 1
    assert payload["date"] == date.today().isoformat()
    assert payload["graded_at"]
    assert payload["markdown"].startswith("# Report Card")
    assert payload["coach_read"]["distance"] == "covered the ground today."
    assert payload["card"]["overall"]["stars"] == rendered["overall"]["stars"]


def test_card_query_tools_are_shared_not_local_only():
    all_names = {t.name for t in tools.ALL_TOOLS}
    local_names = {t.name for t in tools.LOCAL_ONLY_TOOLS}
    assert {"list_report_cards", "get_report_card"} <= all_names
    assert not {"list_report_cards", "get_report_card"} & local_names


def test_importing_tools_does_not_import_garminconnect():
    """S7 (0.36.0): the garminconnect -> requests chain (~28 ms measured) must
    stay out of `import tools` — every stdio session start pays it otherwise.
    sync_garmin_data is its only consumer and imports it in its body."""
    import sys

    code = (
        "import sys; import local_fitness.agent.tools; "
        "sys.exit(1 if 'garminconnect' in sys.modules else 0)"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode()


# --------------------------------------------------------------------------- #
# 0.36.0 speed pass: recovery_pattern range queries + connect counts
# --------------------------------------------------------------------------- #
def _count_connects(monkeypatch):
    counts = {"n": 0}
    orig = db.connect

    def counting(*a, **k):
        counts["n"] += 1
        return orig(*a, **k)

    monkeypatch.setattr(db, "connect", counting)
    return counts


def test_recovery_pattern_counts_workouts_skipped_for_missing_baseline(
    tmp_path, monkeypatch
):
    """A matched workout whose date has no baselines row was silently dropped
    — '3 matched' printed identically for 3-of-3 and 3-of-40."""
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    today = date.today()
    with db.connect(p) as conn:
        for i, has_baseline in ((10, True), (20, False), (30, True)):
            d = (today - timedelta(days=i)).isoformat()
            conn.execute(
                "INSERT INTO activities (activity_id, date, activity_type, "
                "duration_seconds, distance_meters, training_load) "
                "VALUES (?, ?, 'running', 3600, 10000, 80)",
                (i, d),
            )
            if has_baseline:
                conn.execute(
                    "INSERT INTO baselines (date, body_battery_max_60day_mean, "
                    "rhr_60day_mean) VALUES (?, 88.0, 52.0)",
                    (d,),
                )
                conn.execute(
                    "INSERT INTO daily_metrics (date, body_battery_max, rhr) "
                    "VALUES (?, 90, 50)",
                    ((today - timedelta(days=i - 1)).isoformat(),),
                )
    payload, err = call(tools.recovery_pattern, {"activity_type": "running"})
    assert not err
    assert payload["n_workouts_matched"] == 2
    assert payload["n_skipped_no_baseline"] == 1
    # The two survivors both recovered on day +1 (bb 90 >= 0.95*88).
    assert payload["avg_recovery_days_body_battery"] == 1.0


# --------------------------------------------------------------------------- #
# Fix 2: recovery_pattern gates recovery PER METRIC, not on bb baseline alone.
#
# body_battery_max_60day_mean derived from the dead body_battery_max column
# (NULL on every daily_metrics row since ingest never populated it — see the
# daily.py fix) and only exists through 2026-01-27 on real data, while
# rhr_60day_mean is alive through today. The old whole-workout gate
# (`if not baseline or baseline["bb"] is None: skip`) meant a 90-day
# recovery_pattern call always returned 0 matched, even though rhr recovery
# was fully computable the entire time.
# --------------------------------------------------------------------------- #
def test_recovery_pattern_matches_workout_with_rhr_baseline_but_no_bb_baseline(
    tmp_path, monkeypatch
):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    db.init_schema(p)
    today = date.today()
    wdate = today - timedelta(days=10)
    with db.connect(p) as conn:
        conn.execute(
            "INSERT INTO activities (activity_id, date, activity_type, "
            "duration_seconds, distance_meters, training_load) "
            "VALUES (1, ?, 'running', 3600, 10000, 80)",
            (wdate.isoformat(),),
        )
        # rhr baseline present, bb baseline column left NULL — the real-data shape.
        conn.execute(
            "INSERT INTO baselines (date, rhr_60day_mean) VALUES (?, 52.0)",
            (wdate.isoformat(),),
        )
        conn.execute(
            "INSERT INTO daily_metrics (date, rhr) VALUES (?, 50)",
            ((wdate + timedelta(days=2)).isoformat(),),
        )
    payload, err = call(tools.recovery_pattern, {"activity_type": "running", "lookback_days": 30})
    assert not err
    assert payload["n_workouts_matched"] == 1  # matched despite no bb baseline
    assert payload["n_skipped_no_baseline"] == 0
    assert payload["n_skipped_no_bb_baseline"] == 1
    assert payload["n_skipped_no_rhr_baseline"] == 0
    w = payload["recent_workouts"][0]
    assert w["recovery_days_to_rhr_baseline"] == 2  # 50 <= 52*1.03 on day +2
    assert w["recovery_days_to_bb_baseline"] is None
    assert w["bb_baseline_note"] == "no body-battery baseline for this date"
    assert w["rhr_baseline_note"] is None
    assert payload["avg_recovery_days_rhr"] == 2.0
    assert payload["avg_recovery_days_body_battery"] is None


def test_recovery_pattern_matches_workout_with_bb_baseline_but_no_rhr_baseline(
    tmp_path, monkeypatch
):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    db.init_schema(p)
    today = date.today()
    wdate = today - timedelta(days=10)
    with db.connect(p) as conn:
        conn.execute(
            "INSERT INTO activities (activity_id, date, activity_type, "
            "duration_seconds, distance_meters, training_load) "
            "VALUES (1, ?, 'running', 3600, 10000, 80)",
            (wdate.isoformat(),),
        )
        conn.execute(
            "INSERT INTO baselines (date, body_battery_max_60day_mean) VALUES (?, 80.0)",
            (wdate.isoformat(),),
        )
        conn.execute(
            "INSERT INTO daily_metrics (date, body_battery_max) VALUES (?, 90)",
            ((wdate + timedelta(days=3)).isoformat(),),
        )
    payload, err = call(tools.recovery_pattern, {"activity_type": "running", "lookback_days": 30})
    assert not err
    assert payload["n_workouts_matched"] == 1
    assert payload["n_skipped_no_bb_baseline"] == 0
    assert payload["n_skipped_no_rhr_baseline"] == 1
    w = payload["recent_workouts"][0]
    assert w["recovery_days_to_bb_baseline"] == 3  # 90 >= 80*0.95 on day +3
    assert w["recovery_days_to_rhr_baseline"] is None
    assert w["rhr_baseline_note"] == "no RHR baseline for this date"
    assert w["bb_baseline_note"] is None
    assert payload["avg_recovery_days_body_battery"] == 3.0
    assert payload["avg_recovery_days_rhr"] is None


def test_recovery_pattern_both_baselines_present_computes_both(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    db.init_schema(p)
    today = date.today()
    wdate = today - timedelta(days=10)
    with db.connect(p) as conn:
        conn.execute(
            "INSERT INTO activities (activity_id, date, activity_type, "
            "duration_seconds, distance_meters, training_load) "
            "VALUES (1, ?, 'running', 3600, 10000, 80)",
            (wdate.isoformat(),),
        )
        conn.execute(
            "INSERT INTO baselines (date, body_battery_max_60day_mean, rhr_60day_mean) "
            "VALUES (?, 80.0, 52.0)",
            (wdate.isoformat(),),
        )
        conn.execute(
            "INSERT INTO daily_metrics (date, body_battery_max, rhr) VALUES (?, 90, 50)",
            ((wdate + timedelta(days=1)).isoformat(),),
        )
    payload, err = call(tools.recovery_pattern, {"activity_type": "running", "lookback_days": 30})
    assert not err
    assert payload["n_workouts_matched"] == 1
    assert payload["n_skipped_no_baseline"] == 0
    assert payload["n_skipped_no_bb_baseline"] == 0
    assert payload["n_skipped_no_rhr_baseline"] == 0
    w = payload["recent_workouts"][0]
    assert w["recovery_days_to_bb_baseline"] == 1
    assert w["recovery_days_to_rhr_baseline"] == 1
    assert w["bb_baseline_note"] is None
    assert w["rhr_baseline_note"] is None


def test_recovery_pattern_neither_baseline_present_skips_and_counts(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    db.init_schema(p)
    today = date.today()
    wdate = today - timedelta(days=10)
    with db.connect(p) as conn:
        conn.execute(
            "INSERT INTO activities (activity_id, date, activity_type, "
            "duration_seconds, distance_meters, training_load) "
            "VALUES (1, ?, 'running', 3600, 10000, 80)",
            (wdate.isoformat(),),
        )
        # No baselines row at all for wdate.
    payload, err = call(tools.recovery_pattern, {"activity_type": "running", "lookback_days": 30})
    assert not err
    assert payload["n_workouts_matched"] == 0
    assert payload["n_skipped_no_baseline"] == 1
    assert payload["n_skipped_no_bb_baseline"] == 0
    assert payload["n_skipped_no_rhr_baseline"] == 0
    assert payload["recent_workouts"] == []


def test_recovery_pattern_issues_range_queries_not_per_workout_probes(
    seeded, monkeypatch
):
    """The N+1 rewrite: 1 workouts query + 2 range loads, total — the old
    shape issued 1 baselines + up to 7 daily_metrics probes PER workout
    (~953 statements on a year of running)."""
    executed: list[str] = []
    orig = db.connect

    from contextlib import contextmanager

    @contextmanager
    def traced(*a, **k):
        with orig(*a, **k) as conn:
            class _T:
                def execute(self, sql, *ar, **kw):
                    executed.append(sql.strip().split()[0].upper() + " " + sql[:60])
                    return conn.execute(sql, *ar, **kw)

                def __getattr__(self, name):
                    return getattr(conn, name)

            yield _T()

    monkeypatch.setattr(db, "connect", traced)
    payload, err = call(tools.recovery_pattern, {"activity_type": "run"})
    assert not err
    selects = [s for s in executed if s.startswith("SELECT")]
    assert len(selects) == 3  # workouts + baselines range + metrics range


def test_workout_report_card_read_phase_opens_at_most_three_connections(
    rc_seeded, reports_tmp, monkeypatch
):
    """S8: the read pipeline (inputs, profile, user_name, memory, stored
    read, has_event) shares ONE connection; only save_card (worker thread —
    sqlite3 same-thread check) and reflect's read block open their own.
    Was ~8."""
    counts = _count_connects(monkeypatch)
    payload, err = call(tools.workout_report_card, {"format": "table"})
    assert not err
    assert counts["n"] <= 3


def test_get_coach_personality_opens_one_connection(seeded, monkeypatch):
    counts = _count_connects(monkeypatch)
    payload, err = call(tools.get_coach_personality, {})
    assert not err
    assert counts["n"] == 1
    # The single-aggregate rewrite still reports both journal counts.
    assert payload["journal_entries"] == 0
    assert payload["journal_archived"] == 0


# --------------------------------------------------------------------------- #
# 0.37.0 UX pass: input validation, envelopes, error detail, interpretation
# --------------------------------------------------------------------------- #
def test_compare_periods_rejects_reversed_and_impossible_dates(seeded):
    """Reversed/malformed ranges used to return {"n": 0} — unreadable as
    'bad input' vs 'genuinely empty window'."""
    base = {"metric": "rhr", "period_a_start": "2026-07-10", "period_a_end": "2026-07-01",
            "period_b_start": "2026-06-01", "period_b_end": "2026-06-10"}
    payload, err = call(tools.compare_periods, base)
    assert err
    assert "period_a_start must be on or before period_a_end" in payload["error"]

    bad = dict(base, period_a_start="2026-13-45", period_a_end="2026-07-10")
    payload, err = call(tools.compare_periods, bad)
    assert err
    assert "period_a_start must be a valid YYYY-MM-DD" in payload["error"]


def test_find_anomalies_sd_threshold_bounds(seeded):
    for bad in (0, -1, 0.4, 11, "two"):
        payload, err = call(tools.find_anomalies, {"metric": "rhr", "sd_threshold": bad})
        assert err, bad
        assert "sd_threshold must be" in payload["error"]
    _payload, err = call(tools.find_anomalies, {"metric": "rhr", "sd_threshold": 0.5})
    assert not err


def test_query_workouts_min_distance_mi_and_km_alias(seeded):
    # 10 km activity: 6.22 mi excludes it, 6.0 mi... 10000m = 6.21mi so 6.3 excludes.
    payload, err = call(tools.query_workouts, {"min_distance_mi": 6.3})
    assert not err and payload["count"] == 0
    payload, err = call(tools.query_workouts, {"min_distance_mi": 6.0})
    assert not err and payload["count"] == 1
    # km alias still accepted; mi wins when both are given.
    payload, err = call(tools.query_workouts, {"min_distance_km": 9.5})
    assert not err and payload["count"] == 1
    payload, err = call(tools.query_workouts,
                        {"min_distance_mi": 6.3, "min_distance_km": 1.0})
    assert not err and payload["count"] == 0  # mi (excluding) won
    payload, err = call(tools.query_workouts, {"min_distance_mi": "fast"})
    assert err and "min_distance_mi must be a number" in payload["error"]


def test_query_workouts_limit_validation_and_truncation(seeded):
    payload, err = call(tools.query_workouts, {"limit": -1})
    assert err and "limit must be" in payload["error"]
    payload, err = call(tools.query_workouts, {"limit": "ten"})
    assert err and "limit must be" in payload["error"]
    # Add a second activity, then limit=1 → truncated.
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO activities (activity_id, date, activity_type, "
            "duration_seconds, distance_meters) VALUES (2, ?, 'running', 1200, 3000)",
            ((date.today() - timedelta(days=1)).isoformat(),))
    payload, err = call(tools.query_workouts, {"limit": 1})
    assert not err
    assert payload["count"] == 1 and payload["truncated"] is True


def test_list_coach_memories_truncated_flag(seeded):
    for i in range(3):
        journal.save_entry(f"memory number {i}", source="chat")
    payload, err = call(tools.list_coach_memories, {"limit": 2})
    assert not err
    assert payload["count"] == 2 and payload["truncated"] is True
    payload, err = call(tools.list_coach_memories, {"limit": 10})
    assert not err
    assert payload["count"] == 3 and payload["truncated"] is False


def test_run_sql_error_carries_the_sqlite_detail(seeded):
    payload, err = call(tools.run_sql, {"query": "SELECT sleep_hours FROM daily_metrics"})
    assert err
    assert "no such column: sleep_hours" in payload["error"]
    assert "fitness://schema" in payload["error"]


def test_save_brief_validation_error_is_compact_loc_msg_pairs(seeded):
    payload, err = call(tools.save_brief, {"brief": {
        "takeaways": [{"headline": "x", "summary": "y", "tone": "smug",
                       "details": "z"}]}})
    assert err
    msg = payload["error"]
    assert "takeaways.0.tone" in msg
    assert "https://errors.pydantic.dev" not in msg  # the noise is gone


def test_report_card_pdf_failure_returns_stable_reason_and_logs(rc_seeded, reports_tmp, monkeypatch, caplog):
    """0.56.0 contract: the error names the exception class + message (the
    run_sql precedent — render-stack detail, not secrets) and carries a
    remediation the agent can act on. The old "see the server log" was
    observed live (2026-08-03) as a dead end for an agent that cannot read
    the log. The full traceback still goes ONLY to the log."""
    from local_fitness.agent import visuals

    def boom(*_a, **_k):
        raise RuntimeError("cairo exploded with a 40-line traceback")

    monkeypatch.setattr(visuals, "render_report_card_pdf", boom)
    with caplog.at_level(logging.WARNING, logger="local_fitness.agent.tools"):
        payload, err = call(tools.workout_report_card, {})
    assert err
    assert payload["error"] == (
        "PDF render failed: RuntimeError: cairo exploded with a 40-line traceback"
    )
    # The recovery is named: the grading exists without the PDF.
    assert "format='table'" in payload["remediation"]
    assert any(r.exc_info for r in caplog.records)  # the traceback IS in the log


def test_trend_include_values_still_attaches_vs_baseline(seeded):
    """The baseline block the former get_metric attached (0.37.0) survives the
    fold-in: same numbers whether or not the raw series is requested."""
    _stamp_pull(seeded, completed_at=datetime.now().isoformat(),
                last_date_fetched=date.today().isoformat())
    payload, err = call(tools.get_metric_trend,
                        {"metric": "rhr", "days": 14, "include_values": True})
    assert not err
    # seeded: baseline mean 52.0 sd 2.0; newest value 50 → exactly -1.0 SD.
    assert payload["baseline_60day_mean"] == 52.0
    assert payload["current_vs_baseline_sd"] == -1.0
    assert payload["vs_baseline"] == "normal"


def test_trend_include_values_vs_baseline_no_data_for_non_baselined(seeded):
    payload, err = call(tools.get_metric_trend,
                        {"metric": "steps", "days": 14, "include_values": True})
    assert not err
    assert "current_vs_baseline_sd" not in payload
    assert payload["vs_baseline"] == "no data"


def test_training_load_status_description_renders_interpret_constants():
    # The registered description must carry the rendered band numbers from
    # interpret — built, not hand-written, so they can't drift from the
    # classifier attached to the payload.
    text = tools.training_load_status.description
    assert f"TSB > {interpret.TSB_FRESH:g} fresh" in text
    assert f"< {interpret.TSB_VERY_FATIGUED:g} very fatigued" in text


# --- 0.56.0: error envelopes, compaction, sync short-circuit -----------------


def test_unhandled_database_error_returns_envelope_with_remediation(seeded, monkeypatch):
    """The guarded `tool` wrapper: an unanticipated sqlite3.DatabaseError
    (observed live 2026-08-10 as a bare 'database disk image is malformed'
    with is_error unset) must come back as the standard _err envelope with a
    remediation the agent can act on. Patched at db.connect so the failure is
    exactly the corrupt-file shape no per-tool code anticipates."""
    def corrupt(*_a, **_k):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(db, "connect", corrupt)
    payload, err = call(tools.get_training_plan_status, {})
    assert err
    assert payload["error"] == "database error: database disk image is malformed"
    assert "integrity_check" in payload["remediation"]
    assert "do not retry" in payload["remediation"]


def test_every_registered_tool_carries_the_database_error_guard(seeded, monkeypatch):
    """The guard lives in the decorator, so membership in a registry IS the
    proof — but pin one more tool from each registry end to catch a future
    handler registered around the local `tool` wrapper."""
    def corrupt(*_a, **_k):
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(db, "connect", corrupt)
    for t in (tools.query_workouts, tools.list_report_cards):
        payload, err = call(t, {})
        assert err and "remediation" in payload, t.name


def test_query_workouts_rows_are_compact_in_miles_mode(seeded, monkeypatch):
    monkeypatch.delenv("LOCAL_FITNESS_DISPLAY_UNITS", raising=False)
    payload, err = call(tools.query_workouts, {})
    assert not err and payload["count"] >= 1
    w = payload["workouts"][0]
    # Display fields present, raw twins gone (the ~25% duplication cut).
    assert w["distance_mi"] == 6.21
    assert w["duration_formatted"]
    assert "distance_meters" not in w
    assert "duration_seconds" not in w
    assert "avg_pace_sec_per_km" not in w
    # Paceless seeded activity: effort stays as an explicit null.
    assert "effort" in w and w["effort"] is None


def test_query_workouts_rows_keep_raw_fields_in_km_mode(seeded, monkeypatch):
    monkeypatch.setenv("LOCAL_FITNESS_DISPLAY_UNITS", "km")
    payload, err = call(tools.query_workouts, {})
    assert not err
    w = payload["workouts"][0]
    assert w["distance_meters"] == 10000
    assert "distance_mi" not in w


def test_daily_snapshot_recent_workouts_use_the_same_compact_shape(seeded, monkeypatch):
    """status._recent_workouts used to be an inline byte-copy of the tools
    augmentation; both now flow through workout_rows.display_workout, so the
    two surfaces cannot drift."""
    monkeypatch.delenv("LOCAL_FITNESS_DISPLAY_UNITS", raising=False)
    payload, err = call(tools.daily_snapshot, {})
    assert not err
    w = payload["recent_workouts"][0]
    assert w["distance_mi"] == 6.21
    assert "distance_meters" not in w
    assert "duration_seconds" not in w


def test_training_load_status_static_legend_is_gone(seeded):
    """The 3-line interpretation dict re-shipped on every call while the tool
    description already carries the zone bands — same duplication once
    removed from correlate's legend."""
    payload, err = call(tools.training_load_status, {})
    assert not err
    assert "interpretation" not in payload
    assert payload["tsb_zone"]  # the computed read still rides along
    # The description now carries the CTL/ATL/TSB translations instead.
    assert "CTL = fitness" in tools.training_load_status.description


def test_list_observations_rejects_non_integer_limit(seeded):
    payload, err = call(tools.list_observations, {"limit": "abc"})
    assert err
    assert "limit must be an integer" in payload["error"]


def test_list_observations_rejects_negative_limit_no_false_truncated(seeded):
    """limit: -1 used to reach SQLite as LIMIT 0 — an empty page that then
    reported truncated: true about rows it never fetched."""
    payload, err = call(tools.list_observations, {"limit": -1})
    assert err
    assert "limit must be between" in payload["error"]
    assert "truncated" not in payload


def test_list_observations_limit_boundaries(seeded):
    for i in range(3):
        call(tools.log_observation, {"obs_type": "rpe", "value": 5 + i})
    payload, err = call(tools.list_observations, {"limit": 500})
    assert not err and payload["count"] == 3
    payload, err = call(tools.list_observations, {"limit": 501})
    assert err
    payload, err = call(tools.list_observations, {"limit": 2})
    assert not err and payload["count"] == 2 and payload["truncated"] is True


def _seed_ingest_run(minutes_ago: int, status: str = "success") -> None:
    from datetime import datetime
    completed = (datetime.now() - timedelta(minutes=minutes_ago)).isoformat()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO ingest_runs (started_at, completed_at, status, source) "
            "VALUES (?, ?, ?, 'daily')",
            (completed, completed, status),
        )


def test_sync_short_circuits_when_a_recent_pull_succeeded(seeded, monkeypatch):
    """8 of 24 recorded sync calls were pure repeats minutes apart, each a
    full Garmin round-trip that landed nothing. A successful run inside the
    freshness window now answers without touching Garmin at all."""
    _seed_ingest_run(minutes_ago=3)
    monkeypatch.setattr(
        daily_ingest_mod, "pull",
        lambda **_: pytest.fail("a fresh sync must not reach Garmin"),
    )
    payload, err = call(tools.sync_garmin_data, {})
    assert not err
    assert payload["status"] == "fresh"
    assert "force:true" in payload["sync_state"]
    # The report-card handoff still rides the short-circuit payload.
    assert payload["latest_activity"]["activity_id"] == 1


def test_sync_force_bypasses_the_short_circuit(seeded, monkeypatch):
    _seed_ingest_run(minutes_ago=3)
    calls = []
    monkeypatch.setattr(
        daily_ingest_mod, "pull",
        lambda **kw: calls.append(kw) or {
            "status": "success", "days_pulled": 1, "activities_loaded": 0,
            "last_date": date.today().isoformat(), "error": None,
        },
    )
    monkeypatch.setattr(tools.baselines_mod, "recompute", lambda **_: 90)
    payload, err = call(tools.sync_garmin_data, {"force": True})
    assert not err and payload["status"] == "success"
    assert len(calls) == 1


def test_sync_pulls_when_the_last_success_is_stale(seeded, monkeypatch):
    _seed_ingest_run(minutes_ago=45)  # outside the 10-min window
    calls = []
    monkeypatch.setattr(
        daily_ingest_mod, "pull",
        lambda **kw: calls.append(kw) or {
            "status": "success", "days_pulled": 1, "activities_loaded": 0,
            "last_date": date.today().isoformat(), "error": None,
        },
    )
    monkeypatch.setattr(tools.baselines_mod, "recompute", lambda **_: 90)
    payload, err = call(tools.sync_garmin_data, {})
    assert not err and len(calls) == 1


def test_sync_ignores_recent_failed_runs(seeded, monkeypatch):
    """A failed pull ten seconds ago is a reason to retry, not to skip."""
    _seed_ingest_run(minutes_ago=1, status="failure")
    calls = []
    monkeypatch.setattr(
        daily_ingest_mod, "pull",
        lambda **kw: calls.append(kw) or {
            "status": "success", "days_pulled": 1, "activities_loaded": 0,
            "last_date": date.today().isoformat(), "error": None,
        },
    )
    monkeypatch.setattr(tools.baselines_mod, "recompute", lambda **_: 90)
    payload, err = call(tools.sync_garmin_data, {})
    assert not err and len(calls) == 1


def test_sync_success_payload_carries_latest_activity_and_no_null_error(seeded, monkeypatch):
    """latest_activity is the report-card handoff (kills the observed
    sync -> query_workouts -> workout_report_card triple), and a success
    payload no longer ships "error": null (it fooled the audit's own
    error detector)."""
    monkeypatch.setattr(
        daily_ingest_mod, "pull",
        lambda **_: {
            "status": "success", "days_pulled": 1, "activities_loaded": 1,
            "last_date": date.today().isoformat(), "error": None,
        },
    )
    monkeypatch.setattr(tools.baselines_mod, "recompute", lambda **_: 90)
    payload, err = call(tools.sync_garmin_data, {})
    assert not err
    assert "error" not in payload
    latest = payload["latest_activity"]
    assert latest["activity_id"] == 1
    assert latest["date"] == date.today().isoformat()
    assert latest["distance_mi"] == 6.21


def test_sync_min_interval_env_override(seeded, monkeypatch):
    monkeypatch.setenv("LOCAL_FITNESS_SYNC_MIN_INTERVAL_MIN", "60")
    _seed_ingest_run(minutes_ago=45)  # stale under 10, fresh under 60
    monkeypatch.setattr(
        daily_ingest_mod, "pull",
        lambda **_: pytest.fail("45 min < 60 min window — must short-circuit"),
    )
    payload, err = call(tools.sync_garmin_data, {})
    assert not err and payload["status"] == "fresh"


def test_compare_periods_days_shortcut_derives_adjacent_windows(seeded):
    """days=N is the convenience the tool's zero recorded calls were missing:
    every 'last 30d vs prior 30d' ask forced four hand-computed ISO dates
    (run_sql hand-rolled the comparison instead)."""
    payload, err = call(tools.compare_periods, {"metric": "rhr", "days": 14})
    assert not err
    today = date.today()
    derived = payload["derived_periods"]
    assert derived["period_a_end"] == today.isoformat()
    assert derived["period_a_start"] == (today - timedelta(days=13)).isoformat()
    assert derived["period_b_end"] == (today - timedelta(days=14)).isoformat()
    assert derived["period_b_start"] == (today - timedelta(days=27)).isoformat()
    # Windows are adjacent and equal-length: 14 days each, no gap, no overlap.
    assert payload["period_a"]["n"] == 14
    assert payload["period_b"]["n"] == 14


def test_compare_periods_rejects_days_mixed_with_dates(seeded):
    payload, err = call(tools.compare_periods, {
        "metric": "rhr", "days": 14, "period_a_start": "2026-01-01",
    })
    assert err
    assert "not both" in payload["error"]


def test_compare_periods_names_both_forms_when_dates_missing(seeded):
    payload, err = call(tools.compare_periods, {
        "metric": "rhr", "period_a_start": "2026-01-01",
    })
    assert err
    assert "days=N" in payload["error"]


def test_plan_progress_rows_omit_nulls_and_raw_twins(seeded, monkeypatch):
    """The single worst context hog (24% of all returned chars across
    recorded sessions): pending days shipped 4+ null fields per row and every
    graded row carried raw/display pairs."""
    monkeypatch.delenv("LOCAL_FITNESS_DISPLAY_UNITS", raising=False)
    today = date.today()
    _, err = call(tools.propose_training_plan, {
        "goal_type": "10k",
        "race_date": (today + timedelta(days=30)).isoformat(),
        "workouts": [
            {"date": today.isoformat(), "week_index": 0, "type": "easy",
             "target_distance_m": 8046.7, "description": "Easy 5"},
            {"date": (today + timedelta(days=2)).isoformat(), "week_index": 0,
             "type": "rest", "description": "Rest day"},
        ],
    })
    assert not err
    _, err = call(tools.commit_training_plan, {"plan_id": 1})
    assert not err
    payload, err = call(tools.get_training_plan_progress, {"full": True})
    assert not err
    rows = {w["date"]: w for w in payload["workouts"]}
    easy = rows[today.isoformat()]
    # Display twin present, raw dropped.
    assert easy["target_distance_mi"] == 5.0
    assert "target_distance_m" not in easy
    # The rest day has no targets at all — nulls omitted, not shipped.
    rest = rows[(today + timedelta(days=2)).isoformat()]
    assert "target_distance_m" not in rest and "target_distance_mi" not in rest
    assert "target_pace_sec_per_km" not in rest
    assert "actual_distance_m" not in rest
    for w in payload["workouts"]:
        assert all(v is not None for v in w.values()), w


# --------------------------------------------------------------------------- #
# 0.59.0: SETTLING_METRICS — values Garmin REVISES through the day (rhr and
# sleep settle once the night is fully processed; body battery min/max move
# until the day ends). Measured live 2026-08-10: a 06:30 mid-sleep pull stored
# rhr 54, served at 10:04 as "elevated, +1.93 SD"; the 10:09 post-wake pull
# revised it to 50. The contract: a settled verdict is never derived from an
# unsettled number — stale ⇒ today drops out of every stat (raw reading kept,
# labeled), fresh (a pull covering today within the sync window) ⇒ counted
# but labeled provisional. Freshness is coverage-filtered: only a successful
# run whose last_date_fetched reached today counts.
# --------------------------------------------------------------------------- #
def _stamp_pull(p, *, completed_at: str, last_date_fetched: str, status: str = "success"):
    with db.connect(p) as conn:
        conn.execute(
            "INSERT INTO ingest_runs (started_at, completed_at, status, "
            "last_date_fetched, source) VALUES (?, ?, ?, ?, 'daily')",
            (completed_at, completed_at, status, last_date_fetched),
        )


def test_stale_settling_metric_never_drives_the_verdict(seeded):
    # seeded has NO ingest_runs rows -> today's rhr (50) is an unattributable
    # snapshot -> stale. current/mean/slope/vs_baseline anchor on yesterday
    # (51); today's raw reading stays visible, labeled.
    payload, err = call(tools.get_metric_trend,
                        {"metric": "rhr", "days": 14, "include_values": True})
    assert not err
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    assert payload["provisional_today_excluded"] is True
    assert payload["provisional_today_value"] == 50  # today's snapshot, i=0
    assert payload["current"] == 51                  # yesterday, i=1: 50 + 1
    assert payload["values"][-1]["date"] == yesterday
    # The verdict is computed on the settled series: (51 - 52) / 2.0.
    assert payload["current_vs_baseline_sd"] == -0.5
    assert "sync_garmin_data" in payload["note"]
    assert "data_as_of" not in payload  # no pull covering today exists at all


def test_fresh_settling_metric_keeps_today_labeled_provisional(seeded):
    _stamp_pull(seeded, completed_at=datetime.now().isoformat(),
                last_date_fetched=date.today().isoformat())
    payload, err = call(tools.get_metric_trend, {"metric": "rhr", "days": 14})
    assert not err
    assert payload["current"] == 50  # today's value counts when fresh
    assert payload["current_provisional"] is True
    assert payload["data_as_of"]  # stamped by the covering pull
    assert "provisional_today_excluded" not in payload
    assert "note" not in payload


def test_a_recent_backfill_does_not_count_as_covering_today(seeded):
    # A pull that completed seconds ago but only reached 30 days back never
    # touched today's row — counting it would stamp a stale snapshot "fresh",
    # the exact direction of lie data_as_of_today exists to prevent.
    _stamp_pull(seeded, completed_at=datetime.now().isoformat(),
                last_date_fetched=(date.today() - timedelta(days=30)).isoformat())
    payload, err = call(tools.get_metric_trend, {"metric": "rhr", "days": 14})
    assert not err
    assert payload["provisional_today_excluded"] is True
    assert payload["current"] == 51
    assert "data_as_of" not in payload


def test_an_hours_old_pull_covering_today_is_stale_but_stamped(seeded):
    three_hours_ago = (datetime.now() - timedelta(hours=3)).isoformat()
    _stamp_pull(seeded, completed_at=three_hours_ago,
                last_date_fetched=date.today().isoformat())
    payload, err = call(tools.get_metric_trend, {"metric": "rhr", "days": 14})
    assert not err
    assert payload["provisional_today_excluded"] is True
    assert payload["data_as_of"] == three_hours_ago  # stamped, still stale
    assert payload["current"] == 51


def test_failed_pull_never_counts_as_fresh(seeded):
    _stamp_pull(seeded, completed_at=datetime.now().isoformat(),
                last_date_fetched=date.today().isoformat(), status="failure")
    payload, err = call(tools.get_metric_trend, {"metric": "rhr", "days": 14})
    assert not err
    assert payload["provisional_today_excluded"] is True
    assert "data_as_of" not in payload


def test_settling_guard_only_fires_when_today_has_a_row(seeded):
    # Delete today's rhr: the newest row is yesterday's, already settled —
    # no flags, no exclusion, regardless of ingest_runs state.
    with db.connect(seeded) as conn:
        conn.execute("UPDATE daily_metrics SET rhr = NULL WHERE date = ?",
                     (date.today().isoformat(),))
    payload, err = call(tools.get_metric_trend, {"metric": "rhr", "days": 14})
    assert not err
    assert payload["current"] == 51
    assert "provisional_today_excluded" not in payload
    assert "current_provisional" not in payload


def test_find_anomalies_never_scans_today(seeded):
    # Today at rhr 80 is 14 SD out — and still must not scan as an anomaly,
    # because rhr settles through the morning (the live incident: a mid-sleep
    # 54, revised to 50 post-wake, would have scanned as a +2 SD spike). The
    # same 80 on a COMPLETE day is exactly what the tool exists to find.
    past = (date.today() - timedelta(days=10)).isoformat()
    with db.connect(seeded) as conn:
        conn.execute("UPDATE daily_metrics SET rhr = 80 WHERE date = ?",
                     (date.today().isoformat(),))
        conn.execute("UPDATE daily_metrics SET rhr = 80 WHERE date = ?", (past,))
    payload, err = call(tools.find_anomalies, {"metric": "rhr"})
    assert not err
    dates = [a["date"] for a in payload["anomalies"]]
    assert past in dates
    assert date.today().isoformat() not in dates


def test_daily_snapshot_tool_opts_into_the_settling_guard(seeded):
    # The MCP tool serves ad-hoc reads with no pull in front of them — the
    # one snapshot surface where the guard must be on. No ingest_runs rows
    # in seeded -> stale -> the rhr row anchors to yesterday, labeled.
    payload, err = call(tools.daily_snapshot, {})
    assert not err
    rhr_row = next(r for r in payload["metrics"] if r["metric"] == "rhr")
    assert rhr_row["provisional_today_excluded"] is True
    assert rhr_row["provisional_today_value"] == 50
    assert rhr_row["value"] == 51
