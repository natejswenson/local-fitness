"""Claude Agent SDK tools that query the fitness DB.

All tools return text content (JSON-encoded payloads) so the model can reason
over them. Optional-arg tools use full JSON Schema; required-only tools use
the {name: type} shorthand. SQL strings are constructed with whitelisted
column names — no user input ever interpolates into SQL except via params.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from .. import db, notes


SERVER_NAME = "fitness"

BASELINE_METRICS = {"rhr", "sleep_seconds"}
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
    return _text([dict(r) for r in rows])


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
    activity = dict(act)
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
    "Execute a read-only SELECT or WITH query against the fitness DB. Tables: daily_metrics, activities, activity_splits, activity_hr_zones, body_battery_samples, stress_samples, baselines. Use this for ad-hoc analysis the other tools don't cover.",
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
]


def make_server():
    return create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=ALL_TOOLS)


def allowed_tool_names() -> list[str]:
    return [f"mcp__{SERVER_NAME}__{t.name}" for t in ALL_TOOLS]
