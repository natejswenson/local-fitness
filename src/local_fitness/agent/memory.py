"""The one resolver that assembles the coach's memory for a prompt.

Composes the two memory layers — the deterministic ledger (``ledger.py``) and
the coach journal (``journal.py``) — into the single text block every voice
surface injects via ``prompts.coach_memory_block``. This module does the I/O;
the prompt builders stay pure (``memory_text`` is passed in, never read there)
because ``plan_coach``/``workout_coach`` key their disk caches on the
assembled-prompt hash.

``LOCAL_FITNESS_COACH_MEMORY=0`` is the feature's instant-rollback kill
switch: it empties this resolver (so every surface loses the memory section)
AND disables both auto-reflect hooks (``reflect.py`` checks it too). Journal
data is never touched by the switch.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from . import journal, ledger

_LOG = logging.getLogger(__name__)

#: Journal entries shown on full surfaces (chat, PDF coaches) vs the compact
#: V2 brief. The cap bounds prompt cost; the journal itself keeps 60.
FULL_ENTRIES = 10
COMPACT_ENTRIES = 3

#: Hard character budget for the compact variant. V2 is deliberately the
#: shrunk prompt — the agent/code-separation cutover moved work out of it on
#: purpose — so memory must not silently grow it. Enforced by truncating whole
#: lines (never mid-line: a cut-off receipt invites an invented completion).
COMPACT_MAX_CHARS = 600

#: Floor reserved for the journal half of the compact budget when any journal
#: entries exist, so a long ledger can never crowd the journal out entirely
#: (0.38.2 bug: capping the whole joined text popped every journal line but
#: left the "Your journal" header standing — an empty banner in the V2
#: brief, the highest-frequency surface, invites invention rather than
#: silence). Widened per-render if the single newest journal line alone is
#: longer than this floor (see ``_compact_journal_block``).
_JOURNAL_RESERVE_CHARS = 200


def memory_enabled() -> bool:
    import os

    return os.environ.get(
        "LOCAL_FITNESS_COACH_MEMORY", "1"
    ).strip().lower() not in ("0", "false", "no", "off")


def _cap_lines(text: str, max_chars: int) -> str:
    """Trim trailing whole lines until the block fits ``max_chars``."""
    if len(text) <= max_chars:
        return text
    lines = text.splitlines()
    while lines and len("\n".join(lines)) > max_chars:
        lines.pop()
    return "\n".join(lines)


_JOURNAL_HEADER = "Your journal (what you wrote down):\n"


def _compact_journal_block(journal_text: str) -> str:
    """The journal half of the compact render, header included, or ``""``.

    Caps to :data:`_JOURNAL_RESERVE_CHARS` by whole line like ``_cap_lines``,
    but guarantees at least the single newest line survives even if that one
    line alone is longer than the reserve — losing the whole journal to one
    long entry is worse than a slightly larger block (the ledger side gives
    up the difference; see ``render_memory_for_prompt``).
    """
    if not journal_text:
        return ""
    first_line = journal_text.splitlines()[0]
    reserve = max(_JOURNAL_RESERVE_CHARS, len(first_line))
    capped = _cap_lines(journal_text, reserve) or first_line
    return f"{_JOURNAL_HEADER}{capped}"


def render_memory_for_prompt(
    *,
    db_path: Path | None = None,
    conn: sqlite3.Connection | None = None,
    today: str | None = None,
    exclude_source_key: tuple[str, str] | None = None,
    compact: bool = False,
    user_name: str = "the user",
) -> str:
    """The assembled memory text, or ``""`` when disabled, empty, or broken.

    ``exclude_source_key`` drops journal entries about the artifact being
    generated (e.g. ``("report_card", "<activity_id>")``) so the reflect step
    can't cascade: without it, reflecting on a card would change that same
    card's next prompt, bust its cache, and regenerate forever.

    ``today`` means *as of* that date and is honoured by BOTH layers: the
    ledger already computed its facts against it, and the journal is now
    capped at it too (``on_or_before``). Half-anchoring was a real defect
    rather than an omission — a caller rendering a past artifact got that
    date's ledger beside journal lines written weeks later, so the block
    described two different moments at once, and any entry written anywhere
    changed the text of every past artifact's prompt. ``None`` (chat, the MCP
    persona) keeps the unbounded live view, which is correct there: those
    surfaces are about now.

    Never raises — a memory failure must never cost a brief, a card, or a
    chat session. Failure → ``""`` + a logged warning.
    """
    if not memory_enabled():
        return ""
    try:
        led = ledger.compute_relationship_ledger(
            db_path=db_path, conn=conn, today=today)
        ledger_text = ledger.render_ledger_block(led, user_name)
        if compact:
            # The compact ledger drops observation patterns — the brief's
            # planner already carries recovery signals with real numbers.
            led_compact = dict(led, patterns=[])
            ledger_text = ledger.render_ledger_block(led_compact, user_name)

        limit = COMPACT_ENTRIES if compact else FULL_ENTRIES
        entries = journal.list_entries(
            limit=limit + 10, db_path=db_path, conn=conn,
            on_or_before=today)
        if exclude_source_key is not None:
            src, key = exclude_source_key
            entries = [
                e for e in entries
                if not (e.get("source") == src and e.get("source_key") == key)
            ]
        journal_text = journal.render_journal_block(entries[:limit], user_name)

        if compact:
            # Journal gets its guaranteed slice FIRST (fixed by content, not
            # a flat share), then the ledger gets whatever budget remains —
            # never the reverse, or a long ledger silently evicts every
            # journal line while leaving the header standing (0.38.2 bug).
            journal_block = _compact_journal_block(journal_text)
            ledger_budget = COMPACT_MAX_CHARS - (
                len(journal_block) + 1 if journal_block else 0)
            ledger_capped = (
                _cap_lines(ledger_text, max(0, ledger_budget))
                if ledger_text else "")
            parts = [p for p in (ledger_capped, journal_block) if p]
            text = "\n".join(parts)
            # Belt-and-suspenders: the split above already guarantees the
            # total fits, so this is a no-op in practice, never one that can
            # strip the journal block back out from under the guarantee.
            return _cap_lines(text, COMPACT_MAX_CHARS)

        parts = []
        if ledger_text:
            parts.append(ledger_text)
        if journal_text:
            parts.append(f"Your journal (what you wrote down):\n{journal_text}")
        return "\n".join(parts)
    except Exception:
        _LOG.warning("coach memory resolution failed (ignored)", exc_info=True)
        return ""
