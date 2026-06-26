"""Tests for agent/tools.py — the MCP tool handlers that query the DB.

The handlers are async and return ``{"content": [{"type": "text", "text": ...}]}``.
We call them directly against a seeded tmp DB (no SDK runtime, no network).
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta

import pytest

from local_fitness import db
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
    # No style -> calendar (the default). It must be the week-stacked grid (the
    # "M  T  W  T  F  S  S" header is its signature) and COMPACT: a 30-day window
    # is a handful of week-rows, never one row per day (the truncation bug fix).
    text, err = call(tools.chart, {"metric": "rhr", "days": 30})
    assert not err
    assert "rhr · last 30d" in text
    assert "M  T  W  T  F  S  S" in text          # calendar header
    assert any(sq in text for sq in tools.charts._HEAT)
    assert len(text.splitlines()) <= 10           # ~30 days -> <=5 weeks + headers


def test_chart_bar_style_is_one_row_per_day(seeded):
    # Explicit bar style is still available and is one row per day (best for
    # short windows) — distinctly taller than the calendar for the same window.
    text, err = call(tools.chart, {"metric": "rhr", "days": 14, "style": "bar"})
    assert not err
    assert any(sq in text for sq in tools.charts._HEAT)
    assert "M  T  W  T  F  S  S" not in text       # NOT the calendar
    assert len([ln for ln in text.splitlines() if any(s in ln for s in tools.charts._HEAT)]) >= 10


def test_chart_calendar_cumulative_steps_weekly_sum(seeded):
    # steps is an additive metric -> the calendar's weekly column is a SUM, not a
    # mean. The fixture seeds 9000 steps/day and any 14-day window contains a full
    # Mon-Sun week, so a 7*9000 = 63000 weekly total must appear (proves the tool
    # routes steps with cumulative=True).
    text, err = call(tools.chart, {"metric": "steps", "days": 14})
    assert not err
    assert "M  T  W  T  F  S  S" in text
    assert "63000" in text


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
    ):
        assert f"mcp__{tools.SERVER_NAME}__{w}" not in ro
    assert ro < set(tools.allowed_tool_names())
