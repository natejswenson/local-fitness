#!/usr/bin/env python
"""Golden fabricated-DB fixtures for the brief eval harness.

Each fixture is a fully-fabricated SQLite DB (NEVER derived from Nate's real
Garmin data — see CLAUDE.md) that puts the brief composer in a specific,
trigger-relevant state. They span the brief's documented decision surface
(`agent/prompts.py` Step-2 mandates) so a baseline capture and the Phase-3
shadow-run exercise every branch the prompt can take:

  green_light       — fresh, recovered, CTL climbing  → positive "push" workout
  sliding_fitness   — CTL falling, long run gap        → critical conditioning
  fatigued_recovery — RHR elevated, sleep crashed, TSB deep negative → caution
  missed_steps      — yesterday + 7-day avg under goal → steps caution/critical
  taper_plan        — active half-marathon plan, race close, easy session today
  sparse            — near-empty DB                    → valid (shorter) brief, no raise

The builder is **deterministic**: for a fixed ``(scenario, today)`` it writes
byte-identical row contents (no RNG, no wall-clock) so fixtures are reproducible
and the Phase-2 ``assemble_brief_context`` determinism test has stable input.

Usage (as a library):
    from eval_fixtures import build_fixture_db, SCENARIOS
    path = build_fixture_db("green_light", dest_dir / "fitness.db", today=date(2026, 6, 26))
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from local_fitness import db

# History window seeded behind ``today`` (inclusive of today at offset 0).
_WINDOW_DAYS = 30
# Plan/steps goal used by every fixture's settings row.
_STEP_GOAL = 10000
_USER_NAME = "Nate"

SCENARIOS = (
    "green_light",
    "sliding_fitness",
    "fatigued_recovery",
    "missed_steps",
    "taper_plan",
    "sparse",
)


# --- per-scenario daily-metrics curves ------------------------------------
# Each returns the daily_metrics for a given day-offset ``d`` (0 = today,
# increasing back in time). Pure arithmetic on ``d`` → deterministic.

def _green_light_day(d: int) -> dict:
    return {
        "rhr": 48 + (d % 3),                 # sits below the 53 baseline
        "sleep_seconds": 27000 - (d % 4) * 300,
        "sleep_score": 86 - (d % 5),
        "avg_stress": 18 + (d % 4),
        "body_battery_max": 82 - (d % 6),
        "body_battery_min": 25 + (d % 3),
        "steps": 11000 - (d % 7) * 120,      # comfortably over the 10k goal
        "vo2_max": 47.0,
        "intensity_minutes_moderate": 40,
        "intensity_minutes_vigorous": 20,
    }


def _sliding_day(d: int) -> dict:
    # Recovery is fine; the story is conditioning sliding (handled via baselines
    # + a 6-day run gap below). Steps a touch low but not the lead.
    return {
        "rhr": 52 + (d % 2),
        "sleep_seconds": 26400 - (d % 4) * 250,
        "sleep_score": 80 - (d % 4),
        "avg_stress": 22 + (d % 5),
        "body_battery_max": 76 - (d % 5),
        "body_battery_min": 22 + (d % 3),
        "steps": 7800 - (d % 6) * 100,
        "vo2_max": 46.0,
        "intensity_minutes_moderate": 20,
        "intensity_minutes_vigorous": 5,
    }


def _fatigued_day(d: int) -> dict:
    # RHR elevated for the last several days; sleep crashed last night; body
    # battery topping low the last three nights.
    elevated = 58 if d <= 3 else 53
    crashed_sleep = 19800 if d == 0 else 25200 - (d % 4) * 200
    low_bb = (38, 42, 31)
    return {
        "rhr": elevated + (d % 2),
        "sleep_seconds": crashed_sleep,
        "sleep_score": 58 if d == 0 else 74 - (d % 4),
        "avg_stress": 46 + (d % 6),          # 7-day avg lands above 40
        "body_battery_max": low_bb[d] if d < 3 else 70 - (d % 5),
        "body_battery_min": 12 + (d % 3),
        "steps": 8200 - (d % 6) * 90,
        "vo2_max": 46.0,
        "intensity_minutes_moderate": 15,
        "intensity_minutes_vigorous": 5,
    }


def _missed_steps_day(d: int) -> dict:
    # Steps well under goal yesterday and across the week; recovery + fitness
    # otherwise unremarkable so steps is the actionable story.
    return {
        "rhr": 51 + (d % 2),
        "sleep_seconds": 26100 - (d % 4) * 200,
        "sleep_score": 79 - (d % 4),
        "avg_stress": 24 + (d % 5),
        "body_battery_max": 75 - (d % 5),
        "body_battery_min": 21 + (d % 3),
        "steps": 4200 + (d % 5) * 250,       # ~4.2k–5.2k, well under 10k
        "vo2_max": 46.0,
        "intensity_minutes_moderate": 18,
        "intensity_minutes_vigorous": 6,
    }


def _taper_day(d: int) -> dict:
    # Recovery green so the workout call rides the plan, not a red-flag override.
    return {
        "rhr": 49 + (d % 2),
        "sleep_seconds": 27600 - (d % 4) * 200,
        "sleep_score": 85 - (d % 4),
        "avg_stress": 19 + (d % 4),
        "body_battery_max": 84 - (d % 5),
        "body_battery_min": 26 + (d % 3),
        "steps": 10400 - (d % 6) * 110,
        "vo2_max": 48.0,
        "intensity_minutes_moderate": 35,
        "intensity_minutes_vigorous": 22,
    }


# scenario -> (daily-curve fn, baselines row for today, activities, has_plan)
# baselines row keys map straight to the baselines table columns.
_BASELINES = {
    "green_light": dict(
        rhr_60day_mean=53.0, rhr_60day_sd=2.0, body_battery_max_60day_mean=80.0,
        body_battery_min_60day_mean=24.0, sleep_seconds_60day_mean=26800.0,
        sleep_seconds_60day_sd=2400.0, stress_60day_mean=22.0,
        ctl=14.2, atl=11.0, tsb=3.2,
    ),
    "sliding_fitness": dict(
        rhr_60day_mean=52.0, rhr_60day_sd=2.0, body_battery_max_60day_mean=76.0,
        body_battery_min_60day_mean=22.0, sleep_seconds_60day_mean=26200.0,
        sleep_seconds_60day_sd=2400.0, stress_60day_mean=24.0,
        ctl=9.1, atl=6.0, tsb=3.1,           # CTL down ~30% off the 13 it held
    ),
    "fatigued_recovery": dict(
        rhr_60day_mean=52.0, rhr_60day_sd=2.0, body_battery_max_60day_mean=72.0,
        body_battery_min_60day_mean=20.0, sleep_seconds_60day_mean=26900.0,
        sleep_seconds_60day_sd=2400.0, stress_60day_mean=30.0,
        ctl=18.0, atl=40.0, tsb=-22.0,
    ),
    "missed_steps": dict(
        rhr_60day_mean=51.0, rhr_60day_sd=2.0, body_battery_max_60day_mean=75.0,
        body_battery_min_60day_mean=21.0, sleep_seconds_60day_mean=26100.0,
        sleep_seconds_60day_sd=2400.0, stress_60day_mean=24.0,
        ctl=12.5, atl=12.0, tsb=0.5,
    ),
    "taper_plan": dict(
        rhr_60day_mean=51.0, rhr_60day_sd=2.0, body_battery_max_60day_mean=82.0,
        body_battery_min_60day_mean=25.0, sleep_seconds_60day_mean=27200.0,
        sleep_seconds_60day_sd=2400.0, stress_60day_mean=20.0,
        ctl=42.0, atl=33.0, tsb=9.0,
    ),
}

_CURVES = {
    "green_light": _green_light_day,
    "sliding_fitness": _sliding_day,
    "fatigued_recovery": _fatigued_day,
    "missed_steps": _missed_steps_day,
    "taper_plan": _taper_day,
}

# scenario -> list of (days_ago, activity_type, aerobic_te, distance_m, load).
# A 6-day run gap in sliding_fitness fires the "5+ days since last run" trigger.
_ACTIVITIES = {
    "green_light": [
        (1, "running", 3.4, 9600, 78.0),
        (3, "running", 2.6, 6400, 52.0),
        (5, "running", 3.1, 8000, 64.0),
        (8, "running", 2.4, 5600, 44.0),
    ],
    "sliding_fitness": [
        (6, "running", 1.2, 4800, 28.0),     # last run 6 days ago, zone-2 filler
        (13, "running", 1.4, 5200, 30.0),
    ],
    "fatigued_recovery": [
        (1, "running", 4.2, 16000, 142.0),   # yesterday's hard long run
        (3, "running", 2.8, 8000, 60.0),
        (4, "running", 3.0, 9000, 66.0),
    ],
    "missed_steps": [
        (2, "running", 2.6, 7000, 54.0),
        (6, "running", 2.4, 6400, 48.0),
    ],
    "taper_plan": [
        (1, "running", 2.2, 6400, 40.0),     # taper-week easy session
        (3, "running", 3.0, 12000, 88.0),
        (6, "running", 3.6, 19000, 120.0),   # last big long run before taper
    ],
}


def _insert_daily(conn, day: str, m: dict) -> None:
    conn.execute(
        "INSERT INTO daily_metrics (date, sleep_seconds, sleep_score, rhr, "
        "avg_stress, body_battery_min, body_battery_max, steps, vo2_max, "
        "intensity_minutes_moderate, intensity_minutes_vigorous) "
        "VALUES (:date, :sleep_seconds, :sleep_score, :rhr, :avg_stress, "
        ":body_battery_min, :body_battery_max, :steps, :vo2_max, "
        ":intensity_minutes_moderate, :intensity_minutes_vigorous)",
        {"date": day, **m},
    )


def _seed_taper_plan(conn, today: date) -> None:
    """An active half-marathon plan with a race ~10 days out and an easy
    session scheduled for today (recovery is green, so the workout takeaway
    rides the plan rather than overriding it)."""
    race = (today + timedelta(days=10)).isoformat()
    created = (today - timedelta(days=40)).isoformat()
    cur = conn.execute(
        "INSERT INTO training_plans (status, goal_type, goal_distance_m, "
        "race_date, target_time_seconds, title, created_at, committed_at) "
        "VALUES ('active', 'half', 21097.5, ?, 6420, "
        "'Sub-1:47 Half', ?, ?)",
        (race, created, created),
    )
    plan_id = cur.lastrowid
    # A graded session two days back (done), today's easy 5k, and an upcoming
    # race-day row — enough for the plan-aware workout takeaway to anchor on.
    rows = [
        (today - timedelta(days=2), 5, "tempo", 8000.0, 300.0, 2400,
         "20min tempo at half-marathon effort"),
        (today, 6, "easy", 5000.0, None, None, "Easy 5k, conversational pace"),
        (today + timedelta(days=10), 7, "race", 21097.5, 285.0, None,
         "RACE DAY — sub-1:47 half"),
    ]
    for d, wk, typ, dist, pace, dur, desc in rows:
        conn.execute(
            "INSERT INTO plan_workouts (plan_id, date, seq, week_index, type, "
            "target_distance_m, target_pace_sec_per_km, target_duration_sec, "
            "description) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)",
            (plan_id, d.isoformat(), wk, typ, dist, pace, dur, desc),
        )


def build_fixture_db(scenario: str, dest: Path, *, today: date | None = None) -> Path:
    """Write a fabricated SQLite DB for ``scenario`` at ``dest``; return ``dest``.

    ``today`` defaults to ``date.today()`` so the live composer (which reads
    ``date.today()`` internally until Phase 2 threads it) sees coherent recent
    data. Pass a fixed ``today`` for byte-stable fixtures in tests.
    """
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}; expected one of {SCENARIOS}")
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

        if scenario == "sparse":
            # Just two days of bare metrics, no baselines/activities/plan — the
            # empty-ish edge case. The composer must still produce a valid brief.
            for d in (0, 1):
                day = (today - timedelta(days=d)).isoformat()
                _insert_daily(conn, day, {
                    "rhr": 52, "sleep_seconds": 26000, "sleep_score": 78,
                    "avg_stress": 24, "body_battery_min": 20, "body_battery_max": 74,
                    "steps": 6000, "vo2_max": 46.0,
                    "intensity_minutes_moderate": 10, "intensity_minutes_vigorous": 3,
                })
            return dest

        curve = _CURVES[scenario]
        for d in range(_WINDOW_DAYS):
            day = (today - timedelta(days=d)).isoformat()
            _insert_daily(conn, day, curve(d))

        bl = _BASELINES[scenario]
        conn.execute(
            "INSERT INTO baselines (date, rhr_60day_mean, rhr_60day_sd, "
            "body_battery_max_60day_mean, body_battery_min_60day_mean, "
            "sleep_seconds_60day_mean, sleep_seconds_60day_sd, stress_60day_mean, "
            "ctl, atl, tsb) VALUES (:date, :rhr_60day_mean, :rhr_60day_sd, "
            ":body_battery_max_60day_mean, :body_battery_min_60day_mean, "
            ":sleep_seconds_60day_mean, :sleep_seconds_60day_sd, :stress_60day_mean, "
            ":ctl, :atl, :tsb)",
            {"date": today.isoformat(), **bl},
        )

        for i, (days_ago, typ, te, dist, load) in enumerate(_ACTIVITIES[scenario], start=1):
            adate = (today - timedelta(days=days_ago)).isoformat()
            conn.execute(
                "INSERT INTO activities (activity_id, date, start_time, "
                "activity_type, activity_name, duration_seconds, distance_meters, "
                "avg_hr, training_load, aerobic_te) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (i, adate, adate + "T07:00:00", typ, "Run",
                 int(dist / 3.0), dist, 150, load, te),
            )

        if scenario == "taper_plan":
            _seed_taper_plan(conn, today)

    return dest


if __name__ == "__main__":  # pragma: no cover - manual smoke
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        for s in SCENARIOS:
            p = build_fixture_db(s, Path(tmp) / s / "fitness.db", today=date(2026, 6, 26))
            print(f"{s}: {p} ({p.stat().st_size} bytes)")
