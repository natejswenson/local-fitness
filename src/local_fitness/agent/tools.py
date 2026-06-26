"""Claude Agent SDK tools that query the fitness DB.

All tools return text content (JSON-encoded payloads) so the model can reason
over them. Optional-arg tools use full JSON Schema; required-only tools use
the {name: type} shorthand. SQL strings are constructed with whitelisted
column names — no user input ever interpolates into SQL except via params.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import date, datetime, timedelta
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from pydantic import ValidationError

from .. import config, db, notes, plans
from . import briefs, charts, units


SERVER_NAME = "fitness"

BASELINE_METRICS = {"rhr", "sleep_seconds"}

# The single source of truth for observation-type validation. Numeric types
# (weight/rpe/soreness/energy/mood) store into value_num via `value`; free-text
# types (feeling/injury/note) store into value_text via `text`.
OBS_TYPES = frozenset({
    "weight", "rpe", "soreness", "energy", "mood",
    "feeling", "injury", "note",
})
# Single source of truth for which obs_types store into value_num (via `value`)
# vs value_text (via `text`). Text types are derived so the two can't drift.
NUMERIC_OBS_TYPES = frozenset({"weight", "rpe", "soreness", "energy", "mood"})
assert NUMERIC_OBS_TYPES <= OBS_TYPES

# Source of truth for the queryable table/column list advertised by run_sql and
# rendered by the fitness://schema MCP resource. Keep these in sync by rendering
# both from this one constant so the advertised list can't drift.
QUERYABLE_SCHEMA: dict[str, list[str]] = {
    "daily_metrics": [
        "date", "sleep_seconds", "sleep_deep_seconds", "sleep_light_seconds",
        "sleep_rem_seconds", "sleep_awake_seconds", "sleep_score",
        "sleep_quality", "rhr", "avg_stress", "max_stress",
        "body_battery_min", "body_battery_max", "body_battery_charged",
        "body_battery_drained", "steps", "active_calories", "floors_climbed",
        "avg_spo2", "respiration_avg", "vo2_max", "training_status",
        "fitness_age", "intensity_minutes_moderate", "intensity_minutes_vigorous",
    ],
    "activities": [
        "activity_id", "date", "start_time", "activity_type", "activity_name",
        "duration_seconds", "moving_seconds", "distance_meters", "avg_hr",
        "max_hr", "avg_pace_sec_per_km", "elevation_gain_meters",
        "elevation_loss_meters", "calories", "aerobic_te", "anaerobic_te",
        "training_load", "avg_cadence", "vo2_max_estimate", "weather_temp_c",
        "weather_conditions", "source",
    ],
    "activity_splits": [
        "activity_id", "split_index", "distance_meters", "duration_seconds",
        "avg_hr", "avg_pace_sec_per_km", "elevation_gain_meters",
    ],
    "activity_hr_zones": ["activity_id", "zone", "seconds_in_zone"],
    "body_battery_samples": ["date", "timestamp", "value"],
    "stress_samples": ["date", "timestamp", "value"],
    "baselines": [
        "date", "rhr_60day_mean", "rhr_60day_sd",
        "body_battery_max_60day_mean", "body_battery_min_60day_mean",
        "sleep_seconds_60day_mean", "sleep_seconds_60day_sd",
        "stress_60day_mean", "ctl", "atl", "tsb",
    ],
    "observations": [
        "observation_id", "observed_on", "created_at", "obs_type",
        "value_num", "value_text", "activity_id",
    ],
}


def _render_schema() -> str:
    """One-line `table(col, col, ...); ...` rendering of QUERYABLE_SCHEMA, used
    in run_sql's advertised table list so it can't drift from the source."""
    return "; ".join(
        f"{table}({', '.join(cols)})" for table, cols in QUERYABLE_SCHEMA.items()
    )


DAILY_NUMERIC_METRICS = {
    "sleep_seconds", "sleep_score", "sleep_deep_seconds", "sleep_rem_seconds",
    "sleep_light_seconds", "sleep_awake_seconds",
    "rhr", "avg_stress", "max_stress",
    "body_battery_min", "body_battery_max",
    "body_battery_charged", "body_battery_drained",
    "steps", "active_calories", "vo2_max",
    "intensity_minutes_moderate", "intensity_minutes_vigorous",
}


def _text(payload: Any) -> dict:
    # Compact JSON (no indent) — fewer whitespace tokens across the multi-turn
    # agent loop; the model parses either format.
    if not isinstance(payload, str):
        payload = json.dumps(payload, default=str)
    return {"content": [{"type": "text", "text": payload}]}


def _err(msg: str, **extra) -> dict:
    return {"content": [{"type": "text", "text": json.dumps({"error": msg, **extra})}], "is_error": True}


def _validate_days(value: Any, name: str = "days", *, lo: int = 1, hi: int = 3650) -> str | None:
    """Bounds-check a user-supplied day count before it reaches timedelta().

    Returns an error string (for the caller to wrap with ``_err``) or ``None``
    when valid. ``timedelta(days=N)`` raises a raw OverflowError once N exceeds
    ~10**9; the REST layer clamps these via ``Query(ge=, le=)`` but the tool
    surface didn't. Rejects non-ints (and bool, since ``isinstance(True, int)``
    is True) and anything outside ``[lo, hi]`` with a clean, bounded message.
    Mirrors how these tools already reject bad metric names (a clear error, not
    a silent clamp).
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return f"{name} must be an integer between {lo} and {hi}"
    if value < lo or value > hi:
        return f"{name} must be between {lo} and {hi}"
    return None


def _augment_workout(w: dict) -> dict:
    """Attach mile / formatted convenience fields ALONGSIDE the raw columns.

    Raw fields (distance_meters, avg_pace_sec_per_km, duration_seconds) are
    never dropped — correlate / run_sql depend on them. A convenience field is
    only added when units.py returns non-None (null / zero → omitted). The
    ``distance_mi`` field is suppressed entirely when display units aren't miles.
    """
    if units.display_units() == "miles":
        distance_mi = units.to_miles(w.get("distance_meters"))
        if distance_mi is not None:
            w["distance_mi"] = distance_mi
    pace = units.format_pace_min_per_mi(w.get("avg_pace_sec_per_km"))
    if pace is not None:
        w["pace_min_per_mi"] = pace
    duration = units.format_duration(w.get("duration_seconds"))
    if duration is not None:
        w["duration_formatted"] = duration
    return w


@tool(
    "get_today_status",
    "Today's metrics + last 7 days alongside the latest 60-day baselines. Call this first when assessing recovery or making 'should I train hard' decisions.",
    {},
)
async def get_today_status(_args: dict) -> dict:
    today = date.today().isoformat()
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    with db.connect() as conn:
        recent = [dict(r) for r in conn.execute(
            "SELECT date, sleep_seconds, sleep_score, rhr, avg_stress, "
            "body_battery_min, body_battery_max, steps, "
            "intensity_minutes_moderate, intensity_minutes_vigorous "
            "FROM daily_metrics WHERE date >= ? ORDER BY date DESC",
            (week_ago,),
        ).fetchall()]
        baseline = conn.execute(
            "SELECT * FROM baselines WHERE date <= ? ORDER BY date DESC LIMIT 1",
            (today,),
        ).fetchone()
    return _text({
        "today": today,
        "recent_days": recent,
        "current_baseline": dict(baseline) if baseline else None,
    })


@tool(
    "get_metric",
    "Get raw daily values for one metric over the last N days. Returns time series sorted oldest-first.",
    {"metric": str, "days": int},
)
async def get_metric(args: dict) -> dict:
    metric = args["metric"]
    if metric not in DAILY_NUMERIC_METRICS:
        return _err(f"unknown metric '{metric}'", allowed=sorted(DAILY_NUMERIC_METRICS))
    err = _validate_days(args["days"])
    if err:
        return _err(err)
    days = args["days"]
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT date, {metric} AS value FROM daily_metrics WHERE date >= ? ORDER BY date",
            (cutoff,),
        ).fetchall()
    return _text([dict(r) for r in rows])


@tool(
    "get_metric_trend",
    "Trend stats (mean, slope, current vs baseline) for a metric over N days.",
    {"metric": str, "days": int},
)
async def get_metric_trend(args: dict) -> dict:
    metric = args["metric"]
    if metric not in DAILY_NUMERIC_METRICS:
        return _err(f"unknown metric '{metric}'", allowed=sorted(DAILY_NUMERIC_METRICS))
    # lo=2: a trend (slope, current-vs-mean) is meaningless on a single sample.
    err = _validate_days(args["days"], lo=2)
    if err:
        return _err(err)
    days = args["days"]
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT date, {metric} AS v FROM daily_metrics "
            f"WHERE date >= ? AND {metric} IS NOT NULL ORDER BY date",
            (cutoff,),
        ).fetchall()
        baseline = None
        if metric in BASELINE_METRICS:
            baseline = conn.execute(
                f"SELECT {metric}_60day_mean AS m, {metric}_60day_sd AS sd "
                f"FROM baselines WHERE {metric}_60day_mean IS NOT NULL "
                f"ORDER BY date DESC LIMIT 1"
            ).fetchone()
    if not rows:
        return _err("no data in window", metric=metric, days=days)
    values = [r["v"] for r in rows]
    n = len(values)
    mean = sum(values) / n
    xs = list(range(n))
    x_mean = (n - 1) / 2
    denom = sum((x - x_mean) ** 2 for x in xs) or 1e-9
    slope = sum((xs[i] - x_mean) * (values[i] - mean) for i in range(n)) / denom
    payload = {
        "metric": metric,
        "days_window": days,
        "n_samples": n,
        "mean": mean,
        "current": values[-1],
        "slope_per_day": slope,
    }
    if baseline and baseline["m"] is not None:
        payload["baseline_60day_mean"] = baseline["m"]
        payload["baseline_60day_sd"] = baseline["sd"]
        if baseline["sd"]:
            payload["current_vs_baseline_sd"] = (values[-1] - baseline["m"]) / baseline["sd"]
    return _text(payload)


# Metrics the chart tool can plot: every daily numeric column, the three
# training-load series from `baselines` (fitness / fatigue / freshness), and one
# derived series — Garmin's weekly-badge "active minutes" (moderate + 2×vigorous).
# Used as a frozen whitelist before any column name reaches an f-string, same as
# get_metric. Derived/baseline names are mapped to safe SQL below, never f-strung
# from user input.
_CHART_BASELINE_METRICS = frozenset({"ctl", "atl", "tsb"})
_CHART_DERIVED_METRICS = frozenset({"intensity_minutes_weighted"})
_CHART_METRICS = frozenset(DAILY_NUMERIC_METRICS) | _CHART_BASELINE_METRICS | _CHART_DERIVED_METRICS
_CHART_STYLES = frozenset({"calendar", "line", "bar", "combo", "spark"})
# Additive metrics get a weekly SUM in the calendar's right column; everything
# else (level metrics like rhr/tsb/vo2) gets the weekly mean of present days.
_CHART_CUMULATIVE_METRICS = frozenset({
    "steps", "active_calories", "floors_climbed",
    "intensity_minutes_moderate", "intensity_minutes_vigorous",
    "intensity_minutes_weighted", "body_battery_charged", "body_battery_drained",
})

_CHART_SCHEMA = {
    "type": "object",
    "properties": {
        "metric": {
            "type": "string",
            "description": (
                "Any daily metric (rhr, sleep_seconds, steps, "
                "intensity_minutes_moderate/vigorous, vo2_max, ...), a "
                "training-load series (ctl=fitness, atl=fatigue, tsb=freshness), "
                "or intensity_minutes_weighted (Garmin active minutes, mod+2×vig)."
            ),
        },
        "days": {"type": "integer", "description": "Look back this many days"},
        "style": {
            "type": "string",
            "enum": ["calendar", "line", "bar", "combo", "spark"],
            "description": (
                "calendar = week-stacked emoji heat-grid (default; compact and "
                "fully visible for any window); line = colored value-line (heat "
                "emoji on an invisible canvas; weekly-averaged past ~5 weeks so it "
                "fits); bar = emoji-color horizontal bars, one row per day (best ≤ "
                "~2 weeks); combo = 2D vertical bars + trend line (mono, handles "
                "negatives like TSB); spark = one-line sparkline."
            ),
        },
    },
    "required": ["metric", "days"],
}


def _chart_value_fmt(metric: str):
    """Per-metric value formatter so the chart shows hours / decimals sensibly."""
    if metric.endswith("_seconds"):
        return lambda v: f"{v / 3600:.1f}h"
    # Baselines (ctl/atl/tsb) and vo2_max move in fractions across a realistic
    # window (vo2_max 47.9→48.4); integer rounding would collapse every axis
    # label to one value. Genuinely-integer metrics (steps, intensity, rhr) stay
    # integer-formatted below.
    if metric in _CHART_BASELINE_METRICS or metric == "vo2_max":
        return lambda v: f"{v:.1f}"
    return lambda v: f"{int(round(v))}"


@tool("chart", "Render a terminal chart (ASCII/emoji) of a metric over the last N days. styles: calendar (compact week-stacked heat-grid, default — fully visible for any window), line (colored value-line, weekly-averaged for long windows), bar (emoji-color rows, best ≤2wk), combo (2D bars + trend line, handles negatives), spark (one-liner).", _CHART_SCHEMA)
async def chart(args: dict) -> dict:
    metric = args["metric"]
    if metric not in _CHART_METRICS:
        return _err(f"unknown metric '{metric}'", allowed=sorted(_CHART_METRICS))
    err = _validate_days(args["days"])
    if err:
        return _err(err)
    style = args.get("style") or "calendar"
    if style not in _CHART_STYLES:
        return _err(f"unknown style '{style}'", allowed=sorted(_CHART_STYLES))
    days = args["days"]
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    # metric is whitelisted above; the column name interpolated here can only be
    # a frozen-set member, never raw user input — same contract as get_metric.
    if metric in _CHART_BASELINE_METRICS:
        sql = (f"SELECT date, {metric} AS v FROM baselines "
               f"WHERE date >= ? AND {metric} IS NOT NULL ORDER BY date")
    elif metric == "intensity_minutes_weighted":
        sql = ("SELECT date, (intensity_minutes_moderate + 2 * intensity_minutes_vigorous) AS v "
               "FROM daily_metrics WHERE date >= ? AND intensity_minutes_moderate IS NOT NULL "
               "AND intensity_minutes_vigorous IS NOT NULL ORDER BY date")
    else:
        sql = (f"SELECT date, {metric} AS v FROM daily_metrics "
               f"WHERE date >= ? AND {metric} IS NOT NULL ORDER BY date")

    with db.connect() as conn:
        rows = conn.execute(sql, (cutoff,)).fetchall()
    if not rows:
        return _err("no data in window", metric=metric, days=days)

    dates = [r["date"] for r in rows]       # ISO YYYY-MM-DD
    labels = [d[5:] for d in dates]         # MM-DD
    values = [float(r["v"]) for r in rows]
    fmt = _chart_value_fmt(metric)
    title = f"{metric} · last {days}d · n={len(values)}"

    if style == "spark":
        body = f"{title}\n{charts.render_sparkline(values)}  {fmt(min(values))}..{fmt(max(values))}"
    elif style == "line":
        body = charts.render_line(dates, values, value_fmt=fmt, title=title)
    elif style == "combo":
        body = charts.render_combo_chart(labels, values, value_fmt=fmt, title=title)
    elif style == "bar":
        body = charts.render_bar_chart(labels, values, value_fmt=fmt, title=title)
    else:  # calendar (default)
        body = charts.render_calendar(
            dates, values, value_fmt=fmt, title=title,
            cumulative=metric in _CHART_CUMULATIVE_METRICS,
        )
    return _text(body)


_QUERY_WORKOUTS_SCHEMA = {
    "type": "object",
    "properties": {
        "activity_type": {"type": "string", "description": "Substring match, e.g. 'running'"},
        "days": {"type": "integer", "description": "Look back this many days"},
        "min_distance_km": {"type": "number"},
        "min_duration_min": {"type": "integer"},
        "limit": {"type": "integer", "description": "Max rows, default 50"},
    },
    "required": [],
}


@tool(
    "query_workouts",
    "List workouts with optional filters (activity_type substring, days lookback, distance/duration mins). Returns most recent first.",
    _QUERY_WORKOUTS_SCHEMA,
)
async def query_workouts(args: dict) -> dict:
    where: list[str] = []
    params: list = []
    if args.get("activity_type"):
        where.append("activity_type LIKE ?")
        params.append(f"%{args['activity_type']}%")
    if args.get("days"):
        err = _validate_days(args["days"])
        if err:
            return _err(err)
        where.append("date >= ?")
        params.append((date.today() - timedelta(days=args["days"])).isoformat())
    if args.get("min_distance_km"):
        where.append("distance_meters >= ?")
        params.append(float(args["min_distance_km"]) * 1000)
    if args.get("min_duration_min"):
        where.append("duration_seconds >= ?")
        params.append(int(args["min_duration_min"]) * 60)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    limit = int(args.get("limit") or 50)
    with db.connect() as conn:
        rows = conn.execute(
            f"""SELECT activity_id, date, activity_type, activity_name, duration_seconds,
                       distance_meters, avg_hr, max_hr, avg_pace_sec_per_km,
                       elevation_gain_meters, aerobic_te, anaerobic_te, training_load
                FROM activities {where_sql} ORDER BY date DESC, start_time DESC LIMIT ?""",
            (*params, limit),
        ).fetchall()
    return _text([_augment_workout(dict(r)) for r in rows])


@tool(
    "get_workout_detail",
    "Full detail for one workout — splits and HR zones included.",
    {"activity_id": int},
)
async def get_workout_detail(args: dict) -> dict:
    aid = int(args["activity_id"])
    with db.connect() as conn:
        act = conn.execute(
            "SELECT * FROM activities WHERE activity_id = ?", (aid,)
        ).fetchone()
        if not act:
            return _err("activity not found", activity_id=aid)
        zones = [dict(r) for r in conn.execute(
            "SELECT zone, seconds_in_zone FROM activity_hr_zones "
            "WHERE activity_id = ? ORDER BY zone",
            (aid,),
        ).fetchall()]
        splits = [dict(r) for r in conn.execute(
            "SELECT * FROM activity_splits WHERE activity_id = ? ORDER BY split_index",
            (aid,),
        ).fetchall()]
    activity = _augment_workout(dict(act))
    activity.pop("raw_json", None)
    return _text({"activity": activity, "hr_zones": zones, "splits": splits})


@tool(
    "compare_periods",
    "Compare a metric between two ISO date ranges. Returns mean, SD, count for each + delta. Use for things like 'last 30d vs prior 30d'.",
    {
        "metric": str,
        "period_a_start": str,
        "period_a_end": str,
        "period_b_start": str,
        "period_b_end": str,
    },
)
async def compare_periods(args: dict) -> dict:
    metric = args["metric"]
    if metric == "training_load":
        table = "activities"
    elif metric in DAILY_NUMERIC_METRICS:
        table = "daily_metrics"
    else:
        return _err(f"unknown metric '{metric}'", allowed=sorted(DAILY_NUMERIC_METRICS | {"training_load"}))

    def _stats(conn, start: str, end: str) -> dict:
        rows = conn.execute(
            f"SELECT {metric} AS v FROM {table} "
            f"WHERE date >= ? AND date <= ? AND {metric} IS NOT NULL",
            (start, end),
        ).fetchall()
        vals = [r["v"] for r in rows]
        if not vals:
            return {"n": 0, "mean": None, "sd": None}
        m = sum(vals) / len(vals)
        sd = (sum((v - m) ** 2 for v in vals) / max(len(vals) - 1, 1)) ** 0.5
        return {"n": len(vals), "mean": m, "sd": sd}

    with db.connect() as conn:
        a = _stats(conn, args["period_a_start"], args["period_a_end"])
        b = _stats(conn, args["period_b_start"], args["period_b_end"])
    delta = (a["mean"] - b["mean"]) if (a["mean"] is not None and b["mean"] is not None) else None
    return _text({"metric": metric, "period_a": a, "period_b": b, "delta_mean_a_minus_b": delta})


_FIND_ANOMALIES_SCHEMA = {
    "type": "object",
    "properties": {
        "metric": {"type": "string", "enum": ["rhr", "sleep_seconds"]},
        "lookback_days": {"type": "integer", "description": "Default 90"},
        "sd_threshold": {"type": "number", "description": "Default 2.0"},
    },
    "required": ["metric"],
}


@tool(
    "find_anomalies",
    "Days where a metric was more than N standard deviations from its 60-day baseline. Currently supports rhr and sleep_seconds.",
    _FIND_ANOMALIES_SCHEMA,
)
async def find_anomalies(args: dict) -> dict:
    metric = args["metric"]
    if metric not in BASELINE_METRICS:
        return _err("only baseline-tracked metrics supported", allowed=sorted(BASELINE_METRICS))
    days = args.get("lookback_days") or 90
    err = _validate_days(days, name="lookback_days")
    if err:
        return _err(err)
    threshold = float(args.get("sd_threshold") or 2.0)
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with db.connect() as conn:
        rows = conn.execute(
            f"""SELECT dm.date, dm.{metric} AS value,
                       b.{metric}_60day_mean AS baseline_mean,
                       b.{metric}_60day_sd AS baseline_sd
                FROM daily_metrics dm
                LEFT JOIN baselines b ON b.date = dm.date
                WHERE dm.date >= ? AND dm.{metric} IS NOT NULL
                  AND b.{metric}_60day_mean IS NOT NULL
                  AND b.{metric}_60day_sd > 0
                  AND ABS(dm.{metric} - b.{metric}_60day_mean) > b.{metric}_60day_sd * ?
                ORDER BY dm.date DESC""",
            (cutoff, threshold),
        ).fetchall()
    return _text({
        "metric": metric,
        "lookback_days": days,
        "sd_threshold": threshold,
        "anomalies": [dict(r) for r in rows],
    })


@tool(
    "training_load_status",
    "Current CTL/ATL/TSB plus 30-day history. TSB > 5 fresh, -10..5 neutral, < -10 fatigued, < -20 very fatigued.",
    {},
)
async def training_load_status(_args: dict) -> dict:
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    with db.connect() as conn:
        recent = [dict(r) for r in conn.execute(
            "SELECT date, ctl, atl, tsb FROM baselines "
            "WHERE date >= ? AND ctl IS NOT NULL ORDER BY date DESC",
            (cutoff,),
        ).fetchall()]
    if not recent:
        return _err("no training-load data yet — pull activities and run recompute-baselines")
    return _text({
        "current": recent[0],
        "history_30d": recent,
        "interpretation": {
            "ctl": "chronic training load (fitness) — 42-day EWMA of activity training_load",
            "atl": "acute training load (fatigue) — 7-day EWMA",
            "tsb": "training stress balance (form) = CTL - ATL",
        },
    })


_CORRELATE_SCHEMA = {
    "type": "object",
    "properties": {
        "metric_a": {"type": "string"},
        "metric_b": {"type": "string"},
        "days": {"type": "integer"},
        "lag_days": {"type": "integer", "description": "Default 0. Positive = b lags a."},
    },
    "required": ["metric_a", "metric_b", "days"],
}


@tool(
    "correlate",
    "Pearson correlation between two daily metrics over N days, optionally with a lag. Example: does sleep on day N predict RHR on day N+1?",
    _CORRELATE_SCHEMA,
)
async def correlate(args: dict) -> dict:
    a = args["metric_a"]
    b = args["metric_b"]
    if a not in DAILY_NUMERIC_METRICS or b not in DAILY_NUMERIC_METRICS:
        return _err("metrics must be daily numeric", allowed=sorted(DAILY_NUMERIC_METRICS))
    err = _validate_days(args["days"])
    if err:
        return _err(err)
    days = args["days"]
    # lag may legitimately be 0 or negative (sign flips which metric leads);
    # bound its magnitude so days + abs(lag) + 1 can't overflow timedelta().
    lag = args.get("lag_days") or 0
    lag_err = _validate_days(lag, name="lag_days", lo=-365, hi=365)
    if lag_err:
        return _err(lag_err)
    cutoff = (date.today() - timedelta(days=days + abs(lag) + 1)).isoformat()
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            f"SELECT date, {a} AS a_val, {b} AS b_val "
            f"FROM daily_metrics WHERE date >= ? ORDER BY date",
            (cutoff,),
        ).fetchall()]
    by_date = {r["date"]: r for r in rows}
    pairs: list[tuple[float, float]] = []
    for r in rows:
        if r["a_val"] is None:
            continue
        d = date.fromisoformat(r["date"])
        target = (d + timedelta(days=lag)).isoformat()
        partner = by_date.get(target)
        if partner and partner["b_val"] is not None:
            pairs.append((float(r["a_val"]), float(partner["b_val"])))
    n = len(pairs)
    if n < 5:
        return _err("insufficient paired data", n=n)
    mean_a = sum(p[0] for p in pairs) / n
    mean_b = sum(p[1] for p in pairs) / n
    cov = sum((p[0] - mean_a) * (p[1] - mean_b) for p in pairs) / n
    var_a = sum((p[0] - mean_a) ** 2 for p in pairs) / n
    var_b = sum((p[1] - mean_b) ** 2 for p in pairs) / n
    denom = (var_a * var_b) ** 0.5
    r_val = (cov / denom) if denom else None
    return _text({
        "metric_a": a, "metric_b": b, "days": days, "lag_days": lag,
        "n_pairs": n, "pearson_r": r_val,
        "interpretation": "|r| < 0.2 weak, 0.2-0.4 modest, 0.4-0.6 moderate, > 0.6 strong",
    })


_RECOVERY_SCHEMA = {
    "type": "object",
    "properties": {
        "activity_type": {"type": "string"},
        "min_distance_km": {"type": "number"},
        "min_duration_min": {"type": "integer"},
        "lookback_days": {"type": "integer", "description": "Default 365"},
    },
    "required": [],
}


@tool(
    "recovery_pattern",
    "After workouts matching the filter, how many days does body battery max and RHR typically take to return to within 95% / 103% of baseline? Returns averages and the 10 most-recent matched workouts.",
    _RECOVERY_SCHEMA,
)
async def recovery_pattern(args: dict) -> dict:
    where: list[str] = []
    params: list = []
    if args.get("activity_type"):
        where.append("activity_type LIKE ?")
        params.append(f"%{args['activity_type']}%")
    if args.get("min_distance_km"):
        where.append("distance_meters >= ?")
        params.append(float(args["min_distance_km"]) * 1000)
    if args.get("min_duration_min"):
        where.append("duration_seconds >= ?")
        params.append(int(args["min_duration_min"]) * 60)
    lookback = args.get("lookback_days") or 365
    err = _validate_days(lookback, name="lookback_days")
    if err:
        return _err(err)
    where.append("date >= ?")
    params.append((date.today() - timedelta(days=lookback)).isoformat())
    where_sql = " AND ".join(where)

    with db.connect() as conn:
        workouts = [dict(r) for r in conn.execute(
            f"SELECT activity_id, date, activity_type, distance_meters, "
            f"training_load, aerobic_te FROM activities WHERE {where_sql} ORDER BY date",
            params,
        ).fetchall()]
        results = []
        for w in workouts:
            wdate = date.fromisoformat(w["date"])
            baseline = conn.execute(
                "SELECT body_battery_max_60day_mean AS bb, rhr_60day_mean AS rhr "
                "FROM baselines WHERE date = ?",
                (w["date"],),
            ).fetchone()
            if not baseline or baseline["bb"] is None:
                continue
            bb_recovery = None
            rhr_recovery = None
            for offset in range(1, 8):
                d = (wdate + timedelta(days=offset)).isoformat()
                row = conn.execute(
                    "SELECT body_battery_max, rhr FROM daily_metrics WHERE date = ?",
                    (d,),
                ).fetchone()
                if not row:
                    continue
                if (
                    bb_recovery is None
                    and row["body_battery_max"]
                    and row["body_battery_max"] >= baseline["bb"] * 0.95
                ):
                    bb_recovery = offset
                if (
                    rhr_recovery is None
                    and baseline["rhr"]
                    and row["rhr"]
                    and row["rhr"] <= baseline["rhr"] * 1.03
                ):
                    rhr_recovery = offset
            results.append({
                **w,
                "recovery_days_to_bb_baseline": bb_recovery,
                "recovery_days_to_rhr_baseline": rhr_recovery,
            })

    bb_vals = [r["recovery_days_to_bb_baseline"] for r in results if r["recovery_days_to_bb_baseline"]]
    rhr_vals = [r["recovery_days_to_rhr_baseline"] for r in results if r["recovery_days_to_rhr_baseline"]]
    return _text({
        "n_workouts_matched": len(results),
        "avg_recovery_days_body_battery": (sum(bb_vals) / len(bb_vals)) if bb_vals else None,
        "avg_recovery_days_rhr": (sum(rhr_vals) / len(rhr_vals)) if rhr_vals else None,
        "recent_workouts": results[-10:],
    })


# Wall-clock budget for a single run_sql query. A heavier query is aborted by
# the SQLite progress handler so a recursive CTE / cartesian join can't hang the
# (single-threaded) server. Granularity: how many VM ops between deadline checks.
_RUN_SQL_TIME_BUDGET_S = 5.0
_RUN_SQL_PROGRESS_OPS = 10_000


def _run_sql_blocking(q: str) -> list[dict]:
    """Execute `q` against a READ-ONLY connection with a wall-clock deadline.

    The read-only connection is the real write gate (engine-enforced); the
    keyword denylist in run_sql is only defense-in-depth. The progress handler
    aborts once the deadline passes, which makes SQLite raise OperationalError.
    Runs in a worker thread (via asyncio.to_thread) so even a within-budget
    heavy query never blocks the event loop.
    """
    deadline = time.monotonic() + _RUN_SQL_TIME_BUDGET_S

    def _abort_if_over_budget() -> int:
        # Truthy return => SQLite interrupts the running statement.
        return 1 if time.monotonic() > deadline else 0

    with db.connect_readonly() as conn:
        conn.set_progress_handler(_abort_if_over_budget, _RUN_SQL_PROGRESS_OPS)
        try:
            return [dict(r) for r in conn.execute(q).fetchmany(500)]
        finally:
            conn.set_progress_handler(None, 0)


@tool(
    "run_sql",
    "Execute a read-only SELECT or WITH query against the fitness DB. "
    "Tables and columns: " + _render_schema() + ". "
    "Use this for ad-hoc analysis the other tools don't cover.",
    {"query": str},
)
async def run_sql(args: dict) -> dict:
    q = args["query"].strip().rstrip(";")
    lowered = q.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        return _err("read-only: only SELECT/WITH queries permitted")
    # Cheap defense-in-depth: a clean error for the common case. The real gate is
    # the read-only connection in _run_sql_blocking — any write fails there too.
    forbidden = ("insert ", "update ", "delete ", "drop ", "alter ", "create ", "attach ", "pragma ", "replace ")
    padded = f" {lowered} "
    for kw in forbidden:
        if kw in padded:
            return _err(f"forbidden keyword: {kw.strip()}")
    try:
        rows = await asyncio.to_thread(_run_sql_blocking, q)
    except sqlite3.OperationalError as e:
        # "interrupted" is the deadline abort; "readonly database" is a write
        # attempt that slipped past the denylist. Don't leak the raw string.
        if "interrupt" in str(e).lower():
            return _err("query exceeded time budget")
        return _err("query failed: operational error")
    except sqlite3.Error:
        return _err("query failed: invalid query")
    return _text({"rows": rows, "count": len(rows)})


@tool(
    "save_user_note",
    "Persist a NEW durable user preference. Call this ONLY when the user "
    "expresses a lasting preference that does NOT overlap an existing note. "
    "If a similar note already exists, ask the user first whether to "
    "replace it (then call update_user_note) or keep both (then call this). "
    "Skip transient questions, one-off corrections, and clarifications. "
    "One sentence per note.",
    {"note": str},
)
async def save_user_note(args: dict) -> dict:
    text = (args.get("note") or "").strip()
    if not text:
        return _err("note text is required")
    try:
        n = notes.append_note(text)
    except ValueError as e:
        return _err(str(e))
    return _text({"saved": True, "line": n.line, "timestamp": n.timestamp, "text": n.text})


@tool(
    "list_user_notes",
    "Read the current list of saved user-preference notes from disk. "
    "Use this when the user asks 'what notes do you have', 'show me my "
    "settings', or before deciding whether a new preference overlaps an "
    "existing note. Returns notes with their line indices so subsequent "
    "update_user_note / delete_user_note calls can target a specific one.",
    {},
)
async def list_user_notes(_args: dict) -> dict:
    items = notes.read_notes()
    return _text({
        "notes": [
            {"line": n.line, "timestamp": n.timestamp, "text": n.text}
            for n in items
        ],
        "count": len(items),
    })


@tool(
    "update_user_note",
    "Replace the note at the given line index with new text (e.g. when the "
    "user wants to refine an existing preference instead of adding a new "
    "one). The line index comes from list_user_notes or the system "
    "prompt's notes section. Always confirm with the user before "
    "overwriting — don't silently replace.",
    {"line": int, "note": str},
)
async def update_user_note(args: dict) -> dict:
    line = args.get("line")
    text = (args.get("note") or "").strip()
    if line is None or not isinstance(line, int):
        return _err("line index is required")
    if not text:
        return _err("new note text is required")
    try:
        n = notes.update_note(line, text)
    except ValueError as e:
        return _err(str(e))
    if n is None:
        return _err(f"no note at line {line}")
    return _text({"updated": True, "line": n.line, "timestamp": n.timestamp, "text": n.text})


@tool(
    "delete_user_note",
    "Remove the note at the given line index. Use when the user asks to "
    "forget or drop a saved preference. Confirm with the user first if the "
    "intent is ambiguous.",
    {"line": int},
)
async def delete_user_note(args: dict) -> dict:
    line = args.get("line")
    if line is None or not isinstance(line, int):
        return _err("line index is required")
    ok = notes.delete_note(line)
    if not ok:
        return _err(f"no note at line {line}")
    return _text({"deleted": True, "line": line})


@tool(
    "daily_snapshot",
    "The full daily snapshot — today's metrics with baseline deltas / trend "
    "arrows, current CTL/ATL/TSB, recent workouts (with mile + formatted "
    "fields), and saved user notes. The same payload the brief and coach "
    "prompt share. Pure read.",
    {},
)
async def daily_snapshot(_args: dict) -> dict:
    # Lazy import: status.py imports DAILY_NUMERIC_METRICS from this module, so
    # a top-level import here would be circular.
    from .status import assemble_status
    return _text(assemble_status())


_LOG_OBSERVATION_SCHEMA = {
    "type": "object",
    "properties": {
        "obs_type": {
            "type": "string",
            "description": "One of: " + ", ".join(sorted(OBS_TYPES)) + ". "
            "Numeric types (weight/rpe/soreness/energy/mood) use `value`; "
            "free-text types (feeling/injury/note) use `text`.",
        },
        "value": {"type": "number", "description": "Numeric reading (weight/rpe/soreness/energy/mood)."},
        "text": {"type": "string", "description": "Free text (feeling/injury/note)."},
        "date": {"type": "string", "description": "ISO observed-on date, default today."},
        "activity_id": {"type": "integer", "description": "Optional activity this observation refers to."},
    },
    "required": ["obs_type"],
}


@tool(
    "log_observation",
    "Record a subjective / manual observation (weight, RPE, soreness, energy, "
    "mood, a feeling/injury note). Numeric types store `value`; text types "
    "store `text`. Optionally tie it to an existing activity_id.",
    _LOG_OBSERVATION_SCHEMA,
)
async def log_observation(args: dict) -> dict:
    obs_type = args.get("obs_type")
    if obs_type not in OBS_TYPES:
        return _err(f"unknown obs_type '{obs_type}'", allowed=sorted(OBS_TYPES))
    # Numeric types read `value`; text types read `text`. Reject an empty
    # payload up front so we never insert a row with both columns NULL.
    if obs_type in NUMERIC_OBS_TYPES:
        if args.get("value") is None:
            return _err(f"obs_type '{obs_type}' requires a numeric value")
        value_num = args.get("value")
        value_text = None
    else:
        text = args.get("text")
        if not (text and str(text).strip()):
            return _err(f"obs_type '{obs_type}' requires text")
        value_num = None
        value_text = text
    observed_on = args.get("date") or date.today().isoformat()
    # Validate the user-supplied date BEFORE any write — mirror log_manual_workout.
    # A malformed string sorts wrong; a future date is silently excluded from the
    # days-filtered list_observations lookback.
    try:
        parsed_date = date.fromisoformat(observed_on)
    except ValueError:
        return _err(f"invalid date '{observed_on}', expected YYYY-MM-DD")
    if parsed_date > date.today():
        return _err("date cannot be in the future")
    created_at = datetime.now().isoformat()
    activity_id = args.get("activity_id")
    with db.connect() as conn:
        if activity_id is not None:
            exists = conn.execute(
                "SELECT 1 FROM activities WHERE activity_id = ?", (activity_id,)
            ).fetchone()
            if not exists:
                return _err("activity not found", activity_id=activity_id)
        cur = conn.execute(
            "INSERT INTO observations "
            "(observed_on, created_at, obs_type, value_num, value_text, activity_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (observed_on, created_at, obs_type, value_num, value_text, activity_id),
        )
        obs_id = cur.lastrowid
        row = conn.execute(
            "SELECT * FROM observations WHERE observation_id = ?", (obs_id,)
        ).fetchone()
    return _text({"logged": True, "observation": dict(row)})


_LIST_OBSERVATIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "days": {"type": "integer", "description": "Only observations from the last N days."},
        "obs_type": {"type": "string", "description": "Filter to one obs_type."},
    },
    "required": [],
}


@tool(
    "list_observations",
    "List logged observations, most recent first. Optional filters: days "
    "lookback and obs_type.",
    _LIST_OBSERVATIONS_SCHEMA,
)
async def list_observations(args: dict) -> dict:
    where: list[str] = []
    params: list = []
    if args.get("days"):
        err = _validate_days(args["days"])
        if err:
            return _err(err)
        where.append("observed_on >= ?")
        params.append((date.today() - timedelta(days=args["days"])).isoformat())
    if args.get("obs_type"):
        where.append("obs_type = ?")
        params.append(args["obs_type"])
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM observations {where_sql} "
            "ORDER BY observed_on DESC, observation_id DESC",
            params,
        ).fetchall()
    return _text({"observations": [dict(r) for r in rows], "count": len(rows)})


@tool(
    "delete_observation",
    "Delete one logged observation by its observation_id. Use when the user "
    "asks to drop a logged reading.",
    {"observation_id": int},
)
async def delete_observation(args: dict) -> dict:
    obs_id = int(args["observation_id"])
    with db.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM observations WHERE observation_id = ?", (obs_id,)
        ).fetchone()
        if not row:
            return _err(f"no observation at id {obs_id}")
        conn.execute("DELETE FROM observations WHERE observation_id = ?", (obs_id,))
    return _text({"deleted": True, "observation_id": obs_id})


_LOG_MANUAL_WORKOUT_SCHEMA = {
    "type": "object",
    "properties": {
        "activity_type": {"type": "string", "description": "e.g. 'strength', 'cycling', 'yoga'."},
        "duration_min": {"type": "number", "description": "Workout duration in minutes."},
        "date": {"type": "string", "description": "ISO date, default today. May be backdated."},
        "distance_mi": {"type": "number", "description": "Optional distance in miles."},
        "avg_hr": {"type": "integer"},
        "training_load": {"type": "number", "description": "Optional TSS-style load; feeds CTL/ATL/TSB."},
        "name": {"type": "string", "description": "Optional workout name."},
    },
    "required": ["activity_type", "duration_min"],
}


@tool(
    "log_manual_workout",
    "Record a workout Garmin didn't capture (strength, a class, an untracked "
    "run). Gets a synthetic negative activity_id and source='manual', then "
    "training load is recomputed so it shows up in CTL/ATL/TSB. May be backdated.",
    _LOG_MANUAL_WORKOUT_SCHEMA,
)
async def log_manual_workout(args: dict) -> dict:
    activity_type = args["activity_type"]
    duration_min = args["duration_min"]
    workout_date = args.get("date") or date.today().isoformat()
    # Validate the user-supplied date BEFORE any write — a malformed string must
    # not commit the activity row and then raise in the post-insert lookback.
    try:
        parsed_date = date.fromisoformat(workout_date)
    except ValueError:
        return _err(f"invalid date '{workout_date}', expected YYYY-MM-DD")
    # A non-positive duration would store garbage duration_seconds; reject it
    # before any write.
    try:
        if float(duration_min) <= 0:
            return _err("duration_min must be positive")
    except (TypeError, ValueError):
        return _err("duration_min must be positive")
    # A future-dated workout would be stored but never feed CTL/ATL (recompute
    # only walks dates <= today). Reject it before any write.
    if parsed_date > date.today():
        return _err("date cannot be in the future")
    distance_meters = (
        float(args["distance_mi"]) * units._METERS_PER_MILE
        if args.get("distance_mi") is not None else None
    )
    duration_seconds = int(round(float(duration_min) * 60))
    avg_hr = args.get("avg_hr")
    training_load = args.get("training_load")
    name = args.get("name") or f"Manual {activity_type}"

    with db.connect() as conn:
        # Serialize the id-allocation + insert so two concurrent manual logs
        # can't read the same MIN() and collide on the PK. BEGIN IMMEDIATE
        # takes a RESERVED lock up front; db.connect() commits on clean exit.
        conn.execute("BEGIN IMMEDIATE")
        # Floor the table-min at 0 BEFORE subtracting: first manual workout on
        # an all-positive table → -1, then -2, -3, ...
        row = conn.execute(
            "SELECT MIN(MIN(activity_id), 0) - 1 AS next_id FROM activities"
        ).fetchone()
        new_id = row["next_id"] if row and row["next_id"] is not None else -1
        conn.execute(
            "INSERT INTO activities "
            "(activity_id, date, activity_type, activity_name, duration_seconds, "
            "distance_meters, avg_hr, training_load, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'manual')",
            (new_id, workout_date, activity_type, name, duration_seconds,
             distance_meters, avg_hr, training_load),
        )
        inserted = conn.execute(
            "SELECT * FROM activities WHERE activity_id = ?", (new_id,)
        ).fetchone()
        result = dict(inserted)
        result.pop("raw_json", None)

    # Widen the lookback so a BACKDATED workout rewrites its OWN date's baseline
    # row (and everything forward), not just the default 90-day window forward.
    from ..ingest import baselines
    wdate = date.fromisoformat(workout_date)
    lookback = max(baselines.RECOMPUTE_LOOKBACK_DAYS, (date.today() - wdate).days + 1)
    # The activity row is ALREADY committed (db.connect() committed on block
    # exit). recompute() runs on a fresh connection; if it raises (transient
    # "database is locked", a bad stored date, ...) we must NOT propagate — a
    # bare raise reads as a tool failure and a blind retry would insert a SECOND
    # workout, double-counting load. Return a partial-success so the caller can
    # tell the row landed and skip the retry.
    try:
        baselines.recompute(lookback_days=lookback)
    except Exception as e:  # noqa: BLE001 — row is committed; never re-raise here
        return _text({
            "logged": True,
            "activity": _augment_workout(result),
            "recompute_failed": True,
            "warning": "workout saved but training-load recompute failed; "
                       "run `fitness baselines` to refresh",
            "error_detail": str(e),
        })

    return _text({
        "logged": True,
        "activity": _augment_workout(result),
        "note": f"training load recomputed (lookback_days={lookback})",
    })


@tool(
    "delete_manual_workout",
    "Delete a manually-logged workout by its (negative) activity_id. Refuses "
    "non-negative ids so Garmin data can never be deleted. Detaches any "
    "referencing observations, then recomputes training load.",
    {"activity_id": int},
)
async def delete_manual_workout(args: dict) -> dict:
    aid = int(args["activity_id"])
    if aid >= 0:
        return _err("refusing to delete non-manual activity (id >= 0)", activity_id=aid)
    with db.connect() as conn:
        # (1) Read the date FIRST — needed for the widened lookback below.
        row = conn.execute(
            "SELECT date FROM activities WHERE activity_id = ?", (aid,)
        ).fetchone()
        if not row:
            return _err(f"no manual workout at id {aid}")
        workout_date = row["date"]
        # (2) Detach referencing observations (don't orphan a dangling ref).
        conn.execute(
            "UPDATE observations SET activity_id = NULL WHERE activity_id = ?", (aid,)
        )
        # (3) Delete the activity row.
        conn.execute("DELETE FROM activities WHERE activity_id = ?", (aid,))

    # (4) Recompute with the same widened lookback covering that date.
    from ..ingest import baselines
    wdate = date.fromisoformat(workout_date)
    lookback = max(baselines.RECOMPUTE_LOOKBACK_DAYS, (date.today() - wdate).days + 1)
    # The delete is ALREADY committed. recompute() runs on a fresh connection;
    # if it raises, don't propagate a bare exception that implies the delete
    # failed — the row is gone. Return a partial-success instead.
    try:
        baselines.recompute(lookback_days=lookback)
    except Exception as e:  # noqa: BLE001 — delete is committed; never re-raise
        return _text({
            "deleted": True,
            "activity_id": aid,
            "recompute_failed": True,
            "warning": "workout deleted but training-load recompute failed; "
                       "run `fitness baselines` to refresh",
            "error_detail": str(e),
        })

    return _text({
        "deleted": True,
        "activity_id": aid,
        "note": f"training load recomputed (lookback_days={lookback})",
    })


# --- Training plans (the first agent->SQLite write path; DRAFT-ONLY) -------
#
# The model can only ever create/edit DRAFT plans. `status` is never an input:
# propose hardcodes 'draft', revise builds its update from named goal params
# only (never status), and revise refuses a non-draft target. Activating or
# deleting a plan is a human action via the REST layer — there is no tool for
# it. See plans.py for the enforced write boundary.

_PROPOSE_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "goal_type": {"type": "string", "description": "5k | 10k | half | full | custom"},
        "race_date": {"type": "string", "description": "ISO YYYY-MM-DD"},
        "target_time_seconds": {"type": "integer", "description": "Goal finish time in seconds"},
        "goal_distance_m": {"type": "number", "description": "Race distance (m); defaults from goal_type"},
        "title": {"type": "string"},
        "ability_snapshot": {"type": "object", "description": "Current-ability estimate you derived from the athlete's data"},
        "workouts": {
            "type": "array",
            "description": "Full schedule: each {date, week_index, type, target_distance_m?, target_pace_sec_per_km?, target_duration_sec?, description, seq?}",
            "items": {"type": "object"},
        },
    },
    "required": ["goal_type", "race_date", "workouts"],
}

_REVISE_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "plan_id": {"type": "integer"},
        "goal_type": {"type": "string"},
        "race_date": {"type": "string"},
        "target_time_seconds": {"type": "integer"},
        "goal_distance_m": {"type": "number"},
        "title": {"type": "string"},
        "workouts": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["plan_id"],
}

_EDITABLE_TOOL_FIELDS = ("goal_type", "race_date", "target_time_seconds", "goal_distance_m", "title")


@tool(
    "propose_training_plan",
    "Create a DRAFT training plan from a goal + a full workout schedule you "
    "generated. Ground it first: call training_load_status, get_today_status, "
    "and query_workouts to read the athlete's real fitness before proposing. "
    "Archives any prior draft. Does NOT activate the plan — the user commits it.",
    _PROPOSE_PLAN_SCHEMA,
)
async def propose_training_plan(args: dict) -> dict:
    goal_type = args.get("goal_type")
    race_date = args.get("race_date")
    workouts = args.get("workouts")
    if not goal_type or not race_date:
        return _err("goal_type and race_date are required")
    goal_distance_m = args.get("goal_distance_m") or plans.GOAL_DISTANCE_M.get(goal_type)
    target_time = args.get("target_time_seconds")
    created_floor = db.last_known_daily_date() or date.today().isoformat()
    err = plans.validate_plan_input(
        goal_type, race_date, workouts or [], created_floor, goal_distance_m, target_time
    )
    if err:
        return _err(err)
    plan_id = plans.insert_draft(
        {
            "goal_type": goal_type,
            "race_date": race_date,
            "target_time_seconds": target_time,
            "goal_distance_m": goal_distance_m,
            "title": args.get("title"),
            "ability_snapshot": args.get("ability_snapshot"),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        },
        workouts,
    )
    return _text({"plan_id": plan_id, "status": "draft"})


@tool(
    "revise_training_plan",
    "Revise the DRAFT plan during a riff: update goal fields and/or replace the "
    "workout set wholesale. Only works on a draft (refuses active/archived "
    "plans). Cannot change a plan's status — the user commits via the UI.",
    _REVISE_PLAN_SCHEMA,
)
async def revise_training_plan(args: dict) -> dict:
    plan_id = args.get("plan_id")
    if not isinstance(plan_id, int):
        return _err("plan_id (int) is required")
    # status is deliberately NOT among the readable fields — it can never be set here.
    fields = {k: args[k] for k in _EDITABLE_TOOL_FIELDS if k in args}
    workouts = args.get("workouts")

    if workouts is not None:
        current = plans.get_plan(plan_id)
        if current is None:
            return _err(f"no plan {plan_id}")
        gt = fields.get("goal_type", current["goal_type"])
        rd = fields.get("race_date", current["race_date"])
        created_floor = db.last_known_daily_date() or date.today().isoformat()
        err = plans.validate_plan_input(
            gt, rd, workouts, created_floor,
            fields.get("goal_distance_m", current.get("goal_distance_m")),
            fields.get("target_time_seconds", current.get("target_time_seconds")),
        )
        if err:
            return _err(err)
    try:
        plans.revise_draft(plan_id, fields, workouts)
    except (plans.PlanNotFoundError, plans.NotDraftError, ValueError) as e:
        return _err(str(e))
    return _text({"plan_id": plan_id, "status": "draft"})


@tool(
    "get_training_plan_status",
    "Status of the ACTIVE training plan: goal, days to race, the most recent "
    "graded day's prescription + verdict, today's prescribed session, and "
    "overall adherence. Returns {active: false} when there is no active plan. "
    "Call this first in a brief to decide whether to fold the plan in.",
    {},
)
async def get_training_plan_status(_args: dict) -> dict:
    active = plans.get_active_plan()
    if active is None:
        return _text({"active": False})
    frontier = db.last_known_daily_date()
    today = date.today().isoformat()
    dates = [w["date"] for w in active["workouts"]] or [today]
    start = min(dates)
    end = max([today, *dates] + ([frontier] if frontier else []))
    activities_by_date = plans.load_activities_by_date(start, end)
    cfg = plans.resolve_grading_config()
    return _text(plans.build_plan_status(active, frontier, activities_by_date, today, cfg))


@tool(
    "get_training_plan_progress",
    "Full day-by-day progress of the ACTIVE training plan: every prescribed "
    "workout with its graded verdict (done | partial | missed | compliant | "
    "pending), plus goal, days-to-race, adherence %, and projected finish. "
    "Returns {active: false} when there is no active plan. Prefer this over "
    "get_training_plan_status (a slim one-day summary) to answer 'show my plan "
    "through today' / 'how is my plan going' — never query the DB by hand for "
    "this.",
    {},
)
async def get_training_plan_progress(_args: dict) -> dict:
    active = plans.get_active_plan()
    if active is None:  # build_plan_detail has no None guard — guard here first.
        return _text({"active": False})
    frontier = db.last_known_daily_date()
    today = date.today().isoformat()
    # Mirror get_training_plan_status's frontier-INCLUSIVE end (parity: both plan
    # tools compute identical grading windows), not the web tab's exclusive form.
    dates = [w["date"] for w in active["workouts"]] or [today]
    start = min(dates)
    end = max([today, *dates] + ([frontier] if frontier else []))
    activities_by_date = plans.load_activities_by_date(start, end)
    cutoff = (date.today() - timedelta(days=config.riegel_lookback_days())).isoformat()
    best_effort = plans.best_recent_effort(cutoff)
    cfg = plans.resolve_grading_config()
    detail = plans.build_plan_detail(active, frontier, activities_by_date, best_effort, cfg)

    # days_to_race is produced only by build_plan_status, not build_plan_detail —
    # compute it here. Read via .get (absent key -> None, not KeyError) and parse
    # defensively (NULL/unparseable -> None), matching build_plan_status's guard.
    race = plans._parse_iso(active.get("race_date"))
    today_d = plans._parse_iso(today)
    days_to_race = (race - today_d).days if race and today_d else None

    # Deliberate projection: keep the fields an agent needs to answer a
    # plan-progress question; drop identifiers / internal rollups (plan_id,
    # status, ability_snapshot, weekly_mileage, …) that build_plan_detail spreads.
    workouts = [
        {
            "date": w.get("date"),
            "week_index": w.get("week_index"),
            "type": w.get("type"),
            "target_distance_m": w.get("target_distance_m"),
            "target_pace_sec_per_km": w.get("target_pace_sec_per_km"),
            "target_duration_sec": w.get("target_duration_sec"),
            "description": w.get("description"),
            "verdict": w.get("verdict"),
            "actual_distance_m": w.get("actual_distance_m"),
            "actual_pace_sec_per_km": w.get("actual_pace_sec_per_km"),
            "actual_activity_types": w.get("actual_activity_types"),
        }
        for w in detail["workouts"]
    ]
    return _text({
        "active": True,
        "goal_type": detail.get("goal_type"),
        "race_date": detail.get("race_date"),
        "target_time_seconds": detail.get("target_time_seconds"),
        "days_to_race": days_to_race,
        "adherence_pct": detail.get("adherence_pct"),
        "predicted_finish_seconds": detail.get("predicted_finish_seconds"),
        "workouts": workouts,
    })


@tool(
    "save_brief",
    "Persist a composed daily brief. Pass the brief as JSON matching the Brief "
    "schema (a `takeaways` list; date/user_name/generated_at are stamped "
    "server-side). The server validates against the schema and atomically "
    "writes briefings/<today>.json — invalid briefs are rejected. Use this "
    "after composing the brief via the `brief` prompt.",
    {"brief": dict},
)
async def save_brief(args: dict) -> dict:
    # Thin wrapper over briefs.save_brief (the single integrity gate). DROP the
    # returned `brief` pydantic object — only the {saved,date,path} scalars are
    # _text-wrapped (a pydantic Brief through json.dumps would raise TypeError).
    try:
        result = briefs.save_brief(args["brief"])
    except ValidationError as e:
        return _err(f"brief failed schema validation: {e}")
    return _text({"saved": True, "date": result["date"], "path": result["path"]})


ALL_TOOLS = [
    get_today_status,
    get_metric,
    get_metric_trend,
    chart,
    query_workouts,
    get_workout_detail,
    compare_periods,
    find_anomalies,
    training_load_status,
    correlate,
    recovery_pattern,
    run_sql,
    save_user_note,
    list_user_notes,
    update_user_note,
    delete_user_note,
    daily_snapshot,
    log_observation,
    list_observations,
    delete_observation,
    log_manual_workout,
    delete_manual_workout,
    propose_training_plan,
    revise_training_plan,
    get_training_plan_status,
    get_training_plan_progress,
    save_brief,
]


def make_server():
    return create_sdk_mcp_server(name=SERVER_NAME, version="0.5.0", tools=ALL_TOOLS)


def allowed_tool_names() -> list[str]:
    return [f"mcp__{SERVER_NAME}__{t.name}" for t in ALL_TOOLS]


# Explicit ALLOW-LIST of the read-only analysis tools (not a denylist): the
# brief loop runs with exactly this set, so its behavior stays unchanged as new
# tools land. A future tool is excluded by default unless deliberately added
# here. Excludes the note-write tools, all observation/manual-workout write
# tools, and (deliberately) daily_snapshot + list_observations so the brief's
# tool set is identical to before this issue.
_READ_ONLY_TOOL_NAMES = (
    "get_today_status",
    "get_metric",
    "get_metric_trend",
    "query_workouts",
    "get_workout_detail",
    "compare_periods",
    "find_anomalies",
    "training_load_status",
    "correlate",
    "recovery_pattern",
    "run_sql",
)


def read_only_tool_names() -> list[str]:
    return [f"mcp__{SERVER_NAME}__{name}" for name in _READ_ONLY_TOOL_NAMES]
