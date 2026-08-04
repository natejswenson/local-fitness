"""Auto-reflect — the coach writes its journal after a brief or report card.

A sibling of ``workout_coach.py``/``plan_coach.py`` in every mechanical
respect: the Agent SDK is imported inside function bodies (tools.py imports
this module and the containerized server must not pay the SDK import cost),
the call is toolless single-shot with this module's own measured model
constants, and every entry point is fail-silent — a reflect failure can cost
at most a memory, never the brief or the card it reflected on.

Idempotency is layered, not hoped for:
  * ``journal.has_event`` pre-check — a re-render skips even prompt assembly.
  * ``idx_journal_event`` (db.py) — a race that beats the pre-check dies on
    the unique index instead of double-writing.
  * ``memory.render_memory_for_prompt(exclude_source_key=...)`` — the entries
    this step writes are excluded from the prompt of the artifact they are
    about, so reflecting can never bust that artifact's cache and cascade.

Output contract: ``MEMORY: <line>`` × 0-2, or the literal ``NONE``. The
parser is pure and strict-but-forgiving (anything unparseable → no entries,
never an error), because "no memory today" is a correct outcome — most days
are not worth writing down, and the prompt says so.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date

from .. import config
from . import journal, ledger, memory, prompts, workout_coach
from .coach import CoachProfile

_LOG = logging.getLogger(__name__)

#: Same measured Sonnet-low profile as workout_coach (median ~10s): nothing
#: here is intelligence-bound — the ledger already states the facts, the model
#: only picks what is worth remembering and phrases it in-voice.
DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_EFFORT = "low"
DEFAULT_TIMEOUT_S = 45.0

MAX_ENTRIES = 2

_TASK = (
    "You keep a private coach's journal about {user_name} — one dated line "
    "per memory, written to yourself, in your own coaching voice. You are "
    "shown today's event (a daily brief or a graded workout), the computed "
    "relationship facts, and your recent journal.\n\n"
    "Decide whether anything from TODAY'S EVENT is worth remembering a week "
    "from now: a skipped session and the excuse attached to it, a promise, an "
    "injury signal, a pattern crossing a line, a breakthrough performance. "
    "Most days are NOT worth an entry — routine execution is the job, not a "
    "memory.\n\n"
    "Capture COLOR, not a stat recap: what {user_name} SAID or committed to "
    "(an excuse, a promise, a stated feeling or fact), not a number the "
    "computed facts already state and a future report card will state again. "
    "If the only candidate is a bare restatement of a metric with nothing "
    "said around it, stay silent instead.\n\n"
    "Rules:\n"
    "- 0, 1 or at most 2 lines. Each under 200 characters.\n"
    "- Only facts present in the event or the computed facts — never invent "
    "a count, a date, or a quote.\n"
    "- Never repeat something your recent journal already says — escalate it "
    "('second time this month') or stay silent.\n"
    "- Never begin the line with a date — every entry is already shown with "
    "its own date attached when it's read back to you; start straight with "
    "the content.\n"
    "- No star ratings, no letter grades, no CTL/ATL/TSB numbers.\n\n"
    "# Output format — follow exactly\n"
    "Either the single word NONE, or one line per memory:\n"
    "MEMORY: <the line>\n"
    "Nothing else — no preamble, no markdown, no explanation."
)

#: Journal entries must never carry a rating or a raw CTL/ATL/TSB
#: number — ``_TASK`` already asks the model not to, and it complies most of
#: the time, but a live sample (2026-07-26) measured 6+ of 13 hot entries
#: leaking one anyway: "hit distance/pace/HR (A/A+/A+) but load graded C-",
#: "graded D overall", "distance D- (2.05/2.50mi", "after CTL peaked at
#: 58.4". Same division of labor as workout_coach's ``_GRADE_LEAK``: the
#: prompt asks, the code enforces. A leaking entry is REJECTED whole rather
#: than scrubbed in place — cutting the offending token out of a coach-voice
#: sentence usually leaves broken grammar, and the model reliably has
#: another way to phrase the same fact without a grade.
#:
#: ``workout_coach.find_grade_leak`` covers the star half since 0.50.0 (a count
#: adjacent to "star", an "N/5", or a glyph), so the patterns below stay aimed
#: at the shapes it structurally cannot see: a "/"-run never satisfies its
#: leading-boundary lookbehind, and "graded C-" is a letter form.
_GRADED_WORD_RE = re.compile(
    r"\bgraded\s+(?:[A-DF][+-]?|\d(?:\.\d+)?)\b", re.IGNORECASE)
_TRAINING_LOAD_RE = re.compile(r"\bCTL\b|\bATL\b|\bTSB\b", re.IGNORECASE)
#: A run of 2+ slash-separated grade-like tokens, e.g. "A/A+/A+" or "B-/C".
#: workout_coach's ``_GRADE_LEAK`` requires a preceding space/paren/start,
#: which a "/" never satisfies, so it misses this shape entirely. Narrowed
#: to runs of 3+ tokens OR any signed token (len 2, e.g. "A+") to avoid
#: flagging a common two-letter idiom like "A/B testing".
_GRADE_RUN_RE = re.compile(r"\b[A-DF][+-]?(?:/[A-DF][+-]?){1,}\b")

#: A model-written leading date the render will prefix again
#: (``journal.render_journal_block`` already emits "Jul 18: <text>"), e.g. a
#: live entry read "Jul 26: Jul 26 easy run hot..." — the model repeating the
#: date journal.py was always going to add.
_LEADING_DATE_RE = re.compile(
    r"^(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+\d{1,2}"
    r"(?:st|nd|rd|th)?\s*[:,-]?\s*",
    re.IGNORECASE,
)


def _strip_leading_date(entry: str) -> str:
    """Drop a model-written leading date so it doesn't double up with the
    render's own date prefix. Pure; may return ``""`` if the entry was
    nothing but a date. Recapitalizes only when a date was actually
    stripped — an entry with no leading date (the common case) must come
    back byte-identical, never re-cased."""
    stripped = _LEADING_DATE_RE.sub("", entry, count=1)
    if stripped == entry:
        return entry
    stripped = stripped.strip()
    if stripped and stripped[0].islower():
        stripped = stripped[0].upper() + stripped[1:]
    return stripped


def _has_grade_leak(text: str) -> bool:
    """Narrow, deterministic scrub for the two failure modes measured live: a
    letter grade (reusing workout_coach's proven pattern, extended with a
    slash-run and a "graded X" case) and a bare CTL/ATL/TSB mention."""
    if workout_coach.find_grade_leak({"entry": text}) is not None:
        return True
    if _GRADED_WORD_RE.search(text) or _TRAINING_LOAD_RE.search(text):
        return True
    for m in _GRADE_RUN_RE.finditer(text):
        tokens = m.group(0).split("/")
        if len(tokens) >= 3 or any(len(t) == 2 for t in tokens):
            return True
    return False


def build_prompt(
    profile: CoachProfile,
    event: dict,
    ledger_text: str,
    recent_entries: list[dict],
    user_name: str = config.DEFAULT_USER_NAME,
) -> tuple[str, str]:
    """Pure ``(system_prompt, user_prompt)`` assembly — everything passed in."""
    system_prompt = (
        f"{prompts.coach_voice_block(user_name, profile, compact=True)}\n\n"
        f"{_TASK.format(user_name=user_name)}"
    )
    lines = [f"# Today's event ({event.get('kind', 'event')})"]
    for k, v in event.items():
        if k == "kind" or v in (None, "", []):
            continue
        lines.append(f"{k}: {v}")
    lines.append("\n# Computed relationship facts (the only citable counts)")
    lines.append(ledger_text or "(none)")
    lines.append("\n# Your recent journal (do not repeat these)")
    rendered = journal.render_journal_block(recent_entries, user_name)
    lines.append(rendered or "(empty)")
    return system_prompt, "\n".join(lines)


def parse_reflection(text: str, existing: tuple[str, ...] = ()) -> list[str]:
    """``MEMORY:`` lines from a generation — deduped against ``existing``
    (case-insensitive), capped at :data:`MAX_ENTRIES`, each truncated at a
    word boundary to :data:`journal.ENTRY_MAX_CHARS`. ``NONE``, garbage, or
    an empty generation → ``[]``. Pure, never raises.

    Two more rejections happen here, deterministically, rather than trusting
    the prompt: a leading date the model wrote itself is stripped (the render
    already dates every line), and an entry naming a letter grade or a
    CTL/ATL/TSB number is dropped outright (see ``_has_grade_leak``) — the
    same "the prompt asks, the code enforces" split as workout_coach.
    """
    if not text:
        return []
    seen = {e.strip().lower() for e in existing}
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("-* ").strip()
        if not line.lower().startswith("memory:"):
            continue
        entry = line[len("memory:"):].strip()
        if not entry:
            continue
        entry = _strip_leading_date(entry)
        if not entry:
            continue
        if _has_grade_leak(entry):
            continue
        if len(entry) > journal.ENTRY_MAX_CHARS:
            cut = entry[: journal.ENTRY_MAX_CHARS]
            entry = cut.rsplit(" ", 1)[0] if " " in cut else cut
        if entry.strip().lower() in seen:
            continue
        seen.add(entry.strip().lower())
        out.append(entry)
        if len(out) >= MAX_ENTRIES:
            break
    return out


async def generate_reflection(
    profile: CoachProfile,
    event: dict,
    ledger_text: str,
    recent_entries: list[dict],
    *,
    model: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    user_name: str = config.DEFAULT_USER_NAME,
) -> str:
    """One toolless SDK call. Raises on failure — the ``reflect_after_*``
    wrappers are the fail-silent layer, mirroring the coach modules' split."""
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

    system_prompt, user_prompt = build_prompt(
        profile, event, ledger_text, recent_entries, user_name=user_name)
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model or DEFAULT_MODEL,
        permission_mode="bypassPermissions",
        max_turns=1,
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

    return await asyncio.wait_for(_run(), timeout=timeout)


def _brief_event(brief: dict) -> dict:
    takeaways = brief.get("takeaways") or []
    return {
        "kind": "daily brief",
        "date": brief.get("date"),
        "takeaways": "; ".join(
            f"[{t.get('tone')}] {t.get('headline')}"
            for t in takeaways if isinstance(t, dict)
        ),
    }


def _card_event(card: dict) -> dict:
    act = card.get("activity") or {}
    metrics = card.get("metrics") or {}
    read = card.get("coach_read") or {}
    return {
        "kind": "graded workout",
        "date": act.get("date"),
        "activity": act.get("activity_name") or act.get("activity_type"),
        "intent": card.get("intent"),
        # Severity WORDS, never the scores — the same reasoning as
        # `workout_coach.build_prompt`. A journal entry may not name a rating,
        # and handing the model the numbers is precisely how it echoed them
        # (measured 2026-07-26: 6+ of 13 hot entries carried a grade).
        "overall": workout_coach.star_severity(
            (card.get("overall") or {}).get("stars")),
        "grades": ", ".join(
            f"{k}: {workout_coach.star_severity(v.get('stars'))}"
            for k, v in metrics.items()),
        "coach_read": " ".join(str(v) for v in read.values()) if read else None,
    }


async def _reflect(source: str, source_key: str, entry_date: str | None,
                   event: dict) -> None:
    """The shared pipeline. Never raises past its own boundary."""
    if not memory.memory_enabled():
        return
    from .. import db
    from . import coach

    # ONE connection for the whole read phase (0.36.0 — was ~6 opens: the
    # has_event probe, the profile resolve x2, user_name, the ledger, the
    # recent-entries load). The SDK call and the journal WRITES stay outside:
    # writes must land on their own committed connections so a failed write
    # can't hold this read transaction open.
    with db.connect() as conn:
        if journal.has_event(source, source_key, conn=conn):
            return
        profile = coach.resolve_coach_profile(conn=conn)
        user_name = config.user_name(conn=conn)
        today = date.today().isoformat()
        led = ledger.compute_relationship_ledger(conn=conn, today=today)
        ledger_text = ledger.render_ledger_block(led, user_name)
        recent = journal.list_entries(limit=memory.FULL_ENTRIES, conn=conn)

    text = await generate_reflection(
        profile, event, ledger_text, recent, user_name=user_name)
    entries = parse_reflection(text, tuple(e["text"] for e in recent))
    for seq, entry in enumerate(entries, start=1):
        try:
            journal.save_entry(
                entry, source=source, source_key=source_key, seq=seq,
                entry_date=entry_date)
        except Exception:
            # A duplicate (source, source_key, seq) from a race, or a
            # validation edge — either way the memory is lost, nothing else.
            _LOG.warning("journal write failed (ignored)", exc_info=True)
    if entries:
        _LOG.info("coach reflect wrote %d journal entr%s for %s %s",
                  len(entries), "y" if len(entries) == 1 else "ies",
                  source, source_key)


async def reflect_after_report_card(card: dict) -> None:
    """Fire-and-forget hook target for ``tools.workout_report_card``."""
    try:
        activity_id = str((card.get("activity") or {}).get("activity_id") or "")
        if not activity_id:
            return
        await _reflect(
            "report_card", activity_id,
            (card.get("activity") or {}).get("date"), _card_event(card))
    except Exception:
        _LOG.warning("coach reflect after report card failed (ignored)",
                     exc_info=True)


def reflect_after_brief_sync(brief: dict) -> None:
    """Sync hook target for ``briefing.generate_and_save`` (called after the
    brief is saved, its own event loop already exited)."""
    try:
        brief_date = str(brief.get("date") or "")
        if not brief_date:
            return
        asyncio.run(_reflect(
            "brief", brief_date, brief_date, _brief_event(brief)))
    except Exception:
        _LOG.warning("coach reflect after brief failed (ignored)", exc_info=True)
