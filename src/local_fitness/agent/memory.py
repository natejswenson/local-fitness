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
            limit=limit + 10, db_path=db_path, conn=conn)
        if exclude_source_key is not None:
            src, key = exclude_source_key
            entries = [
                e for e in entries
                if not (e.get("source") == src and e.get("source_key") == key)
            ]
        journal_text = journal.render_journal_block(entries[:limit], user_name)

        parts = []
        if ledger_text:
            parts.append(ledger_text)
        if journal_text:
            parts.append(f"Your journal (what you wrote down):\n{journal_text}")
        text = "\n".join(parts)
        if compact:
            text = _cap_lines(text, COMPACT_MAX_CHARS)
        return text
    except Exception:
        _LOG.warning("coach memory resolution failed (ignored)", exc_info=True)
        return ""
