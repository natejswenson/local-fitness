"""Claude-generated verbal read of one workout, for the top of the report card.

A sibling of ``plan_coach.py``, and deliberately a separate module for the same
reasons that one is: the Agent SDK and ``briefing`` are imported inside the
function bodies, never at module scope, because ``agent/tools.py`` imports this
module and ``tools.py`` is imported by the always-running containerized web
server — which must never pay the SDK's import cost for a stdio-only PDF
feature it never invokes. ``briefing`` additionally cannot be imported at
module scope without closing a real cycle (``tools -> workout_coach ->
briefing -> tools``); see ``plan_coach``'s module docstring for the full
history.

Why a *separate* module from ``plan_coach`` rather than a second function in
it: ``plan_coach`` preps Nate for a run he has not done yet, from the plan's
prescription. This one judges a run he already did, from graded results. The
prompts share a voice but not an input shape, a tense, or a failure mode, and
folding them together would mean one function with two disjoint halves.

The grades themselves are never generated here. ``report_card.py`` computes
every letter in tested Python; this module is handed those letters and asked
only to phrase them. That is the repo-wide rule — the LLM phrases a judgment,
it never derives one code can compute — and it is why the fallback below can
be a pure template without losing correctness: the *verdict* is already
decided, only the wording degrades.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

from .coach import CoachProfile

_LOG = logging.getLogger(__name__)

# Measured on a real card (2026-07-20): 22.2s end to end, against the 30s
# plan_coach uses — close enough that an ordinary cold start tipped it into a
# timeout and silently served the template fallback. The caller is already
# waiting on a WeasyPrint render and a possible Garmin fetch, so a generous
# ceiling costs nothing on the happy path and buys real headroom.
DEFAULT_TIMEOUT_S = 90.0

# Same metric-translation contract as prompts.system_prompt and
# plan_coach — included unconditionally so the report card honors it whether
# or not an abbreviation happens to appear in this particular read.
_METRIC_TRANSLATION_BLOCK = (
    "Translate technical metrics on first use, the same way you always do: "
    'CTL -> "fitness" (training base over the last six weeks), '
    'ATL -> "fatigue" (load from the last 7 days), '
    'TSB -> "freshness" (positive = rested, negative = worn down). '
    "Pair every number with its plain-English meaning."
)

_GRADE_TONE = (
    "The grades are already decided and are not yours to revise — do not "
    "argue with them, soften them, or re-grade the run.\n\n"
    "Account for ALL FOUR graded metrics — distance, pace, heart rate, and "
    "training load. Every one of them gets addressed; do not cover two and "
    "leave the others unmentioned. Metrics that went fine can share a single "
    "short clause — spend the words on what went wrong.\n\n"
    "NEVER state a letter grade. Do not write \"A\", \"B-\", \"C+\", "
    '"you got a B", or "your pace grade". The letters are printed in the '
    "table directly below you and repeating them wastes the only sentences "
    "you get. Instead make the REASON for each one obvious from the numbers: "
    "say what he was held to, what he actually did, and why that gap does or "
    "does not matter. A reader should be able to look at your paragraph, then "
    "at the table, and find the letters unsurprising."
)


def build_prompt(
    profile: CoachProfile,
    card: dict,
    notes_text: str | None = None,
) -> tuple[str, str]:
    """Assemble the ``(system_prompt, user_prompt)`` pair for the verbal read.

    Pure string assembly — no I/O, no randomness, fully unit-testable, and the
    complete cache key for ``generate_read_cached``.

    ``notes_text`` is the caller's already-rendered ``notes.render_for_prompt()``
    output (the ``prompts.system_prompt`` / ``plan_coach.build_prompt``
    convention — this function does no I/O of its own), so a saved preference
    is honored here exactly as it is in chat and the brief.
    """
    system_prompt = (
        "You are Nate's running coach, writing the opening read on a report "
        "card for ONE run he just finished.\n\n"
        "HARD LIMIT: 85 words. This is a budget, not a target — going over "
        "gets the paragraph cut off mid-sentence on the page. A sentence "
        "count is not the constraint; total words is. Be terse. Cut every "
        "clause that is not carrying a number or a verdict.\n\n"
        f"{profile.dials_line}\n\n{profile.persona}\n\n"
        f"{_GRADE_TONE}\n\n{_METRIC_TRANSLATION_BLOCK}\n\n"
        "Lead with the single thing that actually mattered about this run. "
        "Write in second person, present tense, the way you'd say it to his "
        "face. Output ONLY the paragraph — no headline, no markdown, no "
        "bullet points, no quotation marks, no preamble."
    )
    if notes_text:
        system_prompt += (
            "\n\n# What Nate has told you (most recent first — prefer the "
            f"newer note when two conflict)\n{notes_text}"
        )

    act = card.get("activity") or {}
    overall = card.get("overall") or {}
    lines = [
        f"Activity: {act.get('activity_name') or act.get('activity_type') or 'run'} "
        f"on {act.get('date')}.",
        f"Overall grade {overall.get('grade')}"
        + (f" ({overall['gpa']:.2f} GPA)." if overall.get("gpa") is not None else "."),
        f"Intent: {card.get('intent')} ({card.get('intent_source')}).",
        reference_summary(card),
        "",
        "Metric grades (already computed — phrase, don't re-derive):",
    ]
    for key, label in _metric_labels():
        m = (card.get("metrics") or {}).get(key) or {}
        if not m.get("grade"):
            lines.append(f"  {label}: n/a — not enough to grade.")
            continue
        line = f"  {label}: {m['grade']} — actual {_fmt(key, m.get('actual'))}"
        # expected_text, not the raw number: HR is held to a BAND, and handing
        # the model a bare midpoint is how it ends up explaining a heart-rate
        # verdict against a number the grade was never measured against.
        expected = _expected_text(key, m)
        if expected != "—":
            line += f" vs target {expected}"
        if m.get("in_band"):
            line += " (inside the range for this intent)"
        lines.append(line + ".")
        if m.get("note"):
            lines.append(f"    note: {m['note']}")
    if (card.get("metrics") or {}).get("load", {}).get("spike"):
        lines.append("  Training load was a SPIKE — more than double his median day.")

    splits = card.get("splits") or {}
    if splits.get("available"):
        drift = splits.get("hr_drift_pct")
        if drift is not None:
            lines.append(
                f"Heart-rate drift, back half vs front half: {drift:+.1f}% "
                "(positive = he was working harder late for the same ground).")
        rows = [r for r in splits.get("rows") or [] if not r.get("partial")]
        if rows:
            lines.append(f"Per-{splits.get('unit', 'lap').lower()} splits:")
            for r in rows:
                lines.append(
                    f"  {splits.get('unit')} {r['index']}: "
                    f"{r.get('pace_min_per_mi') or '—'}/mi, "
                    f"{r.get('avg_hr') or '—'} bpm")

    ctx = card.get("context") or {}
    if ctx.get("ctl") is not None:
        lines.append(
            f"On this date: fitness (CTL) {ctx['ctl']:.0f}, fatigue (ATL) "
            f"{ctx['atl']:.0f}, freshness (TSB) {ctx['tsb']:+.0f}.")

    return system_prompt, "\n".join(lines)


def _metric_labels():
    from .report_card import _METRIC_LABELS

    return _METRIC_LABELS


def _fmt(key: str, value) -> str:
    from .report_card import _FORMATTERS

    return _FORMATTERS[key](value)


def _expected_text(key: str, metric: dict) -> str:
    from .report_card import expected_text

    return expected_text(key, metric)


def reference_summary(card: dict) -> str:
    """The yardstick, stated for the prompt rather than the page.

    Reuses ``report_card.reference_line`` with markdown off so the model is
    never handed ``**`` it might echo into a plain-text paragraph.
    """
    from .report_card import reference_line

    return reference_line(card, markdown=False)


async def generate_read(
    profile: CoachProfile,
    card: dict,
    *,
    model: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    notes_text: str | None = None,
) -> str:
    """Claude-generated opening read for the report card.

    Raises on any failure (missing/expired credential, network, timeout, empty
    response) — the caller falls back to ``fallback_read``. ``model=None``
    resolves to ``briefing.DEFAULT_MODEL`` at call time, so this follows that
    constant rather than duplicating a literal that could drift.
    """
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

    from . import briefing

    if model is None:
        model = briefing.DEFAULT_MODEL

    system_prompt, user_prompt = build_prompt(profile, card, notes_text=notes_text)
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model,
        permission_mode="bypassPermissions",
        max_turns=1,
    )

    async def _run() -> str:
        chunks: list[str] = []
        async for message in query(prompt=user_prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
        return "".join(chunks).strip()

    text = await asyncio.wait_for(_run(), timeout=timeout)
    if not text:
        raise RuntimeError("workout-read generator returned an empty response")
    return text


def _cache_path() -> Path:
    """The read cache, kept next to the SQLite DB — the same already-gitignored,
    host/container-shared writable location ``plan_coach`` uses."""
    from .. import db  # lazy: keep module import cost near zero

    return db.DEFAULT_DB_PATH.parent / "workout_coach_cache.json"


def _read_cache(path: Path, key: str) -> str | None:
    """The cached read for exactly ``key``, else None. Tolerates a missing or
    corrupt cache file — never raises."""
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(entry, dict) and entry.get("key") == key:
            text = entry.get("text")
            if isinstance(text, str) and text:
                return text
    except (OSError, ValueError):
        pass
    return None


def _write_cache(path: Path, key: str, text: str) -> None:
    """Best-effort single-entry cache write (latest key wins). A cache failure
    must never fail the render — swallow and log."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"key": key, "text": text}), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        _LOG.warning("workout_coach cache write failed (ignored)", exc_info=True)


async def generate_read_cached(
    profile: CoachProfile,
    card: dict,
    *,
    model: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    notes_text: str | None = None,
    cache_path: Path | None = None,
) -> str:
    """``generate_read`` behind a single-entry disk cache.

    Same rationale as ``plan_coach.generate_coaching_line_cached``: re-rendering
    the same card is the common case (you look at a run more than once), and
    ``build_prompt`` is pure, so the prompt pair's hash fully captures every
    input — activity, grades, splits, voice, notes. Same key → cached text, no
    SDK call; any change → a fresh read. Only successes are cached, so a
    transient failure never pins a template line to a card.

    Single-entry, not keyed-by-activity: cards are looked at one at a time, and
    a one-line file needs no eviction policy. Alternating between two
    activities re-generates each time, which is the accepted cost of not
    building a cache manager for a personal tool.
    """
    system_prompt, user_prompt = build_prompt(profile, card, notes_text=notes_text)
    # activity_id is part of the key even though it is deliberately absent from
    # the prompt (a bare row id is noise to the model). Without it, two
    # sessions on the same day with the same name and the same grades — a
    # double day, which the tool already handles via other_activities_on_date —
    # hash identically, and the second card silently serves the first's read.
    key = hashlib.sha256(
        "\x00".join([
            system_prompt, user_prompt, model or "default",
            str((card.get("activity") or {}).get("activity_id", "")),
        ]).encode("utf-8")
    ).hexdigest()
    path = cache_path or _cache_path()
    cached = _read_cache(path, key)
    if cached is not None:
        _LOG.info("workout_coach cache hit — reusing read")
        return cached
    text = await generate_read(
        profile, card, model=model, timeout=timeout, notes_text=notes_text)
    _write_cache(path, key, text)
    return text


def fallback_read(card: dict) -> str:
    """Deterministic, template-based opening read — used only when
    ``generate_read`` fails. Pure: identical cards always produce identical
    text. Never raises.

    Deliberately flat rather than doing an impression of the coach voice: a
    template pretending to be a personality reads worse than a template that
    plainly states the result, and the grades above it carry the verdict
    regardless.
    """
    act = card.get("activity") or {}
    overall = card.get("overall") or {}
    grade = overall.get("grade") or "n/a"

    parts = []
    name = act.get("activity_name") or act.get("activity_type") or "This run"
    dist = (card.get("metrics") or {}).get("distance", {}).get("actual")
    if dist:
        from . import units

        parts.append(
            f"{name}: {units.to_miles(dist):.2f} mi, graded {grade}.")
    else:
        parts.append(f"{name}: graded {grade}.")

    # `or ""` rather than a .get default: an ungraded metric carries an
    # explicit `"grade": None`, so the default never fires and None.startswith
    # raises. That is exactly the insufficient-history card.
    def _grade_of(key: str) -> str:
        return ((card.get("metrics") or {}).get(key) or {}).get("grade") or ""

    weak = [label for key, label in _metric_labels()
            if _grade_of(key).startswith(("D", "F"))]
    strong = [label for key, label in _metric_labels()
              if _grade_of(key).startswith("A")]
    if weak:
        parts.append(f"Weakest: {', '.join(weak).lower()}.")
    if strong:
        parts.append(f"Strongest: {', '.join(strong).lower()}.")

    drift = (card.get("splits") or {}).get("hr_drift_pct")
    if drift is not None and drift > 5:
        parts.append(
            f"Heart rate climbed {drift:+.1f}% in the back half for the same ground.")
    return " ".join(parts)


__all__ = [
    "build_prompt", "generate_read", "generate_read_cached", "fallback_read",
    "reference_summary",
]
