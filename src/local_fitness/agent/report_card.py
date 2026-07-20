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

Direction gating is what keeps the rubric honest. An easy run is *supposed*
to be slow — grading |actual − expected| would hand every recovery run an F.
So easy/long days are penalized only for running too FAST, quality days only
for running too SLOW, and each metric's expectation is scaled by the workout's
intent (from the plan when present, inferred otherwise).

Splits are presentation-only. No grade reads ``activity_splits`` — only 87 of
747 activities have them (they are written by the daily-sync ingest path,
never by backfill), so a splits-dependent grade would be unavailable on ~88%
of the history and would silently mean different things on different rows.

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
from . import render, units

__all__ = [
    "grade_from_deviation", "intent_class", "infer_intent", "resolve_intent",
    "distance_deviation", "pace_deviation", "hr_deviation", "load_deviation",
    "overall_grade", "label_splits", "hr_drift_pct", "build_card",
    "render_markdown", "reference_line", "load_report_card_inputs",
    "rolling_reference",
]

# 1 international mile, exactly — matches units.py's constant.
MILE_M = 1609.344
# How close a lap must be to a mile before we call the column "Mile" rather
# than "Lap". Garmin auto-lap lands within a few meters; a manual-lap workout
# can be anything, and mislabeling laps as miles is a lie on the card.
MILE_TOLERANCE = 0.03
# Below this many comparable activities in the window, the median is noise.
# Grading against noise is worse than not grading — return n/a and say so.
MIN_REFERENCE_ACTIVITIES = 5
REFERENCE_WINDOW_DAYS = 60
# Advisory only: a 2x-median load day is a fact worth printing, not an F.
LOAD_SPIKE_FACTOR = 2.0

# The one band table. `d` is a non-negative relative deviation; every metric
# reduces to one, which is why there is exactly one grader.
GRADE_BANDS: tuple[tuple[float, str], ...] = (
    (0.05, "A"), (0.10, "B"), (0.20, "C"), (0.35, "D"),
)
GRADE_POINTS = {"A": 4.0, "B": 3.0, "C": 2.0, "D": 1.0, "F": 0.0}
METRIC_WEIGHTS = {"distance": 0.30, "pace": 0.30, "hr": 0.25, "load": 0.15}
_GPA_CUTS: tuple[tuple[float, str], ...] = (
    (3.5, "A"), (2.5, "B"), (1.5, "C"), (0.5, "D"),
)

# Intent scaling. An easy day is EXPECTED to be shorter and slower than the
# median; a long day longer. Applied to the rolling reference only — a plan
# states its own targets and needs no scaling.
DISTANCE_FACTORS = {"easy": 0.75, "long": 1.40, "quality": 1.00, "steady": 1.00}
# Training load tracks distance closely enough at a fixed intent that a second,
# independently-tuned table would be false precision — deliberately the same
# numbers, named separately so they can diverge if evidence ever says they should.
LOAD_FACTORS = dict(DISTANCE_FACTORS)
PACE_FACTORS = {"easy": 1.10, "long": 1.05, "quality": 0.95, "steady": 1.00}
# (floor, ceiling) as fractions of median HR; None = unbounded on that side.
# HR is graded on appropriateness to intent, never "lower is better".
HR_BANDS: dict[str, tuple[float | None, float | None]] = {
    "easy": (None, 0.88),
    "long": (None, 0.93),
    "quality": (1.02, None),
    "steady": (0.95, 1.05),
}
# A steady/unknown-intent run has no stated target, so its two-sided pace
# bands are widened rather than held to prescription-grade tolerance.
STEADY_WIDEN = 1.5

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
      the rule that stops recovery runs from failing.
    - quality (tempo/interval/race): penalized only for being too SLOW.
      Beating the target is an A, uncapped.
    - steady / unknown: two-sided, with bands widened by ``STEADY_WIDEN`` at
      the grading step.
    """
    if not actual_sec_per_km or not expected_sec_per_km or expected_sec_per_km <= 0:
        return None
    if cls in ("easy", "long"):
        return max(0.0, (expected_sec_per_km - actual_sec_per_km) / expected_sec_per_km)
    if cls == "quality":
        return max(0.0, (actual_sec_per_km - expected_sec_per_km) / expected_sec_per_km)
    return abs(actual_sec_per_km / expected_sec_per_km - 1.0)


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
    """One-sided-low against the intent-scaled rolling median. Above the
    expectation is an A — a big day is not a failure.

    ``expected_load`` is intent-scaled by the caller for the same reason pace
    is direction-gated: an easy day is SUPPOSED to bank less load, and grading
    it against the unscaled median handed a prescribed 3-mile recovery run a D
    on live data (2026-07-19). ``plan_workouts`` has no load column, so this
    metric never has a plan reference to use.
    """
    if not load or not expected_load or expected_load <= 0:
        return None
    return max(0.0, 1.0 - load / expected_load)


def overall_grade(metrics: dict[str, dict]) -> dict:
    """Weighted GPA over gradeable metrics only.

    An ``n/a`` metric drops out and its weight redistributes proportionally,
    so a by-feel plan day with no pace target isn't silently scored as if pace
    were worth 30% of nothing. Zero gradeable metrics yields "n/a" — never
    "F", which would read as a judgment we did not actually make.
    """
    pairs = [
        (METRIC_WEIGHTS[k], GRADE_POINTS[base_letter(m["grade"])])
        for k, m in metrics.items()
        if m.get("grade") and k in METRIC_WEIGHTS
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
    return {"grade": letter, "gpa": round(gpa, 2), "graded_metrics": len(pairs)}


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
) -> dict:
    """Assemble the full report card. Pure — every input is a plain dict, so
    the whole rubric is testable without a DB."""
    intent, intent_source = resolve_intent(activity, plan_workout, reference)
    cls = intent_class(intent)
    has_rolling = reference.get("mode") == "rolling_60d"
    # A rest-day prescription carries null targets; it is an intent signal
    # only, so distance/pace fall through to the rolling reference.
    plan_usable = bool(plan_workout) and (plan_workout or {}).get("type") != "rest"

    # -- distance
    if plan_usable and plan_workout.get("target_distance_m"):
        target = plan_workout["target_distance_m"]
        d = distance_deviation(activity.get("distance_meters"), target, two_sided=True)
        distance = _metric(
            grade_from_deviation(d), activity.get("distance_meters"), target, d, "plan")
    elif has_rolling:
        expected = DISTANCE_FACTORS[cls] * (reference.get("median_distance_m") or 0)
        d = distance_deviation(activity.get("distance_meters"), expected, two_sided=False)
        distance = _metric(
            grade_from_deviation(d), activity.get("distance_meters"), expected or None,
            d, "rolling_60d")
    else:
        distance = _metric(
            None, activity.get("distance_meters"), None, None, reference.get("mode"))

    # -- pace
    if plan_usable and plan_workout.get("target_pace_sec_per_km"):
        expected_pace = plan_workout["target_pace_sec_per_km"]
        pace_ref, widen = "plan", 1.0
    elif plan_usable and plan_workout.get("target_distance_m"):
        # A prescribed distance with no pace is an explicit by-feel day. It
        # earns no pace grade at all, and its 30% weight redistributes.
        expected_pace, pace_ref, widen = None, "plan (by feel)", 1.0
    elif has_rolling and reference.get("median_pace_sec_per_km"):
        expected_pace = PACE_FACTORS[cls] * reference["median_pace_sec_per_km"]
        pace_ref = "rolling_60d"
        widen = STEADY_WIDEN if cls == "steady" else 1.0
    else:
        expected_pace, pace_ref, widen = None, reference.get("mode"), 1.0
    d = pace_deviation(activity.get("avg_pace_sec_per_km"), expected_pace, cls)
    pace = _metric(
        grade_from_deviation(d, widen), activity.get("avg_pace_sec_per_km"),
        expected_pace, d, pace_ref,
        note="by-feel day — no pace target" if pace_ref == "plan (by feel)" else None)

    # -- HR and load: always rolling. plan_workouts has neither column.
    med_hr = reference.get("median_hr") if has_rolling else None
    d = hr_deviation(activity.get("avg_hr"), med_hr, cls)
    hr = _metric(
        grade_from_deviation(d), activity.get("avg_hr"), med_hr, d,
        "rolling_60d" if has_rolling else reference.get("mode"))

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
        "overall": overall_grade(metrics),
        "splits": label_splits(splits),
        "context": context or {},
    }


# --- markdown rendering ----------------------------------------------------

_METRIC_LABELS = [
    ("distance", "Distance"), ("pace", "Pace"),
    ("hr", "Avg HR"), ("load", "Training Load"),
]


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


def _delta_text(key: str, metric: dict) -> str:
    """Signed, human delta between actual and expected — the granularity a
    bare letter loses."""
    actual, expected = metric.get("actual"), metric.get("expected")
    if actual is None or expected is None or not isinstance(expected, (int, float)):
        return "—"
    if key == "pace":
        diff = round((actual - expected) * 1.609344)
        if abs(diff) < 1:
            return "on target"
        return f"{abs(diff)}s/mi {'slower' if diff > 0 else 'faster'}"
    if not expected:
        return "—"
    return f"{(actual / expected - 1) * 100:+.0f}%"


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
    if mode == "insufficient_data":
        return (f"Not enough comparable history to grade — {ref.get('n', 0)} similar "
                f"activities in the last {REFERENCE_WINDOW_DAYS} days "
                f"(need {MIN_REFERENCE_ACTIVITIES}).")
    intent_src = ("prescribed by your plan" if card["intent_source"] == "plan"
                  else "inferred from the run itself")
    pool = ref.get("pool", "")
    widened = (" Pool widened to all on-foot activities — too few of this exact "
               "type to compare against." if ref.get("widened") else "")
    if any((m.get("reference") or "").startswith("plan") for m in card["metrics"].values()):
        return (f"Graded against your {em}training plan{em} for this date "
                f"(intent: {card['intent']}, {intent_src}). HR and training load "
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
        f"## Overall: {overall['grade']}{gpa}",
        "",
        reference_line(card),
        "",
    ]

    rows = []
    for key, label in _METRIC_LABELS:
        m = card["metrics"][key]
        fmt = _FORMATTERS[key]
        rows.append([
            label,
            fmt(m.get("actual")),
            fmt(m.get("expected")) if m.get("expected") is not None else "—",
            _delta_text(key, m),
            m.get("grade") or "n/a",
        ])
    lines.append(render.render_table(
        ["Metric", "Actual", "Expected", "Delta", "Grade"], rows))

    notes = [f"- {label}: {card['metrics'][key]['note']}"
             for key, label in _METRIC_LABELS if card["metrics"][key].get("note")]
    if card["metrics"]["load"].get("spike"):
        notes.append("- Training Load: **spike** — more than double your median day.")
    if notes:
        lines += ["", *notes]

    lines += ["", f"## Per-{card['splits']['unit'].lower()} breakdown", ""]
    if not card["splits"]["available"]:
        lines.append(
            "_No per-mile splits recorded for this activity. Splits are captured "
            "only by the daily sync path — backfilled activities have none._")
    else:
        avg_hr = act.get("avg_hr")
        split_rows = []
        for r in card["splits"]["rows"]:
            label = (f"{card['splits']['unit']} {r['index']}" if not r["partial"]
                     else f"final {r['distance_mi']:.2f} mi")
            hr = r.get("avg_hr")
            elev = r.get("elevation_gain_meters")
            split_rows.append([
                label,
                f"{r['distance_mi']:.2f} mi" if r["distance_mi"] is not None else "—",
                f"{r['pace_min_per_mi']}/mi" if r.get("pace_min_per_mi") else "—",
                f"{hr} bpm" if hr else "—",
                f"{hr - avg_hr:+d}" if hr and avg_hr else "—",
                f"{elev:.0f} m" if elev is not None else "—",
            ])
        lines.append(render.render_table(
            [card["splits"]["unit"], "Distance", "Pace", "Avg HR", "vs run", "Elev"],
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

    atype = activity.get("activity_type")
    rows = [r for r in raw if _exact_type(atype)(r["activity_type"])]
    pool, widened = (atype or "").lower() or "comparable", False
    if len(rows) < MIN_REFERENCE_ACTIVITIES:
        wider = [r for r in raw if _class_type(atype)(r["activity_type"])]
        if len(wider) > len(rows):
            rows, pool, widened = wider, "on-foot", True

    if len(rows) < MIN_REFERENCE_ACTIVITIES:
        return {"mode": "insufficient_data", "n": len(rows),
                "pool": pool, "window_days": REFERENCE_WINDOW_DAYS}
    return {
        "mode": "rolling_60d",
        "n": len(rows),
        "pool": pool,
        "widened": widened,
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
    """
    if activity_id is not None:
        return conn.execute(
            "SELECT * FROM activities WHERE activity_id = ?", (activity_id,)
        ).fetchone()
    if target_date:
        return conn.execute(
            "SELECT * FROM activities WHERE date = ? AND distance_meters > 0 "
            "AND duration_seconds > 0 ORDER BY start_time DESC LIMIT 1",
            (target_date,),
        ).fetchone()
    return conn.execute(
        "SELECT * FROM activities WHERE distance_meters > 0 AND duration_seconds > 0 "
        "ORDER BY date DESC, start_time DESC LIMIT 1"
    ).fetchone()


def load_report_card_inputs(
    conn: sqlite3.Connection,
    activity_id: int | None = None,
    target_date: str | None = None,
) -> dict | None:
    """Every input ``build_card`` needs, or ``None`` when no activity matches."""
    row = _select_activity(conn, activity_id, target_date)
    if row is None:
        return None
    activity = dict(row)
    activity.pop("raw_json", None)
    aid = activity["activity_id"]

    splits = [dict(r) for r in conn.execute(
        "SELECT * FROM activity_splits WHERE activity_id = ? ORDER BY split_index", (aid,)
    ).fetchall()]

    plan_workout = None
    active = plans.get_active_plan(conn=conn)
    if active:
        same_day = [w for w in active.get("workouts", []) if w.get("date") == activity["date"]]
        if same_day:
            # (plan_id, date, seq) is unique; the lowest seq is the day's
            # primary session when a double-day is prescribed.
            plan_workout = min(same_day, key=lambda w: w.get("seq") or 0)

    context: dict = {}
    base = conn.execute(
        "SELECT ctl, atl, tsb FROM baselines WHERE date = ?", (activity["date"],)
    ).fetchone()
    if base is not None:
        context = {k: base[k] for k in ("ctl", "atl", "tsb")}

    return {
        "activity": activity,
        "splits": splits,
        "plan_workout": plan_workout,
        "reference": rolling_reference(conn, activity),
        "context": context,
        "other_activities_on_date": [
            r["activity_id"] for r in conn.execute(
                "SELECT activity_id FROM activities WHERE date = ? AND activity_id != ?",
                (activity["date"], aid),
            ).fetchall()
        ],
    }
