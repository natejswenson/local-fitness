"""Verdict evals for the report card — what letter does a run DESERVE.

Pure pytest, no model, no network, no live DB: every scenario is a fabricated
SQLite database built in ``tmp_path``. See ``tests/evals/report_cards.py`` for
why these exist alongside ``tests/test_report_card.py``'s unit tests — in one
line, those assert that the rubric computes what it says it computes, and these
assert that what it says is right.

Each test below is written so it fails on the rubric that shipped the defect it
names, not merely on a hypothetical one.
"""
from __future__ import annotations

import pytest
from report_cards import EXPECTED_VERDICTS, SCENARIOS, build_scenario_db, grade

from local_fitness import db
from local_fitness.agent import report_card as rc


def _score(card_dict: dict) -> float:
    """The overall star score, for comparing verdicts as an ordering.

    Replaces the old ``_points`` (base-letter GPA lookup): the score IS the
    ordering now, so there is nothing to convert.
    """
    return card_dict["overall"]["stars"]


@pytest.fixture
def card(tmp_path):
    """Grade a scenario end-to-end through the production path."""
    return lambda scenario: grade(scenario, tmp_path)


# --- the registry contract -------------------------------------------------

def test_every_scenario_declares_a_verdict():
    """A fixture with no expected verdict is a fixture nobody has judged. This
    is what stops the suite drifting back into "asserts mechanics only"."""
    assert set(EXPECTED_VERDICTS) == set(SCENARIOS)
    for name, spec in EXPECTED_VERDICTS.items():
        assert ("min_stars" in spec) or ("max_stars" in spec), \
            f"{name} bounds nothing"
        for key in ("min_stars", "max_stars"):
            if key in spec:
                assert rc.STAR_FLOOR <= spec[key] <= rc.STAR_MAX, \
                    f"{name}'s {key} is off the scale"
        assert len(spec.get("why", "")) > 40, f"{name} does not say why"


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_scenario_meets_its_declared_verdict(scenario, card):
    """THE eval. Every scenario's overall score must satisfy its bound.

    `obedient_easy_straddling` is the one that would have caught the 2026-08-02
    defect: it graded C there, against a declared minimum of B (now 3.50).
    """
    spec = EXPECTED_VERDICTS[scenario]
    got = _score(card(scenario))
    if "min_stars" in spec:
        assert got >= spec["min_stars"], (
            f"{scenario}: got {got:.2f}, expected at least "
            f"{spec['min_stars']:.2f} — {spec['why']}")
    if "max_stars" in spec:
        assert got <= spec["max_stars"], (
            f"{scenario}: got {got:.2f}, expected at most "
            f"{spec['max_stars']:.2f} — {spec['why']}")


# --- the prescribed-cap trio -----------------------------------------------

def test_straddling_a_cap_by_a_beat_is_not_a_breach(card):
    """The permanent 2026-08-02 guard, at the level a reader acts on.

    The distinguishing quantities are pinned together on purpose: 60% of the
    run IS above the ceiling (the number 0.40.0 graded, which is why it
    returned F), while the time-weighted excess is ~1.2 bpm (the number that
    says this run obeyed). A rubric that cannot tell those apart fails here.
    """
    c = card("obedient_easy_straddling")
    hr = c["metrics"]["hr"]

    assert hr["cap"] == 140.0                       # the plan stated a ceiling
    assert hr["time_above_cap_pct"] == 60           # ...and 60% of the run cleared it
    assert hr["exceedance_bpm"] == pytest.approx(1.2, abs=0.1)   # by 1.2 bpm
    # 1.2 bpm is under HR_CAP_NOISE_BPM, so the breach is not distinguishable
    # from rounding and the row must be a clean maximum — not "nearly".
    assert hr["stars"] == rc.STAR_MAX
    assert c["overall"]["stars"] == rc.STAR_MAX
    assert c["overall"]["capped"] is False


def test_the_three_cap_scenarios_are_strictly_ordered(card):
    """Obeying > straddling-by-a-beat > blowing it.

    Ordering rather than three fixed letters, because the failure this guards
    is COLLAPSE — 0.40.0 scored straddling and blowing it identically (both F),
    and any fix that over-corrects into "the cap never bites" collapses the
    other end. Both directions are caught here.
    """
    clean = card("obedient_easy_clean")
    straddled = card("obedient_easy_straddling")
    blown = card("cap_blown_hard")

    assert _score(clean) >= _score(straddled)
    assert _score(straddled) > _score(blown)
    # And on the HR row itself, which is where the discrimination lives.
    assert (straddled["metrics"]["hr"]["stars"]
            > blown["metrics"]["hr"]["stars"])
    # The blown run is a genuine breach: >10 bpm over, sustained.
    assert blown["metrics"]["hr"]["exceedance_bpm"] > 10


# --- the splits exceptions --------------------------------------------------

def test_rep_pace_is_graded_on_the_reps_not_the_run_average(card):
    """A manually-lapped interval session must be graded on its reps.

    The fixture's reps are at exactly the prescribed 260 s/km while the run
    average is 399 s/km — a 53% deviation, which is an F several times over.
    Asserting the pace grade AND the gap between the two numbers is what makes
    this fail if the metric ever falls back to the average.
    """
    c = card("interval_manual_laps")
    pace = c["metrics"]["pace"]

    assert c["activity"]["avg_pace_sec_per_km"] == 399.0     # the misleading number
    assert pace["actual"] == pytest.approx(260.0, abs=1.0)   # the one it graded
    assert pace["stars"] == rc.STAR_MAX
    # The average would have bottomed the scale, so the exception is
    # load-bearing here rather than incidentally agreeing with the fallback.
    assert rc.stars_from_deviation(
        rc.pace_deviation(399.0, 260.0, "quality"), "pace",
        rc.PLAN_TIGHTEN) == rc.STAR_FLOOR


def test_the_reference_pool_excludes_walks_that_garmin_calls_running(card):
    """Locomotion is measured, not labelled — the 0.26.0 scandal.

    All 46 pool activities carry `activity_type = 'treadmill_running'`, so a
    type-based filter admits every one of them. Pinning the pool size AND the
    resulting HR verdict is what makes this a real test: if the 30 walks leak
    in, the median HR collapses toward ~90 and this 141 bpm easy run is graded
    against a walking bar.
    """
    c = card("walk_mislabelled")
    ref = c["reference"]

    assert ref["mode"] == "rolling_60d"
    assert ref["n"] == 16                      # the runs only
    assert ref["excluded_other_mode"] == 30    # ...and the card says so
    assert ref["median_hr"] > 130              # a running median, not a walking one
    assert c["metrics"]["hr"]["stars"] >= 4.25


# --- fixture hygiene --------------------------------------------------------

@pytest.mark.parametrize("scenario", SCENARIOS)
def test_build_is_deterministic(scenario, tmp_path):
    """Same (scenario, today) → identical rows, so a verdict change is always a
    rubric change and never fixture drift."""
    def dump(path):
        with db.connect(path) as conn:
            return {
                "activities": [tuple(r) for r in conn.execute(
                    "SELECT activity_id, date, activity_type, distance_meters, "
                    "avg_hr, avg_pace_sec_per_km, training_load FROM activities "
                    "ORDER BY activity_id").fetchall()],
                "splits": [tuple(r) for r in conn.execute(
                    "SELECT activity_id, split_index, distance_meters, "
                    "duration_seconds, avg_hr FROM activity_splits "
                    "ORDER BY activity_id, split_index").fetchall()],
                "plan": [tuple(r) for r in conn.execute(
                    "SELECT date, type, target_distance_m, target_pace_sec_per_km, "
                    "target_hr_max FROM plan_workouts ORDER BY date").fetchall()],
            }

    a = build_scenario_db(scenario, tmp_path / "a" / "fitness.db")
    b = build_scenario_db(scenario, tmp_path / "b" / "fitness.db")
    assert dump(a) == dump(b)


def test_unknown_scenario_raises(tmp_path):
    with pytest.raises(ValueError, match="unknown scenario"):
        build_scenario_db("bogus", tmp_path / "x.db")


def test_every_scenario_actually_grades_something(card):
    """A fixture whose metrics all abstain would satisfy any bound vacuously."""
    for scenario in SCENARIOS:
        c = card(scenario)
        assert c["overall"]["graded_metrics"] >= 3, scenario
        assert c["overall"]["stars"] is not None, scenario
