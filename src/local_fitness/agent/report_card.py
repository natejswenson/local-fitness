"""Deterministic report-card grading for a single workout.

The coach can already *describe* a workout (``get_workout_detail``), but
nothing *judged* one — every assessment was phrased ad hoc by the model, so
the same run could be called "solid" one day and "flat" the next. This module
makes the judgment tested Python, per the repo convention that the LLM phrases
a judgment but never derives one that code can compute (the ``interpret.py``
pattern).

Metrics are partitioned into two kinds, and the split is the point of the
module (0.40.0):

- **compliance** — distance, pace, HR. "Did you execute the prescription?"
  Graded, and the only thing the overall letter averages.
- **stimulus** — training load, aerobic/anaerobic TE, HR-zone distribution,
  drift. "What did this run do to your body?" Reported with numbers and a
  descriptor, never a letter, and structurally unable to move the overall.

Each compliance metric still reduces to a single non-negative relative
deviation ``d`` passed through ONE shared band table. That is the whole trick:
three small deviation functions, one grader, so the rubric stays testable.

The partition exists because grading load *and* HR is grading one variable
twice with the sign reversed. Garmin's training load is essentially
``duration x f(HR)``, so obeying an easy day's HR cap mechanically drives the
load number down — and load's own undershoot penalty then punished exactly the
compliance the HR grade had just rewarded.

Measured 2026-07-29 (median_hr 143, median_load 99.7 over 23 comparable
treadmill runs), the two easy days that week, both prescribed
"Easy 5mi. Keep HR under 140.":

- executed correctly (5.01 mi, avg HR 126, even splits): distance A+, pace A+,
  HR A+, load **F** (25 against a ~75 expectation) -> GPA 3.60, an A, thrown
  away by the F-cap to a **C**.
- cap blown from mile 3 (5.00 mi, avg HR 144, splits hitting 150 and 159):
  distance A+, pace A+, HR A-, load A+ (82, comfortably "enough work")
  -> GPA 4.00, an uncapped **A**.

The rubric was inverted: it handed the disobedient run an A and the obedient
one a C. A compliant sub-cap 50-minute run tops out near 70 load (median 1.42
load/min across the window's sub-140 sessions) and a *properly* easy one lands
near 25, so load's F threshold sat above the physical maximum of a compliant
easy run — the grade was unreachable, not merely strict.

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

Splits are presentation-only, with exactly THREE documented exceptions. No other
grade reads ``activity_splits`` — only 100 of 760 activities have them (they are
written by the daily-sync ingest path, never by backfill), so a splits-dependent
grade would be unavailable on ~87% of the history and would silently mean
different things on different rows. Every exception must handle absence
explicitly, and they do it two different ways on purpose.

1. **quality-day pace against a prescribed rep target** — see
   ``fastest_rep_split``. It exists because the alternative was not a strict
   grade but a broken one: a plan's interval pace describes the reps, while
   ``avg_pace_sec_per_km`` averages in the warmup, the recovery jogs and the
   cooldown, so that comparison returns F for every correctly-executed interval
   session. *Abstains* when splits are missing — n/a with a stated reason, and
   the weight redistributes.
2. **continuity** — see ``continuity_ratio``. Also *abstains*.
3. **the prescribed-HR-cap exceedance** — see ``hr_exceedance_bpm``. This one
   *degrades* instead: with no splits the cap is still graded on the average
   alone, so it adds no new availability cliff.

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
    "stimulus_level", "zone_summary", "stimulus_block", "stimulus_lines",
    "has_stimulus", "stimulus_rows", "stimulus_notes", "stimulus_heading",
    "COMPLIANCE_METRICS", "STIMULUS_METRICS",
    "render_markdown", "reference_line", "bin_hr_trace", "expected_text",
    "actual_text", "hr_band_bounds", "hr_expectation",
    "is_running_effort", "fastest_rep_split", "fastest_rep_split_pace",
    "time_above_cap_fraction", "hr_exceedance_bpm", "hr_cap_severity",
    "hr_cap_deviation", "hr_cap_axis",
    "continuity_ratio", "continuity_deviation",
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
# Load is deliberately ABSENT from every table below — it is a stimulus metric
# now, not a compliance one (see the module docstring). Membership in these
# tables is what `overall_grade` iterates, so removing the key is what makes
# load structurally unable to move the overall: there is no weight small
# enough to be safe, because the failure was the F-cap, not the weight (load
# was already only 10% of an easy day when it turned a 3.60 GPA into a C).
#
# The remaining three keep their pre-0.40.0 proportions exactly — each table is
# the old one with load dropped and renormalized to sum to 1.0, which is the
# same thing `overall_grade`'s redistribution would have computed anyway.
METRIC_WEIGHTS = {"distance": 0.30, "pace": 0.30, "hr": 0.25, "continuity": 0.15}
INTENT_METRIC_WEIGHTS: dict[str, dict[str, float]] = {
    "easy": {"distance": 0.19, "pace": 0.42, "hr": 0.24, "continuity": 0.15},
    "quality": {"distance": 0.19, "pace": 0.42, "hr": 0.24, "continuity": 0.15},
    "long": {"distance": 0.45, "pace": 0.20, "hr": 0.20, "continuity": 0.15},
    # No stated intent means no metric can claim to be the point of the day —
    # keep the neutral split.
    "steady": METRIC_WEIGHTS,
}
# The graded set, derived from the weight tables rather than re-listed, so a
# metric can never be in one and not the other.
COMPLIANCE_METRICS: frozenset[str] = frozenset(METRIC_WEIGHTS)
STIMULUS_METRICS: frozenset[str] = frozenset({"load"})
_GPA_CUTS: tuple[tuple[float, str], ...] = (
    (3.5, "A"), (2.5, "B"), (1.5, "C"), (0.5, "D"),
)
# An F on a COMPLIANCE metric caps the overall here. A card that prints
# "Overall: A" above a row reading F is not reporting a grade, it is averaging
# away the finding — that reasoning is unchanged and the cap is deliberately
# kept. What changed in 0.40.0 is only its scope: `overall_grade` now tests the
# weighted metrics rather than every metric, so a stimulus row can never fire
# it. The cap was never the wrong rule; load was the wrong thing to apply it to.
_F_FLOOR_GRADE = "C"

# Intent scaling. An easy day is EXPECTED to be shorter and slower than the
# median; a long day longer. Applied to the rolling reference only — a plan
# states its own targets and needs no scaling.
DISTANCE_FACTORS = {"easy": 0.75, "long": 1.40, "quality": 1.00, "steady": 1.00}
# Load was `dict(DISTANCE_FACTORS)` until 0.40.0, on the reasoning that a
# separately-tuned table would be false precision — "named separately so they can
# diverge if evidence ever says they should". Evidence now says so, for easy.
#
# Measured 2026-07-29 over the live 60-day treadmill pool: the 9 sessions at or
# under the easy HR ceiling (139 bpm) have a median load of 60.5 against the
# pool's 99.7 — a ratio of 0.61, not the 0.75 inherited from distance. An easy
# day banks proportionally less load than it does distance, because load is
# intensity-weighted and distance is not.
#
# This only ever drives the STIMULUS descriptor now, never a letter, which is
# why it can be recalibrated freely. Worth recording that recalibrating alone
# would NOT have fixed the 2026-07-29 card: at 0.61 the expectation is 60, and
# a correct 25-load easy run still deviates 0.58 — an F. The structural fix
# (load carries no grade) was necessary; this is honesty in the descriptor.
LOAD_FACTORS = {"easy": 0.61, "long": 1.40, "quality": 1.00, "steady": 1.00}
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
# Fraction of a run above a prescribed HR cap worth REPORTING. This is a
# reporting threshold only — it has not driven the grade since 0.40.2, because
# a time fraction cannot be graded (see `hr_exceedance_bpm`). A few seconds over
# on a hill or a treadmill surge is not worth a note; most of the session over
# the stated ceiling is, whichever way the letter lands.
HR_CAP_GRACE_FRACTION = 0.05
# --- the HR-cap severity scale (0.40.2) ------------------------------------
# A breach of a prescribed cap is measured in *bpm above the ceiling*, sustained
# over time — not as a fraction of the cap, and not as a fraction of the run.
#
# Not a fraction of the cap, for the same reason `continuity_deviation` takes a
# raw excess: HR has a huge non-zero offset, so dividing by the cap compresses
# every real breach into the A/B bands. Measured over the 19 completed capped
# days in the live plan, `exceedance / cap` puts the WORST session in the window
# (avg 157 against a 140 cap, splits hitting 185, 48% of it in Garmin zones 4-5)
# at 0.139 — a C. The absolute bpm excursion is the quantity that means
# something physiologically; 12 bpm over a ceiling is 12 bpm over a ceiling
# whether the ceiling is 130 or 150.
#
# HR_CAP_NOISE_BPM is the floor below which a split-average breach is not
# distinguishable from rounding. Calibrated, not guessed: over those 19 days,
# the two runs whose AVERAGE obeyed the cap (139 and 136 against 140) carry a
# time-weighted exceedance of 1.15 and 1.37 bpm, and both spent 0% of their
# time in zones 4-5. They are compliant runs and must read A+. The smallest
# exceedance belonging to a run that broke the cap on average is 4.55 bpm.
# 1.5 separates the two populations with margin on both sides.
HR_CAP_NOISE_BPM = 1.5
# ...and the scale that turns real bpm-over into the shared bands, so an F
# begins at HR_CAP_NOISE_BPM + 0.35 * HR_CAP_BPM_SCALE = 11.3 bpm sustained
# above the ceiling.
#
# Validated against a signal the grade does not read: Garmin's own zone-4+5
# time fraction, computed on the device from the per-sample trace and therefore
# independent of both avg_hr and the splits. In the live window exactly three
# sessions carry an exceedance at or above 11.3 bpm (11.9, 13.6, 19.5) and they
# are exactly the three whose zone-4+5 share is 42% or more — the runs that
# stopped being aerobic runs at all. The highest zone-4+5 share below the
# boundary is 37%. So the F set is "this was not the workout that was
# prescribed", drawn on evidence rather than on intuition.
HR_CAP_BPM_SCALE = 28.0
# How much slower than the run's own median split the SLOWEST split may be before
# the session is treated as having contained a break rather than a pace
# variation. See continuity_deviation for the measurement behind 1.15.
CONTINUITY_TOLERANCE = 1.15
# Distance gap below which the Delta column says "on target" instead of a
# number. ~32 m — GPS wobble and treadmill rounding, not a miss.
_DISTANCE_ON_TARGET_MI = 0.02
# Below this many full splits a slowest-vs-median ratio is noise, not a finding —
# and it is what keeps manually-lapped interval sessions out (see
# continuity_ratio).
MIN_CONTINUITY_SPLITS = 3
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


def pace_bound_kind(cls: str) -> str:
    """Which side of the pace expectation ``pace_deviation`` actually penalizes:
    ``"floor"``, ``"ceiling"`` or ``"point"``.

    Exists so the Expected column can state the bound the grade was measured
    against rather than a bare number. Before this, an easy day printed

        | Pace | 9:44/mi | 9:39/mi | 5s/mi slower | A+ |

    — a stated target, a stated 5s/mi miss, and an A+. Every number there is
    correct and the row still reads as a broken grade, because 9:39 is not a
    point target on an easy day: ``pace_deviation`` gates easy/long to the FAST
    side only, so running slower than it is compliance, not a shortfall. That
    display is the whole reason 91% of the A-band grades on real cards look
    like a participation trophy (measured over 15 stored cards: 9 of 15 pace
    deviations are exactly 0.0, which is mechanically an A+).

    Mirrors ``pace_deviation``'s own branches, and
    ``test_pace_bound_kind_matches_pace_deviation_gating`` re-derives it from
    that function so the two cannot drift.

    Note the bound described is the one relative to the TARGET. easy/long also
    carry ``pace_deviation``'s absolute walk floor
    (``RUN_PACE_CEILING_SEC_PER_MI``), which is an anti-abuse guard rather than
    a prescription and is deliberately not printed here — a card that read
    "9:39-13:00/mi" would imply the plan asked for a range it never asked for.
    """
    if cls in ("easy", "long"):
        # Only too FAST costs, so the expectation is a floor on the pace value
        # (sec/km — bigger is slower).
        return "floor"
    if cls == "quality":
        return "ceiling"
    return "point"


def bounded_display(kind: str, text: str) -> str:
    """A formatted expectation prefixed with the bound it represents.

    Reuses the ``≤`` / ``≥`` idiom ``_fmt_hr_band`` already established, so a
    one-sided pace or distance target reads the same way a one-sided HR band
    does and the reader learns one convention instead of three.
    """
    if kind == "floor":
        return f"≥ {text}"
    if kind == "ceiling":
        return f"≤ {text}"
    return text


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


def continuity_ratio(labelled: dict) -> float | None:
    """Slowest full split / median full split, or ``None`` when unmeasurable.

    Answers the one question distance, pace and HR all miss: **was this one
    continuous session, or did it contain a break?** A walk mile in the middle of
    a run averages away in every other metric — measured 2026-07-28, a tempo day
    with a 12:31 mile among ~9:20 miles graded A+ on distance and A+ on HR, and
    nothing on the card mentioned it.

    Deliberately the slowest split against the run's OWN median, not a standard
    deviation, and not against a plan target:

    - **SD punishes a legitimate warm-up.** On live data the first split is
      routinely the outlier (2026-07-27: SD 22.2 s/mi across the run, 4.9 s/mi
      once the opening mile is dropped). A metric that fails a run for starting
      conservatively would recreate the unfairness the 0.40.0 split just fixed.
    - **A ratio against the run's own median is self-scaling**, so it means the
      same thing on a 9:00 tempo and a 12:00 shakeout, and needs no intent
      factor.
    - **Walk-pace detection alone is not enough**: 12:31/mi is under
      ``RUN_PACE_CEILING_SEC_PER_MI`` (13:00), so the absolute boundary would
      have missed the exact session this metric exists for.

    ``MIN_CONTINUITY_SPLITS`` (3) full splits are required. That floor also
    excludes manually-lapped interval sessions by construction: ``label_splits``
    marks partial relative to the workout's own longest lap, so a
    2-mile-warmup-then-400s workout has exactly one "full" split, and comparing
    reps against a warmup would fail every correctly-run interval day — the same
    trap ``fastest_rep_split`` was written to escape.
    """
    paces = [r["avg_pace_sec_per_km"] for r in (labelled.get("rows") or [])
             if not r.get("partial") and r.get("avg_pace_sec_per_km")]
    if len(paces) < MIN_CONTINUITY_SPLITS:
        return None
    mid = median(paces)
    return (max(paces) / mid) if mid else None


def continuity_deviation(ratio: float | None) -> float | None:
    """Continuity ratio → deviation, as the RAW excess over the tolerance.

    Not ``(ratio - tol) / tol``: the ratio already lives near 1.0, so dividing by
    the tolerance compresses every real break into the A/B bands (a 12:31 mile
    among 9:20s would have scored a B). The raw excess reads as "how much slower
    than an already-generous allowance the worst split was, in units of a median
    split", which puts a 20%-slower mile at an A, 35% at a C and 55% at an F.

    Measured against the 40 split-bearing sessions in the live 90-day window,
    ``CONTINUITY_TOLERANCE`` = 1.15 separates cleanly: 33 sessions sit at or
    under it (easy days cluster at 1.01-1.08), and the 7 above it are all
    genuine run/walk sessions.
    """
    if ratio is None:
        return None
    return max(0.0, ratio - CONTINUITY_TOLERANCE)


def time_above_cap_fraction(labelled: dict, cap: float | None) -> float | None:
    """Fraction of split time whose average HR sat above ``cap``, or ``None``.

    **Reported, never graded** (0.40.2). It answers "for how much of the run?",
    which is worth printing beside the breach — but on its own it cannot say
    whether the run was 1 bpm over or 20, and HR is autocorrelated enough that
    it barely varies between those two cases. Over the 19 completed capped days
    in the live plan it took only two values in practice: 0%, or 58-76%. See
    ``hr_exceedance_bpm`` for the number that carries the severity.
    """
    if not cap or cap <= 0:
        return None
    rows = [r for r in (labelled.get("rows") or [])
            if r.get("avg_hr") and r.get("duration_seconds")]
    total = sum(r["duration_seconds"] for r in rows)
    if not total:
        return None
    above = sum(r["duration_seconds"] for r in rows if r["avg_hr"] > cap)
    return above / total


def hr_exceedance_bpm(labelled: dict, cap: float | None) -> float | None:
    """Time-weighted mean bpm ABOVE ``cap`` across the splits, or ``None``.

    ``sum(duration * max(0, split_hr - cap)) / total_duration`` — the integral
    of the breach over the run, divided by the run. It reads as "you sat, on
    average across the whole session, N bpm above the ceiling you were given",
    and it is the module's SECOND grade to read splits (after quality-day rep
    pace). It earns the exception the same way that one does: the alternative
    number is not stricter but wrong. An average HR sits under a cap that the
    middle of the run spent minutes above — measured 2026-07-27, avg 144 against
    a prescribed 140 is a 2.9% average breach while miles 3-5 ran 150/144/159.

    This REPLACES the 0.40.0 time-fraction axis, which was a category error. A
    fraction of a run and a relative magnitude are different units, so feeding a
    time fraction through ``GRADE_BANDS`` — a table calibrated for "how far off
    target" — graded a quantity the table does not describe, and taking
    ``max()`` over the two compared incommensurable numbers. Its measured
    consequence: across the 19 completed capped days in the live plan, that axis
    produced **only A+ or F, never a letter in between**. On 2026-08-02 a run
    whose average (139) obeyed a 140 cap, whose peak was 148, and which Garmin
    recorded as 0% in zones 4-5, graded **F** — because three of its miles
    averaged 141/143/142, one to three bpm over, and were each counted as 100%
    above the cap. It even ranked the mildest breach in the window (2026-07-16,
    1% of it in zones 4-5) as the single WORST session, at 75% of time over.
    The exceedance integral separates those cases by construction: 1.15 bpm
    against 19.5 bpm.

    Per-split averages, deliberately NOT the per-sample trace: ``get_hr_samples``
    can reach the network, and no grade may depend on an input that might not
    resolve locally (see ``load_report_card_inputs``'s ``hr_trace`` default).
    The locally-cached ``activity_hr_samples`` table is not an escape hatch
    either — it holds 11 of 760 activities, so reading it "when present" would
    make the metric mean one thing on 1.4% of history and another on the rest,
    which is the exact availability trap the splits rule exists to prevent.

    Like the time fraction before it this DEGRADES rather than abstains: with no
    splits the cap is still graded on the average alone.
    """
    if not cap or cap <= 0:
        return None
    rows = [r for r in (labelled.get("rows") or [])
            if r.get("avg_hr") and r.get("duration_seconds")]
    total = sum(r["duration_seconds"] for r in rows)
    if not total:
        return None
    over = sum(r["duration_seconds"] * max(0.0, r["avg_hr"] - cap) for r in rows)
    return over / total


def hr_cap_severity(bpm_over: float | None) -> float:
    """bpm above a prescribed cap → a deviation the shared bands can grade.

    The raw excess past ``HR_CAP_NOISE_BPM``, scaled by ``HR_CAP_BPM_SCALE``.
    One function so both cap axes are measured identically — that is what makes
    ``max()`` over them meaningful, and it is precisely what 0.40.0 lacked.
    """
    if bpm_over is None:
        return 0.0
    return max(0.0, bpm_over - HR_CAP_NOISE_BPM) / HR_CAP_BPM_SCALE


def hr_cap_deviation(
    hr: float | None,
    cap: float | None,
    exceedance_bpm: float | None = None,
) -> float | None:
    """Deviation from an EXPLICIT prescribed HR ceiling (``target_hr_max``).

    Two ways to breach a stated cap; the grade takes the worse:

    - **average** over the cap — the run as a whole ran too hot.
    - **exceedance** — the time-weighted bpm over it, from splits, which catches
      the run whose middle blew the ceiling while its mean did not.

    Both are now *bpm above the ceiling* put through ``hr_cap_severity``, so
    ``max()`` compares like with like. Before 0.40.2 the second axis was a time
    fraction and the comparison had no meaning; see ``hr_exceedance_bpm``.

    (The exceedance normally dominates — by Jensen's inequality the mean of the
    positive part is at least the positive part of the mean. It is still a
    ``max``, not a substitution, because the two are computed from different
    sources: Garmin's activity-level ``avg_hr``, and the splits, which may cover
    only part of the session or carry no HR at all.)

    ``None`` when there is no cap, which is what makes the caller fall back to
    the rolling ``HR_BANDS`` — a plan without a stated cap grades exactly as it
    did before 0.40.0.

    Graded with the BASE bands, not ``PLAN_TIGHTEN``: ``HR_CAP_BPM_SCALE`` is
    already calibrated against prescribed-cap sessions specifically, so
    tightening on top of it would double-count the same strictness.
    """
    axes = _hr_cap_axes(hr, cap, exceedance_bpm)
    return None if axes is None else max(axes)


def _hr_cap_axes(
    hr: float | None,
    cap: float | None,
    exceedance_bpm: float | None = None,
) -> tuple[float, float] | None:
    """The two breach axes as ``(over_average, over_exceedance)``, or ``None``
    when there is no cap to breach.

    Shared by ``hr_cap_deviation`` and ``hr_cap_axis`` so the grade and the
    row explaining it can never be computed from different formulas.
    """
    if not hr or not cap or cap <= 0:
        return None
    return hr_cap_severity(hr - cap), hr_cap_severity(exceedance_bpm)


def hr_cap_axis(
    hr: float | None,
    cap: float | None,
    exceedance_bpm: float | None = None,
) -> str | None:
    """WHICH axis produced the grade — ``"exceedance"``, ``"average"``, or
    ``None`` when nothing was breached (or there was no cap).

    ``hr_cap_deviation`` takes the worse of two axes and then throws away which
    one won. That discard is what printed, on a live card for 2026-08-02:

        | Avg HR | 139 bpm | ≤ 140 bpm | -1% | F |

    Every displayed number there describes the average, which was *compliant*
    and scored 0.0. A reader cannot reconstruct an F from three passing numbers,
    so the row read as a bug in the grade. (That card was in fact a bug in the
    grade — see ``hr_exceedance_bpm`` — but the display contract stands on its
    own: the row must state the quantity the letter was measured against, the
    same contract the pace row keeps via ``actual_display``.)
    """
    axes = _hr_cap_axes(hr, cap, exceedance_bpm)
    if axes is None:
        return None
    over_avg, over_exc = axes
    if max(axes) == 0.0:
        # Compliant on both axes. There is no breach to attribute, and calling
        # one of them "governing" would imply one happened.
        return None
    return "exceedance" if over_exc > over_avg else "average"


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


# --- stimulus (reported, never graded) -------------------------------------

# Descriptor bands on `load / intent-scaled expected load`. The top boundary is
# LOAD_SPIKE_FACTOR itself so the descriptor and the `spike` flag can never
# disagree — that exact contradiction (a load row reading A+ directly above a
# note reading "spike — more than double your median day") is what
# `load_deviation`'s two-sided rewrite was written to fix in 0.37.x, and the
# same discipline applies now that the output is a word instead of a letter.
STIMULUS_LEVELS: tuple[tuple[float, str], ...] = (
    (0.60, "LOW"), (1.40, "MODERATE"), (LOAD_SPIKE_FACTOR, "HIGH"),
)
_STIMULUS_TOP_LEVEL = "VERY HIGH"
# Zones 1-2 are the aerobic-base zones. Their share of total time is what makes
# "this was actually an easy run" checkable instead of asserted — a 96%-aerobic
# run and a 60%-aerobic one can carry the same average HR.
_AEROBIC_ZONES = frozenset({1, 2})


def stimulus_level(ratio: float | None) -> str | None:
    """``load / expected_load`` → LOW | MODERATE | HIGH | VERY HIGH.

    ``None`` in, ``None`` out — an activity with no load, or no comparable
    history to scale an expectation from, gets no descriptor rather than a
    made-up one.
    """
    if ratio is None:
        return None
    for threshold, level in STIMULUS_LEVELS:
        if ratio <= threshold + _EPS:
            return level
    return _STIMULUS_TOP_LEVEL


def zone_summary(zones: list[dict] | None) -> dict | None:
    """``activity_hr_zones`` rows → seconds per zone plus the aerobic share.

    ``None`` when the activity has no zone rows at all. Populated for 90 of the
    last 90 days (the daily-sync path writes them, as it does splits), so the
    None branch is the pre-sync backfilled history — same availability shape as
    ``label_splits``, and handled the same way: omit the line, never guess it.
    """
    by_zone: dict[int, float] = {}
    for z in zones or []:
        zone, seconds = z.get("zone"), z.get("seconds_in_zone")
        if zone is None or not seconds:
            continue
        by_zone[int(zone)] = by_zone.get(int(zone), 0.0) + float(seconds)
    total = sum(by_zone.values())
    if not total:
        return None
    aerobic = sum(s for zn, s in by_zone.items() if zn in _AEROBIC_ZONES)
    return {
        "seconds_by_zone": dict(sorted(by_zone.items())),
        "total_seconds": total,
        "aerobic_pct": round(aerobic / total * 100),
    }


def stimulus_block(
    activity: dict,
    load_metric: dict,
    zones: list[dict] | None = None,
    drift_pct: float | None = None,
) -> dict:
    """The reported-not-graded half of the card.

    Carries no letter by construction — ``level``/``as_intended`` are words, and
    nothing downstream turns them into GPA (``overall_grade`` iterates the
    weight tables, which have no load key).

    ``as_intended`` reuses ``LOAD_SPIKE_FACTOR`` rather than inventing a second
    threshold: undershooting an intent-scaled expectation is ALWAYS fine — that
    is the entire point of the 0.40.0 partition, an easy day is supposed to bank
    less — so the only thing worth flagging is an overshoot big enough to be a
    spike.
    """
    load = activity.get("training_load")
    expected = load_metric.get("expected")
    ratio = (load / expected) if load and expected and expected > 0 else None
    return {
        "load": load,
        "expected_load": expected,
        "ratio": None if ratio is None else round(ratio, 3),
        "level": stimulus_level(ratio),
        # None (not True) when there is nothing to compare against — "as
        # intended" is a claim, and an unverifiable claim is not made.
        "as_intended": None if ratio is None else ratio <= LOAD_SPIKE_FACTOR + _EPS,
        "spike": bool(load_metric.get("spike")),
        "aerobic_te": activity.get("aerobic_te"),
        "anaerobic_te": activity.get("anaerobic_te"),
        "zones": zone_summary(zones),
        "drift_pct": drift_pct,
    }


def overall_grade(metrics: dict[str, dict], cls: str = "steady") -> dict:
    """Intent-weighted GPA over gradeable COMPLIANCE metrics only.

    ``cls`` selects the weight table — see ``INTENT_METRIC_WEIGHTS`` for why the
    weights aren't flat. It defaults to the neutral split so an older caller
    passing only ``metrics`` keeps the previous behavior.

    Stimulus metrics (``STIMULUS_METRICS``) are absent from every weight table,
    so they drop out here the same way an ``n/a`` does — by not being iterated.
    That is deliberate and is the whole 0.40.0 fix: it makes "training load can
    never lower your grade" a property of the data structure rather than of a
    small weight that a cap could still bypass.

    An ``n/a`` metric drops out and its weight redistributes proportionally,
    so a by-feel plan day with no pace target isn't silently scored as if pace
    were worth 30% of nothing. Zero gradeable metrics yields "n/a" — never
    "F", which would read as a judgment we did not actually make.

    Finally, an F on any single *weighted* metric caps the overall at
    ``_F_FLOOR_GRADE``. Redistribution plus generous weighting can otherwise let
    two good metrics average an outright failure up into a passing letter.
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
    # Scoped to the WEIGHTED metrics on purpose — an ungraded stimulus row has
    # no letter to cap with, and a future one that grew a letter still must not
    # be able to. See _F_FLOOR_GRADE.
    if any(base_letter(m.get("grade")) == "F"
           for k, m in metrics.items() if k in weights):
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
    hr_zones: list[dict] | None = None,
) -> dict:
    """Assemble the full report card. Pure — every input is a plain dict, so
    the whole rubric is testable without a DB.

    ``hr_samples`` is the optional per-sample ``(distance_m, hr)`` trace. Like
    ``splits`` it is presentation-only: no grade reads it, so a card renders
    identically whether or not the trace was available.

    ``hr_zones`` is the optional ``activity_hr_zones`` rows, feeding the
    stimulus block's aerobic share. Appended last so every existing positional
    caller keeps working; absent, the zone line is simply omitted.
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
        # `two_sided=False` above: going LONGER than your rolling norm is never
        # a penalty, so this expectation is a floor and has to print as one.
        # The plan branch stays a bare number because it genuinely is a point
        # target — over-running a prescription costs you there.
        if expected:
            distance["bound"] = "floor"
            distance["expected_display"] = bounded_display(
                "floor", _fmt_distance(expected))
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
    # State the BOUND, not a bare number — see pace_bound_kind. Only when there
    # is an expectation at all: a by-feel day has none, and "≥ —" is noise.
    if expected_pace is not None:
        pace["bound"] = pace_bound_kind(cls)
        pace["expected_display"] = bounded_display(
            pace["bound"], _fmt_pace(expected_pace))

    # -- HR: the plan's own ceiling when it states one (0.40.0), else rolling.
    med_hr = reference.get("median_hr") if has_rolling else None
    actual_hr = activity.get("avg_hr")
    plan_cap = (plan_workout or {}).get("target_hr_max") if plan_ref_ok else None
    if plan_cap:
        # An explicit instruction beats a statistical band. Before this existed
        # "Keep HR under 140" was unreadable prose in `description` and HR was
        # measured against 0.97x the rolling median — which on 2026-07-29 was
        # 139 by coincidence, so a genuine breach of the prescription registered
        # as a rounding error.
        above = time_above_cap_fraction(labelled_splits, plan_cap)
        exceedance = hr_exceedance_bpm(labelled_splits, plan_cap)
        d = hr_cap_deviation(actual_hr, plan_cap, exceedance)
        hr = _metric(grade_from_deviation(d), actual_hr, plan_cap, d, "plan")
        hr["cap"] = plan_cap
        hr["expected_display"] = f"≤ {round(plan_cap)} bpm"
        hr["in_band"] = d == 0.0
        if exceedance is not None:
            hr["exceedance_bpm"] = round(exceedance, 1)
        if above is not None:
            hr["time_above_cap_pct"] = round(above * 100)
        # When the split-derived exceedance is what produced the letter, the
        # whole row has to move to that axis — actual, expected and delta
        # together. Leaving the average in place printed a compliant 139-vs-140
        # beside an F (see hr_cap_axis), which reads as a broken grade.
        hr["governing_axis"] = hr_cap_axis(actual_hr, plan_cap, exceedance)
        if hr["governing_axis"] == "exceedance":
            # One decimal, matching the delta below it, so the three cells
            # reconcile by arithmetic: actual - expected = delta. Rounding the
            # actual to whole bpm made a 19.5 print as "+20" beside a "+18"
            # delta against a 1.5 expectation, which does not add up on the page.
            hr["actual_display"] = f"+{exceedance:.1f} bpm over cap" + (
                f" ({round(above * 100)}% of run)" if above else "")
            hr["expected_display"] = f"≤ +{HR_CAP_NOISE_BPM:g} bpm over cap"
        if above is not None and above > HR_CAP_GRACE_FRACTION:
            # State the fraction over the cap AND how far over, always together.
            # The fraction alone is what 0.40.0 graded on and it is not a
            # severity: printing it by itself beside an A+ reads as the card
            # having noticed a breach and then ignored it, which is the prose
            # contradicting the grade from the other direction.
            over = exceedance or 0.0
            hr["note"] = (
                f"{round(above * 100)}% of the run sat above the prescribed "
                f"{round(plan_cap)} bpm cap, by "
                + (f"{over:.1f} bpm on average — inside sensor noise"
                   if d == 0.0 else f"{over:.1f} bpm on average"))
    else:
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
    # `grade=None` unconditionally: load is a STIMULUS metric (see the module
    # docstring). The deviation is still computed and carried, because the card
    # shows the gap and the descriptor is derived from the same ratio — what is
    # withheld is the letter, not the comparison.
    d = load_deviation(activity.get("training_load"), expected_load)
    load = _metric(
        None, activity.get("training_load"), expected_load, d,
        "rolling_60d" if has_rolling else reference.get("mode"))
    actual_load = activity.get("training_load")
    # The spike flag compares against the UNSCALED median: "double your normal
    # day" is the fact worth printing, regardless of what the day intended.
    if actual_load and med_load and actual_load > LOAD_SPIKE_FACTOR * med_load:
        load["spike"] = True

    # -- continuity: was this one session, or did it contain a break?
    ratio = continuity_ratio(labelled_splits)
    d = continuity_deviation(ratio)
    continuity = _metric(
        grade_from_deviation(d), ratio, CONTINUITY_TOLERANCE, d, "own splits")
    if ratio is None:
        # Same shape as the quality-pace exception: state the reason rather than
        # printing a bare n/a, and let the weight redistribute.
        n_full = len([r for r in (labelled_splits.get("rows") or [])
                      if not r.get("partial") and r.get("avg_pace_sec_per_km")])
        if not labelled_splits.get("available"):
            reason = "no splits recorded — continuity can't be measured"
        elif not n_full:
            # Splits exist but none carries a pace. Distinct from "no splits" on
            # purpose: saying "only 0 full splits" of a run that HAS a split
            # table reads like a bug in the card rather than a gap in the data.
            reason = ("splits recorded without pace — continuity can't be "
                      "measured")
        else:
            reason = (f"only {n_full} full "
                      f"{'split' if n_full == 1 else 'splits'} with pace — need "
                      f"{MIN_CONTINUITY_SPLITS} to compare a slowest against "
                      "a median")
        continuity["note"] = reason
    else:
        continuity["actual_display"] = f"{ratio:.2f}x median split"
        # Typographic ≤, matching _fmt_hr_band and bounded_display. This was
        # the one bound on the card still written in ASCII, and the two forms
        # rendered side by side in the same column.
        continuity["expected_display"] = f"≤ {CONTINUITY_TOLERANCE:.2f}x"
        continuity["ratio"] = round(ratio, 3)
        if d and d > 0:
            slowest = max(
                (r for r in labelled_splits["rows"]
                 if not r.get("partial") and r.get("avg_pace_sec_per_km")),
                key=lambda r: r["avg_pace_sec_per_km"])
            continuity["note"] = (
                f"{labelled_splits['unit'].lower()} {slowest['index']} ran "
                f"{_fmt_pace(slowest['avg_pace_sec_per_km'])} — "
                f"{round((ratio - 1) * 100)}% slower than your median "
                f"{labelled_splits['unit'].lower()} for this run")

    metrics = {"distance": distance, "pace": pace, "hr": hr,
               "continuity": continuity, "load": load}
    return {
        "stimulus": stimulus_block(
            activity, load, hr_zones, labelled_splits.get("hr_drift_pct")),
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
#: The 4th key became ``stimulus`` in 0.40.0 (was ``load``). The ARITY is what
#: the downstream contract depends on — ``card_store.read_is_complete`` requires
#: all four, ``workout_coach`` prompts for all four — so renaming rather than
#: dropping keeps every consumer's shape intact while the section stops being
#: about a grade that no longer exists. Renaming the key changes
#: ``read_cache_key``, so stored reads for already-graded cards miss and
#: regenerate once; that is intended, since an old read argues about a load
#: LETTER the card no longer prints.
READ_SECTIONS: tuple[tuple[str, str], ...] = (
    ("distance", "DISTANCE"),
    ("pace", "PACE"),
    ("hr", "HEART RATE"),
    ("stimulus", "STIMULUS"),
)

#: Compliance metrics only — this drives the graded table, and load is no longer
#: in it. Load renders in the stimulus section instead (see
#: ``_stimulus_lines``), which is what stops a reader from seeing a letter
#: column beside it and inferring one.
_METRIC_LABELS = [
    ("distance", "Distance"), ("pace", "Pace"), ("hr", "Avg HR"),
    ("continuity", "Continuity"),
]

# The same metrics named for prose rather than for a table column: "Avg HR"
# reads wrong mid-sentence.
_METRIC_PROSE = {"distance": "distance", "pace": "pace", "hr": "HR",
                 "continuity": "continuity"}


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


def _fmt_ratio(v):
    """Continuity's fallback formatter. Both display strings are normally set by
    build_card; this only fires on a hand-built metric dict."""
    return f"{v:.2f}x" if v else "\u2014"


def _fmt_load(v):
    """Training load is a Garmin index, not a measurement — the stored float
    carries ~13 meaningless decimals. Round it."""
    return f"{round(v)}" if v else "—"


_FORMATTERS = {
    "distance": _fmt_distance, "pace": _fmt_pace,
    "hr": _fmt_hr, "load": _fmt_load, "continuity": _fmt_ratio,
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
    bare letter loses.

    ONE grammar: ``{magnitude in the row's own unit} {direction}``, or
    ``"on target"`` inside rounding. The direction vocabulary is per-unit
    ("slower" beats "over" for a pace) but the SHAPE never varies, and the rule
    that makes it coherent is: **a delta is never a percentage.** A live card
    printed four dialects in four rows — ``on target`` / ``5s/mi slower`` /
    ``53% over`` / ``even`` — where the percentages were a percentage of a
    distance, a percentage of a ratio, and a percentage of a percentage. Those
    are three different quantities wearing one symbol, which is the same defect
    the Delta column's "never compare two quantities" contract exists to
    prevent, one level down in the units.

    The two HR branches below are deliberately left on their own text: they
    are correct under this grammar already, and they are being reworked
    elsewhere.
    """
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
    if key == "distance":
        # Was `+N%`. A percentage of a distance is the least useful form of a
        # number a runner already thinks about in miles, and it was the only
        # row on the card whose delta was not in its own unit.
        diff_mi = (units.to_miles(actual) or 0) - (units.to_miles(expected) or 0)
        if abs(diff_mi) < _DISTANCE_ON_TARGET_MI:
            # Within rounding IS on target; say so. The predecessor of this
            # branch printed "-0%" for a 4.00-of-4.00 mi run off by meters,
            # which reads like a typo beside an A+. The threshold is a real
            # tolerance rather than a display epsilon: 5.01 against a 5.00
            # prescription is 16 m, which is treadmill rounding, and printing
            # "0.01 mi long" for it invents a miss the grade did not find.
            return "on target"
        return f"{abs(diff_mi):.2f} mi {'long' if diff_mi > 0 else 'short'}"
    if key == "continuity":
        # A percentage against the tolerance would read as "+10%" for a run whose
        # worst mile was 27% off its median — the tolerance is not the quantity
        # the reader cares about. State the gap in units of a median split,
        # which is what both `actual` and `expected` are already measured in.
        excess = actual - expected
        if abs(excess) < 0.005:
            return "on target"
        return f"{abs(excess):.2f}x {'over' if excess > 0 else 'under'}"
    if key == "hr" and metric.get("in_band"):
        # Inside the band IS the A. A percentage against one edge would imply
        # a miss that did not happen.
        return "in range"
    if key == "hr" and metric.get("governing_axis") == "exceedance":
        # The average is not the graded quantity here, so its percentage is not
        # the delta — printing "-1%" beside an F stated a gap of the wrong sign
        # on the wrong axis. State the excess past the noise floor, in bpm,
        # which is actual minus expected on the axis that was graded — the same
        # shape continuity uses against its tolerance.
        excess = metric.get("exceedance_bpm", 0.0) - HR_CAP_NOISE_BPM
        return f"{excess:+.1f} bpm"
    if key == "hr":
        # The rolling-band case, reached only when the run sat OUTSIDE the band
        # (in_band returns above). bpm, not a percentage: the Expected column
        # beside it is already in bpm, and "+4%" next to "≤ 141 bpm" made the
        # reader do the arithmetic to find out it meant 6 beats.
        diff = round(actual) - round(expected)
        if diff == 0:
            return "on target"
        return f"{abs(diff)} bpm {'over' if diff > 0 else 'under'}"
    if not expected:
        return "—"
    # Anything with no unit of its own (today: nothing rendered — load is a
    # stimulus metric and has no table row). Kept as a percentage rather than
    # deleted so a future metric degrades to something readable.
    pct = (actual / expected - 1) * 100
    if round(pct) == 0:
        return "on target"
    return f"{pct:+.0f}%"


def _fmt_minutes(seconds: float | None) -> str:
    """Whole minutes, for the zone breakdown. Sub-minute reads "<1m" rather than
    "0m", which looks like the zone was never entered."""
    if not seconds:
        return "0m"
    minutes = seconds / 60
    return f"{minutes:.0f}m" if minutes >= 1 else "<1m"


#: How the intent reads in the stimulus line — "typical for an easy day" is the
#: honest gloss on `LOAD_FACTORS[cls] * median`, which is what `expected` is.
_INTENT_PROSE = {"easy": "an easy day", "long": "a long run",
                 "quality": "a quality day", "steady": "your median day"}


def has_stimulus(card: dict) -> bool:
    """Whether there is anything to REPORT — deliberately not "is there a level".

    An activity with load but no comparable history still has facts worth
    stating; it just cannot be called low or high. Only a card with nothing at
    all renders nothing, because the PDF's density ladder has no content to drop
    and an empty section would cost vertical space for no information.
    """
    stim = card.get("stimulus") or {}
    return bool(stim.get("load") or stim.get("aerobic_te") is not None
                or stim.get("zones"))


def stimulus_rows(card: dict) -> list[list[str]]:
    """The Signal/Value pairs, shared by the markdown card and the PDF.

    Shared rather than written twice: the two renderers diverged within an hour
    of being written (the PDF dropped the whole section when there was no level
    while the markdown kept it), and a card whose table and PDF disagree is the
    one failure mode `render_report_card_pdf` exists to prevent.
    """
    stim = card.get("stimulus") or {}
    intent_prose = _INTENT_PROSE.get(card.get("intent_class"), "your median day")
    load_text = _fmt_load(stim.get("load"))
    expected = stim.get("expected_load")
    if expected:
        load_text += f" (vs ~{_fmt_load(expected)} typical for {intent_prose})"
    rows = [["Training load", load_text]]
    for key, label in (("aerobic_te", "Aerobic TE"), ("anaerobic_te", "Anaerobic TE")):
        if stim.get(key) is not None:
            rows.append([label, f"{stim[key]:.1f}"])
    zones = stim.get("zones")
    if zones:
        detail = " · ".join(
            f"Z{zone} {_fmt_minutes(secs)}"
            for zone, secs in zones["seconds_by_zone"].items())
        rows.append(["HR zones", f"{detail} — {zones['aerobic_pct']}% aerobic"])
    return rows


def stimulus_notes(card: dict) -> list[str]:
    """Plain-text notes under the stimulus table, shared by both renderers.

    The last one is not optional. The reason there is no letter has to be stated
    ON the card: an unexplained absence reads as an omission, and the single most
    likely misreading is the one the 0.40.0 partition exists to prevent — that a
    LOW number is a bad result.
    """
    stim = card.get("stimulus") or {}
    intent_prose = _INTENT_PROSE.get(card.get("intent_class"), "your median day")
    notes = []
    if stim.get("spike"):
        notes.append("Spike — more than double your median day.")
    if not stim.get("level"):
        notes.append("Not enough comparable history to say whether that is high "
                     "or low for this intent.")
    notes.append(
        f"Not graded. Training load is intensity x duration, so running "
        f"{intent_prose} correctly is supposed to bank less of it — it is "
        "reported here and cannot affect the grade above.")
    return notes


METRIC_TABLE_HEADERS = ["Metric", "Actual", "Expected", "Delta", "Grade"]


def metric_table(card: dict) -> tuple[list[str], list[list[str]]]:
    """``(headers, rows)`` for the graded-compliance table.

    Shared by the markdown card and the PDF for the same reason ``split_table``
    and ``stimulus_rows`` are. The *cells* were already single-sourced — both
    renderers called ``actual_text`` / ``expected_text`` / ``_delta_text`` — but
    the header row, the column set and the row-assembly loop were written out
    twice, which is exactly the shape of the ``split_table`` divergence: a
    column added or dropped in one renderer and not the other.

    Rows are ordered by ``_METRIC_LABELS``, and the grade is always the LAST
    cell — the PDF colours that column by grade band, so its position is part
    of the contract rather than an accident of ordering.
    """
    rows = []
    for key, label in _METRIC_LABELS:
        # `.get`, not `[key]`: a card STORED before a metric existed has no
        # entry for it (`continuity` landed in 0.40.0), and `get_report_card`
        # hands those rows straight back. A missing metric renders as n/a
        # rather than raising KeyError halfway through a render.
        m = card["metrics"].get(key) or {}
        rows.append([
            label,
            actual_text(key, m),
            expected_text(key, m),
            _delta_text(key, m),
            m.get("grade") or "n/a",
        ])
    return list(METRIC_TABLE_HEADERS), rows


def metric_notes(card: dict) -> list[tuple[str, str]]:
    """``(label, note)`` for every graded metric carrying a note, in table
    order. Both renderers built this list inline from ``_METRIC_LABELS``."""
    return [(label, (card["metrics"].get(key) or {})["note"])
            for key, label in _METRIC_LABELS
            if (card["metrics"].get(key) or {}).get("note")]


def split_table(card: dict) -> tuple[list[str], list[list[str]]]:
    """``(headers, rows)`` for the per-split breakdown, with dead columns gone.

    Shared by the markdown card and the PDF for the same reason
    ``stimulus_rows`` is: the two renderers had this table written out twice,
    and a column dropped in one and kept in the other is precisely the
    divergence ``render_report_card_pdf`` takes a pre-built card to avoid.

    Elevation and heart rate are both frequently absent, and the table used to
    print a full column of em-dashes for them regardless. Measured over the
    live DB: **13 of 15 stored cards have an entirely empty Elev column**, and
    362 of 428 ``activity_splits`` rows (85%) carry no elevation at all — a
    treadmill run never has any. On a page whose density ladder was already
    bottoming out, that is ~15% of the table's width spent on nothing.

    A column survives if ANY row has a value for it. `vs run` additionally
    needs the activity average to subtract from, and it is kept whenever it
    has data — it is what makes a hot mile visible at a glance, which is not
    something to make the reader do by hand.
    """
    splits = card.get("splits") or {}
    rows = splits.get("rows") or []
    unit = splits.get("unit", "Lap")
    avg_hr = (card.get("activity") or {}).get("avg_hr")

    has_pace = any(r.get("pace_min_per_mi") for r in rows)
    has_hr = any(r.get("avg_hr") for r in rows)
    has_elev = any(r.get("elevation_gain_meters") is not None for r in rows)
    has_vs = has_hr and bool(avg_hr)

    headers = [unit]
    if has_pace:
        headers.append("Pace")
    if has_hr:
        headers.append("Avg HR")
    if has_vs:
        headers.append("vs run")
    if has_elev:
        headers.append("Elev")

    out = []
    for r in rows:
        # The label already carries the partial lap's distance, which is why
        # there is no Distance column: for every full lap it would read
        # "1.00 mi" beside a column literally headed "Mile".
        cells = [f"{unit} {r['index']}" if not r.get("partial")
                 else f"final {r['distance_mi']:.2f} mi"]
        hr = r.get("avg_hr")
        elev = r.get("elevation_gain_meters")
        if has_pace:
            cells.append(f"{r['pace_min_per_mi']}/mi" if r.get("pace_min_per_mi") else "—")
        if has_hr:
            cells.append(f"{hr} bpm" if hr else "—")
        if has_vs:
            cells.append(f"{hr - avg_hr:+d}" if hr else "—")
        if has_elev:
            cells.append(f"{elev:.0f} m" if elev is not None else "—")
        out.append(cells)
    return headers, out


def stimulus_heading(card: dict, *, markdown: bool = True) -> str:
    """"Stimulus: LOW — as intended", or a bare "Stimulus" with no level.

    ``markdown=False`` drops the emphasis for the PDF, whose HTML is escaped
    rather than rendered — the same convention ``reference_line`` uses.
    """
    stim = card.get("stimulus") or {}
    level = stim.get("level")
    as_intended = stim.get("as_intended")
    em = "**" if markdown else ""
    # `as_intended is None` means there was no expectation to compare against, so
    # neither claim is assertable and the qualifier is omitted entirely.
    if as_intended is True:
        qualifier = " — as intended"
    elif as_intended is False:
        qualifier = f" — {em}higher than intended{em}"
    else:
        qualifier = ""
    return f"Stimulus: {level}{qualifier}" if level else "Stimulus"


def stimulus_lines(card: dict) -> list[str]:
    """The Stimulus section: numbers and a descriptor, and NO grade column.

    Kept structurally separate from the compliance table rather than rendered as
    a 4th row with "n/a" in the Grade column, because "n/a" reads as *we could
    not grade this* — a missing measurement. Load is not missing; it is
    deliberately not judged, and a reader has to be able to tell those apart.
    """
    if not has_stimulus(card):
        return []
    return [
        f"## {stimulus_heading(card)}",
        "",
        render.render_table(["Signal", "Value"], stimulus_rows(card)),
        "",
        *(f"- {n}" for n in stimulus_notes(card)),
    ]


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
            # Only blame the thin pool for metrics that ACTUALLY use the pool.
            # Continuity measures a run against its own splits and a by-feel
            # pace has no target at all, so listing either under "only 2
            # comparable activities in the last 60 days" states a cause that
            # isn't theirs — and implies more history would unlock a grade it
            # would not.
            ungraded = _prose_list([
                _METRIC_PROSE[k] for k, _ in _METRIC_LABELS
                if not card["metrics"].get(k, {}).get("grade")
                and (card["metrics"].get(k, {}).get("reference")
                     in (None, "rolling_60d", "insufficient_data"))])
            if not ungraded:
                return plan_sentence
            return (f"{plan_sentence} {ungraded} ungraded — only {n} comparable "
                    f"{'activity' if n == 1 else 'activities'} in the last "
                    f"{REFERENCE_WINDOW_DAYS} days (need {MIN_REFERENCE_ACTIVITIES}).")
        # Training load dropped out of this sentence in 0.40.0: it is no longer
        # graded, so naming its yardstick here implied a judgment the card does
        # not make. The Stimulus section states its own comparison instead.
        hr_ref = (card["metrics"].get("hr") or {}).get("reference") or ""
        if hr_ref.startswith("plan"):
            return f"{plan_sentence}{widened}"
        return (f"{plan_sentence} HR has no plan target, so it uses your "
                f"60-day median of {ref.get('n', 0)} {pool} activities.{widened}")
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

    headers, rows = metric_table(card)
    lines.append(render.render_table(headers, rows))

    notes = [f"- {label}: {note}" for label, note in metric_notes(card)]
    # The load spike note moved to the Stimulus section in 0.40.0 — it is a
    # statement about stimulus, and leaving it under the graded table was half
    # the reason a low-load easy day read as a failure.
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

    stimulus = stimulus_lines(card)
    if stimulus:
        lines += ["", *stimulus]

    lines += ["", f"## Per-{card['splits']['unit'].lower()} breakdown", ""]
    if not card["splits"]["available"]:
        lines.append(
            "_No per-mile splits recorded for this activity. Splits are captured "
            "only by the daily sync path — backfilled activities have none._")
    else:
        headers, split_rows = split_table(card)
        lines.append(render.render_table(headers, split_rows))
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
    # Stimulus input, not a grade input — an activity with no zone rows (the
    # pre-sync backfilled history) simply renders without the zone line.
    hr_zones = [dict(r) for r in conn.execute(
        "SELECT zone, seconds_in_zone FROM activity_hr_zones "
        "WHERE activity_id = ? ORDER BY zone", (aid,)
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
        "hr_zones": hr_zones,
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
