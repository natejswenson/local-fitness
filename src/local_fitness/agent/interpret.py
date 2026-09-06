"""Shared, pure interpretation classifiers — zones, deltas, strengths, gaps.

Every judgment here is a plain function over already-computed numbers: no I/O,
no SDK, no schemas, stdlib only. This is what makes ``status.py`` (the brief
path) and the ad-hoc analysis tools (``agent/tools.py``) agree by
construction instead of the LLM re-deriving the same read twice, once well
and once by eyeballing a raw float.

Design notes:

* Every classifier handles ``None``/zero/degenerate input without raising —
  the same guarantee the rest of the read paths make on an empty DB.
* Two different inclusivity conventions are used **deliberately**, not by
  accident: ``baseline_position``'s ±1 SD bands are strict (`` > +1`` /
  ``< -1``), while ``correlation_read`` and ``effect_size`` use inclusive
  lower bounds (``>=``). ``trend_direction`` and ``delta_direction``'s flat
  bands are inclusive (``<=``) so a zero threshold / zero delta still
  classifies as flat, with no side door. See each function's docstring.
* Named constants exist for every band so a boundary value can be pinned by
  test on both sides, and so callers outside this module (``brief_planner``'s
  ``_TRIGGERS``) can reference the same numbers instead of re-stating them.
"""
from __future__ import annotations

# --- tsb_zone ---------------------------------------------------------------
# Training stress balance (CTL - ATL) zone bands — extracted from
# status._tsb_interpretation; status.py delegates here so the brief and the
# analysis tools agree by construction.
TSB_VERY_FATIGUED = -20.0
TSB_FATIGUED = -10.0
TSB_FRESH = 5.0

# --- delta_direction ---------------------------------------------------------
# Flat band for scalar %-delta fields with no series/slope behind them (e.g.
# ctl_pct_change_14d).
DELTA_DIRECTION_FLAT_PCT = 2.0

# --- baseline_position -------------------------------------------------------
# Strict (non-inclusive) SD bands — exactly ±1.0 is "normal".
BASELINE_POSITION_SD = 1.0

# --- correlation_read ---------------------------------------------------------
# |r| thresholds, lower-bound inclusive. Mirrors the bands that used to live
# in correlate's static legend string (tools.py:679).
CORRELATION_MODEST = 0.2
CORRELATION_MODERATE = 0.4
CORRELATION_STRONG = 0.6

# --- effect_size ---------------------------------------------------------
# Cohen's d conventional magnitude thresholds, lower-bound inclusive.
COHENS_D_SMALL = 0.2
COHENS_D_MODERATE = 0.5
COHENS_D_LARGE = 0.8

# --- trend_direction ---------------------------------------------------------
# Multiplier on the fetched window's sample SD that defines "flat" fitted
# total change across the window (see get_metric_trend's flat_threshold
# derivation).
TREND_FLAT_SD_MULTIPLIER = 0.5

# --- riegel_confidence -------------------------------------------------------
# How far a Riegel projection reaches beyond the effort it was measured from,
# as goal distance / effort distance. Upper-bound **inclusive** (unlike
# baseline_position's strict bands): a 5k measured onto a 10k is exactly 2.0
# and is squarely "medium", not a boundary case to argue about.
#
# The numbers are the exponent's own honesty range, not taste: t2 = t1 *
# (d2/d1)^1.06 was fitted on race results at adjacent distances, so a 1.5x
# reach (5k -> 10k-ish) is what it was built for, while a 10x reach (2 km ->
# half marathon) is asserting a race result from a warmup.
RIEGEL_HIGH_RATIO = 1.5
RIEGEL_MEDIUM_RATIO = 3.0


def tsb_zone(tsb: float | None) -> str:
    """Plain-English read of training stress balance.

    Extracted from ``status._tsb_interpretation`` (status.py delegates here).
    On ``None`` this deliberately returns ``_tsb_interpretation``'s existing
    None-case string ("no training-load data yet" — a sentence, not a zone
    label) for delegation consistency; reachable on ``training_load_status``
    since its SQL filters on ``ctl``, not ``tsb``.
    """
    if tsb is None:
        return "no training-load data yet"
    if tsb < TSB_VERY_FATIGUED:
        return "very fatigued"
    if tsb < TSB_FATIGUED:
        return "fatigued"
    if tsb > TSB_FRESH:
        return "fresh"
    return "neutral"


def pct_change(now: float | None, then: float | None) -> float | None:
    """Percent change from ``then`` to ``now``.

    ``None`` when either input is ``None`` or ``then == 0`` (a zero baseline
    has no defined % change — matches the source truthiness guard at
    brief_planner.py:507). Returns the **unrounded** float; rounding stays at
    the boundaries (callers round).
    """
    if now is None or then is None or then == 0:
        return None
    return (now - then) / then * 100


def trend_direction(slope_per_day: float | None, *, flat_threshold: float) -> str:
    """Classify a per-observation slope into rising/falling/flat/no data.

    Returns ``"no data"`` **iff** ``slope_per_day is None`` — it never sees
    ``n``; the None-mapping for too-few-samples windows is the caller's job
    (see ``get_metric_trend``). ``"flat"`` when
    ``abs(slope_per_day) <= flat_threshold`` (inclusive — so a zero threshold
    from a constant-series SD still classifies slope 0 as flat, no side
    door); sign otherwise.
    """
    if slope_per_day is None:
        return "no data"
    if abs(slope_per_day) <= flat_threshold:
        return "flat"
    return "rising" if slope_per_day > 0 else "falling"


def delta_direction(pct_change: float | None, *, flat_pct: float = DELTA_DIRECTION_FLAT_PCT) -> str:
    """Classify a scalar %-delta field with no series/slope behind it (e.g.
    ``ctl_pct_change_14d``) — routing this through ``trend_direction`` would
    be shape-incoherent, hence a separate classifier.

    ``None`` -> ``"no data"``; ``abs(pct_change) <= flat_pct`` -> ``"flat"``
    (inclusive); sign otherwise.
    """
    if pct_change is None:
        return "no data"
    if abs(pct_change) <= flat_pct:
        return "flat"
    return "rising" if pct_change > 0 else "falling"


def baseline_position(sd_distance: float | None) -> str:
    """Classify a signed SD distance against the current-vs-baseline bands.

    Bands are **deliberately strict** (unlike ``correlation_read`` /
    ``effect_size``'s inclusive lower bounds): exactly ``+1.0`` or ``-1.0``
    is ``"normal"``. ``None`` -> ``"no data"``.
    """
    if sd_distance is None:
        return "no data"
    if sd_distance > BASELINE_POSITION_SD:
        return "elevated"
    if sd_distance < -BASELINE_POSITION_SD:
        return "suppressed"
    return "normal"


def correlation_read(r: float | None) -> dict | None:
    """Strength/direction read of a Pearson r.

    ``r is None`` -> ``None`` (no strength/direction — e.g. ``correlate``'s
    zero-variance-denominator case). Lower-bound **inclusive** thresholds:
    ``|r| >= 0.2`` modest, ``>= 0.4`` moderate, ``>= 0.6`` strong, below 0.2
    weak. Direction at ``r == 0.0`` is ``"positive"`` via ``>=`` (consistent
    with ``sd_position``'s rule).
    """
    if r is None:
        return None
    abs_r = abs(r)
    if abs_r >= CORRELATION_STRONG:
        strength = "strong"
    elif abs_r >= CORRELATION_MODERATE:
        strength = "moderate"
    elif abs_r >= CORRELATION_MODEST:
        strength = "modest"
    else:
        strength = "weak"
    direction = "positive" if r >= 0 else "negative"
    return {"strength": strength, "direction": direction}


def effect_size(
    mean_a: float | None, mean_b: float | None,
    sd_a: float | None, sd_b: float | None,
    n_a: int | None, n_b: int | None,
) -> dict | None:
    """``delta_pct`` + Cohen's d read between two samples (pooled SD).

    ``delta_pct = (mean_a - mean_b) / mean_b * 100`` — matching
    ``compare_periods``'s ``delta_mean_a_minus_b`` a-minus-b direction.

    Degrades **per-field**, not whole-function: whole-``None`` only when ALL
    six inputs are ``None`` (unreachable from ``compare_periods``, whose
    ``_stats`` always returns an int ``n`` — kept as a pure-function contract
    for direct callers/tests). Otherwise: ``delta_pct`` needs only the two
    means (``None`` only when ``mean_b`` is 0/None or ``mean_a`` is ``None``)
    and is computed even when the SDs/ns can't support a d;
    ``cohens_d``/``magnitude`` are ``None`` when either SD is 0/None or
    either ``n < 2``. Magnitude bands are lower-bound inclusive: ``|d| >=
    0.2`` small, ``>= 0.5`` moderate, ``>= 0.8`` large, below 0.2 negligible.
    """
    if (mean_a is None and mean_b is None and sd_a is None and sd_b is None
            and n_a is None and n_b is None):
        return None

    delta_pct = None
    if mean_a is not None and mean_b:
        delta_pct = (mean_a - mean_b) / mean_b * 100

    cohens_d = None
    magnitude = None
    sds_ok = sd_a not in (None, 0) and sd_b not in (None, 0)
    ns_ok = n_a is not None and n_b is not None and n_a >= 2 and n_b >= 2
    means_ok = mean_a is not None and mean_b is not None
    if sds_ok and ns_ok and means_ok:
        pooled_sd = (((n_a - 1) * sd_a ** 2 + (n_b - 1) * sd_b ** 2) / (n_a + n_b - 2)) ** 0.5
        if pooled_sd:
            cohens_d = (mean_a - mean_b) / pooled_sd
            abs_d = abs(cohens_d)
            if abs_d >= COHENS_D_LARGE:
                magnitude = "large"
            elif abs_d >= COHENS_D_MODERATE:
                magnitude = "moderate"
            elif abs_d >= COHENS_D_SMALL:
                magnitude = "small"
            else:
                magnitude = "negligible"

    return {"delta_pct": delta_pct, "cohens_d": cohens_d, "magnitude": magnitude}


def riegel_confidence(extrapolation_ratio: float | None) -> str:
    """How much to trust a Riegel projection, from how far it reaches.

    ``extrapolation_ratio`` is goal distance / measured-effort distance.
    Bands are upper-bound **inclusive**: ``<= 1.5`` high, ``<= 3.0`` medium,
    above that low. See ``RIEGEL_HIGH_RATIO`` for why those numbers.

    ``None`` or a non-positive ratio -> ``"no data"``: a zero/negative reach
    is degenerate, not confident. Unreachable from ``plans.build_plan_detail``
    (which computes the field only once ``riegel_predict`` has returned a
    number, which requires both distances truthy) — defined and pinned anyway,
    per this module's never-raise-on-degenerate-input contract.
    """
    if extrapolation_ratio is None or extrapolation_ratio <= 0:
        return "no data"
    if extrapolation_ratio <= RIEGEL_HIGH_RATIO:
        return "high"
    if extrapolation_ratio <= RIEGEL_MEDIUM_RATIO:
        return "medium"
    return "low"


def sd_position(value: float | None, mean: float | None, sd: float | None) -> dict | None:
    """Signed SD distance + direction of ``value`` against ``mean``/``sd``.

    ``None`` when ``value``/``mean``/``sd`` is missing or ``sd`` is 0 (no
    ``ZeroDivisionError``). Direction at a delta of exactly 0 is ``"above"``
    via ``>=`` (``value >= mean`` -> ``"above"``) — effectively unreachable
    in sane calls (``find_anomalies``'s SQL requires
    ``ABS(value - mean) > sd * threshold``; ``_rhr_anomalies`` requires
    ``> 2*sd``), but defined and pinned regardless.
    """
    if value is None or mean is None or not sd:
        return None
    sd_distance = (value - mean) / sd
    direction = "above" if value >= mean else "below"
    return {"sd_distance": sd_distance, "direction": direction}


# --- locomotion -------------------------------------------------------------
# The run/walk boundary, in seconds per MILE. A brisk walker reaches a 13:00
# mile; sustained running essentially never falls below it.
#
# This lives here, not in a caller, because ``activity_type`` is Garmin's
# LABEL and it lies: a walking-desk session logs as ``treadmill_running``, so
# every substring-on-the-label classifier in the codebase (``plans._is_running``
# among them) passes walks straight through as runs. Measured on live data
# (2026-07-21), a 60-day ``treadmill_running`` pool was cleanly bimodal — 16
# real runs at 8:40-11:46/mi against 30 walking-pad sessions at 14:08-84:20/mi,
# with nothing at all in between. 13:00 sits in that empty band with roughly
# two minutes of margin on either side.
#
# Both the report card's reference pool and plans.py's mileage rollup gate on
# this one function, per the module rule: one classifier, one home.
RUN_PACE_CEILING_SEC_PER_MI = 13 * 60
_KM_PER_MILE = 1.609344


def is_running_effort(pace_sec_per_km: float | None) -> bool | None:
    """Was this activity run or walked, judged by pace rather than by label?

    ``None`` when there is no usable pace — the mode is genuinely unknown, and
    the caller must exclude the row rather than guess a side. Returning a
    third state (instead of defaulting to ``False``) is what keeps a paceless
    row out of BOTH pools: ``None is True`` and ``None is False`` are each
    false, so an identity filter drops it without a special case.

    See ``RUN_PACE_CEILING_SEC_PER_MI`` for why this is measured, not labelled.
    """
    if not pace_sec_per_km or pace_sec_per_km <= 0:
        return None
    return pace_sec_per_km * _KM_PER_MILE <= RUN_PACE_CEILING_SEC_PER_MI


# --- rep-split selection ----------------------------------------------------
# The smallest split a quality-day pace judgment will read. Deliberately NOT
# the ``partial`` flag: that is relative to the workout's OWN longest lap, so a
# manually-lapped interval session (2-mile warmup, then 800m reps) marks every
# rep partial and leaves the warmup as the only "full" split — which graded the
# reps at warmup pace and guaranteed an F on exactly the workouts the splits
# exception exists to grade fairly. 300 m sits under a standard 400 m rep and
# well over any trailing GPS fragment.
#
# This lives here, not in ``report_card``, for the same reason
# ``is_running_effort`` does: ``report_card`` imports ``plans``, so ``plans``
# cannot import back, and both surfaces must select the rep the same way or a
# plan verdict and a report card can disagree about the same session (#242).
QUALITY_MIN_SPLIT_M = 300.0


def fastest_rep_split(labelled: dict) -> dict | None:
    """The fastest rep-sized split, or ``None`` when there isn't one.

    Rep-sized is ``distance_meters >= QUALITY_MIN_SPLIT_M`` rather than "not
    ``partial``". The partial flag is measured against the workout's own longest
    lap, which on a manually-lapped session is the warmup — so every rep of a
    2-mile-warmup-then-800s workout is partial and the warmup is the only
    candidate left, which is how a correctly-run interval session got graded at
    warmup pace.

    The distance floor still solves what the partial filter was there for: a
    90-metre trailing fragment can post an absurdly fast pace and would win
    every time. Anything long enough to be a rep is a fair candidate, and a
    slower warmup simply loses ``min()``.

    ``labelled`` is any mapping with a ``rows`` list — ``report_card``'s
    ``label_splits`` output, or raw ``activity_splits`` rows wrapped as
    ``{"rows": splits}``: only ``distance_meters`` and ``avg_pace_sec_per_km``
    are read, and both are columns of the table.

    This is the one place a *grade* is allowed to read splits — see the quality
    branch of ``report_card.build_card`` and the quality arm of
    ``plans.classify_workout`` for why, and ``report_card``'s module docstring
    for the rule it is an exception to.
    """
    candidates = [r for r in (labelled.get("rows") or [])
                  if (r.get("distance_meters") or 0.0) >= QUALITY_MIN_SPLIT_M
                  and r.get("avg_pace_sec_per_km")]
    return min(candidates, key=lambda r: r["avg_pace_sec_per_km"]) if candidates else None


def fastest_rep_split_pace(labelled: dict) -> float | None:
    """:func:`fastest_rep_split`'s pace in sec/km, or ``None``."""
    best = fastest_rep_split(labelled)
    return best["avg_pace_sec_per_km"] if best else None
