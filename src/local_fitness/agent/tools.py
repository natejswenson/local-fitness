"""Claude Agent SDK tools that query the fitness DB.

All tools return text content (JSON-encoded payloads) so the model can reason
over them. Optional-arg tools use full JSON Schema; required-only tools use
the {name: type} shorthand. SQL strings are constructed with whitelisted
column names — no user input ever interpolates into SQL except via params.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from .. import db, notes
from . import units


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
TEXT_OBS_TYPES = OBS_TYPES - NUMERIC_OBS_TYPES

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
    if not isinstance(payload, str):
        payload = json.dumps(payload, indent=2, default=str)
    return {"content": [{"type": "text", "text": payload}]}


def _err(msg: str, **extra) -> dict:
    return {"content": [{"type": "text", "text": json.dumps({"error": msg, **extra})}], "is_error": True}


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
    days = int(args["days"])
    if metric not in DAILY_NUMERIC_METRICS:
        return _err(f"unknown metric '{metric}'", allowed=sorted(DAILY_NUMERIC_METRICS))
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
    days = int(args["days"])
    if metric not in DAILY_NUMERIC_METRICS:
        return _err(f"unknown metric '{metric}'", allowed=sorted(DAILY_NUMERIC_METRICS))
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
        where.append("date >= ?")
        params.append((date.today() - timedelta(days=int(args["days"]))).isoformat())
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
    days = int(args.get("lookback_days") or 90)
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
    days = int(args["days"])
    lag = int(args.get("lag_days") or 0)
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
    lookback = int(args.get("lookback_days") or 365)
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
    forbidden = ("insert ", "update ", "delete ", "drop ", "alter ", "create ", "attach ", "pragma ", "replace ")
    padded = f" {lowered} "
    for kw in forbidden:
        if kw in padded:
            return _err(f"forbidden keyword: {kw.strip()}")
    with db.connect() as conn:
        try:
            rows = [dict(r) for r in conn.execute(q).fetchmany(500)]
        except Exception as e:
            return _err(str(e))
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
        where.append("observed_on >= ?")
        params.append((date.today() - timedelta(days=int(args["days"]))).isoformat())
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
        date.fromisoformat(workout_date)
    except ValueError:
        return _err(f"invalid date '{workout_date}', expected YYYY-MM-DD")
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
    baselines.recompute(lookback_days=lookback)

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
    baselines.recompute(lookback_days=lookback)

    return _text({
        "deleted": True,
        "activity_id": aid,
        "note": f"training load recomputed (lookback_days={lookback})",
    })


ALL_TOOLS = [
    get_today_status,
    get_metric,
    get_metric_trend,
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
]


def make_server():
    return create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=ALL_TOOLS)


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
