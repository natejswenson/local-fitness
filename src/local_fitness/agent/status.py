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
from . import briefs, interpret, units
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
    """Plain-English read of training stress balance.

    Delegates to interpret.tsb_zone — the single source of the TSB-zone
    bands, so status.py and training_load_status agree by construction.
    """
    return interpret.tsb_zone(tsb)


def _metric_rows(conn, today: str, baseline: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Build the per-metric rows: baseline_delta for the five baselined
    metrics, trend_arrow for the trend set, raw for everything else.

    One query fetches the trailing trend window (which always includes
    ``today``, its last day) up front; today's row and each trend metric's
    series are then sliced out of that single result set in Python, with
    per-column null-filtering standing in for the old per-metric ``WHERE
    <metric> IS NOT NULL`` clause."""
    # Window is relative to the passed `today`, NOT wall-clock — so an
    # injected `today` (fixtures / brief_planner) is reproducible.
    cutoff = (date.fromisoformat(today) - timedelta(days=_TREND_WINDOW_DAYS)).isoformat()
    window_rows = [
        dict(r) for r in conn.execute(
            "SELECT * FROM daily_metrics WHERE date >= ? AND date <= ? ORDER BY date",
            (cutoff, today),
        ).fetchall()
    ]
    today_row = next((r for r in window_rows if r["date"] == today), {})

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
            row = {
                "metric": metric,
                "value": value,
                "treatment": "baseline_delta",
                "baseline": base_val,
                "delta_pct": delta_pct,
                "arrow": arrow,
            }
            if metric == "sleep_seconds":
                # Sleep renders as "7h 33m", not raw seconds or format_duration's
                # "7:33:00" run-duration shape — units.format_hm is the single
                # source (brief_planner._hm delegates to it), so the brief's
                # grounding pool and this snapshot row agree by construction.
                row["value_formatted"] = units.format_hm(value)
                row["baseline_formatted"] = units.format_hm(base_val)
            rows.append(row)
            continue

        if metric in _TREND_METRICS:
            series = [r[metric] for r in window_rows if r.get(metric) is not None]
            rows.append({
                "metric": metric,
                "value": value,
                "treatment": "trend_arrow",
                "arrow": _slope_arrow(series),
            })
            continue

        rows.append({"metric": metric, "value": value, "treatment": "raw"})

    return rows


def _training_load(baseline: dict[str, Any] | None, today: str) -> dict[str, Any]:
    """CTL/ATL/TSB from the latest baselines row + a plain-English read.

    Carries the row's own ``as_of`` date and ``baseline_stale_days`` (days
    between ``today`` and that row, floored at 0) so a downstream surface can
    say "as of Monday" and flag a frozen data frontier. TSB decays daily even
    with zero workouts (ATL's 7-day EWMA fades faster than CTL's 42-day one),
    so a stale baselines row served as *current* reports the wrong freshness
    and zone — the same orphaned-data failure the brief-side
    ``latest_brief_date``/``brief_stale_days`` fields exist to expose.
    """
    if not baseline:
        return {"ctl": None, "atl": None, "tsb": None,
                "as_of": None, "baseline_stale_days": None,
                "interpretation": "no training-load data yet"}
    tsb = baseline.get("tsb")
    as_of = baseline.get("date")
    stale_days: int | None = None
    if as_of:
        try:
            stale_days = max(
                0, (date.fromisoformat(today) - date.fromisoformat(as_of)).days)
        except ValueError:
            stale_days = None
    return {
        "ctl": baseline.get("ctl"),
        "atl": baseline.get("atl"),
        "tsb": tsb,
        "as_of": as_of,
        "baseline_stale_days": stale_days,
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


def _latest_brief_freshness(today: str) -> tuple[str | None, int | None]:
    """``(latest_brief_date, brief_stale_days)`` from the briefings dir.

    Filename-only (``YYYY-MM-DD.json`` sorts lexically == chronologically) —
    no JSON parse, so the snapshot hot path pays a directory listing, not a
    pydantic validation. Surfaces the "orphaned sync" failure the 2026-07-19
    facet review found undetectable from the tool surface: pull advances the
    data frontier but brief generation fails, and until this field existed the
    only way to notice was manually comparing the newest brief's date against
    the newest daily row. ``(None, None)`` when no briefs exist; never raises.
    """
    briefings_dir = briefs.DEFAULT_BRIEFINGS_DIR
    try:
        dates = []
        for p in briefings_dir.glob("*.json"):
            try:
                dates.append(date.fromisoformat(p.stem))
            except ValueError:
                continue  # tmp/junk filename — not a brief, not a poison pill
        if not dates:
            return None, None
        latest = max(dates)
        stale = (date.fromisoformat(today) - latest).days
        return latest.isoformat(), max(0, stale)
    except (OSError, ValueError):
        return None, None


def assemble_status(today: str | None = None) -> dict[str, Any]:
    """Assemble the daily snapshot. Pure read; never raises on an empty DB.

    ``today`` (ISO ``YYYY-MM-DD``) is injectable so callers (fixtures, the brief
    planner) get reproducible output; it defaults to ``date.today()`` so existing
    bare callers are unchanged.

    Returns a dict with keys: ``date``, ``metrics``, ``training_load``,
    ``recent_workouts``, ``user_notes``, ``latest_brief_date``,
    ``brief_stale_days`` (days between ``today`` and the newest saved brief;
    0 = today's brief exists, ``None`` = no briefs on disk — >0 means the
    nightly generation has been failing).

    ``training_load`` additionally carries ``as_of`` (the date of the baselines
    row the CTL/ATL/TSB came from) and ``baseline_stale_days`` (days between
    ``today`` and that row; 0 = current, ``None`` = no baselines yet). A frozen
    data frontier makes the served TSB/zone drift from reality, so this is the
    baselines-side mirror of the brief-staleness fields above.
    """
    today = today or date.today().isoformat()
    with db.connect() as conn:
        baseline = _baseline_row(conn, today)
        metrics = _metric_rows(conn, today, baseline)
        training_load = _training_load(baseline, today)
        recent_workouts = _recent_workouts(conn)

    user_notes = [n.text for n in notes.read_notes() if n.text]
    latest_brief_date, brief_stale_days = _latest_brief_freshness(today)

    return {
        "date": today,
        "metrics": metrics,
        "training_load": training_load,
        "recent_workouts": recent_workouts,
        "user_notes": user_notes,
        "latest_brief_date": latest_brief_date,
        "brief_stale_days": brief_stale_days,
    }
