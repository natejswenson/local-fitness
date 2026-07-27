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
from datetime import date

from .. import config
from . import journal, ledger, memory, prompts
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
    "Rules:\n"
    "- 0, 1 or at most 2 lines. Each under 200 characters.\n"
    "- Only facts present in the event or the computed facts — never invent "
    "a count, a date, or a quote.\n"
    "- Never repeat something your recent journal already says — escalate it "
    "('second time this month') or stay silent.\n"
    "- No letter grades, no CTL/ATL/TSB numbers.\n\n"
    "# Output format — follow exactly\n"
    "Either the single word NONE, or one line per memory:\n"
    "MEMORY: <the line>\n"
    "Nothing else — no preamble, no markdown, no explanation."
)


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
    an empty generation → ``[]``. Pure, never raises."""
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
        "overall": (card.get("overall") or {}).get("grade"),
        "grades": ", ".join(
            f"{k}: {v.get('grade') or 'n/a'}" for k, v in metrics.items()),
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
