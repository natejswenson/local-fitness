"""card_store — the persisted report-card snapshot store.

Pure half with plain dicts; persistence half against a tmp DB. The guarded
UPSERT's four key-pair branches are pinned directly (the guard IS the design),
and the never-raises / fail-silent contracts are exercised for real.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime

import pytest

from local_fitness import db
from local_fitness.agent import card_store
from local_fitness.agent import report_card as rc

REF = {
    "mode": "rolling_60d", "n": 20, "pool": "running",
    "median_distance_m": 10000.0, "median_pace_sec_per_km": 300.0,
    "median_hr": 150.0, "median_load": 100.0,
}
ACTIVITY = {
    "activity_id": 1, "date": "2026-07-19", "activity_name": "Morning Run",
    "activity_type": "running", "distance_meters": 10000,
    "duration_seconds": 3000, "avg_pace_sec_per_km": 300, "avg_hr": 150,
    "training_load": 100,
}
READ = {
    "distance": "covered the ground.", "pace": "too quick.",
    "hr": "stayed low.", "stimulus": "banked what it should.",
}


def a_card(activity=None, *, read=READ, hr_samples=None):
    """A REAL card via build_card (fabricated inputs), with a read attached
    the way tools.workout_report_card attaches one."""
    card = rc.build_card(
        {**ACTIVITY, **(activity or {})}, [], None, REF, {}, hr_samples,
        [{"prompt_only": True}], [{"prompt_only": True}])
    card["coach_read"] = dict(read)
    return card


@pytest.fixture
def cdb(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    return p


def raw_row(activity_id=1):
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM report_cards WHERE activity_id = ?",
            (activity_id,)).fetchone()
    return dict(row) if row is not None else None


@pytest.fixture
def clock(monkeypatch):
    """Deterministic save_card clock — graded_at assertions must not depend
    on two saves landing in different wall-clock seconds."""
    state = {"now": datetime(2026, 7, 23, 7, 0, 0)}

    class _FakeDT:
        @staticmethod
        def now():
            return state["now"]

    monkeypatch.setattr(card_store, "datetime", _FakeDT)

    def tick(minute=1):
        state["now"] = datetime(2026, 7, 23, 7, minute, 0)

    return tick


# --- pure half ---------------------------------------------------------------


def test_card_row_extracts_the_actual_scores():
    card = a_card()
    row = card_store.card_row(card, read_cache_key="k1")
    assert row["activity_id"] == 1
    assert row["activity_date"] == "2026-07-19"
    assert row["intent"] == card["intent"]
    assert row["intent_class"] == card["intent_class"]
    assert row["intent_source"] == "inferred"
    assert row["overall_stars"] == card["overall"]["stars"]
    assert row["mean_stars"] == card["overall"]["mean_stars"]
    assert row["distance_stars"] == card["metrics"]["distance"]["stars"]
    assert row["pace_stars"] == card["metrics"]["pace"]["stars"]
    assert row["hr_stars"] == card["metrics"]["hr"]["stars"]
    # An uncapped card names no metric.
    assert row["capped_by_metric"] is None
    # Load carries no score at all (0.40.0) — asserted as a VALUE, not a
    # round-trip: `== card[...]["stars"]` would pass vacuously with both sides
    # None and would not notice a score reappearing.
    assert card["metrics"]["load"]["stars"] is None
    # The letter columns stop being written at 0.50.0. Same reasoning as
    # load_grade: kept for rows stored under the old rubric, never written now.
    for legacy in ("overall_grade", "gpa", "capped_by", "distance_grade",
                   "pace_grade", "hr_grade", "continuity_grade", "load_grade"):
        assert row[legacy] is None, legacy
    assert row["read_cache_key"] == "k1"
    # Pure: graded_at is save_card's concern, never card_row's.
    assert "graded_at" not in row


def test_card_row_strips_the_reproducible_keys_and_keeps_the_read():
    stored = json.loads(
        card_store.card_row(a_card(), read_cache_key=None)["card_json"])
    for key in ("hr_trace", "recent_activities", "upcoming_workouts"):
        assert key not in stored
    assert stored["coach_read"] == READ
    assert stored["metrics"]["distance"]["stars"] is not None


def test_read_is_complete_requires_all_four_nonempty_sections():
    assert card_store.read_is_complete(READ) is True
    assert card_store.read_is_complete(None) is False
    assert card_store.read_is_complete("DISTANCE: text") is False
    assert card_store.read_is_complete({**READ, "stimulus": ""}) is False
    assert card_store.read_is_complete(
        {k: v for k, v in READ.items() if k != "hr"}) is False


# --- save/load round trip ----------------------------------------------------


def test_save_load_round_trip_pins_columns_and_renders_markdown(cdb):
    card = a_card()
    card_store.save_card(card, read_cache_key="k1")
    loaded = card_store.load_card(1)
    assert loaded["activity_date"] == "2026-07-19"
    assert loaded["overall_stars"] == card["overall"]["stars"]
    assert loaded["distance_stars"] == card["metrics"]["distance"]["stars"]
    assert loaded["read_cache_key"] == "k1"
    # graded_at was stamped by save_card in journal.py's timestamp format.
    datetime.fromisoformat(loaded["graded_at"])
    # Stripped keys come back re-defaulted, and the snapshot still renders.
    assert loaded["card"]["hr_trace"] == []
    assert loaded["card"]["recent_activities"] == []
    assert loaded["card"]["coach_read"] == READ
    markdown = rc.render_markdown(loaded["card"])
    assert markdown.startswith("# Report Card")
    assert "covered the ground." in markdown


def test_load_card_missing_and_corrupt_rows_are_none(cdb):
    assert card_store.load_card(999) is None
    card_store.save_card(a_card(), read_cache_key="k1")
    with db.connect() as conn:
        conn.execute(
            "UPDATE report_cards SET card_json = '{not json' "
            "WHERE activity_id = 1")
    assert card_store.load_card(1) is None


# --- the guarded UPSERT: four key-pair branches ------------------------------


def test_equal_key_resave_is_a_byte_identical_noop(cdb, clock):
    """The keyed no-op: a fast-path / file-cache hit re-invokes save_card with
    the stored render's key but DIFFERENT recomputed grades (the within-bucket
    drift that hashes identically) — nothing may move, graded_at included."""
    card_store.save_card(a_card(), read_cache_key="k1")
    before = raw_row()
    clock()  # a later save would get a different graded_at — if it wrote
    drifted = a_card(activity={"distance_meters": 9000})  # grades move
    assert (drifted["metrics"]["distance"]["deviation"]
            != a_card()["metrics"]["distance"]["deviation"])
    card_store.save_card(drifted, read_cache_key="k1")
    assert raw_row() == before


def test_differing_key_overwrites_the_whole_row(cdb, clock):
    card_store.save_card(a_card(), read_cache_key="k1")
    before = raw_row()
    clock()
    new_read = {**READ, "distance": "a different verdict."}
    card_store.save_card(
        a_card(activity={"distance_meters": 12000}, read=new_read),
        read_cache_key="k2")
    after = raw_row()
    assert after["read_cache_key"] == "k2"
    assert after["graded_at"] != before["graded_at"]
    # Words AND scores moved together — one render's whole row.
    stored = json.loads(after["card_json"])
    assert stored["coach_read"]["distance"] == "a different verdict."
    assert after["distance_stars"] == rc.build_card(
        {**ACTIVITY, "distance_meters": 12000}, [], None, REF, {}, None,
        None, None)["metrics"]["distance"]["stars"]


def test_fallback_never_clobbers_a_real_read_row(cdb, clock):
    """NULL over non-NULL → whole-row no-op: neither the words nor the scores
    nor graded_at move, proving there is no field-level splice."""
    card_store.save_card(a_card(), read_cache_key="k1")
    before = raw_row()
    clock()
    fallback_render = a_card(
        activity={"distance_meters": 14000},
        read={k: f"template {k}." for k in READ})
    card_store.save_card(fallback_render, read_cache_key=None)
    assert raw_row() == before


def test_all_fallback_row_refreshes_on_a_later_fallback(cdb, clock):
    """NULL over NULL → refresh: the first-ever render always persists, and a
    later all-fallback render updates that row's grades (key stays NULL)."""
    card_store.save_card(a_card(), read_cache_key=None)
    first = raw_row()
    assert first["read_cache_key"] is None
    clock()
    card_store.save_card(
        a_card(activity={"distance_meters": 12000}), read_cache_key=None)
    after = raw_row()
    assert after["read_cache_key"] is None
    assert after["graded_at"] != first["graded_at"]
    assert after["card_json"] != first["card_json"]


def test_real_read_overwrites_an_all_fallback_row(cdb):
    card_store.save_card(a_card(), read_cache_key=None)
    card_store.save_card(a_card(), read_cache_key="k1")
    assert raw_row()["read_cache_key"] == "k1"


# --- save_card contracts -----------------------------------------------------


def test_save_card_never_raises_and_drops_the_save_on_db_failure(cdb):
    @contextmanager
    def boom(db_path=None):
        raise RuntimeError("db unavailable")
        yield  # pragma: no cover

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(card_store.db, "connect", boom)
        card_store.save_card(a_card(), read_cache_key="k1")  # must not raise
    assert card_store.load_card(1) is None


def test_save_card_skips_a_card_without_identity(cdb):
    card = a_card()
    card["activity"].pop("activity_id")
    card_store.save_card(card, read_cache_key="k1")
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM report_cards").fetchone()[0]
    assert n == 0


def test_save_card_sets_busy_timeout_and_issues_no_select(cdb, monkeypatch):
    """The atomic-guard contract: exactly one INSERT...ON CONFLICT statement,
    a 5000ms busy_timeout on the connection, and no SELECT before the write."""
    executed: list[str] = []
    real_connect = db.connect

    class SpyConn:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args):
            executed.append(" ".join(str(sql).split()))
            return self._conn.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    @contextmanager
    def spy_connect(db_path=None):
        with real_connect(db_path) as conn:
            yield SpyConn(conn)

    monkeypatch.setattr(card_store.db, "connect", spy_connect)
    card_store.save_card(a_card(), read_cache_key="k1")
    assert executed[0] == "PRAGMA busy_timeout = 5000"
    assert executed[1].startswith("INSERT INTO report_cards")
    assert "ON CONFLICT(activity_id) DO UPDATE" in executed[1]
    assert len(executed) == 2
    assert not any(s.upper().startswith("SELECT") for s in executed)


# --- list_cards --------------------------------------------------------------


@pytest.fixture
def three_cards(cdb):
    for activity_id, day, dist in (
            (1, "2026-07-10", 10000), (2, "2026-07-15", 18000),
            (3, "2026-07-20", 10000)):
        card_store.save_card(
            a_card(activity={
                "activity_id": activity_id, "date": day,
                "distance_meters": dist}),
            read_cache_key=f"k{activity_id}")
    return cdb


def test_list_cards_orders_newest_run_first(three_cards):
    rows = card_store.list_cards()
    assert [r["activity_id"] for r in rows] == [3, 2, 1]
    assert [r["activity_date"] for r in rows] == [
        "2026-07-20", "2026-07-15", "2026-07-10"]


def test_list_cards_date_and_intent_filters_pin_rows(three_cards):
    assert [r["activity_id"] for r in card_store.list_cards(
        start_date="2026-07-12")] == [3, 2]
    assert [r["activity_id"] for r in card_store.list_cards(
        end_date="2026-07-12")] == [1]
    # 18km against a 10km median is a long run; the other two are not.
    assert [r["activity_id"] for r in card_store.list_cards(
        intent_class="long")] == [2]
    assert card_store.list_cards(intent_class="quality") == []


def test_list_cards_respects_limit(three_cards):
    assert [r["activity_id"] for r in card_store.list_cards(limit=1)] == [3]


# --- load_read ---------------------------------------------------------------


def test_load_read_returns_the_stored_key_and_parsed_read(cdb):
    card_store.save_card(a_card(), read_cache_key="k1")
    assert card_store.load_read(1) == ("k1", READ)
    assert card_store.load_read(999) is None


def test_load_read_treats_a_corrupt_row_as_an_unusable_read(cdb):
    card_store.save_card(a_card(), read_cache_key="k1")
    with db.connect() as conn:
        conn.execute(
            "UPDATE report_cards SET card_json = 'nope' WHERE activity_id = 1")
    key, read = card_store.load_read(1)
    assert key == "k1"
    assert card_store.read_is_complete(read) is False


def test_load_read_is_fail_silent_on_db_errors(cdb, monkeypatch):
    @contextmanager
    def boom(db_path=None):
        raise RuntimeError("db unavailable")
        yield  # pragma: no cover

    monkeypatch.setattr(card_store.db, "connect", boom)
    assert card_store.load_read(1) is None  # a miss, never a failed render


# --- the 0.40.0 read-section rename migration --------------------------------

#: A read as rows written before 0.40.0 hold it: the 4th section is `load`.
LEGACY_READ = {
    "distance": "covered the ground.", "pace": "too quick.",
    "hr": "stayed low.", "load": "banked what it should.",
}


def _store_legacy_read(activity_id=1):
    """Put a pre-0.40.0 read into the store, the only way a real one gets
    there: save a normal card, then rewrite the section name in card_json.
    Everything else about the row stays a genuine save_card write."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT card_json FROM report_cards WHERE activity_id = ?",
            (activity_id,)).fetchone()
        card = json.loads(row["card_json"])
        card["coach_read"] = dict(LEGACY_READ)
        conn.execute(
            "UPDATE report_cards SET card_json = ? WHERE activity_id = ?",
            (json.dumps(card, default=str), activity_id))


def test_rename_read_sections_moves_load_into_the_stimulus_slot():
    out = card_store.rename_read_sections(LEGACY_READ)
    assert out == {
        "distance": "covered the ground.", "pace": "too quick.",
        "hr": "stayed low.", "stimulus": "banked what it should.",
    }
    # In place, not appended: a migrated row's JSON differs only where the
    # rename does, so the stored bytes stay comparable to a fresh write.
    assert list(out) == ["distance", "pace", "hr", "stimulus"]
    # The prose is carried across untouched — this is a rename, not a regrade.
    assert out["stimulus"] == LEGACY_READ["load"]


def test_rename_read_sections_returns_none_when_there_is_nothing_to_do():
    assert card_store.rename_read_sections(READ) is None      # already current
    assert card_store.rename_read_sections({}) is None
    assert card_store.rename_read_sections(None) is None
    assert card_store.rename_read_sections("not a dict") is None


def test_rename_read_sections_leaves_a_row_holding_both_keys_alone():
    both = {**READ, "load": "the old paragraph"}
    # Guessing which of two populated sections wins would silently destroy a
    # paragraph; the row is left for a human instead.
    assert card_store.rename_read_sections(both) is None


def test_migration_restores_reuse_for_a_legacy_row(cdb):
    card_store.save_card(a_card(), read_cache_key="k1")
    _store_legacy_read()
    assert card_store.read_is_complete(card_store.load_read(1)[1]) is False

    assert card_store.migrate_read_section_names() == 1

    key, read = card_store.load_read(1)
    assert key == "k1"
    assert read == READ                       # the same four paragraphs
    # The whole point: the row can be reused by the render fast path again.
    assert card_store.read_is_complete(read) is True


def test_migration_is_idempotent(cdb):
    card_store.save_card(a_card(), read_cache_key="k1")
    _store_legacy_read()

    assert card_store.migrate_read_section_names() == 1
    first = raw_row()
    assert card_store.migrate_read_section_names() == 0
    assert card_store.migrate_read_section_names() == 0
    # Byte-identical: a second pass writes nothing at all, so re-running this
    # on every init_schema can never churn the store.
    assert raw_row() == first


def test_migration_does_not_disturb_the_snapshot(cdb, clock):
    card_store.save_card(a_card(), read_cache_key="k1")
    _store_legacy_read()
    before = raw_row()
    clock(minute=30)  # a later clock must NOT leak into the migrated row

    card_store.migrate_read_section_names()

    after = raw_row()
    # A stored card is a historical record: the migration repairs a field NAME
    # the schema renamed underneath it and touches nothing else.
    assert after["graded_at"] == before["graded_at"]
    assert after["read_cache_key"] == before["read_cache_key"]
    for column in ("activity_date", "intent", "intent_class", "intent_source",
                   "overall_stars", "mean_stars", "capped_by_metric",
                   "distance_stars", "pace_stars", "hr_stars",
                   "continuity_stars", "overall_grade", "gpa", "capped_by",
                   "distance_grade", "pace_grade", "hr_grade",
                   "continuity_grade", "load_grade"):
        assert after[column] == before[column], column
    # Inside card_json, only coach_read moves.
    b, a = json.loads(before["card_json"]), json.loads(after["card_json"])
    # Pop OUTSIDE the assert: the `assert a == b` below is only meaningful
    # because coach_read has been removed from both, so that removal must not
    # live in an expression `python -O` would strip.
    before_read, after_read = b.pop("coach_read"), a.pop("coach_read")
    assert before_read == LEGACY_READ
    assert after_read == READ
    assert a == b
    # The stripped keys stay stripped — a decode-and-rewrite that re-defaulted
    # them would silently fatten every stored row.
    assert "hr_trace" not in a


def test_migration_skips_corrupt_rows_without_touching_the_good_ones(cdb):
    card_store.save_card(a_card(), read_cache_key="k1")
    _store_legacy_read(1)
    card_store.save_card(a_card({"activity_id": 2}), read_cache_key="k2")
    _store_legacy_read(2)
    with db.connect() as conn:
        conn.execute(
            "UPDATE report_cards SET card_json = 'not json' "
            "WHERE activity_id = 2")

    assert card_store.migrate_read_section_names() == 1

    assert card_store.read_is_complete(card_store.load_read(1)[1]) is True
    assert raw_row(2)["card_json"] == "not json"   # left exactly as found


def test_migration_never_raises_on_a_db_failure(cdb, monkeypatch):
    @contextmanager
    def boom(db_path=None):
        raise RuntimeError("db unavailable")
        yield  # pragma: no cover

    monkeypatch.setattr(card_store.db, "connect", boom)
    assert card_store.migrate_read_section_names() == 0


def test_init_schema_runs_the_migration(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    card_store.save_card(a_card(), read_cache_key="k1", db_path=p)
    _store_legacy_read()

    db.init_schema(p)   # opening the DB again is what repairs the row

    assert card_store.read_is_complete(card_store.load_read(1, db_path=p)[1])
