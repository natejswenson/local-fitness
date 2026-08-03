"""The coach's journal — the written half of the coach's memory.

Short, dated, LLM- or user-authored memory lines ("Jul 18: blamed the heat
again — second time this month") in the ``coach_journal`` table. The ledger
(``ledger.py``) computes facts; this holds color — the part a relationship has
that a query can't produce. Writers: the reflect step after a brief or report
card (``reflect.py``, ``source`` = ``'brief'``/``'report_card'``) and the chat
tools (``source`` = ``'chat'``).

Contracts that keep it safe to inject into every prompt:
  * Hard cap ``JOURNAL_CAP`` *hot* entries — every write archives the oldest
    beyond the cap (``archived = 1``, never DELETE), so the prompt block's
    token cost stays bounded forever while the journal itself never forgets.
    ``search_entries`` (FTS5 BM25, LIKE fallback) reaches the whole journal,
    archive included; only ``delete_entry`` (user-requested forgets) removes.
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
    """Validate + insert one entry, then archive past ``JOURNAL_CAP``.

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
        archive_overflow(conn)
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
    *,
    include_archived: bool = False,
    on_or_before: str | None = None,
) -> list[dict]:
    """Newest-first entries, optionally restricted to the trailing ``days``.

    Default is the hot (unarchived) set — what injection and reflect see;
    ``include_archived=True`` opens the whole journal for browsing.

    ``on_or_before`` (ISO date) caps the window at the top, so a caller can ask
    what the journal held *as of* a past date rather than what it holds now.
    Off by default — every existing caller keeps the unbounded latest-N view.
    ``memory.render_memory_for_prompt`` passes it whenever it is given a
    ``today``, which is what makes a past report card's prompt (and therefore
    its read cache key) stop moving every time an unrelated entry is written:
    an entry dated after the card can no longer enter that card's memory.
    """
    sql = ("SELECT entry_id, created_at, entry_date, source, source_key, seq, "
           "text, archived FROM coach_journal")
    conditions: list[str] = []
    params: list = []
    if not include_archived:
        conditions.append("archived = 0")
    if days is not None:
        conditions.append("entry_date >= date('now', ?)")
        params.append(f"-{int(days)} days")
    if on_or_before is not None:
        conditions.append("entry_date <= ?")
        params.append(str(on_or_before))
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    # entry_date DESC first: a report_card reflection can be graded on-demand
    # long after the activity happened (normal usage — Nate grades older
    # cards after the fact), so entry_id (write order) runs INVERSE to
    # entry_date for a large share of real rows. Sorting by entry_id alone
    # was tried (0.38.2) and reverted: it silently dropped the newest EVENTS
    # whenever they were graded/written out of order, which is the common
    # case, not the exception. entry_id DESC is only the tiebreak, for two
    # entries that share one entry_date — there it correctly puts the one
    # WRITTEN later (e.g. a same-day correction) above the one it corrects.
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


def archive_overflow(conn: sqlite3.Connection, cap: int = JOURNAL_CAP) -> int:
    """Archive everything but the ``cap`` newest hot entries. Returns rows
    flipped. An UPDATE of the flag only — the FTS index (which triggers on
    ``UPDATE OF text``) never churns, and nothing is ever deleted here."""
    cur = conn.execute(
        "UPDATE coach_journal SET archived = 1 "
        "WHERE archived = 0 AND entry_id NOT IN ("
        "SELECT entry_id FROM coach_journal WHERE archived = 0 "
        "ORDER BY entry_date DESC, entry_id DESC LIMIT ?)",
        (int(cap),),
    )
    return cur.rowcount


# --- search ------------------------------------------------------------------


def _fts_query(raw: str) -> str:
    """Sanitize a user query into FTS5 MATCH syntax: every whitespace token
    becomes a quoted phrase (implicit AND), so MATCH operators (``NEAR(``,
    ``col:``, ``*``, stray quotes) are inert data, never syntax.

    Raises ``ValueError`` when nothing searchable survives."""
    tokens = [t for t in (raw or "").split() if any(c.isalnum() for c in t)]
    if not tokens:
        raise ValueError("query has no searchable words")
    return " ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def _fts_available(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'coach_journal_fts'").fetchone()
    return row is not None


_SEARCH_COLS = ("j.entry_id, j.created_at, j.entry_date, j.source, "
                "j.source_key, j.seq, j.text, j.archived")


def _search_like(
    conn: sqlite3.Connection, raw: str, limit: int
) -> list[dict]:
    """Substring fallback for FTS5-less builds: AND-joined LIKE per token,
    newest-first (no relevance ranking available)."""
    tokens = [t for t in (raw or "").split() if any(c.isalnum() for c in t)]
    if not tokens:
        raise ValueError("query has no searchable words")
    conds, params = [], []
    for t in tokens:
        escaped = t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conds.append(r"j.text LIKE '%' || ? || '%' ESCAPE '\'")
        params.append(escaped)
    params.append(int(limit))
    rows = conn.execute(
        f"SELECT {_SEARCH_COLS} FROM coach_journal j "
        f"WHERE {' AND '.join(conds)} "
        "ORDER BY j.entry_date DESC, j.entry_id DESC LIMIT ?",
        params,
    )
    return [dict(r) for r in rows]


def search_entries(
    query: str,
    *,
    limit: int = 8,
    db_path: Path | None = None,
) -> tuple[list[dict], str]:
    """Keyword search over the WHOLE journal — hot and archived alike.

    Returns ``(matches, mode)`` where mode is ``"fts"`` (BM25 best-first) or
    ``"like"`` (substring, newest-first — used when the SQLite build lacks
    FTS5, or as a belt-and-suspenders catch if MATCH still errors).
    Raises ``ValueError`` only on an unsearchable (empty/punctuation) query.
    """
    match = _fts_query(query)  # validates even when we fall back to LIKE
    with db.connect(db_path) as conn:
        if _fts_available(conn):
            try:
                rows = conn.execute(
                    f"SELECT {_SEARCH_COLS} FROM coach_journal_fts "
                    "JOIN coach_journal j "
                    "ON j.entry_id = coach_journal_fts.rowid "
                    "WHERE coach_journal_fts MATCH ? "
                    "ORDER BY rank LIMIT ?",
                    (match, int(limit)),
                )
                return [dict(r) for r in rows], "fts"
            except sqlite3.OperationalError:
                pass
        return _search_like(conn, query, int(limit)), "like"


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
