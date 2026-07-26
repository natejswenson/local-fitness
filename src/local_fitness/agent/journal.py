"""The coach's journal — the written half of the coach's memory.

Short, dated, LLM- or user-authored memory lines ("Jul 18: blamed the heat
again — second time this month") in the ``coach_journal`` table. The ledger
(``ledger.py``) computes facts; this holds color — the part a relationship has
that a query can't produce. Writers: the reflect step after a brief or report
card (``reflect.py``, ``source`` = ``'brief'``/``'report_card'``) and the chat
tools (``source`` = ``'chat'``).

Contracts that keep it safe to inject into every prompt:
  * Hard cap ``JOURNAL_CAP`` entries — every write prunes the oldest beyond it,
    so the block's token cost is bounded forever.
  * ``ENTRY_MAX_CHARS`` per line — a memory is a line, not an essay.
  * One memory-set per reflected event: ``idx_journal_event`` (db.py) makes a
    duplicate ``(source, source_key, seq)`` insert an IntegrityError, and
    ``has_event`` is the cheap pre-check that keeps re-renders from even
    building a reflect prompt.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

from .. import db

JOURNAL_CAP = 60
ENTRY_MAX_CHARS = 240

VALID_SOURCES = frozenset({"brief", "report_card", "chat"})


def save_entry(
    text: str,
    *,
    source: str,
    source_key: str | None = None,
    seq: int = 1,
    entry_date: str | None = None,
    db_path: Path | None = None,
) -> dict:
    """Validate + insert one entry, then prune past ``JOURNAL_CAP``.

    Raises ``ValueError`` on empty/over-long text or an unknown source, and
    ``sqlite3.IntegrityError`` on a duplicate ``(source, source_key, seq)`` —
    callers decide whether that's a race to swallow (reflect) or a bug.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("journal entry text is required")
    if len(text) > ENTRY_MAX_CHARS:
        raise ValueError(
            f"journal entry too long ({len(text)} chars, max {ENTRY_MAX_CHARS})")
    if source not in VALID_SOURCES:
        raise ValueError(
            f"unknown journal source '{source}', expected one of "
            f"{sorted(VALID_SOURCES)}")
    entry_date = entry_date or date.today().isoformat()
    created_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO coach_journal "
            "(created_at, entry_date, source, source_key, seq, text) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (created_at, entry_date, source, source_key, seq, text),
        )
        entry_id = cur.lastrowid
        prune(conn)
    return {
        "entry_id": entry_id,
        "created_at": created_at,
        "entry_date": entry_date,
        "source": source,
        "source_key": source_key,
        "seq": seq,
        "text": text,
    }


def has_event(
    source: str,
    source_key: str,
    db_path: Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Whether the event was already reflected on — the idempotency pre-check
    that keeps a re-render from paying an SDK call (or writing a duplicate)."""
    sql = ("SELECT 1 FROM coach_journal WHERE source = ? AND source_key = ? "
           "LIMIT 1")
    if conn is not None:
        return conn.execute(sql, (source, source_key)).fetchone() is not None
    with db.connect(db_path) as c:
        return c.execute(sql, (source, source_key)).fetchone() is not None


def list_entries(
    days: int | None = None,
    limit: int = 50,
    db_path: Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """Newest-first entries, optionally restricted to the trailing ``days``."""
    sql = ("SELECT entry_id, created_at, entry_date, source, source_key, seq, "
           "text FROM coach_journal")
    params: list = []
    if days is not None:
        sql += " WHERE entry_date >= date('now', ?)"
        params.append(f"-{int(days)} days")
    sql += " ORDER BY entry_date DESC, entry_id DESC LIMIT ?"
    params.append(int(limit))

    def _run(c: sqlite3.Connection) -> list[dict]:
        return [dict(r) for r in c.execute(sql, params)]

    if conn is not None:
        return _run(conn)
    with db.connect(db_path) as c:
        return _run(c)


def delete_entry(entry_id: int, db_path: Path | None = None) -> bool:
    with db.connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM coach_journal WHERE entry_id = ?", (int(entry_id),))
        return cur.rowcount > 0


def prune(conn: sqlite3.Connection, cap: int = JOURNAL_CAP) -> int:
    """Delete everything but the ``cap`` newest entries. Returns rows removed."""
    cur = conn.execute(
        "DELETE FROM coach_journal WHERE entry_id NOT IN ("
        "SELECT entry_id FROM coach_journal "
        "ORDER BY entry_date DESC, entry_id DESC LIMIT ?)",
        (int(cap),),
    )
    return cur.rowcount


# --- rendering (pure) -------------------------------------------------------


def render_journal_block(entries: list[dict], user_name: str) -> str:
    """Journal lines for the prompt, newest first, ``Jul 18: <text>`` style.
    Pure — entries are passed in, never read here. Empty → ``""``."""
    lines = []
    for e in entries:
        text = (e.get("text") or "").strip()
        if not text:
            continue
        day = e.get("entry_date") or ""
        try:
            d = date.fromisoformat(day)
            day = f"{d:%b} {d.day}"
        except (ValueError, TypeError):
            pass
        lines.append(f"- {day}: {text}" if day else f"- {text}")
    return "\n".join(lines)
