"""Rubric tests for agent/report_card.py.

The whole point of the module is that a grade is derived, not phrased — so
these tests pin the derivation. Everything above the persistence divider is
pure, so most of this file needs no DB at all.

The headline test is `test_easy_run_slower_than_expected_is_not_penalized`:
without direction gating, every recovery run fails on pace.
"""
from __future__ import annotations

from collections import Counter
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
    # Every knot lands exactly on its anchor. These ARE the old GRADE_BANDS
    # boundaries, so a curve that stops passing here has moved a boundary the
    # rubric was calibrated on — including the one HR_CAP_BPM_SCALE depends on.
    (0.0, 5.0),
    (0.05, 4.0),
    (0.10, 3.0),
    (0.20, 2.0),
    (0.35, 1.0),
    (0.50, 1.0),      # saturates, never extrapolates
    (5.0, 1.0),
])
def test_star_curve_hits_every_knot_exactly(d, expected):
    assert rc.stars_from_deviation(d, "hr") == pytest.approx(expected)


def test_star_curve_is_none_in_none_out():
    assert rc.stars_from_deviation(None, "hr") is None


def test_star_curve_interpolates_inside_a_band():
    """The whole point of the change: sub-band position survives instead of
    collapsing to one letter plus a +/- that got stripped before the mean."""
    quarter = rc.stars_from_deviation(0.0125, "hr")
    half = rc.stars_from_deviation(0.025, "hr")
    three_q = rc.stars_from_deviation(0.0375, "hr")
    assert quarter == pytest.approx(4.75)
    assert half == pytest.approx(4.5)
    assert three_q == pytest.approx(4.25)
    assert quarter > half > three_q


def test_star_curve_is_monotone_non_increasing():
    """A worse deviation can never score better. Swept across the whole live
    range at a resolution finer than any band."""
    prev = rc.STAR_MAX + 1
    d = 0.0
    while d <= 1.0:
        s = rc.stars_from_deviation(d, "hr")
        assert s <= prev + 1e-9, f"score rose at d={d}"
        assert rc.STAR_FLOOR <= s <= rc.STAR_MAX
        prev = s
        d += 0.001


def test_widen_scales_the_whole_curve():
    # 0.07 scores 3.5 normally; widened 1.5x it is comfortably higher, and
    # tightened to a plan's 0.6 it is materially lower. Same deviation, three
    # yardsticks — see stars_from_deviation for why that spread is deliberate.
    assert rc.stars_from_deviation(0.07, "hr") == pytest.approx(3.6)
    assert rc.stars_from_deviation(0.07, "hr", rc.STEADY_WIDEN) > 4.0
    assert rc.stars_from_deviation(0.07, "hr", rc.PLAN_TIGHTEN) < 3.0


def test_the_noise_floor_keeps_gps_wobble_at_a_clean_five():
    """A plan distance is two-sided, so it has no exact zeros — 13 of the 17
    plan-referenced runs in the live window sit at d = 0.0002..0.004, which is
    GPS wobble. Those must read a clean 5.00, or the card prints a partial star
    beside a Delta cell that says "on target"."""
    for d in (0.0002, 0.0011, 0.004, 0.0074):
        assert rc.stars_from_deviation(d, "distance") == rc.STAR_MAX
    # ...and the floor is a floor, not a free pass: past it, scoring resumes.
    assert rc.stars_from_deviation(0.02, "distance") < rc.STAR_MAX
    # HR and continuity subtract their own floor upstream, so they get none
    # here — a second floor would move the 11.3 bpm cap boundary.
    assert rc.STAR_NOISE["hr"] == 0.0
    assert rc.STAR_NOISE["continuity"] == 0.0
    assert rc.stars_from_deviation(0.004, "hr") < rc.STAR_MAX


def test_display_stars_never_fakes_or_swallows_a_partial():
    """A full star means the metric earned a full star, and a partial is always
    visibly partial — the clamp holds at .01/.99 where plain rounding fails,
    which is exactly where this distribution puts its mass."""
    assert rc.display_stars(5.0) == 5.0
    assert rc.display_stars(4.99) == 4.75      # never rounds up to a full star
    assert rc.display_stars(4.01) == 4.25      # never rounds down to a bare gap
    assert rc.display_stars(4.5) == 4.5
    assert rc.display_stars(3.6) == 3.5
    assert rc.display_stars(None) is None
    # Banker's rounding would give 4.00 here; floor-then-clamp gives 4.25.
    assert rc.display_stars(4.125) == 4.25


def test_star_bucket_is_not_display_stars():
    """The gate's bucket is plain half-up over the whole range. If it used
    display_stars' clamp, every 4.99 would fall into 4.75 and the top bucket
    would read empty on a perfectly healthy metric."""
    assert rc.star_bucket(4.99) == 5.0
    assert rc.display_stars(4.99) == 4.75
    assert rc.star_bucket(None) is None


def test_star_glyphs_render_the_partial_and_pad_to_five():
    assert rc.star_glyphs(5.0) == "★★★★★"
    assert rc.star_glyphs(4.75) == "★★★★¾"
    assert rc.star_glyphs(4.5) == "★★★★½"
    assert rc.star_glyphs(4.0) == "★★★★☆"
    assert rc.star_glyphs(1.0) == "★☆☆☆☆"
    assert rc.star_glyphs(None) == "n/a"
    # Every row is exactly five glyph positions, or the markdown column goes
    # ragged between a row with a partial and a row without.
    for score in (5.0, 4.99, 4.5, 3.25, 1.0):
        assert len(rc.star_glyphs(score)) == 5


def test_star_display_always_carries_the_numeral():
    """Quarter quantization cannot separate 4.88 from 4.75, and the markdown is
    read aloud by an agent — the glyphs are not speech."""
    assert rc.star_display(4.88) == "★★★★¾ 4.88"
    assert rc.star_display(4.75) == "★★★★¾ 4.75"
    assert rc.star_display(None) == "n/a"


def test_star_verdict_bands_are_monotone_and_bounded():
    assert rc.star_verdict(5.0) == "dead on"
    assert rc.star_verdict(4.90) == "dead on"
    assert rc.star_verdict(4.89) == "on target"
    assert rc.star_verdict(4.25) == "on target"
    assert rc.star_verdict(3.50) == "slightly off target"
    assert rc.star_verdict(2.50) == "off target"
    assert rc.star_verdict(1.50) == "well off target"
    assert rc.star_verdict(1.49) == "missed badly"
    assert rc.star_verdict(None) == "not rated"
    # No band may name a digit, a letter grade, or the word "star" — these
    # words go INTO the model's prompt in place of the score it may not name.
    for _cut, word in rc.STAR_VERDICT_CUTS:
        assert not any(ch.isdigit() for ch in word)
        assert "star" not in word.lower()


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
    assert rc.stars_from_deviation(
        rc.pace_deviation(actual, expected, "easy"), "pace") == rc.STAR_MAX


def test_easy_run_too_fast_is_penalized():
    """The gate is directional, not merely disabled — running the easy day
    hard is still a miss."""
    expected = 330.0
    d = rc.pace_deviation(expected * 0.80, expected, "easy")
    assert d == pytest.approx(0.20)
    # 0.20 is the third knot: exactly 2.0 stars, the old C/D territory.
    assert rc.stars_from_deviation(d, "pace") == pytest.approx(2.03, abs=0.02)


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
    assert card["metrics"]["pace"]["stars"] is None
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
    assert card["metrics"]["load"]["stars"] is None
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
    assert card["metrics"]["load"]["stars"] is None
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
    assert load["stars"] is None
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
    assert card["metrics"]["load"]["stars"] is None
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
    assert card["metrics"]["load"]["stars"] is None
    assert card["stimulus"]["level"] is None
    assert card["stimulus"]["as_intended"] is None
    assert rc.stimulus_lines(card) == []


# --- the prescribed HR cap (0.40.0, regraded 0.40.2) -----------------------

#: Five splits, HR crossing a 140 cap partway through — the shape of the
#: 2026-07-27 session (128 / 139 / 150 / 144 / 159 by mile). Durations differ so
#: a duration-weighted number can't be confused with a plain split count.
CAP_SPLITS = [
    {"split_index": 0, "distance_meters": 1609.34, "duration_seconds": 600, "avg_hr": 128},
    {"split_index": 1, "distance_meters": 1609.34, "duration_seconds": 500, "avg_hr": 139},
    {"split_index": 2, "distance_meters": 1609.34, "duration_seconds": 400, "avg_hr": 150},
    {"split_index": 3, "distance_meters": 1609.34, "duration_seconds": 300, "avg_hr": 144},
    {"split_index": 4, "distance_meters": 1609.34, "duration_seconds": 200, "avg_hr": 159},
]

#: The REAL 2026-08-02 treadmill easy run (activity 23825963527), unmodified:
#: five full mile splits plus the 5-second tail Garmin recorded, against a
#: prescribed 140 bpm cap. Average 139, peak 148, and Garmin logged 0 seconds in
#: zones 4-5. It is the session that exposed the 0.40.0 mis-grade.
LIVE_2026_08_02_SPLITS = [
    {"split_index": 0, "distance_meters": 1609.34, "duration_seconds": 609, "avg_hr": 134},
    {"split_index": 1, "distance_meters": 1609.34, "duration_seconds": 567, "avg_hr": 141},
    {"split_index": 2, "distance_meters": 1609.34, "duration_seconds": 615, "avg_hr": 135},
    {"split_index": 3, "distance_meters": 1609.34, "duration_seconds": 549, "avg_hr": 143},
    {"split_index": 4, "distance_meters": 1609.34, "duration_seconds": 576, "avg_hr": 142},
    {"split_index": 5, "distance_meters": 15.55, "duration_seconds": 5, "avg_hr": 140},
]

#: The REAL 2026-07-22 session (activity 23695862040) against the same 140 cap:
#: average 157, splits reaching 185, and 48% of it in Garmin zones 4-5. The
#: genuine-breach anchor — whatever the fix does, this must keep failing.
LIVE_2026_07_22_SPLITS = [
    {"split_index": 0, "distance_meters": 1609.34, "duration_seconds": 602, "avg_hr": 129},
    {"split_index": 1, "distance_meters": 1609.34, "duration_seconds": 588, "avg_hr": 144},
    {"split_index": 2, "distance_meters": 1609.34, "duration_seconds": 611, "avg_hr": 169},
    {"split_index": 3, "distance_meters": 1609.34, "duration_seconds": 588, "avg_hr": 185},
    {"split_index": 4, "distance_meters": 7.78, "duration_seconds": 2, "avg_hr": 193},
]


def test_time_above_cap_is_duration_weighted_not_split_counted():
    """900 of 2000 seconds sit above 140 (the 400s, 300s and 200s splits). A
    split COUNT would say 3/5 = 60%; the duration weighting says 45%.

    Reported, never graded, since 0.40.2 — see
    test_the_time_fraction_alone_cannot_tell_a_1_bpm_drift_from_a_20_bpm_blowup.
    """
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


def test_the_time_fraction_alone_cannot_tell_a_1_bpm_drift_from_a_20_bpm_blowup():
    """WHY the time fraction stopped being the graded axis in 0.40.2.

    Two real sessions against the same 140 bpm cap: 2026-08-02 sat 1-3 bpm over
    for most of the run and never entered Garmin zone 4, while 2026-07-22 hit
    185 and spent 48% of itself in zones 4-5. Their time-above-cap fractions are
    within 17 points of each other and both land past the old grace, so the old
    axis graded BOTH an F. The exceedance integral separates them by 17x.

    This is the discrimination test: any future replacement for the severity
    measure has to keep these two an order of magnitude apart."""
    mild = rc.label_splits(LIVE_2026_08_02_SPLITS)
    severe = rc.label_splits(LIVE_2026_07_22_SPLITS)

    # The old axis: both "breached", and the milder run reads only slightly
    # better. Feeding either through GRADE_BANDS gives an F.
    assert rc.time_above_cap_fraction(mild, 140.0) == pytest.approx(0.579, abs=0.01)
    assert rc.time_above_cap_fraction(severe, 140.0) == pytest.approx(0.748, abs=0.01)
    assert rc.stars_from_deviation(0.579 - 0.05, "hr") == rc.STAR_FLOOR
    assert rc.stars_from_deviation(0.748 - 0.05, "hr") == rc.STAR_FLOOR

    # The graded axis: 1.2 bpm against 19.5 bpm.
    assert rc.hr_exceedance_bpm(mild, 140.0) == pytest.approx(1.2, abs=0.05)
    assert rc.hr_exceedance_bpm(severe, 140.0) == pytest.approx(19.5, abs=0.05)
    assert rc.hr_exceedance_bpm(severe, 140.0) > 15 * rc.hr_exceedance_bpm(mild, 140.0)


def test_hr_exceedance_is_the_integral_of_the_breach_not_its_duration():
    """Time-weighted mean bpm above the cap. On CAP_SPLITS: 150 for 400s is
    10 bpm over, 144 for 300s is 4, 159 for 200s is 19 — (4000 + 1200 + 3800)
    over 2000 total seconds = 4.5 bpm. Splits UNDER the cap contribute zero, not
    a negative credit: a cap is a ceiling, and running easy early must not buy
    headroom to blow it later."""
    assert rc.hr_exceedance_bpm(rc.label_splits(CAP_SPLITS), 140.0) == pytest.approx(4.5)
    # Halving every breach halves the exceedance — it is linear in magnitude,
    # which is the property the time fraction lacked entirely.
    halved = [dict(s, avg_hr=140 + (s["avg_hr"] - 140) / 2 if s["avg_hr"] > 140
                   else s["avg_hr"]) for s in CAP_SPLITS]
    assert rc.hr_exceedance_bpm(rc.label_splits(halved), 140.0) == pytest.approx(2.25)


def test_hr_exceedance_is_none_without_splits_or_without_a_cap():
    """Same three None paths as the fraction, because the caller degrades on
    exactly this signal: None here means "grade the average alone"."""
    assert rc.hr_exceedance_bpm(rc.label_splits([]), 140.0) is None
    assert rc.hr_exceedance_bpm(rc.label_splits(CAP_SPLITS), None) is None
    assert rc.hr_exceedance_bpm(rc.label_splits(CAP_SPLITS), 0) is None
    no_hr = [{"split_index": 0, "distance_meters": 1609.34, "duration_seconds": 600}]
    assert rc.hr_exceedance_bpm(rc.label_splits(no_hr), 140.0) is None


@pytest.mark.parametrize(("bpm_over", "expected"), [
    (None, 0.0),                       # no splits -> this axis contributes nothing
    (-5.0, 0.0),                       # under the cap is not a negative breach
    (0.0, 0.0),
    (1.5, 0.0),                        # exactly the noise floor
    (1.2, 0.0),                        # the real 2026-08-02 exceedance
    (2.9, pytest.approx(1.4 / 28)),
    (19.5, pytest.approx(18.0 / 28)),  # the real 2026-07-22 exceedance
])
def test_hr_cap_severity_is_the_raw_excess_past_the_noise_floor(bpm_over, expected):
    """One scaling function for both axes — that shared unit is what makes the
    max() in hr_cap_deviation mean something."""
    assert rc.hr_cap_severity(bpm_over) == expected


#: Every completed day in the live plan that carried a prescribed cap, as
#: (avg_hr, cap, time-above-cap fraction, time-weighted exceedance bpm, Garmin
#: zone-4+5 share). Real measured numbers, frozen here so the calibration can be
#: re-checked without the DB. The last two columns are what the two axes see;
#: the zone share is what neither of them reads.
LIVE_CAPPED_DAYS = [
    (120, 140, 0.000, 0.00, 0.00),   # 2026-07-06
    (152, 140, 0.755, 13.58, 0.43),  # 2026-07-07
    (114, 140, 0.000, 0.00, 0.00),   # 2026-07-08
    (103, 140, 0.000, 0.00, 0.00),   # 2026-07-09
    (96, 140, 0.000, 0.00, 0.00),    # 2026-07-11
    (149, 140, 0.703, 9.32, 0.14),   # 2026-07-12
    (116, 140, 0.000, 0.00, 0.00),   # 2026-07-13
    (142, 140, 0.734, 5.48, 0.37),   # 2026-07-15
    (143, 140, 0.754, 4.55, 0.01),   # 2026-07-16
    (106, 140, 0.012, 0.31, 0.03),   # 2026-07-18
    (136, 140, 0.649, 1.37, 0.00),   # 2026-07-19
    (112, 140, 0.000, 0.00, 0.00),   # 2026-07-20
    (157, 140, 0.748, 19.51, 0.48),  # 2026-07-22
    (94, 140, 0.000, 0.00, 0.00),    # 2026-07-26
    (149, 140, 0.724, 11.91, 0.42),  # 2026-07-26
    (144, 140, 0.592, 6.49, 0.16),   # 2026-07-27
    (126, 140, 0.000, 0.00, 0.00),   # 2026-07-29
    (113, 130, 0.000, 0.00, 0.00),   # 2026-07-30
    (139, 140, 0.579, 1.15, 0.00),   # 2026-08-02
]


def test_the_cap_grade_actually_uses_its_bands():
    """The acceptance measure 0.40.0 never ran, and the one that condemns its
    axis outright.

    A grading axis that emits two letters is not grading. Over the nineteen
    completed capped days in the live plan the time-fraction axis produced
    **only A and F** — 9 F's, 10 A's, nothing between — and seven of the runs it
    failed had an average at or under the cap. That is the same degeneracy
    CLAUDE.md records for the old 0.88 easy-HR ceiling ("a bound that appeared
    in 1 of 13 runs"): a standing penalty wearing a rubric's clothes.

    This asserts the corrected axis spreads over the bands AND never fails a run
    whose average obeyed its ceiling."""
    old, new = Counter(), Counter()
    failed_despite_a_compliant_average = {"old": 0, "new": 0}
    for avg, cap, frac, exc, _z45 in LIVE_CAPPED_DAYS:
        # 0.40.0 verbatim: max(relative average, time fraction past the grace).
        old_s = rc.star_bucket(rc.stars_from_deviation(
            max(max(0.0, (avg - cap) / cap), max(0.0, frac - 0.05)), "hr"))
        new_s = rc.star_bucket(rc.stars_from_deviation(
            rc.hr_cap_deviation(avg, float(cap), exc), "hr"))
        old[old_s] += 1
        new[new_s] += 1
        if avg <= cap:
            failed_despite_a_compliant_average["old"] += old_s == rc.STAR_FLOOR
            failed_despite_a_compliant_average["new"] += new_s == rc.STAR_FLOOR

    # The defect: two occupied buckets, and a run is either perfect or floored.
    assert set(old) == {rc.STAR_MAX, rc.STAR_FLOOR}
    assert old[rc.STAR_FLOOR] == 9

    # The fix: the corrected axis spreads across the scale instead of collapsing
    # to its two extremes.
    assert len(new) >= 4, f"only {len(new)} buckets used: {dict(new)}"
    assert new[rc.STAR_FLOOR] == 3
    assert sum(1 for v in new if rc.STAR_FLOOR < v < rc.STAR_MAX) >= 2

    # ...and no run whose average obeyed its own ceiling is failed any more.
    assert failed_despite_a_compliant_average == {"old": 2, "new": 0}


def test_the_grade_tracks_a_signal_it_does_not_read():
    """Calibration evidence, not a restatement of the formula.

    Garmin's zone-4+5 share is computed on-device from the per-sample trace, so
    it is independent of both avg_hr and the splits. Every session the corrected
    axis fails must be one that signal also calls hard, and vice versa — that
    correspondence is what makes HR_CAP_BPM_SCALE calibration rather than taste.

    The old axis fails this outright: it graded a run with 0% in zones 4-5 an F.
    """
    z45_of_failed, z45_of_passed = [], []
    for avg, cap, _frac, exc, z45 in LIVE_CAPPED_DAYS:
        if rc.stars_from_deviation(
                rc.hr_cap_deviation(avg, float(cap), exc), "hr") == rc.STAR_FLOOR:
            z45_of_failed.append(z45)
        else:
            z45_of_passed.append(z45)
    # Every failed session was >= 42% in zones 4-5; every passed one <= 37%.
    assert min(z45_of_failed) >= 0.42
    assert max(z45_of_passed) <= 0.37
    # The two populations are separated, not merely ordered.
    assert min(z45_of_failed) > max(z45_of_passed)


@pytest.mark.parametrize(("bpm_over", "stars"), [
    (1.5, 5.0),                  # the noise floor: HR_CAP_NOISE_BPM exactly
    (2.9, 4.0),                  # the first knot: 1.5 + 0.05*28
    (11.2, pytest.approx(1.02, abs=0.01)),
    (11.3, 1.0),                 # the floor: 1.5 + 0.35*28
    (11.4, 1.0),                 # ...and it saturates, never extrapolates
])
def test_the_floor_sits_where_the_run_stopped_being_aerobic(bpm_over, stars):
    """Calibration guard, not arithmetic restated. HR_CAP_BPM_SCALE was chosen
    so the bottom of the scale begins at 11.3 bpm sustained over the ceiling,
    because in the live window the sessions at or above that are exactly the
    three whose Garmin zone-4+5 share reached 42% — the runs that were no longer
    aerobic runs. The worst session BELOW the boundary sat at 37%.

    The 0.50.0 star curve was built so this boundary did NOT move: STAR_KNOTS is
    GRADE_BANDS with the letters removed, so d = 0.35 was the old F floor and is
    now exactly STAR_FLOOR. That is why the cutover needed no zone-4+5
    revalidation. A change to either constant that moves this boundary has to
    re-run that comparison; this test is what forces the question."""
    assert rc.stars_from_deviation(rc.hr_cap_severity(bpm_over), "hr") == stars


@pytest.mark.parametrize(("hr", "cap", "exceedance", "expected_d", "stars"), [
    (126.0, 140.0, 0.0, 0.0, 5.0),      # under the cap and never over it
    (126.0, 140.0, 1.2, 0.0, 5.0),      # drifted over, inside the noise floor
    (126.0, 140.0, 8.0, pytest.approx(6.5 / 28), pytest.approx(1.786, abs=0.01)),
    (144.0, 140.0, None, pytest.approx(2.5 / 28), pytest.approx(3.214, abs=0.01)),
    (144.0, 140.0, 6.5, pytest.approx(5.0 / 28), pytest.approx(2.214, abs=0.01)),
    (152.0, 140.0, 1.0, pytest.approx(10.5 / 28), 1.0),
    (126.0, None, 8.0, None, None),     # no cap -> caller falls back
    (None, 140.0, 8.0, None, None),     # no HR reading -> ungradeable
])
def test_hr_cap_deviation_takes_the_worse_of_two_commensurable_axes(
        hr, cap, exceedance, expected_d, stars):
    """0.40.0 took max() over a relative magnitude and a time fraction — two
    different units, so the comparison had no meaning and the fraction won
    almost every time. Both axes are bpm-over-cap now.

    Every case pins the resulting SCORE as well as the float. Asserting the
    arithmetic alone is how the defect survived 0.40.1: the old version of this
    test checked that (126, 140, 0.25) produced 0.20 and never asked what that
    0.20 became, so it passed while the axis emitted only its two extremes. The
    six graded cases here land on six distinct scores, which is the property the
    old letter version could not express."""
    assert rc.hr_cap_deviation(hr, cap, exceedance) == expected_d
    assert rc.stars_from_deviation(
        rc.hr_cap_deviation(hr, cap, exceedance), "hr") == stars


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
    # A 126 average, but the run averaged 4.5 bpm over the cap across its
    # duration — so it is still a breach, and the row says so on that axis.
    assert hr["time_above_cap_pct"] == 45
    assert hr["exceedance_bpm"] == pytest.approx(4.5)
    assert hr["governing_axis"] == "exceedance"
    assert hr["expected_display"] == "≤ +1.5 bpm over cap"
    assert "45% of the run sat above the prescribed 140 bpm cap" in hr["note"]
    # (4.5 - 1.5) / 28 = 0.107, just past the B band's 0.10.
    assert hr["deviation"] == pytest.approx(3.0 / 28, abs=1e-4)
    assert hr["stars"] == pytest.approx(2.929, abs=0.01)


def test_obeying_straddling_and_blowing_the_cap_are_three_different_verdicts():
    """The discrimination the axis exists to provide, asserted on the OVERALL
    letter — the verdict a reader actually acts on, not a deviation float.

    Three runs against one prescription, and the middle one is the case that
    matters. `obeyed` never touches the cap, which is the easy fixture and
    proves nothing on its own. `straddled` is the real shape of the 2026-08-02
    failure: the average obeys, but most of the run sits one to three bpm over —
    0.40.0 graded that identically to `blew_it`, because it counted a split as
    wholly above the cap for a single beat.

    A correct rubric has to separate all three, in this order."""
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
    straddled = card_for(
        {"date": "2026-07-19", "distance_meters": 8047, "duration_seconds": 2971,
         "avg_pace_sec_per_km": 374, "avg_hr": 139, "training_load": 52},
        plan=plan,
        splits=[{"split_index": i, "distance_meters": 1609.34,
                 "duration_seconds": 594, "avg_hr": hr}
                for i, hr in enumerate((134, 141, 135, 143, 142))],
    )
    blew_it = card_for(
        {"date": "2026-07-19", "distance_meters": 8047, "duration_seconds": 3011,
         "avg_pace_sec_per_km": 374, "avg_hr": 144, "training_load": 82},
        plan=plan,
        splits=[{"split_index": i, "distance_meters": 1609.34,
                 "duration_seconds": 602, "avg_hr": hr}
                for i, hr in enumerate((128, 139, 150, 144, 159))],
    )

    # Verdict level first — this is the assertion that would have caught the bug.
    assert obeyed["overall"]["stars"] == rc.STAR_MAX
    assert straddled["overall"]["stars"] == rc.STAR_MAX
    assert blew_it["overall"]["stars"] == pytest.approx(4.179, abs=0.01)
    assert obeyed["overall"]["capped"] is False
    assert straddled["overall"]["capped"] is False
    # The blown run's HR row is far enough below its siblings that the headroom
    # rule bites — the cap doing its job, not the old F-cap's cliff: the row is
    # 2.18, nowhere near the floor, and the overall still reads well above the
    # 3.0 an outright failure would have produced.
    assert blew_it["overall"]["capped"] is True
    assert blew_it["overall"]["capped_by"]["metric"] == "hr"
    assert blew_it["overall"]["stars"] > 4.0

    # Straddling by a beat is over the cap for 60% of the run — the exact
    # quantity 0.40.0 graded — and is still compliant, because 1.2 bpm is not
    # a breach of a 140 ceiling.
    assert straddled["metrics"]["hr"]["time_above_cap_pct"] == 60
    assert straddled["metrics"]["hr"]["exceedance_bpm"] == pytest.approx(1.2, abs=0.05)
    # Inside HR_CAP_NOISE_BPM, so it is a clean maximum — not "nearly".
    assert straddled["metrics"]["hr"]["stars"] == rc.STAR_MAX

    # ...while the genuine breach is 5x further over and is penalised for it,
    # by more than two whole stars. Not the floor that 0.40.0 handed every
    # session with any breach at all, and not the max either.
    assert blew_it["metrics"]["hr"]["exceedance_bpm"] == pytest.approx(6.6, abs=0.05)
    blown_hr = blew_it["metrics"]["hr"]["stars"]
    assert rc.STAR_FLOOR < blown_hr < 3.0
    assert straddled["metrics"]["hr"]["stars"] - blown_hr > 2.0

    assert obeyed["overall"]["stars"] > blew_it["overall"]["stars"]
    # ...and the load numbers run the OTHER way (25 / 52 / 82), which is exactly
    # the inversion that used to decide the letters.
    assert (obeyed["stimulus"]["load"] < straddled["stimulus"]["load"]
            < blew_it["stimulus"]["load"])


@pytest.mark.parametrize(("hr", "cap", "exceedance", "expected_axis"), [
    (139.0, 140.0, 6.0, "exceedance"),   # average compliant, the middle blew it
    (144.0, 140.0, None, "average"),     # no split data, the mean ran hot
    (144.0, 140.0, 8.0, "exceedance"),   # both breached, the exceedance is worse
    (152.0, 140.0, 3.0, "average"),      # both breached, the average is worse
    (126.0, 140.0, 1.2, None),           # over the cap, but inside the noise floor
    (126.0, 140.0, 0.0, None),           # clean on both axes
    (126.0, None, 8.0, None),            # no cap to breach
    (None, 140.0, 8.0, None),            # no HR reading at all
])
def test_hr_cap_axis_names_which_breach_produced_the_grade(
        hr, cap, exceedance, expected_axis):
    """The attribution the deviation itself discards. A caller that cannot ask
    which axis won cannot state the number the grade was measured against."""
    assert rc.hr_cap_axis(hr, cap, exceedance) == expected_axis


def test_hr_cap_axis_agrees_with_the_deviation_it_explains():
    """The two must never disagree: a named axis implies a non-zero deviation,
    and a zero deviation implies no axis. They are computed from one helper so
    that stays true, and this is the assertion that would catch them drifting."""
    for hr, exceedance in ((139.0, 6.0), (144.0, None), (126.0, 1.2), (126.0, 0.0),
                           (152.0, 3.0), (168.0, 30.0)):
        d = rc.hr_cap_deviation(hr, 140.0, exceedance)
        axis = rc.hr_cap_axis(hr, 140.0, exceedance)
        assert (axis is None) == (d == 0.0)


def test_the_real_2026_08_02_run_is_not_a_failure():
    """THE regression, on unmodified live numbers (activity 23825963527).

    Prescription: easy 5 mi, keep HR under 140. Executed at 139 average with a
    148 peak; Garmin recorded 0 seconds in zones 4-5. Three of its five miles
    averaged 141, 143 and 142 — one to three bpm over the cap, 0.7% to 2.1%.

    0.40.0 counted each of those miles as 100% above the cap, reached 58% of
    the session "in breach", subtracted the 5% grace and fed 0.53 into
    GRADE_BANDS — a table calibrated for relative magnitudes, where 0.53 means
    catastrophic. The card graded HR an **F**, the F-cap pulled a 3.60 GPA
    down to an overall **C**, and distance, pace and continuity were all A+.

    The run obeyed its prescription. It grades A."""
    card = card_for(
        {"date": "2026-08-02", "distance_meters": 8062.25, "duration_seconds": 2923,
         "avg_pace_sec_per_km": 362.58, "avg_hr": 139, "max_hr": 148,
         "training_load": 52.2},
        plan={"type": "easy", "target_distance_m": 8046.72,
              "target_pace_sec_per_km": 359.77, "target_hr_max": 140.0, "seq": 1},
        splits=LIVE_2026_08_02_SPLITS,
    )
    hr = card["metrics"]["hr"]
    assert hr["stars"] == rc.STAR_MAX
    assert hr["deviation"] == 0.0
    assert hr["governing_axis"] is None
    assert hr["exceedance_bpm"] == pytest.approx(1.2, abs=0.05)
    assert card["overall"]["stars"] == rc.STAR_MAX
    assert card["overall"].get("capped_by") is None

    # The row keeps the bpm display, because the average IS what was graded.
    assert rc.actual_text("hr", hr) == "139 bpm"
    assert rc.expected_text("hr", hr) == "≤ 140 bpm"
    assert rc._delta_text("hr", hr) == "in range"

    # ...and the note reconciles the A+ with the 58% that IS still true, rather
    # than stating the fraction alone beside a passing grade. A grade must never
    # contradict the prose beside it, in either direction.
    assert hr["time_above_cap_pct"] == 58
    assert hr["note"] == ("58% of the run sat above the prescribed 140 bpm cap, "
                          "by 1.2 bpm on average — inside sensor noise")


def test_the_real_2026_07_22_run_still_fails_hard():
    """The other half of the fix, on unmodified live numbers (23695862040).

    Same 140 bpm cap, same easy prescription. Splits reaching 185, 48% of the
    session in Garmin zones 4-5, 19.5 bpm over the ceiling across its duration.
    A correction that merely deleted the time axis would grade this on the
    average alone — 157 vs 140, which lands a full two letters higher. It must
    stay an F, and it must still cap the overall."""
    card = card_for(
        {"date": "2026-07-22", "distance_meters": 6445.14, "duration_seconds": 2392,
         "avg_pace_sec_per_km": 371.2, "avg_hr": 157, "max_hr": 197,
         "training_load": 249.5},
        plan={"type": "easy", "target_distance_m": 6437.0,
              "target_pace_sec_per_km": 360.0, "target_hr_max": 140.0, "seq": 1},
        splits=LIVE_2026_07_22_SPLITS,
    )
    hr = card["metrics"]["hr"]
    assert hr["exceedance_bpm"] == pytest.approx(19.5, abs=0.05)
    assert hr["stars"] == rc.STAR_FLOOR
    assert hr["governing_axis"] == "exceedance"
    assert card["overall"]["capped"] is True
    assert card["overall"]["capped_by"]["metric"] == "hr"

    # The average-only grade this would get if the split axis were dropped —
    # pinned so the two-letter gap is visible, not asserted in prose.
    assert rc.stars_from_deviation(
        rc.hr_cap_severity(157 - 140), "hr") == rc.STAR_FLOOR
    # ...whereas dividing by the cap would have put it mid-scale.
    assert rc.stars_from_deviation((157 - 140) / 140, "hr") > 2.0


def test_the_row_moves_to_the_exceedance_axis_when_that_is_what_graded():
    """The 0.40.1 display contract, carried forward onto the new axis.

    The live 2026-08-02 card printed

        | Avg HR | 139 bpm | ≤ 140 bpm | -1% | F |

    where every number describes the average — which scored 0.0. Whenever the
    split-derived exceedance is what produced the letter, actual, expected and
    delta must all move to it, and the three cells must reconcile by arithmetic:
    actual - expected = delta."""
    card = card_for(
        {"date": "2026-07-22", "distance_meters": 6445.14, "duration_seconds": 2392,
         "avg_pace_sec_per_km": 371.2, "avg_hr": 157, "training_load": 249.5},
        plan={"type": "easy", "target_distance_m": 6437.0,
              "target_pace_sec_per_km": 360.0, "target_hr_max": 140.0, "seq": 1},
        splits=LIVE_2026_07_22_SPLITS,
    )
    hr = card["metrics"]["hr"]
    assert hr["governing_axis"] == "exceedance"
    assert rc.actual_text("hr", hr) == "+19.5 bpm over cap (75% of run)"
    assert rc.expected_text("hr", hr) == "≤ +1.5 bpm over cap"
    assert rc._delta_text("hr", hr) == "+18.0 bpm"
    # 19.5 - 1.5 = 18.0. The cells add up.
    assert 19.5 - 1.5 == pytest.approx(18.0)
    # The numeric fields are untouched — storage and the coach read speak bpm.
    assert hr["actual"] == 157
    assert hr["expected"] == 140.0
    assert hr["cap"] == 140.0


def test_an_average_breach_keeps_the_row_in_bpm():
    """The other half of the two-sided assertion. When the AVERAGE is what blew
    the cap, the average IS the graded quantity — the row must stay in bpm and
    must not acquire a percentage-of-run display it did not earn.

    Splits carrying no HR is what makes the average the only live axis: it is
    the documented degrade path (see
    test_hr_exceedance_is_none_without_splits_or_without_a_cap)."""
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
    assert "exceedance_bpm" not in hr
    assert hr["governing_axis"] == "average"
    assert "actual_display" not in hr
    assert hr["deviation"] == pytest.approx(6.5 / 28, abs=1e-4)  # 8 bpm over, less noise
    assert hr["stars"] == pytest.approx(1.786, abs=0.01)
    assert rc.actual_text("hr", hr) == "148 bpm"
    assert rc.expected_text("hr", hr) == "≤ 140 bpm"
    # bpm, not a percentage — which is what this test's own name asks for and
    # what it did NOT assert before 0.41.0. 148 against a 140 cap is 8 beats;
    # "+6%" made the reader convert it themselves against an Expected column
    # already written in bpm.
    assert rc._delta_text("hr", hr) == "8 bpm over"


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
    assert hr["stars"] == rc.STAR_MAX
    assert hr["exceedance_bpm"] == 0.0
    assert hr["time_above_cap_pct"] == 0
    assert "note" not in hr           # nothing happened; say nothing
    assert hr["governing_axis"] is None
    assert "actual_display" not in hr
    assert rc.actual_text("hr", hr) == "126 bpm"
    assert rc.expected_text("hr", hr) == "≤ 140 bpm"
    assert rc._delta_text("hr", hr) == "in range"


def test_the_rendered_card_never_prints_a_passing_delta_beside_an_f():
    """End-to-end through the markdown the user actually reads — the renderer
    and the PDF share these three helpers, so this covers both surfaces."""
    card = card_for(
        {"date": "2026-07-22", "distance_meters": 6445.14, "duration_seconds": 2392,
         "avg_pace_sec_per_km": 371.2, "avg_hr": 157, "training_load": 249.5},
        plan={"type": "easy", "target_distance_m": 6437.0,
              "target_pace_sec_per_km": 360.0, "target_hr_max": 140.0, "seq": 1},
        splits=LIVE_2026_07_22_SPLITS,
    )
    md = rc.render_markdown(card)
    hr_row = next(ln for ln in md.splitlines() if ln.startswith("| Avg HR"))
    assert hr_row == (
        "| Avg HR | +19.5 bpm over cap (75% of run) | ≤ +1.5 bpm over cap "
        "| +18.0 bpm | ★☆☆☆☆ 1.00 |")
    assert "| 157 bpm | ≤ 140 bpm |" not in md


def test_the_rendered_card_shows_a_compliant_run_in_bpm():
    """The converse, on the live 2026-08-02 numbers: a run that obeyed its cap
    renders as bpm against bpm with no breach language anywhere in the row."""
    card = card_for(
        {"date": "2026-08-02", "distance_meters": 8062.25, "duration_seconds": 2923,
         "avg_pace_sec_per_km": 362.58, "avg_hr": 139, "training_load": 52.2},
        plan={"type": "easy", "target_distance_m": 8046.72,
              "target_pace_sec_per_km": 359.77, "target_hr_max": 140.0, "seq": 1},
        splits=LIVE_2026_08_02_SPLITS,
    )
    md = rc.render_markdown(card)
    hr_row = next(ln for ln in md.splitlines() if ln.startswith("| Avg HR"))
    assert hr_row == "| Avg HR | 139 bpm | ≤ 140 bpm | in range | ★★★★★ 5.00 |"


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
    assert "exceedance_bpm" not in hr


def test_a_cap_grades_on_the_average_when_no_splits_exist():
    """The splits read DEGRADES rather than abstaining — a backfilled activity
    with no splits still gets its cap graded, on the average alone.

    28 bpm over a prescribed ceiling for a whole run is an F. Under 0.40.0 the
    average axis divided by the cap, which put the same run at 0.20 — a **C** —
    for the same reason the exceedance is not divided by the cap: HR's non-zero
    offset compresses every real breach into the passing bands."""
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 8000, "duration_seconds": 2400,
         "avg_pace_sec_per_km": 360, "avg_hr": 168, "training_load": 60},
        plan={"type": "easy", "target_distance_m": 8000,
              "target_hr_max": 140.0, "seq": 1},
    )
    hr = card["metrics"]["hr"]
    assert hr["reference"] == "plan"
    assert "time_above_cap_pct" not in hr      # nothing to measure
    assert hr["deviation"] == pytest.approx(26.5 / 28, abs=1e-4)
    assert hr["stars"] == rc.STAR_FLOOR
    # ...whereas the old cap-relative axis put the same run mid-scale.
    assert rc.stars_from_deviation(28 / 140, "hr") > 1.9


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
    all_max = {k: {"stars": 5.0} for k in rc.METRIC_WEIGHTS}
    assert rc.overall_stars(all_max)["stars"] == 5.0
    all_max["pace"] = {"stars": None}
    out = rc.overall_stars(all_max)
    assert out["stars"] == 5.0
    # Four compliance metrics since 0.40.0, so one n/a leaves three.
    assert out["graded_metrics"] == 3


def test_overall_with_no_gradeable_metrics_is_none_not_the_floor():
    """The floor would read as a judgment we did not make — "failed" and "not
    measured" must never be the same number."""
    out = rc.overall_stars({k: {"stars": None} for k in rc.METRIC_WEIGHTS})
    assert out["stars"] is None
    assert out["mean_stars"] is None
    assert out["graded_metrics"] == 0
    assert out["capped"] is False


def test_sub_band_position_now_moves_the_overall():
    """The deliberate reversal of `base_letter`'s old contract (0.50.0).

    `base_letter` existed so "a modifier can never move an overall grade",
    which rounded every metric UP to its band top before averaging — the reason
    a third of live cards read exactly 4.00 GPA. Two cards that differ only in
    sub-band position must now differ in the overall, or the change bought no
    resolution at all.
    """
    high = {k: {"stars": 3.9} for k in rc.METRIC_WEIGHTS}
    low = {k: {"stars": 3.1} for k in rc.METRIC_WEIGHTS}
    assert rc.overall_stars(high)["stars"] > rc.overall_stars(low)["stars"]
    assert rc.overall_stars(high)["stars"] == pytest.approx(3.9)


def test_overall_is_the_intent_weighted_mean_not_a_bucket():
    """No cuts table any more: the mean IS the score."""
    out = rc.overall_stars(
        {"distance": {"stars": 5.0}, "pace": {"stars": 3.0}}, "steady")
    # distance .30, pace .30 — equal weights, so the mean is 4.0.
    assert out["stars"] == pytest.approx(4.0)
    assert out["mean_stars"] == pytest.approx(4.0)
    assert out["graded_metrics"] == 2


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
    assert cont["stars"] is None
    assert cont["note"] == "no splits recorded — continuity can't be measured"
    assert without["overall"]["graded_metrics"] == 3   # the other three

    with_splits = card_for(activity, splits=PACED_SPLITS)
    assert with_splits["metrics"]["continuity"]["stars"] == rc.STAR_MAX
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
    assert cont["stars"] is None
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
    # 0.125 sits between the 0.10 and 0.20 knots -> between 3 and 2 stars.
    assert cont["stars"] == pytest.approx(2.75, abs=0.01)
    # The note names the offending split so the reader can act on it.
    assert cont["note"] == ("mile 4 ran 12:31/mi — 27% slower than your median "
                            "mile for this run")
    # And it is genuinely independent: the metrics that missed it still pass.
    assert card["metrics"]["distance"]["stars"] == rc.STAR_MAX
    assert card["metrics"]["hr"]["stars"] == rc.STAR_MAX


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
    assert cont["stars"] == rc.STAR_MAX
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
    assert cont["stars"] is None
    assert cont["note"] == ("only 2 full splits with pace — need 3 to compare a "
                            "slowest against a median")


# --- insufficient reference ------------------------------------------------

def test_insufficient_reference_grades_nothing():
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 10000, "avg_pace_sec_per_km": 300,
         "avg_hr": 150, "training_load": 100},
        reference={"mode": "insufficient_data", "n": 3},
    )
    assert all(m["stars"] is None for m in card["metrics"].values())
    assert card["overall"]["stars"] is None


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
    # splits (Mile/Avg HR/vs run — the Distance column was dropped as
    # duplicative, the row label already IS the distance, and 0.41.0 drops any
    # column with no data in ANY row. This fixture's splits carry no pace and
    # no elevation, so those two columns are correctly absent rather than
    # rendering as a full column of em-dashes).
    assert [b[0].count("|") for b in blocks] == [6, 3, 4]
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
    assert card["metrics"]["distance"]["stars"] == rc.STAR_MAX     # graded off the plan
    assert card["metrics"]["hr"]["stars"] is None           # no rolling median
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
    assert card["overall"]["stars"] == rc.STAR_MAX
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
    # Stated in bpm since 0.41.0 — 160 against a 141.62 ceiling is 18 beats.
    assert rc._delta_text("hr", hr) == "18 bpm over"


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
    assert hr["stars"] == rc.STAR_MAX
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
    assert rc.stars_from_deviation(d, "pace") < 4.25


def test_expected_text_falls_back_to_the_numeric_formatter():
    """A metric with no `expected_display` formats its raw number.

    Rewritten in 0.41.0: this used to assert the fallback via a card's DISTANCE
    metric on the claim that "only HR carries a display string". That stopped
    being true — a rolling-reference distance is one-sided and now prints
    "≥ 6.21 mi" — so the test is pointed at the fallback itself with a
    hand-built dict, which is the behavior it was always about.
    """
    assert rc.expected_text("distance", {"expected": 10000.0}) == "6.21 mi"
    assert rc.expected_text("pace", {"expected": 300.0}) == "8:03/mi"
    assert rc.expected_text("load", {"expected": None}) == "—"
    # A display string, when present, always wins over the formatter.
    assert rc.expected_text(
        "distance", {"expected": 10000.0, "expected_display": "≥ 6.21 mi"}
    ) == "≥ 6.21 mi"


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
    assert pace["stars"] == pytest.approx(2.444, abs=0.01)


def test_running_the_prescribed_pace_still_earns_an_A():
    # The tightening must not make a well-executed plan day unachievable:
    # within 3% of target is still an A.
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 4800, "duration_seconds": 1800,
         "avg_pace_sec_per_km": 380, "avg_hr": 136, "training_load": 51},
        plan={"type": "easy", "target_distance_m": 4800,
              "target_pace_sec_per_km": 388, "seq": 1},
    )
    assert card["metrics"]["pace"]["stars"] == pytest.approx(4.379, abs=0.01)
    assert card["metrics"]["distance"]["stars"] == rc.STAR_MAX


def test_the_overall_cannot_read_well_when_the_prescription_was_missed():
    """The self-consistency check: a card whose coaching read says the easy day
    was run at tempo must not print an overall A."""
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 4925, "duration_seconds": 1736,
         "avg_pace_sec_per_km": 352, "avg_hr": 136, "training_load": 51},
        plan={"type": "easy", "target_distance_m": 4828,
              "target_pace_sec_per_km": 390, "seq": 1},
        reference={**REF, "median_hr": 146.0, "median_load": 53.0},
    )
    assert card["overall"]["stars"] < 4.25


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
    assert rc.stars_from_deviation(d, "pace") < 4.25


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
    assert pace["stars"] < 4.25


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
    assert card["metrics"]["pace"]["stars"] == rc.STAR_MAX


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
    assert rc.stars_from_deviation(d, "pace") < 4.25


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
    assert card["metrics"]["pace"]["stars"] == rc.STAR_FLOOR


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
    assert pace["stars"] == rc.STAR_MAX
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
    assert pace["stars"] == rc.STAR_FLOOR


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
    assert pace["stars"] is None
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
    assert pace["stars"] == rc.STAR_MAX
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
    assert pace["stars"] is None
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
        "distance": {"stars": 5.0}, "pace": {"stars": 2.0},
        "hr": {"stars": 5.0}, "load": {"stars": 5.0},
    }
    easy = rc.overall_stars(metrics, "easy")
    long_run = rc.overall_stars(metrics, "long")
    # No continuity key in the fixture, so its 0.15 redistributes over the rest.
    # easy: (.19*5 + .42*2 + .24*5) / .85 = 3.52 — pace dominates.
    assert easy["mean_stars"] == pytest.approx(3.518, abs=0.01)
    # long: (.45*5 + .20*2 + .20*5) / .85 = 4.29 — distance is the point there.
    assert long_run["mean_stars"] == pytest.approx(4.29, abs=0.01)
    # The same four numbers, 0.78 stars apart on the rows purely because of
    # what the day was for. Under the letter rubric both read "B" and the
    # difference was invisible. (The long run's OVERALL is then held to 4.00 by
    # the headroom rule, since its pace row sits at 2.0 — so the gap the reader
    # sees is smaller than the gap the weights produced, and both are real.)
    assert long_run["mean_stars"] - easy["mean_stars"] > 0.75
    assert long_run["stars"] > easy["stars"]


def test_a_load_score_cannot_reach_the_overall_even_if_one_appears():
    """The 0.40.0 guarantee, tested at the arithmetic rather than at build_card:
    load is absent from every weight table, so even a hand-injected load score —
    a maximum or a floor — moves neither the mean nor the cap.

    The floor case is the sharp one: the headroom cap reads `min()` over the
    weighted metrics, so a load row at 1.0 would drag the overall to 3.0 if it
    were ever iterated."""
    base = {"distance": {"stars": 5.0}, "pace": {"stars": 5.0}, "hr": {"stars": 5.0}}
    clean = rc.overall_stars(base, "easy")
    for injected in (5.0, 2.6, rc.STAR_FLOOR):
        out = rc.overall_stars({**base, "load": {"stars": injected}}, "easy")
        assert out["mean_stars"] == clean["mean_stars"] == pytest.approx(5.0)
        assert out["stars"] == pytest.approx(5.0)
        assert out["capped"] is False
    assert "load" not in rc.METRIC_WEIGHTS
    assert all("load" not in w for w in rc.INTENT_METRIC_WEIGHTS.values())


def test_steady_intent_keeps_the_neutral_split():
    """No stated intent means no metric can claim to be the point of the day."""
    metrics = {
        "distance": {"stars": 5.0}, "pace": {"stars": 2.0},
        "hr": {"stars": 5.0}, "load": {"stars": 5.0},
    }
    # (.30*5 + .30*2 + .25*5) / .85 = 3.94 (continuity absent, redistributed)
    assert rc.overall_stars(metrics, "steady")["mean_stars"] == pytest.approx(3.941, abs=0.01)
    assert rc.overall_stars(metrics)["mean_stars"] == pytest.approx(3.941, abs=0.01)  # default


def test_a_floored_row_caps_the_overall():
    """Two strong metrics must not average an outright failure into a good
    overall. The continuous analogue of the old F-cap, and it reproduces that
    rule exactly at the old boundary: STAR_FLOOR + OVERALL_STAR_HEADROOM = 3.0,
    which is the C the letter version pinned to."""
    metrics = {
        "distance": {"stars": 5.0}, "pace": {"stars": rc.STAR_FLOOR},
        "hr": {"stars": 5.0}, "load": {"stars": 5.0},
    }
    out = rc.overall_stars(metrics, "long")
    # (.45*5 + .20*1 + .20*5) / .85 = 4.06 on the rows alone...
    assert out["mean_stars"] == pytest.approx(4.06, abs=0.01)
    # ...held to worst + 2.0.
    assert out["stars"] == pytest.approx(3.0)
    assert out["capped"] is True
    assert out["capped_by"] == {"metric": "pace", "stars": 1.0}


def test_the_cap_bites_before_the_floor_too():
    """Unlike the old F-cap, the headroom rule also catches a card whose worst
    row is merely bad rather than floored — 16 such cards in the live corpus,
    every one of which the letter cap ignored entirely."""
    out = rc.overall_stars(
        {"distance": {"stars": 5.0}, "pace": {"stars": 2.0}, "hr": {"stars": 5.0}},
        "long")
    assert out["mean_stars"] > 4.0
    assert out["stars"] == pytest.approx(4.0)      # 2.0 + 2.0
    assert out["capped"] is True


def test_the_cap_never_raises_a_worse_score():
    """`min()`, so a card already below the ceiling is untouched."""
    metrics = {"pace": {"stars": rc.STAR_FLOOR}, "hr": {"stars": rc.STAR_FLOOR}}
    out = rc.overall_stars(metrics, "easy")
    assert out["stars"] == rc.STAR_FLOOR
    assert out["capped"] is False


def test_the_f_cap_is_stated_on_the_card():
    """A long run that nailed distance, HR and load but was run far too slow
    for its prescription: without the cap the composite reads B over an F."""
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 20000, "duration_seconds": 9000,
         "avg_pace_sec_per_km": 200.0, "avg_hr": 150, "training_load": 140},
        plan={"type": "long", "target_distance_m": 20000,
              "target_pace_sec_per_km": 300.0, "seq": 1},
    )
    assert card["metrics"]["pace"]["stars"] == rc.STAR_FLOOR
    assert card["overall"]["mean_stars"] > 3.5   # would have read comfortably fine
    assert card["overall"]["stars"] == pytest.approx(3.0)
    assert card["overall"]["capped_by"]["metric"] == "pace"
    # The note states BOTH numbers so it reconciles by arithmetic.
    md = rc.render_markdown(card)
    assert "held to 3.00" in md
    assert f"average {card['overall']['mean_stars']:.2f}" in md


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
    by meters): within rounding of zero IS on target — say so.

    Since 0.41.0 there is no percentage on this row at all: a real gap reads in
    miles. The negative-zero case is still the point of the test.
    """
    just_under = {"actual": 6432.0, "expected": 6437.376, "stars": 5.0}
    assert rc._delta_text("distance", just_under) == "on target"
    just_over = {"actual": 6440.0, "expected": 6437.376, "stars": 5.0}
    assert rc._delta_text("distance", just_over) == "on target"
    real_gap = {"actual": 7000.0, "expected": 6437.376, "stars": 3.6}
    assert rc._delta_text("distance", real_gap) == "0.35 mi long"
    short = {"actual": 5000.0, "expected": 6437.376, "stars": 1.6}
    assert rc._delta_text("distance", short) == "0.89 mi short"


# --- 0.41.0: the card states the bound it graded against -------------------
# The display half of the A+ problem. Direction gating means a run on the free
# side of a one-sided expectation scores an exact 0.0 deviation, which is
# mechanically an A+ — measured over the 15 stored cards at the time, 9 of 15
# pace deviations were exactly 0.0 and A+ was 29 of the 32 A-band grades. The
# grades are right; a bare "9:39/mi" in the Expected column beside a stated
# "5s/mi slower" and an A+ is what made them read as a participation trophy.

def test_pace_bound_kind_matches_pace_deviation_gating():
    """`pace_bound_kind` is a claim ABOUT `pace_deviation`, so derive the truth
    from `pace_deviation` itself rather than restating the branches.

    A "floor" means running slower than the expectation is free; a "ceiling"
    means running faster is free; a "point" means neither is. If the two ever
    disagree the card prints a bound the grade did not use, which is the exact
    class of bug this whole change is fixing.
    """
    target = 360.0  # 6:00/km
    for cls in ("easy", "long", "quality", "steady"):
        slower = rc.pace_deviation(target * 1.10, target, cls)
        faster = rc.pace_deviation(target * 0.90, target, cls)
        kind = rc.pace_bound_kind(cls)
        if kind == "floor":
            assert slower == 0.0, cls    # slow side free
            assert faster > 0.0, cls
        elif kind == "ceiling":
            assert faster == 0.0, cls    # fast side free
            assert slower > 0.0, cls
        else:
            assert slower > 0.0 and faster > 0.0, cls


def test_easy_pace_expected_prints_as_a_floor_not_a_point_target():
    """The headline case. A prescribed easy day run 5s/mi SLOWER is compliance,
    and the row has to say so — before this it printed a bare target beside a
    stated miss and an A+."""
    card = card_for(
        {"date": "2026-08-02", "distance_meters": 8047, "duration_seconds": 2923,
         "avg_pace_sec_per_km": 363, "avg_hr": 130, "training_load": 52},
        plan={"type": "easy", "target_distance_m": 8047,
              "target_pace_sec_per_km": 360, "seq": 1},
    )
    pace = card["metrics"]["pace"]
    assert card["intent_class"] == "easy"
    assert pace["deviation"] == 0.0                 # slower is free
    assert pace["stars"] >= 4.25
    assert pace["bound"] == "floor"
    assert rc.expected_text("pace", pace).startswith("≥ ")
    assert rc._delta_text("pace", pace).endswith("slower")


def test_quality_pace_expected_prints_as_a_ceiling():
    """The mirror: a rep target is a speed the run must MEET, so beating it is
    free and the bound points the other way."""
    assert rc.pace_bound_kind("quality") == "ceiling"
    assert rc.bounded_display("ceiling", "6:58/mi") == "≤ 6:58/mi"


def test_two_sided_expectations_keep_a_bare_number():
    """A plan DISTANCE is a genuine point target — over-running a prescription
    costs you — so it must NOT grow a bound prefix. Only the one-sided
    expectations do."""
    card = card_for(
        {"date": "2026-08-02", "distance_meters": 8047, "duration_seconds": 2923,
         "avg_pace_sec_per_km": 363, "avg_hr": 130, "training_load": 52},
        plan={"type": "easy", "target_distance_m": 8047,
              "target_pace_sec_per_km": 360, "seq": 1},
    )
    distance = card["metrics"]["distance"]
    assert distance["reference"] == "plan"
    assert "bound" not in distance
    assert rc.expected_text("distance", distance) == "5.00 mi"
    assert rc.pace_bound_kind("steady") == "point"
    assert rc.bounded_display("point", "5.00 mi") == "5.00 mi"


def test_rolling_distance_expected_prints_as_a_floor():
    """`distance_deviation(two_sided=False)` against the rolling median: going
    LONGER than your norm is never a penalty, so the expectation is a floor."""
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 10000, "duration_seconds": 3000,
         "avg_pace_sec_per_km": 300, "avg_hr": 150, "training_load": 100})
    distance = card["metrics"]["distance"]
    assert distance["reference"] == "rolling_60d"
    assert distance["bound"] == "floor"
    assert rc.expected_text("distance", distance).startswith("≥ ")


# --- 0.41.0: one Delta grammar, never a percentage -------------------------

def test_every_delta_is_in_the_rows_own_unit():
    """A live card printed four dialects in four rows — `on target` /
    `5s/mi slower` / `53% over` / `even` — where the percentages meant a
    percentage of a distance, of a ratio, and of a percentage. Three different
    quantities wearing one symbol.

    The rule: a delta is stated in the unit its own row is measured in.
    """
    assert rc._delta_text(
        "distance", {"actual": 7000.0, "expected": 6437.376, "stars": 3.6}
    ) == "0.35 mi long"
    assert rc._delta_text(
        "pace", {"actual": 375.0, "expected": 360.0, "stars": 3.6}
    ) == "24s/mi slower"
    assert rc._delta_text(
        "hr", {"actual": 148.0, "expected": 140.0, "stars": 2.6}
    ) == "8 bpm over"
    assert rc._delta_text(
        "continuity", {"actual": 1.30, "expected": 1.15, "stars": 1.6}
    ) == "0.15x over"
    # None of them may carry a percent sign.
    for key, metric in (
        ("distance", {"actual": 7000.0, "expected": 6437.376, "stars": 3.6}),
        ("pace", {"actual": 375.0, "expected": 360.0, "stars": 3.6}),
        ("hr", {"actual": 148.0, "expected": 140.0, "stars": 2.6}),
        ("continuity", {"actual": 1.30, "expected": 1.15, "stars": 1.6}),
    ):
        assert "%" not in rc._delta_text(key, metric), key


def test_continuity_delta_states_the_gap_in_median_splits_both_ways():
    """`even` lost the magnitude on the compliant side, and `N% over` was a
    percentage of a ratio on the other. Both are now a gap in the unit
    `actual` and `expected` are already in."""
    compliant = {"actual": 1.07, "expected": 1.15, "stars": 5.0}
    assert rc._delta_text("continuity", compliant) == "0.08x under"
    assert rc._delta_text(
        "continuity", {"actual": 1.15, "expected": 1.15, "stars": 4.6}
    ) == "on target"


def test_continuity_expected_uses_the_typographic_bound():
    """The card printed `<= 1.15x` beside `≤ 140 bpm` in the same column."""
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 10000, "duration_seconds": 3000,
         "avg_pace_sec_per_km": 300, "avg_hr": 150, "training_load": 100},
        splits=MILE_SPLITS)
    continuity = card["metrics"]["continuity"]
    if continuity.get("expected_display"):
        assert continuity["expected_display"].startswith("≤ ")
        assert "<=" not in continuity["expected_display"]


# --- 0.41.0: dead split columns are dropped --------------------------------

def test_split_table_drops_columns_with_no_data_and_keeps_the_rest():
    """13 of 15 stored cards had an entirely empty Elev column and 85% of
    `activity_splits` rows carry no elevation — a treadmill run never has any.
    A full column of em-dashes is ~15% of the table's width spent on nothing,
    on a page whose density ladder was already bottoming out.

    `vs run` is deliberately KEPT when it has data: it is what makes a hot mile
    visible at a glance.
    """
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 10000, "duration_seconds": 3000,
         "avg_pace_sec_per_km": 300, "avg_hr": 150, "training_load": 100},
        splits=[{"split_index": i, "distance_meters": 1609.34,
                 "duration_seconds": 600 + i, "avg_hr": 140 + i,
                 "avg_pace_sec_per_km": 373.0} for i in range(4)])
    headers, rows = rc.split_table(card)
    assert "Elev" not in headers            # no elevation anywhere
    assert "Avg HR" in headers
    assert "vs run" in headers              # has data — kept
    assert "Pace" in headers
    assert all(len(r) == len(headers) for r in rows)
    assert not any("—" == c for r in rows for c in r)

    # ...and a column WITH data survives.
    with_elev = card_for(
        {"date": "2026-07-19", "distance_meters": 10000, "duration_seconds": 3000,
         "avg_pace_sec_per_km": 300, "avg_hr": 150, "training_load": 100},
        splits=[{"split_index": i, "distance_meters": 1609.34,
                 "duration_seconds": 600 + i, "avg_hr": 140 + i,
                 "avg_pace_sec_per_km": 373.0,
                 "elevation_gain_meters": 12.0} for i in range(4)])
    assert "Elev" in rc.split_table(with_elev)[0]


def test_split_table_drops_hr_columns_together_when_the_watch_recorded_none():
    """No per-split HR means `Avg HR` and `vs run` are both dead. Dropping one
    and keeping the other would leave a column of dashes headed by a
    comparison against a number that isn't shown."""
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 10000, "duration_seconds": 3000,
         "avg_pace_sec_per_km": 300, "training_load": 100},
        splits=[{"split_index": i, "distance_meters": 1609.34,
                 "duration_seconds": 600, "avg_pace_sec_per_km": 373.0}
                for i in range(4)])
    headers, _rows = rc.split_table(card)
    assert "Avg HR" not in headers
    assert "vs run" not in headers
    assert headers[0] == "Mile"


def test_split_table_is_the_single_source_for_both_renderers():
    """The markdown card and the PDF had this table written out twice, and a
    column dropped in one and kept in the other is precisely the divergence
    `render_report_card_pdf`'s already-built-card contract exists to prevent —
    the same reason `stimulus_rows` is shared."""
    from local_fitness.agent import visuals

    card = card_for(
        {"date": "2026-07-19", "distance_meters": 10000, "duration_seconds": 3000,
         "avg_pace_sec_per_km": 300, "avg_hr": 150, "training_load": 100},
        splits=MILE_SPLITS)
    headers, rows = rc.split_table(card)
    md = rc.render_markdown(card)
    html_doc = visuals._render_splits_html(card, None)
    for h in headers:
        assert f"| {h} " in md or f"|{h}|" in md.replace(" ", "")
        assert f"<th>{h}</th>" in html_doc
    assert "<th>Elev</th>" not in html_doc
    assert html_doc.count("<th>") == len(headers)
    for cell in rows[0]:
        assert f"<td>{cell}</td>" in html_doc


# --- metric_table: one definition, two renderers ----------------------------
# The cells were already single-sourced (both renderers call actual_text /
# expected_text / _delta_text), but the HEADER ROW and the column set were
# written out twice — the same divergence split_table and stimulus_rows exist
# to prevent, one level up. Extracted in 0.48.0.


def _graded_card_for_table():
    return {
        "metrics": {
            "distance": {"actual": 8046.72, "expected": 8046.72, "stars": 4.6,
                         "deviation": 0.0, "unit": "m", "reference": "plan"},
            "pace": {"actual": 360.0, "expected": 355.0, "stars": 4.0,
                     "deviation": 0.014, "unit": "sec_per_km", "reference": "plan",
                     "note": "a shade slow"},
            "hr": {"actual": 139.0, "expected": 140.0, "stars": 5.0,
                   "deviation": 0.0, "unit": "bpm", "reference": "plan"},
            "continuity": {"actual": 1.05, "expected": 1.15, "stars": 4.6,
                           "deviation": 0.0, "unit": "ratio", "reference": "self"},
        },
    }


def test_metric_table_headers_and_row_order_are_one_definition():
    headers, rows = rc.metric_table(_graded_card_for_table())
    assert headers == ["Metric", "Actual", "Expected", "Delta", "Rating"]
    assert [r[0] for r in rows] == [label for _k, label in rc._METRIC_LABELS]
    assert all(len(r) == len(headers) for r in rows)


def test_metric_table_puts_the_rating_last():
    """The PDF styles the final cell by score, so the rating's POSITION is part
    of the contract, not an accident of ordering."""
    _headers, rows = rc.metric_table(_graded_card_for_table())
    assert [r[-1] for r in rows] == [
        "★★★★½ 4.60", "★★★★☆ 4.00", "★★★★★ 5.00", "★★★★½ 4.60"]


def test_a_stored_letter_card_still_renders_its_letters():
    """A card stored under the pre-0.50.0 rubric has grades and no scores.
    Storage is a dated snapshot with no backfill path, so the table must show
    what that render actually said rather than turning it all into n/a."""
    card = _graded_card_for_table()
    for m in card["metrics"].values():
        m["grade"] = "B+"
        m.pop("stars", None)
    _headers, rows = rc.metric_table(card)
    assert [r[-1] for r in rows] == ["B+"] * 4
    # ...and a metric with neither is still an honest n/a.
    card["metrics"]["pace"].pop("grade")
    _headers, rows = rc.metric_table(card)
    assert rows[1][-1] == "n/a"


def test_both_renderers_take_the_metric_table_from_one_source():
    """The guard that makes the extraction worth anything: add a column to
    metric_table and BOTH the markdown card and the PDF must show it. Before
    0.48.0 each built its own header row, so one could gain a column the other
    never rendered.

    The RATING cell is the one deliberate exception since 0.50.0 — the markdown
    prints glyphs and the PDF draws SVG geometry, because the brand mono has no
    star glyph and a text star would render in whatever font the host machine
    happens to have. They are checked below for agreeing on the VALUE, which is
    the thing that must not diverge.
    """
    from local_fitness.agent import branding, render, visuals

    card = _graded_card_for_table()
    headers, rows = rc.metric_table(card)

    md = render.render_table(headers, rows)
    html_out = visuals._render_metric_table_html(
        card, branding.load_theme(), visuals.CARD_DENSITY_PRESETS[0])

    for h in headers:
        assert h in md, f"markdown lost header {h}"
        assert f"<th>{h}</th>" in html_out, f"PDF lost header {h}"
    for row in rows:
        for cell in row[:-1]:                       # every cell but the rating
            assert cell in md, f"markdown lost cell {cell}"
            assert cell in html_out, f"PDF lost cell {cell}"


def test_both_renderers_show_the_same_rating_value():
    """The rating is drawn two different ways and must still be ONE number.

    The markdown carries the numeral; the PDF carries it in the numeral span AND
    in the SVG's aria-label. A renderer that quantized differently, or read a
    different key, would show a different number here.
    """
    from local_fitness.agent import branding, visuals

    card = _graded_card_for_table()
    _headers, rows = rc.metric_table(card)
    html_out = visuals._render_metric_table_html(
        card, branding.load_theme(), visuals.CARD_DENSITY_PRESETS[0])

    for (key, _label), row in zip(rc._METRIC_LABELS, rows, strict=True):
        score = card["metrics"][key]["stars"]
        assert f"{score:.2f}" in row[-1]                       # markdown numeral
        assert f'<span class="star-num">{score:.2f}</span>' in html_out
        assert f'aria-label="{score:.2f} out of 5 stars"' in html_out


def test_metric_notes_are_shared_and_table_ordered():
    card = _graded_card_for_table()
    card["metrics"]["distance"]["note"] = "spot on"
    assert rc.metric_notes(card) == [("Distance", "spot on"), ("Pace", "a shade slow")]
    # A metric with no note contributes nothing.
    assert all(label not in ("Avg HR", "Continuity")
               for label, _n in rc.metric_notes(card))


# --- prescribed walks (0.55.0) ---------------------------------------------
#
# The walk floor exists to catch a walk MASQUERADING as a run. A plan is also
# allowed to prescribe a walk outright, and then walking is compliance. The
# rubric could not tell the two apart, so obeying a prescribed walk was
# punished — the same inversion as 0.40.0's load metric.

def _spk(mmss: str) -> float:
    m, s = mmss.split(":")
    return (int(m) * 60 + int(s)) / 1.609344


def test_obeying_a_prescribed_walk_is_not_penalized():
    exact = rc.pace_deviation(
        _spk("17:00"), _spk("17:00"), "easy", prescribed_walk=True)
    assert exact == 0.0
    assert rc.stars_from_deviation(exact, "pace") == 5.0


def test_walking_slower_than_prescribed_is_still_compliance():
    # An easy day is free on the slow side, and a recovery walk taken gently is
    # the LAST thing that should be marked down. Pre-fix this hit the 1.00 floor.
    for slower in ("18:00", "20:00", "25:00"):
        d = rc.pace_deviation(
            _spk(slower), _spk("17:00"), "easy", prescribed_walk=True)
        assert d == 0.0, slower


def test_walking_faster_than_prescribed_still_costs():
    # The easy-day gate must keep working — this is a recovery prescription,
    # and hurrying it is the one thing worth flagging.
    d = rc.pace_deviation(
        _spk("14:00"), _spk("17:00"), "easy", prescribed_walk=True)
    assert d > 0.15
    assert rc.stars_from_deviation(d, "pace") < 3.0


def test_the_floor_is_unchanged_when_the_walk_was_not_prescribed():
    # The regression this guard exists to prevent: a walk graded against a
    # RUNNING expectation must still be floored, or a walking-desk session
    # scores a perfect pace against an easy target again (the 0.26.0 case).
    d = rc.pace_deviation(_spk("20:00"), _spk("9:39"), "easy")
    assert d > 0.35
    assert rc.stars_from_deviation(d, "pace") == 1.0


def test_a_slow_rolling_reference_is_not_an_instruction_to_walk():
    # `prescribed_walk` is gated on the PLAN reference in build_card. A rolling
    # median that merely happens to be walk-paced must keep the floor, or the
    # documented quality-day hole reopens (a 15:20/mi walk on a prescribed
    # tempo day scoring A+ against a walking-pool median).
    # 15:20 is FASTER than the 16:00 walking median, so quality's slow-only
    # gate scores 0.0 and the floor is the only thing standing between this and
    # a perfect pace. Pinned as the DIFFERENCE the flag makes, so neither side
    # can drift silently.
    d = rc.pace_deviation(_spk("15:20"), _spk("16:00"), "quality")
    assert d > 0.15, "quality walk floor stopped firing"
    assert rc.pace_deviation(
        _spk("15:20"), _spk("16:00"), "quality", prescribed_walk=True) == 0.0


def test_a_walk_prescription_keeps_its_plan_reference(tmp_path):
    """The compounding half: `plan_walk_mismatch` refused the plan outright.

    It fires on "walked + plan type is a running type", and a prescribed walk
    is stored as `easy`. So every day of a walking block refused its own
    target, printed a note claiming the prescription did not apply to the
    effort it prescribed, and fell back to a rolling reference that — in an
    injury block — is itself all walks and cannot grade anything.
    """
    import pathlib
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent / "evals"))
    from report_cards import grade

    card = grade("prescribed_walk_obeyed", tmp_path)
    assert card["metrics"]["pace"]["reference"] == "plan"
    assert card["metrics"]["distance"]["reference"] == "plan"
    assert not card["metrics"]["pace"].get("note")
    assert card["overall"]["graded_metrics"] >= 3
