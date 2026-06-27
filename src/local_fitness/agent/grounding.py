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

# Per the design: relative bands. A token within EXACT of a known number counts
# as faithful; within NEARBY (but past EXACT) is a close-but-unequal mis-state;
# beyond NEARBY is a different quantity and is ignored. ABS_FLOOR keeps tiny
# absolute diffs on small values (3.2 vs 3.3) from reading as contradictions.
_EXACT_REL = 0.03
_NEARBY_REL = 0.12
_ABS_FLOOR = 0.5


class GroundingFlag(BaseModel):
    """One prose number that looks like a known metric but doesn't match it."""
    takeaway_index: int
    token: str
    nearest_metric: str
    delta: float          # prose value − nearest known value


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


def _display_numbers(gv: GroundedValue) -> list[float]:
    """The numbers in a GroundedValue's coach-ready display (post-conversion)."""
    out = []
    for tok in _NUM_RE.findall(gv.display):
        v = _parse(tok)
        if v is not None:
            out.append(v)
    return out


def _union(context: BriefContext) -> list[GroundedValue]:
    """Every citable GroundedValue — the exact set the prose may draw from."""
    out: list[GroundedValue] = []
    out.extend(context.snapshot)
    out.extend(context.training_load)
    out.extend(context.trends)
    for c in context.candidates:
        out.extend(c.metrics)
    return out


def _grounded_pool(context: BriefContext) -> list[tuple[float, str]]:
    """(magnitude, source-name) for every known number the prose may cite."""
    pool: list[tuple[float, str]] = []
    for gv in _union(context):
        for n in _display_numbers(gv):
            pool.append((abs(n), gv.name))
    # Scalar context numbers that are legitimate to cite but aren't GroundedValues.
    if context.step_goal is not None:
        pool.append((float(context.step_goal), "step_goal"))
    if context.days_to_race is not None:
        pool.append((float(abs(context.days_to_race)), "days_to_race"))
    return pool


def _nearest(value: float, pool: list[tuple[float, str]]) -> tuple[float, str]:
    """Nearest pool entry to ``value`` by relative distance. Pool is non-empty."""
    best_rel, best_val, best_name = float("inf"), 0.0, ""
    for val, name in pool:
        denom = max(value, val, 1.0)
        rel = abs(value - val) / denom
        if rel < best_rel:
            best_rel, best_val, best_name = rel, val, name
    return best_val, best_name


def flag(brief: Brief, context: BriefContext) -> list[GroundingFlag]:
    """Advisory: prose numbers that look like a known metric but are off.

    Never raises, never mutates the brief. Returns [] when the context carries
    no citable numbers (nothing to contradict)."""
    pool = _grounded_pool(context)
    if not pool:
        return []
    flags: list[GroundingFlag] = []
    for i, tk in enumerate(brief.takeaways):
        text = f"{tk.headline} {tk.summary} {tk.details}"
        for m in _NUM_RE.finditer(text):
            if _WINDOW_AFTER.match(text, m.end()):
                continue  # a time window ("14 days"), not a metric claim
            tok = m.group()
            x = _parse(tok)
            if x is None:  # pragma: no cover - defensive; _NUM_RE only matches parseable tokens
                continue
            ax = abs(x)
            near_val, near_name = _nearest(ax, pool)
            denom = max(ax, near_val, 1.0)
            rel = abs(ax - near_val) / denom
            if rel <= _EXACT_REL or abs(ax - near_val) <= _ABS_FLOOR:
                continue                      # faithful citation
            if rel <= _NEARBY_REL:
                flags.append(GroundingFlag(
                    takeaway_index=i, token=tok.strip(),
                    nearest_metric=near_name, delta=round(x - near_val, 2)))
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
