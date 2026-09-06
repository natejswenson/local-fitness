#!/usr/bin/env python
"""Golden fabricated-DB fixtures for the plan-VERDICT evals.

The sibling of ``tests/evals/report_cards.py``, which does this for the report
card. Every fixture is fully fabricated (NEVER derived from real Garmin data —
see CLAUDE.md); the shapes are modelled on verdicts that actually reached a
brief, but every number here was written by hand.

**Why a second layer when ``tests/test_plans.py`` has 100+ tests.** Those
assert that the grader computes what it says it computes — a fraction, a ladder
boundary, a helper's return. Not one of them could fail while the *answer* was
wrong, which is exactly what happened: for eight releases a tempo day graded
``done`` for any running at all, the brief wrote "tempo hit as prescribed" into
the coach's permanent journal, and a later brief cited it. These fixtures assert
the **verdict a session deserves** — the thing a reader acts on. Same rule
CLAUDE.md already states for the report card: a grade change needs a verdict
eval, not just a unit test.

They run the full path — a real SQLite DB through ``load_activities_by_date``
-> ``build_plan_detail`` — rather than handing ``classify_workout`` a hand-made
day. That is load-bearing: the splits the pace cap reads are attached by the
loader, and a fixture that hand-attached them could not catch the loader
forgetting to.

  tempo_jogged            — THE #242 shape: 7:48/mi prescribed, 10:07/mi run,
                            89% of the prescribed distance covered
  tempo_hit               — the same prescription, executed
  interval_reps_hit       — manual laps: warmup + cooldown drag the run average
                            far off rep pace, the reps are on it
  interval_reps_missed    — the same session run a minute per mile slow
  tempo_short_and_slow    — half the distance AND off the pace
  tempo_no_splits         — the backfilled tail: no splits, so the cap abstains
  quality_walked          — a walking-pad session on a tempo day

The builder is **deterministic**: for a fixed scenario it writes byte-identical
rows (no RNG, no wall-clock).

Usage (as a library):
    from plan_verdicts import SCENARIOS, build_scenario_db, verdict
    v = verdict("tempo_jogged", tmp_path)
"""
from __future__ import annotations

from pathlib import Path

from local_fitness import db, plans

MILE_M = 1609.344
#: Every scenario prescribes and grades this one day; the frontier sits after
#: it so nothing is held ``pending``.
GRADED_DATE = "2026-07-01"
FRONTIER = "2026-07-08"
RACE_DATE = "2026-09-14"


def _per_km(sec_per_mile: float) -> float:
    """A pace written the way a runner says it (min/mi) in sec/km, the column's
    unit. Every pace in this file is written as sec/mile and converted here, so
    a fixture reads as the prescription a coach would actually give."""
    return sec_per_mile / (MILE_M / 1000.0)


SCENARIOS = (
    "tempo_jogged",
    "tempo_hit",
    "interval_reps_hit",
    "interval_reps_missed",
    "tempo_short_and_slow",
    "tempo_no_splits",
    "quality_walked",
)


#: The verdict each scenario must produce, exactly. A verdict is three-valued,
#: not a continuous score, so there is no bound to state — the contract IS the
#: word, and a rubric that returns a different one is wrong rather than
#: recalibrated.
#:
#: Adding a scenario without an entry here fails
#: ``test_every_scenario_declares_a_verdict``, which is what keeps this table
#: the single place a plan verdict's expected behaviour is written down.
EXPECTED_VERDICTS: dict[str, dict] = {
    "tempo_jogged": {
        "verdict": "missed",
        "why": "THE #242 guard. A tempo prescribing 3x1mi at 7:48/mi, run at "
               "10:07/mi — 2:19/mi slower than the reps asked for, at an easy "
               "heart rate. It covered 89% of the prescribed distance, which "
               "is why the distance ladder alone (the issue's non-optional "
               "half) still calls it done and why the pace cap is the fix. "
               "Graded done for eight releases, and the brief wrote 'tempo hit "
               "as prescribed' into the coach's journal, where a later brief "
               "cited it.",
    },
    "tempo_hit": {
        "verdict": "done",
        "why": "The same prescription, executed at the pace it asked for. "
               "Without this the change is a one-way ratchet that marks every "
               "quality day down — the 0.55.0 prescribed-walk inversion, where "
               "obeying the plan scored worse than ignoring it.",
    },
    "interval_reps_hit": {
        "verdict": "done",
        "why": "2 mi warmup, 4x800m at the prescribed 6:58/mi, 2 mi cooldown. "
               "The run AVERAGE is 10:42/mi by construction, so a cap reading "
               "the average fails a session that was executed correctly — the "
               "trap report_card's quality branch was written to escape, and "
               "the reason both surfaces share one rep selector.",
    },
    "interval_reps_missed": {
        "verdict": "missed",
        "why": "The same manually-lapped session with every rep a minute per "
               "mile slow. Pinned beside interval_reps_hit so the rep selector "
               "is proven to discriminate rather than to always find something "
               "fast enough somewhere in a lapped workout.",
    },
    "tempo_short_and_slow": {
        "verdict": "missed",
        "why": "Half the prescribed distance AND off the rep pace. Both axes "
               "fail, so this is the case that would still pass if the cap "
               "silently replaced the volume verdict instead of capping it.",
    },
    "tempo_no_splits": {
        "verdict": "done",
        "why": "The backfilled tail: the historical import never wrote splits, "
               "so there is no rep to read and the cap must abstain rather "
               "than fail a session it cannot measure. Same abstention the "
               "card's quality-pace metric makes. This day covered its "
               "distance, so volume alone says done and that is what stands.",
    },
    "quality_walked": {
        "verdict": "missed",
        "why": "A 16:00/mi walking-pad session that Garmin labels "
               "`treadmill_running`. `_ran`'s pace gate has excluded these "
               "from quality-day volume since 0.27.0; the restructure runs "
               "through that gate, and its splits must not reach the cap "
               "either.",
    },
}


# --- the prescription + what was actually done, per scenario ----------------
# (plan workout, activity, splits as (distance_m, sec_per_mile) pairs)

_TEMPO_PLAN = {
    "type": "tempo", "target_distance_m": 4.5 * MILE_M,
    "target_pace_sec_per_km": _per_km(468.0),        # 7:48/mi
    "description": "3x1mi @ 7:48/mi with 2min jog recovery",
}
_INTERVAL_PLAN = {
    "type": "interval", "target_distance_m": 6 * MILE_M,
    "target_pace_sec_per_km": _per_km(418.0),        # 6:58/mi
    "description": "2mi w/u, 4x800m @ 6:58/mi, 2mi c/d",
}


def _run(distance_m: float, duration_s: int, sec_per_mile: float,
         activity_type: str = "running") -> dict:
    return {"activity_type": activity_type, "distance_meters": distance_m,
            "duration_seconds": duration_s,
            "avg_pace_sec_per_km": _per_km(sec_per_mile)}


def _mile_splits(*paces: float) -> list[tuple[float, float]]:
    return [(MILE_M, p) for p in paces]


_GRADED: dict[str, tuple[dict, dict, list[tuple[float, float]]]] = {
    # 4.01 mi of a 4.5 mi prescription — 89% of target, above DONE_FRACTION —
    # with every mile 2:19/mi or worse off the prescribed rep pace.
    "tempo_jogged": (
        _TEMPO_PLAN, _run(4.01 * MILE_M, 2540, 633.0),
        _mile_splits(625.0, 607.0, 790.0, 648.0),
    ),
    # The same day, done. Distance and structure identical; only the pace moves.
    "tempo_hit": (
        _TEMPO_PLAN, _run(4.5 * MILE_M, 2160, 480.0),
        _mile_splits(500.0, 466.0, 468.0, 464.0),
    ),
    # Manually lapped, so the run average is nowhere near rep pace.
    "interval_reps_hit": (
        _INTERVAL_PLAN, _run(6 * MILE_M, 3852, 642.0),
        [(2 * MILE_M, 660.0)] + [(800.0, 418.0)] * 4 + [(2 * MILE_M, 850.0)],
    ),
    # ...and the same session with the reps a minute per mile slow.
    "interval_reps_missed": (
        _INTERVAL_PLAN, _run(6 * MILE_M, 4032, 672.0),
        [(2 * MILE_M, 660.0)] + [(800.0, 478.0)] * 4 + [(2 * MILE_M, 850.0)],
    ),
    # 2.2 mi of 4.5, and slow with it.
    "tempo_short_and_slow": (
        _TEMPO_PLAN, _run(2.2 * MILE_M, 1430, 650.0),
        _mile_splits(645.0, 655.0),
    ),
    # Distance covered at a jog, but no splits were ever ingested for this row.
    "tempo_no_splits": (
        _TEMPO_PLAN, _run(4.5 * MILE_M, 2925, 650.0), [],
    ),
    # The label lie: a walking-pad session filed as treadmill_running.
    "quality_walked": (
        _TEMPO_PLAN,
        _run(4.5 * MILE_M, 4320, 960.0, activity_type="treadmill_running"),
        _mile_splits(955.0, 960.0, 962.0, 963.0),
    ),
}


def build_scenario_db(scenario: str, dest: Path) -> Path:
    """Write the fabricated DB for ``scenario`` at ``dest``; return ``dest``."""
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}; expected one of {SCENARIOS}")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    db.init_schema(dest)

    workout, activity, splits = _GRADED[scenario]
    with db.connect(dest) as conn:
        cur = conn.execute(
            "INSERT INTO training_plans (status, goal_type, goal_distance_m, "
            "race_date, title, created_at, committed_at) "
            "VALUES ('active', 'half', 21097.5, ?, 'Eval Plan', ?, ?)",
            (RACE_DATE, "2026-06-01T00:00:00", "2026-06-01T00:00:00"),
        )
        conn.execute(
            "INSERT INTO plan_workouts (plan_id, date, seq, week_index, type, "
            "target_distance_m, target_pace_sec_per_km, target_duration_sec, "
            "description) VALUES (?, ?, 1, 5, ?, ?, ?, NULL, ?)",
            (cur.lastrowid, GRADED_DATE, workout["type"],
             workout["target_distance_m"], workout["target_pace_sec_per_km"],
             workout["description"]),
        )
        conn.execute(
            "INSERT INTO activities (activity_id, date, start_time, "
            "activity_type, activity_name, duration_seconds, distance_meters, "
            "avg_pace_sec_per_km) VALUES (1, ?, ?, ?, 'Session', ?, ?, ?)",
            (GRADED_DATE, GRADED_DATE + "T07:00:00", activity["activity_type"],
             activity["duration_seconds"], activity["distance_meters"],
             activity["avg_pace_sec_per_km"]),
        )
        for i, (dist, sec_per_mile) in enumerate(splits):
            pace = _per_km(sec_per_mile)
            conn.execute(
                "INSERT INTO activity_splits (activity_id, split_index, "
                "distance_meters, duration_seconds, avg_hr, avg_pace_sec_per_km) "
                "VALUES (1, ?, ?, ?, 150, ?)",
                (i, dist, round(dist / 1000.0 * pace), pace),
            )
    return dest


def graded_day(scenario: str, tmp_dir: Path) -> dict:
    """Build the scenario and return its ONE graded workout, via the production
    path.

    Deliberately not ``classify_workout`` on a hand-made day: the splits the
    pace cap reads are attached by ``load_activities_by_date``, and a fixture
    that attached them itself could not catch the loader dropping them.
    """
    path = build_scenario_db(scenario, Path(tmp_dir) / scenario / "fitness.db")
    plan = plans.get_active_plan(db_path=path)
    assert plan is not None, f"{scenario}: fixture did not resolve an active plan"
    by_date = plans.load_activities_by_date(GRADED_DATE, GRADED_DATE, db_path=path)
    detail = plans.build_plan_detail(plan, frontier=FRONTIER,
                                     activities_by_date=by_date)
    return detail["workouts"][0]


def verdict(scenario: str, tmp_dir: Path) -> str:
    """``graded_day``'s verdict word."""
    return graded_day(scenario, tmp_dir)["verdict"]


if __name__ == "__main__":  # pragma: no cover - manual smoke
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        for s in SCENARIOS:
            w = graded_day(s, Path(tmp))
            print(f"{s:<24} verdict={w['verdict']:<8} "
                  f"actual={w.get('actual_distance_m') or 0:.0f}m")
