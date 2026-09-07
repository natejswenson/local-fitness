"""Verdict evals for plan adherence — what does a session DESERVE to be called.

Pure pytest, no model, no network, no live DB: every scenario is a fabricated
SQLite database built in ``tmp_path``. See ``tests/evals/plan_verdicts.py`` for
why these exist alongside ``tests/test_plans.py``'s unit tests — in one line,
those assert that the grader computes what it says it computes, and these assert
that what it says is right.

Each test below is written so it fails on the grader that shipped the defect it
names, not merely on a hypothetical one.
"""
from __future__ import annotations

import pytest
from plan_verdicts import (
    _GRADED,
    EXPECTED_VERDICTS,
    GRADED_DATE,
    SCENARIOS,
    build_scenario_db,
    graded_day,
    verdict,
)

from local_fitness import db, plans


@pytest.fixture
def day(tmp_path):
    """Grade a scenario end-to-end through the production path."""
    return lambda scenario: graded_day(scenario, tmp_path)


# --- the registry contract -------------------------------------------------

def test_every_scenario_declares_a_verdict():
    """A fixture with no expected verdict is a fixture nobody has judged. This
    is what stops the suite drifting back into "asserts mechanics only"."""
    assert set(EXPECTED_VERDICTS) == set(SCENARIOS)
    for name, spec in EXPECTED_VERDICTS.items():
        assert spec["verdict"] in ("done", "partial", "missed"), \
            f"{name} declares no verdict a plan can return"
        assert len(spec.get("why", "")) > 40, f"{name} does not say why"


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_scenario_meets_its_declared_verdict(scenario, tmp_path):
    """THE eval. Every scenario's graded verdict must be the declared one.

    `tempo_jogged` is the one that would have caught #242: it graded `done`
    there, and the brief wrote that into the coach's journal as fact.
    """
    spec = EXPECTED_VERDICTS[scenario]
    got = verdict(scenario, tmp_path)
    assert got == spec["verdict"], (
        f"{scenario}: got {got!r}, expected {spec['verdict']!r} — {spec['why']}")


# --- the pace cap -----------------------------------------------------------

def test_a_tempo_run_at_easy_pace_is_not_a_tempo(day):
    """The permanent #242 guard, at the level a reader acts on.

    The distinguishing quantities are pinned together on purpose: the run DID
    cover 89% of the prescribed distance (the number the distance ladder
    grades, which is why a volume-only fix leaves this `done`), while its
    fastest mile is 2:19/mi off the prescribed rep pace. A grader that cannot
    tell those apart fails here.
    """
    w = day("tempo_jogged")
    covered = w["actual_distance_m"] / w["target_distance_m"]

    assert covered > plans.DONE_FRACTION            # volume alone says done
    assert covered == pytest.approx(0.891, abs=0.005)
    assert w["verdict"] == "missed"


def test_the_distance_ladder_alone_would_still_have_said_done(tmp_path):
    """The finding that decided the shape of this fix, pinned as its own case.

    All four days #242 reports ran 84-119% of their prescribed distance, so the
    issue's non-optional half — fall back to the distance ladder — leaves every
    one of them `done`. Asserted against the volume verdict directly, so a
    future simplification that deletes the pace cap fails here rather than
    quietly restoring the bug.
    """
    path = build_scenario_db("tempo_jogged", tmp_path / "fitness.db")
    plan = plans.get_active_plan(db_path=path)
    by_date = plans.load_activities_by_date(GRADED_DATE, GRADED_DATE, db_path=path)
    workout = plan["workouts"][0]
    activities = by_date[GRADED_DATE]
    ran_seconds = plans._running_duration(activities)

    assert plans._quality_volume_verdict(
        workout, activities, ran_seconds, plans._DEFAULT_GRADING_CONFIG) == "done"
    assert plans.classify_workout(workout, activities) == "missed"


def test_obeying_a_quality_prescription_still_grades_done(day):
    """The other direction. A change that makes every quality day partial is
    the 0.55.0 prescribed-walk inversion again — where obeying the plan scored
    worse than ignoring it — and `tempo_jogged` alone cannot catch it."""
    assert day("tempo_hit")["verdict"] == "done"


def test_the_two_tempo_days_differ_only_in_pace(day):
    """Ordering rather than two fixed words, because the failure this guards is
    COLLAPSE: the pre-fix grader called both of these `done`, and a fix that
    over-corrects calls both `missed`. Both directions are caught here."""
    jogged, hit = day("tempo_jogged"), day("tempo_hit")

    assert jogged["type"] == hit["type"] == "tempo"
    assert jogged["target_pace_sec_per_km"] == hit["target_pace_sec_per_km"]
    assert jogged["verdict"] != hit["verdict"]


def test_rep_pace_is_read_from_the_reps_not_the_run_average(day):
    """A manually-lapped interval session must be graded on its reps.

    The fixture's reps are at exactly the prescribed 6:58/mi while the run
    average is 10:42/mi — a 53% deviation, which is `missed` several times
    over. Asserting the verdict AND the gap between the two numbers is what
    makes this fail if the cap ever falls back to the average.
    """
    w = day("interval_reps_hit")
    target = w["target_pace_sec_per_km"]

    assert w["actual_pace_sec_per_km"] / target > 1.5   # the misleading number
    assert w["verdict"] == "done"
    # ...and the selector discriminates: the same session run slower fails.
    assert day("interval_reps_missed")["verdict"] == "missed"


def test_the_cap_abstains_on_the_backfilled_tail(day):
    """Splits cover the daily-sync era and nothing before it. A cap that failed
    a session it cannot measure would rewrite years of verdicts on the strength
    of missing data — the availability trap the report card's splits rule
    exists to prevent."""
    w = day("tempo_no_splits")
    assert w["verdict"] == "done"
    # It is genuinely off the rep pace — abstention, not agreement.
    assert w["actual_pace_sec_per_km"] > w["target_pace_sec_per_km"] * 1.3


def test_a_walk_on_a_quality_day_is_still_missed(day):
    """`_ran`'s pace gate, which the restructure runs through. Garmin labels a
    walking-pad session `treadmill_running`, so the label admits it and only
    the measured pace excludes it."""
    w = day("quality_walked")
    # Stored as `treadmill_running`; surfaced as the walk it measurably was.
    assert _GRADED["quality_walked"][1]["activity_type"] == "treadmill_running"
    assert w["actual_activity_types"] == ["walking"]
    assert w["actual_distance_m"] == pytest.approx(4.5 * 1609.344)
    assert w["verdict"] == "missed"


# --- fixture hygiene --------------------------------------------------------

@pytest.mark.parametrize("scenario", SCENARIOS)
def test_build_is_deterministic(scenario, tmp_path):
    """Same scenario → identical rows, so a verdict change is always a grader
    change and never fixture drift."""
    def dump(path):
        with db.connect(path) as conn:
            return {
                "activities": [tuple(r) for r in conn.execute(
                    "SELECT activity_id, date, activity_type, distance_meters, "
                    "duration_seconds, avg_pace_sec_per_km FROM activities "
                    "ORDER BY activity_id").fetchall()],
                "splits": [tuple(r) for r in conn.execute(
                    "SELECT activity_id, split_index, distance_meters, "
                    "duration_seconds, avg_pace_sec_per_km FROM activity_splits "
                    "ORDER BY activity_id, split_index").fetchall()],
                "plan": [tuple(r) for r in conn.execute(
                    "SELECT date, type, target_distance_m, target_pace_sec_per_km, "
                    "target_duration_sec FROM plan_workouts ORDER BY date").fetchall()],
            }

    a = build_scenario_db(scenario, tmp_path / "a" / "fitness.db")
    b = build_scenario_db(scenario, tmp_path / "b" / "fitness.db")
    assert dump(a) == dump(b)


def test_unknown_scenario_raises(tmp_path):
    with pytest.raises(ValueError, match="unknown scenario"):
        build_scenario_db("bogus", tmp_path / "x.db")


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_every_scenario_actually_grades_something(scenario, day):
    """A day held `pending`, or one with no activity at all, would satisfy its
    declared verdict without exercising the rubric. Every fixture must present
    a real session to a real prescription."""
    w = day(scenario)
    assert w["verdict"] != "pending", scenario
    assert w["actual_distance_m"] > 0, scenario
    assert w["target_distance_m"] > 0, scenario
    assert w["target_duration_sec"] is None, \
        f"{scenario}: a duration target skips the branch these evals grade"
