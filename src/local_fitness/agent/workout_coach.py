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
import re
from pathlib import Path

from .. import config
from . import prompts
from .coach import CoachProfile

_LOG = logging.getLogger(__name__)

# Measured on real cards, 2026-07-21, 12 generations across 2 cards:
#
#   sonnet-4-6, no effort, adaptive thinking   median 142.9s   max 165.6s
#   sonnet-5,   effort=low, thinking off       median  10.0s   max  10.8s
#
# The old 180s ceiling was sized for the first row and was closer to firing
# than its own comment admitted — a 165s run leaves 15s of margin, and a
# fallback here silently swaps the coach's voice for a template.
#
# 90s is ~8x the measured max for the shipped config, which is generous for a
# transient slow stream while still failing over promptly when the stream is
# genuinely dead (a documented failure mode — see CLAUDE.md's brief-job notes).
# A ceiling costs nothing on the happy path: the render is on-demand rather
# than interactive, and the disk cache makes every repeat instant.
DEFAULT_TIMEOUT_S = 90.0

#: This module's model, deliberately NOT ``briefing.DEFAULT_MODEL``.
#:
#: That constant also drives the daily brief generator, where a model change is
#: a prompt change and has to clear the scorer and a cross-model A/B first. The
#: two calls have nothing in common but a vendor: the brief reasons over a whole
#: day of data, while this one phrases four 45-word paragraphs from grades
#: ``report_card.py`` already computed and the prompt explicitly forbids it from
#: re-deriving. Coupling them meant this call could not be tuned at all.
#:
#: Sized to that job. Sonnet tier, not Opus — nothing here is intelligence-bound.
#: Not Haiku either: the four-section ``READ_SECTIONS`` contract is load-bearing
#: (a missing section raises and drops the card to the deterministic template),
#: and the coach voice is the feature.
DEFAULT_MODEL = "claude-sonnet-5"

#: Low effort and thinking off, and these are load-bearing rather than polish.
#: Sonnet 5 runs adaptive thinking whenever ``thinking`` is unset, so moving the
#: model ID forward *without* these would have made an already-67s call slower.
#: There is nothing for the model to reason about: every judgment on the card
#: was decided in Python before this prompt was built.
DEFAULT_EFFORT = "low"

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

#: The four paragraphs, in card order. The key is what the model must label
#: its output with; the label is what the reader sees above each paragraph.
READ_SECTIONS: tuple[tuple[str, str], ...] = (
    ("distance", "DISTANCE"),
    ("pace", "PACE"),
    ("hr", "HEART RATE"),
    ("load", "TRAINING LOAD"),
)

#: Deliberately states the rule without demonstrating it. The previous version
#: spelled out the forbidden tokens ("Do not write \"A\", \"B-\", \"C+\"…"),
#: which put four letter grades into the model's context in the same breath as
#: the ban. Between that and the per-metric letters the user prompt used to
#: carry, the read was being shown the thing it was told not to say — and it
#: leaked at 3.1% of paragraphs, regenerating to the SAME letter on retry.
_GRADE_TONE = (
    "The verdicts are already decided and are not yours to revise — do not "
    "argue with them, soften them, or re-grade the run.\n\n"
    "Write about the NUMBERS, never about a score. The report card prints a "
    "letter grade for each area in the table directly below you, so a letter "
    "in your paragraph is a word spent repeating what the reader can already "
    "see. Do not name one, do not spell one out, and do not invent a grading "
    "scale of your own. Make the reason obvious instead: what he was held to, "
    "what he actually did, and whether that gap matters. A reader should look "
    "at your paragraph, then at the table, and find the letter unsurprising.\n\n"
    "Do NOT discuss CTL, ATL, TSB, fitness base, fatigue score or freshness. "
    "Those are printed elsewhere and are not what this card is about."
)

_FORMAT_RULES = (
    "# Output format — follow exactly\n"
    "Write FOUR short paragraphs, one per graded area, each on its own line "
    "and prefixed with its label and a colon:\n\n"
    "DISTANCE: <paragraph>\n"
    "PACE: <paragraph>\n"
    "HEART RATE: <paragraph>\n"
    "TRAINING LOAD: <paragraph>\n\n"
    "All four labels must appear, exactly once, in that order. Nothing before "
    "the first label and nothing after the last paragraph — no heading, no "
    "summary, no markdown, no bullets, no quotation marks.\n\n"
    "HARD LIMIT: 45 words per paragraph. This is a budget, not a target. "
    "Two or three tight sentences each. Cut any clause not carrying a number "
    "or a verdict.\n\n"
    "You know the date of this run and what came before and after it. Use "
    "that where it changes the meaning — a pace that is fine in isolation "
    "reads differently as the third hard day in a row, and a short run makes "
    "sense the week of a race. Reach backward or forward only when it "
    "actually informs THAT paragraph's metric; do not narrate the calendar."
)


def build_prompt(
    profile: CoachProfile,
    card: dict,
    notes_text: str | None = None,
    user_name: str = config.DEFAULT_USER_NAME,
    memory_text: str | None = None,
) -> tuple[str, str]:
    """Assemble the ``(system_prompt, user_prompt)`` pair for the verbal read.

    Pure string assembly — no I/O, no randomness, fully unit-testable, and the
    complete cache key for ``generate_read_cached``.

    ``notes_text`` is the caller's already-rendered ``notes.render_for_prompt()``
    output (the ``prompts.system_prompt`` / ``plan_coach.build_prompt``
    convention — this function does no I/O of its own), so a saved preference
    is honored here exactly as it is in chat and the brief. ``memory_text``
    follows the same passed-in convention for the coach's memory; the caller
    (``tools.workout_report_card``) resolves it with this card's own journal
    entries excluded, so reflecting on a card never changes that card's prompt.
    """
    system_prompt = (
        f"You are {user_name}'s running coach, writing the opening read on a "
        "report card for ONE run he just finished. One short paragraph per "
        "graded area, each covering only that area.\n\n"
        f"{prompts.coach_voice_block(user_name, profile)}\n\n"
        f"{_GRADE_TONE}\n\n{_METRIC_TRANSLATION_BLOCK}\n\n"
        "Write in second person, present tense, the way you'd say it to his "
        "face.\n\n"
        f"{_FORMAT_RULES}"
    )
    memory_section = prompts.coach_memory_block(user_name, memory_text)
    if memory_section:
        system_prompt += f"\n\n{memory_section}"
    notes_section = prompts.user_notes_block(user_name, notes_text)
    if notes_section:
        system_prompt += f"\n\n{notes_section}"

    act = card.get("activity") or {}
    overall = card.get("overall") or {}
    lines = [
        f"Activity: {act.get('activity_name') or act.get('activity_type') or 'run'} "
        f"on {act.get('date')}.",
        # Severity, not the letter, and no GPA — see _GRADE_SEVERITY. The read
        # may not name either, and printing them here is what it echoed.
        f"Overall, the session was {grade_severity(overall.get('grade'))}.",
        f"Intent: {card.get('intent')} ({card.get('intent_source')}).",
        reference_summary(card),
        "",
        "Per-metric verdicts (already computed — phrase them, never re-derive "
        "them, and never convert them back into a letter):",
    ]
    for key, label in _metric_labels():
        m = (card.get("metrics") or {}).get(key) or {}
        if not m.get("grade"):
            # Prefer the metric's own reason. "not enough to grade" is true of a
            # thin reference pool but wrong for an interval day with no splits,
            # and the model will happily invent the difference.
            lines.append(f"  {label}: n/a — {m.get('note') or 'not enough to grade'}.")
            continue
        # actual_text, not the raw number: quality pace is graded on the fastest
        # split, and a bare "9:25/mi" would read as the whole run's average.
        line = f"  {label}: {grade_severity(m['grade'])} — actual {_actual_text(key, m)}"
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

    # No CTL/ATL/TSB block: the training-load model is printed on the card in
    # its own line and is not what these four paragraphs are about. Handing it
    # over just invited a freshness lecture in place of a distance verdict.

    recent = card.get("recent_activities") or []
    if recent:
        lines.append(f"\nWhat led into this run (most recent first, "
                     f"{_RECENT_LABEL}):")
        for r in recent:
            lines.append("  " + _describe_activity(r))

    upcoming = card.get("upcoming_workouts") or []
    if upcoming:
        lines.append("\nWhat this was setting up for (next 7 days, prescribed):")
        for w in upcoming:
            lines.append("  " + _describe_prescription(w))

    return system_prompt, "\n".join(lines)


_RECENT_LABEL = "trailing 14 days"


def _describe_activity(row: dict) -> str:
    from .report_card import _fmt_distance, _fmt_pace

    parts = [f"{row.get('date')}: {row.get('activity_type') or 'activity'}"]
    if row.get("distance_meters"):
        parts.append(_fmt_distance(row["distance_meters"]))
    if row.get("avg_pace_sec_per_km"):
        parts.append(_fmt_pace(row["avg_pace_sec_per_km"]))
    if row.get("avg_hr"):
        parts.append(f"{round(row['avg_hr'])} bpm")
    if row.get("training_load"):
        parts.append(f"load {round(row['training_load'])}")
    return " · ".join(parts)


def _describe_prescription(w: dict) -> str:
    parts = [f"{w.get('date')}: {w.get('type') or 'workout'}"]
    if w.get("distance_mi") is not None:
        parts.append(f"{w['distance_mi']} mi")
    if w.get("pace_min_per_mi"):
        parts.append(f"@ {w['pace_min_per_mi']}/mi")
    if w.get("description"):
        parts.append(str(w["description"]))
    return " · ".join(parts)


def _metric_labels():
    from .report_card import _METRIC_LABELS

    return _METRIC_LABELS


def _fmt(key: str, value) -> str:
    from .report_card import _FORMATTERS

    return _FORMATTERS[key](value)


def _expected_text(key: str, metric: dict) -> str:
    from .report_card import expected_text

    return expected_text(key, metric)


def _actual_text(key: str, metric: dict) -> str:
    from .report_card import actual_text

    return actual_text(key, metric)


def reference_summary(card: dict) -> str:
    """The yardstick, stated for the prompt rather than the page.

    Reuses ``report_card.reference_line`` with markdown off so the model is
    never handed ``**`` it might echo into a plain-text paragraph.
    """
    from .report_card import reference_line

    return reference_line(card, markdown=False)


#: A letter grade named in the prose. ``_GRADE_TONE`` forbids this outright —
#: the letters are printed in the table directly below the read, so repeating
#: one spends words the paragraph does not have on information already on the
#: page. The model obeys most of the time and not always: measured 2026-07-22
#: over 96 paragraphs on 3 real cards, **3 leaked** (3.1%) — "an F, no rounding
#: it up", "F-grade pace", "the C+ says so".
#:
#: Narrow by construction, because the FALSE POSITIVE is the expensive error: a
#: bare "A" is nearly always the article ("A blown interval session…"), and
#: treating it as a grade would throw away a clean read and pay for another
#: generation. So a bare letter only counts when it is punctuated like a
#: sentence, preceded by an article, or followed by grade-talk; a letter with a
#: +/- sign is unambiguous and always counts. Validated against 12 cases (5 real
#: leaks, 7 lookalikes) in ``tests/test_workout_coach.py`` — extend BOTH lists
#: there before touching this pattern.
_GRADE_LEAK = re.compile(
    r"(?:^|(?<=[\s(]))"
    r"(?:"
    r"[A-DF][+-]"                                    # signed: unambiguous
    r"|[A-DF](?=[.,;:!?]\"?\s)"                      # "F. Target 6:58/mi…"
    r"|(?:an?\s+)[A-DF](?=[\s.,;:])"                 # "an F", "a B"
    r"|[A-DF](?=\s+(?:is|on paper|effort|grade))"    # "F is F", "B on paper"
    r"|[A-DF](?=-grade\b)"                           # "F-grade pace"
    r")"
)


#: Grade letter → the severity word the PROMPT carries in its place.
#:
#: The read is forbidden from naming a letter, and the prompt used to hand it
#: every letter anyway ("Distance: D- — actual 5.95 mi vs target 5.00 mi"). That
#: is not a rule the model can follow reliably; it is a token sitting in its
#: context next to the metric it is being asked to write about, and a leaked
#: read regenerated to the SAME letter twice in testing (2026-07-22) because the
#: retry saw the same prompt.
#:
#: The severity still has to be present, or the read drifts out of agreement
#: with the table beside it: a +19% distance overshoot is a D- only because the
#: intent scaling says an interval day is not the place for extra miles, and
#: that judgment is not recoverable from the raw numbers. So the band goes in
#: and the letter stays out.
_GRADE_SEVERITY = {
    "A": "on target",
    "B": "slightly off target",
    "C": "off target",
    "D": "well off target",
    "F": "missed badly",
}


def grade_severity(grade: str | None) -> str:
    """Severity word for a grade, keyed on the BASE letter so "D-" and "D+"
    read the same. Unknown/ungraded → "n/a"."""
    if not grade or grade == "n/a":
        return "n/a"
    return _GRADE_SEVERITY.get(grade[0], "n/a")


def find_grade_leak(sections: dict[str, str]) -> str | None:
    """The first letter grade named in a parsed read, or ``None`` if clean.

    Pure, so the decision to regenerate is testable without an SDK call. See
    ``_GRADE_LEAK`` for why the pattern is deliberately narrow.
    """
    for text in sections.values():
        m = _GRADE_LEAK.search(text or "")
        if m:
            return m.group(0).strip()
    return None


def parse_read(text: str) -> dict[str, str]:
    """``LABEL: paragraph`` lines → ``{metric_key: paragraph}``.

    Pure. Raises ``ValueError`` unless all four sections are present and
    non-empty, so a malformed generation falls back to the deterministic
    four-paragraph template rather than rendering a card with a missing or
    half-parsed section. Tolerates the cosmetics a model varies on — leading
    bullets, bold markers, blank lines between sections, a label in any case —
    because none of those change the content, and regenerating over a stray
    asterisk would be wasteful.
    """
    if not text:
        raise ValueError("empty read")

    # Locate each label's span, then take everything up to the next label.
    positions: list[tuple[int, int, str]] = []
    for key, label in READ_SECTIONS:
        m = re.search(
            rf"^[\s>*\-#]*{re.escape(label)}\s*:", text,
            re.IGNORECASE | re.MULTILINE,
        )
        if m is None:
            raise ValueError(f"read is missing the {label} section")
        positions.append((m.start(), m.end(), key))
    positions.sort()

    out: dict[str, str] = {}
    for i, (_, body_start, key) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        body = text[body_start:end].strip().strip("*").strip()
        if not body:
            raise ValueError(f"read has an empty {key} section")
        # Collapse the model's own wrapping; the renderer does the wrapping.
        out[key] = " ".join(body.split())
    return out


async def generate_read(
    profile: CoachProfile,
    card: dict,
    *,
    model: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    notes_text: str | None = None,
    user_name: str = config.DEFAULT_USER_NAME,
    memory_text: str | None = None,
) -> str:
    """Claude-generated opening read for the report card.

    Raises on any failure (missing/expired credential, network, timeout, empty
    response) — the caller falls back to ``fallback_read``. ``model=None``
    resolves to this module's own ``DEFAULT_MODEL``; see that constant for why
    it is no longer ``briefing.DEFAULT_MODEL``.
    """
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

    if model is None:
        model = DEFAULT_MODEL

    system_prompt, user_prompt = build_prompt(
        profile, card, notes_text=notes_text, user_name=user_name,
        memory_text=memory_text)
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model,
        permission_mode="bypassPermissions",
        max_turns=1,
        # See DEFAULT_EFFORT: without these, a newer model ID would be a
        # latency regression rather than an improvement.
        effort=DEFAULT_EFFORT,
        thinking={"type": "disabled"},
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


def read_cache_key(
    profile: CoachProfile,
    card: dict,
    *,
    model: str | None = None,
    notes_text: str | None = None,
    user_name: str = config.DEFAULT_USER_NAME,
    memory_text: str | None = None,
) -> str:
    """The read's cache key — the ONE key definition, shared by
    ``generate_read_cached``'s file cache and ``card_store``'s persisted rows.

    Pure: ``build_prompt`` is pure, so the key is a function of the card,
    voice, notes and memory alone. The byte layout is load-bearing —
    ``sha256("\\x00".join([system_prompt, user_prompt, model or "default",
    str(activity_id)]))``, with the literal ``"default"`` (NOT
    ``DEFAULT_MODEL``) when ``model`` is None — because a stored row's key
    must hash identically to the file cache's or the fast path silently
    never fires.

    activity_id is part of the key even though it is deliberately absent from
    the prompt (a bare row id is noise to the model). Without it, two
    sessions on the same day with the same name and the same grades — a
    double day, which the tool already handles via other_activities_on_date —
    hash identically, and the second card silently serves the first's read.
    """
    system_prompt, user_prompt = build_prompt(
        profile, card, notes_text=notes_text, user_name=user_name,
        memory_text=memory_text)
    return hashlib.sha256(
        "\x00".join([
            system_prompt, user_prompt, model or "default",
            str((card.get("activity") or {}).get("activity_id", "")),
        ]).encode("utf-8")
    ).hexdigest()


async def generate_read_cached(
    profile: CoachProfile,
    card: dict,
    *,
    model: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    notes_text: str | None = None,
    user_name: str = config.DEFAULT_USER_NAME,
    memory_text: str | None = None,
    cache_path: Path | None = None,
) -> dict[str, str]:
    """``generate_read`` behind a single-entry disk cache, parsed into sections.

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
    key = read_cache_key(
        profile, card, model=model, notes_text=notes_text,
        user_name=user_name, memory_text=memory_text)
    path = cache_path or _cache_path()
    cached = _read_cache(path, key)
    if cached is not None:
        try:
            sections = parse_read(cached)
            _LOG.info("workout_coach cache hit — reusing read")
            return sections
        except ValueError:
            # A cached string that no longer parses (an older format, a
            # truncated write) is a miss, not a failure.
            _LOG.info("workout_coach cached read no longer parses — regenerating")
    text = await generate_read(
        profile, card, model=model, timeout=timeout, notes_text=notes_text,
        user_name=user_name, memory_text=memory_text)
    # Parse BEFORE caching: an unparseable generation must not be stored, or
    # every later render pays to rediscover that it is unusable.
    sections = parse_read(text)

    # `_GRADE_TONE` forbids naming a letter grade and the model complies ~97% of
    # the time (measured: 3 of 96 paragraphs). A prompt cannot make that a
    # guarantee, so the code checks — the same division of labour that has
    # `parse_read` enforce the four-section contract rather than trusting it.
    #
    # ONE retry, not a loop: sampling is non-deterministic (no temperature is
    # pinned), so a second draw is a genuinely different read and clears the
    # leak the overwhelming majority of the time, while a loop would let a
    # pathological card spend unbounded time and tokens. The retry only wins if
    # it is actually clean; otherwise the original stands, so a leak can never
    # cost more than one extra call. It matters more than 3% suggests because
    # the result is CACHED — a leaked read would otherwise stick until the
    # card's inputs change.
    leak = find_grade_leak(sections)
    if leak:
        _LOG.info("workout_coach read named a grade (%r) — regenerating once", leak)
        try:
            retry_text = await generate_read(
                profile, card, model=model, timeout=timeout,
                notes_text=notes_text, user_name=user_name,
                memory_text=memory_text)
            retry_sections = parse_read(retry_text)
        except Exception:
            # A failed retry must never cost the card the read it already has.
            _LOG.warning("workout_coach retry failed — keeping the first read",
                         exc_info=True)
        else:
            retry_leak = find_grade_leak(retry_sections)
            if retry_leak is None:
                text, sections = retry_text, retry_sections
            else:
                _LOG.warning(
                    "workout_coach read named a grade twice (%r, %r) — keeping "
                    "the first", leak, retry_leak)

    _write_cache(path, key, text)
    return sections


def fallback_read(card: dict) -> dict[str, str]:
    """Deterministic, template-based four-paragraph read — used only when
    ``generate_read`` fails or its output cannot be parsed. Pure: identical
    cards always produce identical text. Never raises.

    Deliberately flat rather than doing an impression of the coach voice: a
    template pretending to be a personality reads worse than one that plainly
    states the result, and the table below carries the verdict regardless.
    """
    from .report_card import _delta_text

    out: dict[str, str] = {}
    for key, label in READ_SECTIONS:
        m = ((card.get("metrics") or {}).get(key)) or {}
        actual = _actual_text(key, m)
        if not m.get("grade"):
            reason = m.get("note") or "not enough comparable history to grade this"
            out[key] = f"{actual}. {reason[0].upper()}{reason[1:]}."
            continue
        target = _expected_text(key, m)
        delta = _delta_text(key, m)
        sentence = f"{actual} against {target}." if target != "—" else f"{actual}."
        if delta != "—":
            sentence += f" {delta.capitalize()}."
        if m.get("note"):
            sentence += f" {m['note'].capitalize()}."
        if key == "load" and m.get("spike"):
            sentence += " More than double your median day."
        out[key] = sentence
    return out


__all__ = [
    "build_prompt", "generate_read", "generate_read_cached", "fallback_read",
    "parse_read", "read_cache_key", "reference_summary", "READ_SECTIONS",
    "DEFAULT_MODEL", "DEFAULT_EFFORT", "DEFAULT_TIMEOUT_S",
]
