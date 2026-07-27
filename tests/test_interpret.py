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
