#!/usr/bin/env python
"""Synthetic multi-year fixture DB for the MCP-tool perf benchmark suite.

``tests/test_perf_benchmarks.py`` needs a **realistic-scale** DB — years of
daily_metrics/activities/baselines history plus an active training plan — to
make the connection-count and unbounded-query fixes in
``docs/plans/2026-07-09-mcp-speed-and-ui-retirement-design.md`` Part A
observable. The existing ``eval_fixtures.py`` scenarios are deliberately
small (~30 days) and adequate for brief-quality evals, but too small by
construction to expose a query that scans the whole table.

Deterministic, like ``eval_fixtures.py``: every value is pure arithmetic on
the day offset, no RNG, no wall-clock — a fixed ``today`` produces a
byte-identical DB so benchmark runs are comparable across machines/CI runs.
Per CLAUDE.md, this is FABRICATED data, never derived from ``data/fitness.db``.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from local_fitness import db

#: history length — long enough that an unbounded query over `activities` or
#: `daily_metrics` is measurably slower than a bounded one.
YEARS = 3
DAYS = 365 * YEARS
_STEP_GOAL = 10000
_USER_NAME = "Nate"

# A run every 2 days -> ~1.5 years of run history feeding `_compute_signals`'s
# activities query (unbounded pre-fix; bounded to 35 days post-fix).
_RUN_EVERY_N_DAYS = 2


def _daily_row(d: int) -> dict:
    """Deterministic, plausible daily_metrics values for day-offset ``d``
    (0 = today, increasing back in time). A slow sinusoid-ish wobble via `%`
    arithmetic keeps values realistic-shaped without true randomness."""
    return {
        "rhr": 50 + (d % 7),
        "sleep_seconds": 26000 + (d % 5) * 300,
        "sleep_score": 75 + (d % 10),
        "avg_stress": 22 + (d % 8),
        "body_battery_max": 78 - (d % 9),
        "body_battery_min": 22 + (d % 4),
        "steps": 9000 + (d % 11) * 150,
        "vo2_max": 46.0 + (d % 3) * 0.2,
        "intensity_minutes_moderate": 20 + (d % 6),
        "intensity_minutes_vigorous": 8 + (d % 4),
    }


def _baseline_row(d: int) -> dict:
    """Deterministic CTL/ATL/TSB, drifting slowly so `_compute_signals`'s
    14-day CTL-%-change lookback has real (non-zero) deltas to compute."""
    ctl = 20.0 + ((DAYS - d) % 40) * 0.3
    atl = 15.0 + ((DAYS - d) % 20) * 0.4
    return {
        "rhr_60day_mean": 52.0, "rhr_60day_sd": 2.5,
        "body_battery_max_60day_mean": 76.0, "body_battery_min_60day_mean": 23.0,
        "sleep_seconds_60day_mean": 26500.0, "sleep_seconds_60day_sd": 2200.0,
        "stress_60day_mean": 25.0,
        "ctl": round(ctl, 1), "atl": round(atl, 1), "tsb": round(ctl - atl, 1),
    }


def _seed_plan(conn, today: date) -> None:
    """An active marathon-training plan spanning most of the fixture's
    history: a race ~14 weeks out, one prescribed workout roughly every other
    day back to plan creation, so get_training_plan_progress/_build_plan_section
    have a realistically long ``workouts`` list to grade, not a handful."""
    race = (today + timedelta(weeks=14)).isoformat()
    created = (today - timedelta(weeks=10)).isoformat()
    cur = conn.execute(
        "INSERT INTO training_plans (status, goal_type, goal_distance_m, "
        "race_date, target_time_seconds, title, created_at, committed_at) "
        "VALUES ('active', 'marathon', 42195.0, ?, 13800, "
        "'Sub-3:50 Marathon', ?, ?)",
        (race, created, created),
    )
    plan_id = cur.lastrowid

    types = ("easy", "tempo", "long", "easy", "rest", "easy", "long")
    for i, day_offset in enumerate(range(-70, 98, 2)):  # 10 weeks back, 14 ahead
        d = today + timedelta(days=day_offset)
        wtype = types[i % len(types)]
        week_index = (day_offset + 70) // 7 + 1
        if wtype == "rest":
            dist, pace, dur, desc = None, None, None, "Rest day"
        elif wtype == "long":
            dist, pace, dur, desc = 18000.0 + (i % 5) * 1000, 330.0, None, "Long run"
        elif wtype == "tempo":
            dist, pace, dur, desc = 8000.0, 300.0, 2400, "Tempo effort"
        else:
            dist, pace, dur, desc = 6000.0 + (i % 4) * 500, 340.0, None, "Easy run"
        conn.execute(
            "INSERT INTO plan_workouts (plan_id, date, seq, week_index, type, "
            "target_distance_m, target_pace_sec_per_km, target_duration_sec, "
            "description) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (plan_id, d.isoformat(), i, week_index, wtype, dist, pace, dur, desc),
        )


def build_perf_fixture_db(dest: Path, *, today: date | None = None) -> Path:
    """Write a fabricated, multi-year SQLite DB at ``dest`` with an active
    plan; return ``dest``. Deterministic for a fixed ``today``."""
    today = today or date.today()
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    db.init_schema(dest)

    with db.connect(dest) as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES ('user_name', ?)", (_USER_NAME,))
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('daily_step_goal', ?)",
            (str(_STEP_GOAL),),
        )

        for d in range(DAYS):
            day = (today - timedelta(days=d)).isoformat()
            conn.execute(
                "INSERT INTO daily_metrics (date, sleep_seconds, sleep_score, rhr, "
                "avg_stress, body_battery_min, body_battery_max, steps, vo2_max, "
                "intensity_minutes_moderate, intensity_minutes_vigorous) "
                "VALUES (:date, :sleep_seconds, :sleep_score, :rhr, :avg_stress, "
                ":body_battery_min, :body_battery_max, :steps, :vo2_max, "
                ":intensity_minutes_moderate, :intensity_minutes_vigorous)",
                {"date": day, **_daily_row(d)},
            )
            conn.execute(
                "INSERT INTO baselines (date, rhr_60day_mean, rhr_60day_sd, "
                "body_battery_max_60day_mean, body_battery_min_60day_mean, "
                "sleep_seconds_60day_mean, sleep_seconds_60day_sd, stress_60day_mean, "
                "ctl, atl, tsb) VALUES (:date, :rhr_60day_mean, :rhr_60day_sd, "
                ":body_battery_max_60day_mean, :body_battery_min_60day_mean, "
                ":sleep_seconds_60day_mean, :sleep_seconds_60day_sd, :stress_60day_mean, "
                ":ctl, :atl, :tsb)",
                {"date": day, **_baseline_row(d)},
            )

        activity_id = 1
        for d in range(0, DAYS, _RUN_EVERY_N_DAYS):
            day = (today - timedelta(days=d)).isoformat()
            dist_m = 6000 + (d % 9) * 800
            conn.execute(
                "INSERT INTO activities (activity_id, date, start_time, "
                "activity_type, activity_name, duration_seconds, distance_meters, "
                "avg_hr, training_load, aerobic_te) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (activity_id, day, day + "T07:00:00", "running", "Run",
                 int(dist_m / 3.0), dist_m, 148 + (d % 10), 40.0 + (d % 15) * 3, 2.0 + (d % 4) * 0.3),
            )
            activity_id += 1

        _seed_plan(conn, today)

    return dest


if __name__ == "__main__":  # pragma: no cover - manual smoke
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        p = build_perf_fixture_db(Path(tmp) / "perf" / "fitness.db", today=date(2026, 7, 9))
        print(f"perf fixture: {p} ({p.stat().st_size} bytes)")
