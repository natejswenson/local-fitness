"""Persisted report cards — the coach's durable memory of graded workouts.

Each row in ``report_cards`` is a **dated snapshot**: the card Nate was
actually shown, as graded on ``graded_at``, against the plan active at that
render. Grades are NOT recomputable history (``build_card`` grades against
the currently-active plan), so a stored card is a historical record, never
a live view — and never backfilled.

Key identity is the save discriminator. A stored row represents one
*logical render*, identified by its ``read_cache_key`` (the workout-coach
prompt hash). ``save_card`` writes the whole row or writes nothing, decided
entirely by the incoming key vs. the stored key inside ONE atomic guarded
UPSERT — an equal-key render is a byte-identical no-op, and a fallback
(NULL-key) render never overwrites a real-read row. That is what keeps a
stored card internally consistent: its ``coach_read`` and its grade columns
always originate from the same render, so the card can never print words
that contradict the grade table beside them.

Load-bearing assumptions, do not regress:
  * **No delete/prune path exists.** The only mutation is ``save_card``'s
    guarded UPSERT. The fast path in ``tools.workout_report_card`` reads
    ``load_read`` *outside* that guard; a concurrent pruning delete would
    turn that benign window corrupting. A future GC tool must revisit the
    fast path first.
  * **Never inject these rows into ``render_memory_for_prompt`` as prose or
    raw rows.** Memory text is inside the plan/workout-coach prompt hashes,
    and ``exclude_source_key`` is journal-scoped — folding cards in would
    bust those caches on every write and re-open the self-render cascade.
    The ONE sanctioned exception (0.34.0) is ``ledger.report_card_facts``:
    a deterministic AGGREGATE (count/mean GPA/grade distribution/trend —
    numbers only, never ``card_json``/``coach_read``) restricted to
    ``activity_date`` strictly before today, and idempotent under
    equal-grade re-saves — that cutoff+idempotence pair IS its "parallel
    exclusion". Any other injection (raw rows, prose, or a ``graded_at``-
    scoped fact — ``graded_at`` mutates on every distinct-key re-render, so
    a fact keyed on it would change when a card is merely *viewed*) still
    needs its own exclusion analysis first.

Pure half (``card_row``, ``read_is_complete``) works on plain dicts and
never reads the clock or the DB; the persistence half stamps ``graded_at``
and owns the one write. Same divider as ``journal.py``/``plans.py``.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from .. import db
from .report_card import READ_SECTIONS

_LOG = logging.getLogger(__name__)

#: 5s: a briefly-locked DB (another local render committing) waits instead of
#: dropping the save into the fail-silent except. ``db.connect()`` itself sets
#: no busy_timeout, so this is per-save-connection, not global.
BUSY_TIMEOUT_MS = 5000

#: Presentation- and prompt-only card keys — reproducible from the DB and the
#: bulk of the bytes, so storage strips them. Re-defaulted on load purely for
#: shape parity with a freshly built card (``render_markdown`` reads none).
_STRIPPED_KEYS = ("hr_trace", "recent_activities", "upcoming_workouts")

INTENT_CLASSES = ("easy", "long", "quality", "steady")

_LIST_COLUMNS = (
    "activity_id, activity_date, graded_at, intent, intent_class, "
    "intent_source, overall_grade, gpa, capped_by, distance_grade, "
    "pace_grade, hr_grade, continuity_grade, load_grade"
)

# The branch rules live entirely in this WHERE guard — no SELECT precedes the
# write, so there is no read-modify-write window inside save_card. Walking it:
# equal non-NULL keys satisfy neither disjunct (keyed no-op); a differing or
# first real key satisfies the first (full overwrite, words+grades from ONE
# render); a NULL key over a real row satisfies neither (a fallback can never
# null a real key); NULL over NULL satisfies the second (an all-fallback row
# refreshes its grades). With no existing row the INSERT simply proceeds.
_UPSERT_SQL = """
INSERT INTO report_cards
    (activity_id, activity_date, graded_at, intent, intent_class,
     intent_source, overall_grade, gpa, capped_by, distance_grade,
     pace_grade, hr_grade, continuity_grade, load_grade, read_cache_key,
     card_json)
VALUES (:activity_id, :activity_date, :graded_at, :intent, :intent_class,
     :intent_source, :overall_grade, :gpa, :capped_by, :distance_grade,
     :pace_grade, :hr_grade, :continuity_grade, :load_grade, :read_cache_key,
     :card_json)
ON CONFLICT(activity_id) DO UPDATE SET
    activity_date  = excluded.activity_date,
    graded_at      = excluded.graded_at,
    intent         = excluded.intent,
    intent_class   = excluded.intent_class,
    intent_source  = excluded.intent_source,
    overall_grade  = excluded.overall_grade,
    gpa            = excluded.gpa,
    capped_by      = excluded.capped_by,
    distance_grade = excluded.distance_grade,
    pace_grade     = excluded.pace_grade,
    hr_grade       = excluded.hr_grade,
    continuity_grade = excluded.continuity_grade,
    load_grade     = excluded.load_grade,
    read_cache_key = excluded.read_cache_key,
    card_json      = excluded.card_json
WHERE
    (excluded.read_cache_key IS NOT NULL
     AND (report_cards.read_cache_key IS NULL
          OR excluded.read_cache_key <> report_cards.read_cache_key))
    OR (excluded.read_cache_key IS NULL
        AND report_cards.read_cache_key IS NULL)
"""


# --- pure half --------------------------------------------------------------


def card_row(card: dict, *, read_cache_key: str | None) -> dict:
    """Reduce a card dict to the row payload. Pure — never reads the clock
    (``graded_at`` is ``save_card``'s concern) and never touches the DB."""
    activity = card.get("activity") or {}
    overall = card.get("overall") or {}
    metrics = card.get("metrics") or {}

    def _grade(key: str) -> str | None:
        return (metrics.get(key) or {}).get("grade")

    stored = {k: v for k, v in card.items() if k not in _STRIPPED_KEYS}
    return {
        "activity_id": activity.get("activity_id"),
        "activity_date": activity.get("date"),
        "intent": card.get("intent"),
        "intent_class": card.get("intent_class"),
        "intent_source": card.get("intent_source"),
        "overall_grade": overall.get("grade"),
        "gpa": overall.get("gpa"),
        "capped_by": overall.get("capped_by"),
        "distance_grade": _grade("distance"),
        "pace_grade": _grade("pace"),
        "hr_grade": _grade("hr"),
        "continuity_grade": _grade("continuity"),
        # Always NULL from 0.40.0 on — load is a stimulus metric and carries no
        # grade. The column is retained rather than dropped because rows graded
        # before the split hold real letters, and a SQLite column drop is a table
        # rebuild for no gain.
        "load_grade": _grade("load"),
        "read_cache_key": read_cache_key,
        "card_json": json.dumps(stored, default=str),
    }


def read_is_complete(read: object) -> bool:
    """Whether a stored read is safe to reuse for display: a dict carrying all
    four ``READ_SECTIONS`` keys, each a non-empty string. ``None`` (an older
    row, or one whose read failed to decode) is a miss. Display-side
    corruption defense only — a regenerated read hashes to the same key, so
    the store is never rewritten by a failed check."""
    if not isinstance(read, dict):
        return False
    for key, _label in READ_SECTIONS:
        value = read.get(key)
        if not isinstance(value, str) or not value.strip():
            return False
    return True


def _decode_card(card_json: str) -> dict | None:
    """card_json → card dict with the stripped keys re-defaulted, or ``None``
    on a corrupt row — the loaders' fail-silent contract."""
    try:
        card = json.loads(card_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(card, dict):
        return None
    for key in _STRIPPED_KEYS:
        card.setdefault(key, [])
    return card


# --- persistence ------------------------------------------------------------


def save_card(card: dict, *, read_cache_key: str | None,
              db_path: Path | None = None) -> None:
    """Persist the card via the atomic guarded UPSERT. **Never raises** — a
    save failure must never fail a render; it logs and returns. Synchronous:
    the async call site awaits it via ``asyncio.to_thread`` so the
    ``busy_timeout`` wait can never block the event loop."""
    try:
        row = card_row(card, read_cache_key=read_cache_key)
        if row["activity_id"] is None or not row["activity_date"]:
            _LOG.warning(
                "report-card save skipped: card has no activity_id/date")
            return
        row["graded_at"] = datetime.now().isoformat(timespec="seconds")
        with db.connect(db_path) as conn:
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            conn.execute(_UPSERT_SQL, row)
    except Exception:
        _LOG.warning("report-card save failed (ignored)", exc_info=True)


def load_card(activity_id: int, *, db_path: Path | None = None) -> dict | None:
    """The stored row for one activity — extracted columns plus the decoded
    ``card`` — or ``None`` when absent or the row's card_json is corrupt."""
    with db.connect(db_path) as conn:
        row = conn.execute(
            f"SELECT {_LIST_COLUMNS}, read_cache_key, card_json "
            "FROM report_cards WHERE activity_id = ?",
            (int(activity_id),),
        ).fetchone()
    if row is None:
        return None
    card = _decode_card(row["card_json"])
    if card is None:
        _LOG.warning(
            "report-card row for activity %s has undecodable card_json — "
            "treating as no stored card", activity_id)
        return None
    out = {k: row[k] for k in row.keys() if k != "card_json"}
    out["card"] = card
    return out


def list_cards(
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    intent_class: str | None = None,
    limit: int = 20,
    db_path: Path | None = None,
) -> list[dict]:
    """Stored cards, newest run first (``activity_date DESC`` — the index and
    the "trend my runs" ordering; ``graded_at`` would surface a re-rendered
    old run above a newer one). Extracted columns only — no JSON decode."""
    sql = f"SELECT {_LIST_COLUMNS} FROM report_cards"
    where: list[str] = []
    params: list = []
    if start_date:
        where.append("activity_date >= ?")
        params.append(start_date)
    if end_date:
        where.append("activity_date <= ?")
        params.append(end_date)
    if intent_class:
        where.append("intent_class = ?")
        params.append(intent_class)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY activity_date DESC, activity_id DESC LIMIT ?"
    params.append(int(limit))
    with db.connect(db_path) as conn:
        return [dict(r) for r in conn.execute(sql, params)]


# json_extract pulls ONLY the coach_read fragment rather than handing Python
# the whole card_json snapshot to decode — the render fast path needs four
# paragraphs, not the metrics/splits/reference/HR tree around them, and a
# stored card is the largest row in the schema.
#
# The json_valid() guard is load-bearing, not defensive dressing: SQLite's
# json_extract RAISES "malformed JSON" on a corrupt card_json, which would
# turn a corrupt row into a whole-lookup miss (None) — but the contract is
# that the KEY still comes back, with an unusable read (key, None), so a
# corrupt row's next render regenerates and re-saves under the same key
# instead of being treated as absent. json_valid returns 0 instead of
# raising, so the corrupt case lands on SQL NULL and keeps the key.
_READ_SQL = """
SELECT read_cache_key,
       CASE WHEN json_valid(card_json)
            THEN json_extract(card_json, '$.coach_read') END AS read_json
FROM report_cards WHERE activity_id = ?
"""


def _read_row(conn, activity_id: int):
    return conn.execute(_READ_SQL, (int(activity_id),)).fetchone()


def _decode_read(raw: object) -> dict | None:
    """A ``$.coach_read`` fragment → the read dict, or ``None``.

    SQLite hands back JSON *text* for an object (hence the decode) and SQL
    NULL when the path is absent or the row was unparseable. Anything that
    isn't a decodable object is ``None`` — the same fail-silent shape
    ``_decode_card`` has, so display-side corruption stays a cache miss."""
    if not isinstance(raw, str):
        return None
    try:
        read = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return read if isinstance(read, dict) else None


def load_read(
    activity_id: int, *, db_path: Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[str | None, dict | None] | None:
    """``(read_cache_key, coach_read)`` for the stored row, or ``None``.

    The render fast path's lookup, so it is fail-silent end to end: any
    error — a missing table, a locked DB, a corrupt row — is a miss, never
    a failed render. A corrupt ``card_json`` makes ``json_extract`` itself
    raise, which lands in the same except and is still a miss.

    Accepts an already-open ``conn`` so the report-card path can share the
    one connection it already holds (mirrors ``db.get_setting``)."""
    try:
        if conn is not None:
            row = _read_row(conn, activity_id)
        else:
            with db.connect(db_path) as c:
                row = _read_row(c, activity_id)
        if row is None:
            return None
        return (row["read_cache_key"], _decode_read(row["read_json"]))
    except Exception:
        _LOG.warning(
            "report-card read lookup failed for activity %s (treated as a "
            "miss)", activity_id, exc_info=True)
        return None
