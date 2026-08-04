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

#: The pre-0.50.0 letter columns are still selected. They are the only record
#: of what a card stored under the letter rubric actually said, and a card is a
#: historical snapshot with no backfill path by design.
_LIST_COLUMNS = (
    "activity_id, activity_date, graded_at, intent, intent_class, "
    "intent_source, overall_stars, mean_stars, capped_by_metric, "
    "distance_stars, pace_stars, hr_stars, continuity_stars, "
    "overall_grade, gpa, capped_by, distance_grade, "
    "pace_grade, hr_grade, continuity_grade, load_grade"
)

# The branch rules live entirely in this WHERE guard — no SELECT precedes the
# write, so there is no read-modify-write window inside save_card. Walking it:
# equal non-NULL keys satisfy neither disjunct (keyed no-op); a differing or
# first real key satisfies the first (full overwrite, words+ratings from ONE
# render); a NULL key over a real row satisfies neither (a fallback can never
# null a real key); NULL over NULL satisfies the second (an all-fallback row
# refreshes its ratings). With no existing row the INSERT simply proceeds.
#
# The letter columns are written as NULL from 0.50.0 on. A row re-rendered
# under the star rubric therefore loses its stored letters — correct, because
# that row IS the new render; only rows never re-rendered keep theirs.
_UPSERT_SQL = """
INSERT INTO report_cards
    (activity_id, activity_date, graded_at, intent, intent_class,
     intent_source, overall_stars, mean_stars, capped_by_metric,
     distance_stars, pace_stars, hr_stars, continuity_stars,
     overall_grade, gpa, capped_by, distance_grade,
     pace_grade, hr_grade, continuity_grade, load_grade, read_cache_key,
     card_json)
VALUES (:activity_id, :activity_date, :graded_at, :intent, :intent_class,
     :intent_source, :overall_stars, :mean_stars, :capped_by_metric,
     :distance_stars, :pace_stars, :hr_stars, :continuity_stars,
     :overall_grade, :gpa, :capped_by, :distance_grade,
     :pace_grade, :hr_grade, :continuity_grade, :load_grade, :read_cache_key,
     :card_json)
ON CONFLICT(activity_id) DO UPDATE SET
    activity_date  = excluded.activity_date,
    graded_at      = excluded.graded_at,
    intent         = excluded.intent,
    intent_class   = excluded.intent_class,
    intent_source  = excluded.intent_source,
    overall_stars  = excluded.overall_stars,
    mean_stars     = excluded.mean_stars,
    capped_by_metric = excluded.capped_by_metric,
    distance_stars = excluded.distance_stars,
    pace_stars     = excluded.pace_stars,
    hr_stars       = excluded.hr_stars,
    continuity_stars = excluded.continuity_stars,
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

    def _stars(key: str) -> float | None:
        return (metrics.get(key) or {}).get("stars")

    capped_by = overall.get("capped_by")
    stored = {k: v for k, v in card.items() if k not in _STRIPPED_KEYS}
    return {
        "activity_id": activity.get("activity_id"),
        "activity_date": activity.get("date"),
        "intent": card.get("intent"),
        "intent_class": card.get("intent_class"),
        "intent_source": card.get("intent_source"),
        "overall_stars": overall.get("stars"),
        "mean_stars": overall.get("mean_stars"),
        # The metric NAME that pulled the overall down, or NULL. The old
        # `capped_by` held the literal "F"; naming the metric says which row to
        # look at, which is the only reason a reader wants the field.
        "capped_by_metric": (capped_by or {}).get("metric")
        if isinstance(capped_by, dict) else None,
        "distance_stars": _stars("distance"),
        "pace_stars": _stars("pace"),
        "hr_stars": _stars("hr"),
        "continuity_stars": _stars("continuity"),
        # The letter columns stop being written at 0.50.0. Retained rather than
        # dropped because rows stored under the letter rubric hold real letters
        # and are the only record of them, and a SQLite column drop is a table
        # rebuild for no gain — the same reasoning that kept `load_grade` when
        # load stopped being graded in 0.40.0.
        "overall_grade": None,
        "gpa": None,
        "capped_by": None,
        "distance_grade": None,
        "pace_grade": None,
        "hr_grade": None,
        "continuity_grade": None,
        "load_grade": None,
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


#: The 0.40.0 rename of the read's 4th section. Rows written before it carry
#: ``load`` where ``READ_SECTIONS`` now says ``stimulus``, which is a pure
#: naming change — the paragraph itself is the same training-load prose. See
#: ``migrate_read_section_names``.
_RENAMED_READ_SECTIONS = (("load", "stimulus"),)


def rename_read_sections(read: object) -> dict | None:
    """A stored read with ``_RENAMED_READ_SECTIONS`` applied, or ``None`` when
    there is nothing to do. Pure — no DB, no clock.

    ``None`` (not "the unchanged dict") is the no-op signal, so the caller
    writes only the rows that genuinely move. That is what makes the migration
    idempotent: a second pass finds the old key gone and returns ``None`` for
    every row.

    The rename is positional, not append-then-delete: the new key takes the
    old one's slot so the re-serialized JSON keeps its original key order and
    the row's bytes differ only where the rename does. A read already carrying
    the new key is left ALONE even if the old one is also present — two
    populated sections is not a case this can resolve by guessing, and
    silently discarding one of them would destroy a paragraph.
    """
    if not isinstance(read, dict):
        return None
    pending = [(old, new) for old, new in _RENAMED_READ_SECTIONS
               if old in read and new not in read]
    if not pending:
        return None
    swap = dict(pending)
    return {swap.get(k, k): v for k, v in read.items()}


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


def migrate_read_section_names(
    db_path: Path | None = None, conn: sqlite3.Connection | None = None
) -> int:
    """Apply ``rename_read_sections`` to every stored row. Returns rows written.

    The one-time repair for the 0.40.0 section rename. Rows written before it
    hold ``$.coach_read.load`` where ``read_is_complete`` now demands
    ``stimulus``, so they fail that check and can never be reused however well
    their cache key matches — the render regenerates the read it is already
    holding, at full SDK cost. Measured on the live corpus 2026-08-02: 12 of 15
    stored rows.

    **Idempotent** — a second pass finds no row with the old key and writes
    nothing (``rename_read_sections`` returns ``None``), so this is safe to run
    on every ``init_schema``.

    **Does not disturb the snapshot.** The UPDATE touches ``card_json`` and
    nothing else: ``graded_at``, ``read_cache_key`` and every grade column are
    left exactly as the render that produced them wrote them. Within
    ``card_json`` the only edit is the key name — the paragraph text, the key
    order and every other field survive, and the re-serialization uses
    ``card_row``'s own ``json.dumps(..., default=str)`` so a migrated row is
    byte-identical to what that render would write today. This is a repair of a
    field NAME the schema renamed underneath the row, not a regrade: no card's
    words, letters or date move.

    Never raises — a migration failure must not stop a DB from opening. Rows
    with corrupt or non-object ``card_json`` are skipped, not repaired.
    """
    def _run(c: sqlite3.Connection) -> int:
        rows = c.execute(
            "SELECT activity_id, card_json FROM report_cards").fetchall()
        written = 0
        for row in rows:
            try:
                # NOT _decode_card: that re-defaults the stripped keys, which
                # would ADD hr_trace/recent_activities/upcoming_workouts to a
                # row that deliberately stores without them.
                card = json.loads(row["card_json"])
            except (TypeError, ValueError):
                continue
            if not isinstance(card, dict):
                continue
            renamed = rename_read_sections(card.get("coach_read"))
            if renamed is None:
                continue
            card["coach_read"] = renamed
            c.execute(
                "UPDATE report_cards SET card_json = ? WHERE activity_id = ?",
                (json.dumps(card, default=str), row["activity_id"]),
            )
            written += 1
        if written:
            _LOG.info("migrated %d stored report-card read(s) to the current "
                      "section names", written)
        return written

    try:
        if conn is not None:
            return _run(conn)
        with db.connect(db_path) as c:
            return _run(c)
    except Exception:
        _LOG.warning("report-card read-section migration failed (ignored)",
                     exc_info=True)
        return 0


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
