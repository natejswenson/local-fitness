"""Claude Agent SDK tools that query the fitness DB.

All tools return text content (JSON-encoded payloads) so the model can reason
over them. Optional-arg tools use full JSON Schema; required-only tools use
the {name: type} shorthand. SQL strings are constructed with whitelisted
column names — no user input ever interpolates into SQL except via params.
"""
from __future__ import annotations

import asyncio
import atexit
import hashlib
import importlib.metadata
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
from dataclasses import replace as replace_dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from claude_agent_sdk import create_sdk_mcp_server
from claude_agent_sdk import tool as _sdk_tool
from pydantic import ValidationError

from .. import config, db, notes, plans
from ..ingest import baselines as baselines_mod
from . import (
    briefs,
    card_store,
    charts,
    coach,
    interpret,
    journal,
    memory,
    personality,
    plan_coach,
    reflect,
    report_card,
    units,
    workout_coach,
    workout_rows,
)
from .schemas import Brief

LOG = logging.getLogger(__name__)


SERVER_NAME = "fitness"

# Strong references to in-flight auto-reflect tasks (workout_report_card's
# fire-and-forget hook). asyncio holds tasks weakly; without this set a
# scheduled reflection could be garbage-collected mid-flight.
_REFLECT_TASKS: set[asyncio.Task] = set()

BASELINE_METRICS = {"rhr", "sleep_seconds"}

# Caps a chat-triggered sync's lookback so a long absence doesn't turn one
# tool call into a multi-minute Garmin backfill.
SYNC_MAX_DAYS = 30

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


DAILY_NUMERIC_METRICS = {
    "sleep_seconds", "sleep_score", "sleep_deep_seconds", "sleep_rem_seconds",
    "sleep_light_seconds", "sleep_awake_seconds",
    "rhr", "avg_stress", "max_stress",
    "body_battery_min", "body_battery_max",
    "body_battery_charged", "body_battery_drained",
    "steps", "active_calories", "vo2_max",
    "intensity_minutes_moderate", "intensity_minutes_vigorous",
}


# Fix 10 (2026-07-27): fields whose raw float64 carries real sub-hundredth
# precision that's borderline lossy at the default 2dp — pace (seconds/km,
# summed over a whole run so 2dp's ~0.01 sec/km could compound visibly across
# a long distance) and training-effect scores (already a 1dp Garmin scale, but
# derived arithmetic on it — e.g. te_collapsing's `< 1.0` comparisons — reads
# the raw value, not the rounded serialized one, so 4dp here just gives a
# generous margin at negligible size cost). Every other float gets 2dp — the
# measured win (10.2%/10.1%/6.8%/3.4% smaller payloads across query_workouts/
# daily_snapshot/plan progress) comes from the ~15-digit tail float64 division
# leaves behind (e.g. "avg_pace_sec_per_km": 333.2222672948015), and 2dp
# already strips all of that for values callers only ever read to whole or
# tenths precision.
_TEXT_HIGH_PRECISION_KEYS = frozenset({
    "avg_pace_sec_per_km", "target_pace_sec_per_km", "actual_pace_sec_per_km",
    "aerobic_te", "anaerobic_te",
})
_TEXT_DEFAULT_DP = 2
_TEXT_HIGH_DP = 4


def _round_floats(obj: Any, key: str | None = None) -> Any:
    """Recursively round float64 noise before JSON serialization.

    The ONE choke point every tool payload flows through (``_text``/``_err``),
    so every one of the 55+ tools gets this for free rather than each caller
    rounding its own fields by hand (which is how the noise shipped in the
    first place — most call sites DO round, a few don't, and json.dumps
    serializes whatever float64 division left behind: 15+ digits for a value
    nobody reads past the first 1-2).

    ``bool`` is checked before ``float``/``int`` because ``bool`` is an
    ``int`` subclass in Python (``isinstance(True, int) is True``) — without
    the explicit check a boolean payload field would round-trip as an int.
    ``key`` threads the enclosing dict key down so ``_TEXT_HIGH_PRECISION_KEYS``
    (pace, aerobic/anaerobic TE) get 4dp instead of the default 2; every other
    float, at any nesting depth, gets 2dp. Ints/strs/None pass through
    untouched — this never corrupts a non-float type.
    """
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        dp = _TEXT_HIGH_DP if key in _TEXT_HIGH_PRECISION_KEYS else _TEXT_DEFAULT_DP
        return round(obj, dp)
    if isinstance(obj, dict):
        return {k: _round_floats(v, k) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round_floats(v, key) for v in obj]
    return obj


def _text(payload: Any) -> dict:
    # Compact JSON (no indent) — fewer whitespace tokens across the multi-turn
    # agent loop; the model parses either format.
    if not isinstance(payload, str):
        payload = json.dumps(_round_floats(payload), default=str)
    return {"content": [{"type": "text", "text": payload}]}


def _err(msg: str, **extra) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(_round_floats({"error": msg, **extra}))}],
        "is_error": True,
    }


#: What a caller can actually DO about a corrupt database. Observed live
#: (2026-08-10): two tools returned the bare sqlite3 string "database disk
#: image is malformed" — no envelope, no tool name, no next step — and the
#: agent had nothing to act on. The remediation names concrete actions and
#: tells the model NOT to retry: a corrupt file does not heal between calls.
_DB_ERROR_REMEDIATION = (
    "the SQLite file itself is damaged or unreadable — do not retry this "
    "call unchanged. Check it with `sqlite3 data/fitness.db 'PRAGMA "
    "integrity_check'`; if it reports corruption, restore data/fitness.db "
    "from a backup or rebuild it (`fitness setup` then `fitness backfill "
    "<export.zip>` / `fitness pull`) — Garmin is the source of truth, so "
    "nothing but manual notes/plans is lost with the file."
)


def tool(name: str, description: str, input_schema: Any):
    """The SDK ``tool`` decorator plus a last-resort error envelope.

    Every handler is wrapped in one try/except so an unhandled
    ``sqlite3.DatabaseError`` (a corrupt DB file, a malformed page read —
    failures no per-tool code anticipates) surfaces as the same enveloped
    ``_err`` shape every anticipated failure already uses, with a
    ``remediation`` the model can act on, instead of a bare exception string
    with ``is_error`` unset. One wrapper, zero per-tool diff — a future tool
    inherits the guard by using this decorator, which it must, since this
    module shadows the SDK import.
    """
    sdk_decorator = _sdk_tool(name, description, input_schema)

    def decorate(fn):
        async def guarded(args: dict) -> dict:
            try:
                return await fn(args)
            except sqlite3.DatabaseError as e:
                LOG.warning("tool %s: database error", name, exc_info=True)
                return _err(f"database error: {e}", remediation=_DB_ERROR_REMEDIATION)

        guarded.__name__ = getattr(fn, "__name__", name)
        guarded.__doc__ = fn.__doc__
        return sdk_decorator(guarded)

    return decorate


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


def _validation_error_summary(e: ValidationError) -> str:
    """A pydantic ValidationError as compact ``loc: msg`` pairs.

    The full repr is multi-line, carries a docs URL, and buries the two
    fields that actually failed — the model needs ``takeaways.0.tone:
    Input should be ...``, nothing else."""
    try:
        pairs = [
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
            for err in e.errors()
        ]
        return "; ".join(pairs) or str(e)
    except Exception:
        return str(e)


def _validate_date(value: Any, name: str = "date") -> str | None:
    """Validate a user-supplied ISO date; error string or ``None`` when valid.

    ``date.fromisoformat``, not a shape regex: the retired ``_DATE_RE``
    accepted ``2026-13-45`` and ``2026-02-30`` — impossible dates that then
    hit SQL as strings and matched nothing, indistinguishable from a real
    empty window. One helper, one message, replacing the two idioms
    (regex + "malformed date" vs try/except + "invalid date") that had grown
    side by side in this module. Mirrors ``_validate_days``' return contract.
    """
    if not isinstance(value, str):
        return f"{name} must be a YYYY-MM-DD date string"
    try:
        date.fromisoformat(value)
    except ValueError:
        return f"{name} must be a valid YYYY-MM-DD date (got {value!r})"
    # fromisoformat also accepts e.g. "2026-07-26T10:00" on newer Pythons and
    # compact forms like "20260726"; hold the tool surface to the one shape
    # every date column stores.
    if len(value) != 10:
        return f"{name} must be a valid YYYY-MM-DD date (got {value!r})"
    return None


# One definition for both shapes lives in workout_rows.py (pure; shared with
# status.py, whose inline copy of this function is retired). The DETAIL shape
# (_augment_workout: convenience fields alongside raw columns) stays on
# get_workout_detail / correlate / the manual-workout and plan-write echoes;
# the LIST shape (_display_workout: display fields only, None-optionals
# omitted) is for query_workouts and other multi-row surfaces — raw/formatted
# pairs measured as ~25% of every list payload (2026-08-10 audit).
_augment_workout = workout_rows.augment_workout
_display_workout = workout_rows.display_workout


# 0.48.0: `get_today_status` is GONE. It and `daily_snapshot` had byte-identical
# bodies and shared this very description constant — two names for one tool, so
# the model coin-flipped between them (16/5 across recorded sessions). The
# 2026-07-10 design that converged them said so itself: "Two tools for one job
# is exactly the ambiguity that causes an agent to pick the weaker one." The
# compat window is closed; `daily_snapshot` is the one name.
_DAILY_SNAPSHOT_DESCRIPTION = (
    "The full daily snapshot — today's metrics with baseline deltas / trend "
    "arrows, current CTL/ATL/TSB, recent workouts (with mile + formatted "
    "fields, plus a measured `effort` \"run\"/\"walk\"/null since Garmin's own "
    "activity_type label can misreport a walk as a run), and saved user "
    "notes. The same payload the brief and coach prompt share. Pure read. "
    "Settling metrics (rhr, sleep_*, body battery max/min — Garmin revises "
    "them through the day) anchor to yesterday with "
    "`provisional_today_excluded` when no sync covering today is fresh "
    "(~10 min); call sync_garmin_data first for a settled same-day read. "
    "No plan/anomalies/candidates in this payload — "
    "use get_brief_context for the full read or anything plan-/trend-related."
)


#: Cap on the raw series get_metric_trend attaches with include_values=true
#: (0.57.0, the former `get_metric` — its unbounded dump measured 63 KB at
#: days=3650). Most-recent rows win; values_truncated flags the cut.
_TREND_MAX_VALUES = 120


@tool(
    "get_metric_trend",
    # 0.57.0: `get_metric` folded in as include_values=true — identical
    # {metric, days} schema, same anchor logic, and its unbounded raw dump
    # (63 KB at days=3650, measured) is now capped at _TREND_MAX_VALUES rows.
    "Trend stats (mean, slope, current vs baseline) for a metric over N "
    "days. Pass include_values=true to also get the raw {date, value} series "
    "(most-recent 120 rows max, values_truncated flags the cut; *_seconds "
    "rows carry a value_formatted like '7h 33m'). For same-day running-tally "
    "metrics (steps, avg_stress, max_stress, active_calories, intensity "
    "minutes, body_battery_charged/drained) the window anchors on YESTERDAY "
    "— today's tally is partial all day and a trend/mean computed against it "
    "is misleading; `current` is therefore yesterday's value for those "
    "metrics, and `partial_today_excluded: true` is attached. For SETTLING "
    "metrics (rhr, sleep_*, body_battery_max/min — values Garmin revises "
    "through the day), today's row counts only when a sync covering today "
    "completed within ~10 min: fresh attaches `current_provisional` + "
    "`data_as_of`; stale excludes today from every stat (raw reading kept in "
    "`provisional_today_value`) — call sync_garmin_data first for a settled "
    "same-day read.",
    {
        "type": "object",
        "properties": {
            "metric": {"type": "string"},
            "days": {"type": "integer"},
            "include_values": {
                "type": "boolean",
                "description": "Attach the raw daily series (capped). Default false.",
            },
        },
        "required": ["metric", "days"],
    },
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
    today = date.today()
    anchor = _partial_day_anchor(metric, today)
    cutoff = (anchor - timedelta(days=days)).isoformat()
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT date, {metric} AS v FROM daily_metrics "
            f"WHERE date >= ? AND date <= ? AND {metric} IS NOT NULL ORDER BY date",
            (cutoff, anchor.isoformat()),
        ).fetchall()
        baseline = None
        if metric in BASELINE_METRICS:
            baseline = conn.execute(
                f"SELECT {metric}_60day_mean AS m, {metric}_60day_sd AS sd "
                f"FROM baselines WHERE {metric}_60day_mean IS NOT NULL "
                f"ORDER BY date DESC LIMIT 1"
            ).fetchone()
        # 0.59.0: today's row of a SETTLING metric is a snapshot Garmin may
        # have revised since the last pull. Staleness is resolved on this same
        # connection (no extra open) while it's still in scope.
        provisional = None
        if metric in SETTLING_METRICS and rows and rows[-1]["date"] == today.isoformat():
            provisional = settling_staleness(conn, today.isoformat(), datetime.now())
    provisional_value = None
    if provisional is not None and provisional["stale"]:
        # Stale ⇒ today's number may not drive ANY judgment — current, mean,
        # slope, or the vs_baseline verdict (this served a mid-sleep rhr 54,
        # later revised to 50, as "elevated +1.93 SD"). It drops out exactly
        # like a PARTIAL_DAY anchor, but stays visible as a labeled raw
        # reading so "what does it say right now" still has an answer.
        provisional_value = rows[-1]["v"]
        rows = rows[:-1]
    if not rows:
        if provisional_value is not None:
            return _err(
                "no settled data in window — today's value is a provisional "
                "snapshot; call sync_garmin_data, then retry",
                metric=metric, days=days,
                provisional_today_value=provisional_value,
            )
        return _err("no data in window", metric=metric, days=days)
    values = [r["v"] for r in rows]
    n = len(values)
    mean = sum(values) / n

    # n < 2 -> no defined slope: get_metric_trend owns this None mapping (the
    # least-squares denominator below is guarded with `or 1e-9`, so without
    # this the tool would compute a slope of 0.0 even for a single sample,
    # making "no data" unreachable). The sample SD used for the flat band is
    # likewise undefined at n=1, so the threshold computation is skipped too.
    slope: float | None
    flat_threshold = 0.0
    if n < 2:
        slope = None
    else:
        xs = list(range(n))
        x_mean = (n - 1) / 2
        denom = sum((x - x_mean) ** 2 for x in xs) or 1e-9
        slope = sum((xs[i] - x_mean) * (values[i] - mean) for i in range(n)) / denom
        # Flat band: the fitted total change across the window (slope is
        # per-observation, not per-day — xs is the sample index) stays within
        # half a sample SD. flat_threshold = (0.5 * sample_sd) / (n - 1) so
        # the in-function abs(slope) <= flat_threshold comparison equals that.
        sample_sd = (sum((v - mean) ** 2 for v in values) / max(n - 1, 1)) ** 0.5
        flat_threshold = (interpret.TREND_FLAT_SD_MULTIPLIER * sample_sd) / max(n - 1, 1)

    current_vs_baseline_sd = None
    payload = {
        "metric": metric,
        "days_window": days,
        "n_samples": n,
        "mean": mean,
        "current": values[-1],
        "slope_per_day": slope,
        "slope_direction": interpret.trend_direction(slope, flat_threshold=flat_threshold),
    }
    if anchor != today:
        payload["partial_today_excluded"] = True
    if args.get("include_values"):
        # The former get_metric's raw series, capped: most-recent
        # _TREND_MAX_VALUES rows, oldest-first within the cap, with the
        # duration-shaped value_formatted companion the coach voice speaks.
        fmt = units.format_hm if metric.endswith("_seconds") else None
        out = []
        for r in rows[-_TREND_MAX_VALUES:]:
            row = {"date": r["date"], "value": r["v"]}
            if fmt is not None:
                formatted = fmt(row["value"])
                if formatted is not None:
                    row["value_formatted"] = formatted
            out.append(row)
        payload["values"] = out
        if n > _TREND_MAX_VALUES:
            payload["values_truncated"] = True
    if baseline and baseline["m"] is not None:
        payload["baseline_60day_mean"] = baseline["m"]
        payload["baseline_60day_sd"] = baseline["sd"]
        if baseline["sd"]:
            current_vs_baseline_sd = (values[-1] - baseline["m"]) / baseline["sd"]
            payload["current_vs_baseline_sd"] = current_vs_baseline_sd
    # vs_baseline is ALWAYS attached — "no data" whenever current_vs_baseline_sd
    # is absent/None (every metric outside rhr/sleep_seconds, or a zero SD).
    payload["vs_baseline"] = interpret.baseline_position(current_vs_baseline_sd)
    if provisional is not None:
        if provisional["data_as_of"]:
            payload["data_as_of"] = provisional["data_as_of"]
        if provisional["stale"]:
            payload["provisional_today_excluded"] = True
            payload["provisional_today_value"] = provisional_value
            payload["note"] = (
                f"today's {metric} is a snapshot from a pull old enough that "
                "Garmin may have revised it since (rhr/sleep settle through "
                "the morning) — it is excluded from current/mean/slope/"
                "vs_baseline; call sync_garmin_data for the settled reading"
            )
        else:
            # Fresh (a pull covering today completed within the sync window):
            # the value is as current as it can get, but Garmin can still
            # revise it until the day settles — included in the stats, labeled.
            payload["current_provisional"] = True

    # Round at the payload boundary; None passes through unrounded.
    for field, ndigits in (
        ("mean", 2), ("slope_per_day", 3),
        ("baseline_60day_mean", 2), ("baseline_60day_sd", 2),
        ("current_vs_baseline_sd", 2),
    ):
        if payload.get(field) is not None:
            payload[field] = round(payload[field], ndigits)
    return _text(payload)


# Metrics the chart tool can plot: every daily numeric column, the three
# training-load series from `baselines` (fitness / fatigue / freshness), and one
# derived series — Garmin's weekly-badge "active minutes" (moderate + 2×vigorous).
# Used as a frozen whitelist before any column name reaches an f-string, same as
# get_metric_trend. Derived/baseline names are mapped to safe SQL below, never f-strung
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

# Fix 8 (2026-07-27): metrics that are a same-day RUNNING TALLY — Garmin
# computes them incrementally through the day, so "today"'s reading is
# necessarily partial no matter what time it's read, not just before some
# cutoff we could detect from date-only data. Reuses _CHART_CUMULATIVE_METRICS
# (same underlying fact: these accumulate over the day) plus avg_stress/
# max_stress, which that set omits only because they don't get SUMMED in the
# chart tool's weekly calendar column — they're still a running computation
# over the day's samples-so-far. Measured live: a 06:30 brief read avg_stress
# off 50 overnight samples (00:00-02:27) as "17", narrated 3x as recovery
# evidence against a 32 baseline, when every complete day that week ran
# 24-32. body_battery_max/min are deliberately EXCLUDED — Fix 1 (0.39.0)
# already made those a per-day MIN/MAX rollup over the full day's samples;
# that fix's scope stops there and this one doesn't reopen it.
PARTIAL_DAY_METRICS = _CHART_CUMULATIVE_METRICS | frozenset({"avg_stress", "max_stress"})


def _partial_day_anchor(metric: str, today: date) -> date:
    """The latest date safe to treat as a COMPLETE reading for `metric`.

    For PARTIAL_DAY_METRICS this is always yesterday, full stop — mirroring
    ledger.py's as-of-yesterday step-streak discipline (today's tally is
    partial ALL DAY, not just before some time-of-day cutoff nothing here can
    observe). Every other metric anchors on `today`, unchanged.
    """
    return today - timedelta(days=1) if metric in PARTIAL_DAY_METRICS else today


# 0.59.0: metrics Garmin REVISES during the day rather than accumulates.
# PARTIAL_DAY_METRICS are running tallies — partial ALL day by construction.
# These are different: today's stored value is a point-in-time snapshot that
# Garmin itself rewrites as the day is processed — rhr and the sleep_* fields
# settle once the full night is scored (measured live 2026-08-10: a 06:30
# mid-sleep pull stored rhr 54; the 10:09 post-wake pull revised it to 50 —
# and the stale 54 was served as "elevated, +1.93 SD"), and body battery's
# min/max keep moving until the day ends. The contract these power: a SETTLED
# VERDICT (SD position, anomaly, slope) must never be derived from an
# UNSETTLED number. vo2_max is deliberately out — it only moves after an
# activity sync and drifts slowly, so its staleness is harmless.
SETTLING_METRICS = frozenset({
    "rhr", "sleep_seconds", "sleep_score", "sleep_deep_seconds",
    "sleep_light_seconds", "sleep_rem_seconds", "sleep_awake_seconds",
    "body_battery_max", "body_battery_min",
})


def data_as_of_today(conn, today_iso: str) -> str | None:
    """``completed_at`` of the newest successful ingest run whose pull REACHED
    ``today`` — the honest freshness stamp for today's daily_metrics row.

    Coverage-filtered on ``last_date_fetched``: a ZIP backfill or historical
    pull that completed seconds ago never touched today's row, and counting it
    would stamp a stale snapshot "fresh" — the exact direction of lie this
    field exists to prevent. Fail-open ``None`` on any DB problem (fresh
    clone, no runs yet). Takes the caller's connection — daily_snapshot is on
    the perf gate's ``db.connect()`` open-count, so this must never open one.
    """
    placeholders = ",".join("?" * len(_SYNC_FAILURE_STATUSES))
    try:
        row = conn.execute(
            "SELECT completed_at FROM ingest_runs "
            f"WHERE completed_at IS NOT NULL AND status NOT IN ({placeholders}) "
            "AND status != 'in_progress' AND last_date_fetched >= ? "
            "ORDER BY completed_at DESC LIMIT 1",
            (*tuple(_SYNC_FAILURE_STATUSES), today_iso),
        ).fetchone()
    except sqlite3.Error:
        return None
    return row["completed_at"] if row and row["completed_at"] else None


def settling_staleness(conn, today_iso: str, now: datetime) -> dict:
    """``{"data_as_of": iso|None, "stale": bool}`` for today's settling rows.

    Stale = no successful pull covering today completed inside the sync
    freshness window (``_sync_min_interval_minutes``, default 10) — the same
    bar ``sync_garmin_data``'s short-circuit uses, so "not stale" and "a sync
    right now would short-circuit as fresh" are the same fact, and the
    deterministic remedy for stale is always one ``sync_garmin_data`` call.
    A future-dated ``completed_at`` (clock skew) reads as stale, mirroring
    ``_recent_successful_sync``.
    """
    as_of = data_as_of_today(conn, today_iso)
    if as_of is None:
        return {"data_as_of": None, "stale": True}
    try:
        completed = datetime.fromisoformat(as_of)
    except ValueError:
        return {"data_as_of": as_of, "stale": True}
    fresh = timedelta() <= now - completed <= timedelta(
        minutes=_sync_min_interval_minutes())
    return {"data_as_of": as_of, "stale": not fresh}


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
                "calendar = week-stacked emoji heat-grid (ascii default; "
                "compact and fully visible for any window); line = value "
                "curve; bar = per-day bars, weekly-bucketed past ~3 weeks; "
                "combo = bars + trend line (handles negatives like TSB); "
                "spark = one-line sparkline (ascii only). png supports "
                "line/bar/combo, default line."
            ),
        },
        "format": {
            "type": "string",
            "enum": ["ascii", "png"],
            "description": (
                "ascii (default) = terminal chart to reproduce in the reply; "
                "png = rendered matplotlib image returned inline."
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


def _fetch_metric_series(
    metric: str, days: int, end: str | None = None
) -> tuple[list[str], list[float]]:
    """Shared whitelisted fetch for chart()'s ascii and png branches — dates
    for `metric` over the `days` days ending on `end` (default today).

    Validates `metric` against `_CHART_METRICS` before building any SQL —
    the check lives inside this helper, not left to each caller to remember;
    every chart surface inherits it from here. Raises
    ValueError on an unwhitelisted metric so callers translate it into their
    own `_err()` response.

    `end` exists because the window used to be open-ended: it was anchored to
    ``date.today()`` with no upper bound, so re-rendering a PAST brief drew
    charts running to today and could show data the brief's prose never saw.
    ``generate_brief_report`` passes the brief's own date, matching the rule
    ``_build_plan_section`` already follows. The live caller (``chart``,
    both formats) keeps today's behavior via the default.
    """
    if metric not in _CHART_METRICS:
        raise ValueError(f"unknown metric '{metric}'")
    end_date = date.fromisoformat(end) if end else date.today()
    cutoff = (end_date - timedelta(days=days)).isoformat()
    end_iso = end_date.isoformat()

    # metric is whitelisted above; the column name interpolated here can only be
    # a frozen-set member, never raw user input — same contract as get_metric_trend.
    if metric in _CHART_BASELINE_METRICS:
        sql = (f"SELECT date, {metric} AS v FROM baselines "
               f"WHERE date >= ? AND date <= ? AND {metric} IS NOT NULL ORDER BY date")
    elif metric == "intensity_minutes_weighted":
        sql = ("SELECT date, (intensity_minutes_moderate + 2 * intensity_minutes_vigorous) AS v "
               "FROM daily_metrics WHERE date >= ? AND date <= ? "
               "AND intensity_minutes_moderate IS NOT NULL "
               "AND intensity_minutes_vigorous IS NOT NULL ORDER BY date")
    else:
        sql = (f"SELECT date, {metric} AS v FROM daily_metrics "
               f"WHERE date >= ? AND date <= ? AND {metric} IS NOT NULL ORDER BY date")

    with db.connect() as conn:
        rows = conn.execute(sql, (cutoff, end_iso)).fetchall()
    dates = [r["date"] for r in rows]       # ISO YYYY-MM-DD
    values = [float(r["v"]) for r in rows]
    return dates, values


# Past this many points, one-row/one-column-per-day styles (bar, combo) stop
# fitting a terminal — bucket to Monday-anchored weeks instead of degrading
# (round-2 facet review: a "90-day bar graph" ask couldn't be honored).
_LONG_WINDOW_BAR_DAYS = 21


def _bucket_weekly(
    dates: list[str], values: list[float], cumulative: bool
) -> tuple[list[str], list[float]]:
    """Aggregate a daily series into Monday-anchored ISO weeks — SUM for
    cumulative metrics (steps, intensity minutes), mean of present days
    otherwise. Returns (week_start_iso_dates, aggregated_values), weeks in
    chronological order. Same cumulative rule the calendar renderer uses."""
    buckets: dict[str, list[float]] = {}
    for d, v in zip(dates, values, strict=True):
        day = date.fromisoformat(d)
        week_start = (day - timedelta(days=day.weekday())).isoformat()
        buckets.setdefault(week_start, []).append(v)
    weeks = sorted(buckets)
    agg = [
        sum(buckets[w]) if cumulative else sum(buckets[w]) / len(buckets[w])
        for w in weeks
    ]
    return weeks, agg


@tool(
    "chart",
    # One tool, two formats (0.57.0) — `generate_chart` folded in as
    # format="png". They shared _fetch_metric_series, _CHART_METRICS and
    # overlapping style enums: two names for one job, the exact ambiguity
    # the get_today_status removal (0.48.0) documented.
    "Chart a metric over the last N days. format 'ascii' (default): "
    "terminal chart (styles: calendar heat-grid [default], line, bar, "
    "combo, spark) — reproduce the full output in a fenced code block in "
    "your reply, never leave it only in the collapsed tool call. format "
    "'png': polished matplotlib image returned inline as an image content "
    "block plus its saved file path (styles: line [default], bar, combo). "
    "For scheduled-vs-actual plan views use plan_chart instead.",
    _CHART_SCHEMA,
)
async def chart(args: dict) -> dict:
    metric = args["metric"]
    if metric not in _CHART_METRICS:
        return _err(f"unknown metric '{metric}'", allowed=sorted(_CHART_METRICS))
    err = _validate_days(args["days"])
    if err:
        return _err(err)
    fmt_arg = args.get("format") or "ascii"
    if fmt_arg not in ("ascii", "png"):
        return _err(f"unknown format '{fmt_arg}'", allowed=["ascii", "png"])
    if fmt_arg == "png":
        return await _chart_png(args)
    style = args.get("style") or "calendar"
    if style not in _CHART_STYLES:
        return _err(f"unknown style '{style}'", allowed=sorted(_CHART_STYLES))
    days = args["days"]
    dates, values = _fetch_metric_series(metric, days)
    if not values:
        return _err("no data in window", metric=metric, days=days)

    labels = [d[5:] for d in dates]         # MM-DD
    fmt = _chart_value_fmt(metric)
    title = f"{metric} · last {days}d · n={len(values)}"

    # bar/combo scale one row/column per point — past ~3 weeks, bucket to
    # weekly aggregates so a long-window "bar graph" ask is honored instead
    # of degrading into a wall of rows (or a wrapping 90-column canvas).
    if style in ("bar", "combo") and len(values) > _LONG_WINDOW_BAR_DAYS:
        cumulative = metric in _CHART_CUMULATIVE_METRICS
        week_dates, values = _bucket_weekly(dates, values, cumulative)
        labels = [d[5:] for d in week_dates]
        title += " · weekly sum" if cumulative else " · weekly avg"

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


_PLAN_CHART_SCHEMA = {
    "type": "object",
    "properties": {
        "days": {
            "type": "integer",
            "description": "Trailing window in days, ending at the data frontier (default 14)",
        },
        "weekly": {
            "type": "boolean",
            "description": "Force weekly buckets; default auto (daily rows ≤21 days, weekly above)",
        },
    },
    "required": [],
}


def _plan_chart_rows(graded: list[dict]) -> list[dict]:
    """Daily renderer rows from graded plan workouts. Actual mileage is
    suppressed for pending/compliant days — the same verdict-conditional
    rule ``weekly_rollup`` applies, so the chart and the PDF table agree."""
    rows: list[dict] = []
    for w in graded:
        verdict = w.get("verdict")
        is_rest = w["type"] == "rest" or verdict == "compliant"
        target_m = w.get("target_distance_m")
        actual_m = w.get("actual_distance_m")
        actual = (
            units.to_miles(actual_m)
            if actual_m and verdict not in ("pending", "compliant") else None
        )
        rows.append({
            "label": f"{w['date'][5:]} {w['type']}",
            "verdict": "rest" if is_rest else verdict,
            "planned": units.to_miles(target_m) if target_m else None,
            "actual": actual,
            "rest": is_rest,
        })
    return rows


def _plan_chart_weekly_rows(graded: list[dict]) -> list[dict]:
    """Monday-anchored weekly renderer rows: planned/actual mileage totals
    per week, verdict colored by completion ratio (≥90% done, 70–89%
    partial, <70% missed; no planned mileage → rest row)."""
    buckets: dict[str, dict[str, float]] = {}
    for w in graded:
        day = date.fromisoformat(w["date"])
        week_start = (day - timedelta(days=day.weekday())).isoformat()
        b = buckets.setdefault(week_start, {"planned": 0.0, "actual": 0.0})
        target_m = w.get("target_distance_m")
        actual_m = w.get("actual_distance_m")
        if target_m and w.get("verdict") != "pending":
            b["planned"] += units.to_miles(target_m) or 0.0
        if actual_m and w.get("verdict") not in ("pending", "compliant"):
            b["actual"] += units.to_miles(actual_m) or 0.0
    rows: list[dict] = []
    for week_start in sorted(buckets):
        b = buckets[week_start]
        planned, actual = round(b["planned"], 1), round(b["actual"], 1)
        if planned == 0 and actual == 0:
            verdict, rest = "rest", True
        else:
            ratio = (actual / planned) if planned else 1.0
            verdict = "done" if ratio >= 0.9 else ("partial" if ratio >= 0.7 else "missed")
            rest = False
        rows.append({
            "label": f"wk {week_start[5:]}",
            "verdict": verdict,
            "planned": planned if planned else None,
            "actual": actual if actual else None,
            "rest": rest,
        })
    return rows


@tool(
    "plan_chart",
    "Render a scheduled-vs-actual training-plan chart (ASCII/emoji): one bar "
    "per day (or per week for long windows) — █ = on-foot miles (run + walk, "
    "since easy days count prescribed walking), ░ = shortfall vs plan, verdict "
    "glyph per row (🟩done 🟨partial 🟥missed 🟦rest ⬜pending). "
    "THE tool for 'planned vs actual' / 'am I hitting my plan' asks — don't "
    "hand-roll a chart. Reproduce the full output in a fenced code block in "
    "your reply, then add the coach read — never leave it only in the "
    "collapsed tool call.",
    _PLAN_CHART_SCHEMA,
)
async def plan_chart(args: dict) -> dict:
    days = args.get("days") or 14
    err = _validate_days(days)
    if err:
        return _err(err)

    with db.connect() as conn:
        active = plans.get_active_plan(conn=conn)
        if active is None:
            return _err("no active training plan")
        frontier = db.last_known_daily_date(conn=conn)
        plan_dates = [w["date"] for w in active["workouts"]]
        anchor = frontier or date.today().isoformat()
        start = min(plan_dates) if plan_dates else anchor
        end = max([anchor, *plan_dates])
        activities_by_date = plans.load_activities_by_date(start, end, conn=conn)
        cfg = plans.resolve_grading_config(conn=conn)
    detail = plans.build_plan_detail(active, frontier, activities_by_date, cfg=cfg)

    window_start = (date.fromisoformat(anchor) - timedelta(days=days - 1)).isoformat()
    graded = [w for w in detail["workouts"] if window_start <= w["date"] <= anchor]
    if not graded:
        return _err("no plan workouts in window", days=days,
                    hint="the active plan has no prescribed days in this trailing window")

    weekly = args.get("weekly")
    if weekly is None:
        weekly = days > _LONG_WINDOW_BAR_DAYS
    adherence = detail.get("adherence_pct")
    adh = f" · adherence {adherence}%" if adherence is not None else ""

    if weekly:
        rows = _plan_chart_weekly_rows(graded)
        title = f"plan vs actual · last {days}d · weekly · {len(rows)} wks{adh}"
        legend = "█ actual vs ░ planned mi · 🟩≥90% 🟨70–89% 🟥<70% of plan · 🟦rest wk"
    else:
        rows = _plan_chart_rows(graded)
        n_runs = sum(1 for r in rows if not r["rest"])
        title = f"plan vs actual · last {days}d · {n_runs} runs{adh}"
        # █ plots actual_distance_m, which is ON-FOOT miles (run + walk) —
        # NOT run-only. Easy days count prescribed walking by design, so the
        # label must say on-foot, matching the bar and the PDF strip's
        # run/walk convention (0.27.0 label-vs-measurement class).
        legend = "█ on-foot mi vs ░ short of plan · 🟩done 🟨partial 🟥missed 🟦rest ⬜pending"

    return _text(charts.render_plan_vs_actual(rows, title=title, legend=legend))


_QUERY_WORKOUTS_SCHEMA = {
    "type": "object",
    "properties": {
        "activity_type": {"type": "string", "description": "Substring match, e.g. 'running'"},
        "days": {"type": "integer", "description": "Look back this many days"},
        # min_distance_km left the ADVERTISED schema at 0.57.0 (a deprecated
        # alias re-shipped in every session's preamble); _min_distance_meters
        # still accepts it silently, so an old client keeps working.
        "min_distance_mi": {"type": "number", "description": "Minimum distance in MILES (the app's display unit)"},
        "min_duration_min": {"type": "integer"},
        "limit": {"type": "integer", "description": "Max rows 1-500, default 50"},
    },
    "required": [],
}


def _min_distance_meters(args: dict) -> float | None | str:
    """Resolve the min-distance filter to meters, or an error string.

    ``min_distance_mi`` is the native param (this is a miles-display app —
    "runs over 5 miles" sent as ``min_distance_km: 5`` silently filtered at
    5 km); ``min_distance_km`` stays accepted as a deprecated alias, miles
    winning when both are given. Non-numeric values error instead of raising
    a raw ValueError at the user."""
    for key, to_meters in (("min_distance_mi", units.from_miles),
                           ("min_distance_km", lambda v: v * 1000.0)):
        raw = args.get(key)
        if raw is None or raw == "":
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return f"{key} must be a number"
        if value < 0:
            return f"{key} must be non-negative"
        return to_meters(value)
    return None


def _validate_limit(args: dict, *, default: int = 50, hi: int = 500) -> int | str:
    """A positive bounded row limit, or an error string. ``limit: -1``
    previously reached SQLite as ``LIMIT -1`` — which means NO limit, an
    unbounded table dump into model context; non-numeric raised raw."""
    raw = args.get("limit")
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int):
        return f"limit must be an integer between 1 and {hi}"
    if not (1 <= raw <= hi):
        return f"limit must be between 1 and {hi}"
    return raw


@tool(
    "query_workouts",
    "List workouts with optional filters (activity_type substring, days "
    "lookback, min distance in miles, min duration). Returns "
    "{workouts, count, truncated} — most recent first; truncated=true means "
    "more rows matched than the limit returned. Each workout also carries "
    "a measured `effort` (\"run\"/\"walk\"/null) — activity_type is Garmin's "
    "own label and can misreport a walk as a run (e.g. treadmill_running), "
    "so prefer `effort` for run-vs-walk questions.",
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
    min_meters = _min_distance_meters(args)
    if isinstance(min_meters, str):
        return _err(min_meters)
    if min_meters is not None:
        where.append("distance_meters >= ?")
        params.append(min_meters)
    if args.get("min_duration_min"):
        try:
            min_duration = int(args["min_duration_min"])
        except (TypeError, ValueError):
            return _err("min_duration_min must be an integer")
        where.append("duration_seconds >= ?")
        params.append(min_duration * 60)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    limit = _validate_limit(args)
    if isinstance(limit, str):
        return _err(limit)
    with db.connect() as conn:
        # limit+1 fetch: the extra row is the truncation signal (the
        # list_observations pattern) — without it "show me all my runs this
        # year" silently answered from a clipped 50.
        rows = conn.execute(
            f"""SELECT activity_id, date, activity_type, activity_name, duration_seconds,
                       distance_meters, avg_hr, max_hr, avg_pace_sec_per_km,
                       elevation_gain_meters, aerobic_te, anaerobic_te, training_load
                FROM activities {where_sql} ORDER BY date DESC, start_time DESC LIMIT ?""",
            (*params, limit + 1),
        ).fetchall()
    truncated = len(rows) > limit
    workouts = [_display_workout(dict(r)) for r in rows[:limit]]
    return _text({"workouts": workouts, "count": len(workouts), "truncated": truncated})


@tool(
    "get_workout_detail",
    "Full detail for one workout — splits and HR zones included. Carries a "
    "measured `effort` (\"run\"/\"walk\"/null) on the activity and each split — "
    "activity_type is Garmin's own label and can misreport a walk as a run.",
    {"activity_id": int},
)
async def get_workout_detail(args: dict) -> dict:
    aid = int(args["activity_id"])
    with db.connect() as conn:
        # Explicit columns, not SELECT *: raw_json is ~50 KB of preserved Garmin
        # payload that this response has always popped straight back off.
        # report_card owns the one list (see report_card._ACTIVITY_COLUMNS).
        act = conn.execute(
            f"SELECT {report_card._ACTIVITY_SELECT} FROM activities "
            "WHERE activity_id = ?",
            (aid,),
        ).fetchone()
        if not act:
            return _err("activity not found", activity_id=aid)
        zones = [dict(r) for r in conn.execute(
            "SELECT zone, seconds_in_zone FROM activity_hr_zones "
            "WHERE activity_id = ? ORDER BY zone",
            (aid,),
        ).fetchall()]
        splits = [_augment_workout(dict(r)) for r in conn.execute(
            "SELECT * FROM activity_splits WHERE activity_id = ? ORDER BY split_index",
            (aid,),
        ).fetchall()]
    # No raw_json pop — the SELECT above never fetched it.
    activity = _augment_workout(dict(act))
    return _text({"activity": activity, "hr_zones": zones, "splits": splits})


# 2g: distance_meters is SUM-per-period semantics (a running total, not a
# per-observation mean/sd) — kept as a separate frozen whitelist so the
# metric column is still membership-checked before it reaches an f-string,
# same discipline as DAILY_NUMERIC_METRICS / the "training_load" special case.
_COMPARE_SUM_METRICS = frozenset({"distance_meters"})


def _compare_periods_sum(conn, metric: str, args: dict) -> dict:
    """SUM-semantics branch of compare_periods (2g): "how much did I run this
    week vs last" has no per-observation stats — a period total, not a
    mean/sd. Per-period {n, total} (+ total_mi, miles-gated, the same
    display_units() gate _augment_workout uses); top-level delta/delta_pct
    follow the same a-minus-b convention as effect_size's delta_pct, but
    there is no per-observation SD to pool, so no cohens_d/magnitude.
    """
    def _period_total(start: str, end: str) -> dict:
        row = conn.execute(
            f"SELECT COUNT(*) AS n, SUM({metric}) AS total FROM activities "
            f"WHERE date >= ? AND date <= ? AND {metric} IS NOT NULL",
            (start, end),
        ).fetchone()
        return {"n": row["n"] or 0, "total": row["total"]}

    a = _period_total(args["period_a_start"], args["period_a_end"])
    b = _period_total(args["period_b_start"], args["period_b_end"])

    if units.display_units() == "miles":
        if a["total"] is not None:
            a["total_mi"] = units.to_miles(a["total"])
        if b["total"] is not None:
            b["total_mi"] = units.to_miles(b["total"])

    delta = None
    delta_pct = None
    if a["total"] is not None and b["total"] is not None:
        delta = a["total"] - b["total"]
        if b["total"]:
            delta_pct = (a["total"] - b["total"]) / b["total"] * 100

    for period in (a, b):
        if period.get("total") is not None:
            period["total"] = round(period["total"], 2)
    if delta is not None:
        delta = round(delta, 2)
    if delta_pct is not None:
        delta_pct = round(delta_pct, 1)

    return {"metric": metric, "period_a": a, "period_b": b, "delta": delta, "delta_pct": delta_pct}


_COMPARE_PERIODS_SCHEMA = {
    "type": "object",
    "properties": {
        "metric": {"type": "string"},
        "days": {
            "type": "integer",
            "description": "Shortcut: compare the last N days against the "
            "prior N days — no date arithmetic needed. Mutually exclusive "
            "with the four period_* dates.",
        },
        "period_a_start": {"type": "string"},
        "period_a_end": {"type": "string"},
        "period_b_start": {"type": "string"},
        "period_b_end": {"type": "string"},
    },
    "required": ["metric"],
}

_COMPARE_PERIOD_FIELDS = (
    "period_a_start", "period_a_end", "period_b_start", "period_b_end",
)


@tool(
    "compare_periods",
    "Compare a metric between two windows: pass days=N for 'last N days vs "
    "the prior N' (the common case), or two explicit ISO date ranges. "
    "Returns mean, SD, count for each + delta. Also accepts distance_meters "
    "(activities, SUMMED per period — no mean/SD for a period total, but a "
    "total_mi convenience and top-level delta/delta_pct). Use for things "
    "like 'last 30d vs prior 30d' or 'how much did I run this week vs last'.",
    _COMPARE_PERIODS_SCHEMA,
)
async def compare_periods(args: dict) -> dict:
    # days=N convenience (0.56.0): this tool had ZERO recorded calls while
    # run_sql hand-rolled period comparisons — every "last 30d vs prior 30d"
    # ask forced the model to compute four ISO dates. Derived windows are
    # echoed in the payload (derived_periods) so the answer can cite them.
    derived_periods = None
    if args.get("days") is not None:
        if any(args.get(f) is not None for f in _COMPARE_PERIOD_FIELDS):
            return _err("pass either days or the four period dates, not both")
        if err := _validate_days(args["days"]):
            return _err(err)
        n = args["days"]
        today = date.today()
        args = {
            **args,
            "period_a_start": (today - timedelta(days=n - 1)).isoformat(),
            "period_a_end": today.isoformat(),
            "period_b_start": (today - timedelta(days=2 * n - 1)).isoformat(),
            "period_b_end": (today - timedelta(days=n)).isoformat(),
        }
        derived_periods = {f: args[f] for f in _COMPARE_PERIOD_FIELDS}
    elif any(args.get(f) is None for f in _COMPARE_PERIOD_FIELDS):
        return _err(
            "pass either days=N (last N vs prior N) or all four period dates "
            f"({', '.join(_COMPARE_PERIOD_FIELDS)})"
        )
    # Validate all four dates + ordering BEFORE any query: a malformed or
    # reversed range compares as strings in SQL and returns {"n": 0} —
    # indistinguishable from a genuinely empty window, so the model reads
    # "no data" where the truth is "bad input".
    for field in _COMPARE_PERIOD_FIELDS:
        if msg := _validate_date(args.get(field), field):
            return _err(msg)
    for label in ("a", "b"):
        if args[f"period_{label}_start"] > args[f"period_{label}_end"]:
            return _err(
                f"period_{label}_start must be on or before period_{label}_end "
                f"(got {args[f'period_{label}_start']} > {args[f'period_{label}_end']})")
    metric = args["metric"]
    if metric in _COMPARE_SUM_METRICS:
        with db.connect() as conn:
            payload = _compare_periods_sum(conn, metric, args)
        if derived_periods:
            payload["derived_periods"] = derived_periods
        return _text(payload)
    if metric == "training_load":
        table = "activities"
    elif metric in DAILY_NUMERIC_METRICS:
        table = "daily_metrics"
    else:
        return _err(
            f"unknown metric '{metric}'",
            allowed=sorted(DAILY_NUMERIC_METRICS | {"training_load"} | _COMPARE_SUM_METRICS),
        )

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

    # _stats always returns an int n (0 on no rows), so effect_size's
    # whole-None branch is unreachable here — it always returns a dict.
    effect = interpret.effect_size(a["mean"], b["mean"], a["sd"], b["sd"], a["n"], b["n"])
    delta_pct = effect["delta_pct"] if effect else None
    cohens_d = effect["cohens_d"] if effect else None
    magnitude = effect["magnitude"] if effect else None

    payload = {
        "metric": metric, "period_a": a, "period_b": b,
        "delta_mean_a_minus_b": delta,
        "delta_pct": delta_pct, "cohens_d": cohens_d, "magnitude": magnitude,
    }
    # Round at the payload boundary; None passes through unrounded.
    for period in (payload["period_a"], payload["period_b"]):
        for field in ("mean", "sd"):
            if period.get(field) is not None:
                period[field] = round(period[field], 2)
    for field, ndigits in (("delta_mean_a_minus_b", 2), ("delta_pct", 1), ("cohens_d", 3)):
        if payload.get(field) is not None:
            payload[field] = round(payload[field], ndigits)
    if derived_periods:
        payload["derived_periods"] = derived_periods
    return _text(payload)


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
    "Days where a metric was more than N standard deviations from its 60-day "
    "baseline. Currently supports rhr and sleep_seconds. Today is always "
    "excluded: both metrics settle through the morning (Garmin revises them "
    "as the night is processed), so a provisional same-day value is not a "
    "confirmed anomaly — use get_metric_trend for today's read.",
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
    raw_threshold = args.get("sd_threshold")
    try:
        # None → default. `or` would also default an EXPLICIT 0 — which must
        # error (0 marks every day an anomaly), not silently become 2.0.
        threshold = 2.0 if raw_threshold is None else float(raw_threshold)
    except (TypeError, ValueError):
        return _err("sd_threshold must be a number between 0.5 and 10")
    # Bounded: 0 or negative marks EVERY day an anomaly; a huge value is a
    # silent no-op. Both read as data problems rather than input problems.
    if not (0.5 <= threshold <= 10):
        return _err("sd_threshold must be between 0.5 and 10")
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    # 0.59.0: `dm.date < today` — both supported metrics are SETTLING_METRICS
    # (Garmin revises them through the morning), and an anomaly is a settled
    # fact. Measured live 2026-08-10: a mid-sleep pull's rhr 54 (revised to 50
    # post-wake) would have scanned as a +2 SD spike.
    with db.connect() as conn:
        rows = conn.execute(
            f"""SELECT dm.date, dm.{metric} AS value,
                       b.{metric}_60day_mean AS baseline_mean,
                       b.{metric}_60day_sd AS baseline_sd
                FROM daily_metrics dm
                LEFT JOIN baselines b ON b.date = dm.date
                WHERE dm.date >= ? AND dm.date < ? AND dm.{metric} IS NOT NULL
                  AND b.{metric}_60day_mean IS NOT NULL
                  AND b.{metric}_60day_sd > 0
                  AND ABS(dm.{metric} - b.{metric}_60day_mean) > b.{metric}_60day_sd * ?
                ORDER BY dm.date DESC""",
            (cutoff, date.today().isoformat(), threshold),
        ).fetchall()
    # sleep_seconds baselines are raw AVG()/SD floats (~10 significant digits)
    # and the value is raw seconds — attach the "7h 33m" companion the coach
    # voice must speak instead, and round the baseline floats at the payload
    # boundary, the same discipline get_metric_trend/compare_periods follow.
    fmt = units.format_hm if metric.endswith("_seconds") else None
    anomalies = []
    for r in rows:
        row = dict(r)
        position = interpret.sd_position(row.get("value"), row.get("baseline_mean"), row.get("baseline_sd"))
        if position is not None:
            row["sd_distance"] = round(position["sd_distance"], 2)
            row["direction"] = position["direction"]
        if fmt is not None:
            for raw_key, fmt_key in (
                ("value", "value_formatted"),
                ("baseline_mean", "baseline_formatted"),
            ):
                formatted = fmt(row.get(raw_key))
                if formatted is not None:
                    row[fmt_key] = formatted
        for field in ("baseline_mean", "baseline_sd"):
            if row.get(field) is not None:
                row[field] = round(row[field], 2)
        anomalies.append(row)
    return _text({
        "metric": metric,
        "lookback_days": days,
        "sd_threshold": threshold,
        "anomalies": anomalies,
    })


# The pull statuses that mean nothing landed and the caller has to act.
# `partial` is deliberately NOT here: daily.pull reports it whenever any gap
# remains anywhere back to EARLIEST_BACKFILL_DATE, so a DB with one missing
# historical day is partial on every sync forever — flagging that as an error
# told the user their fresh sync had failed.
_SYNC_FAILURE_STATUSES = frozenset({
    "auth_failure", "not_configured", "failure", "interrupted",
})


def _sync_state(result: dict, *, recomputed: bool) -> str:
    """One-line plain-English read of a pull that isn't an outright failure.

    House rule (see interpret.py): tested Python derives the judgment, the
    model only phrases it around this string. Deterministic in the pull dict.
    """
    status = result.get("status") or "unknown"
    days = int(result.get("days_pulled") or 0)
    activities = int(result.get("activities_loaded") or 0)
    last_date = result.get("last_date")

    if days or activities:
        counted = []
        if days:
            counted.append(f"{days} day(s)")
        if activities:
            counted.append(f"{activities} activity row(s)")
        head = "synced " + ", ".join(counted)
        if last_date:
            head += f" through {last_date}"
    elif status == "skipped":
        head = "already up to date"
    else:
        head = "no new data landed"

    caveats = [
        f"{count} day(s) {label}"
        for count, label in (
            (int(result.get("deferred_count") or 0), "deferred"),
            (int(result.get("days_failed") or 0), "failed"),
            (int(result.get("gap_days_remaining") or 0), "still missing"),
        )
        if count
    ]
    tail = "baselines recomputed" if recomputed else "baselines unchanged"
    segments = [f"{status} — {head}"] + ([", ".join(caveats)] if caveats else []) + [tail]
    return "; ".join(segments)


def _sync_min_interval_minutes() -> int:
    """How recently a successful pull must have completed for a new
    `sync_garmin_data` call to short-circuit as fresh. Env-overridable per the
    repo pattern; a bad value degrades to the default rather than erroring."""
    try:
        return int(os.environ.get("LOCAL_FITNESS_SYNC_MIN_INTERVAL_MIN", "10"))
    except ValueError:
        return 10


def _recent_successful_sync(now: datetime) -> datetime | None:
    """completed_at of the newest non-failed ingest run inside the freshness
    window, else None. Failure statuses never count as fresh — a failed pull
    ten seconds ago is a reason to retry, not to skip. Fail-open on any DB
    problem: a fresh clone with no schema yet must fall through to the real
    pull (which is what creates the data), never error on its guard."""
    placeholders = ",".join("?" * len(_SYNC_FAILURE_STATUSES))
    try:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT completed_at FROM ingest_runs "
                f"WHERE completed_at IS NOT NULL AND status NOT IN ({placeholders}) "
                "AND status != 'in_progress' "
                "ORDER BY completed_at DESC LIMIT 1",
                tuple(_SYNC_FAILURE_STATUSES),
            ).fetchone()
    except sqlite3.Error:
        return None
    if not row or not row["completed_at"]:
        return None
    try:
        completed = datetime.fromisoformat(row["completed_at"])
    except ValueError:
        return None
    if timedelta() <= now - completed <= timedelta(minutes=_sync_min_interval_minutes()):
        return completed
    return None


def _latest_activity_summary() -> dict | None:
    """The newest activity as a compact handoff row, or None on an empty DB.

    Attached to every non-short-circuit sync payload so "pull my data and
    grade my run" is TWO calls (sync -> workout_report_card), not three —
    recorded sessions burned a query_workouts call seven times purely to
    resolve "my recent run" into an activity_id the report card could take.
    Fail-open (None) on any DB problem — this is garnish on the sync payload,
    never a reason for a sync that just succeeded to report an error.
    """
    try:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT activity_id, date, activity_type, distance_meters, "
                "avg_pace_sec_per_km FROM activities "
                "ORDER BY date DESC, start_time DESC LIMIT 1"
            ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    w = dict(row)
    mode = interpret.is_running_effort(w.get("avg_pace_sec_per_km"))
    summary = {
        "activity_id": w["activity_id"],
        "date": w["date"],
        "activity_type": w["activity_type"],
        "effort": {True: "run", False: "walk", None: None}[mode],
    }
    distance_mi = units.to_miles(w.get("distance_meters"))
    if distance_mi is not None:
        summary["distance_mi"] = distance_mi
    return summary


_SYNC_GARMIN_DATA_SCHEMA = {
    "type": "object",
    "properties": {
        "force": {
            "type": "boolean",
            "description": "Pull even when a successful sync completed within "
            "the freshness window (default 10 min). Default false.",
        },
    },
    "required": [],
}


@tool(
    "sync_garmin_data",
    "Pull the latest data from Garmin Connect into the database (gap-aware: "
    "fills missing days, always refreshes the last few days so day-end totals "
    "overwrite partial values) and recompute baselines/training-load whenever "
    "any new day or activity landed. Every other tool only reads what's "
    "already in the DB — call this first when the user asks to "
    "sync/refresh/pull/update their data, or when today's data looks stale or "
    "missing. A successful sync in the last ~10 minutes short-circuits as "
    "status 'fresh' with no Garmin call — a re-ask moments later needs no "
    "second pull; pass force:true to pull anyway. The payload's "
    "`latest_activity` is the newest workout's id/date/type — hand it "
    "straight to workout_report_card, no query_workouts lookup needed. Read "
    "the returned `sync_state` line: status 'partial' is normal when older "
    "history is still incomplete and does NOT mean this sync failed.",
    _SYNC_GARMIN_DATA_SCHEMA,
)
async def sync_garmin_data(args: dict) -> dict:
    # Freshness short-circuit (0.56.0) — at the TOOL layer, deliberately not
    # inside daily.pull: pull's own 3-day refresh union makes every pull hit
    # Garmin, and the launchd jobs WANT that (day-end totals overwrite partial
    # values). What this guards is the chat-session shape measured in the
    # audit: the user re-asks, the agent re-syncs — 8 of 24 recorded calls
    # were pure repeats minutes apart, each a full Garmin round-trip that
    # landed nothing. Checked before the garminconnect import so the fresh
    # path costs one indexed SQLite read.
    if not args.get("force"):
        completed = _recent_successful_sync(datetime.now())
        if completed is not None:
            minutes_ago = max(0, int((datetime.now() - completed).total_seconds() // 60))
            payload = {
                "status": "fresh",
                "synced_through": db.last_known_daily_date(),
                "sync_state": (
                    f"already synced {minutes_ago} min ago — nothing pulled; "
                    "pass force:true to pull again"
                ),
            }
            latest = _latest_activity_summary()
            if latest is not None:
                payload["latest_activity"] = latest
            return _text(payload)

    # Deferred import: garminconnect drags requests (~28 ms measured) into
    # every `import tools` — every stdio session start, every server boot —
    # and this tool is its ONLY consumer. Tests patch
    # local_fitness.ingest.daily.pull directly (same module object).
    from ..ingest import daily as daily_ingest

    result = await asyncio.to_thread(daily_ingest.pull, max_days=SYNC_MAX_DAYS)
    status = result.get("status")
    # Recompute on any landed data, not just a clean `success`: activities can
    # arrive without a new wellness day, and gating on `success` meant a DB
    # with an old gap never refreshed CTL/ATL/TSB again — downstream tools
    # served stale training load while reporting current data.
    recomputed = bool(result.get("days_pulled") or result.get("activities_loaded"))
    if recomputed:
        await asyncio.to_thread(baselines_mod.recompute, lookback_days=90)
    if status in _SYNC_FAILURE_STATUSES:
        return _err(
            result.get("error") or f"Garmin sync failed ({status})",
            status=status,
            days_pulled=result.get("days_pulled", 0),
            days_failed=result.get("days_failed", 0),
            last_date=result.get("last_date"),
        )
    # Drop a null `error` key rather than shipping it: a success payload
    # carrying "error": null pattern-matches as a failure to any naive
    # detector (it fooled the 2026-08-10 audit's first pass).
    payload = {k: v for k, v in result.items() if k != "error" or v is not None}
    payload["sync_state"] = _sync_state(result, recomputed=recomputed)
    latest = _latest_activity_summary()
    if latest is not None:
        payload["latest_activity"] = latest
    return _text(payload)


@tool(
    "training_load_status",
    # Band prose is BUILT from the interpret constants so the description can
    # never drift from the classifier actually attached to the payload (the
    # identical duplication was already removed from correlate's legend once).
    "Current CTL/ATL/TSB plus 30-day history. CTL = fitness (42-day EWMA), "
    "ATL = fatigue (7-day EWMA), TSB = form (CTL - ATL). "
    f"TSB > {interpret.TSB_FRESH:g} fresh, "
    f"{interpret.TSB_FATIGUED:g}..{interpret.TSB_FRESH:g} neutral, "
    f"< {interpret.TSB_FATIGUED:g} fatigued, < {interpret.TSB_VERY_FATIGUED:g} very fatigued. "
    "`current` is always the last COMPLETE day (never today's own row, which "
    "assumes zero training_load until something syncs) — today's same-day "
    "projection, if any, rides along separately under `projected_end_of_day`.",
    {},
)
async def training_load_status(_args: dict) -> dict:
    # Lazy import: brief_planner -> status -> tools would cycle at module
    # scope (same pattern as get_brief_context, tools.py:1591-1594).
    from . import brief_planner

    cutoff = (date.today() - timedelta(days=30)).isoformat()
    with db.connect() as conn:
        recent = [dict(r) for r in conn.execute(
            "SELECT date, ctl, atl, tsb FROM baselines "
            "WHERE date >= ? AND ctl IS NOT NULL ORDER BY date DESC",
            (cutoff,),
        ).fetchall()]
        if not recent:
            return _err(
                "no training-load data yet — call sync_garmin_data to pull "
                "activities (baselines recompute automatically once data lands)"
            )
        # The 14-day "then" CTL is single-sourced in brief_planner (the same
        # no-lookback-floor query the brief signal uses) so this agrees with
        # the brief by construction, even on gappy baselines — a point
        # picked from this tool's own 30-day window could disagree. Runs on
        # this tool's existing connection: not in tests/test_perf_benchmarks.py's
        # benchmarked set, so the extra indexed point-query is allowed.
        anchor = (date.today() - timedelta(days=brief_planner._LOOKBACK_DAYS)).isoformat()
        ctl_then = brief_planner.ctl_at_or_before(conn, anchor)

    # Fix 9 (2026-07-27): "current" is the last COMPLETE day (date < today),
    # never today's own row — baselines.recompute walks the CTL/ATL EWMA
    # forward assuming today's training_load is whatever's posted so far (0
    # before any activity syncs), so treating today's row as current form
    # pre-credits a zero-load rest day that hasn't happened. Measured live:
    # TSB read -12.74 today vs -22.41 yesterday, crossing the
    # very-fatigued/fatigued zone boundary purely because no run had posted
    # yet. `recent` is already DESC by date, so the first date < today wins.
    today_str = date.today().isoformat()
    current = next((r for r in recent if r["date"] < today_str), None)
    projected_row = next((r for r in recent if r["date"] == today_str), None)
    if current is None:
        return _err(
            "no training-load data yet — call sync_garmin_data to pull "
            "activities (baselines recompute automatically once data lands)"
        )
    ctl_pct = interpret.pct_change(current.get("ctl"), ctl_then)
    if ctl_pct is not None:
        ctl_pct = round(ctl_pct, 1)
    tsb_zone = interpret.tsb_zone(current.get("tsb"))
    # A scalar %-delta, not a slope/series — delta_direction, not trend_direction.
    ctl_direction = interpret.delta_direction(ctl_pct)

    projected_end_of_day = None
    if projected_row is not None:
        # Today's own (same-day-projection) row, exposed separately — never
        # reported as "current".
        projected_end_of_day = {
            "ctl": projected_row.get("ctl"),
            "atl": projected_row.get("atl"),
            "tsb": projected_row.get("tsb"),
            "interpretation": interpret.tsb_zone(projected_row.get("tsb")),
        }
        for field in ("ctl", "atl", "tsb"):
            if projected_end_of_day.get(field) is not None:
                projected_end_of_day[field] = round(projected_end_of_day[field], 2)

    for row in recent:  # `current` is one of these rows — rounding recent rounds it too.
        for field in ("ctl", "atl", "tsb"):
            if row.get(field) is not None:
                row[field] = round(row[field], 2)

    # No static `interpretation` legend (removed 0.56.0): the three prose
    # lines re-shipped on every call while the tool DESCRIPTION already
    # carries the zone bands built from the interpret constants — the same
    # duplication that was once removed from correlate's legend.
    return _text({
        "current": current,
        "history_30d": recent,
        "tsb_zone": tsb_zone,
        "ctl_pct_change_14d": ctl_pct,
        "ctl_direction": ctl_direction,
        "projected_end_of_day": projected_end_of_day,
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
    read = interpret.correlation_read(r_val)
    return _text({
        "metric_a": a, "metric_b": b, "days": days, "lag_days": lag,
        "n_pairs": n,
        "pearson_r": round(r_val, 3) if r_val is not None else None,
        "strength": read["strength"] if read else None,
        "direction": read["direction"] if read else None,
    })


_RECOVERY_SCHEMA = {
    "type": "object",
    "properties": {
        "activity_type": {"type": "string"},
        "min_distance_mi": {"type": "number", "description": "Minimum distance in MILES"},
        "min_distance_km": {"type": "number", "description": "(deprecated — use min_distance_mi)"},
        "min_duration_min": {"type": "integer"},
        "lookback_days": {"type": "integer", "description": "Default 365"},
    },
    "required": [],
}


@tool(
    "recovery_pattern",
    "After workouts matching the filter, how many days does body battery max and RHR typically take to return to within 95% / 103% of baseline? Returns averages, the 10 most-recent matched workouts, and how many were skipped for want of a baseline. A workout matches as long as EITHER channel has a usable baseline for its date — the other channel reads null with a bb_baseline_note/rhr_baseline_note explaining why, never a false 'recovered instantly'.",
    _RECOVERY_SCHEMA,
)
async def recovery_pattern(args: dict) -> dict:
    where: list[str] = []
    params: list = []
    if args.get("activity_type"):
        where.append("activity_type LIKE ?")
        params.append(f"%{args['activity_type']}%")
    min_meters = _min_distance_meters(args)
    if isinstance(min_meters, str):
        return _err(min_meters)
    if min_meters is not None:
        where.append("distance_meters >= ?")
        params.append(min_meters)
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
        # 2f: avg_pace_sec_per_km + duration_seconds widened in so
        # _augment_workout below can produce its full field set (pace,
        # duration_formatted), not just distance_mi.
        workouts = [dict(r) for r in conn.execute(
            f"SELECT activity_id, date, activity_type, distance_meters, "
            f"training_load, aerobic_te, avg_pace_sec_per_km, duration_seconds "
            f"FROM activities WHERE {where_sql} ORDER BY date",
            params,
        ).fetchall()]
        # Two range loads instead of a point query per workout per day. The old
        # shape was 1 baselines lookup + up to 7 daily_metrics probes for EVERY
        # matched workout — on a year of running that is ~950 round trips for a
        # payload of ten rows. Both lookup tables are keyed by date, so one
        # BETWEEN each covers every workout and the offsets resolve in Python.
        baselines_by_date: dict[str, dict] = {}
        metrics_by_date: dict[str, dict] = {}
        if workouts:
            w_dates = [w["date"] for w in workouts]
            lo, hi = min(w_dates), max(w_dates)
            # Recovery is probed at offsets +1..+7, so the metrics window runs
            # one week past the last workout; baselines are read at the workout
            # date itself.
            metrics_hi = (date.fromisoformat(hi) + timedelta(days=7)).isoformat()
            metrics_lo = (date.fromisoformat(lo) + timedelta(days=1)).isoformat()
            baselines_by_date = {r["date"]: dict(r) for r in conn.execute(
                "SELECT date, body_battery_max_60day_mean AS bb, rhr_60day_mean AS rhr "
                "FROM baselines WHERE date BETWEEN ? AND ?",
                (lo, hi),
            ).fetchall()}
            metrics_by_date = {r["date"]: dict(r) for r in conn.execute(
                "SELECT date, body_battery_max, rhr FROM daily_metrics "
                "WHERE date BETWEEN ? AND ?",
                (metrics_lo, metrics_hi),
            ).fetchall()}

    results = []
    # Whole-workout skip only when NEITHER channel has a usable baseline for
    # that date — the original gate skipped the entire workout (including a
    # perfectly good rhr baseline) whenever body_battery_max_60day_mean was
    # NULL, which is every date after 2026-01-27 (that baseline column derived
    # from the now-dead body_battery_max ingest — see the daily.py fix). A
    # 90-day recovery_pattern call was therefore returning 0 matched no matter
    # what, even though rhr_60day_mean was alive the whole time.
    n_skipped_no_baseline = 0
    # Of the workouts that DID match, how many had no usable baseline for one
    # specific channel — that channel is n/a on the workout (see
    # bb_baseline_note/rhr_baseline_note) rather than silently 0/None looking
    # like "recovered instantly".
    n_skipped_no_bb_baseline = 0
    n_skipped_no_rhr_baseline = 0
    for w in workouts:
        wdate = date.fromisoformat(w["date"])
        baseline = baselines_by_date.get(w["date"])
        bb_baseline = baseline["bb"] if baseline else None
        rhr_baseline = baseline["rhr"] if baseline else None
        if bb_baseline is None and rhr_baseline is None:
            n_skipped_no_baseline += 1
            continue

        bb_note = None
        rhr_note = None
        if bb_baseline is None:
            n_skipped_no_bb_baseline += 1
            bb_note = "no body-battery baseline for this date"
        if rhr_baseline is None:
            n_skipped_no_rhr_baseline += 1
            rhr_note = "no RHR baseline for this date"

        bb_recovery = None
        rhr_recovery = None
        for offset in range(1, 8):
            row = metrics_by_date.get((wdate + timedelta(days=offset)).isoformat())
            if not row:
                continue
            if (
                bb_recovery is None
                and bb_baseline is not None
                and row["body_battery_max"]
                and row["body_battery_max"] >= bb_baseline * 0.95
            ):
                bb_recovery = offset
            if (
                rhr_recovery is None
                and rhr_baseline is not None
                and row["rhr"]
                and row["rhr"] <= rhr_baseline * 1.03
            ):
                rhr_recovery = offset
        results.append(_augment_workout({
            **w,
            "recovery_days_to_bb_baseline": bb_recovery,
            "recovery_days_to_rhr_baseline": rhr_recovery,
            "bb_baseline_note": bb_note,
            "rhr_baseline_note": rhr_note,
        }))

    bb_vals = [r["recovery_days_to_bb_baseline"] for r in results if r["recovery_days_to_bb_baseline"]]
    rhr_vals = [r["recovery_days_to_rhr_baseline"] for r in results if r["recovery_days_to_rhr_baseline"]]
    return _text({
        "n_workouts_matched": len(results),
        # Workouts that cleared the filter but had NEITHER channel's baseline
        # for their own date, so nothing at all could be computed for them.
        # These were always dropped silently, which made "3 matched"
        # unreadable — 3 of 3 and 3 of 40 printed identically.
        "n_skipped_no_baseline": n_skipped_no_baseline,
        # Of the MATCHED workouts, how many had no usable baseline for just
        # one channel (that channel's recovery is n/a on the workout, with a
        # note, rather than silently reading as "recovered instantly"). A
        # whole-workout skip used to be driven by body-battery baseline alone
        # (dead since 2026-01-27 — see daily.py's body_battery_min/max fix),
        # which zeroed out every recent-window match even when rhr baselines
        # were fine.
        "n_skipped_no_bb_baseline": n_skipped_no_bb_baseline,
        "n_skipped_no_rhr_baseline": n_skipped_no_rhr_baseline,
        "avg_recovery_days_body_battery": round(sum(bb_vals) / len(bb_vals), 2) if bb_vals else None,
        "avg_recovery_days_rhr": round(sum(rhr_vals) / len(rhr_vals), 2) if rhr_vals else None,
        "recent_workouts": results[-10:],
    })


# Wall-clock budget for a single run_sql query. A heavier query is aborted by
# the SQLite progress handler so a recursive CTE / cartesian join can't hang the
# (single-threaded) server. Granularity: how many VM ops between deadline checks.
_RUN_SQL_TIME_BUDGET_S = 5.0
_RUN_SQL_PROGRESS_OPS = 10_000
# Hard row cap on a run_sql result. Bounds a "SELECT * FROM activities"-style ask
# so an ad-hoc query can't shove the whole table into the model's context. The
# cap is SIGNALLED, not silent: _run_sql_blocking fetches one past it so run_sql
# can tell "exactly 500 rows matched" from "the cap clipped a larger result".
_RUN_SQL_ROW_CAP = 500


def _run_sql_blocking(q: str) -> list[dict]:
    """Execute `q` against a READ-ONLY connection with a wall-clock deadline.

    The read-only connection is the real write gate (engine-enforced); the
    keyword denylist in run_sql is only defense-in-depth. The progress handler
    aborts once the deadline passes, which makes SQLite raise OperationalError.
    Runs in a worker thread (via asyncio.to_thread) so even a within-budget
    heavy query never blocks the event loop.

    Fetches ``_RUN_SQL_ROW_CAP + 1`` rows so the caller can detect (and flag)
    truncation instead of silently returning a clipped set as if complete.
    """
    deadline = time.monotonic() + _RUN_SQL_TIME_BUDGET_S

    def _abort_if_over_budget() -> int:
        # Truthy return => SQLite interrupts the running statement.
        return 1 if time.monotonic() > deadline else 0

    with db.connect_readonly() as conn:
        conn.set_progress_handler(_abort_if_over_budget, _RUN_SQL_PROGRESS_OPS)
        try:
            return [dict(r) for r in conn.execute(q).fetchmany(_RUN_SQL_ROW_CAP + 1)]
        finally:
            conn.set_progress_handler(None, 0)


@tool(
    "run_sql",
    # 0.57.0: the full per-table column dump (1.6 KB re-shipped every
    # session) is gone from this description — table names only; columns live
    # in the fitness://schema resource, and a bad column gets a corrective
    # error naming the valid ones (observed retries succeed first try).
    "Execute a read-only SELECT or WITH query against the fitness DB. "
    "Tables: " + ", ".join(QUERYABLE_SCHEMA) + ". "
    "Column lists: read the fitness://schema resource, or expect a "
    "corrective error naming the valid columns on a miss. "
    "Use this for ad-hoc analysis the other tools don't cover. "
    f"Results are capped at {_RUN_SQL_ROW_CAP} rows; when more match, the "
    "payload carries \"truncated\": true — add a LIMIT or aggregate to see the rest.",
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
        # attempt that slipped past the denylist.
        if "interrupt" in str(e).lower():
            return _err("query exceeded time budget")
        # The sqlite message IS included (0.37.0 — a deliberate reversal of
        # the earlier "don't leak the raw string" stance): the query was
        # authored by the MODEL, so "no such column: sleep_hours" leaks only
        # the model's own typo and is the entire signal needed for a one-shot
        # correction. The read-only URI gate means no write-channel detail
        # can appear here. Withholding it produced blind same-shape retries.
        return _err(
            f"query failed: {e} — check table/column names "
            "against the fitness://schema resource"
        )
    except sqlite3.Error as e:
        return _err(
            f"query failed: {e} — check table/column names "
            "against the fitness://schema resource"
        )
    if len(rows) > _RUN_SQL_ROW_CAP:
        rows = rows[:_RUN_SQL_ROW_CAP]
        return _text({
            "rows": rows,
            "count": len(rows),
            "truncated": True,
            "hint": f"row cap hit — showing the first {_RUN_SQL_ROW_CAP} "
                    "of a larger result; add a LIMIT or aggregate to see the rest",
        })
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
    "save_coach_memory",
    "Write ONE line into your coach's journal — YOUR record of the "
    "relationship, distinct from user-preference notes. Use it when "
    "something in the conversation is worth remembering across sessions: "
    "an excuse for a skipped session, a promise ('back on it Monday'), an "
    "injury flag, a breakthrough. One dated line in your own coach voice, "
    "under 240 chars. Skip routine Q&A. The newest 60 entries show "
    "everywhere; older ones archive (searchable via recall_coach_memories), "
    "never vanish. Feeds every future brief and report card.",
    {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The memory line, <=240 chars, coach voice.",
            },
            "date": {
                "type": "string",
                "description": "ISO date the memory is about; defaults to today.",
            },
        },
        "required": ["text"],
    },
)
async def save_coach_memory(args: dict) -> dict:
    text = (args.get("text") or "").strip()
    entry_date = args.get("date")
    if entry_date is not None and (msg := _validate_date(entry_date)):
        return _err(msg)
    try:
        entry = journal.save_entry(text, source="chat", entry_date=entry_date)
    except ValueError as e:
        return _err(str(e))
    return _text({"saved": True, **entry})


@tool(
    "list_coach_memories",
    "Read your coach's journal (newest first) — what you've written down "
    "about the relationship. Use when the user asks 'what do you remember', "
    "before saving a new memory (avoid duplicates — escalate instead), or "
    "to ground a callback. Returns entry_ids for delete_coach_memory. Set "
    "include_archived to browse past the hot 60; use recall_coach_memories "
    "to SEARCH the archive instead of paging through it.",
    {
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": "Only entries about the trailing N days.",
            },
            "limit": {
                "type": "integer",
                "description": "Max entries returned (default 50).",
            },
            "include_archived": {
                "type": "boolean",
                "description": "Also list archived entries (default false).",
            },
        },
        "required": [],
    },
)
async def list_coach_memories(args: dict) -> dict:
    days = args.get("days")
    limit = args.get("limit")
    include_archived = bool(args.get("include_archived", False))
    if limit is None:
        limit = 50
    if days is not None and (not isinstance(days, int) or days <= 0):
        return _err("days must be a positive integer")
    if not isinstance(limit, int) or limit <= 0:
        return _err("limit must be a positive integer")
    cap = 200 if include_archived else journal.JOURNAL_CAP
    effective = min(limit, cap)
    entries = journal.list_entries(
        days=days, limit=effective + 1, include_archived=include_archived)
    truncated = len(entries) > effective
    entries = entries[:effective]
    return _text({"memories": entries, "count": len(entries), "truncated": truncated})


@tool(
    "delete_coach_memory",
    "Remove one journal entry by entry_id (from list_coach_memories). Use "
    "when the user tells you to forget something, or when an entry turned "
    "out to be wrong. Confirm with the user first if the target is ambiguous.",
    {"entry_id": int},
)
async def delete_coach_memory(args: dict) -> dict:
    entry_id = args.get("entry_id")
    if entry_id is None or not isinstance(entry_id, int):
        return _err("entry_id is required")
    if not journal.delete_entry(entry_id):
        return _err(f"no journal entry with entry_id {entry_id}")
    return _text({"deleted": True, "entry_id": entry_id})


@tool(
    "recall_coach_memories",
    "Search your ENTIRE coach journal by keyword — including entries "
    "archived beyond the 60 recent ones in your memory section. Use BEFORE "
    "answering anything about past conversations or older context ('didn't "
    "we talk about...', an old injury, a past promise): search first, then "
    "answer only from what comes back — never cite a memory the search "
    "didn't return. Best matches first; archived entries are flagged.",
    {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Keywords to search for, e.g. 'knee pain' or "
                    "'marathon goal'."),
            },
            "limit": {
                "type": "integer",
                "description": "Max matches returned (default 8, max 25).",
            },
        },
        "required": ["query"],
    },
)
async def recall_coach_memories(args: dict) -> dict:
    query = (args.get("query") or "").strip()
    if not query:
        return _err("query is required")
    if len(query) > 200:
        return _err("query too long (max 200 chars)")
    limit = args.get("limit")
    if limit is None:
        limit = 8
    if not isinstance(limit, int) or limit <= 0:
        return _err("limit must be a positive integer")
    try:
        matches, mode = journal.search_entries(query, limit=min(limit, 25))
    except ValueError as e:
        return _err(str(e))
    for m in matches:
        m["archived"] = bool(m["archived"])
    return _text({
        "query": query,
        "matches": matches,
        "count": len(matches),
        "search": mode,
    })


def _spec_payload(spec: personality.PersonalitySpec) -> dict:
    return {
        "base_profile": spec.base_profile,
        "identity": spec.identity,
        "catchphrases": list(spec.catchphrases),
        "principles": list(spec.principles),
        "never_do": list(spec.never_do),
        "intensity": dict(spec.intensity),
        "updated_at": spec.updated_at,
    }


@tool(
    "get_coach_personality",
    "The live coach personality: active profile, the tuned spec (or the "
    "profile-file seed if never tuned), the five numeric dials, and journal "
    "size. Call before update_coach_personality so an edit patches what is "
    "actually there. `customized` says whether a tuned spec is in force; "
    "`base_profile_mismatch` means a spec exists but was tuned for a "
    "DIFFERENT profile (ignored until you switch back or reset).",
    {},
)
async def get_coach_personality(_args: dict) -> dict:
    # ONE connection for the whole tool (was 4: resolve_coach_profile opened
    # two on its own, get_setting a third, the counts a fourth), and ONE pass
    # over coach_journal instead of two full COUNT(*) scans.
    with db.connect() as conn:
        profile = coach.resolve_coach_profile(conn=conn)
        stored = personality.parse_spec(
            db.get_setting(personality.SPEC_KEY, conn=conn))
        journal_count, archived_count = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN archived = 0 THEN 1 END), 0), "
            "       COALESCE(SUM(CASE WHEN archived = 1 THEN 1 END), 0) "
            "FROM coach_journal"
        ).fetchone()
    effective = profile.spec or personality.seed_from_profile(profile)
    return _text({
        "profile": profile.name,
        "customized": profile.spec is not None,
        "base_profile_mismatch": (
            stored is not None and stored.base_profile != profile.name),
        "spec": _spec_payload(effective),
        "dials": {
            "harshness": profile.harshness,
            "warmth": profile.warmth,
            "push": profile.push,
            "roast_threshold": profile.roast_threshold,
            "praise_threshold": profile.praise_threshold,
        },
        "intensity_levels": list(personality.INTENSITY_LEVELS),
        "known_topics": sorted(personality.TOPIC_WHITELIST),
        "journal_entries": journal_count,
        "journal_archived": archived_count,
        "memory_enabled": memory.memory_enabled(),
    })


_DIAL_FIELDS = {"harshness": (0, 10), "warmth": (0, 10), "push": (0, 10)}
_THRESH_FIELDS = {"roast_threshold": (0.0, 1.20), "praise_threshold": (0.0, 1.20)}


@tool(
    "update_coach_personality",
    "Tune the coach's personality conversationally — the agent-owned write "
    "path (there is no UI). All fields optional: `identity` replaces the "
    "persona prose; add/remove_catchphrase, add/remove_principle, "
    "add/remove_never_do edit the lists; `set_intensity` maps topic slugs "
    "(e.g. step_goal_nagging, quality_day_misses — 'medium' clears an "
    "override) to off|low|medium|high|brutal; harshness/warmth/push (0-10) "
    "and roast/praise_threshold (0-1.20) write the numeric dials; "
    "`reset: true` discards the tuned spec (with no other fields = back to "
    "the shipped profile). The first tuning call seeds the spec from the "
    "active profile. Takes effect on the next prompt render — no restart.",
    {
        "type": "object",
        "properties": {
            "identity": {"type": "string", "description": "Replacement persona prose (<=4000 chars)."},
            "add_catchphrase": {"type": "string"},
            "remove_catchphrase": {"type": "string"},
            "add_principle": {"type": "string"},
            "remove_principle": {"type": "string"},
            "add_never_do": {"type": "string"},
            "remove_never_do": {"type": "string"},
            "set_intensity": {
                "type": "object",
                "description": "topic slug -> off|low|medium|high|brutal",
            },
            "harshness": {"type": "integer"},
            "warmth": {"type": "integer"},
            "push": {"type": "integer"},
            "roast_threshold": {"type": "number"},
            "praise_threshold": {"type": "number"},
            "reset": {"type": "boolean"},
        },
        "required": [],
    },
)
async def update_coach_personality(args: dict) -> dict:
    reset = bool(args.get("reset"))
    dial_args = {}
    errors: list[str] = []
    for key, (lo, hi) in _DIAL_FIELDS.items():
        if key in args:
            v = args[key]
            if not isinstance(v, int) or not (lo <= v <= hi):
                errors.append(f"{key} must be an integer {lo}-{hi}")
            else:
                dial_args[f"coach_{key}"] = str(v)
    for key, (lo, hi) in _THRESH_FIELDS.items():
        if key in args:
            v = args[key]
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not (lo <= v <= hi):
                errors.append(f"{key} must be a number {lo}-{hi}")
            else:
                dial_args[f"coach_{key}"] = str(float(v))

    spec_patch = {k: v for k, v in args.items()
                  if k in personality.PATCH_FIELDS}
    clean_patch, patch_errors = personality.validate_patch(spec_patch)
    errors.extend(patch_errors)
    unknown = set(args) - personality.PATCH_FIELDS - set(_DIAL_FIELDS) \
        - set(_THRESH_FIELDS) - {"reset"}
    for key in sorted(unknown):
        errors.append(f"unknown field '{key}'")
    if errors:
        return _err(
            "; ".join(errors),
            editable_fields=sorted(
                personality.PATCH_FIELDS | set(_DIAL_FIELDS)
                | set(_THRESH_FIELDS) | {"reset"}),
            intensity_levels=list(personality.INTENSITY_LEVELS),
        )
    if not reset and not clean_patch and not dial_args:
        return _err("nothing to update — pass at least one editable field")

    for key, value in dial_args.items():
        db.set_setting(key, value)

    profile = coach.resolve_coach_profile()
    if reset and not clean_patch:
        # Back to stock: drop the stored spec outright.
        with db.connect() as conn:
            conn.execute("DELETE FROM settings WHERE key = ?",
                         (personality.SPEC_KEY,))
        return _text({"updated": True, "reset": True,
                      "profile": profile.name, "customized": False,
                      "dials_changed": sorted(dial_args)})

    if clean_patch:
        base = (personality.seed_from_profile(profile) if reset
                else (profile.spec or personality.seed_from_profile(profile)))
        new_spec = personality.apply_patch(base, clean_patch)
        new_spec = replace_dataclass(
            new_spec,
            base_profile=profile.name,
            updated_at=datetime.now().isoformat(timespec="seconds"),
        )
        raw = personality.spec_to_json(new_spec)
        if len(raw.encode("utf-8")) > personality.SPEC_MAX_BYTES:
            return _err(
                f"personality spec would exceed {personality.SPEC_MAX_BYTES} "
                "bytes — trim the identity or the lists first")
        db.set_setting(personality.SPEC_KEY, raw)
        return _text({"updated": True, "profile": profile.name,
                      "customized": True, "spec": _spec_payload(new_spec),
                      "dials_changed": sorted(dial_args)})

    return _text({"updated": True, "profile": profile.name,
                  "customized": profile.spec is not None,
                  "dials_changed": sorted(dial_args)})


#: Static description of the launchd schedule. The send TIME lives in
#: `ops/com.localfitness.briefmail.plist.template`, not in settings, so this is
#: reported as prose rather than as an editable field — a tool that returned it
#: as data would imply it could be written back, and it cannot be (changing it
#: means regenerating and reloading the plist on the host, which a networked
#: `/mcp/` caller has no business doing and no way to do).
_BRIEF_EMAIL_SCHEDULE = "19:00 daily, backstop 20:00 (launchd com.localfitness.briefmail)"


@tool(
    "get_brief_email_settings",
    "How the evening brief email is configured: whether it's enabled, who it "
    "goes to, whether a sending credential is present, and the schedule. Call "
    "before update_brief_email_settings so an edit patches what is actually "
    "there. Reports `password_configured` as a boolean only — the SMTP "
    "password is never returned by any tool. Changing the send TIME is not a "
    "setting: edit the launchd plist template and re-run "
    "./ops/install-launchd.sh briefmail.",
    {},
)
async def get_brief_email_settings(_args: dict) -> dict:
    from . import mailer

    with db.connect() as conn:
        enabled = config.brief_email_enabled(conn=conn)
        recipients = config.brief_email_to(conn=conn)
    smtp_user = os.environ.get("LOCAL_FITNESS_SMTP_USER", "").strip()
    configured = mailer.password_configured()
    return _text({
        "enabled": enabled,
        # Empty means "not configured"; mailer falls back to the sending
        # account, so report the address that would actually be used.
        "to": list(recipients) or ([smtp_user] if smtp_user else []),
        "to_is_explicit": bool(recipients),
        "password_configured": configured,
        "smtp_user": smtp_user or None,
        "smtp_host": os.environ.get("LOCAL_FITNESS_SMTP_HOST") or mailer.DEFAULT_SMTP_HOST,
        "schedule": _BRIEF_EMAIL_SCHEDULE,
        "can_send": bool(enabled and configured and smtp_user),
        "blocked_reason": (
            None if (enabled and configured and smtp_user)
            else "disabled via settings" if not enabled
            else "LOCAL_FITNESS_SMTP_PASSWORD is not set in <repo>/.env"
            if not configured
            else "LOCAL_FITNESS_SMTP_USER is not set in <repo>/.env"
        ),
    })


#: Loose sanity check, not RFC 5322. The point is to catch a transposed or
#: truncated address before it becomes a silent nightly bounce, not to
#: adjudicate exotic-but-legal addresses — a false rejection here would be a
#: worse failure than a permissive one.
_EMAIL_RE = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")


@tool(
    "update_brief_email_settings",
    "Configure the evening brief email conversationally — the agent-owned "
    "write path (there is no UI). `enabled: false` stops the nightly send "
    "without touching launchd; `to` replaces the recipient list (pass every "
    "address you want, not just the new one). Both optional; pass at least "
    "one. The SMTP password is NOT settable here — it is a secret and lives "
    "only in <repo>/.env. Takes effect on the next send; no restart.",
    {
        "type": "object",
        "properties": {
            "enabled": {
                "type": "boolean",
                "description": "False stops the nightly email; True resumes it.",
            },
            "to": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Full replacement recipient list, e.g. "
                               "[\"you@gmail.com\", \"you@work.com\"].",
            },
        },
        "required": [],
    },
)
async def update_brief_email_settings(args: dict) -> dict:
    from . import mailer

    errors: list[str] = []

    if unknown := sorted(set(args) - {"enabled", "to"}):
        errors.extend(f"unknown field '{k}'" for k in unknown)

    enabled = args.get("enabled")
    if "enabled" in args and not isinstance(enabled, bool):
        errors.append("enabled must be true or false")

    recipients: list[str] = []
    if "to" in args:
        raw = args["to"]
        # A bare string is the shape a model reaches for when there's one
        # address; accepting it beats failing on an unambiguous intent.
        if isinstance(raw, str):
            raw = [a for a in (p.strip() for p in raw.split(",")) if a]
        if not isinstance(raw, list) or not raw:
            errors.append("to must be a non-empty list of email addresses")
        else:
            for addr in raw:
                if not isinstance(addr, str) or not _EMAIL_RE.match(addr.strip()):
                    errors.append(f"not a valid email address: {addr!r}")
                else:
                    recipients.append(addr.strip())

    if errors:
        return _err("; ".join(errors), editable_fields=["enabled", "to"])
    if "enabled" not in args and not recipients:
        return _err("nothing to update — pass `enabled` and/or `to`")

    changed: list[str] = []
    if "enabled" in args:
        db.set_setting("brief_email_enabled", "true" if enabled else "false")
        changed.append("enabled")
    if recipients:
        db.set_setting("brief_email_to", ",".join(recipients))
        changed.append("to")

    with db.connect() as conn:
        now_enabled = config.brief_email_enabled(conn=conn)
        now_to = config.brief_email_to(conn=conn)
    return _text({
        "updated": True,
        "changed": changed,
        "enabled": now_enabled,
        "to": list(now_to),
        "schedule": _BRIEF_EMAIL_SCHEDULE,
        # Surfaced on every write so "I turned it on" can never be the last
        # word when the send would still be blocked by a missing credential.
        "password_configured": mailer.password_configured(),
    })


#: Same contract as `_BRIEF_EMAIL_SCHEDULE` above — prose, not a writable
#: field. 19:05 rather than 19:00 so the two evening jobs don't contend for the
#: same wake; nothing about the calendar sync depends on the email having run.
#: Since 0.53.0 this job RECONCILES rather than writes-once, because the plan
#: tools sync themselves — so it is a self-heal pass, not the only writer.
_PLAN_CALENDAR_SCHEDULE = (
    "reconciled on every plan edit, plus 19:05 daily with a 20:05 backstop "
    "(launchd com.localfitness.plancal)")


@tool(
    "get_plan_calendar_settings",
    "How the Google Calendar sync is configured: whether it's enabled, which "
    "calendar the training plan is written to, whether OAuth credentials are "
    "present, and the schedule. Call before update_plan_calendar_settings so "
    "an edit patches what is actually there. Reports "
    "`credentials_configured` as a boolean only — the OAuth client secret and "
    "refresh token are never returned by any tool. Changing the sync TIME is "
    "not a setting: edit the launchd plist template and re-run "
    "./ops/install-launchd.sh plancal.",
    {},
)
async def get_plan_calendar_settings(_args: dict) -> dict:
    from . import gcal

    with db.connect() as conn:
        enabled = config.plan_calendar_enabled(conn=conn)
        calendar_id = config.plan_calendar_id(conn=conn)
    configured = gcal.credentials_configured()
    return _text({
        "enabled": enabled,
        "calendar_id": calendar_id,
        "credentials_configured": configured,
        "schedule": _PLAN_CALENDAR_SCHEDULE,
        "creates_events_for": "every prescribed session on the active plan "
                              "from today through its last day, as all-day "
                              "events; a rest day creates nothing, and a day "
                              "the plan drops is DELETED from the calendar",
        "requires_active_plan": True,
        "can_write": bool(enabled and configured),
        "blocked_reason": (
            None if (enabled and configured)
            else "disabled via settings" if not enabled
            else "OAuth credentials are not set in <repo>/.env — run "
                 "`uv run fitness calendar-auth` (see docs/google-calendar.md)"
        ),
    })


@tool(
    "update_plan_calendar_settings",
    "Configure the Google Calendar sync conversationally — the agent-owned "
    "write path (there is no UI). `enabled: false` stops the sync without "
    "touching launchd; `calendar_id` picks which calendar to write to "
    "('primary' is the authenticated account's default). Both optional; pass "
    "at least one. OAuth credentials are NOT settable here — they are secrets "
    "and live only in <repo>/.env. Takes effect on the next run; no restart.",
    {
        "type": "object",
        "properties": {
            "enabled": {
                "type": "boolean",
                "description": "False stops the nightly calendar event; True resumes it.",
            },
            "calendar_id": {
                "type": "string",
                "description": "Google Calendar id — 'primary' (the default) or "
                               "a specific calendar's address.",
            },
        },
        "required": [],
    },
)
async def update_plan_calendar_settings(args: dict) -> dict:
    from . import gcal

    errors: list[str] = []

    if unknown := sorted(set(args) - {"enabled", "calendar_id"}):
        errors.extend(f"unknown field '{k}'" for k in unknown)

    enabled = args.get("enabled")
    if "enabled" in args and not isinstance(enabled, bool):
        errors.append("enabled must be true or false")

    calendar_id = None
    if "calendar_id" in args:
        raw = args["calendar_id"]
        if not isinstance(raw, str) or not raw.strip():
            errors.append("calendar_id must be a non-empty string")
        else:
            calendar_id = raw.strip()
            # 'primary' is the one legal non-address value; anything else has
            # to look like the email-shaped id Google actually issues, or the
            # nightly job fails with a 404 nobody sees until the event doesn't
            # appear.
            if calendar_id != "primary" and not _EMAIL_RE.match(calendar_id):
                errors.append(
                    f"calendar_id must be 'primary' or a calendar address, "
                    f"got {raw!r}")

    if errors:
        return _err("; ".join(errors), editable_fields=["enabled", "calendar_id"])
    if "enabled" not in args and calendar_id is None:
        return _err("nothing to update — pass `enabled` and/or `calendar_id`")

    changed: list[str] = []
    if "enabled" in args:
        db.set_setting("plan_calendar_enabled", "true" if enabled else "false")
        changed.append("enabled")
    if calendar_id is not None:
        db.set_setting("plan_calendar_id", calendar_id)
        changed.append("calendar_id")

    with db.connect() as conn:
        now_enabled = config.plan_calendar_enabled(conn=conn)
        now_calendar = config.plan_calendar_id(conn=conn)
    return _text({
        "updated": True,
        "changed": changed,
        "enabled": now_enabled,
        "calendar_id": now_calendar,
        "schedule": _PLAN_CALENDAR_SCHEDULE,
        # Surfaced on every write so "I turned it on" can never be the last
        # word when the write would still be blocked by a missing credential.
        "credentials_configured": gcal.credentials_configured(),
    })


@tool(
    "daily_snapshot",
    _DAILY_SNAPSHOT_DESCRIPTION,
    {},
)
async def daily_snapshot(_args: dict) -> dict:
    # Lazy import: status.py imports DAILY_NUMERIC_METRICS from this module, so
    # a top-level import here would be circular.
    from .status import assemble_status
    # settling_guard: this tool serves ad-hoc reads with no pull in front of
    # them — the one surface where today's rhr/sleep can be a stale snapshot.
    return _text(assemble_status(settling_guard=True))


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
    if msg := _validate_date(observed_on):
        return _err(msg)
    parsed_date = date.fromisoformat(observed_on)
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


# Default row cap on list_observations. Observations are the daily-logging
# surface (weight, RPE, soreness, mood), so an unbounded SELECT * would dump a
# year+ of rows into the reply once logging is at cadence. Mirrors
# query_workouts' default-50 pattern; run_sql stays the full-history escape hatch.
_LIST_OBSERVATIONS_DEFAULT_LIMIT = 100

_LIST_OBSERVATIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "days": {"type": "integer", "description": "Only observations from the last N days."},
        "obs_type": {"type": "string", "description": "Filter to one obs_type."},
        "limit": {
            "type": "integer",
            "description": f"Max rows, most recent first (default {_LIST_OBSERVATIONS_DEFAULT_LIMIT}).",
        },
    },
    "required": [],
}


@tool(
    "list_observations",
    "List logged observations, most recent first. Optional filters: days "
    f"lookback and obs_type. Capped at {_LIST_OBSERVATIONS_DEFAULT_LIMIT} rows "
    "by default (pass limit for more); when the cap is hit the payload carries "
    "\"truncated\": true — narrow with days/obs_type or raise limit.",
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
    # _validate_limit, like every other list surface (0.56.0). The bare
    # int() cast it replaces let `limit: "abc"` escape as a raw ValueError
    # and `limit: -1` reach SQLite as `LIMIT 0` — an empty page that then
    # reported `truncated: true` about rows it never fetched.
    limit = _validate_limit(args, default=_LIST_OBSERVATIONS_DEFAULT_LIMIT)
    if isinstance(limit, str):
        return _err(limit)
    with db.connect() as conn:
        # Fetch one past the cap so a full page is distinguishable from a
        # clipped larger set — same truncation-signal shape as run_sql.
        # Explicit columns, not SELECT * — the schema owns what a payload
        # carries, not whatever a future ALTER adds.
        rows = conn.execute(
            "SELECT observation_id, observed_on, created_at, obs_type, "
            f"value_num, value_text, activity_id FROM observations {where_sql} "
            "ORDER BY observed_on DESC, observation_id DESC LIMIT ?",
            (*params, limit + 1),
        ).fetchall()
    truncated = len(rows) > limit
    rows = rows[:limit]
    payload = {"observations": [dict(r) for r in rows], "count": len(rows)}
    if truncated:
        payload["truncated"] = True
    return _text(payload)


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
    if msg := _validate_date(workout_date):
        return _err(msg)
    parsed_date = date.fromisoformat(workout_date)
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
    wdate = date.fromisoformat(workout_date)
    lookback = max(baselines_mod.RECOMPUTE_LOOKBACK_DAYS, (date.today() - wdate).days + 1)
    # The activity row is ALREADY committed (db.connect() committed on block
    # exit). recompute() runs on a fresh connection; if it raises (transient
    # "database is locked", a bad stored date, ...) we must NOT propagate — a
    # bare raise reads as a tool failure and a blind retry would insert a SECOND
    # workout, double-counting load. Return a partial-success so the caller can
    # tell the row landed and skip the retry. to_thread, not a bare call:
    # recompute is seconds of DB work on a wide lookback and this is an async
    # handler — a sync call parks the whole event loop (sync_garmin_data
    # already does it right).
    try:
        await asyncio.to_thread(baselines_mod.recompute, lookback_days=lookback)
    except Exception as e:  # noqa: BLE001 — row is committed; never re-raise here
        return _text({
            "logged": True,
            "activity": _augment_workout(result),
            "recompute_failed": True,
            "warning": "workout saved but training-load recompute failed; "
                       "baselines may lag until the next successful sync "
                       "(the nightly job, or sync_garmin_data once new "
                       "Garmin data exists)",
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
    wdate = date.fromisoformat(workout_date)
    lookback = max(baselines_mod.RECOMPUTE_LOOKBACK_DAYS, (date.today() - wdate).days + 1)
    # The delete is ALREADY committed. recompute() runs on a fresh connection;
    # if it raises, don't propagate a bare exception that implies the delete
    # failed — the row is gone. Return a partial-success instead. to_thread
    # for the same event-loop reason as log_manual_workout above.
    try:
        await asyncio.to_thread(baselines_mod.recompute, lookback_days=lookback)
    except Exception as e:  # noqa: BLE001 — delete is committed; never re-raise
        return _text({
            "deleted": True,
            "activity_id": aid,
            "recompute_failed": True,
            "warning": "workout deleted but training-load recompute failed; "
                       "baselines may lag until the next successful sync "
                       "(the nightly job, or sync_garmin_data once new "
                       "Garmin data exists)",
            "error_detail": str(e),
        })

    return _text({
        "deleted": True,
        "activity_id": aid,
        "note": f"training load recomputed (lookback_days={lookback})",
    })


# --- Training plans (the agent owns the entire plan lifecycle) -------------
#
# The agent is the sole plan write path: propose/revise create and edit DRAFT
# structure (`status` is never a caller input), `update_plan_workout`
# re-prescribes a single day on the ACTIVE plan (prescription columns only —
# it can move a long run or swap a session, but it cannot re-key, re-status,
# or restructure the plan), and commit_training_plan/discard_training_plan_draft/
# abandon_active_plan cover the rest of the lifecycle (activate a draft, drop a
# draft, or abandon the active plan outright). See plans.py for the enforced
# write boundary.

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
            "description": "Full schedule: each {date, week_index, type, target_distance_m?, target_pace_sec_per_km?, target_duration_sec?, target_hr_max?, description, seq?}. Set target_hr_max (bpm) on any day with a heart-rate ceiling — the report card grades against it, and a cap written only into the description is invisible to the grader.",
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
    "generated. Ground it first: call training_load_status, daily_snapshot, "
    "and query_workouts to read the athlete's real fitness before proposing. "
    "Archives any prior draft. Does NOT activate the plan — call "
    "commit_training_plan to activate it, or discard_training_plan_draft to "
    "drop it.",
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
    "plans). Cannot change a plan's status — call commit_training_plan to "
    "activate it, or discard_training_plan_draft to drop it.",
    _REVISE_PLAN_SCHEMA,
)
async def revise_training_plan(args: dict) -> dict:
    plan_id = args.get("plan_id")
    if not isinstance(plan_id, int):
        return _err("plan_id (int) is required")
    # status is deliberately NOT among the readable fields — it can never be set here.
    fields = {k: args[k] for k in _EDITABLE_TOOL_FIELDS if k in args}
    workouts = args.get("workouts")

    # A goal_type change without an explicit goal_distance_m re-derives the
    # distance the same way propose does — otherwise revise(goal_type="half")
    # on a 10k draft leaves goal_distance_m=10000 and the Riegel projection
    # predicts a 10k finish labeled as a half (round-2 facet review, MED-4).
    if "goal_type" in fields and "goal_distance_m" not in fields:
        derived = plans.GOAL_DISTANCE_M.get(fields["goal_type"])
        if derived is not None:
            fields["goal_distance_m"] = derived

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


_UPDATE_WORKOUT_SCHEMA = {
    "type": "object",
    "properties": {
        "date": {"type": "string", "description": "ISO YYYY-MM-DD of the day to re-prescribe in the ACTIVE plan"},
        "type": {"type": "string", "enum": ["easy", "long", "tempo", "interval", "rest", "race", "cross"]},
        "distance_mi": {"type": "number", "description": "target distance in miles (omit for rest / by-feel)"},
        "pace_min_per_mi": {
            "type": ["string", "number"],
            "description": 'target pace per mile as "M:SS" (preferred). A bare '
                           "number is DECIMAL minutes — 9.65 is 9:39/mi, 9.39 "
                           "is 9:23/mi; never copy a display string as a number.",
        },
        "duration_min": {"type": "number", "description": "target duration in minutes — the graded field for tempo/interval sessions"},
        "hr_max": {
            "type": "number",
            "description": "prescribed heart-rate CEILING in bpm. The grader "
                           "reads THIS field only — a cap stated just in the "
                           "prose description is invisible to it.",
        },
        "description": {"type": "string", "description": "prose prescription for the day"},
        "seq": {"type": "integer", "description": "intra-day session on a double day: 1 = first/AM (default), 2 = second/PM"},
    },
    "required": ["date"],
}


def _prescription_fields(args: dict) -> tuple[dict | None, str | None]:
    """Build the DB-column fields dict for one day's prescription.

    Returns ``(fields, None)`` or ``(None, error_message)``. Shared by
    ``update_plan_workout`` and ``update_plan_workouts`` so the two cannot
    drift — every unit conversion and every sanity bound is defined once. The
    rest-day clear is deliberately NOT here: it lives at the write boundary in
    ``plans.apply_rest_semantics`` so it protects any caller, not just these
    two.
    """
    fields: dict = {}
    wtype = args.get("type")
    if wtype is not None:
        if wtype not in plans.WORKOUT_TYPES:
            return None, f"unknown type '{wtype}' (allowed: {sorted(plans.WORKOUT_TYPES)})"
        fields["type"] = wtype
    if args.get("distance_mi") is not None:
        fields["target_distance_m"] = units.from_miles(float(args["distance_mi"]))
    if args.get("pace_min_per_mi") is not None:
        # "M:SS" string (preferred, round-trips the app's own display format)
        # or decimal minutes. The parse exists because a model copying the
        # display string "9:39" as the float 9.39 silently prescribed
        # 9:23/mi — a 16 s/mi error invisible in the echo.
        sec_per_mi = units.parse_pace_min_per_mi(args["pace_min_per_mi"])
        if sec_per_mi is None:
            return None, ('pace_min_per_mi must be "M:SS" (e.g. "9:39") or decimal '
                          "minutes (9.65 = 9:39/mi)")
        # Sanity bound: 3:00–30:00/mi. Catches transposed args and
        # unit-confused numbers before they land on the active plan.
        if not (180.0 <= sec_per_mi <= 1800.0):
            return None, (
                f"pace_min_per_mi of {units.format_pace_min_per_mi(units.pace_sec_per_mi_to_sec_per_km(sec_per_mi))}/mi "
                "is outside the plausible 3:00–30:00/mi range")
        fields["target_pace_sec_per_km"] = units.pace_sec_per_mi_to_sec_per_km(sec_per_mi)
    if args.get("duration_min") is not None:
        fields["target_duration_sec"] = round(float(args["duration_min"]) * 60)
    if args.get("hr_max") is not None:
        hr_max = float(args["hr_max"])
        # Sanity bound: a prescribed ceiling outside 90-210 bpm is a transposed
        # or unit-confused argument, not a coaching decision. Same discipline as
        # the pace bound above, and it matters more here because the report card
        # grades against this number directly.
        # Bounds live in plans.py so the CREATE path (validate_plan_input)
        # and this EDIT path share one definition — they disagreed until
        # 0.47.0, and hr_max=14 was rejected here but accepted on a proposal.
        if not (plans.MIN_PRESCRIBED_HR <= hr_max <= plans.MAX_PRESCRIBED_HR):
            return None, (
                f"hr_max of {hr_max:.0f} bpm is outside the plausible "
                f"{plans.MIN_PRESCRIBED_HR:.0f}-{plans.MAX_PRESCRIBED_HR:.0f} bpm range")
        fields["target_hr_max"] = hr_max
    if args.get("description") is not None:
        fields["description"] = args["description"]
    if not fields:
        return None, ("nothing to update — pass type / distance_mi / "
                      "pace_min_per_mi / duration_min / hr_max / description")
    return fields, None


def _validate_seq(seq) -> tuple[int | None, str | None]:
    """``seq`` defaults to 1 (the first/AM session) and must be a positive int.
    ``bool`` is excluded explicitly — it is an ``int`` subclass, so ``True``
    would otherwise pass as seq 1."""
    if seq is None:
        return 1, None
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        return None, "seq must be a positive integer (1 = first/AM session)"
    return seq, None


@tool(
    "update_plan_workout",
    "Re-prescribe ONE day on the ACTIVE training plan: date plus any of "
    "type/distance_mi/pace_min_per_mi/duration_min/hr_max/description. "
    "type='rest' clears distance/pace/duration/hr_max and defaults the "
    "description to 'Rest day'. Edits the prescription only — it cannot "
    "re-key or restructure the plan. For 2+ days use update_plan_workouts "
    "(one atomic call), never a loop of this.",
    _UPDATE_WORKOUT_SCHEMA,
)
async def update_plan_workout(args: dict) -> dict:
    date_str = args.get("date")
    if msg := _validate_date(date_str):
        return _err(msg)

    fields, err = _prescription_fields(args)
    if err is not None:
        return _err(err)

    seq, err = _validate_seq(args.get("seq"))
    if err is not None:
        return _err(err)

    try:
        row = plans.update_active_workout(date_str, fields, seq=seq)
    except plans.NoActivePlanError:
        return _err("no active training plan")
    except ValueError as e:
        return _err(str(e))

    # Echo the whole prescription that was written, so the model can confirm
    # the change from the tool result without a follow-up read. duration_min is
    # the graded field for tempo/interval days (per this tool's own
    # description), so target_duration_sec MUST be in the echo — via the
    # duration_seconds key _augment_workout formats into duration_formatted,
    # mirroring the distance_meters/avg_pace_sec_per_km remaps beside it. seq
    # tells the user which session of a double day was edited.
    return _text(await _calendar_after_plan_write(_augment_workout({
        "date": row["date"], "type": row["type"], "seq": row["seq"],
        "distance_meters": row["target_distance_m"],
        "avg_pace_sec_per_km": row["target_pace_sec_per_km"],
        "duration_seconds": row["target_duration_sec"],
        "target_hr_max": row["target_hr_max"],
        "description": row["description"],
    })))


async def _calendar_after_plan_write(payload: dict) -> dict:
    """Push the plan's new state to Google Calendar and note it in ``payload``.

    Every tool that mutates an ACTIVE plan calls this. Three properties, and
    all three are the point:

    * **It never raises.** The plan write has already committed and is the
      source of truth; the calendar is a projection. Letting a Google outage
      raise here would turn a successful edit into a failed tool call, and the
      model would reasonably retry the edit — which is how a transport problem
      becomes a data problem. ``calendar_sync`` swallows and reports.
    * **It runs in a worker thread.** The sync is blocking HTTPS, and the
      repo's rule for blocking I/O inside a handler is ``asyncio.to_thread``
      (see ``run_sql``, ``sync_garmin_data``).
    * **The key is OMITTED, not null, when nothing was attempted.** A clone
      with no Google credentials should not get a line of calendar noise
      appended to every plan edit it ever makes.
    """
    from . import calendar_sync

    result = await asyncio.to_thread(calendar_sync.sync_after_plan_write)
    if result is not None:
        payload["calendar"] = result
    return payload


def _echo_workout(row: dict) -> dict:
    """The written prescription, in the shape the single-day tool echoes."""
    return _augment_workout({
        "date": row["date"], "type": row["type"], "seq": row["seq"],
        "distance_meters": row["target_distance_m"],
        "avg_pace_sec_per_km": row["target_pace_sec_per_km"],
        "duration_seconds": row["target_duration_sec"],
        "target_hr_max": row["target_hr_max"],
        "description": row["description"],
    })


# Item properties are LIFTED from the single-day schema rather than restated,
# so a new prescription field or a reworded unit note can never describe one
# tool and not the other.
_UPDATE_WORKOUTS_SCHEMA = {
    "type": "object",
    "properties": {
        "updates": {
            "type": "array",
            "minItems": 1,
            "maxItems": plans.MAX_BATCH_UPDATES,
            "description": (
                "One entry per day to re-prescribe. Each needs `date` plus the "
                "fields that change. Applied atomically: if any entry is "
                "invalid or names a day not on the plan, NOTHING is written."
            ),
            "items": {
                "type": "object",
                "properties": dict(_UPDATE_WORKOUT_SCHEMA["properties"]),
                "required": ["date"],
            },
        },
    },
    "required": ["updates"],
}


@tool(
    "update_plan_workouts",
    "Re-prescribe MANY days on the ACTIVE plan in ONE atomic all-or-nothing "
    "call (a bad entry writes nothing) — the right tool for reshaping a week "
    "or block; same fields per entry as update_plan_workout. Re-prescribes "
    "EXISTING days only: a swap is two entries (rest the old day, prescribe "
    "the new one) and the new day must already be on the plan. For a whole "
    f"new structure use propose_training_plan. Max {plans.MAX_BATCH_UPDATES} "
    "entries.",
    _UPDATE_WORKOUTS_SCHEMA,
)
async def update_plan_workouts(args: dict) -> dict:
    raw = args.get("updates")
    if not isinstance(raw, list) or not raw:
        return _err("updates must be a non-empty array of {date, ...} objects")

    prepared: list[tuple[str, int, dict]] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            return _err(f"update {i}: each entry must be an object")
        date_str = entry.get("date")
        if msg := _validate_date(date_str):
            return _err(f"update {i}: {msg}")
        fields, err = _prescription_fields(entry)
        if err is not None:
            return _err(f"update {i} ({date_str}): {err}")
        seq, err = _validate_seq(entry.get("seq"))
        if err is not None:
            return _err(f"update {i} ({date_str}): {err}")
        prepared.append((date_str, seq, fields))

    try:
        rows = plans.update_active_workouts(prepared)
    except plans.NoActivePlanError:
        return _err("no active training plan")
    except ValueError as e:
        return _err(str(e))

    # Echo every written row, so a 20-day restructure can be confirmed from
    # this one result without a follow-up read.
    return _text(await _calendar_after_plan_write(
        {"updated": len(rows), "workouts": [_echo_workout(r) for r in rows]}))


@tool(
    "commit_training_plan",
    "Activate a DRAFT training plan, archiving any prior active plan. Only "
    "works on a draft (refuses to touch an already-active or archived plan). "
    "Call after propose_training_plan/revise_training_plan once the athlete "
    "has agreed to the plan.",
    {"type": "object", "properties": {"plan_id": {"type": "integer"}}, "required": ["plan_id"]},
)
async def commit_training_plan(args: dict) -> dict:
    from . import calendar_sync

    plan_id = args.get("plan_id")
    if not isinstance(plan_id, int):
        return _err("plan_id (int) is required")

    # Read the outgoing plan BEFORE the commit archives it. Its calendar events
    # can't be overwritten by the new plan's — `event_id` keys on plan_id, so
    # the new plan lands on entirely different ids — and two plans' worth of
    # sessions sitting on one calendar is worse than none. Gated on the sync
    # being configured at all, so an unconfigured clone pays no extra read.
    superseded = None
    if calendar_sync.blocked_reason() is None:
        with db.connect() as conn:
            prior = plans.get_active_plan(conn=conn)
        superseded = prior["plan_id"] if prior else None

    try:
        plans.commit_plan(plan_id, now=datetime.now().isoformat(timespec="seconds"))
    except (plans.PlanNotFoundError, plans.NotDraftError) as e:
        return _err(str(e))

    payload = {"plan_id": plan_id, "status": "active"}
    calendar = await asyncio.to_thread(calendar_sync.sync_after_commit, superseded)
    if calendar is not None:
        payload["calendar"] = calendar
    return _text(payload)


@tool(
    "discard_training_plan_draft",
    "Discard (archive) the DRAFT training plan without activating it. Only "
    "works on a draft — refuses to touch the active plan (call "
    "commit_training_plan on a new draft to replace it instead). Only call "
    "when the user explicitly asks to drop or reject a draft.",
    {"type": "object", "properties": {"plan_id": {"type": "integer"}}, "required": ["plan_id"]},
)
async def discard_training_plan_draft(args: dict) -> dict:
    plan_id = args.get("plan_id")
    if not isinstance(plan_id, int):
        return _err("plan_id (int) is required")
    try:
        plans.discard_draft(plan_id)
    except (plans.PlanNotFoundError, plans.NotDraftError) as e:
        return _err(str(e))
    return _text({"plan_id": plan_id, "status": "archived"})


@tool(
    "abandon_active_plan",
    "Archive the currently active training plan with nothing queued to "
    "replace it. Only call when the user explicitly asks to stop "
    "following their plan entirely — never proactively, and never as "
    "part of activating a new plan (commit_training_plan already "
    "archives the prior active plan atomically as part of that swap). "
    "No undo tool exists for this.",
    {"type": "object", "properties": {}},
)
async def abandon_active_plan(_args: dict) -> dict:
    from . import calendar_sync

    try:
        plan_id = plans.abandon_active_plan()
    except plans.NoActivePlanError as e:
        return _err(str(e))

    # The one path that only DELETES. A plan nobody follows must not keep
    # prescribing work from the calendar, and there is no new plan to reconcile
    # against — so this is `remove`, not `sync`. Past events stay: they record
    # what was prescribed at the time.
    payload = {"plan_id": plan_id, "status": "archived"}
    calendar = await asyncio.to_thread(calendar_sync.remove_after_abandon, plan_id)
    if calendar is not None:
        payload["calendar"] = calendar
    return _text(payload)


def weekly_rollup(workouts: list[dict], target_date: str) -> dict:
    """Trailing-7-day (ending on ``target_date``) planned/actual mileage
    rollup, shared by ``_build_plan_section`` (the PDF) and
    ``get_training_plan_progress`` (``this_week``).

    Pure, I/O-free: ``workouts`` is an already-graded workout list (each
    entry needs ``date``/``verdict``/``type``/``target_distance_m``/
    ``actual_distance_m``) — no DB connection, directly import-testable.
    ``target_date`` is an ISO date STRING (the repo's window-comparison
    convention: callers already window via ISO-string comparison, and
    string comparison of ISO dates is order-correct) — the comparison
    stays string-based; computing ``window_start`` still parses the date
    once.

    Owns the whole aggregation contract, so the per-day rows and the
    three totals agree by construction:
    - the trailing-7-day window ending on ``target_date`` (deliberately
      different from ``plans.weekly_mileage``'s per-``week_index``
      rollup — this matches the PDF section's existing definition);
    - the per-day ``units.to_miles`` conversion (2 dp);
    - the verdict-conditional suppression of ``actual_mi`` for
      ``pending``/``compliant`` days — a run that hasn't been graded yet,
      or a rest day, doesn't count into "actual mileage" (the existing
      PDF rule, promoted to the shared definition);
    - the totals are summed from that same ``days`` list, THEN rounded to
      1 dp — summing raw meters first and converting once can differ by
      0.1 from the per-day-rounded sum.

    ``days`` is REVERSE-CHRONOLOGICAL (most recent first) — load-bearing
    downstream: ``plan_coach.build_prompt`` labels the list "most recent
    first" and ``fallback_coaching_line`` picks the first non-pending
    entry as the latest graded day. Empty window -> ``days: []`` with
    zero totals; the empty -> ``None`` short-circuit for callers that
    want that behavior (``_build_plan_section``) lives in the caller, not
    here.
    """
    window_start = (date.fromisoformat(target_date) - timedelta(days=6)).isoformat()
    week_workouts = sorted(
        (w for w in workouts if window_start <= w["date"] <= target_date),
        key=lambda w: w["date"],
        reverse=True,
    )
    days: list[dict] = []
    for w in week_workouts:
        target_m = w.get("target_distance_m")
        planned_mi = units.to_miles(target_m) if target_m is not None else None
        if w["verdict"] in ("pending", "compliant"):
            actual_mi = run_mi = walk_mi = None
        else:
            actual_m = w.get("actual_distance_m")
            actual_mi = units.to_miles(actual_m) if actual_m is not None else None
            # Split by MEASURED locomotion (plans.build_plan_detail), not by
            # Garmin's label. Before this, a 3.23 mi walking-pad session filed
            # as `treadmill_running` counted into run mileage, so an interval
            # day whose run was 5.95 mi reported 9.2 mi actual.
            run_m, walk_m = w.get("actual_run_distance_m"), w.get("actual_walk_distance_m")
            run_mi = units.to_miles(run_m) if run_m is not None else actual_mi
            walk_mi = units.to_miles(walk_m) if walk_m is not None else 0.0
        days.append({
            "date": w["date"],
            "verdict": w["verdict"],
            "type": w["type"],
            "planned_mi": planned_mi,
            "actual_mi": actual_mi,
            "run_mi": run_mi,
            "walk_mi": walk_mi,
        })
    week_planned_mi = round(sum(d["planned_mi"] or 0 for d in days), 1)
    week_actual_mi = round(sum(d["actual_mi"] or 0 for d in days), 1)
    slips = sum(1 for d in days if d["verdict"] in ("partial", "missed"))
    return {
        "week_planned_mi": week_planned_mi,
        "week_actual_mi": week_actual_mi,
        # Run and walk sum back to week_actual_mi by construction, so the
        # strip's "22.3 / 29.5" and its "7.0 walk" always reconcile.
        "week_run_mi": round(sum(d["run_mi"] or 0 for d in days), 1),
        "week_walk_mi": round(sum(d["walk_mi"] or 0 for d in days), 1),
        "slips": slips,
        "days": days,
    }


def _augment_plan_workout(w: dict) -> dict:
    """Fix C (2026-07-10 doc): attach mile/pace convenience fields to a plan
    workout dict, reproducing ``_augment_workout``'s exact
    ``display_units()`` gating split — distance gated behind miles mode,
    pace unconditional.

    Symmetric field set: ``target_distance_mi``/``actual_distance_mi``
    (miles-mode only, omitted entirely — not ``None`` — in km mode) and
    ``target_pace_min_per_mi``/``actual_pace_min_per_mi`` (always, when the
    underlying raw value is present), mirroring the raw ``target_*``/
    ``actual_*`` pairs already on the payload. Used by
    ``get_training_plan_progress`` (every workout entry) and
    ``get_training_plan_status`` (``today``/``last_graded``, individually
    None-guarded by the caller — that path has no ``actual_*`` keys at all,
    so ``.get`` returning ``None`` naturally omits the actual-mile/pace
    fields there). NOT used by ``_build_plan_section``, which keeps its own
    inline conversion + verdict-suppression logic (see ``weekly_rollup``).
    """
    if units.display_units() == "miles":
        target_mi = units.to_miles(w.get("target_distance_m"))
        if target_mi is not None:
            w["target_distance_mi"] = target_mi
        actual_mi = units.to_miles(w.get("actual_distance_m"))
        if actual_mi is not None:
            w["actual_distance_mi"] = actual_mi
    target_pace = units.format_pace_min_per_mi(w.get("target_pace_sec_per_km"))
    if target_pace is not None:
        w["target_pace_min_per_mi"] = target_pace
    actual_pace = units.format_pace_min_per_mi(w.get("actual_pace_sec_per_km"))
    if actual_pace is not None:
        w["actual_pace_min_per_mi"] = actual_pace
    return w


#: Raw plan-workout fields whose display twin (attached by
#: _augment_plan_workout / the duration formatter) replaces them in
#: get_training_plan_progress's compact rows. Mirrors
#: workout_rows._RAW_DISPLAY_PAIRS for activity rows.
_PLAN_RAW_DISPLAY_PAIRS = (
    ("target_distance_m", "target_distance_mi"),
    ("actual_distance_m", "actual_distance_mi"),
    ("target_pace_sec_per_km", "target_pace_min_per_mi"),
    ("actual_pace_sec_per_km", "actual_pace_min_per_mi"),
    ("target_duration_sec", "target_duration_formatted"),
)


@tool(
    "get_training_plan_status",
    "Status of the ACTIVE training plan: goal, days to race, the most recent "
    "graded day's prescription + verdict, today's prescribed session, and "
    "overall adherence. Returns {active: false} when there is no active plan. "
    "Call this first in a brief to decide whether to fold the plan in. Slim "
    "by design — for week rollups, goal gap, or projected finish, use "
    "get_training_plan_progress instead. "
    "ALSO reports `pending_draft` (null when there is none): a proposed plan "
    "that has never been committed, so it governs nothing. It is a loose end "
    "— surface it, then either commit_training_plan or "
    "discard_training_plan_draft. Proposing another plan silently archives "
    "it. Use get_training_plan_draft to read the whole thing.",
    {},
)
async def get_training_plan_status(_args: dict) -> dict:
    with db.connect() as conn:
        active = plans.get_active_plan(conn=conn)
        # Read on the connection we already hold — no extra open. Resolved
        # BEFORE the no-active-plan early return on purpose: "no active plan
        # but a draft waiting to be committed" is precisely the state that
        # needs reporting, and a bare {active: false} hides it.
        pending_draft = plans.draft_summary(plans.get_draft_plan(conn=conn))
        if active is None:
            return _text({"active": False, "pending_draft": pending_draft})
        frontier = db.last_known_daily_date(conn=conn)
        today = date.today().isoformat()
        dates = [w["date"] for w in active["workouts"]] or [today]
        start = min(dates)
        end = max([today, *dates] + ([frontier] if frontier else []))
        activities_by_date = plans.load_activities_by_date(start, end, conn=conn)
        cfg = plans.resolve_grading_config(conn=conn)
    status = plans.build_plan_status(active, frontier, activities_by_date, today, cfg)
    status["pending_draft"] = pending_draft

    # 2d: pure formatting of data already in hand. (plans.py does import
    # agent.units since 0.35.0 — a pure stdlib leaf, no cycle — so display
    # formatting there is fine too; projection_basis is the precedent.) No
    # goal_gap/this_week/predicted_finish_formatted here, this tool has no
    # Riegel projection.
    status["target_time_formatted"] = units.format_duration(status.get("target_time_seconds"))
    for key in ("today", "last_graded"):
        w = status.get(key)
        if w is not None:
            _augment_plan_workout(w)
            duration_formatted = units.format_duration(w.get("target_duration_sec"))
            if duration_formatted is not None:
                w["target_duration_formatted"] = duration_formatted
    return _text(status)


_PROGRESS_SCHEMA = {
    "type": "object",
    "properties": {
        "full": {
            "type": "boolean",
            "description": (
                "Return the complete workout list for the whole plan instead of "
                "the default rolling window (14 days back from the data "
                "frontier, 7 days forward from today). Default false."
            ),
        },
    },
    "required": [],
}


@tool(
    "get_training_plan_progress",
    "Day-by-day progress of the ACTIVE training plan: every prescribed "
    "workout with its graded verdict (done | partial | missed | compliant | "
    "pending), plus goal, days-to-race, adherence %, projected finish, goal "
    "gap, and this week's mileage. this_week.week_actual_mi is total ON-FOOT "
    "miles (run + walk — easy days count prescribed walking by design); "
    "week_run_mi + week_walk_mi split it, and week_run_mi matches the brief "
    "PDF's plan-strip headline. The `workouts` list is "
    "windowed by default (14 days back from the data frontier, 7 days "
    "forward from today — today is always in-window even under a stale "
    "frontier) — pass full=true for the complete list across the whole plan "
    "(e.g. 'show my plan through today' on a plan older than 2 weeks). "
    "Returns {active: false} when there is no active plan. For just today's "
    "prescribed session, use get_training_plan_status (a slim one-day "
    "summary) instead — never query the DB by hand for this.",
    _PROGRESS_SCHEMA,
)
async def get_training_plan_progress(args: dict) -> dict:
    with db.connect() as conn:
        active = plans.get_active_plan(conn=conn)
        if active is None:  # build_plan_detail has no None guard — guard here first.
            return _text({"active": False})
        frontier = db.last_known_daily_date(conn=conn)
        today = date.today().isoformat()
        # Mirror get_training_plan_status's frontier-INCLUSIVE end (parity: both plan
        # tools compute identical grading windows), not the web tab's exclusive form.
        dates = [w["date"] for w in active["workouts"]] or [today]
        start = min(dates)
        end = max([today, *dates] + ([frontier] if frontier else []))
        activities_by_date = plans.load_activities_by_date(start, end, conn=conn)
        cutoff = (date.today() - timedelta(
            days=config.riegel_lookback_days(conn=conn))).isoformat()
        # The goal distance is a PREFERENCE for the basis, not a filter: it
        # asks for an effort at least a quarter of race distance so the
        # projection isn't a 10x reach, and falls back to any qualifying run
        # when nothing that long exists (see best_recent_effort).
        best_effort = plans.best_recent_effort(
            cutoff, conn=conn, goal_distance_m=active.get("goal_distance_m"))
        cfg = plans.resolve_grading_config(conn=conn)
    detail = plans.build_plan_detail(active, frontier, activities_by_date, best_effort, cfg)

    # days_to_race is produced only by build_plan_status, not build_plan_detail —
    # compute it here. Read via .get (absent key -> None, not KeyError) and parse
    # defensively (NULL/unparseable -> None), matching build_plan_status's guard.
    race = plans._parse_iso(active.get("race_date"))
    today_d = plans._parse_iso(today)
    days_to_race = (race - today_d).days if race and today_d else None

    # Deliberate projection: keep the fields an agent needs to answer a
    # plan-progress question; drop identifiers / internal rollups (plan_id,
    # status, ability_snapshot, weekly_mileage, …) that build_plan_detail
    # spreads. Compaction (0.56.0): this tool measured as 24% of ALL chars
    # returned across recorded sessions (median ~11 KB/call) — the single
    # worst context hog on the surface. Two pure cuts, applied before
    # windowing so full=true gets them too: (1) None-valued keys are omitted
    # AT BUILD TIME (a pending day shipped 4+ nulls per row; an absent key
    # reads the same as null to the model — and the single-pass build is
    # perf-gate-load-bearing: a build-then-strip second dict pass measured
    # 15.4% over the CI benchmark floor), and (2) in miles mode a raw field
    # is popped once its display twin landed (same rule as
    # workout_rows.display_workout — the twin is gated, so a paceless or
    # distance-less row keeps its raw column). Rollups untouched.
    miles_mode = units.display_units() == "miles"
    workouts_full = []
    for w in detail["workouts"]:
        row = {
            k: v for k, v in (
                ("date", w.get("date")),
                ("seq", w.get("seq", 1)),
                ("week_index", w.get("week_index")),
                ("type", w.get("type")),
                ("target_distance_m", w.get("target_distance_m")),
                ("target_pace_sec_per_km", w.get("target_pace_sec_per_km")),
                ("target_duration_sec", w.get("target_duration_sec")),
                ("target_hr_max", w.get("target_hr_max")),
                ("description", w.get("description")),
                ("verdict", w.get("verdict")),
                ("actual_distance_m", w.get("actual_distance_m")),
                ("actual_pace_sec_per_km", w.get("actual_pace_sec_per_km")),
                ("actual_activity_types", w.get("actual_activity_types")),
            ) if v is not None
        }
        # 2d/2e: per-workout mile/pace convenience fields (Fix C) + a
        # formatted target duration — pure computation over rows already
        # fetched. _augment_plan_workout only ADDS non-None fields, so the
        # no-nulls invariant established above survives it.
        _augment_plan_workout(row)
        duration_formatted = units.format_duration(row.get("target_duration_sec"))
        if duration_formatted is not None:
            row["target_duration_formatted"] = duration_formatted
        if miles_mode:
            for raw, display in _PLAN_RAW_DISPLAY_PAIRS:
                if display in row:
                    row.pop(raw, None)
        workouts_full.append(row)

    full = bool(args.get("full", False))
    if full:
        workouts = workouts_full
    else:
        # 2c: [anchor_back - 14d, anchor_fwd + 7d]. anchor_back anchors to the
        # data frontier (keeps graded history in view); anchor_fwd's max(...,
        # today) guarantees today (and today's prescribed workout) stays
        # in-window even when the frontier is stale (>7d behind, after a sync
        # gap) — the `else today` fallbacks keep the window defined on a
        # fresh DB where frontier is None. No clamping to plan bounds.
        anchor_back = frontier if frontier is not None else today
        anchor_fwd = max(frontier or today, today)
        window_start = (date.fromisoformat(anchor_back) - timedelta(days=14)).isoformat()
        window_end = (date.fromisoformat(anchor_fwd) + timedelta(days=7)).isoformat()
        workouts = [w for w in workouts_full if window_start <= w["date"] <= window_end]

    # Rollups are computed from the FULL graded workout list (detail /
    # detail["workouts"]), never the 2c-windowed projection — adherence_pct/
    # days_to_race/goal_gap stay whole-plan, this_week is its own
    # trailing-7-days window (never 2c's), so both are identical whether
    # full is true or false.
    predicted_finish_seconds = detail.get("predicted_finish_seconds")
    target_time_seconds = detail.get("target_time_seconds")
    goal_gap = plans.goal_gap(predicted_finish_seconds, target_time_seconds)
    rollup = weekly_rollup(detail["workouts"], today)
    # week_actual_mi is total ON-FOOT miles (run + walk) — easy/recovery days
    # count prescribed walking by design (CLAUDE.md). The brief PDF's plan strip
    # headlines RUN miles instead, so expose the run/walk split here (already on
    # the rollup) so the two surfaces reconcile: week_run_mi + week_walk_mi ==
    # week_actual_mi, and week_run_mi is the PDF strip's headline number.
    this_week = {
        "week_planned_mi": rollup["week_planned_mi"],
        "week_actual_mi": rollup["week_actual_mi"],
        "week_run_mi": rollup["week_run_mi"],
        "week_walk_mi": rollup["week_walk_mi"],
        "slips": rollup["slips"],
    }

    return _text({
        "active": True,
        "goal_type": detail.get("goal_type"),
        "race_date": detail.get("race_date"),
        "target_time_seconds": target_time_seconds,
        "target_time_formatted": units.format_duration(target_time_seconds),
        "days_to_race": days_to_race,
        "adherence_pct": detail.get("adherence_pct"),
        "sessions_adherence_pct": detail.get("sessions_adherence_pct"),
        "rest_days_counted": detail.get("rest_days_counted"),
        "predicted_finish_seconds": predicted_finish_seconds,
        "predicted_finish_formatted": units.format_duration(predicted_finish_seconds),
        # Spread, not two .get()s: build_plan_detail OMITS both keys when there
        # is no basis (never None-values them), and that absence has to survive
        # this projection — a `"projection_basis": null` beside a real
        # predicted time reads as a measurement with the receipt lost.
        **{k: detail[k] for k in ("projection_basis", "projection_confidence")
           if k in detail},
        "goal_gap": goal_gap,
        "this_week": this_week,
        "workouts": workouts,
    })


@tool(
    "get_training_plan_draft",
    "The DRAFT training plan awaiting a decision, if one exists — created by "
    "propose_training_plan/revise_training_plan and otherwise invisible: "
    "get_training_plan_status/get_training_plan_progress report the ACTIVE "
    "plan only, never a draft. Returns {draft: false} when there is no "
    "draft. Once you have the plan_id from here, hand it to "
    "commit_training_plan to activate the draft or discard_training_plan_draft "
    "to drop it.",
    {},
)
async def get_training_plan_draft(_args: dict) -> dict:
    draft = plans.get_draft_plan()
    if draft is None:
        return _text({"draft": False})

    workouts = [plans._slim_workout(w) for w in draft["workouts"]]
    for w in workouts:
        _augment_plan_workout(w)
        duration_formatted = units.format_duration(w.get("target_duration_sec"))
        if duration_formatted is not None:
            w["target_duration_formatted"] = duration_formatted

    race = plans._parse_iso(draft.get("race_date"))
    today_d = plans._parse_iso(date.today().isoformat())
    days_to_race = (race - today_d).days if race and today_d else None

    return _text({
        "draft": True,
        "plan_id": draft["plan_id"],
        "title": draft.get("title"),
        "goal_type": draft.get("goal_type"),
        "race_date": draft.get("race_date"),
        "target_time_seconds": draft.get("target_time_seconds"),
        "target_time_formatted": units.format_duration(draft.get("target_time_seconds")),
        "days_to_race": days_to_race,
        "created_at": draft.get("created_at"),
        "workout_count": len(workouts),
        "workouts": workouts,
    })


def _save_brief_input_schema() -> dict:
    """Advertise the real Brief JSON Schema as the tool's input contract, not an
    opaque ``{"brief": dict}``.

    Derived from the pydantic ``Brief`` model so it can never drift from what the
    server validates. Two adjustments make the advertised contract match what a
    caller must actually supply:

    * ``$defs`` are hoisted to the top level so the nested ``$ref``s (Takeaway,
      TakeawayMetric, and the tone/metric enums) resolve — a client reading the
      schema sees the exact takeaway shape and enum values instead of grepping
      ``schemas.py`` for them (which the agent had to do on 2026-07-22).
    * ``brief.required`` is narrowed to ``["takeaways"]`` because
      ``briefs.save_brief`` stamps ``date``/``user_name``/``generated_at``
      server-side before validation — advertising them as required would force
      the caller to invent values the server discards.
    """
    brief_schema = Brief.model_json_schema()
    defs = brief_schema.pop("$defs", {})
    brief_schema["required"] = ["takeaways"]
    return {
        "type": "object",
        "properties": {"brief": brief_schema},
        "required": ["brief"],
        "$defs": defs,
    }


_SAVE_BRIEF_INPUT_SCHEMA = _save_brief_input_schema()


@tool(
    "save_brief",
    "Persist a composed daily brief. Pass `brief` as a JSON object matching the "
    "Brief schema in this tool's inputSchema — a `takeaways` list (1-5 items), "
    "each with headline, summary, tone (positive/caution/critical/neutral), an "
    "optional metric ({metric, days}), and optional markdown `details`. "
    "date/user_name/generated_at are stamped server-side — omit them. The server "
    "validates and atomically writes briefings/<today>.json; invalid briefs are "
    "rejected. Use after composing the brief via the `brief` prompt.",
    _SAVE_BRIEF_INPUT_SCHEMA,
)
async def save_brief(args: dict) -> dict:
    # Thin wrapper over briefs.save_brief (the single integrity gate). DROP the
    # returned `brief` pydantic object — only the {saved,date,path} scalars are
    # _text-wrapped (a pydantic Brief through json.dumps would raise TypeError).
    try:
        result = briefs.save_brief(args["brief"])
    except ValidationError as e:
        return _err(f"brief failed schema validation: {_validation_error_summary(e)}")
    return _text({"saved": True, "date": result["date"], "path": result["path"]})


@tool(
    "get_brief_context",
    "Today's pre-assembled brief context — the deterministic planner's typed "
    "output in ONE call: priority-ordered candidate takeaways (which triggers "
    "fired, the citable metrics with their display rendering, an advisory tone), "
    "today's snapshot + 60-day baselines, training load (ctl/atl/tsb), the actual "
    "14-day workout list, RHR anomalies, active-plan status + adherence + "
    "days-to-race, and recent-brief continuity. Prefer this over orchestrating "
    "many tools when answering 'how am I doing / what's today's read / what should "
    "I do today' — every number is pre-computed and traceable to the data. "
    "Overkill for a single-metric question — use get_metric_trend instead.",
    {},
)
async def get_brief_context(_args: dict) -> dict:
    # Lazy import: brief_planner -> status -> tools would cycle at import time.
    from . import brief_planner

    # recent_briefs must be passed IN — assemble_brief_context never reads the
    # briefings dir itself, so omitting it (as this handler used to) left
    # `continuity` permanently empty over MCP while the in-process composer
    # got the real headlines.
    return _text(brief_planner.assemble_brief_context(
        recent_briefs=briefs.load_recent_briefs()).model_dump())


# --- generate_brief_report: PDF report rendering ---------------------------
# Reachable ONLY via run_stdio() (see web/mcp_server.py) — never merged into
# ALL_TOOLS, never served over the streamable-HTTP /mcp/ transport. A
# phone-triggered call over that network transport would get back a
# container-internal path with no way to retrieve the file; this boundary is
# structural, not just documented (see
# docs/plans/2026-07-07-pdf-chart-reports-design.md). chart's png format
# (the former generate_chart, folded in 0.57.0) does NOT share this boundary
# — its inline image content block sidesteps the no-file-retrieval problem
# that still applies to a PDF.

_PROJECT_ROOT = Path(__file__).resolve().parents[3]

_EPHEMERAL_DIR_PID_RE = re.compile(r"^local-fitness-reports-(\d+)-")
_EPHEMERAL_DIR_MAX_AGE_SECONDS = 24 * 60 * 60  # longer than any plausible single session

_EPHEMERAL_DIR: Path | None = None
_EPHEMERAL_DIR_LOCK = threading.Lock()


def _rmtree_ignore_errors(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _sweep_stale_ephemeral_dirs() -> None:
    """Best-effort removal of `local-fitness-reports-<pid>-*` directories
    left behind by processes that exited without running atexit handlers
    (SIGKILL/SIGTERM). Only removes a candidate when its mtime is older
    than 24h AND the PID embedded in its name is dead (os.kill(pid, 0)
    raises ProcessLookupError) — a directory whose PID is still alive, or
    whose name doesn't match the expected pattern, is left alone. Each
    candidate is handled in its own try/except so one bad candidate never
    aborts sweeping the rest."""
    try:
        candidates = list(Path(tempfile.gettempdir()).glob("local-fitness-reports-*"))
    except OSError:
        return
    for candidate in candidates:
        try:
            match = _EPHEMERAL_DIR_PID_RE.match(candidate.name)
            if not match:
                continue
            pid = int(match.group(1))
            age_seconds = time.time() - candidate.stat().st_mtime
            if age_seconds <= _EPHEMERAL_DIR_MAX_AGE_SECONDS:
                continue
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass  # PID is dead — safe to remove
            else:
                continue  # PID is alive (or we can't tell) — leave it alone
            _rmtree_ignore_errors(candidate)
        except Exception:
            continue


def _default_reports_dir() -> Path:
    """Resolve the reports directory. Honor LOCAL_FITNESS_REPORTS_DIR for a
    host override (persistent, no atexit cleanup — this branch never
    touches _EPHEMERAL_DIR, so there's nothing to guard or clean up).

    Otherwise, resolve to a per-process ephemeral tempfile.mkdtemp()
    directory, memoized in the module-level _EPHEMERAL_DIR cache and
    registered for atexit cleanup on first use. The directory name embeds
    this process's PID (local-fitness-reports-<pid>-<random>) so a
    best-effort stale-directory sweep (_sweep_stale_ephemeral_dirs) can
    tell a genuinely dead session's leftover directory apart from a live
    one's, and only removes the former.

    _EPHEMERAL_DIR_LOCK guards the whole check-then-set-and-create sequence
    below: both tools dispatch this function via asyncio.to_thread (to keep
    its filesystem I/O off the event loop), so two concurrent tool calls
    run this body on separate OS worker threads — without the lock, both
    could observe _EPHEMERAL_DIR as None and each create their own
    ephemeral directory.

    Accepted residual risk: atexit doesn't fire on SIGKILL or (Python's
    default disposition for) SIGTERM, so an abruptly-killed process's
    directory can leak until the age+liveness sweep above claims it. That
    sweep itself has a narrower residual gap: os.kill(pid, 0) reads a
    zombie (exited but unreaped) child as still alive, and a reaped PID
    can be reassigned to an unrelated process — both can extend leakage
    beyond one stale session in rare cases. Building a real liveness
    registry to close this isn't justified for a personal, single-user
    tool."""
    override = os.environ.get("LOCAL_FITNESS_REPORTS_DIR")
    if override:
        return Path(override)

    with _EPHEMERAL_DIR_LOCK:
        global _EPHEMERAL_DIR
        if _EPHEMERAL_DIR is not None:
            return _EPHEMERAL_DIR
        _sweep_stale_ephemeral_dirs()
        new_dir = Path(tempfile.mkdtemp(prefix=f"local-fitness-reports-{os.getpid()}-"))
        atexit.register(_rmtree_ignore_errors, new_dir)
        _EPHEMERAL_DIR = new_dir
        return new_dir


async def _auto_open(path: Path) -> None:
    """Best-effort: open `path` in the OS default viewer (macOS `open`).
    Never raises and never fails the tool call — the file was generated
    successfully either way. `check=False` means a non-zero exit code
    (e.g. "no GUI session") doesn't raise, so it's checked explicitly and
    logged; a missing `open` binary or a timeout raise real exceptions,
    caught below. stdout/stderr are redirected to DEVNULL rather than
    inherited from the parent — this process's stdout is the live
    JSON-RPC framing channel for the whole stdio MCP session, so anything
    `open` ever wrote there would corrupt that transport. The sleep after
    a successful open is a deliberate grace-period delay: even on a
    completely normal process exit (no crash), atexit-triggered cleanup
    could otherwise race the just-spawned viewer process while it's still
    reading the file off disk."""
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["open", str(path)],
            check=False,
            timeout=10,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            LOG.warning("auto-open exited non-zero (%s) for %s", result.returncode, path)
        await asyncio.sleep(1.5)
    except Exception:
        LOG.warning("auto-open failed for %s", path, exc_info=True)


def _content_tag(data: bytes) -> str:
    """A short content hash for a rendered artifact's filename.

    Content-addressing the filename fixes a real trust bug: artifacts are
    written to a per-process tmp dir and auto-opened with macOS `open`, but
    `open` RE-FOCUSES an already-open Preview window for a path it has seen
    rather than reloading the bytes. A deterministic `brief-<date>.pdf` name
    therefore showed a stale render on every re-generate — a user read
    yesterday's-looking page and concluded the data pipeline was stale
    (observed 2026-07-22). Suffixing the name with the content hash means
    changed content always lands on a NEW filename (a genuinely fresh window),
    while identical content reuses the same file (idempotent — refocusing is
    correct when the bytes match).

    Use this on artifact bytes ONLY when the renderer is byte-reproducible
    (matplotlib's PNG writer is). PDFs must go through `_render_tag` instead:
    WeasyPrint's byte stream is NOT reproducible — see that docstring."""
    return hashlib.sha256(data).hexdigest()[:8]


def _render_tag(*parts: object) -> str:
    """`_content_tag` for PDFs: hash the render's logical INPUTS, not its bytes.

    WeasyPrint's PDF serialization is not byte-reproducible — the same HTML
    string rendered twice in one process can produce different bytes depending
    on interpreter allocation state (measured 2026-07-23: ~50% of paired
    renders diverged on Linux CI/Docker; macOS's allocator usually masks it,
    which is why 0.28.2's bytes-hash looked stable locally). Hashing the PDF
    bytes therefore broke the "identical content reuses one filename" half of
    the contract at random. Hashing what we ASKED the renderer to draw keeps
    both halves deterministic on every platform.

    Each part is either raw bytes (chart PNGs — themselves content) or a
    JSON-serializable structure, canonicalized with sort_keys. The app version
    and resolved brand theme are always mixed in, so a release that changes
    layout or a brand-file change still lands on a fresh filename rather than
    refocusing a stale-looking window."""
    from local_fitness.agent import branding

    h = hashlib.sha256()
    h.update(_APP_VERSION.encode())
    h.update(json.dumps(branding.load_theme(), sort_keys=True, default=str).encode())
    for p in parts:
        if isinstance(p, (bytes, bytearray)):
            h.update(bytes(p))
        else:
            h.update(json.dumps(p, sort_keys=True, default=str).encode())
        h.update(b"\x1f")
    return h.hexdigest()[:8]


try:
    _APP_VERSION = importlib.metadata.version("local-fitness")
except importlib.metadata.PackageNotFoundError:  # uninstalled source tree
    _APP_VERSION = "0"


def _write_atomic(reports_dir: Path, final_name: str, data: bytes) -> Path:
    """Write `data` to `reports_dir/final_name` via a per-call-unique .tmp
    sibling + os.replace() — the atomic-write shape agent/briefs.py's
    save_brief() already uses, refined with a uuid4-suffixed temp name (not
    save_brief()'s fixed one) so two concurrent processes/threads racing an
    identical call never share a temp inode before either reaches replace().
    Confirms the resolved final path is contained under reports_dir before
    writing anything — the same `.resolve().relative_to()` containment
    pattern CLAUDE.md's security section mandates for path joins with user
    input."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    final_path = reports_dir / final_name
    final_path.resolve().relative_to(reports_dir.resolve())
    tmp_path = reports_dir / f".{final_name}.{uuid4().hex[:8]}.tmp"
    tmp_path.write_bytes(data)
    os.replace(tmp_path, final_path)
    return final_path


def _build_plan_section(target_date: str) -> dict | None:
    """Training Plan section payload for generate_brief_report's PDF, or
    None when there's nothing to show (no active plan, or an active plan
    with no workout data at all in the trailing-7-day window ending on
    target_date). Computed fresh from plans.py, keyed to target_date (the
    brief's own date) as "today" — not date.today() — so regenerating an
    old brief's PDF shows that day's plan state, not today's. Mirrors
    get_training_plan_progress's plan-loading pattern, anchored to
    target_date instead of the real wall-clock date."""
    with db.connect() as conn:
        active = plans.get_active_plan(conn=conn)
        if active is None:
            return None

        frontier = db.last_known_daily_date(conn=conn)
        dates = [w["date"] for w in active["workouts"]] or [target_date]
        start = min(dates)
        end = max([target_date, *dates] + ([frontier] if frontier else []))
        activities_by_date = plans.load_activities_by_date(start, end, conn=conn)
        cfg = plans.resolve_grading_config(conn=conn)
    # build_plan_detail has no "as of" date concept — grade_workout's pending
    # holdout compares each workout's OWN date against the real data frontier,
    # not against target_date, so every workout's verdict is a settled fact
    # once the frontier has passed it (regenerating an old brief's PDF still
    # shows accurate historical grading). target_date only selects which
    # graded workout is "today" and which trailing window to show below —
    # it is never passed to build_plan_detail itself (its 4th positional
    # param is best_effort, a Riegel-projection dict, not a date).
    detail = plans.build_plan_detail(active, frontier, activities_by_date, cfg=cfg)

    # 2a: weekly_rollup owns the windowing, per-day to_miles conversion, and
    # verdict-conditional actual_mi suppression — days IS the last_7_days
    # table as-is (no per-day enrichment needed here); the empty->None
    # short-circuit stays local to this consumer.
    rollup = weekly_rollup(detail["workouts"], target_date)
    last_7_days = rollup["days"]
    if not last_7_days:
        return None

    week_planned_mi = rollup["week_planned_mi"]
    week_actual_mi = rollup["week_run_mi"]
    week_walk_mi = rollup["week_walk_mi"]
    slips = rollup["slips"]
    adherence_pct = detail.get("adherence_pct")
    if adherence_pct is None:
        adherence_pct = 0

    today_entry = next((w for w in detail["workouts"] if w["date"] == target_date), None)
    today_payload = None
    if today_entry is not None:
        target_m = today_entry.get("target_distance_m")
        today_payload = {
            "type": today_entry["type"],
            "distance_mi": units.to_miles(target_m) if target_m else None,
            "pace_min_per_mi": units.format_pace_min_per_mi(today_entry.get("target_pace_sec_per_km")),
            "description": today_entry.get("description") or "",
        }

    # build_plan_detail has no days_to_race field at all (unlike goal_type/
    # race_date, it's never a stored plan column) -- build_plan_status
    # computes it the same way, on the fly, from race_date and its own
    # "today" parameter. Anchored to target_date here (not build_plan_detail,
    # which has no date concept) since "days from the brief's date to the
    # race" is inherently relative to whichever date this report is for.
    race = detail.get("race_date")
    days_to_race = (
        (date.fromisoformat(race) - date.fromisoformat(target_date)).days
        if race else None
    )

    return {
        "adherence_pct": adherence_pct,
        # The rest-day-free companion to adherence_pct, plus how many rest days
        # separate them. Stays None (not 0-defaulted like adherence_pct above)
        # when the window holds no prescribed session: 0% would assert that
        # nothing got done, when the truth is that nothing was asked for.
        "sessions_adherence_pct": detail.get("sessions_adherence_pct"),
        "rest_days_counted": detail.get("rest_days_counted"),
        "goal_type": detail.get("goal_type") or "goal",
        "days_to_race": days_to_race,
        "week_planned_mi": week_planned_mi,
        # RUN miles, not foot miles — the strip says so, and week_walk_mi
        # carries the rest so the two reconcile to what was actually covered.
        "week_actual_mi": week_actual_mi,
        "week_walk_mi": week_walk_mi,
        "slips": slips,
        "today": today_payload,
        "last_7_days": last_7_days,
    }


async def assemble_brief_render_inputs(
    brief: Brief, target_date: str
) -> tuple[dict[str, bytes], dict | None]:
    """Chart PNGs + the resolved Training Plan section for one saved brief.

    Extracted from ``generate_brief_report`` so the PDF and the evening email
    (``cli.brief_email``) build their render inputs from ONE implementation.
    They target different renderers, but "which takeaways get a chart", "what
    window does that chart cover" and "what does the plan section say" are
    properties of the brief, not of the output format. Two copies would drift
    silently, and the divergence would only be visible to someone holding both
    artifacts side by side.

    Returns ``(charts_by_index, plan_section)``. ``charts_by_index`` is keyed by
    ``str(index)`` over ``enumerate(brief.takeaways)`` — NOT by metric name (two
    takeaways can cite the same metric). ``plan_section`` is None when there is
    no active plan or no plan data in the trailing window, and otherwise carries
    ``today["coaching_line"]`` already resolved.

    Best-effort throughout, and deliberately so: a chart that will not render is
    skipped, a malformed plan section becomes None, and a failed coaching-line
    generation falls back to the deterministic template. Both callers are
    enriching a brief that is already saved and already correct, so nothing here
    may take that brief down with it.
    """
    from . import visuals  # lazy: defers matplotlib/weasyprint import cost

    charts_by_index: dict[str, bytes] = {}
    for index, takeaway in enumerate(brief.takeaways):
        if takeaway.metric is None:
            continue
        try:
            # Window ENDS on the brief's date, not today — see _fetch_metric_series.
            m_dates, m_values = _fetch_metric_series(
                takeaway.metric.metric, takeaway.metric.days, end=target_date)
            if not m_values:
                continue
            fmt = _chart_value_fmt(takeaway.metric.metric)
            # Takeaways pick their own windows, so one brief routinely carries
            # 14/30/60-day charts at identical size. Caption which is which.
            label = f"last {takeaway.metric.days} days"
            async with visuals.RENDER_LOCK:
                png_bytes = await asyncio.to_thread(
                    visuals.render_chart_png,
                    list(zip(m_dates, m_values, strict=True)), "line", fmt, label,
                )
            charts_by_index[str(index)] = png_bytes
        except Exception:
            # A per-takeaway fetch/render problem (transient DB lock,
            # degenerate single-point/all-identical-value series, etc.)
            # must never fail the whole report — that takeaway simply
            # renders without a chart image.
            LOG.warning(
                "chart render skipped for takeaway %d in brief %s",
                index, target_date, exc_info=True,
            )
            continue

    plan_section: dict | None = None
    try:
        plan_section = _build_plan_section(target_date)
    except Exception:
        # Malformed/partial plan data must never fail the whole report —
        # same "one section's problem never sinks the report" precedent
        # as the chart-rendering loop above.
        LOG.warning("plan section build failed for brief %s", target_date, exc_info=True)
        plan_section = None

    if plan_section is not None and plan_section["today"] is not None:
        # ONE connection for the whole pre-render resolution (was 6: the
        # profile resolve opened two, user_name one per call and it was called
        # TWICE, the memory resolve two more). Resolved into locals here and
        # threaded into the generator below — the await stays outside the
        # block, and `_user_name` is now computed once so the prompt (and
        # therefore plan_coach's disk-cache key) is byte-identical to before.
        with db.connect() as conn:
            profile = coach.resolve_coach_profile(conn=conn)
            _user_name = config.user_name(conn=conn)
            _memory_text = memory.render_memory_for_prompt(
                conn=conn,
                today=target_date,
                exclude_source_key=("brief", target_date),
                user_name=_user_name,
            )
        try:
            coaching_line = await plan_coach.generate_coaching_line_cached(
                profile,
                plan_section["today"],
                plan_section["last_7_days"],
                plan_section["adherence_pct"],
                plan_section["days_to_race"],
                plan_section["goal_type"],
                # .get, not []: an older payload shape (a cached section, a
                # caller predating this field) must not KeyError. None keeps
                # the prompt — and so the disk cache key — exactly as it was.
                sessions_adherence_pct=plan_section.get("sessions_adherence_pct"),
                notes_text=notes.render_for_prompt(),
                user_name=_user_name,
                # Keyed to the brief's own date (like the whole section), with
                # that brief's own reflection excluded so reflect can't bust
                # this cache (see memory.render_memory_for_prompt).
                memory_text=_memory_text,
            )
        except Exception:
            LOG.warning(
                "plan coaching-line generation failed for brief %s, using fallback",
                target_date, exc_info=True,
            )
            coaching_line = plan_coach.fallback_coaching_line(
                plan_section["today"],
                plan_section["last_7_days"],
                plan_section["days_to_race"],
                plan_section["goal_type"],
                target_date=target_date,
            )
        plan_section["today"]["coaching_line"] = coaching_line

        # 4a: advisory grounding of the coaching line against the deterministic
        # plan section — mirrors grounding.log_grounding's pattern (log-only,
        # never gates, never alters the PDF). Runs whichever line ended up in
        # the section (Claude-generated or the deterministic fallback above).
        try:
            flags = plan_coach.ground_coaching_line(coaching_line, plan_section)
            detail = "".join(
                f" [{f.nearest_metric}:{f.token}Δ{f.delta}]" for f in flags[:5])
            LOG.info("plan_coach_grounding flags=%d%s", len(flags), detail)
        except Exception:  # noqa: BLE001 — an advisory signal must never break the PDF
            LOG.exception("plan_coach_grounding failed (advisory, ignored)")

    return charts_by_index, plan_section


@tool(
    "generate_brief_report",
    "Render a saved daily brief into a polished, beautiful PDF report "
    "(visually comparable to the sibling budget project's monthly reports). "
    "Local-only: reachable via stdio MCP clients (Claude Code/Claude Desktop "
    "on this same machine), never over the network. Returns a local file "
    "path the user can open directly.",
    {"date": str},
)
async def generate_brief_report(args: dict) -> dict:
    target_date = args["date"]
    if msg := _validate_date(target_date):
        return _err(msg)

    brief_path = briefs.DEFAULT_BRIEFINGS_DIR / f"{target_date}.json"
    if not brief_path.exists():
        return _err(f"no saved brief for {target_date}")
    try:
        brief = Brief.model_validate_json(brief_path.read_text(encoding="utf-8"))
    except ValidationError as e:
        return _err(f"brief failed schema validation: {_validation_error_summary(e)}")

    from . import visuals  # lazy: defers matplotlib/weasyprint import cost

    charts_by_index, plan_section = await assemble_brief_render_inputs(
        brief, target_date)

    # Shrink first, then truncate. The density ladder inside render_brief_pdf
    # handles the common case; only when even the densest rung still spills do
    # we start dropping takeaways, lowest-priority first (brief_planner emits
    # them in priority order, so the tail is the cheapest thing to lose). The
    # drop is always stated on the page — see `omitted` -> p.omitted-note.
    #
    # Retries after the first attempt use the densest preset ONLY: the roomier
    # rungs already failed with strictly MORE content, so re-walking the whole
    # ladder each round would just pay for known-losing layouts. Bounds the
    # worst case at len(PRESETS) + len(takeaways) - 1 layout passes.
    kept = list(brief.takeaways)
    omitted = 0
    try:
        async with visuals.RENDER_LOCK:
            while True:
                trial = (brief if not omitted
                         else brief.model_copy(update={"takeaways": kept}))
                trial_charts = {
                    k: v for k, v in charts_by_index.items() if int(k) < len(kept)
                }
                presets = (visuals.DENSITY_PRESETS if not omitted
                           else visuals.DENSITY_PRESETS[-1:])
                pdf_bytes, pages = await asyncio.to_thread(
                    visuals.render_brief_pdf,
                    trial, trial_charts, plan_section, omitted, presets,
                )
                if pages == 1 or len(kept) <= 1:
                    break
                kept = kept[:-1]
                omitted += 1
    except Exception as e:
        # The full WeasyPrint/cairo traceback belongs in the server log, but
        # the exception CLASS + message are render-stack detail, not secrets
        # (the run_sql 0.37.0 precedent) — and "see the server log" was
        # observed live as an unactionable dead end for an agent that cannot
        # read the log. Name the failure and the recovery.
        LOG.warning("PDF render failed", exc_info=True)
        return _err(
            f"PDF render failed: {type(e).__name__}: {e}",
            remediation=(
                "the brief data itself is fine — this is the PDF renderer "
                "(WeasyPrint needs native Pango/HarfBuzz libs; on macOS see "
                "DYLD_LIBRARY_PATH in .env.example). Read the brief via "
                "get_brief_context or the fitness://brief/latest resource "
                "instead of retrying the render."
            ),
        )
    if pages != 1:
        # A single takeaway that still overflows is a content bug upstream
        # (a runaway `details` block), not something to paper over silently.
        LOG.warning(
            "brief %s still %d pages at the densest preset with %d takeaway(s)",
            target_date, pages, len(kept),
        )

    try:
        reports_dir = await asyncio.to_thread(_default_reports_dir)
    except OSError as e:
        return _err(f"could not prepare reports directory: {e}")
    try:
        tag = _render_tag(
            trial.model_dump(mode="json"), plan_section, omitted,
            *(trial_charts[k] for k in sorted(trial_charts)),
        )
        final_path = _write_atomic(
            reports_dir, f"brief-{target_date}-{tag}.pdf", pdf_bytes)
    except ValueError:
        return _err("resolved path escaped reports directory")
    await _auto_open(final_path)
    return _text({"path": str(final_path)})


_GENERATE_CHART_TYPES = frozenset({"line", "bar", "combo"})

async def _chart_png(args: dict) -> dict:
    """chart's format="png" branch — the former `generate_chart` tool body
    (folded in 0.57.0). `style` maps onto the old `chart_type`; the ascii-only
    styles error with the allowed list (the get_metric_trend pattern) rather
    than silently falling back."""
    metric = args["metric"]
    days = args["days"]
    chart_type = args.get("style") or "line"
    if chart_type not in _GENERATE_CHART_TYPES:
        return _err(
            f"style '{chart_type}' is not available as png",
            allowed=sorted(_GENERATE_CHART_TYPES),
        )
    dates, values = _fetch_metric_series(metric, days)
    if not values:
        return _err("no data in window", metric=metric, days=days)

    from . import visuals  # lazy: defers matplotlib/weasyprint import cost

    fmt = _chart_value_fmt(metric)
    try:
        async with visuals.RENDER_LOCK:
            png_bytes = await asyncio.to_thread(
                visuals.render_chart_png, list(zip(dates, values, strict=True)), chart_type, fmt
            )
    except Exception as e:
        return _err(f"chart render failed: {e}")

    try:
        reports_dir = await asyncio.to_thread(_default_reports_dir)
    except OSError as e:
        return _err(f"could not prepare reports directory: {e}")
    # Content-address the filename with the same tag the two PDF tools use
    # (0.28.2): macOS `open` REFOCUSES an already-open Preview window for a path
    # it has seen rather than reloading the bytes, so a day-deterministic
    # `chart-...-<date>.png` name showed a STALE chart when the same
    # metric/chart_type/days re-rendered after an intra-day sync. The content
    # tag makes changed bytes land on a NEW filename (a genuinely fresh window)
    # while identical bytes reuse one file (idempotent — refocusing is correct
    # when the bytes match). See _content_tag and the PDF paths above.
    try:
        final_path = _write_atomic(
            reports_dir,
            f"chart-{metric}-{chart_type}-{days}d-{_content_tag(png_bytes)}.png",
            png_bytes,
        )
    except ValueError:
        return _err("resolved path escaped reports directory")
    await _auto_open(final_path)
    # Fix A (2026-07-10 doc): add an inline image content block alongside the
    # existing text (file path) block, so a networked /mcp/ client — which
    # has no way to retrieve a local file path — still gets the chart.
    # Reuses visuals._data_uri's base64 step, stripping the "data:...," prefix
    # ImageContent.data doesn't want (the encoding math must not be re-derived
    # at a second call site).
    image_b64 = visuals._data_uri(png_bytes).split(",", 1)[1]
    return {
        "content": [
            {"type": "text", "text": json.dumps({"path": str(final_path)})},
            {"type": "image", "data": image_b64, "mimeType": "image/png"},
        ]
    }


_REPORT_CARD_FORMATS = frozenset({"both", "table", "pdf"})

_REPORT_CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "activity_id": {
            "type": "integer",
            "description": "Grade this exact activity. Overrides `date`.",
        },
        "date": {
            "type": "string",
            "description": "YYYY-MM-DD. Grades that day's primary session.",
        },
        "format": {
            "type": "string",
            "enum": ["both", "table", "pdf"],
            "description": "Output format, default 'both'.",
        },
    },
    "required": [],
}


@tool(
    "workout_report_card",
    "Rated report card for ONE workout — distance, pace, HR (broken down by "
    "mile) and continuity, each given a 1-5 star score with fractional "
    "precision, plus an intent-weighted overall. 5 stars means the day was "
    "executed as prescribed: it is a COMPLIANCE score, not a verdict on how "
    "good the run was, so a correctly-run easy day rates 5. Training load is "
    "reported alongside but never rated. Defaults to the most recent logged "
    "activity — after a sync, call this DIRECTLY (no query_workouts lookup "
    "to find an id; sync_garmin_data's `latest_activity` already names it); "
    "pass activity_id or date to grade a specific one. Grades against the active training plan's "
    "prescribed workout when one exists for that date, otherwise against a "
    "60-day rolling median of comparable activities — the card always says "
    "which. Returns a `markdown` field (render it to the user VERBATIM, it is "
    "already formatted) and, unless format='table', a `path` to a PDF. "
    "Local-only: reachable via stdio MCP clients on this machine, never over "
    "the network.",
    _REPORT_CARD_SCHEMA,
)
async def workout_report_card(args: dict) -> dict:
    target_date = args.get("date")
    if target_date is not None and (msg := _validate_date(target_date)):
        return _err(msg)
    fmt = args.get("format") or "both"
    if fmt not in _REPORT_CARD_FORMATS:
        return _err(f"unknown format '{fmt}'", allowed=sorted(_REPORT_CARD_FORMATS))
    activity_id = args.get("activity_id")

    # ONE connection for every read this tool makes (was ~8: the inputs load,
    # then resolve_coach_profile ×2, user_name, the memory/ledger resolve ×2,
    # load_read and has_event each opening their own). Everything inside is
    # synchronous and local — the SDK call, the PDF render and save_card all
    # stay OUTSIDE the block. save_card in particular MUST keep opening its
    # own connection: it runs on a worker thread via asyncio.to_thread, and
    # sqlite3 connections are same-thread-checked.
    #
    # The per-sample HR trace is only worth resolving for the PDF (it can reach
    # the network on a cache miss, and the markdown card has nowhere to plot
    # it). format='table' therefore stays a purely local, no-network read.
    with db.connect() as conn:
        inputs = report_card.load_report_card_inputs(
            conn, activity_id=activity_id, target_date=target_date,
            hr_trace=fmt != "table",
        )
        if inputs is None:
            return _err(
                "no matching activity found", activity_id=activity_id,
                date=target_date)

        card = report_card.build_card(
            inputs["activity"], inputs["splits"], inputs["plan_workout"],
            inputs["reference"], inputs["context"], inputs.get("hr_samples"),
            inputs.get("recent_activities"), inputs.get("upcoming_workouts"),
            inputs.get("hr_zones"),
        )

        # The coach's verbal read leads the card. Claude-generated behind the
        # same single-entry disk cache plan_coach uses, with a deterministic
        # template fallback — a missing credential or a dead stream costs the
        # phrasing, never the card, since every grade in it was already
        # computed in Python.
        profile = coach.resolve_coach_profile(conn=conn)
        _activity_key = str(inputs["activity"]["activity_id"])
        _user_name = config.user_name(conn=conn)
        # Resolved ONCE into locals and threaded byte-identically into both the
        # key and the generator — any drift and the fast-path key never matches.
        _notes_text = notes.render_for_prompt()
        # THIS card's own journal entries are excluded: without that, reflecting
        # on the card would change its next prompt, bust the read cache, and
        # regenerate on every render forever.
        #
        # `today` is the ACTIVITY's date, not the clock's (0.40.2, mirroring
        # the plan-coach call above, which has anchored to `target_date` since
        # it was written). A read about a run on 2026-07-28 must cite the
        # relationship as it stood then — the streaks, the plan misses and the
        # trailing card aggregate that were true when he finished it — not
        # figures that have moved in the weeks since. Reading today's ledger
        # onto an old card is the same category of error as grading it against
        # today's plan, which build_card already refuses to do.
        #
        # The cache consequence is a consequence, not the reason: the ledger
        # renders a step-streak counter that increments daily, so an
        # unanchored memory block put a fresh value into every card's prompt
        # every day, rotating the read cache key and turning every re-render of
        # a past card into a full SDK call. Measured on the live corpus
        # 2026-08-02: 14 of 15 stored cards missed the fast path.
        _memory_text = memory.render_memory_for_prompt(
            conn=conn,
            today=inputs["activity"]["date"],
            exclude_source_key=("report_card", _activity_key),
            user_name=_user_name,
        )
        # Fast path: the stored card doubles as a per-activity read cache. On a
        # key match the stored parsed read is reused as-is — no SDK call — and
        # read_key stays non-NULL so save_card's guarded UPSERT sees an equal
        # key and no-ops (the row stays byte-identical; this render's
        # recomputed grades never land under the stored render's words).
        read_key: str | None = workout_coach.read_cache_key(
            profile, card, notes_text=_notes_text, user_name=_user_name,
            memory_text=_memory_text)
        _stored = card_store.load_read(
            inputs["activity"]["activity_id"], conn=conn)
        # Hoisted onto the shared connection with the other reads. Safe to
        # decide here rather than after the read: reflect is the only writer
        # of ("report_card", <this id>) entries, it is fired below, and its
        # own has_event pre-check plus idx_journal_event still backstop the
        # race. Short-circuit order is preserved — with memory disabled the
        # journal is never touched.
        _should_reflect = memory.memory_enabled() and not journal.has_event(
            "report_card", _activity_key, conn=conn)

    if (_stored and _stored[0] == read_key
            and card_store.read_is_complete(_stored[1])):
        card["coach_read"] = _stored[1]
        LOG.info("workout read reused from the stored card (key match)")
    else:
        try:
            card["coach_read"] = await workout_coach.generate_read_cached(
                profile, card, notes_text=_notes_text, user_name=_user_name,
                memory_text=_memory_text)
        except Exception:
            LOG.warning(
                "workout read generation failed for activity %s, using fallback",
                inputs["activity"]["activity_id"], exc_info=True)
            card["coach_read"] = workout_coach.fallback_read(card)
            # Fallback-ness is known ONLY from this except branch — the
            # template and a real read are structurally identical dicts. A
            # NULL key means "not the coach's voice": never reused by the
            # fast path, never allowed to overwrite a real-read row.
            read_key = None

    # Auto-reflect (fire-and-forget): the coach may write this session into
    # its journal. has_event makes the common case — re-rendering a card —
    # skip even task creation, so a re-render never pays an SDK call and can
    # never double-write (the DB unique index backstops the race regardless).
    # The task reference is held module-side; asyncio only weakly references
    # scheduled tasks and an unreferenced one can be GC'd mid-flight.
    if _should_reflect:
        _task = asyncio.create_task(reflect.reflect_after_report_card(card))
        _REFLECT_TASKS.add(_task)
        _task.add_done_callback(_REFLECT_TASKS.discard)

    # Persist the card as a dated snapshot (both formats). save_card never
    # raises, and key identity decides the write: an equal-key render is a
    # byte-identical no-op, a fallback never overwrites a real-read row.
    # to_thread keeps its busy_timeout wait off the event loop, mirroring the
    # PDF renders below.
    await asyncio.to_thread(card_store.save_card, card, read_cache_key=read_key)
    payload = {
        "markdown": report_card.render_markdown(card),
        "activity_id": inputs["activity"]["activity_id"],
        "date": inputs["activity"].get("date"),
        "overall": card["overall"],
        "stars": {k: v.get("stars") for k, v in card["metrics"].items()},
        # The rendered rating strings, so an agent reading this card aloud
        # renders them verbatim instead of re-deriving a star string of its own
        # — the same single-source discipline metric_table keeps.
        "ratings": {k: report_card.star_display(v.get("stars"))
                    for k, v in card["metrics"].items()},
        "overall_rating": report_card.star_display(card["overall"].get("stars")),
        "scale_note": _STAR_SCALE_NOTE,
        "reference": card["reference"].get("mode"),
        "intent": card["intent"],
        "intent_source": card["intent_source"],
        "splits_available": card["splits"]["available"],
    }
    if inputs["other_activities_on_date"]:
        # A double-day shouldn't silently hide its second session.
        payload["other_activities_on_date"] = inputs["other_activities_on_date"]
    if fmt == "table":
        return _text(payload)

    from . import visuals  # lazy: defers matplotlib/weasyprint import cost

    split_chart: bytes | None = None
    # The trace can exist without per-lap splits (backfilled activities have no
    # splits but Garmin still holds their details), so either series is reason
    # enough to chart.
    has_series = bool(card.get("hr_trace")) or (
        card["splits"]["available"]
        and any(r.get("avg_hr") for r in card["splits"]["rows"])
    )
    if has_series:
        try:
            async with visuals.RENDER_LOCK:
                split_chart = await asyncio.to_thread(visuals.render_split_hr_png, card)
        except Exception:
            # A chart problem must never sink the card — the same "one
            # section's problem never fails the report" precedent as
            # generate_brief_report's per-takeaway chart loop above.
            LOG.warning(
                "split chart render skipped for activity %s",
                inputs["activity"]["activity_id"], exc_info=True,
            )

    try:
        async with visuals.RENDER_LOCK:
            pdf_bytes, pages = await asyncio.to_thread(
                visuals.render_report_card_pdf, card, split_chart
            )
    except Exception as e:
        # Same contract as generate_brief_report's render failure: class +
        # message are actionable render-stack detail, and the recovery is
        # named — the graded card exists without the PDF.
        LOG.warning("PDF render failed", exc_info=True)
        return _err(
            f"PDF render failed: {type(e).__name__}: {e}",
            remediation=(
                "the grading succeeded — only the PDF renderer failed "
                "(WeasyPrint needs native Pango/HarfBuzz libs; on macOS see "
                "DYLD_LIBRARY_PATH in .env.example). Retry with "
                "format='table' to get the full markdown card with no PDF."
            ),
        )
    if pages != 1:
        # The card has no droppable content (unlike the brief's takeaway tail),
        # so the density ladder is the only lever and it's exhausted. Never let
        # a PDF spill silently (CLAUDE.md) — a warning is the honest signal that
        # this card's splits + coach read outgrew even the densest rung.
        LOG.warning(
            "report card for activity %s still %d pages at the densest preset",
            inputs["activity"]["activity_id"], pages,
        )
        payload["pages"] = pages

    try:
        reports_dir = await asyncio.to_thread(_default_reports_dir)
    except OSError as e:
        return _err(f"could not prepare reports directory: {e}")
    try:
        tag = _render_tag(card, split_chart or b"")
        final_path = _write_atomic(
            reports_dir,
            f"report-card-{inputs['activity']['activity_id']}-{tag}.pdf",
            pdf_bytes,
        )
    except ValueError:
        return _err("resolved path escaped reports directory")
    await _auto_open(final_path)
    payload["path"] = str(final_path)
    return _text(payload)


# Shared text both card-query tool descriptions carry: the stored card is a
# dated snapshot, and the honest labeling of what graded_at does (and does
# not) promise is the load-bearing part — see the 2026-07-23 design doc.
_CARD_SNAPSHOT_NOTE = (
    "This is the stored snapshot as rated on graded_at — the most recent "
    "render whose read differed (a distinct prompt-key render), so graded_at "
    "can lag more recent renders whose inputs hashed identically. Ratings "
    "reflect the plan active at that render, not a retroactive re-rating, and "
    "a fresh live render may show a slightly different score."
)

#: What a 5 means, carried in the payload so an agent summarising a card aloud
#: cannot drop the claim the card makes about itself. See visuals for why the
#: review-score connotation has to be corrected explicitly rather than trusted.
_STAR_SCALE_NOTE = (
    "5 stars = the day was executed as prescribed. A compliance score, not a "
    "verdict on how good the run was."
)


@tool(
    "list_report_cards",
    "Past workout report cards (stored snapshots), newest run first. One call "
    "answers 'how have my quality days trended' — each row carries the "
    "overall 1-5 star score and the four metric scores without re-rating "
    "anything. Rows stored before 0.50.0 carry the retired letter grade "
    "instead and have null stars. Filter by date range and/or intent_class "
    "(easy|long|quality|steady). Use get_report_card for one card's full "
    "detail and the coach's read. History accumulates as cards are rendered "
    "(no backfill), so older activities may have no row. "
    + _CARD_SNAPSHOT_NOTE,
    {
        "type": "object",
        "properties": {
            "start_date": {
                "type": "string",
                "description": "Earliest workout date, YYYY-MM-DD inclusive.",
            },
            "end_date": {
                "type": "string",
                "description": "Latest workout date, YYYY-MM-DD inclusive.",
            },
            "intent_class": {
                "type": "string",
                "enum": ["easy", "long", "quality", "steady"],
                "description": "Only cards of this workout class.",
            },
            "limit": {
                "type": "integer",
                "description": "Max cards returned (default 20).",
            },
        },
        "required": [],
    },
)
async def list_report_cards(args: dict) -> dict:
    for field in ("start_date", "end_date"):
        value = args.get(field)
        if value is not None and (msg := _validate_date(value, field)):
            return _err(msg)
    intent_class = args.get("intent_class")
    if intent_class is not None and intent_class not in card_store.INTENT_CLASSES:
        return _err(
            f"unknown intent_class '{intent_class}'",
            allowed=list(card_store.INTENT_CLASSES))
    limit = _validate_limit(args, default=20)
    if isinstance(limit, str):
        return _err(limit)
    # limit+1 fetch — the extra row is the truncation signal (see
    # query_workouts / list_observations for the pattern).
    rows = card_store.list_cards(
        start_date=args.get("start_date"), end_date=args.get("end_date"),
        intent_class=intent_class, limit=limit + 1)
    truncated = len(rows) > limit
    rows = rows[:limit]
    cards = [
        {
            "activity_id": r["activity_id"],
            "date": r["activity_date"],
            "graded_at": r["graded_at"],
            "intent": r["intent"],
            "intent_class": r["intent_class"],
            "overall": r["overall_stars"],
            "mean_stars": r["mean_stars"],
            "capped_by": r["capped_by_metric"],
            "stars": {
                "distance": r["distance_stars"],
                "pace": r["pace_stars"],
                "hr": r["hr_stars"],
                "continuity": r["continuity_stars"],
            },
            # Pre-0.50.0 rows only; null on everything rated since.
            "legacy_grade": r["overall_grade"],
        }
        for r in rows
    ]
    return _text({"cards": cards, "count": len(cards), "truncated": truncated})


@tool(
    "get_report_card",
    "One stored workout report card by activity_id: the full graded snapshot, "
    "the coach's verbal read from that render, and a preformatted `markdown` "
    "card (render it to the user VERBATIM — it is already formatted). "
    + _CARD_SNAPSHOT_NOTE + " Use list_report_cards to find activity_ids.",
    {"activity_id": int},
)
async def get_report_card(args: dict) -> dict:
    activity_id = args.get("activity_id")
    if activity_id is None or not isinstance(activity_id, int):
        return _err("activity_id is required")
    loaded = card_store.load_card(activity_id)
    if loaded is None:
        return _err(
            f"no stored report card for activity {activity_id} yet — a card "
            "is stored whenever it is rendered from a local session "
            "(workout_report_card is stdio-only and cannot be called over "
            "the network)",
            activity_id=activity_id)
    try:
        markdown = report_card.render_markdown(loaded["card"])
    except Exception:
        # A stored snapshot that predates a renderer change must still be
        # retrievable — the structured card is the data, markdown is sugar.
        LOG.warning(
            "stored card for activity %s failed to render markdown",
            activity_id, exc_info=True)
        markdown = None
    return _text({
        "activity_id": loaded["activity_id"],
        "date": loaded["activity_date"],
        "graded_at": loaded["graded_at"],
        "card": loaded["card"],
        "markdown": markdown,
        "coach_read": loaded["card"].get("coach_read"),
    })


ALL_TOOLS = [
    get_brief_context,
    # 0.57.0: get_metric and generate_chart are GONE — folded into
    # get_metric_trend (include_values=true) and chart (format="png"). Both
    # were near-duplicates of their survivor (identical schema/anchor logic;
    # shared fetch + whitelist), i.e. two names for one job — the
    # get_today_status ambiguity (0.48.0) again.
    get_metric_trend,
    chart,
    plan_chart,
    query_workouts,
    get_workout_detail,
    compare_periods,
    find_anomalies,
    sync_garmin_data,
    training_load_status,
    correlate,
    recovery_pattern,
    run_sql,
    save_user_note,
    list_user_notes,
    update_user_note,
    delete_user_note,
    save_coach_memory,
    list_coach_memories,
    delete_coach_memory,
    recall_coach_memories,
    get_coach_personality,
    update_coach_personality,
    get_brief_email_settings,
    update_brief_email_settings,
    get_plan_calendar_settings,
    update_plan_calendar_settings,
    daily_snapshot,
    log_observation,
    list_observations,
    delete_observation,
    log_manual_workout,
    delete_manual_workout,
    propose_training_plan,
    revise_training_plan,
    update_plan_workout,
    update_plan_workouts,
    commit_training_plan,
    discard_training_plan_draft,
    abandon_active_plan,
    get_training_plan_status,
    get_training_plan_progress,
    get_training_plan_draft,
    save_brief,
    list_report_cards,
    get_report_card,
]

# Registered ONLY here, never merged into ALL_TOOLS — wired into run_stdio()
# alone (see web/mcp_server.py's build_server(extra_tools=...)), never into
# build_session_manager()'s HTTP /mcp/ transport. A phone-triggered call over
# that transport would get back a container-internal path with no way to
# retrieve the file — a real constraint for a PDF, which isn't representable
# as MCP ImageContent. chart's png format (the former
# generate_chart, folded in 0.57.0) is deliberately NOT here: it returns an
# inline image content block, so the "no way to retrieve the file remotely"
# problem does not apply and it stays reachable over both transports via
# ALL_TOOLS above. Only the heavy `import matplotlib`/`import weasyprint`
# statements (inside generate_brief_report's body and visuals.py's own module
# body) are deferred, not this list.
# workout_report_card joins it for the same reason: it writes a PDF, and its
# markdown table rides along in the same payload so the table and the PDF can
# never report different grades. That does put a transport-safe table behind
# the stdio boundary; splitting it into an ALL_TOOLS table-only sibling is a
# ~15-line addition calling the same report_card.build_card() if that's ever
# wanted — not shipped speculatively.
LOCAL_ONLY_TOOLS = [generate_brief_report, workout_report_card]


def server_version() -> str:
    """The app version every MCP client sees in ``serverInfo``.

    Read from installed package metadata rather than hardcoded. It was pinned
    at a literal "0.6.0" from the server's first commit, so by 0.44.0 every
    client — Claude Desktop, opencode, a phone over /mcp/ — was reporting a
    version 38 releases stale, which is exactly the number you read off the
    client while trying to work out whether a fix has shipped.

    Falls back to "0.0.0" when the package isn't installed (a source checkout
    running without an install); a version string must never be the reason a
    server fails to start.
    """
    try:
        return importlib.metadata.version("local-fitness")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover
        return "0.0.0"


def make_server(extra_tools: list | None = None):
    return create_sdk_mcp_server(
        name=SERVER_NAME, version=server_version(), tools=ALL_TOOLS + (extra_tools or [])
    )


def allowed_tool_names() -> list[str]:
    return [f"mcp__{SERVER_NAME}__{t.name}" for t in ALL_TOOLS]


# Explicit ALLOW-LIST of the read-only analysis tools (not a denylist): the
# brief loop runs with exactly this set, so its behavior stays unchanged as new
# tools land. A future tool is excluded by default unless deliberately added
# here. Excludes the note-write tools, all observation/manual-workout write
# tools, and (deliberately) daily_snapshot + list_observations so the brief's
# tool set is identical to before this issue.
_READ_ONLY_TOOL_NAMES = (
    # briefing_prompt (V1) instructs "call get_training_plan_status FIRST" —
    # this entry keeps the rollback path's tool grant matching its prompt.
    # Missing here 2026-06-27→2026-07-19: a V1 rollback silently lost
    # plan-aware briefs (round-2 facet review, prompts finding 1).
    "get_training_plan_status",
    # 0.48.0: was "get_today_status" until that tool was removed as a duplicate
    # of daily_snapshot. briefing_prompt (V1) lists it as step 1, so the grant
    # has to move with the prompt — the note above is this exact bug, and it
    # went unnoticed for three weeks the last time. daily_snapshot was
    # previously excluded here ONLY to keep the V1 tool set byte-identical
    # while both names existed; with one name left, excluding it would strip
    # the daily snapshot from the rollback path entirely.
    "daily_snapshot",
    # 0.57.0: "get_metric" left with its tool (folded into get_metric_trend's
    # include_values) — a grant naming an unregistered tool would be dead
    # weight, and briefing_prompt never named it (verified against
    # tests/test_tools.py's grant-matching test).
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
