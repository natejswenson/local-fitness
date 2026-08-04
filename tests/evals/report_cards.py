#!/usr/bin/env python
"""Golden fabricated-DB fixtures for the report-card VERDICT evals.

The sibling of ``scripts/eval_fixtures.py``, which does this for the daily
brief. Every fixture is fully fabricated (NEVER derived from real Garmin data —
see CLAUDE.md); the shapes are modelled on failures that actually reached a
rendered card, but every number here was written by hand.

**Why a second fixture layer when ``tests/test_report_card.py`` has 140+ tests.**
Those tests assert that the rubric computes what it says it computes — a
deviation float, a band boundary, a display string. Not one of them could fail
when the rubric's *answer* was wrong, which is why every grading defect in this
module's history was found by a human reading a rendered card rather than by
the suite. These fixtures assert the **overall letter a run deserves**: the
thing a reader acts on. A wrong grade cannot pass one of these by being
arithmetically consistent with itself.

They also run the FULL path — a real SQLite DB through
``load_report_card_inputs`` -> ``build_card`` — rather than handing
``build_card`` a pre-made ``reference`` dict. That is load-bearing for two
scenarios: ``walk_mislabelled`` exists precisely to exercise the reference-pool
filter, and pool selection is where the 0.26.0 scandal lived. A dict-level test
cannot see it.

  obedient_easy_clean      — easy 5mi under a stated 140 cap, never touches it
  obedient_easy_straddling — the 2026-08-02 shape: average obeys, most of the
                             run sits 1-3 bpm over. THE permanent guard.
  cap_blown_hard           — sustained 12+ bpm over the same ceiling
  interval_manual_laps     — 2mi warmup then 800m reps; the average pace is far
                             off rep pace by construction
  walk_mislabelled         — a real run whose 60-day pool is mostly walking-desk
                             sessions logged as `treadmill_running`

The builder is **deterministic**: for a fixed ``(scenario, today)`` it writes
byte-identical rows (no RNG, no wall-clock).

Usage (as a library):
    from report_cards import SCENARIOS, build_scenario_db, grade
    card = grade("obedient_easy_straddling", tmp_path)
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from local_fitness import db

MILE_M = 1609.344
_USER_NAME = "Nate"
#: Every scenario grades an activity dated here; the reference window is the
#: 60 days behind it.
GRADED_DATE = date(2026, 7, 19)
#: The prescribed ceiling shared by the three HR-cap scenarios, so their
#: verdicts differ only by execution.
CAP_BPM = 140.0

SCENARIOS = (
    "obedient_easy_clean",
    "obedient_easy_straddling",
    "cap_blown_hard",
    "interval_manual_laps",
    "walk_mislabelled",
)


#: The verdict each scenario must produce, as a BOUND rather than an exact
#: score. A bound is what the contract actually is — "a run that obeyed its
#: prescription must not be marked down" — and it survives the ordinary
#: recalibration that an exact value would fight.
#:
#: Restated in stars at 0.50.0. The bounds are the SAME contract at the same
#: strength: the old letters mapped to the star scale through
#: ``STAR_VERDICT_CUTS``, so "at least a B" became "at least 3.50" and "at most
#: a C" became "at most 3.49" — a max is the top of the band it named, minus a
#: hair, because a bound that admitted the whole B band would be looser than the
#: letter it replaced.
#:
#: Adding a scenario without an entry here fails
#: ``test_every_scenario_declares_a_verdict``, which is what keeps this table
#: the single place a report card's expected behavior is written down.
EXPECTED_VERDICTS: dict[str, dict] = {
    "obedient_easy_clean": {
        "min_stars": 4.25,
        "why": "Distance, pace and HR all on prescription, never near the cap. "
               "There is no defensible reading in which this is not an A.",
    },
    "obedient_easy_straddling": {
        "min_stars": 3.50,
        "why": "THE 2026-08-02 guard. The average obeyed a stated 140 ceiling "
               "and the run sat 1-3 bpm over it for 60% of its duration. That "
               "is compliance with noise on top, not disobedience. 0.40.0 "
               "graded the HR row F and capped the card at C, and 0.40.1 "
               "looked straight at it and changed only the row's wording.",
    },
    "cap_blown_hard": {
        "max_stars": 3.49,
        "why": "15 bpm over a stated ceiling, sustained, peaking 25 over. The "
               "cap has to bite here or the straddling fix has simply "
               "disabled the metric.",
    },
    "interval_manual_laps": {
        "min_stars": 3.50,
        "why": "Reps hit at the prescribed pace. The run AVERAGE is 2:19/mi "
               "slower than rep pace by construction (warmup + cooldown are "
               "in it), so grading the average guarantees an F on a session "
               "that was executed correctly.",
    },
    "walk_mislabelled": {
        "min_stars": 3.50,
        "why": "An ordinary easy run whose 60-day pool is 30 walking-desk "
               "sessions to 16 runs, all labelled `treadmill_running`. If the "
               "walks reach the median the yardstick becomes a 15:00/mi walk "
               "at ~90 bpm and this run is graded against it.",
    },
}


def _mile_splits(hrs: tuple[int, ...], sec_per_mile: int) -> list[dict]:
    """One split per mile, equal duration, HR per the tuple."""
    return [
        {"distance_meters": MILE_M, "duration_seconds": sec_per_mile, "avg_hr": hr,
         "avg_pace_sec_per_km": sec_per_mile / (MILE_M / 1000.0)}
        for hr in hrs
    ]


# --- the graded activity + its prescription, per scenario -------------------
# (activity fields, plan fields or None, splits)

def _easy_activity(avg_hr: int, load: float) -> dict:
    """A 5-mile easy run at 9:54/mi. Distance and pace are held constant across
    the three cap scenarios so the ONLY thing that moves the verdict is HR."""
    return {
        "activity_type": "treadmill_running", "activity_name": "Easy 5",
        "distance_meters": 5 * MILE_M, "duration_seconds": 2970,
        "avg_pace_sec_per_km": 369.0, "avg_hr": avg_hr, "training_load": load,
        "aerobic_te": 2.4,
    }


_EASY_PLAN = {
    "type": "easy", "target_distance_m": 5 * MILE_M,
    "target_pace_sec_per_km": 369.0, "target_hr_max": CAP_BPM,
    "description": "Easy 5mi. Keep HR under 140.",
}

_GRADED = {
    # Comfortably inside the ceiling the whole way.
    "obedient_easy_clean": (
        _easy_activity(126, 25.0), _EASY_PLAN,
        _mile_splits((117, 125, 127, 126, 134), 594),
    ),
    # The 2026-08-02 shape. Average 139 obeys a 140 ceiling; miles 2, 4 and 5
    # sit 1-3 bpm over, so 60% of the run is "above the cap" by a beat. 0.40.0
    # graded this identically to a genuine breach and returned F.
    "obedient_easy_straddling": (
        _easy_activity(139, 52.0), _EASY_PLAN,
        _mile_splits((134, 141, 135, 143, 142), 594),
    ),
    # A real breach: every mile but the first well over, peaking 25 over.
    "cap_blown_hard": (
        _easy_activity(156, 95.0), _EASY_PLAN,
        _mile_splits((138, 152, 158, 161, 165), 594),
    ),
    # Manually lapped: a 2-mile warmup, four 800m reps at 6:58/mi, a cooldown.
    # avg_pace_sec_per_km averages all of it, so comparing the run average to
    # the rep prescription is an arithmetic guarantee of failure.
    "interval_manual_laps": (
        {"activity_type": "running", "activity_name": "Intervals",
         "distance_meters": 6 * MILE_M, "duration_seconds": 3852,
         "avg_pace_sec_per_km": 399.0, "avg_hr": 152, "training_load": 110.0,
         "aerobic_te": 3.6},
        {"type": "interval", "target_distance_m": 6 * MILE_M,
         "target_pace_sec_per_km": 260.0, "target_hr_max": None,
         "description": "2mi w/u, 4x800m @ 6:58/mi, 2mi c/d"},
        # warmup (2 mi), 4 reps of 800 m at rep pace, cooldown (2 mi)
        [{"distance_meters": 2 * MILE_M, "duration_seconds": 1320, "avg_hr": 138,
          "avg_pace_sec_per_km": 410.0}]
        + [{"distance_meters": 800.0, "duration_seconds": 208, "avg_hr": 168,
            "avg_pace_sec_per_km": 260.0} for _ in range(4)]
        + [{"distance_meters": 2 * MILE_M, "duration_seconds": 1700, "avg_hr": 132,
            "avg_pace_sec_per_km": 528.0}],
    ),
    # An ordinary outdoor easy run, no plan. Its reference pool is where the
    # interest is — see _walk_pool below.
    "walk_mislabelled": (
        {"activity_type": "treadmill_running", "activity_name": "Easy",
         "distance_meters": 3 * MILE_M, "duration_seconds": 1890,
         "avg_pace_sec_per_km": 391.0, "avg_hr": 141, "training_load": 45.0,
         "aerobic_te": 2.5},
        None,
        _mile_splits((136, 142, 145), 630),
    ),
}


# --- the 60-day reference pool ---------------------------------------------

def _run_pool(n: int = 16) -> list[dict]:
    """Real runs: 8:40-11:00/mi at 130-160 bpm. Deterministic fan-out on ``i``."""
    return [
        {"activity_type": "treadmill_running",
         "distance_meters": (3 + i % 4) * MILE_M,
         "duration_seconds": 1800 + (i % 5) * 120,
         "avg_pace_sec_per_km": 340.0 + (i % 7) * 12,
         "avg_hr": 138 + (i % 9) * 3, "training_load": 55.0 + (i % 6) * 9}
        for i in range(n)
    ]


def _walk_pool(n: int = 30) -> list[dict]:
    """Walking-desk sessions that Garmin labels ``treadmill_running`` — the
    label lie. 15:00-20:00/mi at 76-110 bpm, loads of 3-25. If these reach the
    median, an easy run is graded against a walk: measured live in 0.26.0, that
    put the pool median at 15:50/mi and 116 bpm and handed an interval session
    A+ on both HR and load for clearing a walking bar."""
    return [
        {"activity_type": "treadmill_running",
         "distance_meters": (2 + i % 3) * MILE_M,
         "duration_seconds": 3600 + (i % 4) * 600,
         "avg_pace_sec_per_km": 560.0 + (i % 8) * 20,
         "avg_hr": 76 + (i % 11) * 3, "training_load": 3.0 + (i % 6) * 4}
        for i in range(n)
    ]


_POOLS = {
    "obedient_easy_clean": _run_pool(),
    "obedient_easy_straddling": _run_pool(),
    "cap_blown_hard": _run_pool(),
    "interval_manual_laps": _run_pool(),
    # The whole point of this scenario: the walks OUTNUMBER the runs 30-16, so
    # an unfiltered median is a walk.
    "walk_mislabelled": _run_pool() + _walk_pool(),
}


def _insert_activity(conn, aid: int, day: str, a: dict) -> None:
    conn.execute(
        "INSERT INTO activities (activity_id, date, start_time, activity_type, "
        "activity_name, duration_seconds, distance_meters, avg_hr, "
        "avg_pace_sec_per_km, training_load, aerobic_te) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (aid, day, day + "T07:00:00", a["activity_type"],
         a.get("activity_name", "Run"), a["duration_seconds"], a["distance_meters"],
         a.get("avg_hr"), a.get("avg_pace_sec_per_km"), a.get("training_load"),
         a.get("aerobic_te")),
    )


def build_scenario_db(scenario: str, dest: Path, *, today: date = GRADED_DATE) -> Path:
    """Write the fabricated DB for ``scenario`` at ``dest``; return ``dest``."""
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}; expected one of {SCENARIOS}")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    db.init_schema(dest)

    activity, plan, splits = _GRADED[scenario]
    with db.connect(dest) as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES ('user_name', ?)",
                     (_USER_NAME,))

        # The graded activity is id 1, dated `today`.
        _insert_activity(conn, 1, today.isoformat(), activity)
        for i, s in enumerate(splits):
            conn.execute(
                "INSERT INTO activity_splits (activity_id, split_index, "
                "distance_meters, duration_seconds, avg_hr, avg_pace_sec_per_km) "
                "VALUES (1, ?, ?, ?, ?, ?)",
                (i, s["distance_meters"], s["duration_seconds"], s.get("avg_hr"),
                 s.get("avg_pace_sec_per_km")),
            )

        # The reference pool, spread one per day back through the 60-day window.
        # Starts 2 days back so nothing lands on or after the graded date (the
        # window ENDS the day before, so a same-day row would simply be ignored
        # — spreading them properly keeps the fixture honest either way).
        for i, row in enumerate(_POOLS[scenario]):
            day = (today - timedelta(days=2 + (i % 58))).isoformat()
            _insert_activity(conn, 100 + i, day, row)

        if plan is not None:
            created = (today - timedelta(days=30)).isoformat()
            cur = conn.execute(
                "INSERT INTO training_plans (status, goal_type, goal_distance_m, "
                "race_date, title, created_at, committed_at) "
                "VALUES ('active', 'half', 21097.5, ?, 'Eval Plan', ?, ?)",
                ((today + timedelta(days=30)).isoformat(), created, created),
            )
            conn.execute(
                "INSERT INTO plan_workouts (plan_id, date, seq, week_index, type, "
                "target_distance_m, target_pace_sec_per_km, target_duration_sec, "
                "target_hr_max, description) VALUES (?, ?, 1, 5, ?, ?, ?, NULL, ?, ?)",
                (cur.lastrowid, today.isoformat(), plan["type"],
                 plan["target_distance_m"], plan["target_pace_sec_per_km"],
                 plan["target_hr_max"], plan["description"]),
            )
    return dest


def grade(scenario: str, tmp_dir: Path, *, today: date = GRADED_DATE) -> dict:
    """Build the scenario and return its report card, via the production path.

    Deliberately NOT ``build_card`` on a hand-made ``reference`` dict: the
    reference POOL is part of what these evals grade.
    """
    from local_fitness.agent import report_card as rc

    path = build_scenario_db(scenario, Path(tmp_dir) / scenario / "fitness.db",
                             today=today)
    with db.connect(path) as conn:
        inputs = rc.load_report_card_inputs(conn, activity_id=1)
        assert inputs is not None, f"{scenario}: fixture did not resolve activity 1"
        return rc.build_card(
            inputs["activity"], inputs["splits"], inputs.get("plan_workout"),
            inputs["reference"], inputs.get("context"),
            hr_zones=inputs.get("hr_zones"),
        )


if __name__ == "__main__":  # pragma: no cover - manual smoke
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        for s in SCENARIOS:
            c = grade(s, Path(tmp))
            rows = "  ".join(
                f"{k}={v.get('grade') or 'n/a'}" for k, v in c["metrics"].items())
            print(f"{s:<26} overall={c['overall'].get('grade'):<4} {rows}")
