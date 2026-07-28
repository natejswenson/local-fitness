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
from .tools import DAILY_NUMERIC_METRICS, PARTIAL_DAY_METRICS

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

# The only columns _metric_rows reads off a daily_metrics row: every metric it
# reports, plus the `date` it slices today's row out of the window with. Derived
# from DAILY_NUMERIC_METRICS rather than hand-listed so the two cannot drift —
# a metric added to that set is fetched here automatically. `SELECT *` used to
# drag each row's raw_json (the preserved ~16 KB Garmin payload) through an
# 8-day window for fields nothing below reads. Sorted for a stable SQL string.
# Interpolated, never parameterized — frozen identifiers from a module constant,
# not user input.
_METRIC_WINDOW_COLUMNS: tuple[str, ...] = ("date",) + tuple(sorted(DAILY_NUMERIC_METRICS))
_METRIC_WINDOW_SELECT = ", ".join(_METRIC_WINDOW_COLUMNS)


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


def _baseline_row_before(conn, today: str) -> dict[str, Any] | None:
    """Latest baselines row STRICTLY BEFORE `today` — the last COMPLETE day's
    training-load state (Fix 9, 2026-07-27).

    ``_baseline_row`` (on/before today) is right for the 60-day MEAN reference
    columns (rhr_60day_mean etc.) — those windows already exclude the target
    day from their own average (``build_baseline_rows`` admits rows with
    ``date < d``), so a mean "as of today" is a legitimate same-day reference.
    CTL/ATL/TSB are different: ``baselines.recompute`` walks forward day by
    day feeding each day's OWN ``training_load`` into the EWMA, so TODAY's row
    assumes today's training_load is whatever has posted so far — 0 on a
    typical morning read, before any activity syncs. Reporting that row as
    "current form" pre-credits a zero-load rest day that hasn't happened:
    measured live, TSB read -12.74 today vs -22.41 yesterday, a swing large
    enough to cross the very-fatigued/fatigued zone boundary, purely because
    no run had posted yet. "Current form" is therefore always the latest row
    with date < today (the standard Banister/TrainingPeaks convention —
    stable across the day, immune to same-day sync order).
    """
    row = conn.execute(
        "SELECT * FROM baselines WHERE date < ? ORDER BY date DESC LIMIT 1",
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
    <metric> IS NOT NULL`` clause.

    Fix 8 (2026-07-27): for a same-day running-tally metric
    (``tools.PARTIAL_DAY_METRICS`` — steps, avg_stress, max_stress,
    active_calories, intensity minutes, body_battery_charged/drained),
    today's value is a PARTIAL number all day (Garmin accumulates it through
    the day), so any DERIVED comparison — a baseline_delta % or a trend_arrow
    slope — anchors on YESTERDAY instead. Measured live: avg_stress read 17
    off 50 overnight samples (00:00-02:27) against a 32 baseline, narrated as
    "-47%, recovery holding" in a 06:30 brief, when every complete day that
    week ran 24-32. Raw-treatment metrics (no comparison to get wrong) keep
    today's live number — a mid-day "how are my steps so far" answer must not
    silently become yesterday's — but carry ``partial_today: true`` so a
    caller knows it's still accumulating."""
    # Window is relative to the passed `today`, NOT wall-clock — so an
    # injected `today` (fixtures / brief_planner) is reproducible.
    cutoff = (date.fromisoformat(today) - timedelta(days=_TREND_WINDOW_DAYS)).isoformat()
    window_rows = [
        dict(r) for r in conn.execute(
            f"SELECT {_METRIC_WINDOW_SELECT} FROM daily_metrics "
            "WHERE date >= ? AND date <= ? ORDER BY date",
            (cutoff, today),
        ).fetchall()
    ]
    today_row = next((r for r in window_rows if r["date"] == today), {})
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    yesterday_row = next((r for r in window_rows if r["date"] == yesterday), {})

    rows: list[dict[str, Any]] = []
    for metric in sorted(DAILY_NUMERIC_METRICS):
        is_partial = metric in PARTIAL_DAY_METRICS
        value = today_row.get(metric)

        if metric in _BASELINE_DELTA_MAP:
            mean_col, _sd_col = _BASELINE_DELTA_MAP[metric]
            base_val = baseline.get(mean_col) if baseline else None
            cmp_value = yesterday_row.get(metric) if is_partial else value
            delta_pct: float | None = None
            arrow: str | None = None
            if cmp_value is not None and base_val:
                delta_pct = round((cmp_value - base_val) / base_val * 100, 1)
                arrow = _arrow(cmp_value - base_val)
            row = {
                "metric": metric,
                "value": cmp_value,
                "treatment": "baseline_delta",
                "baseline": base_val,
                "delta_pct": delta_pct,
                "arrow": arrow,
            }
            if is_partial:
                row["partial_today_excluded"] = True
            if metric == "sleep_seconds":
                # Sleep renders as "7h 33m", not raw seconds or format_duration's
                # "7:33:00" run-duration shape — units.format_hm is the single
                # source (brief_planner._hm delegates to it), so the brief's
                # grounding pool and this snapshot row agree by construction.
                row["value_formatted"] = units.format_hm(cmp_value)
                row["baseline_formatted"] = units.format_hm(base_val)
            rows.append(row)
            continue

        if metric in _TREND_METRICS:
            cmp_value = yesterday_row.get(metric) if is_partial else value
            series = [
                r[metric] for r in window_rows
                if r.get(metric) is not None and (not is_partial or r["date"] != today)
            ]
            row = {
                "metric": metric,
                "value": cmp_value,
                "treatment": "trend_arrow",
                "arrow": _slope_arrow(series),
            }
            if is_partial:
                row["partial_today_excluded"] = True
            rows.append(row)
            continue

        row = {"metric": metric, "value": value, "treatment": "raw"}
        if is_partial and value is not None:
            row["partial_today"] = True
        rows.append(row)

    return rows


def _projected_end_of_day(today_row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Today's OWN baselines row, if one exists, labelled as what it is: a
    same-day PROJECTION that assumes no further training_load posts today —
    never the "current form" read (see ``_training_load``)."""
    if not today_row:
        return None
    tsb = today_row.get("tsb")
    return {
        "ctl": today_row.get("ctl"),
        "atl": today_row.get("atl"),
        "tsb": tsb,
        "interpretation": _tsb_interpretation(tsb),
    }


def _training_load(
    baseline: dict[str, Any] | None, today: str,
    current_form: dict[str, Any] | None = None, today_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """CTL/ATL/TSB "current form" + a plain-English read, plus today's own
    row exposed separately as an explicit projection.

    Fix 9 (2026-07-27): ``baselines.recompute`` walks forward day by day
    feeding each day's OWN ``training_load`` into the CTL/ATL EWMA, so
    TODAY's baselines row assumes today's training_load is whatever has
    posted so far — 0 on a typical morning read, before any activity syncs.
    Reporting that row as "current form" pre-credits a zero-load rest day
    that hasn't happened: measured live, TSB read -12.74 today vs -22.41
    yesterday, crossing the very-fatigued/fatigued zone boundary purely
    because no run had posted yet, a swing that reverses the moment the
    day's session logs. So ``ctl``/``atl``/``tsb`` here are ALWAYS the last
    COMPLETE day's values (``current_form`` — see ``_baseline_row_before``,
    the standard Banister/TrainingPeaks convention, stable across the day);
    today's own row, if any, rides along under ``projected_end_of_day``
    rather than being silently dropped.

    ``as_of``/``baseline_stale_days`` are DELIBERATELY UNCHANGED — they still
    measure the true data-pipeline frontier from ``baseline`` (latest row
    on/before today), not the current-form date, so a caller can still tell
    "the pipeline hasn't run in 3 days" (a real alarm) apart from "current
    form is, by design, always at least one day behind" (never an alarm).
    Conflating the two would either kill the frozen-data-frontier warning
    (if anchored to current_form, which is never `today`) or bolt today's
    projected TSB onto an as_of date it doesn't belong to.
    """
    projected = _projected_end_of_day(today_row)
    # as_of/baseline_stale_days are computed from `baseline` UNCONDITIONALLY —
    # this is the true pipeline-freshness signal and must not disappear just
    # because current_form (a DIFFERENT row, always < today) is unavailable.
    # A single-day-old DB (baseline present, no prior day yet) is "no
    # training-load data yet" for current form but is NOT a stale pipeline.
    as_of = baseline.get("date") if baseline else None
    stale_days: int | None = None
    if as_of:
        try:
            stale_days = max(
                0, (date.fromisoformat(today) - date.fromisoformat(as_of)).days)
        except ValueError:
            stale_days = None
    if not current_form:
        return {"ctl": None, "atl": None, "tsb": None,
                "as_of": as_of, "baseline_stale_days": stale_days,
                "interpretation": "no training-load data yet",
                "current_form_date": None,
                "projected_end_of_day": projected}
    tsb = current_form.get("tsb")
    return {
        "ctl": current_form.get("ctl"),
        "atl": current_form.get("atl"),
        "tsb": tsb,
        "as_of": as_of,
        "baseline_stale_days": stale_days,
        "interpretation": _tsb_interpretation(tsb),
        "current_form_date": current_form.get("date"),
        "projected_end_of_day": projected,
    }


def _recent_workouts(conn, limit: int = 5) -> list[dict[str, Any]]:
    """Last ~5 workouts with raw fields plus mile/formatted convenience fields
    from units.py. Omits a formatted field when units.py returns None (null or
    zero distance / pace). Mirrors tools._augment_workout's exact field set
    (including the measured ``effort`` read below) — this module duplicates
    the augmentation inline rather than importing it, to avoid a status <->
    tools import cycle."""
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
        # Measured run-vs-walk (interpret.is_running_effort, pace only) — NOT
        # activity_type, which Garmin mislabels (walking-desk sessions log as
        # treadmill_running). Additive only; never filters/excludes a workout.
        mode = interpret.is_running_effort(w.get("avg_pace_sec_per_km"))
        w["effort"] = {True: "run", False: "walk", None: None}[mode]
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
    row the pipeline last wrote — pipeline-freshness signal, NOT the date of
    the reported ctl/atl/tsb) and ``baseline_stale_days`` (days between
    ``today`` and that row; 0 = current, ``None`` = no baselines yet). A frozen
    data frontier makes the served TSB/zone drift from reality, so this is the
    baselines-side mirror of the brief-staleness fields above. ``ctl``/
    ``atl``/``tsb`` themselves are the last COMPLETE day's values (Fix 9,
    2026-07-27 — see ``_training_load``), dated by ``current_form_date``;
    today's own (same-day-projection) row, if any, rides along under
    ``projected_end_of_day`` rather than being reported as current.
    """
    today = today or date.today().isoformat()
    with db.connect() as conn:
        baseline = _baseline_row(conn, today)
        current_form = _baseline_row_before(conn, today)
        today_row = baseline if (baseline and baseline.get("date") == today) else None
        metrics = _metric_rows(conn, today, baseline)
        training_load = _training_load(baseline, today, current_form, today_row)
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
