#!/usr/bin/env python
"""Synthetic multi-year fixture DB for the MCP-tool perf benchmark suite.

``tests/test_perf_benchmarks.py`` needs a **realistic-scale** DB — years of
daily_metrics/activities/baselines history plus an active training plan — to
make the connection-count and unbounded-query fixes in
``docs/plans/2026-07-09-mcp-speed-and-ui-retirement-design.md`` Part A
observable. The existing ``eval_fixtures.py`` scenarios are deliberately
small (~30 days) and adequate for brief-quality evals, but too small by
construction to expose a query that scans the whole table.

Two builders, two databases:

* ``build_perf_fixture_db`` — the multi-year brief/plan fixture the five
  baselined benchmarks run against. **Its output is frozen by the committed
  baseline**: any change to the rows it writes shifts those timings against a
  15%-of-min gate that can only be rebaselined on ubuntu CI, so treat it as
  append-only-with-measurement, not as a scratchpad.
* ``build_report_card_fixture_db`` — a smaller, separate DB shaped for the
  ``workout_report_card`` path (paced runs and walks, splits, HR zones, a plan
  with an HR cap). Separate on purpose; see the comment above ``_RC_DAYS``.

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

# --- report-card fixture -----------------------------------------------------
#
# A SEPARATE database, not extra rows in the one above, and that separation is
# load-bearing rather than tidiness. The report-card path needs paces on its
# activities (run-vs-walk is decided by measured pace everywhere since 0.27.0 —
# a paceless row has an UNKNOWN mode, which skips `rolling_reference`'s
# locomotion partition entirely), but `activities.avg_pace_sec_per_km` is read
# by `plans.best_recent_effort`, which EXCLUDES paceless rows today. Adding
# paces to the shared fixture therefore hands the existing benchmarks rows they
# currently skip: measured locally 2026-08-02, `get_training_plan_status` moved
# +7.2% on min and `get_training_plan_progress` +4.4%, against a
# `--benchmark-compare-fail=min:15%` gate whose baseline may only be recaptured
# on ubuntu CI. Spending half the budget on fixture noise — on top of the
# documented runner-fleet drift — would leave every later PR one slow draw from
# a false failure. A distinct DB file costs one extra build and moves nothing.

#: Long enough for a full 60-day `REFERENCE_WINDOW_DAYS` pool with room to
#: spare; there is no unbounded-scan claim to prove here, so the multi-year
#: scale of the shared fixture would only slow the suite down.
_RC_DAYS = 180

#: Comfortably under `interpret.RUN_PACE_CEILING_SEC_PER_MI`, so these measure
#: as running efforts.
_RC_RUN_PACE_SEC_PER_KM = 300.0

#: Walking-desk pace. Deliberately logged as `treadmill_running`, which is the
#: real mislabel from live data (CLAUDE.md) — this is what gives
#: `rolling_reference`'s mode filter something to actually exclude, and what
#: makes `excluded_other_mode` non-zero on the benchmarked card.
_RC_WALK_PACE_SEC_PER_KM = 900.0

#: Seconds in HR zones 1-5. Shaped like a real mostly-aerobic run so
#: `zone_summary` has a dominant zone to report rather than a flat tie.
_RC_ZONE_SECONDS = (120, 900, 1100, 240, 60)

#: Splits are written for the trailing N days only, mirroring the real DB's
#: shape: the daily-sync ingest writes them, the historical backfill never did,
#: so ~87% of live rows have neither. A fixture where every activity had splits
#: would benchmark a case that does not exist.
_RC_DETAIL_DAYS = 60


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


def build_report_card_fixture_db(
    dest: Path, *, today: date | None = None
) -> tuple[Path, int]:
    """Fabricated DB shaped for the ``workout_report_card`` read path.

    Returns ``(path, activity_id)`` — the id of the card to grade, which is
    today's run. Deterministic for a fixed ``today``, same as its sibling.

    What it deliberately contains, because each one is a branch the report card
    would otherwise never execute on a fixture:

    * **paced runs AND paced walks**, the walks logged as ``treadmill_running``
      — so ``rolling_reference`` runs its locomotion partition for real and
      reports a non-zero ``excluded_other_mode``, instead of falling through to
      a type-only comparison the way a paceless fixture does;
    * **more than ``MIN_REFERENCE_ACTIVITIES`` comparable runs in the trailing
      60 days**, so the reference resolves to ``rolling_60d`` and every metric
      grades — an ``insufficient_data`` fixture benchmarks the early return;
    * **per-lap splits with one slow lap**, so ``continuity_ratio`` computes a
      real ratio off a real outlier rather than the degenerate 1.0 a uniform
      set of laps gives, and ``fastest_rep_split`` has qualifying candidates;
    * **HR-zone rows**, so the stimulus block renders its zone line;
    * **an active plan prescribing today's run, with ``target_hr_max`` set**, so
      HR grades against the cap (reference ``"plan"``) — the 0.40.0 path — and
      distance/pace grade plan-referenced under ``PLAN_TIGHTEN``.
    """
    today = today or date.today()
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    db.init_schema(dest)

    graded_id = 1
    with db.connect(dest) as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('user_name', ?)",
            (_USER_NAME,))

        activity_id = graded_id
        for d in range(_RC_DAYS):
            day = (today - timedelta(days=d)).isoformat()
            if d % 2 == 0:
                dist_m = 8000.0 + (d % 7) * 600
                pace = _RC_RUN_PACE_SEC_PER_KM + (d % 40)
                conn.execute(
                    "INSERT INTO activities (activity_id, date, start_time, "
                    "activity_type, activity_name, duration_seconds, "
                    "distance_meters, avg_hr, max_hr, avg_pace_sec_per_km, "
                    "training_load, aerobic_te, anaerobic_te) "
                    "VALUES (?, ?, ?, 'running', 'Run', ?, ?, ?, ?, ?, ?, ?, ?)",
                    (activity_id, day, day + "T07:00:00",
                     int(dist_m * pace / 1000), dist_m, 142 + (d % 12),
                     168 + (d % 8), pace, 80.0 + (d % 20) * 2,
                     2.6 + (d % 4) * 0.2, 1.2 + (d % 3) * 0.2),
                )
                activity_id += 1
            if d % 3 == 0:
                # The walking-desk session Garmin labels as a run.
                dist_m = 2500.0 + (d % 5) * 200
                conn.execute(
                    "INSERT INTO activities (activity_id, date, start_time, "
                    "activity_type, activity_name, duration_seconds, "
                    "distance_meters, avg_hr, avg_pace_sec_per_km, "
                    "training_load, aerobic_te) "
                    "VALUES (?, ?, ?, 'treadmill_running', 'Walk', ?, ?, ?, "
                    "?, ?, ?)",
                    (activity_id, day, day + "T12:00:00",
                     int(dist_m * _RC_WALK_PACE_SEC_PER_KM / 1000), dist_m,
                     92 + (d % 6), _RC_WALK_PACE_SEC_PER_KM, 12.0, 0.6),
                )
                activity_id += 1
            conn.execute(
                "INSERT INTO baselines (date, ctl, atl, tsb) VALUES (?, ?, ?, ?)",
                (day, 42.0, 38.0, 4.0))

        detail_floor = (today - timedelta(days=_RC_DETAIL_DAYS)).isoformat()
        rows = conn.execute(
            "SELECT activity_id, distance_meters, duration_seconds, avg_hr "
            "FROM activities WHERE date >= ? AND activity_type = 'running' "
            "ORDER BY activity_id", (detail_floor,),
        ).fetchall()
        for row in rows:
            laps = max(1, int(row["distance_meters"] // 1609.34))
            lap_seconds = row["duration_seconds"] / laps
            for idx in range(laps):
                slow = 1.35 if idx == 2 else 1.0   # one real outlier lap
                conn.execute(
                    "INSERT INTO activity_splits (activity_id, split_index, "
                    "distance_meters, duration_seconds, avg_hr, "
                    "avg_pace_sec_per_km) VALUES (?, ?, ?, ?, ?, ?)",
                    (row["activity_id"], idx, 1609.34, lap_seconds * slow,
                     row["avg_hr"] + idx,
                     lap_seconds * slow / 1.60934),
                )
            for zone, seconds in enumerate(_RC_ZONE_SECONDS, start=1):
                conn.execute(
                    "INSERT INTO activity_hr_zones (activity_id, zone, "
                    "seconds_in_zone) VALUES (?, ?, ?)",
                    (row["activity_id"], zone, seconds),
                )

        created = (today - timedelta(weeks=6)).isoformat()
        cur = conn.execute(
            "INSERT INTO training_plans (status, goal_type, goal_distance_m, "
            "race_date, target_time_seconds, title, created_at, committed_at) "
            "VALUES ('active', 'half', 21097.5, ?, 6600, 'Sub-1:50 Half', ?, ?)",
            ((today + timedelta(weeks=8)).isoformat(), created, created),
        )
        plan_id = cur.lastrowid
        for day_offset in range(-42, 57):
            d = today + timedelta(days=day_offset)
            conn.execute(
                "INSERT INTO plan_workouts (plan_id, date, seq, week_index, "
                "type, target_distance_m, target_pace_sec_per_km, "
                "target_hr_max, description) "
                "VALUES (?, ?, 1, ?, 'easy', 8000.0, 305.0, 145.0, 'Easy run')",
                (plan_id, d.isoformat(), (day_offset + 42) // 7 + 1),
            )

    return dest, graded_id


if __name__ == "__main__":  # pragma: no cover - manual smoke
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        p = build_perf_fixture_db(Path(tmp) / "perf" / "fitness.db", today=date(2026, 7, 9))
        print(f"perf fixture: {p} ({p.stat().st_size} bytes)")
        rc, aid = build_report_card_fixture_db(
            Path(tmp) / "perf_rc" / "fitness.db", today=date(2026, 7, 9))
        print(f"report-card fixture: {rc} ({rc.stat().st_size} bytes), "
              f"graded activity {aid}")
