"""Deterministic report-card grading for a single workout.

The coach can already *describe* a workout (``get_workout_detail``), but
nothing *judged* one — every assessment was phrased ad hoc by the model, so
the same run could be called "solid" one day and "flat" the next. This module
makes the judgment tested Python, per the repo convention that the LLM phrases
a judgment but never derives one that code can compute (the ``interpret.py``
pattern).

Four metrics are graded — distance, pace, HR, training load — each reduced to
a single non-negative relative deviation ``d`` and passed through ONE shared
band table. That is the whole trick: four small deviation functions, one
grader, so the rubric stays testable.

Two reference points, and the card always says which it used:

- **plan** — the active training plan's prescribed workout for that date.
  Available for distance and pace only (``plan_workouts`` has no HR or load
  column), so those two always fall back to the rolling reference.
- **rolling_60d** — trailing-60-day *medians* over comparable activities,
  computed on the fly. NOT the ``baselines`` table: that holds only
  rhr/sleep/stress/CTL/ATL/TSB and has no per-workout aggregates at all.
  Median, not mean, because the history carries real training-load outliers.

Comparability is exact activity_type first, widening to the on-foot class only
when the exact pool is too thin. Measured on live data (2026-07-19): pooling
``running`` with ``treadmill_running`` put median HR at 119 when outdoor runs
average 140, which handed a normal outdoor easy run a D on heart rate. That
was an artifact of mixing two HR regimes, not a judgment — treadmill and road
are different modalities and must not share a yardstick unless forced to.

Comparability is ALSO gated on locomotion, measured rather than labelled, because
``activity_type`` is not trustworthy: a walking-desk session logs as
``treadmill_running``, and both the exact-type filter and ``plans._is_running``
(a substring match on "running") pass it straight through. See
``RUN_PACE_CEILING_SEC_PER_MI`` — on live data this was distorting 40% of the
composite.

Direction gating is what keeps the rubric honest. An easy run is *supposed*
to be slow — grading |actual − expected| would hand every recovery run an F.
So easy/long days are penalized only for running too FAST, quality days only
for running too SLOW, and each metric's expectation is scaled by the workout's
intent (from the plan when present, inferred otherwise).

Splits are presentation-only, with exactly one documented exception. No grade
reads ``activity_splits`` — only 87 of 747 activities have them (they are
written by the daily-sync ingest path, never by backfill), so a splits-dependent
grade would be unavailable on ~88% of the history and would silently mean
different things on different rows.

The exception is **quality-day pace against a prescribed rep target**, and it
exists because the alternative was not a strict grade but a broken one: a plan's
interval pace describes the reps, while ``avg_pace_sec_per_km`` averages in the
warmup, the recovery jogs and the cooldown, so that comparison returns F for
every correctly-executed interval session. The fastest full split is the only
available number that can answer "did you hit the reps". Where splits are
missing the metric returns n/a with a stated reason and its weight
redistributes — it never falls back to the average-vs-rep comparison.

The pure section below is import-light (stdlib + ``render``/``units``) and
unit-testable with plain dicts; DB access lives under the persistence divider,
mirroring how ``plans.py`` is organized.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date as _date
from datetime import timedelta
from statistics import median

from .. import plans
from . import interpret, render, units

__all__ = [
    "grade_from_deviation", "intent_class", "infer_intent", "resolve_intent",
    "distance_deviation", "pace_deviation", "hr_deviation", "load_deviation",
    "overall_grade", "label_splits", "hr_drift_pct", "build_card",
    "render_markdown", "reference_line", "bin_hr_trace", "expected_text",
    "actual_text", "hr_band_bounds", "hr_expectation",
    "is_running_effort", "fastest_rep_split", "fastest_rep_split_pace",
    "READ_SECTIONS",
    "load_report_card_inputs", "rolling_reference",
]

# 1 international mile, exactly — units.py owns the constant (0.38.0);
# the local name survives because the module uses it ~everywhere.
MILE_M = units.METERS_PER_MILE
# How close a lap must be to a mile before we call the column "Mile" rather
# than "Lap". Garmin auto-lap lands within a few meters; a manual-lap workout
# can be anything, and mislabeling laps as miles is a lie on the card.
MILE_TOLERANCE = 0.03
# Below this many comparable activities in the window, the median is noise.
# Grading against noise is worse than not grading — return n/a and say so.
MIN_REFERENCE_ACTIVITIES = 5
# The smallest split a quality-day pace grade will read. Deliberately NOT the
# `partial` flag: that is relative to the workout's OWN longest lap, so a
# manually-lapped interval session (2-mile warmup, then 800m reps) marks every
# rep partial and leaves the warmup as the only "full" split — which graded the
# reps at warmup pace and guaranteed an F on exactly the workouts the splits
# exception exists to grade fairly. 300 m sits under a standard 400 m rep and
# well over any trailing GPS fragment.
QUALITY_MIN_SPLIT_M = 300.0
REFERENCE_WINDOW_DAYS = 60
# The run/walk boundary and its classifier moved to ``interpret.py`` (the pure
# classifier module) on 2026-07-22, so ``plans.py`` can gate its mileage rollup
# on the same function without a ``plans -> report_card -> plans`` import cycle.
# Re-exported here because both names are in this module's ``__all__`` and are
# referenced by tests and by ``rolling_reference`` below. See interpret.py for
# the measured bimodal distribution that sets the 13:00 ceiling.
RUN_PACE_CEILING_SEC_PER_MI = interpret.RUN_PACE_CEILING_SEC_PER_MI
is_running_effort = interpret.is_running_effort
# The same boundary, converted once to sec/km — the unit `pace_deviation` and
# the plan-reference gate below actually compare against. Mirrors the
# conversion `plans.py`'s `best_recent_effort` already does at its own call
# site (`interpret.RUN_PACE_CEILING_SEC_PER_MI / interpret._KM_PER_MILE`), so
# the two can't drift apart.
_RUN_PACE_CEILING_SEC_PER_KM = interpret.RUN_PACE_CEILING_SEC_PER_MI / interpret._KM_PER_MILE
# plan_workouts.type values that assert the workout IS a run. Used to decide
# when a walk-effort activity must refuse its plan reference (see build_card):
# "cross" is explicitly non-running cross-training and "rest" has no
# reference to refuse at all (see plan_usable), so neither belongs here.
_RUNNING_PLAN_TYPES = frozenset({"easy", "long", "tempo", "interval", "race"})
# Advisory only: a 2x-median load day is a fact worth printing, not an F.
LOAD_SPIKE_FACTOR = 2.0
# Bucket width for the per-sample HR trace chart. A tenth of a mile is fine
# enough to show where a run actually turned over, coarse enough that a
# 3-mile run yields ~31 readable bars rather than 1700.
HR_TRACE_BIN_MI = 0.1
# How far either side of the graded day the coaching read gets to look. Enough
# to say "your third run this week" and "you have intervals Thursday" without
# handing the model a training log to summarize.
RECENT_WINDOW_DAYS = 14
UPCOMING_WINDOW_DAYS = 7
# The read is told not to narrate the calendar, so it does not need the whole
# window enumerated — a handful either side is enough to place the run, and
# every extra row is prompt the model pays to read.
MAX_CONTEXT_ACTIVITIES = 5

# Every `activities` column EXCEPT `raw_json` — the whole-row read list, and the
# single definition of it (`tools.get_workout_detail` imports this one rather
# than keeping a second copy). `raw_json` is the preserved Garmin payload, ~50 KB
# per row, and nothing downstream of a whole-row fetch reads it: both call sites
# used to `SELECT *` and then pop the key back off, which is 50 KB decoded out of
# SQLite and thrown away per activity. `source` is here because init_schema's
# guarded ALTER guarantees it (see db.init_schema) — a DB that skipped
# init_schema is out of contract for every other table too.
# tests/test_report_card.py pins this against PRAGMA table_info, so a new column
# in db.SCHEMA fails the build instead of silently going unread.
_ACTIVITY_COLUMNS: tuple[str, ...] = (
    "activity_id", "date", "start_time", "activity_type", "activity_name",
    "duration_seconds", "moving_seconds", "distance_meters", "avg_hr",
    "max_hr", "avg_pace_sec_per_km", "elevation_gain_meters",
    "elevation_loss_meters", "calories", "aerobic_te", "anaerobic_te",
    "training_load", "avg_cadence", "vo2_max_estimate", "weather_temp_c",
    "weather_conditions", "source",
)
# Interpolated into SQL, never parameterized — these are frozen identifiers from
# the constant above, not user input (the whitelist-not-f-string rule).
_ACTIVITY_SELECT = ", ".join(_ACTIVITY_COLUMNS)

# The one band table. `d` is a non-negative relative deviation; every metric
# reduces to one, which is why there is exactly one grader.
GRADE_BANDS: tuple[tuple[float, str], ...] = (
    (0.05, "A"), (0.10, "B"), (0.20, "C"), (0.35, "D"),
)
GRADE_POINTS = {"A": 4.0, "B": 3.0, "C": 2.0, "D": 1.0, "F": 0.0}
# Weights are per intent class, because the metric a workout exists to satisfy
# differs by workout. Running easy IS the easy day; hitting rep pace IS the
# quality day; covering the distance IS the long run.
#
# Flat weights (the previous distance .30 / pace .30 / hr .25 / load .15 for
# every intent) let the two lowest-information metrics outvote the point of the
# session. Measured 2026-07-21: a prescribed 10:28 easy run executed at 9:28 —
# a full minute per mile too hot, which is the *only* way an easy day fails —
# scored an overall B (3.40), because HR and load together carried 40% and both
# landed A. The card's own read called it "not recovery, that's a race finish".
METRIC_WEIGHTS = {"distance": 0.30, "pace": 0.30, "hr": 0.25, "load": 0.15}
INTENT_METRIC_WEIGHTS: dict[str, dict[str, float]] = {
    "easy": {"distance": 0.20, "pace": 0.45, "hr": 0.25, "load": 0.10},
    "quality": {"distance": 0.20, "pace": 0.45, "hr": 0.25, "load": 0.10},
    "long": {"distance": 0.45, "pace": 0.20, "hr": 0.20, "load": 0.15},
    # No stated intent means no metric can claim to be the point of the day —
    # keep the neutral split.
    "steady": METRIC_WEIGHTS,
}
_GPA_CUTS: tuple[tuple[float, str], ...] = (
    (3.5, "A"), (2.5, "B"), (1.5, "C"), (0.5, "D"),
)
# An F anywhere caps the overall here. A card that prints "Overall: A" above a
# row reading F is not reporting a grade, it is averaging away the finding.
_F_FLOOR_GRADE = "C"

# Intent scaling. An easy day is EXPECTED to be shorter and slower than the
# median; a long day longer. Applied to the rolling reference only — a plan
# states its own targets and needs no scaling.
DISTANCE_FACTORS = {"easy": 0.75, "long": 1.40, "quality": 1.00, "steady": 1.00}
# Training load tracks distance closely enough at a fixed intent that a second,
# independently-tuned table would be false precision — deliberately the same
# numbers, named separately so they can diverge if evidence ever says they should.
LOAD_FACTORS = dict(DISTANCE_FACTORS)
PACE_FACTORS = {"easy": 1.10, "long": 1.05, "quality": 0.95, "steady": 1.00}
# (floor, ceiling) as fractions of the rolling median HR; None = unbounded on
# that side. HR is graded on appropriateness to intent, never "lower is better".
#
# Recalibrated 2026-07-20 against live data, because the original values were
# not reachable. The reference median is taken over ALL comparable activities,
# which for a runner whose training is mostly easy is itself close to easy HR —
# so demanding 12% below it (the old easy ceiling of 0.88) asked for a number
# that appeared in 1 of 13 runs in the window, and only in the one whose HR
# looks like a sensor fault. Every ordinary easy run was therefore marked "too
# hot" and the HR grade was a standing penalty rather than a judgment.
#
# These express the defensible generic claim instead: against a mixed-intent
# median, an easy run should sit a little below it, a long run about at it, and
# a quality run at or above it.
#
# Re-verified 2026-07-21 after RUN_PACE_CEILING_SEC_PER_MI cleaned the pools,
# and deliberately left unchanged. The 0.97 easy ceiling was tuned against the
# `running` (outdoor) distribution, which was never contaminated — its 60-day
# median HR is 144.5. Excluding walking-pad sessions moves the
# `treadmill_running` median from 116 to 145, so the two pools now agree and the
# same constants describe both. At a 145 median the easy ceiling is 141 bpm
# (reachable: 4 of 12 outdoor runs in the window sit at or below it) and the
# quality floor is 145 bpm (reachable: 8 of 16 treadmill runs clear it). Both
# bounds are attainable rather than aspirational, which is the property the
# 2026-07-20 recalibration was after.
HR_BANDS: dict[str, tuple[float | None, float | None]] = {
    "easy": (None, 0.97),
    "long": (None, 1.00),
    "quality": (1.00, None),
    "steady": (0.93, 1.07),
}
# A steady/unknown-intent run has no stated target, so its two-sided pace
# bands are widened rather than held to prescription-grade tolerance.
STEADY_WIDEN = 1.5
# ...and the converse: a PLAN target is an explicit instruction, not a fuzzy
# reference, so it is held tighter. Without this the two were graded on the
# same scale, and a prescribed 10:28 easy run executed at 9:28 — a full minute
# per mile fast, which is the entire failure mode an easy day has — scored a
# B- and let the card hand an overall A to a run its own coaching read called
# "you never ran easy at all". Bands become A<=3%, B<=6%, C<=12%, D<=21%.
PLAN_TIGHTEN = 0.6

# plan_workouts.type (plans.WORKOUT_TYPES) and inferred intents, collapsed to
# the four classes the factor/band tables are keyed by.
_INTENT_CLASSES = {
    "easy": "easy", "recovery": "easy",
    "long": "long",
    "tempo": "quality", "interval": "quality", "race": "quality",
    "quality": "quality",
}
# Names that override inference outright — "recovery shakeout" is an easy run
# no matter what its HR looked like.
_EASY_NAME_RE = re.compile(r"recovery|easy|shake ?out", re.IGNORECASE)

_HR_EASY_RATIO = 0.92
_HR_QUALITY_RATIO = 1.06
_LONG_DISTANCE_RATIO = 1.40

# Float slop: d == 0.05 must land in the A band, not B.
_EPS = 1e-9


# --- grading primitives ----------------------------------------------------

def grade_from_deviation(d: float | None, widen: float = 1.0) -> str | None:
    """Relative deviation → letter grade, with a +/- modifier for position
    within the band. ``None`` in, ``None`` out (an ungradeable metric).

    ``widen`` scales every band boundary — used for steady/unknown-intent
    pace, which has no stated target to be held tightly against.
    """
    if d is None:
        return None
    d = max(0.0, float(d))
    lo = 0.0
    for threshold, letter in GRADE_BANDS:
        hi = threshold * widen
        if d <= hi + _EPS:
            return letter + _modifier(d, lo, hi)
        lo = hi
    return "F"


def _modifier(d: float, lo: float, hi: float) -> str:
    """Position within a band → "+", "" or "-". Lower deviation is better, so
    the bottom third of the band earns the "+"."""
    span = hi - lo
    if span <= 0:
        return ""
    p = (d - lo) / span
    if p < 1 / 3:
        return "+"
    if p < 2 / 3:
        return ""
    return "-"


def base_letter(grade: str | None) -> str | None:
    """Strip the +/- modifier. GPA math runs on base letters so the weights
    stay the approved ones and a modifier can never move an overall grade."""
    return grade[0] if grade else None


def intent_class(intent: str | None) -> str:
    """Plan type or inferred intent → one of easy | long | quality | steady."""
    return _INTENT_CLASSES.get((intent or "").lower(), "steady")


def infer_intent(activity: dict, reference: dict) -> str:
    """Classify a workout's intent from its own numbers when no plan says so.

    Order matters: distance is checked before HR because a long run is
    typically run at easy HR, and classifying it "easy" would grade it against
    a 0.75x-median distance expectation — an automatic A on a metric that
    should be asking whether the long run was actually long.
    """
    if _EASY_NAME_RE.search(activity.get("activity_name") or ""):
        return "easy"
    hr = activity.get("avg_hr")
    med_hr = reference.get("median_hr")
    dist = activity.get("distance_meters")
    med_dist = reference.get("median_distance_m")
    if dist and med_dist and dist >= _LONG_DISTANCE_RATIO * med_dist:
        return "long"
    if hr and med_hr:
        if hr <= _HR_EASY_RATIO * med_hr:
            return "easy"
        if hr >= _HR_QUALITY_RATIO * med_hr:
            return "quality"
    return "steady"


def resolve_intent(activity: dict, plan_workout: dict | None, reference: dict) -> tuple[str, str]:
    """(intent, source) — the plan's prescribed type when there is one, else
    inference. Surfaced on the card so an easy-run yardstick is visible rather
    than quietly applied."""
    if plan_workout and plan_workout.get("type"):
        return plan_workout["type"], "plan"
    return infer_intent(activity, reference), "inferred"


# --- per-metric deviations -------------------------------------------------

def distance_deviation(
    actual_m: float | None, expected_m: float | None, *, two_sided: bool
) -> float | None:
    """Distance deviation. Two-sided against a plan (a 12-miler on a 10-mile
    prescription is over-cooking the plan and costs you); one-sided-low against
    the rolling median (going longer than your norm is never a penalty)."""
    if not actual_m or not expected_m or expected_m <= 0:
        return None
    ratio = actual_m / expected_m
    return abs(ratio - 1.0) if two_sided else max(0.0, 1.0 - ratio)


def pace_deviation(
    actual_sec_per_km: float | None, expected_sec_per_km: float | None, cls: str
) -> float | None:
    """Pace deviation, direction-gated by intent. Pace is sec/km — LOWER is
    faster.

    - easy / long: penalized only for running too FAST. Slower than the easy
      expectation is the entire point of an easy run and scores an A. This is
      the rule that stops recovery runs from failing. BUT there is a slow-side
      floor anchored on ``RUN_PACE_CEILING_SEC_PER_MI``, the measured run/walk
      boundary: past it the activity is a walk, not a slow run, and a 0.0
      deviation there is what let a walking-desk session (83:49/mi, one
      measured case) read a perfect A+ pace against an "easy" expectation.
      The floor is anchored on the boundary itself, not on the (often much
      faster) plan/rolling expectation, so it fires only once the actual pace
      genuinely crosses into walk territory — a legitimate slow-but-real easy
      run (anything up to a 13:00 mile) is untouched, which is what keeps this
      from over-correcting the headline recovery-run case above.
    - quality (tempo/interval/race): penalized only for being too SLOW.
      Beating the target is an A, uncapped — but the SAME slow-side floor
      applies. A walked "tempo"/"interval" day that refuses its plan pace
      reference (see ``build_card``) falls to the rolling reference, which
      for a walk is itself a *walking*-pool median — and quality's
      slow-only gate scores 0.0 whenever the walk happens to be brisker than
      that (often very slow) walking median. Measured: a 15:20/mi walk on a
      prescribed tempo day scored A+ pace this way even after the plan-gate
      fix, because nothing here checked whether "not slow" also meant "not a
      walk". The floor closes that gap the same way it does for easy/long.
    - steady / unknown: two-sided, with bands widened by ``STEADY_WIDEN`` at
      the grading step. Not floored — the two-sided formula already
      penalizes a walk-paced steady day relative to its own expectation, so a
      walk cannot quietly clear it.
    """
    if not actual_sec_per_km or not expected_sec_per_km or expected_sec_per_km <= 0:
        return None
    walked = max(
        0.0,
        (actual_sec_per_km - _RUN_PACE_CEILING_SEC_PER_KM) / _RUN_PACE_CEILING_SEC_PER_KM,
    )
    if cls in ("easy", "long"):
        fast = max(0.0, (expected_sec_per_km - actual_sec_per_km) / expected_sec_per_km)
        return max(fast, walked)
    if cls == "quality":
        slow = max(0.0, (actual_sec_per_km - expected_sec_per_km) / expected_sec_per_km)
        return max(slow, walked)
    return abs(actual_sec_per_km / expected_sec_per_km - 1.0)


def hr_band_bounds(
    median_hr: float | None, cls: str
) -> tuple[float | None, float | None]:
    """The intent's HR band in BPM, as ``(floor, ceiling)``; either may be None.

    Exists so the card can state the number it actually graded against. HR is
    the one metric judged against a *range* rather than a point, and displaying
    the bare median as "expected" made the row contradict itself: an easy run
    at 136 against a 146 median rendered as "-7%" next to a B+, when the real
    finding was 6% ABOVE the 128 ceiling that produced the grade.
    """
    if not median_hr:
        return (None, None)
    floor_f, ceiling_f = HR_BANDS.get(cls, HR_BANDS["steady"])
    return (
        floor_f * median_hr if floor_f is not None else None,
        ceiling_f * median_hr if ceiling_f is not None else None,
    )


def hr_expectation(hr: float | None, median_hr: float | None, cls: str) -> float | None:
    """The single bound the HR grade was measured against — the ceiling when
    the run ran hot, the floor when it ran cold, and the bound it is being held
    to when it sat inside the band.

    Returning the *governing* bound (rather than the median) is what lets
    ``_delta_text`` state a delta the grade can be checked against.
    """
    lo, hi = hr_band_bounds(median_hr, cls)
    if hr is None:
        return hi if hi is not None else lo
    if hi is not None and hr > hi:
        return hi
    if lo is not None and hr < lo:
        return lo
    # Inside the band: report the edge it is closest to being judged on.
    return hi if hi is not None else lo


def hr_deviation(hr: float | None, median_hr: float | None, cls: str) -> float | None:
    """How far outside the intent's HR band the run sat, as a fraction of the
    band edge. Inside the band is 0.0 (an A) — HR is graded on appropriateness,
    so an easy run at easy HR is perfect, not merely "low"."""
    if not hr or not median_hr:
        return None
    floor_f, ceiling_f = HR_BANDS.get(cls, HR_BANDS["steady"])
    ratio = hr / median_hr
    if ceiling_f is not None and ratio > ceiling_f:
        return (ratio - ceiling_f) / ceiling_f
    if floor_f is not None and ratio < floor_f:
        return (floor_f - ratio) / floor_f
    return 0.0


def load_deviation(load: float | None, expected_load: float | None) -> float | None:
    """Deviation from the intent-scaled rolling median, penalized in BOTH
    directions — but only once an overshoot becomes a spike.

    Undershooting costs you. Overshooting is free up to ``LOAD_SPIKE_FACTOR``,
    because a big day is not a failure; past it, the penalty grows with the
    excess.

    That ceiling exists because the one-sided-low version contradicted the card
    it printed on. Measured 2026-07-21: a day at 81 load against a 22
    expectation scored **A+** on the row directly above the note "Training Load:
    **spike** — more than double your median day", while the coaching read
    called it "stacking debt before the week's hardest ask". A grade that means
    "this is a red flag" is not a grade. The threshold is deliberately the same
    constant the flag uses, so the letter and the prose can never disagree
    again.

    ``expected_load`` is intent-scaled by the caller for the same reason pace
    is direction-gated: an easy day is SUPPOSED to bank less load, and grading
    it against the unscaled median handed a prescribed 3-mile recovery run a D
    on live data (2026-07-19). ``plan_workouts`` has no load column, so this
    metric never has a plan reference to use.
    """
    if not load or not expected_load or expected_load <= 0:
        return None
    ratio = load / expected_load
    if ratio > LOAD_SPIKE_FACTOR:
        # Measured from the spike threshold, not from the expectation: landing
        # exactly on the threshold is still a clean A.
        return (ratio - LOAD_SPIKE_FACTOR) / LOAD_SPIKE_FACTOR
    return max(0.0, 1.0 - ratio)


def overall_grade(metrics: dict[str, dict], cls: str = "steady") -> dict:
    """Intent-weighted GPA over gradeable metrics only.

    ``cls`` selects the weight table — see ``INTENT_METRIC_WEIGHTS`` for why the
    weights aren't flat. It defaults to the neutral split so an older caller
    passing only ``metrics`` keeps the previous behavior.

    An ``n/a`` metric drops out and its weight redistributes proportionally,
    so a by-feel plan day with no pace target isn't silently scored as if pace
    were worth 30% of nothing. Zero gradeable metrics yields "n/a" — never
    "F", which would read as a judgment we did not actually make.

    Finally, an F on any single metric caps the overall at ``_F_FLOOR_GRADE``.
    Redistribution plus generous weighting can otherwise let three good metrics
    average an outright failure up into a passing letter.
    """
    weights = INTENT_METRIC_WEIGHTS.get(cls, METRIC_WEIGHTS)
    pairs = [
        (weights[k], GRADE_POINTS[base_letter(m["grade"])])
        for k, m in metrics.items()
        if m.get("grade") and k in weights
    ]
    if not pairs:
        return {"grade": "n/a", "gpa": None, "graded_metrics": 0}
    total_w = sum(w for w, _ in pairs)
    gpa = sum(w * p for w, p in pairs) / total_w
    letter = "F"
    for cut, candidate in _GPA_CUTS:
        if gpa >= cut:
            letter = candidate
            break
    out = {"grade": letter, "gpa": round(gpa, 2), "graded_metrics": len(pairs)}
    if any(base_letter(m.get("grade")) == "F" for m in metrics.values()):
        # Report the cap rather than quietly rewriting the letter — the GPA
        # stays honest and the card can say why the two disagree.
        if GRADE_POINTS[letter] > GRADE_POINTS[_F_FLOOR_GRADE]:
            out["grade"] = _F_FLOOR_GRADE
            out["capped_by"] = "F"
    return out


# --- splits (presentation only — no grade reads these) ---------------------

def label_splits(splits: list[dict]) -> dict:
    """Normalize raw ``activity_splits`` rows into display rows.

    ``split_index`` is 0-based in the DB, so it is displayed +1. Laps are
    called "Mile" only when the full-size ones actually are miles; otherwise
    "Lap". A trailing partial lap (Garmin always emits one) is shown but
    flagged ``partial`` so it stays out of the drift math, where a 90-meter
    fragment's HR would be noise.
    """
    if not splits:
        return {"available": False, "unit": "Lap", "rows": [], "hr_drift_pct": None}

    dists = [s.get("distance_meters") or 0.0 for s in splits]
    full = max(dists) if dists else 0.0
    rows: list[dict] = []
    for s in splits:
        d = s.get("distance_meters") or 0.0
        rows.append({
            "index": (s.get("split_index") or 0) + 1,
            "distance_meters": d,
            "distance_mi": units.to_miles(d),
            "duration_seconds": s.get("duration_seconds"),
            "avg_hr": s.get("avg_hr"),
            "avg_pace_sec_per_km": s.get("avg_pace_sec_per_km"),
            "pace_min_per_mi": units.format_pace_min_per_mi(s.get("avg_pace_sec_per_km")),
            "elevation_gain_meters": s.get("elevation_gain_meters"),
            # "partial" is relative to the workout's OWN full-lap size, not to
            # a mile — a 1 km-lap workout has partials too.
            "partial": bool(full) and d < full * (1 - MILE_TOLERANCE),
        })

    mile_like = [r for r in rows if not r["partial"]]
    unit = "Lap"
    if mile_like and all(
        abs(r["distance_meters"] - MILE_M) / MILE_M <= MILE_TOLERANCE for r in mile_like
    ):
        unit = "Mile"
    return {
        "available": True,
        "unit": unit,
        "rows": rows,
        "hr_drift_pct": hr_drift_pct(mile_like),
    }


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

    This is the one place a *grade* is allowed to read splits — see the quality
    branch of ``build_card`` for why, and the module docstring for the rule it
    is an exception to.
    """
    candidates = [r for r in (labelled.get("rows") or [])
                  if (r.get("distance_meters") or 0.0) >= QUALITY_MIN_SPLIT_M
                  and r.get("avg_pace_sec_per_km")]
    return min(candidates, key=lambda r: r["avg_pace_sec_per_km"]) if candidates else None


def fastest_rep_split_pace(labelled: dict) -> float | None:
    """:func:`fastest_rep_split`'s pace in sec/km, or ``None``."""
    best = fastest_rep_split(labelled)
    return best["avg_pace_sec_per_km"] if best else None


def bin_hr_trace(
    samples: list[tuple[float, int]], bin_mi: float = HR_TRACE_BIN_MI
) -> list[dict]:
    """Average HR per fixed distance bucket, from a cumulative-distance trace.

    `samples` is `(cumulative_distance_m, hr)` in order, as
    `ingest.details.parse_hr_samples` returns it. Each sample lands in the
    bucket its distance falls in and every bucket reports the MEAN of its
    samples — not the last value, which would turn the chart into a sampling
    artifact rather than a summary.

    Sampling is time-based (roughly every few seconds), so a bucket's sample
    count varies with pace: a slow tenth collects more samples than a fast one.
    That is correct — each bucket answers "what was my HR over this stretch of
    ground", and a stretch you spent longer on legitimately has more evidence.

    The trailing bucket is almost always short (a 3.06-mile run ends 0.06 into
    its 31st tenth). It is kept and flagged `partial`, the same convention
    `label_splits` uses, so the renderer can de-emphasize it instead of
    presenting a fragment as a full interval.
    """
    if not samples or bin_mi <= 0:
        return []

    bin_m = bin_mi * MILE_M
    buckets: dict[int, list] = {}
    for sample in samples:
        distance_m = sample[0]
        if distance_m is None or distance_m < 0:
            continue
        buckets.setdefault(int(distance_m // bin_m), []).append(sample)
    if not buckets:
        return []

    total_m = max(s[0] for s in samples)
    last = max(buckets)
    rows: list[dict] = []
    for idx in range(last + 1):
        bucket = buckets.get(idx)
        if not bucket:
            # A gap (GPS dropout, paused watch) — skip rather than plot a zero.
            continue
        start_mi = idx * bin_mi
        # The final bucket ends where the activity does, not at a full bin.
        end_mi = min((idx + 1) * bin_mi, total_m / MILE_M)
        rows.append({
            "index": idx + 1,
            "start_mi": round(start_mi, 3),
            "end_mi": round(end_mi, 3),
            "avg_hr": round(sum(s[1] for s in bucket) / len(bucket)),
            "pace_sec_per_mi": _bucket_pace(bucket),
            "samples": len(bucket),
            "partial": (end_mi - start_mi) < bin_mi * (1 - MILE_TOLERANCE),
        })
    return rows


def _bucket_pace(bucket: list) -> float | None:
    """Seconds per mile across one distance bucket, or None when the trace
    carries no usable time.

    Measured as elapsed-time-over-ground-covered between the bucket's first and
    last sample — NOT an average of instantaneous speeds, which would weight a
    stopped second the same as a moving one. Sampling is time-based and dense
    (~56 samples per tenth of a mile on a real run), so first-to-last spans
    essentially the whole bucket.

    Returns None for a bucket that can't support the arithmetic: a single
    sample, no duration channel, or a non-advancing clock or odometer. A
    missing point is a gap in the pace line, which is honest; a fabricated one
    is a lie about how fast he ran.
    """
    timed = [s for s in bucket if len(s) > 2 and s[2] is not None]
    if len(timed) < 2:
        return None
    d_m = timed[-1][0] - timed[0][0]
    d_t = timed[-1][2] - timed[0][2]
    if d_m <= 0 or d_t <= 0:
        return None
    return (d_t / d_m) * MILE_M


def hr_drift_pct(full_rows: list[dict]) -> float | None:
    """Back-half mean HR vs front-half mean HR, as a percentage. Displayed as
    context, never graded — cardiac drift is expected on a long run and is not
    by itself good or bad."""
    hrs = [r["avg_hr"] for r in full_rows if r.get("avg_hr")]
    if len(hrs) < 4:
        return None
    half = len(hrs) // 2
    front = sum(hrs[:half]) / half
    back = sum(hrs[-half:]) / half
    if not front:
        return None
    return round((back - front) / front * 100, 1)


# --- card assembly ---------------------------------------------------------

def _metric(grade, actual, expected, deviation, ref, note=None) -> dict:
    out = {
        "grade": grade, "actual": actual, "expected": expected,
        "deviation": None if deviation is None else round(deviation, 4),
        "reference": ref,
    }
    if note:
        out["note"] = note
    return out


def build_card(
    activity: dict,
    splits: list[dict],
    plan_workout: dict | None,
    reference: dict,
    context: dict | None = None,
    hr_samples: list[tuple[float, int]] | None = None,
    recent_activities: list[dict] | None = None,
    upcoming_workouts: list[dict] | None = None,
) -> dict:
    """Assemble the full report card. Pure — every input is a plain dict, so
    the whole rubric is testable without a DB.

    ``hr_samples`` is the optional per-sample ``(distance_m, hr)`` trace. Like
    ``splits`` it is presentation-only: no grade reads it, so a card renders
    identically whether or not the trace was available.
    """
    intent, intent_source = resolve_intent(activity, plan_workout, reference)
    cls = intent_class(intent)
    has_rolling = reference.get("mode") == "rolling_60d"
    # Hoisted: the quality-pace branch below needs the normalized splits, and
    # labelling them twice would be the only alternative.
    labelled_splits = label_splits(splits)
    # A rest-day prescription carries null targets; it is an intent signal
    # only, so distance/pace fall through to the rolling reference.
    plan_usable = bool(plan_workout) and (plan_workout or {}).get("type") != "rest"

    # Locomotion mode of the activity actually being graded, resolved ONCE —
    # measured via pace (`interpret.is_running_effort`), never `activity_type`
    # (Garmin's label lies; see the module docstring). `None` means the row
    # has no usable pace and its mode is genuinely unknown, so it keeps
    # today's behavior rather than being forced to one side.
    mode = is_running_effort(activity.get("avg_pace_sec_per_km"))
    # A walk against a RUNNING plan type must not be graded against that
    # plan's target: a 4-miler walked at 14:09/mi against a 9:39/mi easy
    # prescription is not "over 4x too fast" (the plan target math would say
    # so), it's a different activity than the one prescribed. Scoped to
    # running plan types only — "cross" is deliberately non-running and
    # "rest" has no reference to refuse (`plan_usable` already excludes it).
    plan_walk_mismatch = (
        mode is False and plan_usable
        and (plan_workout or {}).get("type") in _RUNNING_PLAN_TYPES
    )
    plan_ref_ok = plan_usable and not plan_walk_mismatch
    walk_note = None
    if plan_walk_mismatch:
        walk_note = (
            f"measured pace says this was walked, not run — a prescribed "
            f"'{plan_workout['type']}' target doesn't apply to a walk effort; "
            "graded against your rolling reference instead"
        )

    # -- distance
    if plan_ref_ok and plan_workout.get("target_distance_m"):
        target = plan_workout["target_distance_m"]
        d = distance_deviation(activity.get("distance_meters"), target, two_sided=True)
        distance = _metric(
            grade_from_deviation(d, PLAN_TIGHTEN), activity.get("distance_meters"),
            target, d, "plan")
    elif has_rolling:
        expected = DISTANCE_FACTORS[cls] * (reference.get("median_distance_m") or 0)
        d = distance_deviation(activity.get("distance_meters"), expected, two_sided=False)
        distance = _metric(
            grade_from_deviation(d), activity.get("distance_meters"), expected or None,
            d, "rolling_60d", note=walk_note)
    else:
        distance = _metric(
            None, activity.get("distance_meters"), None, None, reference.get("mode"),
            note=walk_note)

    # -- pace
    avg_pace = activity.get("avg_pace_sec_per_km")
    # What gets graded. Normally the run average; on a quality day with a
    # prescribed rep pace it is the fastest split instead (see below).
    graded_pace, pace_note, pace_display = avg_pace, None, None
    if plan_ref_ok and plan_workout.get("target_pace_sec_per_km"):
        expected_pace = plan_workout["target_pace_sec_per_km"]
        pace_ref, widen = "plan", PLAN_TIGHTEN
        if cls == "quality":
            # A quality day's plan pace is a REP target, but avg_pace_sec_per_km
            # is a whole-run average that bakes in the warmup, the recovery jogs
            # and the cooldown. Grading one against the other isn't a hard
            # rubric, it's an arithmetic guarantee of an F — every correctly
            # executed interval session averages far slower than its rep pace.
            # Measured 2026-07-21: a prescribed 6:58/mi interval day averaged
            # 10:42/mi and scored F, while its 4th mile ran 9:25 at 164 bpm.
            #
            # The fastest rep-sized split is the only number available that can
            # answer "did you hit the reps", so quality pace — and ONLY quality
            # pace — reads splits. Everything else keeps the no-splits rule.
            best_split = fastest_rep_split(labelled_splits)
            graded_pace = best_split["avg_pace_sec_per_km"] if best_split else None
            if graded_pace is None:
                # Backfilled activities carry no splits at all. Show the average
                # so the number isn't lost, but refuse to grade it against a rep
                # target — pace's weight redistributes instead.
                pace_display = f"{_fmt_pace(avg_pace)} avg" if avg_pace else None
                reason = ("no splits recorded" if not labelled_splits["available"]
                          else "no split long enough to be a rep")
                pace_note = (f"interval day, {reason} — average pace "
                             "can't be graded against a rep target")
            else:
                # No note in the success case: "9:25/mi best mile" against a
                # "6:58/mi" target already states exactly what was compared, and
                # the PDF's one-page budget is real — this bullet alone pushed a
                # 6-split card onto a second page. A note earns its row only
                # when the reader could not otherwise infer the reason.
                # A rep shorter than the workout's full lap is not a "best
                # mile", and calling it one on a manual-lap card is the kind of
                # small lie the card is not allowed to tell.
                unit = ("split" if best_split.get("partial")
                        else labelled_splits["unit"].lower())
                pace_display = f"{_fmt_pace(graded_pace)} best {unit}"
    elif plan_ref_ok and plan_workout.get("target_distance_m"):
        # A prescribed distance with no pace is an explicit by-feel day. It
        # earns no pace grade at all, and its 30% weight redistributes.
        expected_pace, pace_ref, widen = None, "plan (by feel)", 1.0
    elif has_rolling and reference.get("median_pace_sec_per_km"):
        expected_pace = PACE_FACTORS[cls] * reference["median_pace_sec_per_km"]
        pace_ref = "rolling_60d"
        widen = STEADY_WIDEN if cls == "steady" else 1.0
    else:
        expected_pace, pace_ref, widen = None, reference.get("mode"), 1.0
    if pace_ref == "plan (by feel)":
        pace_note = "by-feel day — no pace target"
    elif plan_walk_mismatch:
        pace_note = walk_note
    d = pace_deviation(graded_pace, expected_pace, cls)
    pace = _metric(
        grade_from_deviation(d, widen),
        # `actual` stays the number the grade was measured against, so the Delta
        # column can never compare two different quantities. When that isn't the
        # run average, `actual_display` says which number it is.
        graded_pace if graded_pace is not None else avg_pace,
        expected_pace, d, pace_ref, note=pace_note)
    if pace_display:
        pace["actual_display"] = pace_display

    # -- HR and load: always rolling. plan_workouts has neither column.
    med_hr = reference.get("median_hr") if has_rolling else None
    actual_hr = activity.get("avg_hr")
    d = hr_deviation(actual_hr, med_hr, cls)
    # Expected is the BOUND that governed the grade, not the median — see
    # hr_expectation. The band itself is carried for display so the reader can
    # see the range rather than infer it from one edge.
    hr_lo, hr_hi = hr_band_bounds(med_hr, cls)
    hr = _metric(
        grade_from_deviation(d), actual_hr,
        hr_expectation(actual_hr, med_hr, cls), d,
        "rolling_60d" if has_rolling else reference.get("mode"))
    if med_hr:
        hr["band"] = {"floor": hr_lo, "ceiling": hr_hi}
        hr["in_band"] = d == 0.0
        hr["median_hr"] = med_hr
        hr["expected_display"] = _fmt_hr_band(hr_lo, hr_hi)

    med_load = reference.get("median_load") if has_rolling else None
    expected_load = LOAD_FACTORS[cls] * med_load if med_load else None
    d = load_deviation(activity.get("training_load"), expected_load)
    load = _metric(
        grade_from_deviation(d), activity.get("training_load"), expected_load, d,
        "rolling_60d" if has_rolling else reference.get("mode"))
    actual_load = activity.get("training_load")
    # The spike flag compares against the UNSCALED median: "double your normal
    # day" is the fact worth printing, regardless of what the day intended.
    if actual_load and med_load and actual_load > LOAD_SPIKE_FACTOR * med_load:
        load["spike"] = True

    metrics = {"distance": distance, "pace": pace, "hr": hr, "load": load}
    return {
        "activity": dict(activity),
        "intent": intent,
        "intent_source": intent_source,
        "intent_class": cls,
        "reference": reference,
        "plan_workout": plan_workout,
        "metrics": metrics,
        "overall": overall_grade(metrics, cls),
        "splits": labelled_splits,
        "hr_trace": bin_hr_trace(hr_samples or []),
        # Prompt-only, like splits and the trace: no grade reads either, so a
        # card grades identically with or without them.
        "recent_activities": recent_activities or [],
        "upcoming_workouts": upcoming_workouts or [],
        "context": context or {},
    }


# --- markdown rendering ----------------------------------------------------

#: The four coach-read paragraphs, in card order. The key is what the model
#: labels its output with; the label is what the reader sees above each
#: paragraph. Lives HERE (0.38.0, formerly workout_coach.READ_SECTIONS)
#: because it is the card's contract — render_markdown, visuals and
#: card_store all consume it, and housing it in workout_coach forced each of
#: them to import from the module that judges the card rather than the one
#: that defines it. workout_coach re-exports it unchanged.
READ_SECTIONS: tuple[tuple[str, str], ...] = (
    ("distance", "DISTANCE"),
    ("pace", "PACE"),
    ("hr", "HEART RATE"),
    ("load", "TRAINING LOAD"),
)

_METRIC_LABELS = [
    ("distance", "Distance"), ("pace", "Pace"),
    ("hr", "Avg HR"), ("load", "Training Load"),
]

# The same metrics named for prose rather than for a table column: "Avg HR"
# reads wrong mid-sentence.
_METRIC_PROSE = {"distance": "distance", "pace": "pace",
                 "hr": "HR", "load": "training load"}


def _prose_list(items: list[str]) -> str:
    """``["HR", "training load"]`` → ``"HR and training load"``, sentence-cased
    because the result opens a sentence."""
    if not items:
        return ""
    joined = items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"
    return joined[0].upper() + joined[1:]


def _fmt_distance(m):
    mi = units.to_miles(m)
    return f"{mi:.2f} mi" if mi is not None else "—"


def _fmt_pace(sec_per_km):
    p = units.format_pace_min_per_mi(sec_per_km)
    return f"{p}/mi" if p else "—"


def _fmt_hr(v):
    return f"{round(v)} bpm" if v else "—"


def _fmt_load(v):
    """Training load is a Garmin index, not a measurement — the stored float
    carries ~13 meaningless decimals. Round it."""
    return f"{round(v)}" if v else "—"


_FORMATTERS = {
    "distance": _fmt_distance, "pace": _fmt_pace,
    "hr": _fmt_hr, "load": _fmt_load,
}


def _fmt_hr_band(lo: float | None, hi: float | None) -> str:
    """The HR band as the reader should see it: a ceiling, a floor, or a range."""
    if lo is not None and hi is not None:
        return f"{round(lo)}–{round(hi)} bpm"
    if hi is not None:
        return f"≤ {round(hi)} bpm"
    if lo is not None:
        return f"≥ {round(lo)} bpm"
    return "—"


def expected_text(key: str, metric: dict) -> str:
    """What the run was held to, as the reader should see it.

    HR carries a band rather than a point, so it supplies its own display
    string; every other metric formats its numeric expectation.
    """
    display = metric.get("expected_display")
    if display:
        return display
    expected = metric.get("expected")
    return _FORMATTERS[key](expected) if expected is not None else "—"


def actual_text(key: str, metric: dict) -> str:
    """What the run actually did, as the reader should see it.

    The mirror of ``expected_text``. Quality-day pace is graded on the fastest
    split rather than the run average, and a bare "9:25/mi" beside a rep target
    would silently imply the whole run was run at that pace — so that branch
    supplies its own label.
    """
    display = metric.get("actual_display")
    if display:
        return display
    actual = metric.get("actual")
    return _FORMATTERS[key](actual) if actual is not None else "—"


def _delta_text(key: str, metric: dict) -> str:
    """Signed, human delta between actual and expected — the granularity a
    bare letter loses."""
    actual, expected = metric.get("actual"), metric.get("expected")
    if actual is None or expected is None or not isinstance(expected, (int, float)):
        return "—"
    if not metric.get("grade"):
        # An ungraded metric has no gap worth stating: the two numbers weren't
        # comparable, which is precisely why it wasn't graded. Printing
        # "224s/mi slower" beside an n/a re-makes the comparison the n/a exists
        # to refuse.
        return "—"
    if key == "pace":
        diff = round((actual - expected) * units.KM_PER_MILE)
        if abs(diff) < 1:
            return "on target"
        return f"{abs(diff)}s/mi {'slower' if diff > 0 else 'faster'}"
    if key == "hr" and metric.get("in_band"):
        # Inside the band IS the A. A percentage against one edge would imply
        # a miss that did not happen.
        return "in range"
    if not expected:
        return "—"
    pct = (actual / expected - 1) * 100
    if round(pct) == 0:
        # A sub-half-percent shortfall printed "-0%" on a live card (4.00 of
        # 4.00 mi, off by meters) — negative zero reads like a typo beside an
        # A+. Within rounding of zero IS on target; say so.
        return "on target"
    return f"{pct:+.0f}%"


def reference_line(card: dict, *, markdown: bool = True) -> str:
    """One sentence naming the yardstick. The card must never leave this
    ambiguous — the same run grades differently under a plan than under the
    rolling median, and the reader has to know which happened.

    ``markdown=False`` drops the ``**`` emphasis for the PDF, whose HTML is
    escaped rather than markdown-rendered — otherwise the asterisks print
    literally on the page.
    """
    ref = card["reference"]
    mode = ref.get("mode")
    em = "**" if markdown else ""
    plan_graded = any((m.get("reference") or "").startswith("plan")
                      for m in card["metrics"].values())
    if mode == "insufficient_data" and not plan_graded:
        return (f"Not enough comparable history to grade — {ref.get('n', 0)} similar "
                f"activities in the last {REFERENCE_WINDOW_DAYS} days "
                f"(need {MIN_REFERENCE_ACTIVITIES}).")
    intent_src = ("prescribed by your plan" if card["intent_source"] == "plan"
                  else "inferred from the run itself")
    pool = ref.get("pool", "")
    widened = (" Pool widened to all on-foot activities — too few of this exact "
               "type to compare against." if ref.get("widened") else "")
    # The locomotion filter is invisible in the numbers, so it has to be stated:
    # a reader comparing against Garmin's own app would otherwise see a different
    # median and have no way to account for the gap.
    n_excluded = ref.get("excluded_other_mode") or 0
    other = "walking" if ref.get("mode_label") == "running" else "running"
    excluded = (f" {n_excluded} same-window {other}-effort "
                f"{'activity' if n_excluded == 1 else 'activities'} excluded — "
                f"Garmin labels them the same, the pace says otherwise."
                if n_excluded else "")
    widened += excluded
    if plan_graded:
        plan_sentence = (f"Graded against your {em}training plan{em} for this date "
                         f"(intent: {card['intent']}, {intent_src}).")
        if mode == "insufficient_data":
            # The plan still graded what it has targets for, so the blanket
            # "not enough history to grade" sentence would contradict the
            # letters printed directly above it. Scope the disclaimer to the
            # metrics it actually applies to.
            n = ref.get("n", 0)
            ungraded = _prose_list([
                _METRIC_PROSE[k] for k, _ in _METRIC_LABELS
                if not card["metrics"].get(k, {}).get("grade")])
            if not ungraded:
                return plan_sentence
            return (f"{plan_sentence} {ungraded} ungraded — only {n} comparable "
                    f"{'activity' if n == 1 else 'activities'} in the last "
                    f"{REFERENCE_WINDOW_DAYS} days (need {MIN_REFERENCE_ACTIVITIES}).")
        return (f"{plan_sentence} HR and training load "
                f"have no plan target, so they use your 60-day median of "
                f"{ref.get('n', 0)} {pool} activities.{widened}")
    return (f"Graded against your {em}60-day rolling median{em} of {ref.get('n', 0)} "
            f"{pool} activities (intent: {card['intent']}, {intent_src}).{widened}")


def render_markdown(card: dict) -> str:
    """The tabular half of the report card."""
    act = card["activity"]
    name = act.get("activity_name") or act.get("activity_type") or "Workout"
    overall = card["overall"]
    gpa = f" ({overall['gpa']:.2f} GPA)" if overall.get("gpa") is not None else ""

    lines = [
        f"# Report Card — {name}",
        f"**{act.get('date')}** · {_fmt_distance(act.get('distance_meters'))} "
        f"in {units.format_duration(act.get('duration_seconds')) or '—'} · "
        f"{_fmt_pace(act.get('avg_pace_sec_per_km'))}",
        "",
    ]
    lines += [f"## Overall: {overall['grade']}{gpa}", ""]
    # One short paragraph per graded area, under the grade line. The yardstick
    # is no longer printed as its own sentence — the Expected column states it
    # per metric, which is where a reader actually checks it.
    read = card.get("coach_read") or {}
    if read:
        for key, label in READ_SECTIONS:
            if read.get(key):
                lines += [f"**{label.title()}** — {read[key]}", ""]

    rows = []
    for key, label in _METRIC_LABELS:
        m = card["metrics"][key]
        rows.append([
            label,
            actual_text(key, m),
            expected_text(key, m),
            _delta_text(key, m),
            m.get("grade") or "n/a",
        ])
    lines.append(render.render_table(
        ["Metric", "Actual", "Expected", "Delta", "Grade"], rows))

    notes = [f"- {label}: {card['metrics'][key]['note']}"
             for key, label in _METRIC_LABELS if card["metrics"][key].get("note")]
    if card["metrics"]["load"].get("spike"):
        notes.append("- Training Load: **spike** — more than double your median day.")
    if card["overall"].get("capped_by") == "F":
        notes.append(
            f"- Overall: capped at {card['overall']['grade']} — a metric graded F.")
    if notes:
        lines += ["", *notes]

    # The yardstick. Dropped when the Expected column landed, restored now that
    # the pool stops being "every activity Garmin filed under this type" —
    # which median a run is measured against is a real decision the card makes,
    # and the reader can't reconstruct it from the numbers.
    lines += ["", f"_{reference_line(card)}_"]

    lines += ["", f"## Per-{card['splits']['unit'].lower()} breakdown", ""]
    if not card["splits"]["available"]:
        lines.append(
            "_No per-mile splits recorded for this activity. Splits are captured "
            "only by the daily sync path — backfilled activities have none._")
    else:
        avg_hr = act.get("avg_hr")
        split_rows = []
        for r in card["splits"]["rows"]:
            # The label already carries the partial lap's distance, which is
            # why there is no Distance column: for every full lap it would
            # read "1.00 mi" beside a column literally headed "Mile".
            label = (f"{card['splits']['unit']} {r['index']}" if not r["partial"]
                     else f"final {r['distance_mi']:.2f} mi")
            hr = r.get("avg_hr")
            elev = r.get("elevation_gain_meters")
            split_rows.append([
                label,
                f"{r['pace_min_per_mi']}/mi" if r.get("pace_min_per_mi") else "—",
                f"{hr} bpm" if hr else "—",
                f"{hr - avg_hr:+d}" if hr and avg_hr else "—",
                f"{elev:.0f} m" if elev is not None else "—",
            ])
        lines.append(render.render_table(
            [card["splits"]["unit"], "Pace", "Avg HR", "vs run", "Elev"],
            split_rows))
        drift = card["splits"]["hr_drift_pct"]
        if drift is not None:
            lines += ["", f"_HR drift back-half vs front-half: {drift:+.1f}%_"]

    ctx = card.get("context") or {}
    if ctx.get("ctl") is not None:
        lines += [
            "",
            f"_Fitness (CTL) {ctx['ctl']:.0f} · fatigue (ATL) {ctx['atl']:.0f} · "
            f"freshness (TSB) {ctx['tsb']:+.0f} on this date._",
        ]
    return "\n".join(lines)


# === Persistence ===========================================================
# Everything above is pure. Everything below reads the DB.


def _plus_days(iso_date: str, days: int) -> str:
    """ISO date shifted by ``days``. Returns the input unchanged if it isn't a
    parseable date, so a malformed row narrows the window rather than raising."""
    try:
        return (_date.fromisoformat(iso_date) + timedelta(days=days)).isoformat()
    except (TypeError, ValueError):
        return iso_date


def _exact_type(activity_type: str | None):
    target = (activity_type or "").lower()
    return lambda t: (t or "").lower() == target


def _class_type(activity_type: str | None):
    """The wider net: running compares to running, walking to walking — reusing
    plans.py's substring classifiers rather than re-deriving them."""
    if plans._is_running(activity_type):
        return plans._is_running
    if plans._is_walking(activity_type):
        return plans._is_walking
    return _exact_type(activity_type)


def _median_of(rows: list[dict], key: str) -> float | None:
    vals = [r[key] for r in rows if r.get(key)]
    return float(median(vals)) if vals else None


def rolling_reference(conn: sqlite3.Connection, activity: dict) -> dict:
    """Trailing-60-day medians over comparable activities.

    The window ENDS the day before the graded activity so a workout can never
    move its own goalposts.

    Comparability is exact ``activity_type`` first, widening to the on-foot
    class only if the exact pool is too thin — and the widening is disclosed on
    the card. See the module docstring: pooling road with treadmill runs is
    what produced a spurious D on heart rate for a perfectly normal easy run.

    Under ``MIN_REFERENCE_ACTIVITIES`` comparable rows even after widening, the
    reference is declared insufficient rather than quietly grading against one
    or two samples.
    """
    try:
        end = _date.fromisoformat(activity["date"]) - timedelta(days=1)
    except (TypeError, ValueError):
        return {"mode": "insufficient_data", "n": 0}
    start = end - timedelta(days=REFERENCE_WINDOW_DAYS - 1)
    raw = [dict(r) for r in conn.execute(
        "SELECT activity_id, activity_type, distance_meters, avg_pace_sec_per_km, "
        "avg_hr, training_load FROM activities "
        "WHERE date >= ? AND date <= ? AND distance_meters > 0",
        (start.isoformat(), end.isoformat()),
    ).fetchall()]
    raw = [r for r in raw if r["activity_id"] != activity.get("activity_id")]

    # Partition on the DATA before partitioning on the label. A run is only ever
    # compared against running-effort activities and a walk against walking-effort
    # ones, because `activity_type` cannot tell them apart — see
    # RUN_PACE_CEILING_SEC_PER_MI. This runs BEFORE the type filters so the
    # widened on-foot pool is mode-clean too; widening is what would otherwise
    # pull the whole walking-pad corpus into a thin running pool.
    atype = activity.get("activity_type")
    mode = is_running_effort(activity.get("avg_pace_sec_per_km"))
    excluded_other_mode = 0
    mode_label = {True: "running", False: "walking", None: None}[mode]
    if mode is not None:
        # A paceless row has an unknown mode and matches neither side.
        in_mode = [r for r in raw if is_running_effort(r["avg_pace_sec_per_km"]) is mode]
        # Count only rows this pool could actually have drawn from, so the
        # card's "Garmin labels them the same" claim is true of every one of
        # them. A genuinely-typed `walking` row was never a candidate and
        # counting it would overstate the mislabelling.
        candidate = _class_type(atype)
        excluded_other_mode = sum(
            1 for r in raw
            if r not in in_mode and candidate(r["activity_type"]))
        raw = in_mode
    # else: the graded activity has no pace of its own, so its mode is unknowable.
    # Fall through to type-only comparison rather than guessing a side.

    rows = [r for r in raw if _exact_type(atype)(r["activity_type"])]
    pool, widened = (atype or "").lower() or "comparable", False
    if len(rows) < MIN_REFERENCE_ACTIVITIES:
        wider = [r for r in raw if _class_type(atype)(r["activity_type"])]
        if len(wider) > len(rows):
            rows, pool, widened = wider, "on-foot", True

    if len(rows) < MIN_REFERENCE_ACTIVITIES:
        return {"mode": "insufficient_data", "n": len(rows),
                "pool": pool, "window_days": REFERENCE_WINDOW_DAYS,
                "excluded_other_mode": excluded_other_mode}
    return {
        "mode": "rolling_60d",
        "n": len(rows),
        "pool": pool,
        "widened": widened,
        # How many in-window activities were dropped as the other locomotion
        # mode. Surfaced on the card: "compared against 16 runs, 30 walking-pad
        # sessions excluded" is the reader's only clue that this filter ran.
        "excluded_other_mode": excluded_other_mode,
        "mode_label": mode_label,
        "window_days": REFERENCE_WINDOW_DAYS,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "median_distance_m": _median_of(rows, "distance_meters"),
        "median_pace_sec_per_km": _median_of(rows, "avg_pace_sec_per_km"),
        "median_hr": _median_of(rows, "avg_hr"),
        "median_load": _median_of(rows, "training_load"),
    }


def _select_activity(
    conn: sqlite3.Connection, activity_id: int | None, target_date: str | None
) -> sqlite3.Row | None:
    """Resolution order: explicit id, then date, then most recent.

    The ``distance_meters > 0`` filter keeps the default from landing on a
    strength session, which has no distance, pace or per-mile anything to
    grade. An explicit ``activity_id`` bypasses it — if you asked for that one,
    you get it, with n/a where metrics are missing. ``start_time`` is
    'YYYY-MM-DD HH:MM:SS' text, so lexicographic ordering is chronological.

    The date branch prefers the day's LONGEST running-effort session (pace-
    gated via ``is_running_effort``, never the ``activity_type`` label),
    falling back to the earliest session when NONE of the day's activities
    measure as running — a genuine walk day must still be gradeable.

    This replaced a bare "first by start_time" rule, whose own justification
    (an evening shakeout paired with a morning long run) was measured to have
    the OPPOSITE shape 26 times in the real corpus's 52 multi-activity days:
    the FIRST session is a walk and the real run comes later. Live cases:
    2026-07-21 selected a 3.23 mi @ 29:15/mi walk over a 5.95 mi @ 10:42/mi
    interval run; 2026-07-26 selected a 2.53 mi @ 17:06/mi walk over a 4.00 mi
    @ 8:56/mi run. Longest (not fastest) among running-effort candidates, so a
    brief brisk shakeout can't outrank the day's real workout. The no-argument
    branch is unaffected: "most recent" genuinely means the latest session.
    """
    if activity_id is not None:
        return conn.execute(
            f"SELECT {_ACTIVITY_SELECT} FROM activities WHERE activity_id = ?",
            (activity_id,),
        ).fetchone()
    if target_date:
        rows = conn.execute(
            f"SELECT {_ACTIVITY_SELECT} FROM activities WHERE date = ? "
            "AND distance_meters > 0 AND duration_seconds > 0 "
            "ORDER BY start_time ASC",
            (target_date,),
        ).fetchall()
        if not rows:
            return None
        running = [r for r in rows
                   if is_running_effort(r["avg_pace_sec_per_km"]) is True]
        if running:
            return max(running, key=lambda r: r["distance_meters"])
        return rows[0]
    return conn.execute(
        f"SELECT {_ACTIVITY_SELECT} FROM activities "
        "WHERE distance_meters > 0 AND duration_seconds > 0 "
        "ORDER BY date DESC, start_time DESC LIMIT 1"
    ).fetchone()


def load_report_card_inputs(
    conn: sqlite3.Connection,
    activity_id: int | None = None,
    target_date: str | None = None,
    *,
    hr_trace: bool = False,
) -> dict | None:
    """Every input ``build_card`` needs, or ``None`` when no activity matches.

    ``hr_trace=True`` additionally resolves the per-sample HR trace, which may
    reach the network on a cache miss (see ``ingest.details.get_hr_samples``).
    It defaults to OFF so the plain tabular path — and every existing caller —
    stays purely local and fast; only the PDF render, which already accepts
    seconds of latency for WeasyPrint, opts in.
    """
    row = _select_activity(conn, activity_id, target_date)
    if row is None:
        return None
    # No raw_json to strip — _select_activity never fetched it (_ACTIVITY_COLUMNS).
    activity = dict(row)
    aid = activity["activity_id"]

    splits = [dict(r) for r in conn.execute(
        "SELECT * FROM activity_splits WHERE activity_id = ? ORDER BY split_index", (aid,)
    ).fetchall()]

    plan_workout = None
    upcoming_workouts: list[dict] = []
    active = plans.get_active_plan(conn=conn)
    if active:
        same_day = [w for w in active.get("workouts", []) if w.get("date") == activity["date"]]
        if same_day:
            # (plan_id, date, seq) is unique; the lowest seq is the day's
            # primary session when a double-day is prescribed.
            plan_workout = min(same_day, key=lambda w: w.get("seq") or 0)
        # What this run was setting up for. The coaching read is written with
        # the card's date in hand, so it can say "you have intervals Thursday"
        # rather than judging the run in isolation.
        upcoming_workouts = sorted(
            (w for w in active.get("workouts", [])
             if w.get("date") and activity["date"] < w["date"] <= _plus_days(
                 activity["date"], UPCOMING_WINDOW_DAYS)),
            key=lambda w: (w["date"], w.get("seq") or 0),
        )[:MAX_CONTEXT_ACTIVITIES]

    # ...and what led into it, for the same reason in the other direction.
    recent_activities = [dict(r) for r in conn.execute(
        "SELECT date, activity_type, activity_name, distance_meters, "
        "avg_pace_sec_per_km, avg_hr, training_load FROM activities "
        "WHERE date < ? AND date >= ? AND distance_meters > 0 "
        "ORDER BY date DESC, start_time DESC LIMIT ?",
        (activity["date"], _plus_days(activity["date"], -RECENT_WINDOW_DAYS),
         MAX_CONTEXT_ACTIVITIES),
    ).fetchall()]

    context: dict = {}
    base = conn.execute(
        "SELECT ctl, atl, tsb FROM baselines WHERE date = ?", (activity["date"],)
    ).fetchone()
    if base is not None:
        context = {k: base[k] for k in ("ctl", "atl", "tsb")}

    hr_samples: list[tuple[float, int]] = []
    if hr_trace:
        from ..ingest import details  # lazy: keeps garminconnect off the hot path

        hr_samples = details.get_hr_samples(conn, aid)

    return {
        "activity": activity,
        "splits": splits,
        "hr_samples": hr_samples,
        "plan_workout": plan_workout,
        "recent_activities": recent_activities,
        "upcoming_workouts": upcoming_workouts,
        "reference": rolling_reference(conn, activity),
        "context": context,
        # Enough of each other session to say WHICH one wasn't graded. A bare
        # id told the reader a second session existed and nothing else, so
        # "grade my run" on a double day gave no way to tell whether the card
        # covered the one they meant.
        "other_activities_on_date": [
            {"activity_id": r["activity_id"],
             "activity_type": r["activity_type"],
             "distance_mi": units.to_miles(r["distance_meters"]),
             "start_time": r["start_time"]}
            for r in conn.execute(
                "SELECT activity_id, activity_type, distance_meters, start_time "
                "FROM activities WHERE date = ? AND activity_id != ? "
                "ORDER BY start_time",
                (activity["date"], aid),
            ).fetchall()
        ],
    }
