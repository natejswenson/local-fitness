"""Advisory grounding signal for the brief generator (agent/code separation).

Runs ONCE on the complete Brief, AFTER the stream, AFTER validation — it is a
MEASUREMENT, never a gate. It never raises, never rejects a brief, never drops a
takeaway, never reprompts. A single-turn toolless generator has no corrective
round-trip; this exists to surface an *invention-rate signal* (logged per brief,
checked in the shadow-run), not to certify every number.

How it works (resolves design F1/F2):
- The generator is toolless, so every number it may legitimately cite is present
  in the BriefContext. We build the UNION of citable numbers from the context's
  GroundedValue ``display`` renderings (snapshot ∪ training_load ∪ trends ∪
  candidates[].metrics) plus the scalar context numbers (step goal, days-to-race).
- For each numeric token in the prose, we find the nearest known number by
  RELATIVE distance:
    * within the EXACT band  → a faithful citation (or a correctly-converted
      miles/pace/duration token that equals its display) → fine.
    * within the NEARBY band but unequal → a token that *looks like* a known
      metric but is off → a likely corrupted metric value → FLAG it.
    * beyond NEARBY → unrelated quantity (a prescription "45 min", a date, a
      goal) → ignored. Contradiction-only: no nearby metric ⇒ no flag.

This deliberately catches SUBTLE corruption near a real value, not wild numbers
(a wildly different number reads as a different quantity, not a mis-stated
metric). An occasional false positive is tolerable noise in an advisory signal.
"""
from __future__ import annotations

import logging
import re

from pydantic import BaseModel

from .schemas import Brief, BriefContext, GroundedValue

_LOG = logging.getLogger(__name__)

# Any number in prose: optional sign, digit groups with commas, optional
# decimal, optional k/% suffix. Catches "56", "-22", "+3.2", "11,000", "9.2k",
# "120%" (and "7h 12m" as 7 then 12, "45-60" as 45 then -60).
_NUM_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?\s*[kK%]?")

# A number immediately followed by a time-window word ("14 days", "7-day",
# "two-week") is a window, never a metric claim — skip it. (Coach prose is full
# of "14 days" / "60-day baseline", which would otherwise collide with metric
# magnitudes and produce false positives.)
_WINDOW_AFTER = re.compile(r"[\s-]*(?:day|week|month|year)s?\b", re.IGNORECASE)

# A written duration like "7h 30m" (this is literally `_hm()`'s own render
# for sleep_seconds — see brief_planner.py — so real coach prose says this
# constantly) is a composite of TWO numbers glued to their unit letters;
# neither half is a standalone metric citation. Measured: with signed
# sign-mismatch matching (below), the bare "30" out of "7h 30m" landed at
# rel 0.27 against an unrelated -22 TSB pool entry — comfortably inside the
# sign-mismatch band — and was flagged as a false sign inversion. Skip both
# halves the same way a time-window is skipped.
_HOUR_MIN_DURATION = re.compile(r"\b\d+h\s*\d+m\b")


def _in_duration_fragment(text: str, start: int, end: int) -> bool:
    """True when the ``[start, end)`` token span sits inside an "Nh Mm"
    duration composite found anywhere in ``text``."""
    return any(
        dm.start() <= start and end <= dm.end()
        for dm in _HOUR_MIN_DURATION.finditer(text)
    )

# Per the design: relative bands. A token within EXACT of a known number counts
# as faithful; within NEARBY (but past EXACT) is a close-but-unequal mis-state;
# beyond NEARBY is a different quantity and is ignored. ABS_FLOOR keeps tiny
# absolute diffs on small values (3.2 vs 3.3) from reading as contradictions.
_EXACT_REL = 0.03
_NEARBY_REL = 0.12
_ABS_FLOOR = 0.5

# Sign-mismatch is checked at a WIDER band than NEARBY_REL — a flipped TSB
# ("+40.0" cited when the real value is -22.4, rel 0.44) is still the "same
# metric, wrong sign" and must flag even though 0.44 is well past the normal
# NEARBY cutoff (a sign flip is qualitatively wrong regardless of magnitude
# drift). But unbounded is worse: nearest-by-magnitude ALWAYS returns
# something, even when nothing in the pool is remotely close, so an
# unrelated small number ("7" from "7h 30m") landing near the *magnitude* of
# an unrelated negative metric (tsb=-22, rel 0.68) is not a sign inversion of
# anything — it's noise. 0.5 catches the measured true case (0.44) while
# excluding the measured false ones (0.68, 0.84) — see
# test_sign_mismatch_flags_even_far_from_nearby_band /
# test_sign_check_does_not_fire_on_an_unrelated_distant_number.
_SIGN_MISMATCH_REL = 0.5


class GroundingFlag(BaseModel):
    """One prose number that looks like a known metric but doesn't match it.

    ``kind`` distinguishes two failure modes that read very differently:
    ``"value"`` is the original meaning (a magnitude mis-state, e.g. RHR
    53 cited when it's really 58); ``"sign"`` is a SIGN inversion (e.g. TSB
    "+22.4" cited when it's really -22.4) — magnitude-only matching missed
    these entirely (abs(+22.4) == abs(-22.4), so it read as an exact,
    faithful citation) even though a positive-vs-negative TSB is the
    difference between "you're rested, go hard" and "very fatigued, back
    off". Defaults to ``"value"`` so existing callers that never set it
    (``plan_coach.ground_coaching_line``) are unaffected."""
    takeaway_index: int
    token: str
    nearest_metric: str
    delta: float          # prose value − nearest known value (both SIGNED)
    kind: str = "value"


def _parse(token: str) -> float | None:
    """Parse a prose numeric token to a float (handles commas, k, %)."""
    t = token.strip().replace(",", "")
    if not t:
        return None
    mult = 1.0
    if t[-1] in "kK":
        t, mult = t[:-1], 1000.0
    elif t[-1] == "%":
        t = t[:-1]
    try:
        return float(t) * mult
    except ValueError:
        return None


def _is_percent_token(token: str) -> bool:
    """True when a numeric token is percent-suffixed ("147%")."""
    return token.rstrip().endswith("%")


def _union(context: BriefContext) -> list[GroundedValue]:
    """Every citable GroundedValue — the exact set the prose may draw from.

    Was snapshot ∪ training_load ∪ trends ∪ candidates[].metrics only —
    ``workouts_14d``, ``anomalies``, and ``plan_today`` are ALSO rendered
    into the prompt verbatim (``prompts._render_context_block`` dumps the
    whole ``BriefContext``), so a genuine citation from one of those (e.g.
    "8mi at HR 132" from a recent workout) had no pool entry to match and
    was unciteable by construction. Those three carry plain dicts, not
    ``GroundedValue``s, so they're pooled separately in ``_grounded_pool``
    via ``_workout_pool_entries`` / ``_plan_today_pool_entries`` /
    ``_anomaly_pool_entries`` rather than being coerced into this list."""
    out: list[GroundedValue] = []
    out.extend(context.snapshot)
    out.extend(context.training_load)
    out.extend(context.trends)
    for c in context.candidates:
        out.extend(c.metrics)
    return out


_METERS_PER_MILE = 1609.344


def _workout_pool_entries(w: dict) -> list[tuple[float, str, str]]:
    """(signed value, name, unit) leaves from one ``workouts_14d`` entry.

    These fields are already coach-ready (miles/bpm/load/TE — see
    ``brief_planner._workouts_payload``), so they're pooled as their raw
    numeric value directly. ``pace_min_per_mi`` ("8:42") and ``duration``
    ("45m") are pre-formatted STRINGS, not bare numbers — same reasoning as
    ``_grounded_pool`` no longer scraping a display string (see its
    docstring): a multi-token string isn't a single scalar citation target,
    so it stays unpooled rather than injecting fragments."""
    if not isinstance(w, dict):
        return []
    out: list[tuple[float, str, str]] = []
    if isinstance(w.get("distance_mi"), (int, float)):
        out.append((float(w["distance_mi"]), "workouts_14d.distance_mi", "mi"))
    if isinstance(w.get("avg_hr"), (int, float)):
        out.append((float(w["avg_hr"]), "workouts_14d.avg_hr", "bpm"))
    if isinstance(w.get("training_load"), (int, float)):
        out.append((float(w["training_load"]), "workouts_14d.training_load", "none"))
    if isinstance(w.get("aerobic_te"), (int, float)):
        out.append((float(w["aerobic_te"]), "workouts_14d.aerobic_te", "none"))
    return out


def _plan_today_pool_entries(plan_today: dict | None) -> list[tuple[float, str, str]]:
    """(signed value, name, unit) leaves from ``plan_today`` (mirrors
    ``plans.build_plan_status``'s shape) — today's prescribed distance and
    the plan's adherence percentage, both things the generator is
    explicitly told it may cite ("today's 6mi prescribed run")."""
    if not isinstance(plan_today, dict) or not plan_today.get("active"):
        return []
    out: list[tuple[float, str, str]] = []
    today_w = plan_today.get("today")
    if isinstance(today_w, dict):
        dist_m = today_w.get("target_distance_m")
        if isinstance(dist_m, (int, float)):
            out.append((dist_m / _METERS_PER_MILE, "plan_today.target_distance_mi", "mi"))
    if isinstance(plan_today.get("adherence_pct"), (int, float)):
        out.append((float(plan_today["adherence_pct"]), "plan_today.adherence_pct", "pct"))
    if isinstance(plan_today.get("days_to_race"), (int, float)):
        out.append((float(plan_today["days_to_race"]), "plan_today.days_to_race", "none"))
    return out


# Extend as more anomaly ``metric`` values ship — today ``_rhr_anomalies`` is
# the only producer, so "rhr" is the only known key.
_ANOMALY_UNITS = {"rhr": "bpm"}


def _anomaly_pool_entries(a: dict) -> list[tuple[float, str, str]]:
    """(signed value, name, unit) leaves from one ``anomalies`` entry (see
    ``brief_planner._rhr_anomalies``): the flagged reading itself, plus its
    ``sd_distance`` when present (unit ``"sd"`` — already a defined
    ``GroundedUnit``, e.g. "1.76 SD below baseline")."""
    if not isinstance(a, dict):
        return []
    out: list[tuple[float, str, str]] = []
    val = a.get("value")
    if isinstance(val, (int, float)):
        metric = a.get("metric") or "value"
        out.append((float(val), f"anomalies.{metric}", _ANOMALY_UNITS.get(metric, "none")))
    sd = a.get("sd_distance")
    if isinstance(sd, (int, float)):
        out.append((float(sd), "anomalies.sd_distance", "sd"))
    return out


def _grounded_pool(context: BriefContext) -> list[tuple[float, str, str]]:
    """(signed value, source-name, unit) for every known number the prose
    may cite.

    Two changes from the original design, both measured against the real
    saved-brief corpus (2026-07-27):

    1. Pools each ``GroundedValue``'s raw ``value`` directly — never tokens
       scraped out of its rendered ``display`` string. A multi-number
       display like sleep's "7h 28m" used to inject BARE ``7`` and ``28``
       into the pool via regex, which then collided with unrelated
       same-magnitude metrics (measured: ``intensity_minutes_vigorous=52``
       vs ``rhr_baseline=52``, ``days_to_race=53`` vs ``rhr=53``) — pure
       parsing artifacts, not real ambiguity. The raw seconds value (e.g.
       26,880) doesn't collide with either.
    2. Carries each entry's real ``unit`` (``GroundedValue.unit``, or an
       explicit tag for the workouts_14d/plan_today/anomalies additions)
       instead of a "does the display string contain %" text-sniff. Percent
       and non-percent are the one unit distinction a bare prose token can
       reliably signal (its own "%" suffix) — an HR cap of 140 bpm sitting
       within the NEARBY band of a 147% steps-vs-goal value was a real
       measured false positive (2026-07-19). Finer-grained matching (bpm vs
       mi vs sec) isn't attempted: prose carries no machine-readable unit
       marker for those, so ``flag()`` still pools all non-percent units
       into one shared bucket — tagging them is a foundation for a future,
       smarter classifier, not a claim that one exists today.

    Values are SIGNED (not ``abs``) so ``flag()`` can detect a sign
    inversion (TSB "+22.4" cited when the real value is -22.4) instead of
    reading it as an exact match on magnitude alone."""
    pool: list[tuple[float, str, str]] = []
    for gv in _union(context):
        pool.append((gv.value, gv.name, gv.unit))
    for w in context.workouts_14d:
        pool.extend(_workout_pool_entries(w))
    pool.extend(_plan_today_pool_entries(context.plan_today))
    for a in context.anomalies:
        pool.extend(_anomaly_pool_entries(a))
    # Scalar context numbers that are legitimate to cite but aren't GroundedValues.
    if context.step_goal is not None:
        pool.append((float(context.step_goal), "step_goal", "steps"))
    if context.days_to_race is not None:
        pool.append((float(context.days_to_race), "days_to_race", "none"))
    return pool


def _nearest(value: float, pool: list[tuple[float, str]]) -> tuple[float, str]:
    """Nearest pool entry to ``value`` by relative distance. Pool is non-empty.

    UNSIGNED contract: both ``value`` and every pool entry are assumed
    already-abs magnitudes — this is the original public-facing shape
    (``nearest_pool_match`` / ``classify_against_pool``), still used as-is
    by ``plan_coach.ground_coaching_line``'s abs-valued pool. ``flag()``
    itself no longer calls this — see ``_nearest_signed`` — so that a sign
    inversion isn't silently treated as an exact match."""
    best_rel, best_val, best_name = float("inf"), 0.0, ""
    for val, name in pool:
        denom = max(value, val, 1.0)
        rel = abs(value - val) / denom
        if rel < best_rel:
            best_rel, best_val, best_name = rel, val, name
    return best_val, best_name


def _nearest_signed(ax: float, pool: list[tuple[float, str]]) -> tuple[float, str]:
    """Nearest pool entry to a prose token's ABS magnitude ``ax``, matched by
    magnitude (so a sign-flipped exact match is still FOUND, not skipped
    over) but returning the pool's ORIGINAL SIGNED value — the whole reason
    ``_grounded_pool`` stores signed values now. ``flag()`` uses this,
    instead of ``_nearest``, to tell a same-sign near-miss (a magnitude
    mis-state) apart from an opposite-sign match (a sign inversion) against
    the SAME nearest metric. Pool is non-empty."""
    best_rel, best_val, best_name = float("inf"), 0.0, ""
    for val, name in pool:
        aval = abs(val)
        denom = max(ax, aval, 1.0)
        rel = abs(ax - aval) / denom
        if rel < best_rel:
            best_rel, best_val, best_name = rel, val, name
    return best_val, best_name


def parse_number(token: str) -> float | None:
    """Public wrapper around the internal numeric-token parser (handles
    commas, k, %). Re-exported so other modules that ground LLM prose against
    a pool of known numbers (e.g. ``plan_coach.ground_coaching_line``) reuse
    this exact parsing instead of importing the underscore-prefixed
    ``_parse`` cross-module."""
    return _parse(token)


def numeric_tokens(text: str) -> list[str]:
    """Numeric tokens in ``text``, in order, skipping any token immediately
    followed by a time-window word ("14 days", "7-day", "two-week") — those
    are windows, not metric citations (see ``_WINDOW_AFTER``) — and any token
    that's half of an "Nh Mm" duration composite (see ``_HOUR_MIN_DURATION``).
    Public re-export of ``flag()``'s tokenizing step so other callers scan
    prose identically."""
    tokens: list[str] = []
    for m in _NUM_RE.finditer(text):
        if _WINDOW_AFTER.match(text, m.end()):
            continue
        if _in_duration_fragment(text, m.start(), m.end()):
            continue
        tokens.append(m.group())
    return tokens


def nearest_pool_match(value: float, pool: list[tuple[float, str]]) -> tuple[float, str]:
    """Public wrapper around the internal nearest-match search. ``pool`` must
    be non-empty (callers guard with an empty-pool check first — see
    ``flag()``'s ``if not pool: return []`` and its mirror in
    ``plan_coach.ground_coaching_line``)."""
    return _nearest(value, pool)


def classify_against_pool(
    value: float, pool: list[tuple[float, str]]
) -> tuple[str, float, str]:
    """Classify ``value`` against ``pool`` using the same relative-distance
    bands ``flag()`` applies inline (grounding.py:150-156): ``"faithful"``
    (within EXACT_REL, or within ABS_FLOOR absolute — a correct citation,
    never flagged), ``"flag"`` (within NEARBY_REL but not EXACT_REL — looks
    like a known metric but is off), or ``"ignore"`` (beyond NEARBY_REL — an
    unrelated quantity, contradiction-only). Returns
    ``(verdict, nearest_value, nearest_name)``. ``pool`` must be non-empty."""
    near_val, near_name = _nearest(value, pool)
    denom = max(value, near_val, 1.0)
    rel = abs(value - near_val) / denom
    if rel <= _EXACT_REL or abs(value - near_val) <= _ABS_FLOOR:
        return "faithful", near_val, near_name
    if rel <= _NEARBY_REL:
        return "flag", near_val, near_name
    return "ignore", near_val, near_name


def flag(brief: Brief, context: BriefContext) -> list[GroundingFlag]:
    """Advisory: prose numbers that look like a known metric but are off.

    Never raises, never mutates the brief. Returns [] when the context carries
    no citable numbers (nothing to contradict).

    Matching is kind-partitioned: a percent-suffixed prose token is compared
    only against percent-valued pool entries, a plain token only against plain
    magnitudes. Cross-kind magnitude collisions (HR 140 vs 147% of step goal)
    are exactly the false positives that pinned invention_rate at 1.0 and made
    the signal useless as a monitor.

    Every prose token and pool value is matched SIGNED, not just by
    magnitude — a token whose ABS magnitude is an exact (or near) match but
    whose SIGN is opposite the nearest metric's is a ``kind="sign"`` flag
    (see ``GroundingFlag``), reported regardless of how close or far the
    magnitudes otherwise are: "TSB +22.4" when the real value is -22.4 was
    previously read as a faithful citation (abs(+22.4) == abs(-22.4)) even
    though the two mean opposite things to a runner. A same-sign near-miss
    (a magnitude mis-state, e.g. RHR 53 cited when it's really 58) is still
    the ``kind="value"`` flag with the SIGNED gap as ``delta`` — the old
    ``delta`` was ``prose − abs(nearest)``, so a -24.0 cited against a real
    -22.4 (a 1.6-unit miss) reported delta -46.4 instead of -1.6."""
    tagged_pool = _grounded_pool(context)
    if not tagged_pool:
        return []
    pct_pool = [(v, n) for v, n, unit in tagged_pool if unit == "pct"]
    plain_pool = [(v, n) for v, n, unit in tagged_pool if unit != "pct"]
    flags: list[GroundingFlag] = []
    for i, tk in enumerate(brief.takeaways):
        text = f"{tk.headline} {tk.summary} {tk.details}"
        for m in _NUM_RE.finditer(text):
            if _WINDOW_AFTER.match(text, m.end()):
                continue  # a time window ("14 days"), not a metric claim
            if _in_duration_fragment(text, m.start(), m.end()):
                continue  # half of "7h 30m" — a composite, not a scalar citation
            tok = m.group()
            x = _parse(tok)
            if x is None:  # pragma: no cover - defensive; _NUM_RE only matches parseable tokens
                continue
            pool = pct_pool if _is_percent_token(tok) else plain_pool
            if not pool:
                continue  # no same-kind numbers to contradict
            ax = abs(x)
            near_val, near_name = _nearest_signed(ax, pool)
            aval = abs(near_val)
            denom = max(ax, aval, 1.0)
            rel = abs(ax - aval) / denom
            if rel <= _SIGN_MISMATCH_REL and ((x > 0 > near_val) or (x < 0 < near_val)):
                # Close enough (within the WIDER sign band) to be "the same
                # metric, wrong sign" — flagged regardless of whether rel
                # also clears the tighter NEARBY_REL check below.
                flags.append(GroundingFlag(
                    takeaway_index=i, token=tok.strip(), nearest_metric=near_name,
                    delta=round(x - near_val, 2), kind="sign"))
                continue
            if rel <= _EXACT_REL or abs(ax - aval) <= _ABS_FLOOR:
                continue                      # faithful citation
            if rel <= _NEARBY_REL:
                flags.append(GroundingFlag(
                    takeaway_index=i, token=tok.strip(), nearest_metric=near_name,
                    delta=round(x - near_val, 2), kind="value"))
            # else: unrelated quantity → ignored (contradiction-only)
    return flags


def invention_rate(brief: Brief, context: BriefContext) -> float:
    """Report metric: fraction of takeaways carrying ≥1 flagged (off) number.
    In [0.0, 1.0]. (Brief enforces ≥1 takeaway, so the denominator is never 0.)"""
    flagged = {f.takeaway_index for f in flag(brief, context)}
    return round(len(flagged) / len(brief.takeaways), 3)


def log_grounding(brief: Brief, context: BriefContext) -> None:
    """ADVISORY: log the invention-rate signal for a finished brief. The single
    shared logging wrapper every brief-composing caller that HOLDS its context can
    run (today: the in-process composer). Runs once, after validation; never
    alters/gates the brief; swallows its own errors — a measurement, not a
    corrective round-trip. Emits on the ``grounding`` logger."""
    try:
        flags = flag(brief, context)
        rate = invention_rate(brief, context)
        detail = "".join(
            f" [{f.nearest_metric}:{f.token}Δ{f.delta}]" for f in flags[:5])
        _LOG.info("brief_grounding invention_rate=%.3f flags=%d%s", rate, len(flags), detail)
    except Exception:  # noqa: BLE001 — an advisory signal must never break the brief
        _LOG.exception("brief_grounding failed (advisory, ignored)")
