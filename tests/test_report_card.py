"""Rubric tests for agent/report_card.py.

The whole point of the module is that a grade is derived, not phrased — so
these tests pin the derivation. Everything above the persistence divider is
pure, so most of this file needs no DB at all.

The headline test is `test_easy_run_slower_than_expected_is_not_penalized`:
without direction gating, every recovery run fails on pace.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from local_fitness import db
from local_fitness.agent import report_card as rc

# A reference stand-in with round numbers, so every expectation below is
# arithmetic a reader can check in their head.
REF = {
    "mode": "rolling_60d",
    "n": 20,
    "pool": "running",
    "median_distance_m": 10000.0,
    "median_pace_sec_per_km": 300.0,   # 5:00/km
    "median_hr": 150.0,
    "median_load": 100.0,
}


def card_for(activity, *, plan=None, reference=REF, splits=()):
    return rc.build_card(activity, list(splits), plan, reference)


# --- the band table --------------------------------------------------------

@pytest.mark.parametrize("d,expected", [
    (0.0, "A"), (0.05, "A"), (0.0500000001, "A"),   # boundary lands in A
    (0.06, "B"), (0.10, "B"),
    (0.11, "C"), (0.20, "C"),
    (0.21, "D"), (0.35, "D"),
    (0.36, "F"), (5.0, "F"),
])
def test_grade_band_boundaries(d, expected):
    assert rc.base_letter(rc.grade_from_deviation(d)) == expected


def test_grade_none_in_none_out():
    assert rc.grade_from_deviation(None) is None


def test_grade_modifier_tracks_position_in_band():
    # A band spans (0, 0.05]: bottom third "+", top third "-".
    assert rc.grade_from_deviation(0.005) == "A+"
    assert rc.grade_from_deviation(0.025) == "A"
    assert rc.grade_from_deviation(0.048) == "A-"
    # F is unbounded above, so it carries no modifier.
    assert rc.grade_from_deviation(2.0) == "F"


def test_widen_scales_every_boundary():
    # 0.07 is a B normally; widened 1.5x the A band reaches 0.075.
    assert rc.base_letter(rc.grade_from_deviation(0.07)) == "B"
    assert rc.base_letter(rc.grade_from_deviation(0.07, rc.STEADY_WIDEN)) == "A"


def test_base_letter_strips_modifier():
    assert rc.base_letter("B+") == "B"
    assert rc.base_letter("D-") == "D"
    assert rc.base_letter(None) is None


# --- distance: two-sided under a plan, one-sided under the median ----------

def test_plan_distance_is_two_sided():
    """Overshooting a prescription costs the same as undershooting it — a
    12-miler on a 10-mile plan is over-cooking the plan."""
    over = rc.distance_deviation(12000, 10000, two_sided=True)
    under = rc.distance_deviation(8000, 10000, two_sided=True)
    assert over == pytest.approx(under) == pytest.approx(0.2)


def test_rolling_distance_is_one_sided_low():
    """Going longer than your norm is never a penalty."""
    assert rc.distance_deviation(15000, 10000, two_sided=False) == 0.0
    assert rc.distance_deviation(6000, 10000, two_sided=False) == pytest.approx(0.4)


def test_distance_deviation_guards_nulls_and_zero():
    assert rc.distance_deviation(None, 10000, two_sided=True) is None
    assert rc.distance_deviation(10000, None, two_sided=True) is None
    assert rc.distance_deviation(10000, 0, two_sided=True) is None


# --- pace: the recovery-run problem ---------------------------------------

def test_easy_run_slower_than_expected_is_not_penalized():
    """THE headline case. An easy run 12% slower than the easy expectation is
    exactly what an easy run should be. Without direction gating this is an F.
    """
    expected = rc.PACE_FACTORS["easy"] * REF["median_pace_sec_per_km"]  # 330
    actual = expected * 1.12
    assert rc.pace_deviation(actual, expected, "easy") == 0.0
    assert rc.base_letter(rc.grade_from_deviation(
        rc.pace_deviation(actual, expected, "easy"))) == "A"


def test_easy_run_too_fast_is_penalized():
    """The gate is directional, not merely disabled — running the easy day
    hard is still a miss."""
    expected = 330.0
    d = rc.pace_deviation(expected * 0.80, expected, "easy")
    assert d == pytest.approx(0.20)
    assert rc.base_letter(rc.grade_from_deviation(d)) == "C"


def test_quality_run_faster_than_target_is_an_A_uncapped():
    assert rc.pace_deviation(240.0, 300.0, "quality") == 0.0
    assert rc.pace_deviation(1.0, 300.0, "quality") == 0.0


def test_quality_run_too_slow_is_penalized():
    d = rc.pace_deviation(360.0, 300.0, "quality")
    assert d == pytest.approx(0.2)


def test_steady_pace_is_two_sided():
    assert rc.pace_deviation(330.0, 300.0, "steady") == pytest.approx(0.1)
    assert rc.pace_deviation(270.0, 300.0, "steady") == pytest.approx(0.1)


def test_by_feel_plan_day_gets_no_pace_grade():
    """A prescribed distance with a null target pace is explicitly by-feel.
    It must not be graded, and its weight must redistribute."""
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 10000, "avg_pace_sec_per_km": 400,
         "avg_hr": 140, "training_load": 90},
        plan={"type": "easy", "target_distance_m": 10000,
              "target_pace_sec_per_km": None, "seq": 1},
    )
    assert card["metrics"]["pace"]["grade"] is None
    assert card["metrics"]["pace"]["note"]
    # 4 compliance metrics; pace n/a for its own reason and continuity n/a for
    # want of splits, leaving distance and HR.
    assert card["overall"]["graded_metrics"] == 2


# --- HR: appropriateness to intent, never "lower is better" ---------------

def test_easy_run_above_hr_ceiling_is_downgraded():
    # ceiling 0.97 * 150 = 145.5 (recalibrated 2026-07-20 — see HR_BANDS; the
    # old 0.88 ceiling of 132 was below all but one of 13 real runs).
    assert rc.hr_deviation(145, 150, "easy") == 0.0
    assert rc.hr_deviation(120, 150, "easy") == 0.0   # comfortably under: fine
    assert rc.hr_deviation(160, 150, "easy") > 0.0


def test_quality_run_below_hr_floor_is_downgraded():
    # floor 1.00 * 150 = 150
    assert rc.hr_deviation(160, 150, "quality") == 0.0
    assert rc.hr_deviation(130, 150, "quality") > 0.0


def test_same_hr_grades_differently_under_different_intents():
    """The core claim: HR is meaningless without intent. 160bpm is a great
    quality day and a blown easy day."""
    easy = rc.hr_deviation(160, 150, "easy")
    quality = rc.hr_deviation(160, 150, "quality")
    assert easy > 0.0
    assert quality == 0.0


def test_steady_hr_band_is_two_sided():
    assert rc.hr_deviation(150, 150, "steady") == 0.0
    assert rc.hr_deviation(120, 150, "steady") > 0.0
    assert rc.hr_deviation(180, 150, "steady") > 0.0


def test_hr_deviation_guards_nulls():
    assert rc.hr_deviation(None, 150, "easy") is None
    assert rc.hr_deviation(150, None, "easy") is None


# --- training load ---------------------------------------------------------

def test_load_above_expectation_is_an_A():
    assert rc.load_deviation(150, 100) == 0.0


def test_load_below_expectation_is_penalized():
    assert rc.load_deviation(70, 100) == pytest.approx(0.3)


def test_load_is_intent_scaled_for_the_descriptor_but_carries_no_grade():
    """Load is intent-scaled — that is what makes "as intended" meaningful — but
    the scaling drives a DESCRIPTOR now, never a letter. 0.40.0."""
    activity = {"date": "2026-07-19", "distance_meters": 7500,
                "avg_pace_sec_per_km": 340, "avg_hr": 130, "training_load": 75}
    card = card_for(activity, plan={"type": "easy", "seq": 1})
    assert card["metrics"]["load"]["expected"] == pytest.approx(61.0)  # 0.61 * 100
    assert card["metrics"]["load"]["grade"] is None
    # 75/61 = 1.23 -> inside MODERATE, and under the spike ceiling, so intended.
    assert card["stimulus"]["level"] == "MODERATE"
    assert card["stimulus"]["as_intended"] is True


def test_a_correct_easy_day_reads_low_stimulus_not_a_failure():
    """The headline regression. A 25-load easy run against a ~61 easy-day
    expectation is the whole 2026-07-29 case: it used to score F on load and
    drag a 3.60-GPA A down to a C. It must now carry no load letter, no cap, and
    a LOW-but-intended descriptor."""
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 7500, "duration_seconds": 2971,
         "avg_pace_sec_per_km": 340, "avg_hr": 126, "training_load": 25,
         "aerobic_te": 2.0, "anaerobic_te": 0.0},
        plan={"type": "easy", "seq": 1},
    )
    assert card["metrics"]["load"]["grade"] is None
    assert card["overall"].get("capped_by") is None
    stim = card["stimulus"]
    assert stim["level"] == "LOW"
    assert stim["as_intended"] is True
    assert stim["spike"] is False
    # The card must SAY why there is no letter — the absence otherwise reads as
    # an omission, and "low = bad" is exactly the misreading being fixed.
    rendered = "\n".join(rc.stimulus_lines(card))
    assert "Not graded" in rendered
    assert "bank less of it" in rendered


def test_a_spike_is_reported_as_stimulus_not_punished_as_a_grade():
    """A spike used to score A+ on the row directly above the note calling it a
    spike; then it scored a D. Now it carries no letter at all — the descriptor
    and the flag say it instead, and neither can touch the overall."""
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 10000, "avg_pace_sec_per_km": 300,
         "avg_hr": 150, "training_load": 250},
    )
    load = card["metrics"]["load"]
    assert load["spike"] is True
    # Steady intent, so expected == the unscaled 100 median. The deviation is
    # still computed for display; what is withheld is the letter.
    assert load["expected"] == pytest.approx(100.0)
    assert load["deviation"] == pytest.approx(0.25)
    assert load["grade"] is None
    stim = card["stimulus"]
    assert stim["level"] == "VERY HIGH"
    assert stim["as_intended"] is False
    assert stim["spike"] is True
    assert card["overall"].get("capped_by") is None
    assert "Spike" in "\n".join(rc.stimulus_lines(card))


def test_load_at_the_spike_threshold_is_high_but_still_as_intended():
    """The boundary is inclusive: doubling the expectation is a big day, not an
    overreach. ``as_intended`` and the spike flag share ``LOAD_SPIKE_FACTOR`` so
    the descriptor and the flag can never disagree."""
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 10000, "avg_pace_sec_per_km": 300,
         "avg_hr": 150, "training_load": 200},
    )
    assert card["metrics"]["load"]["deviation"] == pytest.approx(0.0)
    assert card["metrics"]["load"]["grade"] is None
    # Exactly 2.0x is not > 2.0x, so no spike flag either.
    assert "spike" not in card["metrics"]["load"]
    assert card["stimulus"]["level"] == "HIGH"
    assert card["stimulus"]["as_intended"] is True
    assert card["stimulus"]["spike"] is False


@pytest.mark.parametrize(("load", "expected_d"), [
    (100.0, 0.0),    # on the expectation
    (75.0, 0.25),    # a quarter short
    (150.0, 0.0),    # over, but under the spike ceiling — still free
    (300.0, 0.5),    # 3.0x -> (3.0-2.0)/2.0
])
def test_load_deviation_is_two_sided_past_the_spike_ceiling(load, expected_d):
    assert rc.load_deviation(load, 100.0) == pytest.approx(expected_d)


def test_no_load_means_no_stimulus_descriptor_rather_than_a_guess():
    """With nothing to compare, "as intended" is an unverifiable claim — so it is
    None, the level is None, and the section renders as nothing at all rather
    than an empty heading."""
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 10000, "avg_pace_sec_per_km": 300,
         "avg_hr": 150, "training_load": None},
    )
    assert card["metrics"]["load"]["grade"] is None
    assert card["stimulus"]["level"] is None
    assert card["stimulus"]["as_intended"] is None
    assert rc.stimulus_lines(card) == []


# --- the prescribed HR cap (0.40.0) ----------------------------------------

#: Five even splits, HR crossing a 140 cap partway through — the shape of the
#: 2026-07-27 session (128 / 139 / 150 / 144 / 159 by mile). Durations differ so
#: a duration-weighted fraction can't be confused with a plain split count.
CAP_SPLITS = [
    {"split_index": 0, "distance_meters": 1609.34, "duration_seconds": 600, "avg_hr": 128},
    {"split_index": 1, "distance_meters": 1609.34, "duration_seconds": 500, "avg_hr": 139},
    {"split_index": 2, "distance_meters": 1609.34, "duration_seconds": 400, "avg_hr": 150},
    {"split_index": 3, "distance_meters": 1609.34, "duration_seconds": 300, "avg_hr": 144},
    {"split_index": 4, "distance_meters": 1609.34, "duration_seconds": 200, "avg_hr": 159},
]


def test_time_above_cap_is_duration_weighted_not_split_counted():
    """900 of 2000 seconds sit above 140 (the 400s, 300s and 200s splits). A
    split COUNT would say 3/5 = 60%; the duration weighting says 45%."""
    labelled = rc.label_splits(CAP_SPLITS)
    assert rc.time_above_cap_fraction(labelled, 140.0) == pytest.approx(0.45)


def test_time_above_cap_is_none_without_splits_or_without_a_cap():
    """Both None paths matter: no splits must DEGRADE to average-only grading
    rather than abstaining, and no cap means this axis does not apply."""
    assert rc.time_above_cap_fraction(rc.label_splits([]), 140.0) is None
    assert rc.time_above_cap_fraction(rc.label_splits(CAP_SPLITS), None) is None
    # Splits present but carrying no HR (a backfilled shape) is also None.
    no_hr = [{"split_index": 0, "distance_meters": 1609.34, "duration_seconds": 600}]
    assert rc.time_above_cap_fraction(rc.label_splits(no_hr), 140.0) is None


@pytest.mark.parametrize(("hr", "cap", "above", "expected_d"), [
    (126.0, 140.0, 0.0, 0.0),      # under the cap and never over it
    (126.0, 140.0, 0.04, 0.0),     # inside the grace fraction — still clean
    (126.0, 140.0, 0.25, 0.20),    # average fine, but a quarter of it was over
    (144.0, 140.0, 0.0, pytest.approx(4 / 140)),   # average breach only
    (144.0, 140.0, 0.59, pytest.approx(0.54)),  # time breach dominates the average
    (126.0, None, 0.5, None),      # no cap -> no deviation, caller falls back
    (None, 140.0, 0.5, None),      # no HR reading -> ungradeable
])
def test_hr_cap_deviation_takes_the_worse_of_average_and_time(hr, cap, above, expected_d):
    assert rc.hr_cap_deviation(hr, cap, above) == expected_d


def test_a_prescribed_cap_beats_the_rolling_band():
    """The Phase-2 fix. An explicit "keep HR under 140" is graded against 140,
    not against 0.97x the rolling median — which on live data was 139 by pure
    coincidence, so a real breach registered as a rounding error."""
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 8047, "duration_seconds": 2000,
         "avg_pace_sec_per_km": 374, "avg_hr": 126, "training_load": 25},
        plan={"type": "easy", "target_distance_m": 8047,
              "target_pace_sec_per_km": 360, "target_hr_max": 140.0, "seq": 1},
        splits=CAP_SPLITS,
    )
    hr = card["metrics"]["hr"]
    assert hr["reference"] == "plan"
    assert hr["expected"] == 140.0
    # 45% of this fixture sits above the cap, so even a 126 average is a breach.
    assert hr["time_above_cap_pct"] == 45
    # ...and because TIME is what breached, the row states that axis rather than
    # the compliant average — see
    # test_the_hr_row_states_the_axis_that_produced_the_grade. The numeric
    # `expected` above is still the cap in bpm; only the display moves.
    assert hr["governing_axis"] == "time"
    assert hr["expected_display"] == "≤ 5% above cap"
    assert "45% of the run sat above the prescribed 140 bpm cap" in hr["note"]
    # 0.45 above the cap minus the 0.05 grace = 0.40, past the D band's 0.35.
    assert hr["deviation"] == pytest.approx(0.40)
    assert hr["grade"] == "F"


def test_obeying_the_cap_outranks_blowing_it():
    """The ordering assertion, and the whole point of the 0.40.0 change. Before
    it, the obedient run scored C (load F-capped) and the disobedient one scored
    A — the rubric was inverted, not merely blunt."""
    plan = {"type": "easy", "target_distance_m": 8047,
            "target_pace_sec_per_km": 360, "target_hr_max": 140.0, "seq": 1}
    obeyed = card_for(
        {"date": "2026-07-19", "distance_meters": 8047, "duration_seconds": 2971,
         "avg_pace_sec_per_km": 374, "avg_hr": 126, "training_load": 25},
        plan=plan,
        splits=[{"split_index": i, "distance_meters": 1609.34,
                 "duration_seconds": 594, "avg_hr": hr}
                for i, hr in enumerate((117, 125, 127, 126, 134))],
    )
    blew_it = card_for(
        {"date": "2026-07-19", "distance_meters": 8047, "duration_seconds": 3011,
         "avg_pace_sec_per_km": 374, "avg_hr": 144, "training_load": 82},
        plan=plan,
        splits=[{"split_index": i, "distance_meters": 1609.34,
                 "duration_seconds": 602, "avg_hr": hr}
                for i, hr in enumerate((128, 139, 150, 144, 159))],
    )
    assert obeyed["overall"]["grade"] == "A"
    assert obeyed["overall"].get("capped_by") is None
    assert blew_it["metrics"]["hr"]["grade"] == "F"
    assert blew_it["overall"]["capped_by"] == "F"
    assert (rc.GRADE_POINTS[obeyed["overall"]["grade"]]
            > rc.GRADE_POINTS[blew_it["overall"]["grade"]])
    # ...and the load numbers run the OTHER way (25 vs 82), which is exactly the
    # inversion that used to decide the letters.
    assert obeyed["stimulus"]["load"] < blew_it["stimulus"]["load"]


#: An average that OBEYS the cap over a run that mostly didn't — 1160 of 2000
#: seconds above 140, with a 139 average. This is the shape that exposed the
#: display bug on 2026-08-02; the numbers are fabricated to land on a clean 58%.
UNDER_AVG_OVER_TIME_SPLITS = [
    {"split_index": 0, "distance_meters": 1609.34, "duration_seconds": 600, "avg_hr": 134},
    {"split_index": 1, "distance_meters": 1609.34, "duration_seconds": 580, "avg_hr": 141},
    {"split_index": 2, "distance_meters": 1609.34, "duration_seconds": 240, "avg_hr": 135},
    {"split_index": 3, "distance_meters": 1609.34, "duration_seconds": 300, "avg_hr": 143},
    {"split_index": 4, "distance_meters": 1609.34, "duration_seconds": 280, "avg_hr": 142},
]


@pytest.mark.parametrize(("hr", "cap", "above", "expected_axis"), [
    (139.0, 140.0, 0.58, "time"),    # average compliant, time is the breach
    (144.0, 140.0, 0.0, "average"),  # no split data over it, the mean ran hot
    (144.0, 140.0, 0.59, "time"),    # both breached, time is worse
    (144.0, 140.0, 0.06, "average"), # both breached, the average is worse
    (126.0, 140.0, 0.04, None),      # inside the grace fraction — no breach
    (126.0, 140.0, 0.0, None),       # clean on both axes
    (126.0, None, 0.5, None),        # no cap to breach
    (None, 140.0, 0.5, None),        # no HR reading at all
])
def test_hr_cap_axis_names_which_breach_produced_the_grade(hr, cap, above, expected_axis):
    """The attribution the deviation itself discards. A caller that cannot ask
    which axis won cannot state the number the grade was measured against."""
    assert rc.hr_cap_axis(hr, cap, above) == expected_axis


def test_hr_cap_axis_agrees_with_the_deviation_it_explains():
    """The two must never disagree: a named axis implies a non-zero deviation,
    and a zero deviation implies no axis. They are computed from one helper so
    that stays true, and this is the assertion that would catch them drifting."""
    for hr, above in ((139.0, 0.58), (144.0, 0.0), (126.0, 0.04), (126.0, 0.0)):
        d = rc.hr_cap_deviation(hr, 140.0, above)
        axis = rc.hr_cap_axis(hr, 140.0, above)
        assert (axis is None) == (d == 0.0)


def test_the_hr_row_states_the_axis_that_produced_the_grade():
    """Regression, live card 2026-08-02. The row printed

        | Avg HR | 139 bpm | ≤ 140 bpm | -1% | F |

    where every number describes the average — which was UNDER the cap and
    scored 0.0. The F came from 58% of the run sitting above 140, a quantity
    the row never showed, so three passing numbers sat beside a failing letter
    and the grade read as broken. Actual, expected and delta must all move to
    the axis that was graded."""
    card = card_for(
        {"date": "2026-08-02", "distance_meters": 8047, "duration_seconds": 2000,
         "avg_pace_sec_per_km": 363, "avg_hr": 139, "training_load": 52},
        plan={"type": "easy", "target_distance_m": 8047,
              "target_pace_sec_per_km": 360, "target_hr_max": 140.0, "seq": 1},
        splits=UNDER_AVG_OVER_TIME_SPLITS,
    )
    hr = card["metrics"]["hr"]
    assert hr["grade"] == "F"
    assert hr["governing_axis"] == "time"
    # 58% over the cap, less the 5% grace, is what the letter was computed from.
    assert hr["deviation"] == pytest.approx(0.53)

    # The three displayed cells now describe that axis...
    assert rc.actual_text("hr", hr) == "58% above cap (avg 139 bpm)"
    assert rc.expected_text("hr", hr) == "≤ 5% above cap"
    assert rc._delta_text("hr", hr) == "53% over"
    # ...and specifically no longer report the compliant average as the graded
    # quantity. "-1%" beside an F is the exact defect.
    assert rc._delta_text("hr", hr) != "-1%"

    # The numeric fields are untouched — storage and the note still speak bpm.
    assert hr["actual"] == 139
    assert hr["expected"] == 140.0
    assert hr["cap"] == 140.0
    assert hr["time_above_cap_pct"] == 58
    assert "58% of the run sat above the prescribed 140 bpm cap" in hr["note"]


def test_an_average_breach_keeps_the_row_in_bpm():
    """The other half of the two-sided assertion. When the AVERAGE is what blew
    the cap, the average IS the graded quantity — the row must stay in bpm and
    must not acquire a percentage-of-run display it did not earn.

    Splits carrying no HR is what makes the average the only live axis: it is
    the documented degrade path (see
    test_time_above_cap_is_none_without_splits_or_without_a_cap), and a run
    whose splits DO carry HR above the cap is time-governed by construction —
    every second over the cap is also weight on the mean."""
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 8047, "duration_seconds": 3011,
         "avg_pace_sec_per_km": 374, "avg_hr": 148, "training_load": 82},
        plan={"type": "easy", "target_distance_m": 8047,
              "target_pace_sec_per_km": 360, "target_hr_max": 140.0, "seq": 1},
        splits=[{"split_index": i, "distance_meters": 1609.34,
                 "duration_seconds": 602} for i in range(5)],
    )
    hr = card["metrics"]["hr"]
    assert "time_above_cap_pct" not in hr
    assert hr["governing_axis"] == "average"
    assert "actual_display" not in hr
    assert rc.actual_text("hr", hr) == "148 bpm"
    assert rc.expected_text("hr", hr) == "≤ 140 bpm"
    assert rc._delta_text("hr", hr) == "+6%"


def test_a_compliant_capped_run_names_no_axis_and_reads_in_range():
    """A clean run must not be given a breach to explain. `governing_axis` is
    None, the row keeps the bpm display, and the delta stays "in range"."""
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 8047, "duration_seconds": 3011,
         "avg_pace_sec_per_km": 374, "avg_hr": 126, "training_load": 25},
        plan={"type": "easy", "target_distance_m": 8047,
              "target_pace_sec_per_km": 360, "target_hr_max": 140.0, "seq": 1},
        splits=[{"split_index": i, "distance_meters": 1609.34,
                 "duration_seconds": 602, "avg_hr": h}
                for i, h in enumerate((117, 125, 127, 126, 134))],
    )
    hr = card["metrics"]["hr"]
    assert hr["grade"] == "A+"
    assert hr["governing_axis"] is None
    assert "actual_display" not in hr
    assert rc.actual_text("hr", hr) == "126 bpm"
    assert rc.expected_text("hr", hr) == "≤ 140 bpm"
    assert rc._delta_text("hr", hr) == "in range"


def test_the_rendered_card_never_prints_a_passing_delta_beside_an_f():
    """End-to-end through the markdown the user actually reads — the renderer
    and the PDF share these three helpers, so this covers both surfaces."""
    card = card_for(
        {"date": "2026-08-02", "distance_meters": 8047, "duration_seconds": 2000,
         "avg_pace_sec_per_km": 363, "avg_hr": 139, "training_load": 52},
        plan={"type": "easy", "target_distance_m": 8047,
              "target_pace_sec_per_km": 360, "target_hr_max": 140.0, "seq": 1},
        splits=UNDER_AVG_OVER_TIME_SPLITS,
    )
    md = rc.render_markdown(card)
    hr_row = next(ln for ln in md.splitlines() if ln.startswith("| Avg HR"))
    assert hr_row == "| Avg HR | 58% above cap (avg 139 bpm) | ≤ 5% above cap | 53% over | F |"
    assert "| 139 bpm | ≤ 140 bpm | -1% | F |" not in md


def test_no_prescribed_cap_still_uses_the_rolling_band():
    """Regression guard: a plan day without target_hr_max must grade exactly as
    it did before 0.40.0, band and all."""
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 8000, "duration_seconds": 2400,
         "avg_pace_sec_per_km": 360, "avg_hr": 130, "training_load": 60},
        plan={"type": "easy", "target_distance_m": 8000, "seq": 1},
    )
    hr = card["metrics"]["hr"]
    assert hr["reference"] == "rolling_60d"
    assert hr["band"] == {"floor": None, "ceiling": pytest.approx(145.5)}
    assert "cap" not in hr
    assert "time_above_cap_pct" not in hr


def test_a_cap_grades_on_the_average_when_no_splits_exist():
    """The splits read DEGRADES rather than abstaining — a backfilled activity
    with no splits still gets its cap graded, on the average alone."""
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 8000, "duration_seconds": 2400,
         "avg_pace_sec_per_km": 360, "avg_hr": 168, "training_load": 60},
        plan={"type": "easy", "target_distance_m": 8000,
              "target_hr_max": 140.0, "seq": 1},
    )
    hr = card["metrics"]["hr"]
    assert hr["reference"] == "plan"
    assert "time_above_cap_pct" not in hr      # nothing to measure
    assert hr["deviation"] == pytest.approx(28 / 140)   # 0.20 -> C
    assert rc.base_letter(hr["grade"]) == "C"


def test_a_walked_day_refuses_the_plan_cap_with_the_rest_of_the_plan():
    """A walk against a running prescription already refuses the plan's distance
    and pace targets; the cap must travel with them rather than being the one
    plan field a walk is still held to."""
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 6400, "duration_seconds": 5400,
         "avg_pace_sec_per_km": 843, "avg_hr": 100, "training_load": 20},
        plan={"type": "easy", "target_distance_m": 6400,
              "target_hr_max": 140.0, "seq": 1},
    )
    assert card["metrics"]["hr"]["reference"] == "rolling_60d"


# --- intent inference ------------------------------------------------------

def test_infer_intent_long_beats_easy_on_a_low_hr_long_run():
    """Ordering guard: a long run is usually run at easy HR. If "easy" won,
    the long run would be graded against a 0.75x distance expectation and get
    a free A on the one metric that should be interrogating it."""
    assert rc.infer_intent(
        {"distance_meters": 15000, "avg_hr": 130}, REF) == "long"


def test_infer_intent_easy_quality_and_steady():
    assert rc.infer_intent({"distance_meters": 8000, "avg_hr": 130}, REF) == "easy"
    assert rc.infer_intent({"distance_meters": 8000, "avg_hr": 165}, REF) == "quality"
    assert rc.infer_intent({"distance_meters": 8000, "avg_hr": 150}, REF) == "steady"


def test_infer_intent_null_hr_falls_back_to_steady():
    assert rc.infer_intent({"distance_meters": 8000, "avg_hr": None}, REF) == "steady"


@pytest.mark.parametrize("name", ["Recovery jog", "easy shakeout", "Shake Out"])
def test_activity_name_overrides_inference(name):
    """A run named "recovery" is an easy run no matter what its HR says."""
    assert rc.infer_intent(
        {"distance_meters": 8000, "avg_hr": 170, "activity_name": name}, REF) == "easy"


def test_plan_type_wins_over_inference():
    activity = {"date": "2026-07-19", "distance_meters": 8000, "avg_hr": 170,
                "avg_pace_sec_per_km": 300, "training_load": 100}
    card = card_for(activity, plan={"type": "easy", "seq": 1})
    assert (card["intent"], card["intent_source"]) == ("easy", "plan")
    card = card_for(activity)
    assert card["intent_source"] == "inferred"


def test_intent_class_collapses_plan_types():
    assert rc.intent_class("tempo") == "quality"
    assert rc.intent_class("interval") == "quality"
    assert rc.intent_class("race") == "quality"
    assert rc.intent_class("recovery") == "easy"
    assert rc.intent_class("cross") == "steady"
    assert rc.intent_class(None) == "steady"


def test_rest_day_prescription_falls_through_to_rolling():
    """A rest-day row has null targets — it's an intent signal, not a
    yardstick. Running anyway must still be graded, against the median."""
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 10000, "avg_pace_sec_per_km": 300,
         "avg_hr": 150, "training_load": 100},
        plan={"type": "rest", "seq": 1},
    )
    assert card["metrics"]["distance"]["reference"] == "rolling_60d"


# --- overall ---------------------------------------------------------------

def test_overall_renormalizes_over_gradeable_metrics():
    """One n/a metric must not drag the average toward zero — its weight
    redistributes across the rest."""
    all_a = {k: {"grade": "A"} for k in rc.METRIC_WEIGHTS}
    assert rc.overall_grade(all_a)["grade"] == "A"
    all_a["pace"] = {"grade": None}
    out = rc.overall_grade(all_a)
    assert out["grade"] == "A"
    # Four compliance metrics since 0.40.0, so one n/a leaves three.
    assert out["graded_metrics"] == 3


def test_overall_with_no_gradeable_metrics_is_na_not_f():
    """"F" would read as a judgment we did not make."""
    out = rc.overall_grade({k: {"grade": None} for k in rc.METRIC_WEIGHTS})
    assert out["grade"] == "n/a"
    assert out["gpa"] is None


def test_overall_uses_base_letters_so_modifiers_cannot_move_it():
    with_mods = {k: {"grade": "B+"} for k in rc.METRIC_WEIGHTS}
    without = {k: {"grade": "B"} for k in rc.METRIC_WEIGHTS}
    assert rc.overall_grade(with_mods) == rc.overall_grade(without)


def test_overall_cuts():
    assert rc.overall_grade({"distance": {"grade": "F"}})["grade"] == "F"
    assert rc.overall_grade({"distance": {"grade": "C"}})["grade"] == "C"


# --- splits ----------------------------------------------------------------

MILE_SPLITS = [
    {"split_index": 0, "distance_meters": 1609.34, "duration_seconds": 600, "avg_hr": 140},
    {"split_index": 1, "distance_meters": 1609.34, "duration_seconds": 590, "avg_hr": 145},
    {"split_index": 2, "distance_meters": 1609.34, "duration_seconds": 585, "avg_hr": 150},
    {"split_index": 3, "distance_meters": 1609.34, "duration_seconds": 580, "avg_hr": 155},
    {"split_index": 4, "distance_meters": 91.17, "duration_seconds": 27, "avg_hr": 160},
]


def test_label_splits_detects_miles_and_is_one_indexed():
    out = rc.label_splits(MILE_SPLITS)
    assert out["unit"] == "Mile"
    assert out["rows"][0]["index"] == 1          # DB is 0-based, display is not
    assert out["rows"][-1]["partial"] is True


def test_label_splits_calls_non_mile_laps_laps():
    """Mislabeling a 1km lap as a mile is a lie on the card."""
    km = [{"split_index": i, "distance_meters": 1000.0, "avg_hr": 140} for i in range(4)]
    assert rc.label_splits(km)["unit"] == "Lap"


def test_label_splits_empty_is_unavailable():
    out = rc.label_splits([])
    assert out["available"] is False
    assert out["rows"] == []


def test_hr_drift_excludes_the_partial_lap():
    """A 90-meter fragment's HR is noise and must not move the drift number."""
    full = rc.label_splits(MILE_SPLITS)["rows"][:-1]
    assert rc.hr_drift_pct(full) == pytest.approx(
        rc.label_splits(MILE_SPLITS)["hr_drift_pct"])
    # front half 140,145 -> 142.5; back half 150,155 -> 152.5
    assert rc.label_splits(MILE_SPLITS)["hr_drift_pct"] == pytest.approx(7.0, abs=0.1)


def test_hr_drift_needs_enough_laps():
    assert rc.hr_drift_pct([{"avg_hr": 140}, {"avg_hr": 150}]) is None


def test_only_the_documented_exceptions_read_splits():
    """~87% of activities have no splits, so a splits-dependent grade would mean
    different things on different rows. The rule is therefore: splits may change
    a grade ONLY through a documented exception, and absence must be EXPLICIT
    (n/a plus a stated reason, weight redistributing) rather than silent.

    Distance, pace-on-a-non-quality-day, and HR-without-a-prescribed-cap must be
    byte-identical with and without splits.
    """
    activity = {"date": "2026-07-19", "distance_meters": 10000,
                "avg_pace_sec_per_km": 300, "avg_hr": 150, "training_load": 100}
    with_splits = card_for(activity, splits=MILE_SPLITS)
    without = card_for(activity)
    for key in ("distance", "pace", "hr", "load"):
        assert with_splits["metrics"][key] == without["metrics"][key], key


#: MILE_SPLITS carries no pace (other tests depend on that shape), and
#: continuity is a pace measure — so it gets its own paced fixture. Even miles.
PACED_SPLITS = [
    {"split_index": i, "distance_meters": 1609.34, "duration_seconds": 600,
     "avg_hr": 140, "avg_pace_sec_per_km": 372.0 + i}
    for i in range(4)
]


def test_continuity_abstains_explicitly_when_splits_are_missing():
    """The third splits exception (0.40.0), held to the same contract as the
    quality-pace one: no splits means n/a WITH a reason, never a fabricated
    grade, and the weight redistributes over the metrics that could be graded."""
    activity = {"date": "2026-07-19", "distance_meters": 10000,
                "avg_pace_sec_per_km": 300, "avg_hr": 150, "training_load": 100}
    without = card_for(activity)
    cont = without["metrics"]["continuity"]
    assert cont["grade"] is None
    assert cont["note"] == "no splits recorded — continuity can't be measured"
    assert without["overall"]["graded_metrics"] == 3   # the other three

    with_splits = card_for(activity, splits=PACED_SPLITS)
    assert with_splits["metrics"]["continuity"]["grade"] == "A+"
    assert with_splits["overall"]["graded_metrics"] == 4


def test_continuity_says_so_when_splits_carry_no_pace():
    """Distinct from "no splits": a run WITH a split table but no pace column
    would otherwise report "only 0 full splits", which reads as a card bug."""
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 10000,
         "avg_pace_sec_per_km": 300, "avg_hr": 150, "training_load": 100},
        splits=MILE_SPLITS,
    )
    cont = card["metrics"]["continuity"]
    assert cont["grade"] is None
    assert cont["note"] == ("splits recorded without pace — continuity can't be "
                            "measured")


def test_continuity_catches_a_walk_mile_nothing_else_sees():
    """The reason this axis exists. Modelled on 2026-07-28: a tempo session whose
    4th mile ran 12:31 among ~9:20 miles. Distance was A+, HR was A+, and the
    average pace absorbed the break — nothing on the card said a mile had been
    walked.

    Note 12:31/mi is UNDER the 13:00 run/walk boundary, so absolute walk-pace
    detection would have missed it. The ratio against the run's own median is
    what catches it.
    """
    # The real 2026-07-28 per-mile paces (10:19, 8:40, 9:19, 12:31), in sec/km
    # because that is the column's unit.
    paces = (384.6, 323.1, 347.4, 466.6)
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 6437, "duration_seconds": 2745,
         "avg_pace_sec_per_km": 426.0, "avg_hr": 143, "training_load": 85},
        plan={"type": "easy", "target_distance_m": 6437, "seq": 1},
        splits=[{"split_index": i, "distance_meters": 1609.34,
                 "duration_seconds": round(p * 1.60934), "avg_hr": 140,
                 "avg_pace_sec_per_km": p}
                for i, p in enumerate(paces)],
    )
    cont = card["metrics"]["continuity"]
    # 466.6 / median(366.0) = 1.275 -> d = 1.275 - 1.15 = 0.125 -> C
    assert cont["ratio"] == pytest.approx(1.275, abs=0.001)
    assert cont["deviation"] == pytest.approx(0.125, abs=0.001)
    assert rc.base_letter(cont["grade"]) == "C"
    # The note names the offending split so the reader can act on it.
    assert cont["note"] == ("mile 4 ran 12:31/mi — 27% slower than your median "
                            "mile for this run")
    # And it is genuinely independent: the metrics that missed it still pass.
    assert card["metrics"]["distance"]["grade"] == "A+"
    assert rc.base_letter(card["metrics"]["hr"]["grade"]) == "A"


def test_continuity_ignores_a_conservative_opening_mile():
    """The false-positive guard, and the reason this is a slowest-vs-median ratio
    rather than a standard deviation. Measured 2026-07-27: SD across the run was
    22.2 s/mi, but 4.9 s/mi once the warm-up mile is dropped. Starting easy and
    settling in must not read as a break."""
    # The real 2026-07-27 per-mile paces (10:41 opener, then 9:48-9:59), sec/km.
    paces = (398.3, 365.4, 367.9, 372.2, 366.6)
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 8046, "duration_seconds": 3011,
         "avg_pace_sec_per_km": 374.0, "avg_hr": 144, "training_load": 82},
        splits=[{"split_index": i, "distance_meters": 1609.34,
                 "duration_seconds": round(p * 1.60934), "avg_hr": 140,
                 "avg_pace_sec_per_km": p}
                for i, p in enumerate(paces)],
    )
    cont = card["metrics"]["continuity"]
    assert cont["ratio"] == pytest.approx(1.083, abs=0.001)   # under the 1.15 gate
    assert cont["deviation"] == 0.0
    assert cont["grade"] == "A+"
    assert "note" not in cont


def test_continuity_needs_three_full_splits_to_say_anything():
    """Two splits cannot separate a slowest from a median. The note says which
    of the two reasons applies rather than reusing the no-splits wording."""
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 10000,
         "avg_pace_sec_per_km": 300, "avg_hr": 150, "training_load": 100},
        splits=PACED_SPLITS[:2],
    )
    cont = card["metrics"]["continuity"]
    assert cont["grade"] is None
    assert cont["note"] == ("only 2 full splits with pace — need 3 to compare a "
                            "slowest against a median")


# --- insufficient reference ------------------------------------------------

def test_insufficient_reference_grades_nothing():
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 10000, "avg_pace_sec_per_km": 300,
         "avg_hr": 150, "training_load": 100},
        reference={"mode": "insufficient_data", "n": 3},
    )
    assert all(m["grade"] is None for m in card["metrics"].values())
    assert card["overall"]["grade"] == "n/a"


# --- rendering -------------------------------------------------------------

def test_render_markdown_tables_are_well_formed():
    card = card_for(
        {"date": "2026-07-19", "activity_name": "Morning Run", "distance_meters": 10000,
         "duration_seconds": 3000, "avg_pace_sec_per_km": 300, "avg_hr": 150,
         "training_load": 100},
        splits=MILE_SPLITS,
    )
    md = rc.render_markdown(card)
    # Group consecutive pipe-rows into blocks, then assert each block is
    # internally consistent. One global width stopped being the right assertion
    # when the 2-column Stimulus table landed (0.40.0), and per-table is the
    # property that actually matters anyway: a row narrower than its own header
    # is what renders as a broken table.
    blocks: list[list[str]] = []
    prev_was_row = False
    for line in md.splitlines():
        is_row = line.startswith("|")
        if is_row:
            if not prev_was_row:
                blocks.append([])
            blocks[-1].append(line)
        prev_was_row = is_row
    # compliance (Metric/Actual/Expected/Delta/Grade), stimulus (Signal/Value),
    # splits (Mile/Pace/Avg HR/vs run/Elev — the Distance column was dropped as
    # duplicative, the row label already IS the distance).
    assert [b[0].count("|") for b in blocks] == [6, 3, 6]
    for block in blocks:
        assert len({ln.count("|") for ln in block}) == 1
    assert "Morning Run" in md


def test_render_markdown_says_no_splits_when_absent():
    card = card_for({"date": "2026-07-19", "distance_meters": 10000,
                     "avg_pace_sec_per_km": 300, "avg_hr": 150, "training_load": 100})
    assert "No per-mile splits recorded" in rc.render_markdown(card)


def test_reference_line_names_the_yardstick():
    """The card must never leave the reference ambiguous."""
    plan_card = card_for(
        {"date": "2026-07-19", "distance_meters": 10000, "avg_pace_sec_per_km": 300,
         "avg_hr": 150, "training_load": 100},
        plan={"type": "easy", "target_distance_m": 10000,
              "target_pace_sec_per_km": 330, "seq": 1},
    )
    assert "training plan" in rc.reference_line(plan_card)
    rolling_card = card_for(
        {"date": "2026-07-19", "distance_meters": 10000, "avg_pace_sec_per_km": 300,
         "avg_hr": 150, "training_load": 100})
    assert "rolling median" in rc.reference_line(rolling_card)


def test_reference_line_does_not_disclaim_what_the_plan_graded():
    """A thin rolling pool used to print "not enough history to grade" directly
    under two letters the plan had just graded. The disclaimer is now scoped to
    the metrics it actually applies to."""
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 10000, "duration_seconds": 3000,
         "avg_pace_sec_per_km": 300, "avg_hr": 150, "training_load": 100},
        plan={"type": "easy", "target_distance_m": 10000,
              "target_pace_sec_per_km": 310, "seq": 1},
        reference={"mode": "insufficient_data", "n": 2, "pool": "running"},
    )
    assert card["metrics"]["distance"]["grade"] == "A+"     # graded off the plan
    assert card["metrics"]["hr"]["grade"] is None           # no rolling median
    line = rc.reference_line(card)
    assert line == (
        # Training load is no longer named here (0.40.0): it is never graded, so
        # listing it as "ungraded for want of history" implied a judgment that
        # more data would have unlocked.
        "Graded against your **training plan** for this date (intent: easy, "
        "prescribed by your plan). HR ungraded — only 2 "
        "comparable activities in the last 60 days (need 5).")


def test_scoped_caveat_counts_one_activity_in_the_singular():
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 10000, "duration_seconds": 3000,
         "avg_pace_sec_per_km": 300, "avg_hr": 150, "training_load": 100},
        plan={"type": "easy", "target_distance_m": 10000, "seq": 1},
        reference={"mode": "insufficient_data", "n": 1, "pool": "running"},
    )
    # A by-feel prescription grades distance only, so pace joins the caveat.
    assert rc.reference_line(card, markdown=False).endswith(
        # Pace is NOT listed: a by-feel prescription has no pace target, so
        # blaming its n/a on a thin pool would promise a grade more history
        # cannot unlock. Same reason continuity is absent (0.40.0).
        "HR ungraded — only 1 comparable activity in "
        "the last 60 days (need 5).")


def test_reference_line_still_disclaims_when_the_plan_graded_nothing():
    """The no-plan case is untouched: with nothing graded at all, the blanket
    sentence is the honest one."""
    card = card_for({"date": "2026-07-19", "distance_meters": 10000,
                     "avg_pace_sec_per_km": 300, "avg_hr": 150, "training_load": 100},
                    reference={"mode": "insufficient_data", "n": 3})
    assert rc.reference_line(card) == (
        "Not enough comparable history to grade — 3 similar activities in the "
        "last 60 days (need 5).")


def test_reference_line_drops_markdown_for_the_pdf():
    """The PDF escapes its HTML rather than rendering markdown, so leaving the
    asterisks in printed a literal `**training plan**` on the page."""
    card = card_for({"date": "2026-07-19", "distance_meters": 10000,
                     "avg_pace_sec_per_km": 300, "avg_hr": 150, "training_load": 100})
    assert "**" in rc.reference_line(card)
    assert "**" not in rc.reference_line(card, markdown=False)


def test_load_is_rounded_for_display():
    """Garmin's stored load carries ~13 meaningless decimals."""
    assert rc._fmt_load(51.28765869140625) == "51"


# === persistence ===========================================================

@pytest.fixture
def rc_db(tmp_path, monkeypatch):
    """A DB with enough comparable running history to build a real reference,
    plus one treadmill run so the exact-type-first rule is exercised."""
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    today = date.today()
    with db.connect(p) as conn:
        for i in range(1, 13):
            d = (today - timedelta(days=i)).isoformat()
            conn.execute(
                "INSERT INTO activities (activity_id, date, start_time, activity_type, "
                "activity_name, duration_seconds, distance_meters, avg_hr, "
                "avg_pace_sec_per_km, training_load) VALUES "
                "(?, ?, ?, 'running', 'Run', 3000, 10000, 150, 300, 100)",
                (100 + i, d, f"{d} 07:00:00"),
            )
        # Same-day treadmill session at a very different HR: it must NOT
        # pollute an outdoor run's reference.
        conn.execute(
            "INSERT INTO activities (activity_id, date, start_time, activity_type, "
            "activity_name, duration_seconds, distance_meters, avg_hr, "
            "avg_pace_sec_per_km, training_load) VALUES "
            "(200, ?, ?, 'treadmill_running', 'Treadmill', 3000, 10000, 110, 400, 40)",
            ((today - timedelta(days=2)).isoformat(),
             (today - timedelta(days=2)).isoformat() + " 18:00:00"),
        )
        # The activity under grading.
        conn.execute(
            "INSERT INTO activities (activity_id, date, start_time, activity_type, "
            "activity_name, duration_seconds, distance_meters, avg_hr, "
            "avg_pace_sec_per_km, training_load) VALUES "
            "(1, ?, ?, 'running', 'Morning Run', 3000, 10000, 150, 300, 100)",
            (today.isoformat(), today.isoformat() + " 07:00:00"),
        )
        for i, s in enumerate(MILE_SPLITS):
            conn.execute(
                "INSERT INTO activity_splits (activity_id, split_index, distance_meters, "
                "duration_seconds, avg_hr) VALUES (1, ?, ?, ?, ?)",
                (i, s["distance_meters"], s["duration_seconds"], s["avg_hr"]),
            )
        conn.execute(
            "INSERT INTO baselines (date, ctl, atl, tsb) VALUES (?, 40.0, 45.0, -5.0)",
            (today.isoformat(),),
        )
    return p


def test_rolling_reference_prefers_the_exact_activity_type(rc_db):
    """Measured on live data: pooling road with treadmill runs put median HR
    at 119 against an outdoor average of 140, and handed a normal easy run a
    D on heart rate."""
    with db.connect(rc_db) as conn:
        act = dict(conn.execute("SELECT * FROM activities WHERE activity_id=1").fetchone())
        ref = rc.rolling_reference(conn, act)
    assert ref["mode"] == "rolling_60d"
    assert ref["pool"] == "running"
    assert ref["widened"] is False
    assert ref["n"] == 12                      # the treadmill row is excluded
    assert ref["median_hr"] == pytest.approx(150.0)


def test_rolling_reference_excludes_the_graded_activity(rc_db):
    """A workout must never move its own goalposts."""
    with db.connect(rc_db) as conn:
        conn.execute("UPDATE activities SET training_load=9999 WHERE activity_id=1")
        act = dict(conn.execute("SELECT * FROM activities WHERE activity_id=1").fetchone())
        ref = rc.rolling_reference(conn, act)
    assert ref["median_load"] == pytest.approx(100.0)


def test_rolling_reference_window_ends_the_day_before(rc_db):
    with db.connect(rc_db) as conn:
        act = dict(conn.execute("SELECT * FROM activities WHERE activity_id=1").fetchone())
        ref = rc.rolling_reference(conn, act)
    assert ref["window_end"] == (date.today() - timedelta(days=1)).isoformat()


def test_rolling_reference_widens_when_the_exact_pool_is_thin(rc_db):
    """A treadmill run has only one same-type peer, so the pool widens to
    on-foot — and says so, rather than widening silently."""
    with db.connect(rc_db) as conn:
        act = dict(conn.execute("SELECT * FROM activities WHERE activity_id=200").fetchone())
        ref = rc.rolling_reference(conn, act)
    assert ref["widened"] is True
    assert ref["pool"] == "on-foot"
    assert ref["mode"] == "rolling_60d"


def test_rolling_reference_insufficient_below_the_floor(tmp_path, monkeypatch):
    p = tmp_path / "f.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    today = date.today()
    with db.connect(p) as conn:
        for i in range(1, 5):     # 4 peers, one below MIN_REFERENCE_ACTIVITIES
            conn.execute(
                "INSERT INTO activities (activity_id, date, activity_type, "
                "distance_meters, avg_hr) VALUES (?, ?, 'running', 10000, 150)",
                (i, (today - timedelta(days=i)).isoformat()),
            )
        act = {"activity_id": 99, "date": today.isoformat(), "activity_type": "running"}
        ref = rc.rolling_reference(conn, act)
    assert ref["mode"] == "insufficient_data"
    assert ref["n"] == 4


def test_rolling_reference_handles_a_malformed_date():
    class _Conn:
        def execute(self, *a, **k):        # pragma: no cover - never reached
            raise AssertionError("must not query on a bad date")

    assert rc.rolling_reference(_Conn(), {"date": "nope"})["mode"] == "insufficient_data"


class TracingConn:
    """Records the SQL each `execute` issues AND the column names SQLite
    actually returned for it, then delegates everything else to the real
    connection. The description is the honest answer to "did raw_json leave
    SQLite" — a `not in` check on the caller's dict can't tell a column that
    was never fetched from one that was fetched and popped."""

    def __init__(self, conn):
        self._conn = conn
        self.calls: list[tuple[str, list[str]]] = []

    def execute(self, sql, params=()):
        cur = self._conn.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        self.calls.append((sql, cols))
        return cur

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def sql_for(self, table: str) -> str:
        """The one SELECT issued against `table`. Fails loudly on 0 or 2+, so
        a refactor that splits or drops the query can't quietly pass."""
        hits = [sql for sql, _ in self.calls
                if f"FROM {table}" in sql and sql.lstrip().upper().startswith("SELECT")]
        assert len(hits) == 1, f"expected one SELECT on {table}, got {len(hits)}"
        return hits[0]

    def columns_for(self, table: str) -> list[str]:
        return next(cols for sql, cols in self.calls if f"FROM {table}" in sql)


def test_activity_columns_is_every_activities_column_except_raw_json(rc_db):
    """The frozen list must stay exhaustive. A column added to db.SCHEMA (or by
    an init_schema ALTER, like `source`) and not added here would be silently
    unreadable everywhere the whole-row fetch is used — this fails the build
    instead. raw_json is the one deliberate omission."""
    with db.connect(rc_db) as conn:
        actual = [r["name"] for r in conn.execute("PRAGMA table_info(activities)")]
    assert list(rc._ACTIVITY_COLUMNS) == [c for c in actual if c != "raw_json"]
    assert "raw_json" in actual                     # ...and it really is a column
    assert "source" in rc._ACTIVITY_COLUMNS         # the ALTER-added one


def test_select_activity_never_fetches_raw_json_on_any_branch(rc_db):
    """raw_json is ~50 KB of preserved Garmin payload per row. All three
    resolution branches used to `SELECT *` and hand it back for the caller to
    pop — decoded out of SQLite only to be discarded."""
    today = date.today().isoformat()
    for kwargs in ({"activity_id": 1}, {"target_date": today}, {}):
        with db.connect(rc_db) as conn:
            tracer = TracingConn(conn)
            row = rc._select_activity(
                tracer, kwargs.get("activity_id"), kwargs.get("target_date"))
        sql, cols = tracer.calls[0]
        assert "SELECT *" not in sql, kwargs
        assert "raw_json" not in cols, kwargs
        assert cols == list(rc._ACTIVITY_COLUMNS), kwargs
        assert row["activity_id"] == 1              # branch still resolves


def test_load_report_card_inputs_hands_back_no_raw_json_and_never_popped_one(rc_db):
    """The pop in load_report_card_inputs is gone, so this is now a claim about
    the query rather than about cleanup after it: the key is absent because it
    was never selected."""
    with db.connect(rc_db) as conn:
        conn.execute(
            "UPDATE activities SET raw_json = ? WHERE activity_id = 1",
            ('{"big": "' + "x" * 5000 + '"}',),
        )
        tracer = TracingConn(conn)
        inputs = rc.load_report_card_inputs(tracer)
    assert "raw_json" not in inputs["activity"]
    assert "raw_json" not in tracer.columns_for("activities")
    # The rest of the row is intact — pruning dropped exactly one column.
    assert inputs["activity"]["activity_name"] == "Morning Run"
    assert inputs["activity"]["training_load"] == 100


def test_default_activity_lookup_is_served_by_the_ordering_index(rc_db):
    """`ORDER BY date DESC, start_time DESC` against the single-column
    idx_activities_date forced a USE TEMP B-TREE — SQLite materializing and
    sorting the whole activities table to return one row. Explain the SQL the
    code actually issued, not a copy of it."""
    with db.connect(rc_db) as conn:
        tracer = TracingConn(conn)
        rc._select_activity(tracer, None, None)
        sql = tracer.sql_for("activities")
        plan = " ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql))
    assert "idx_activities_date_start" in plan
    assert "TEMP B-TREE" not in plan


def test_load_inputs_defaults_to_the_most_recent_activity(rc_db):
    with db.connect(rc_db) as conn:
        inputs = rc.load_report_card_inputs(conn)
    assert inputs["activity"]["activity_id"] == 1
    assert len(inputs["splits"]) == len(MILE_SPLITS)
    assert inputs["context"]["ctl"] == 40.0


def test_load_inputs_skips_activities_with_no_distance(rc_db):
    """A strength session has nothing this card can grade, so the default must
    not land on one."""
    with db.connect(rc_db) as conn:
        conn.execute(
            "INSERT INTO activities (activity_id, date, start_time, activity_type, "
            "duration_seconds, distance_meters) VALUES (999, ?, ?, 'strength_training', "
            "1800, 0)",
            (date.today().isoformat(), date.today().isoformat() + " 20:00:00"),
        )
        inputs = rc.load_report_card_inputs(conn)
    assert inputs["activity"]["activity_id"] == 1


def test_explicit_activity_id_bypasses_the_distance_filter(rc_db):
    with db.connect(rc_db) as conn:
        conn.execute(
            "INSERT INTO activities (activity_id, date, activity_type, "
            "duration_seconds, distance_meters) VALUES (999, ?, 'strength_training', "
            "1800, 0)",
            (date.today().isoformat(),),
        )
        inputs = rc.load_report_card_inputs(conn, activity_id=999)
    assert inputs["activity"]["activity_id"] == 999


def test_load_inputs_by_date_and_miss(rc_db):
    with db.connect(rc_db) as conn:
        assert rc.load_report_card_inputs(
            conn, target_date=date.today().isoformat())["activity"]["activity_id"] == 1
        assert rc.load_report_card_inputs(conn, target_date="1999-01-01") is None


def test_load_inputs_reports_other_activities_on_the_date(rc_db):
    """A double-day must not silently hide its second session — and a bare id
    doesn't tell the reader which session went ungraded."""
    today = date.today().isoformat()
    with db.connect(rc_db) as conn:
        conn.execute(
            "INSERT INTO activities (activity_id, date, start_time, activity_type, "
            "duration_seconds, distance_meters) VALUES (555, ?, ?, 'running', 1200, 5000)",
            (today, today + " 05:00:00"),
        )
        inputs = rc.load_report_card_inputs(conn)
    assert inputs["other_activities_on_date"] == [
        {"activity_id": 555, "activity_type": "running", "distance_mi": 3.11,
         "start_time": today + " 05:00:00"},
    ]


def test_date_branch_grades_the_first_session_of_a_double_day(rc_db):
    """The prescription a date-selected card is graded against is the day's
    lowest seq — the morning session — so taking the day's LAST activity paired
    an evening shakeout with the morning's target and graded the wrong run."""
    today = date.today().isoformat()
    with db.connect(rc_db) as conn:
        conn.execute(
            "INSERT INTO activities (activity_id, date, start_time, activity_type, "
            "activity_name, duration_seconds, distance_meters, avg_hr, "
            "avg_pace_sec_per_km, training_load) VALUES "
            "(777, ?, ?, 'running', 'Evening Shakeout', 1200, 3200, 140, 380, 30)",
            (today, today + " 18:30:00"),
        )
        inputs = rc.load_report_card_inputs(conn, target_date=today)
    assert inputs["activity"]["activity_id"] == 1          # 07:00, not 18:30
    assert inputs["other_activities_on_date"] == [
        {"activity_id": 777, "activity_type": "running", "distance_mi": 1.99,
         "start_time": today + " 18:30:00"},
    ]


def test_explicit_id_still_beats_the_first_session_rule(rc_db):
    """Asking for one activity by id gets that activity, morning or not."""
    today = date.today().isoformat()
    with db.connect(rc_db) as conn:
        conn.execute(
            "INSERT INTO activities (activity_id, date, start_time, activity_type, "
            "duration_seconds, distance_meters) VALUES (777, ?, ?, 'running', 1200, 3200)",
            (today, today + " 18:30:00"),
        )
        inputs = rc.load_report_card_inputs(conn, activity_id=777)
    assert inputs["activity"]["activity_id"] == 777


def test_load_inputs_picks_the_planned_workout_for_that_date(rc_db):
    from local_fitness import plans

    today = date.today().isoformat()
    with db.connect(rc_db) as conn:
        conn.execute(
            "INSERT INTO training_plans (plan_id, status, goal_type, race_date, "
            "title, created_at) VALUES (7, 'active', '10k', ?, 'Plan', ?)",
            ((date.today() + timedelta(days=30)).isoformat(), today),
        )
        for seq, dist in ((2, 8000), (1, 12000)):
            conn.execute(
                "INSERT INTO plan_workouts (plan_id, date, seq, week_index, type, "
                "target_distance_m, description) VALUES (7, ?, ?, 1, 'easy', ?, 'Easy')",
                (today, seq, dist),
            )
        inputs = rc.load_report_card_inputs(conn)
    assert plans is not None
    # Lowest seq is the day's primary session.
    assert inputs["plan_workout"]["target_distance_m"] == 12000


def test_build_card_end_to_end_against_the_db(rc_db):
    with db.connect(rc_db) as conn:
        inputs = rc.load_report_card_inputs(conn)
    card = rc.build_card(
        inputs["activity"], inputs["splits"], inputs["plan_workout"],
        inputs["reference"], inputs["context"],
    )
    # Identical to the median on every axis -> straight As.
    assert card["overall"]["grade"] == "A"
    assert card["splits"]["unit"] == "Mile"
    assert "Mile 1" in rc.render_markdown(card)


# --- HR is graded against a BAND, and the card must say so -----------------

def test_hr_expected_is_the_bound_the_grade_was_measured_against():
    """Regression for a card that contradicted itself: an easy run at 136
    against a 146 median rendered "expected 146, -7%" beside a B+, when the
    grade actually came from being 6% ABOVE the 0.97x ceiling. Expected must
    be the number the deviation was computed from, like every other metric."""
    ref = {**REF, "median_hr": 146.0}
    card = card_for(
        {"date": "2026-07-19", "activity_name": "easy shakeout",
         "distance_meters": 4800, "duration_seconds": 1800,
         "avg_pace_sec_per_km": 375, "avg_hr": 160, "training_load": 60},
        reference=ref,
    )
    hr = card["metrics"]["hr"]
    ceiling = 0.97 * 146.0
    assert card["intent_class"] == "easy"
    assert hr["expected"] == pytest.approx(ceiling)
    assert hr["expected"] != 146.0          # NOT the bare median
    # And the delta agrees with the grade's direction: over the ceiling.
    assert rc._delta_text("hr", hr).startswith("+")


def test_hr_inside_the_band_reads_in_range_not_a_percentage():
    # An easy run below the ceiling is an A; a percentage against one edge
    # would imply a miss that did not happen.
    ref = {**REF, "median_hr": 150.0}
    card = card_for(
        {"date": "2026-07-19", "activity_name": "recovery jog",
         "distance_meters": 4800, "duration_seconds": 1800,
         "avg_pace_sec_per_km": 375, "avg_hr": 130, "training_load": 60},
        reference=ref,
    )
    hr = card["metrics"]["hr"]
    assert hr["in_band"] is True
    assert rc.base_letter(hr["grade"]) == "A"
    assert rc._delta_text("hr", hr) == "in range"


@pytest.mark.parametrize("cls,expected", [
    ("easy", "≤ 146 bpm"),
    ("long", "≤ 150 bpm"),
    ("quality", "≥ 150 bpm"),
    ("steady", "140–160 bpm"),
])
def test_hr_band_renders_as_a_ceiling_floor_or_range(cls, expected):
    lo, hi = rc.hr_band_bounds(150.0, cls)
    assert rc._fmt_hr_band(lo, hi) == expected


def test_hr_band_bounds_are_none_without_a_median():
    assert rc.hr_band_bounds(None, "easy") == (None, None)
    assert rc._fmt_hr_band(None, None) == "—"


def test_easy_hr_ceiling_is_actually_reachable():
    """The calibration guard. The old 0.88 ceiling demanded 12% below the
    mixed-intent median — a number that appeared in 1 of 13 real runs, so
    every ordinary easy run was marked too hot and HR was a standing penalty
    rather than a judgment. An easy run at the median must not be an F."""
    median_hr = 146.0
    _, ceiling = rc.hr_band_bounds(median_hr, "easy")
    # A typical easy run sits a few percent under the all-run median.
    typical_easy = 0.95 * median_hr
    assert typical_easy <= ceiling
    # And a genuinely hot "easy" run still gets marked down.
    d = rc.hr_deviation(1.10 * median_hr, median_hr, "easy")
    assert rc.base_letter(rc.grade_from_deviation(d)) in ("B", "C", "D", "F")


def test_expected_text_falls_back_to_the_numeric_formatter():
    # Only HR carries a display string; every other metric formats its number.
    card = card_for({"date": "2026-07-19", "distance_meters": 10000,
                     "duration_seconds": 3000, "avg_pace_sec_per_km": 300,
                     "avg_hr": 150, "training_load": 100})
    assert rc.expected_text("distance", card["metrics"]["distance"]) == "6.21 mi"
    assert rc.expected_text("load", {"expected": None}) == "—"


# --- a plan target is an instruction, a median is a reference --------------

def test_plan_pace_is_graded_tighter_than_a_rolling_reference():
    """The same 9.5%-fast miss scores differently depending on whether a plan
    actually prescribed the pace. Without this, a prescribed 10:28 easy run
    executed at 9:28 — the entire failure mode an easy day has — took a B- and
    the card handed the run an overall A."""
    plan_card = card_for(
        {"date": "2026-07-19", "distance_meters": 4800, "duration_seconds": 1800,
         "avg_pace_sec_per_km": 351, "avg_hr": 136, "training_load": 51},
        plan={"type": "easy", "target_distance_m": 4800,
              "target_pace_sec_per_km": 388, "seq": 1},
    )
    pace = plan_card["metrics"]["pace"]
    assert pace["reference"] == "plan"
    # ~9.5% fast: a C against a prescription, where the untightened bands said B.
    assert rc.base_letter(pace["grade"]) == "C"


def test_running_the_prescribed_pace_still_earns_an_A():
    # The tightening must not make a well-executed plan day unachievable:
    # within 3% of target is still an A.
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 4800, "duration_seconds": 1800,
         "avg_pace_sec_per_km": 380, "avg_hr": 136, "training_load": 51},
        plan={"type": "easy", "target_distance_m": 4800,
              "target_pace_sec_per_km": 388, "seq": 1},
    )
    assert rc.base_letter(card["metrics"]["pace"]["grade"]) == "A"
    assert rc.base_letter(card["metrics"]["distance"]["grade"]) == "A"


def test_overall_grade_cannot_be_an_A_when_the_prescription_was_missed():
    """The self-consistency check: a card whose coaching read says the easy day
    was run at tempo must not print an overall A."""
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 4925, "duration_seconds": 1736,
         "avg_pace_sec_per_km": 352, "avg_hr": 136, "training_load": 51},
        plan={"type": "easy", "target_distance_m": 4828,
              "target_pace_sec_per_km": 390, "seq": 1},
        reference={**REF, "median_hr": 146.0, "median_load": 53.0},
    )
    assert card["overall"]["grade"] != "A"


# === locomotion gating =====================================================
# `activity_type` is Garmin's label, not a measurement: a walking-desk session
# logs as `treadmill_running`. Measured 2026-07-21, that put the "median
# comparable activity" at a 15:50/mi walk (116 bpm, 22 load) and handed a real
# interval session an A+ on both HR and load for clearing a walking bar.

@pytest.mark.parametrize(("pace_sec_per_km", "expected"), [
    (300.0, True),    # 8:03/mi — running
    (484.0, True),    # 12:59/mi — just inside the ceiling
    (485.0, False),   # 13:00/mi — the boundary itself is walking
    (600.0, False),   # 16:05/mi — the walking-desk regime
    (3127.0, False),  # 83:49/mi — a paused watch
])
def test_is_running_effort_splits_on_pace_not_label(pace_sec_per_km, expected):
    assert rc.is_running_effort(pace_sec_per_km) is expected


@pytest.mark.parametrize("pace", [None, 0, 0.0, -1.0])
def test_is_running_effort_is_none_when_pace_is_unusable(pace):
    """A third state, not False. `None == True` and `None == False` are both
    False, so an equality filter drops a paceless row from BOTH pools instead
    of silently filing it under walking."""
    assert rc.is_running_effort(pace) is None


@pytest.fixture
def bimodal_db(tmp_path, monkeypatch):
    """A `treadmill_running` pool shaped like the real one: 8 genuine runs and
    12 walking-desk sessions, all under the same activity_type.

    The two activities under grading sit at offset 1 and the pool starts at
    offset 2, because the reference window ends the day *before* the graded
    activity — same-day peers would silently drop out and make every count in
    these tests an off-by-one puzzle.
    """
    p = tmp_path / "bimodal.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    today = date.today()
    with db.connect(p) as conn:
        # The graded pair: one run, one walking-desk session, same type.
        rows = [(1, 1, "treadmill_running", 10000, 150, 400.0, 100.0),
                (2, 1, "treadmill_running", 5000, 95, 1000.0, 10.0)]
        # Runs: 10:44/mi, HR 150, load 100.
        for i in range(8):
            rows.append((301 + i, i + 2, "treadmill_running", 10000, 150, 400.0, 100.0))
        # Walking desk: 26:49/mi, HR 95, load 10.
        for i in range(12):
            rows.append((401 + i, i + 2, "treadmill_running", 5000, 95, 1000.0, 10.0))
        for aid, offset, atype, dist, hr, pace, load in rows:
            d = (today - timedelta(days=offset)).isoformat()
            conn.execute(
                "INSERT INTO activities (activity_id, date, start_time, activity_type,"
                " activity_name, duration_seconds, distance_meters, avg_hr,"
                " avg_pace_sec_per_km, training_load)"
                " VALUES (?, ?, ?, ?, 'X', 3000, ?, ?, ?, ?)",
                (aid, d, f"{d} 07:00:00", atype, dist, hr, pace, load),
            )
    return p


def _graded(conn, activity_id):
    return dict(conn.execute(
        "SELECT * FROM activities WHERE activity_id=?", (activity_id,)).fetchone())


def test_a_run_is_graded_against_runs_only(bimodal_db):
    """The regression that motivated the filter. Without it the median HR is
    dragged from 150 to ~95 and the median load from 100 to ~10, which is how
    a genuine interval session scored A+ on both."""
    with db.connect(bimodal_db) as conn:
        ref = rc.rolling_reference(conn, _graded(conn, 1))
    assert ref["n"] == 8
    assert ref["excluded_other_mode"] == 12
    assert ref["mode_label"] == "running"
    assert ref["median_hr"] == pytest.approx(150.0)
    assert ref["median_load"] == pytest.approx(100.0)
    assert ref["median_pace_sec_per_km"] == pytest.approx(400.0)


def test_a_walk_is_graded_against_walks_only(bimodal_db):
    """Symmetric: the filter isn't 'drop the walks', it's 'compare like with
    like'. A walking-desk session must not be measured against running."""
    with db.connect(bimodal_db) as conn:
        ref = rc.rolling_reference(conn, _graded(conn, 2))
    assert ref["n"] == 12
    assert ref["excluded_other_mode"] == 8
    assert ref["mode_label"] == "walking"
    assert ref["median_hr"] == pytest.approx(95.0)
    assert ref["median_load"] == pytest.approx(10.0)


def test_locomotion_filter_runs_before_widening(bimodal_db):
    """Widening is the dangerous path: an exact pool thinned below the floor
    would otherwise pull the entire walking corpus in as 'on-foot'."""
    with db.connect(bimodal_db) as conn:
        conn.execute("DELETE FROM activities WHERE activity_id BETWEEN 303 AND 308")
        ref = rc.rolling_reference(conn, _graded(conn, 1))
    # 2 running peers left, below MIN_REFERENCE_ACTIVITIES, and widening finds
    # no more runs — so it reports insufficient rather than grading on walks.
    assert ref["mode"] == "insufficient_data"
    assert ref["n"] == 2


def test_a_paceless_activity_falls_back_to_type_only_comparison(bimodal_db):
    """Its own mode is unknowable, so the filter is skipped rather than
    guessing a side and grading against the wrong half."""
    with db.connect(bimodal_db) as conn:
        act = _graded(conn, 1)
        act["avg_pace_sec_per_km"] = None
        ref = rc.rolling_reference(conn, act)
    assert ref["excluded_other_mode"] == 0
    assert ref["n"] == 20                      # every same-type peer, both modes
    assert ref["mode_label"] is None


def test_reference_line_states_the_exclusion(bimodal_db):
    """The filter is invisible in the numbers — a reader comparing against
    Garmin's own app would see a different median with no way to account for it."""
    with db.connect(bimodal_db) as conn:
        act = _graded(conn, 1)
        card = rc.build_card(act, [], None, rc.rolling_reference(conn, act))
    line = rc.reference_line(card)
    assert "12 same-window walking-effort activities excluded" in line
    assert "Garmin labels them the same, the pace says otherwise" in line


def test_reference_line_says_nothing_when_nothing_was_excluded():
    card = card_for({"date": "2026-07-19", "distance_meters": 10000,
                     "avg_pace_sec_per_km": 300, "avg_hr": 150})
    assert "excluded" not in rc.reference_line(card)


# === plan-reference walk gate ==============================================
# Batch 4a fix: the plan branch was the one place in the module that never
# checked measured locomotion at all, so a walk-effort activity could be
# graded straight against a RUNNING plan target.

# 9:39/mi and 14:09/mi, converted to sec/km exactly as `is_running_effort`
# does (`* units.KM_PER_MILE` inverted) — the live 2026-07-20 case.
_EASY_TARGET_PACE = (9 * 60 + 39) / rc.units.KM_PER_MILE      # 359.76 s/km
_WALKED_PACE = (14 * 60 + 9) / rc.units.KM_PER_MILE           # 527.50 s/km


def test_pace_deviation_easy_long_has_a_slow_side_floor():
    """`pace_deviation`'s own contract, isolated from `build_card`: a pace past
    the run/walk boundary can never read as a perfectly-graded easy pace,
    regardless of how much slower the expectation itself is."""
    d = rc.pace_deviation(_WALKED_PACE, _EASY_TARGET_PACE, "easy")
    assert d > 0.0
    assert rc.base_letter(rc.grade_from_deviation(d)) != "A"


def test_pace_deviation_floor_does_not_fire_on_a_genuine_slow_run():
    """The over-correction guard: an honest easy run up to a 13:00 mile is
    still ungated — only crossing the walk boundary trips the floor."""
    just_inside = rc.RUN_PACE_CEILING_SEC_PER_MI / rc.units.KM_PER_MILE - 1
    d = rc.pace_deviation(just_inside, _EASY_TARGET_PACE, "easy")
    assert d == pytest.approx(0.0)


def test_walk_against_a_running_plan_type_refuses_the_plan_reference():
    """Live case, 2026-07-20: 4.00 mi at 14:09/mi (measured walking effort)
    against a 9:39/mi easy prescription. Before this fix the plan branch
    never checked locomotion and graded distance A+, pace A+."""
    activity = {"date": "2026-07-20", "distance_meters": 6437.0,
                "duration_seconds": 6789, "avg_pace_sec_per_km": _WALKED_PACE,
                "avg_hr": 112, "training_load": 40}
    plan = {"type": "easy", "target_distance_m": 6437.0,
            "target_pace_sec_per_km": _EASY_TARGET_PACE, "seq": 1}
    card = card_for(activity, plan=plan)
    distance, pace = card["metrics"]["distance"], card["metrics"]["pace"]
    assert distance["reference"] != "plan"
    assert pace["reference"] != "plan"
    assert distance["note"] and "walk" in distance["note"]
    assert pace["note"] and "walk" in pace["note"]
    assert rc.base_letter(pace["grade"]) != "A"


def test_walk_against_a_running_plan_type_falls_to_rolling_reference():
    """The refusal doesn't just blank the metric — it falls through to the
    rolling reference (a walk pool, since `rolling_reference` gates the SAME
    way on the graded activity's own mode) exactly like a plan-free day."""
    activity = {"date": "2026-07-20", "distance_meters": 6437.0,
                "duration_seconds": 6789, "avg_pace_sec_per_km": _WALKED_PACE,
                "avg_hr": 112, "training_load": 40}
    plan = {"type": "easy", "target_distance_m": 6437.0,
            "target_pace_sec_per_km": _EASY_TARGET_PACE, "seq": 1}
    card = card_for(activity, plan=plan)
    assert card["metrics"]["distance"]["reference"] == "rolling_60d"
    assert card["metrics"]["pace"]["reference"] == "rolling_60d"


def test_cross_day_walk_does_not_trigger_the_plan_walk_gate():
    """"cross" is deliberately non-running cross-training — a walk on a cross
    day is the point, not a mismatch, so its plan reference (when it has one)
    must not be refused."""
    activity = {"date": "2026-07-20", "distance_meters": 6437.0,
                "avg_pace_sec_per_km": _WALKED_PACE, "avg_hr": 112,
                "training_load": 40}
    plan = {"type": "cross", "target_distance_m": 6437.0, "seq": 1}
    card = card_for(activity, plan=plan)
    assert card["metrics"]["distance"]["reference"] == "plan"


def test_a_slow_but_real_easy_run_still_uses_the_plan_reference():
    """Neither the mismatch gate nor the pace floor may fire on an ordinary,
    honestly-run easy day that is merely slower than prescribed — the
    headline "don't over-correct" case from the same fix."""
    activity = {"date": "2026-07-20", "distance_meters": 6437.0,
                "duration_seconds": 3000, "avg_pace_sec_per_km": 466.0,  # ~12:30/mi
                "avg_hr": 130, "training_load": 45}
    plan = {"type": "easy", "target_distance_m": 6437.0,
            "target_pace_sec_per_km": _EASY_TARGET_PACE, "seq": 1}
    card = card_for(activity, plan=plan)
    assert card["metrics"]["distance"]["reference"] == "plan"
    assert card["metrics"]["pace"]["reference"] == "plan"
    assert rc.base_letter(card["metrics"]["pace"]["grade"]) == "A"


def test_quality_pace_floor_closes_the_walked_tempo_gap():
    """Residual found while re-sweeping the fix: a walk on a prescribed
    'tempo' day refuses its plan pace reference (mismatch gate) and falls to
    the rolling reference — but that reference is itself a WALKING-pool
    median for a walked activity, and quality's slow-only gate scored 0.0
    whenever the walk was brisker than that (often very slow) median. Live
    case, 2026-07-14: a 15.20 min/mi walk on a tempo day still graded A+
    pace even after the plan-gate + easy/long floor fix."""
    d = rc.pace_deviation(3125.3, 800.0, "quality")  # 83:49/mi vs a slow walk median
    assert d > 0.0
    assert rc.base_letter(rc.grade_from_deviation(d)) != "A"


def test_quality_pace_floor_does_not_touch_a_genuine_fast_tempo():
    """A real tempo run beating its target is still an A, uncapped — the
    floor only fires once actual pace crosses the walk boundary, which a
    legitimate tempo effort never does."""
    assert rc.pace_deviation(240.0, 300.0, "quality") == 0.0


def test_walking_desk_session_pace_floor_applies_even_without_a_plan():
    """Case 2 from the evidence: 2.42 mi at 83:49/mi, inferred 'easy' off its
    own low HR (no plan involved at all). Without the floor this scored a
    deviation of 0.0 — a perfect A+ pace grade for an 83-minute mile."""
    activity = {"date": "2026-05-22", "distance_meters": 3897.0,
                "duration_seconds": 12180, "avg_pace_sec_per_km": 3125.3,
                "avg_hr": 90, "training_load": 8}
    card = card_for(activity)
    assert card["intent_class"] == "easy"
    assert rc.base_letter(card["metrics"]["pace"]["grade"]) == "F"


# --- _select_activity: prefer the real run over a leading walk -------------

def test_date_branch_prefers_a_later_run_over_a_leading_walk(rc_db):
    """The corpus's real shape 26 times over: the FIRST session of the day is
    a walk and the real run comes later. Live case, 2026-07-21: a 3.23 mi @
    29:15/mi walk was selected over a 5.95 mi @ 10:42/mi interval run."""
    today = date.today().isoformat()
    with db.connect(rc_db) as conn:
        conn.execute(
            "UPDATE activities SET activity_type='walking', distance_meters=5200, "
            "avg_pace_sec_per_km=1100, avg_hr=95 WHERE activity_id=1")
        conn.execute(
            "INSERT INTO activities (activity_id, date, start_time, activity_type, "
            "activity_name, duration_seconds, distance_meters, avg_hr, "
            "avg_pace_sec_per_km, training_load) VALUES "
            "(888, ?, ?, 'running', 'Intervals', 3576, 9577, 164, 400, 90)",
            (today, today + " 17:30:00"),
        )
        row = rc._select_activity(conn, None, today)
    assert row["activity_id"] == 888


def test_date_branch_falls_back_to_first_when_the_day_is_all_walking(rc_db):
    """A genuine walk day (no running-effort session at all) must still be
    gradeable — falls back to the earliest session, same as before."""
    today = date.today().isoformat()
    with db.connect(rc_db) as conn:
        conn.execute(
            "UPDATE activities SET activity_type='walking', distance_meters=5200, "
            "avg_pace_sec_per_km=1100, avg_hr=95 WHERE activity_id=1")
        conn.execute(
            "INSERT INTO activities (activity_id, date, start_time, activity_type, "
            "duration_seconds, distance_meters, avg_pace_sec_per_km) VALUES "
            "(889, ?, ?, 'walking', 2000, 4000, 1200)",
            (today, today + " 18:00:00"),
        )
        row = rc._select_activity(conn, None, today)
    assert row["activity_id"] == 1


def test_date_branch_a_paceless_activity_still_resolves(rc_db):
    """An unknowable-mode row keeps today's default behavior rather than
    being forced into the running or the walking bucket."""
    today = date.today().isoformat()
    with db.connect(rc_db) as conn:
        conn.execute("UPDATE activities SET avg_pace_sec_per_km=NULL WHERE activity_id=1")
        row = rc._select_activity(conn, None, today)
    assert row["activity_id"] == 1


# === quality-day pace ======================================================
# The one documented exception to "no grade reads activity_splits".

def _mile_splits(*paces_sec_per_km):
    """Full one-mile splits, plus a fast trailing fragment that must be ignored."""
    rows = [{"split_index": i, "distance_meters": rc.MILE_M, "duration_seconds": 480,
             "avg_hr": 160, "avg_pace_sec_per_km": p}
            for i, p in enumerate(paces_sec_per_km)]
    rows.append({"split_index": len(paces_sec_per_km), "distance_meters": 90.0,
                 "duration_seconds": 20, "avg_hr": 170,
                 "avg_pace_sec_per_km": 100.0})
    return rows


def test_fastest_rep_split_ignores_the_trailing_fragment():
    """A 90-metre fragment can post an absurd pace and would win every time."""
    labelled = rc.label_splits(_mile_splits(360.0, 300.0, 330.0))
    assert rc.fastest_rep_split_pace(labelled) == pytest.approx(300.0)


def test_fastest_rep_split_is_none_without_splits():
    assert rc.fastest_rep_split_pace(rc.label_splits([])) is None


def _manual_lap_splits():
    """A manually-lapped interval session: one warmup lap, then 800m reps with
    200m recovery jogs between them. The warmup is the LONGEST lap, so
    label_splits flags every rep partial — which is exactly the shape that used
    to hand the warmup's pace to the rep grade."""
    rows = [{"split_index": 0, "distance_meters": 1600.0, "duration_seconds": 624,
             "avg_hr": 130, "avg_pace_sec_per_km": 390.0}]
    for i in range(4):
        rows.append({"split_index": 1 + i * 2, "distance_meters": 800.0,
                     "duration_seconds": 208, "avg_hr": 168,
                     "avg_pace_sec_per_km": 260.0})
        rows.append({"split_index": 2 + i * 2, "distance_meters": 200.0,
                     "duration_seconds": 120, "avg_hr": 140,
                     "avg_pace_sec_per_km": 600.0})
    return rows


def test_fastest_rep_split_takes_the_rep_not_the_warmup_lap():
    """The manual-lap failure: every rep is "partial" against a 1600m warmup, so
    the old full-splits-only rule left the 6:30/km warmup as the only
    candidate."""
    labelled = rc.label_splits(_manual_lap_splits())
    assert all(r["partial"] for r in labelled["rows"][1:])   # reps and recoveries
    assert rc.fastest_rep_split_pace(labelled) == pytest.approx(260.0)


def test_fastest_rep_split_ignores_recovery_jogs_below_the_floor():
    """The 200m recovery jogs are under QUALITY_MIN_SPLIT_M, so they can't win
    the comparison even though they are the shortest rows present."""
    short = [{"split_index": i, "distance_meters": 200.0, "duration_seconds": 60,
              "avg_hr": 150, "avg_pace_sec_per_km": 300.0 - i} for i in range(4)]
    assert rc.QUALITY_MIN_SPLIT_M == 300.0
    assert rc.fastest_rep_split_pace(rc.label_splits(short)) is None


def test_interval_pace_is_graded_on_the_fastest_split():
    """The headline fix. A whole-run average bakes in the warmup, the recovery
    jogs and the cooldown, so grading it against a REP target is not a strict
    rubric — it is an arithmetic guarantee of an F."""
    card = card_for(
        # Averages 10:42/mi across the session; best mile is 5:00/km (8:03/mi).
        {"date": "2026-07-21", "distance_meters": 9575, "duration_seconds": 3822,
         "avg_pace_sec_per_km": 399.2, "avg_hr": 150, "training_load": 100},
        plan={"type": "interval", "target_distance_m": 8047,
              "target_pace_sec_per_km": 300.0, "seq": 1},
        splits=_mile_splits(420.0, 300.0, 450.0),
    )
    pace = card["metrics"]["pace"]
    assert pace["actual"] == pytest.approx(300.0)          # the fastest split
    assert pace["actual_display"] == "8:03/mi best mile"
    assert pace["deviation"] == pytest.approx(0.0)         # hit the rep target
    assert pace["grade"] == "A+"
    # No note: "8:03/mi best mile" beside a "5:00/mi" target already says what
    # was compared, and the PDF's one-page budget is real — this bullet alone
    # pushed a 6-split card onto a second page.
    assert pace.get("note") is None


def test_interval_pace_still_fails_when_the_reps_were_missed():
    """The exception must not become a free pass — it changes WHICH number is
    graded, not whether a missed workout is called out."""
    card = card_for(
        {"date": "2026-07-21", "distance_meters": 9575, "duration_seconds": 3822,
         "avg_pace_sec_per_km": 399.2, "avg_hr": 150, "training_load": 100},
        plan={"type": "interval", "target_distance_m": 8047,
              "target_pace_sec_per_km": 260.0, "seq": 1},
        splits=_mile_splits(420.0, 351.0, 450.0),
    )
    pace = card["metrics"]["pace"]
    # Best mile 351 vs a 260 target: (351-260)/260 = 0.35, and PLAN_TIGHTEN
    # scales the F boundary to 0.35*0.6 = 0.21.
    assert pace["deviation"] == pytest.approx(0.35, abs=1e-3)
    assert pace["grade"] == "F"


def test_interval_pace_is_na_without_splits_not_a_fabricated_f():
    """~88% of history is backfilled and carries no splits. The metric refuses
    rather than falling back to the comparison it exists to avoid."""
    card = card_for(
        {"date": "2026-07-21", "distance_meters": 9575, "duration_seconds": 3822,
         "avg_pace_sec_per_km": 399.2, "avg_hr": 150, "training_load": 100},
        plan={"type": "interval", "target_distance_m": 8047,
              "target_pace_sec_per_km": 300.0, "seq": 1},
    )
    pace = card["metrics"]["pace"]
    assert pace["grade"] is None
    assert pace["deviation"] is None
    assert pace["actual"] == pytest.approx(399.2)          # the average is kept
    assert pace["actual_display"] == "10:42/mi avg"
    assert "no splits recorded" in pace["note"]
    # ...and the weight redistributes rather than scoring pace as zero.
    # 4 compliance metrics; pace n/a for its own reason and continuity n/a for
    # want of splits, leaving distance and HR.
    assert card["overall"]["graded_metrics"] == 2


def test_manual_lap_interval_is_graded_on_the_rep_not_the_warmup():
    """The manual-lap failure end to end: a 2-mile-warmup-then-800s session had
    every rep flagged partial, so the rep grade read the 6:30/km warmup against
    a 4:20/km target — a guaranteed F on a session that hit every rep."""
    card = card_for(
        {"date": "2026-07-21", "distance_meters": 5600, "duration_seconds": 2000,
         "avg_pace_sec_per_km": 357.1, "avg_hr": 155, "training_load": 100},
        plan={"type": "interval", "target_distance_m": 5600,
              "target_pace_sec_per_km": 260.0, "seq": 1},
        splits=_manual_lap_splits(),
    )
    pace = card["metrics"]["pace"]
    assert pace["actual"] == pytest.approx(260.0)      # the 800m rep, not 390
    assert pace["deviation"] == pytest.approx(0.0)
    assert pace["grade"] == "A+"
    # The 1600m warmup is mile-sized, so the split table's unit is "Mile" — but
    # the graded rep is not a mile and the card must not say it was.
    assert card["splits"]["unit"] == "Mile"
    assert pace["actual_display"] == "6:58/mi best split"


def test_interval_pace_is_na_when_no_split_is_rep_sized():
    """Splits exist but none clears the floor — still n/a, and the note says
    why rather than repeating the no-splits wording."""
    tiny = [{"split_index": i, "distance_meters": 200.0, "duration_seconds": 60,
             "avg_hr": 150, "avg_pace_sec_per_km": 300.0} for i in range(6)]
    card = card_for(
        {"date": "2026-07-21", "distance_meters": 9575, "duration_seconds": 3822,
         "avg_pace_sec_per_km": 399.2, "avg_hr": 150, "training_load": 100},
        plan={"type": "interval", "target_distance_m": 8047,
              "target_pace_sec_per_km": 300.0, "seq": 1},
        splits=tiny,
    )
    pace = card["metrics"]["pace"]
    assert pace["grade"] is None
    assert pace["note"] == ("interval day, no split long enough to be a rep — "
                            "average pace can't be graded against a rep target")
    assert pace["actual_display"] == "10:42/mi avg"
    # Continuity IS graded here — 6 evenly-paced 200 m splits, none partial, so
    # they clear MIN_CONTINUITY_SPLITS even though none is rep-sized. Distance,
    # HR and continuity remain; only pace redistributes.
    assert card["overall"]["graded_metrics"] == 3


def test_ungraded_metric_prints_no_delta():
    """A delta beside an n/a re-makes the very comparison the n/a refuses."""
    card = card_for(
        {"date": "2026-07-21", "distance_meters": 9575, "duration_seconds": 3822,
         "avg_pace_sec_per_km": 399.2, "avg_hr": 150, "training_load": 100},
        plan={"type": "interval", "target_distance_m": 8047,
              "target_pace_sec_per_km": 300.0, "seq": 1},
    )
    assert rc._delta_text("pace", card["metrics"]["pace"]) == "—"
    assert "224s/mi" not in rc.render_markdown(card)


def test_easy_day_pace_is_untouched_by_the_quality_exception():
    """Scoped to quality intent only — an easy day keeps grading its average,
    and keeps reading no splits at all."""
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 4925, "duration_seconds": 1736,
         "avg_pace_sec_per_km": 352.0, "avg_hr": 136, "training_load": 51},
        plan={"type": "easy", "target_distance_m": 4828,
              "target_pace_sec_per_km": 390.0, "seq": 1},
        splits=_mile_splits(300.0, 310.0, 320.0),
    )
    assert card["metrics"]["pace"]["actual"] == pytest.approx(352.0)
    assert "actual_display" not in card["metrics"]["pace"]


def test_actual_text_falls_back_to_the_plain_number():
    assert rc.actual_text("hr", {"actual": 136.0}) == "136 bpm"
    assert rc.actual_text("hr", {"actual": None}) == "—"


# === intent-weighted composite + the F floor ===============================

def test_intent_weights_let_pace_carry_an_easy_day():
    """Flat weights let the low-information metrics outvote the one metric an
    easy day exists to satisfy. Same grades, different intent, different total.

    The ``load`` entry is deliberately present and deliberately ignored — see
    ``test_a_load_letter_cannot_reach_the_overall_even_if_one_appears``.
    """
    metrics = {
        "distance": {"grade": "A"}, "pace": {"grade": "D"},
        "hr": {"grade": "A"}, "load": {"grade": "A"},
    }
    easy = rc.overall_grade(metrics, "easy")
    long_run = rc.overall_grade(metrics, "long")
    # No continuity key in the fixture, so its 0.15 redistributes over the rest.
    # easy: (.19*4 + .42*1 + .24*4) / .85 = 2.52
    assert easy["gpa"] == pytest.approx(2.52)
    # long: (.45*4 + .20*1 + .20*4) / .85 = 3.29 — distance is the point there.
    assert long_run["gpa"] == pytest.approx(3.29)
    assert easy["grade"] == "B" and long_run["grade"] == "B"


def test_a_load_letter_cannot_reach_the_overall_even_if_one_appears():
    """The 0.40.0 guarantee, tested at the arithmetic rather than at build_card:
    load is absent from every weight table, so even a hand-injected load grade —
    an A or an F — moves neither the GPA nor the cap."""
    base = {"distance": {"grade": "A"}, "pace": {"grade": "A"}, "hr": {"grade": "A"}}
    clean = rc.overall_grade(base, "easy")
    for injected in ("A+", "C", "F"):
        out = rc.overall_grade({**base, "load": {"grade": injected}}, "easy")
        assert out["gpa"] == clean["gpa"] == pytest.approx(4.0)
        assert out["grade"] == "A"
        assert "capped_by" not in out
    assert "load" not in rc.METRIC_WEIGHTS
    assert all("load" not in w for w in rc.INTENT_METRIC_WEIGHTS.values())


def test_steady_intent_keeps_the_neutral_split():
    """No stated intent means no metric can claim to be the point of the day."""
    metrics = {
        "distance": {"grade": "A"}, "pace": {"grade": "D"},
        "hr": {"grade": "A"}, "load": {"grade": "A"},
    }
    # (.30*4 + .30*1 + .25*4) / .85 = 2.94 (continuity absent, redistributed)
    assert rc.overall_grade(metrics, "steady")["gpa"] == pytest.approx(2.94)
    assert rc.overall_grade(metrics)["gpa"] == pytest.approx(2.94)  # default


def test_an_f_caps_the_overall():
    """Two strong metrics must not average an outright failure into an A."""
    metrics = {
        "distance": {"grade": "A+"}, "pace": {"grade": "F"},
        "hr": {"grade": "A"}, "load": {"grade": "A"},
    }
    out = rc.overall_grade(metrics, "long")
    # (.45*4 + .20*0 + .20*4) / .85 = 3.06 -> B, capped to C.
    assert out["gpa"] == pytest.approx(3.06)
    assert out["grade"] == "C"
    assert out["capped_by"] == "F"


def test_the_f_cap_never_raises_a_worse_grade():
    metrics = {"pace": {"grade": "F"}, "hr": {"grade": "F"}}
    out = rc.overall_grade(metrics, "easy")
    assert out["grade"] == "F"
    assert "capped_by" not in out


def test_the_f_cap_is_stated_on_the_card():
    """A long run that nailed distance, HR and load but was run far too slow
    for its prescription: without the cap the composite reads B over an F."""
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 20000, "duration_seconds": 9000,
         "avg_pace_sec_per_km": 200.0, "avg_hr": 150, "training_load": 140},
        plan={"type": "long", "target_distance_m": 20000,
              "target_pace_sec_per_km": 300.0, "seq": 1},
    )
    assert card["metrics"]["pace"]["grade"] == "F"
    assert card["overall"]["gpa"] > 2.5          # would have printed a B
    assert card["overall"]["grade"] == "C"
    assert card["overall"]["capped_by"] == "F"
    assert "capped at C" in rc.render_markdown(card)


def test_exclusion_count_covers_only_mislabelled_rows(bimodal_db):
    """The card's sentence claims "Garmin labels them the same". A genuinely
    typed `walking` row was never a candidate for a running pool, so counting
    it would overstate the mislabelling the filter exists to undo."""
    today = date.today()
    with db.connect(bimodal_db) as conn:
        for i in range(3):
            d = (today - timedelta(days=i + 2)).isoformat()
            conn.execute(
                "INSERT INTO activities (activity_id, date, start_time,"
                " activity_type, activity_name, duration_seconds, distance_meters,"
                " avg_hr, avg_pace_sec_per_km, training_load)"
                " VALUES (?, ?, ?, 'walking', 'Walk', 3000, 4000, 95, 1000.0, 10.0)",
                (900 + i, d, f"{d} 12:00:00"),
            )
        ref = rc.rolling_reference(conn, _graded(conn, 1))
    # Still 12 — the 3 honestly-typed walks are excluded from the pool but are
    # not counted as mislabelled.
    assert ref["excluded_other_mode"] == 12
    assert ref["n"] == 8


def test_report_card_imports_without_workout_coach():
    """0.38.0 (M1): READ_SECTIONS lives here now — report_card must be fully
    importable without workout_coach ever entering sys.modules (the old
    direction forced lazy imports in three consumers)."""
    import subprocess
    import sys

    code = (
        "import sys; from local_fitness.agent import report_card; "
        "assert report_card.READ_SECTIONS[0] == ('distance', 'DISTANCE'); "
        "sys.exit(1 if 'local_fitness.agent.workout_coach' in sys.modules else 0)"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode()


def test_delta_text_never_prints_negative_zero_percent():
    """A live card printed 'Distance ... -0%' for 4.00-of-4.00 miles (short
    by meters): within rounding of zero IS on target — say so."""
    just_under = {"actual": 6432.0, "expected": 6437.376, "grade": "A+"}
    assert rc._delta_text("distance", just_under) == "on target"
    just_over = {"actual": 6440.0, "expected": 6437.376, "grade": "A+"}
    assert rc._delta_text("distance", just_over) == "on target"
    real_gap = {"actual": 7000.0, "expected": 6437.376, "grade": "B"}
    assert rc._delta_text("distance", real_gap) == "+9%"
