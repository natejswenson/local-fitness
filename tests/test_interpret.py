"""Tests for agent/interpret.py — the shared, pure interpretation classifiers.

Every classifier is a plain function over already-computed numbers: no DB, no
SDK. This file pins every band boundary on both sides and every degenerate
(``None``/zero) input path, per the WS1 design contract — this module should
be ~100% covered.
"""
from __future__ import annotations

import pytest

from local_fitness.agent import interpret

# === tsb_zone ================================================================

@pytest.mark.parametrize("tsb,expected", [
    (None, "no training-load data yet"),
    (-25.0, "very fatigued"),
    (-20.0001, "very fatigued"),
    (-20.0, "fatigued"),          # boundary: NOT very-fatigued at exactly -20
    (-10.0001, "fatigued"),
    (-10.0, "neutral"),           # boundary: NOT fatigued at exactly -10
    (0.0, "neutral"),
    (5.0, "neutral"),             # boundary: NOT fresh at exactly +5
    (5.0001, "fresh"),
    (10.0, "fresh"),
])
def test_tsb_zone_bands(tsb, expected):
    assert interpret.tsb_zone(tsb) == expected


# === pct_change ===============================================================

def test_pct_change_none_now_is_none():
    assert interpret.pct_change(None, 10.0) is None


def test_pct_change_none_then_is_none():
    assert interpret.pct_change(10.0, None) is None


def test_pct_change_both_none_is_none():
    assert interpret.pct_change(None, None) is None


def test_pct_change_zero_baseline_is_none():
    # Zero baseline has no defined % change — matches the source's
    # truthiness guard at brief_planner.py:507.
    assert interpret.pct_change(10.0, 0.0) is None


def test_pct_change_computed_exact():
    assert interpret.pct_change(12.0, 10.0) == 20.0
    assert interpret.pct_change(8.0, 10.0) == -20.0


def test_pct_change_returns_unrounded():
    # (1 - 3) / 3 * 100 = -66.6666...; rounding stays at the caller boundary.
    result = interpret.pct_change(1.0, 3.0)
    assert result == pytest.approx(-66.66666666666667)
    assert result != round(result, 1)


# === trend_direction ==========================================================

def test_trend_direction_none_is_no_data():
    assert interpret.trend_direction(None, flat_threshold=1.0) == "no data"


def test_trend_direction_zero_threshold_constant_series_is_flat():
    # Load-bearing inclusive case: a constant series has slope 0 and a
    # sample-SD-derived flat_threshold of 0 — must still classify "flat".
    assert interpret.trend_direction(0.0, flat_threshold=0.0) == "flat"


@pytest.mark.parametrize("slope,threshold,expected", [
    (0.5, 0.5, "flat"),      # exactly at threshold, positive side
    (-0.5, 0.5, "flat"),     # exactly at threshold, negative side
    (0.5001, 0.5, "rising"),
    (-0.5001, 0.5, "falling"),
    (0.0, 1.0, "flat"),
])
def test_trend_direction_bands(slope, threshold, expected):
    assert interpret.trend_direction(slope, flat_threshold=threshold) == expected


# === delta_direction ===========================================================

def test_delta_direction_none_is_no_data():
    assert interpret.delta_direction(None) == "no data"


@pytest.mark.parametrize("pct,expected", [
    (2.0, "flat"),        # exactly at the default flat_pct, positive side
    (-2.0, "flat"),        # exactly at the default flat_pct, negative side
    (2.0001, "rising"),
    (-2.0001, "falling"),
    (0.0, "flat"),
])
def test_delta_direction_default_flat_pct_bands(pct, expected):
    assert interpret.delta_direction(pct) == expected


def test_delta_direction_custom_flat_pct():
    assert interpret.delta_direction(5.0, flat_pct=10.0) == "flat"
    assert interpret.delta_direction(10.0, flat_pct=10.0) == "flat"
    assert interpret.delta_direction(10.0001, flat_pct=10.0) == "rising"


# === baseline_position =========================================================

def test_baseline_position_none_is_no_data():
    assert interpret.baseline_position(None) == "no data"


@pytest.mark.parametrize("sd_distance,expected", [
    (1.0, "normal"),        # strict bands: exactly +1.0 -> normal, not elevated
    (-1.0, "normal"),       # exactly -1.0 -> normal, not suppressed
    (1.0001, "elevated"),
    (-1.0001, "suppressed"),
    (0.0, "normal"),
])
def test_baseline_position_bands(sd_distance, expected):
    assert interpret.baseline_position(sd_distance) == expected


# === correlation_read ==========================================================

def test_correlation_read_none_is_none():
    assert interpret.correlation_read(None) is None


@pytest.mark.parametrize("r,strength,direction", [
    (0.0, "weak", "positive"),        # r == 0.0 -> "positive" via >=
    (0.1999, "weak", "positive"),
    (0.2, "modest", "positive"),      # lower-bound inclusive
    (0.3999, "modest", "positive"),
    (0.4, "moderate", "positive"),    # lower-bound inclusive
    (0.5999, "moderate", "positive"),
    (0.6, "strong", "positive"),      # lower-bound inclusive
    (0.9, "strong", "positive"),
    (-0.0001, "weak", "negative"),
    (-0.6, "strong", "negative"),
])
def test_correlation_read_bands(r, strength, direction):
    result = interpret.correlation_read(r)
    assert result == {"strength": strength, "direction": direction}


# === effect_size ===============================================================

def test_effect_size_all_none_is_none():
    assert interpret.effect_size(None, None, None, None, None, None) is None


def test_effect_size_full_computation():
    # mean_a=10, mean_b=5: delta_pct = (10-5)/5*100 = 100.0
    # pooled_sd = sqrt(((9*4)+(9*4))/18) = 2.0; cohens_d = (10-5)/2 = 2.5 -> large
    result = interpret.effect_size(10.0, 5.0, 2.0, 2.0, 10, 10)
    assert result["delta_pct"] == 100.0
    assert result["cohens_d"] == 2.5
    assert result["magnitude"] == "large"


def test_effect_size_two_one_day_periods_degrades_per_field():
    # compare_periods' single-sample-period case: sd is 0 via the
    # max(len - 1, 1) denominator, n is 1 for each period. delta_pct must
    # still be computed from the means; cohens_d/magnitude must be None.
    result = interpret.effect_size(10.0, 8.0, 0.0, 0.0, 1, 1)
    assert result["delta_pct"] == 25.0
    assert result["cohens_d"] is None
    assert result["magnitude"] is None


def test_effect_size_zero_mean_b_nulls_only_delta_pct():
    # mean_b == 0 nulls delta_pct (division guard) but does NOT block
    # cohens_d, which divides by pooled_sd, not mean_b.
    result = interpret.effect_size(5.0, 0.0, 1.0, 1.0, 5, 5)
    assert result["delta_pct"] is None
    assert result["cohens_d"] is not None
    assert result["magnitude"] is not None


def test_effect_size_none_mean_a_nulls_both_fields():
    result = interpret.effect_size(None, 5.0, 1.0, 1.0, 5, 5)
    assert result["delta_pct"] is None
    assert result["cohens_d"] is None
    assert result["magnitude"] is None


def test_effect_size_zero_sd_nulls_only_cohens_d():
    result = interpret.effect_size(5.0, 3.0, 0.0, 2.0, 5, 5)
    assert result["delta_pct"] == pytest.approx((5.0 - 3.0) / 3.0 * 100)
    assert result["cohens_d"] is None
    assert result["magnitude"] is None


def test_effect_size_none_sd_nulls_only_cohens_d():
    result = interpret.effect_size(5.0, 3.0, None, 2.0, 5, 5)
    assert result["cohens_d"] is None
    assert result["magnitude"] is None


def test_effect_size_n_below_two_nulls_only_cohens_d():
    result = interpret.effect_size(5.0, 3.0, 1.0, 1.0, 1, 5)
    assert result["delta_pct"] == pytest.approx((5.0 - 3.0) / 3.0 * 100)
    assert result["cohens_d"] is None
    assert result["magnitude"] is None


@pytest.mark.parametrize("mean_a,expected_magnitude", [
    (0.19999, "negligible"),
    (0.2, "small"),        # lower-bound inclusive
    (0.49999, "small"),
    (0.5, "moderate"),     # lower-bound inclusive
    (0.79999, "moderate"),
    (0.8, "large"),        # lower-bound inclusive
    (1.5, "large"),
])
def test_effect_size_magnitude_bands(mean_a, expected_magnitude):
    # pooled_sd == 1.0 when sd_a == sd_b == 1.0 regardless of n (as long as
    # n_a == n_b), so mean_a - mean_b == cohens_d directly here.
    result = interpret.effect_size(mean_a, 0.0, 1.0, 1.0, 2, 2)
    assert result["cohens_d"] == pytest.approx(mean_a)
    assert result["magnitude"] == expected_magnitude


def test_effect_size_negative_cohens_d_uses_absolute_value_for_magnitude():
    result = interpret.effect_size(-0.2, 0.0, 1.0, 1.0, 2, 2)
    assert result["cohens_d"] == pytest.approx(-0.2)
    assert result["magnitude"] == "small"


# === sd_position ================================================================

@pytest.mark.parametrize("value,mean,sd", [
    (None, 5.0, 1.0),
    (5.0, None, 1.0),
    (5.0, 3.0, None),
    (5.0, 3.0, 0.0),
])
def test_sd_position_degenerate_inputs_are_none(value, mean, sd):
    assert interpret.sd_position(value, mean, sd) is None


def test_sd_position_above():
    result = interpret.sd_position(5.0, 3.0, 2.0)
    assert result == {"sd_distance": 1.0, "direction": "above"}


def test_sd_position_below():
    result = interpret.sd_position(3.0, 5.0, 2.0)
    assert result == {"sd_distance": -1.0, "direction": "below"}


def test_sd_position_zero_delta_is_above():
    # Direction at delta 0 is "above" via >= — effectively unreachable in
    # sane calls (find_anomalies/_rhr_anomalies filter on a real distance),
    # but defined and pinned regardless.
    result = interpret.sd_position(3.0, 3.0, 2.0)
    assert result == {"sd_distance": 0.0, "direction": "above"}


# === riegel_confidence =========================================================

@pytest.mark.parametrize("ratio,expected", [
    (1.0, "high"),          # a 5k effort onto a 5k goal — no reach at all
    (1.5, "high"),          # boundary: inclusive upper bound, still high
    (1.5001, "medium"),
    (2.0, "medium"),        # 5k measured, 10k goal
    (3.0, "medium"),        # boundary: inclusive upper bound, still medium
    (3.0001, "low"),
    (10.5, "low"),          # 2 km effort onto a half marathon
    (21.1, "low"),          # 2 km effort onto a marathon
])
def test_riegel_confidence_bands(ratio, expected):
    assert interpret.riegel_confidence(ratio) == expected


@pytest.mark.parametrize("ratio", [None, 0.0, -1.0])
def test_riegel_confidence_degenerate_inputs_are_no_data(ratio):
    # A zero/negative reach is degenerate, not confident. Unreachable from
    # build_plan_detail (which needs both distances truthy first), pinned
    # anyway per the module's never-raise contract.
    assert interpret.riegel_confidence(ratio) == "no data"


# === fastest_rep_split =======================================================
# Moved here from report_card at 0.63.0 so plans.classify_workout can select the
# same rep the card grades (#242). The card's own cases stay in
# tests/test_report_card.py against the re-exported names; these pin the
# selection rule at its new home, including the raw-rows calling convention
# plans.py uses.

def _rows(*pairs):
    """``(distance_m, pace_sec_per_km)`` pairs as raw ``activity_splits`` rows."""
    return {"rows": [{"distance_meters": d, "avg_pace_sec_per_km": p}
                     for d, p in pairs]}


def test_fastest_rep_split_takes_the_fastest_rep_sized_split():
    # 2 km warmup at 410, four 800m reps at 260, 2 km cooldown at 528.
    labelled = _rows((2000.0, 410.0), *[(800.0, 260.0)] * 4, (2000.0, 528.0))
    assert interpret.fastest_rep_split_pace(labelled) == pytest.approx(260.0)
    assert interpret.fastest_rep_split(labelled)["distance_meters"] == 800.0


def test_fastest_rep_split_ignores_anything_under_the_floor():
    """A 90 m trailing fragment posts an absurd pace and would win every time;
    a 200 m recovery jog is not a rep either. Both sit under the floor."""
    assert interpret.QUALITY_MIN_SPLIT_M == 300.0
    labelled = _rows((1609.344, 400.0), (90.0, 120.0), (200.0, 250.0))
    assert interpret.fastest_rep_split_pace(labelled) == pytest.approx(400.0)


def test_fastest_rep_split_boundary_is_inclusive():
    """Exactly QUALITY_MIN_SPLIT_M is rep-sized; a hair under is not."""
    assert interpret.fastest_rep_split_pace(
        _rows((1000.0, 400.0), (300.0, 250.0))) == pytest.approx(250.0)
    assert interpret.fastest_rep_split_pace(
        _rows((1000.0, 400.0), (299.99, 250.0))) == pytest.approx(400.0)


@pytest.mark.parametrize("labelled", [
    {}, {"rows": None}, {"rows": []},
    {"rows": [{"distance_meters": 800.0, "avg_pace_sec_per_km": None}]},
    {"rows": [{"distance_meters": None, "avg_pace_sec_per_km": 260.0}]},
])
def test_fastest_rep_split_abstains_rather_than_raising(labelled):
    """No splits, no paces, no distances — every degenerate shape returns None
    so the caller abstains. plans.py grades the backfilled tail (no splits at
    all) on volume alone through exactly this path."""
    assert interpret.fastest_rep_split(labelled) is None
    assert interpret.fastest_rep_split_pace(labelled) is None


def test_fastest_rep_split_ignores_a_closing_kick_beside_repeated_mile_laps():
    """#242 f-8a26a574: a tempo prescribing 3x1mi has reps that ARE mile-length
    auto-laps. A fast trailing fragment (a closing kick, or the watch's own
    always-emitted partial lap) sits above QUALITY_MIN_SPLIT_M and would win
    on pace alone — certifying a session whose every prescribed mile ran
    2:20/mi off target as having hit its rep pace."""
    labelled = _rows((1609.344, 625.0), (1609.344, 607.0), (1609.344, 648.0),
                      (400.0, 421.0))
    assert interpret.fastest_rep_split_pace(labelled) == pytest.approx(607.0)
    assert interpret.fastest_rep_split(labelled)["distance_meters"] == \
        pytest.approx(1609.344)


def test_fastest_rep_split_still_prefers_a_genuine_short_rep_over_the_warmup():
    """The dominant-cluster guard must not resurrect the manual-lap failure it
    sits beside: four repeated 800 m reps outrank a single, larger 1600 m
    warmup lap even though the warmup's bucket has only one member."""
    labelled = _rows((1600.0, 390.0), *[(800.0, 260.0)] * 4)
    assert interpret.fastest_rep_split_pace(labelled) == pytest.approx(260.0)


def test_fastest_rep_split_does_nothing_without_a_repeated_lap_size():
    """A single-rep day (or one with no consistent lap size) has no dominant
    unit to compare against — the guard must not invent one and exclude the
    only rep-sized split there is."""
    labelled = _rows((1000.0, 400.0), (300.0, 250.0))
    assert interpret.fastest_rep_split_pace(labelled) == pytest.approx(250.0)


# --- a matched warmup/cooldown pair is a repeat that is not a rep -------------
# #242 r2, f-854c3442 / f-04b7680c. `len(bucket) >= 2` treated bookends as the
# session's dominant unit and the floor they defined deleted the real work
# between them — the "graded the reps at warmup pace" failure the whole selector
# exists to escape, one layer up. Every case below FAILS on the pre-fix code
# with the warmup's pace as the answer.

@pytest.mark.parametrize("splits, expected, note", [
    # f-854c3442: a single 800 m rep bracketed by a matched 2-mile warmup and
    # cooldown. The 3218 m bucket repeats, floor 1931 m, the one rep is dropped.
    ([(3218.688, 410.0), (800.0, 260.0), (3218.688, 425.0)], 260.0,
     "one rep inside a matched 2mi warmup/cooldown"),
    ([(1609.344, 400.0), (600.0, 255.0), (1609.344, 415.0)], 255.0,
     "one rep inside a matched 1mi warmup/cooldown"),
    # f-04b7680c: counts TIE at 2, and the old tie-break took the larger bucket.
    ([(1609.344, 390.0), (800.0, 260.0), (800.0, 262.0), (1609.344, 430.0)], 260.0,
     "manually-lapped 2x800 inside a matched 1mi warmup/cooldown"),
    ([(3218.688, 420.0), (1609.344, 270.0), (1609.344, 272.0), (3218.688, 450.0)], 270.0,
     "manually-lapped 2x1mi inside a matched 2mi warmup/cooldown"),
])
def test_a_matched_warmup_cooldown_pair_is_never_the_dominant_unit(
        splits, expected, note):
    assert interpret.fastest_rep_split_pace(_rows(*splits)) == \
        pytest.approx(expected), note


def test_the_bookend_exclusion_does_not_readmit_the_closing_kick():
    """The guard the exclusion sits beside must survive it: a 400 m kick after
    three mile auto-laps is still dropped, because the repeated 1609 m bucket
    holds three members and so is not a bookend pair."""
    labelled = _rows((1609.344, 625.0), (1609.344, 607.0), (1609.344, 648.0),
                     (400.0, 421.0))
    assert interpret.fastest_rep_split_pace(labelled) == pytest.approx(607.0)


def test_a_repeated_pair_that_is_the_whole_day_still_counts():
    """Two laps and nothing else ARE the reps — there is no third lap for a
    floor to drop, so excluding them would only lose the dominant unit."""
    assert interpret.fastest_rep_split_pace(
        _rows((800.0, 260.0), (800.0, 255.0))) == pytest.approx(255.0)


def test_a_count_tie_breaks_toward_the_smaller_lap_size():
    """1mi warmup, 2x800 reps, 1mi, then a 400 m kick: the mile bucket is no
    longer a bookend pair (the kick is last), so both buckets tie at two
    members. Breaking toward the larger drops both reps AND the kick and grades
    the day off a mile lap; breaking toward the smaller drops only the kick."""
    labelled = _rows((1609.344, 390.0), (800.0, 260.0), (800.0, 262.0),
                     (1609.344, 430.0), (400.0, 200.0))
    assert interpret.fastest_rep_split_pace(labelled) == pytest.approx(260.0)


def test_bookends_are_matched_by_identity_not_equality():
    """Two laps with the same distance and pace are equal dicts but different
    laps. A three-member bucket whose first and last members happen to equal
    each other must not read as a bookend pair."""
    labelled = _rows((1609.344, 400.0), (1609.344, 300.0), (1609.344, 400.0),
                     (400.0, 250.0))
    # All three miles are one bucket (len 3, not a pair), so the kick is still
    # dropped and the fastest mile wins.
    assert interpret.fastest_rep_split_pace(labelled) == pytest.approx(300.0)


# --- a fragment OUTSIDE the warmup/cooldown pair defeats the pair, not just
# the pair's own bucket ---------------------------------------------------
# #242 r3, f-6d873a9b: bookends were keyed on candidates[0]/candidates[-1],
# so a trailing remainder lap (or a leading walk-out) — a candidate the
# warmup/cooldown pair does not include — became one of the two bookend ids
# in its place. The pair's own bucket then failed the identity match, fell
# back into the dominant-cluster pool, and its floor deleted the real rep
# between them. Every case here FAILED on the pre-fix code, each returning
# the warmup's pace (410.0) instead of the rep's (260.0).

def test_a_trailing_remainder_lap_does_not_defeat_the_bookend_pair():
    """The exact r3 failure scenario: a manually-lapped session's trailing
    remainder lap (the segment Garmin always emits after the last lap press)
    sits after the cooldown and is itself long enough to be a candidate."""
    labelled = _rows((3218.688, 410.0), (800.0, 260.0), (3218.688, 425.0),
                     (400.0, 500.0))
    assert interpret.fastest_rep_split_pace(labelled) == pytest.approx(260.0)


def test_a_leading_walk_out_fragment_does_not_defeat_the_bookend_pair():
    """Same failure, mirrored: a fragment before the warmup instead of after
    the cooldown breaks the identity match from the other end."""
    labelled = _rows((300.0, 200.0), (3218.688, 410.0), (800.0, 260.0),
                     (3218.688, 425.0))
    assert interpret.fastest_rep_split_pace(labelled) == pytest.approx(260.0)


def test_a_trailing_remainder_lap_is_dropped_even_with_no_other_repeat():
    """No warmup/cooldown pair at all — just two reps and a trailing
    remainder lap. The remainder must never win on pace alone merely because
    nothing repeated to floor it out."""
    labelled = _rows((800.0, 260.0), (800.0, 255.0), (400.0, 100.0))
    assert interpret.fastest_rep_split_pace(labelled) == pytest.approx(255.0)


# === module hygiene =============================================================

def test_interpret_imports_nothing_outside_stdlib():
    """Checkable invariant: interpret.py is pure — no db, no SDK, no schemas."""
    import pathlib

    src = pathlib.Path(interpret.__file__).read_text()
    for banned in ("import sqlite3", "from .. import", "from . import",
                   "claude_agent_sdk", "from .schemas"):
        assert banned not in src, banned


def test_km_per_mile_pinned_to_the_units_constant():
    """interpret stays deliberately stdlib-only (its documented contract), so
    it keeps a private copy of the mile factor rather than importing units —
    this pin is what stops the two from drifting."""
    from local_fitness.agent import units

    assert interpret._KM_PER_MILE == units.KM_PER_MILE
