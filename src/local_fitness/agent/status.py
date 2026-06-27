"""The single source of the "daily snapshot".

``assemble_status()`` is a pure READ over the fitness DB: it never mutates and
never raises on an empty/new DB. It is the one place the daily snapshot is
assembled so a future ``daily_snapshot`` tool and a ``coach`` MCP prompt can
share exactly the same payload.

Design notes:

* Each daily metric is reported under one of three *treatments*:
  - ``baseline_delta`` — value compared against its 60-day baseline mean
    (only the five metrics that actually have a baseline column). Carries
    ``baseline``, ``delta_pct`` and a direction ``arrow``.
  - ``trend_arrow`` — short-window (~7 day) slope direction for metrics where
    a recent trend is the meaningful read (steps, sleep_score, and max_stress,
    which has no baseline column).
  - ``raw`` — value only, for everything else.
* The metric→baseline-column map is *explicit*: ``avg_stress`` maps to
  ``stress_60day_mean``, which is not derivable from the metric name, so we
  never build the column name with an f-string.
* Every DB-row access is guarded — a fresh clone with zero daily_metrics,
  zero activities and zero baselines returns a well-formed empty payload.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .. import db, notes
from . import units
from .tools import DAILY_NUMERIC_METRICS

# Explicit metric → (baseline mean column, baseline sd column | None). Do NOT
# derive these from the metric name: avg_stress → stress_60day_mean breaks the
# f"{metric}_60day_mean" pattern, and only rhr / sleep_seconds carry an sd.
_BASELINE_DELTA_MAP: dict[str, tuple[str, str | None]] = {
    "rhr": ("rhr_60day_mean", "rhr_60day_sd"),
    "sleep_seconds": ("sleep_seconds_60day_mean", "sleep_seconds_60day_sd"),
    "avg_stress": ("stress_60day_mean", None),
    "body_battery_max": ("body_battery_max_60day_mean", None),
    "body_battery_min": ("body_battery_min_60day_mean", None),
}

# Metrics whose meaningful read is a short recent trend, not a 60-day baseline.
# max_stress is here because it has no baseline column at all.
_TREND_METRICS: tuple[str, ...] = ("steps", "sleep_score", "max_stress")

# How many recent days feed the trend-slope computation.
_TREND_WINDOW_DAYS = 7


def _arrow(delta: float) -> str:
    """Direction glyph for a signed delta. Pure direction — no good/bad."""
    if delta > 0:
        return "↑"
    if delta < 0:
        return "↓"
    return "→"


def _slope_arrow(values: list[float]) -> str | None:
    """Least-squares slope sign over an ordered series → arrow, or None when
    there are too few points to read a trend."""
    n = len(values)
    if n < 2:
        return None
    xs = list(range(n))
    x_mean = (n - 1) / 2
    y_mean = sum(values) / n
    denom = sum((x - x_mean) ** 2 for x in xs) or 1e-9
    slope = sum((xs[i] - x_mean) * (values[i] - y_mean) for i in range(n)) / denom
    return _arrow(slope)


def _baseline_row(conn, today: str) -> dict[str, Any] | None:
    """Latest baselines row on/before today, as a plain dict (or None)."""
    row = conn.execute(
        "SELECT * FROM baselines WHERE date <= ? ORDER BY date DESC LIMIT 1",
        (today,),
    ).fetchone()
    return dict(row) if row else None


def _tsb_interpretation(tsb: float | None) -> str:
    """Plain-English read of training stress balance."""
    if tsb is None:
        return "no training-load data yet"
    if tsb < -20:
        return "very fatigued"
    if tsb < -10:
        return "fatigued"
    if tsb > 5:
        return "fresh"
    return "neutral"


def _metric_rows(conn, today: str, baseline: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Build the per-metric rows: baseline_delta for the five baselined
    metrics, trend_arrow for the trend set, raw for everything else."""
    # Today's daily_metrics row (may be absent on an empty/new DB).
    today_row_raw = conn.execute(
        "SELECT * FROM daily_metrics WHERE date = ?", (today,)
    ).fetchone()
    today_row = dict(today_row_raw) if today_row_raw else {}

    rows: list[dict[str, Any]] = []
    for metric in sorted(DAILY_NUMERIC_METRICS):
        value = today_row.get(metric)

        if metric in _BASELINE_DELTA_MAP:
            mean_col, _sd_col = _BASELINE_DELTA_MAP[metric]
            base_val = baseline.get(mean_col) if baseline else None
            delta_pct: float | None = None
            arrow: str | None = None
            if value is not None and base_val:
                delta_pct = round((value - base_val) / base_val * 100, 1)
                arrow = _arrow(value - base_val)
            rows.append({
                "metric": metric,
                "value": value,
                "treatment": "baseline_delta",
                "baseline": base_val,
                "delta_pct": delta_pct,
                "arrow": arrow,
            })
            continue

        if metric in _TREND_METRICS:
            # Window is relative to the passed `today`, NOT wall-clock — so an
            # injected `today` (fixtures / brief_planner) is reproducible.
            cutoff = (date.fromisoformat(today) - timedelta(days=_TREND_WINDOW_DAYS)).isoformat()
            series = [
                r["v"] for r in conn.execute(
                    f"SELECT {metric} AS v FROM daily_metrics "
                    f"WHERE date >= ? AND {metric} IS NOT NULL ORDER BY date",
                    (cutoff,),
                ).fetchall()
            ]
            rows.append({
                "metric": metric,
                "value": value,
                "treatment": "trend_arrow",
                "arrow": _slope_arrow(series),
            })
            continue

        rows.append({"metric": metric, "value": value, "treatment": "raw"})

    return rows


def _training_load(baseline: dict[str, Any] | None) -> dict[str, Any]:
    """CTL/ATL/TSB from the latest baselines row + a plain-English read."""
    if not baseline:
        return {"ctl": None, "atl": None, "tsb": None,
                "interpretation": "no training-load data yet"}
    tsb = baseline.get("tsb")
    return {
        "ctl": baseline.get("ctl"),
        "atl": baseline.get("atl"),
        "tsb": tsb,
        "interpretation": _tsb_interpretation(tsb),
    }


def _recent_workouts(conn, limit: int = 5) -> list[dict[str, Any]]:
    """Last ~5 workouts with raw fields plus mile/formatted convenience fields
    from units.py. Omits a formatted field when units.py returns None (null or
    zero distance / pace)."""
    rows = conn.execute(
        """SELECT activity_id, date, activity_type, activity_name, duration_seconds,
                  distance_meters, avg_hr, max_hr, avg_pace_sec_per_km,
                  elevation_gain_meters, aerobic_te, anaerobic_te, training_load
           FROM activities ORDER BY date DESC, start_time DESC LIMIT ?""",
        (limit,),
    ).fetchall()

    miles = units.display_units() == "miles"
    out: list[dict[str, Any]] = []
    for r in rows:
        w = dict(r)
        if miles:
            distance_mi = units.to_miles(w.get("distance_meters"))
            if distance_mi is not None:
                w["distance_mi"] = distance_mi
        pace = units.format_pace_min_per_mi(w.get("avg_pace_sec_per_km"))
        if pace is not None:
            w["pace_min_per_mi"] = pace
        duration = units.format_duration(w.get("duration_seconds"))
        if duration is not None:
            w["duration_formatted"] = duration
        out.append(w)
    return out


def assemble_status(today: str | None = None) -> dict[str, Any]:
    """Assemble the daily snapshot. Pure read; never raises on an empty DB.

    ``today`` (ISO ``YYYY-MM-DD``) is injectable so callers (fixtures, the brief
    planner) get reproducible output; it defaults to ``date.today()`` so existing
    bare callers are unchanged.

    Returns a dict with keys: ``date``, ``metrics``, ``training_load``,
    ``recent_workouts``, ``user_notes``.
    """
    today = today or date.today().isoformat()
    with db.connect() as conn:
        baseline = _baseline_row(conn, today)
        metrics = _metric_rows(conn, today, baseline)
        training_load = _training_load(baseline)
        recent_workouts = _recent_workouts(conn)

    user_notes = [n.text for n in notes.read_notes() if n.text]

    return {
        "date": today,
        "metrics": metrics,
        "training_load": training_load,
        "recent_workouts": recent_workouts,
        "user_notes": user_notes,
    }
