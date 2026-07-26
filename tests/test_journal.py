"""Coach journal CRUD — cap/archive, search, validation, idempotency, rendering."""
from __future__ import annotations

import sqlite3

import pytest

from local_fitness import db
from local_fitness.agent import journal


@pytest.fixture
def jdb(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    return p


def test_save_and_list_round_trip_newest_first(jdb):
    journal.save_entry("older memory", source="chat", entry_date="2026-07-20")
    saved = journal.save_entry(
        "newer memory", source="brief", source_key="2026-07-22",
        entry_date="2026-07-22")
    assert saved["entry_id"] is not None
    entries = journal.list_entries(db_path=jdb)
    assert [e["text"] for e in entries] == ["newer memory", "older memory"]
    assert entries[0]["source"] == "brief"
    assert entries[0]["source_key"] == "2026-07-22"


def test_save_validates_text_and_source(jdb):
    with pytest.raises(ValueError, match="required"):
        journal.save_entry("   ", source="chat")
    with pytest.raises(ValueError, match="too long"):
        journal.save_entry("x" * (journal.ENTRY_MAX_CHARS + 1), source="chat")
    with pytest.raises(ValueError, match="unknown journal source"):
        journal.save_entry("fine", source="dream")
    # Exactly at the cap is allowed.
    journal.save_entry("x" * journal.ENTRY_MAX_CHARS, source="chat")


def test_61st_write_archives_not_deletes(jdb):
    for i in range(journal.JOURNAL_CAP):
        journal.save_entry(
            f"memory {i}", source="chat", entry_date=f"2026-05-{(i % 28) + 1:02d}")
    journal.save_entry("the 61st", source="chat", entry_date="2026-07-22")
    # The hot set is capped: the oldest by (entry_date, entry_id) is gone
    # from the default view...
    entries = journal.list_entries(limit=200, db_path=jdb)
    assert len(entries) == journal.JOURNAL_CAP
    assert entries[0]["text"] == "the 61st"
    assert "memory 0" not in {e["text"] for e in entries}
    # ...but never deleted: it survives in the archive, flagged.
    everything = journal.list_entries(
        limit=200, db_path=jdb, include_archived=True)
    assert len(everything) == journal.JOURNAL_CAP + 1
    archived = [e for e in everything if e["archived"]]
    assert [e["text"] for e in archived] == ["memory 0"]
    with db.connect(jdb) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM coach_journal").fetchone()[0] == 61


def test_search_finds_archived_entry_by_keyword(jdb):
    journal.save_entry(
        "left achilles flared on the hill repeats", source="chat",
        entry_date="2026-01-05")
    for i in range(journal.JOURNAL_CAP):
        journal.save_entry(
            f"filler {i}", source="chat", entry_date="2026-07-01")
    # The achilles line is now archived (oldest beyond the cap)...
    hot_texts = {e["text"] for e in journal.list_entries(limit=200, db_path=jdb)}
    assert "left achilles flared on the hill repeats" not in hot_texts
    # ...yet search still reaches it, best-match first, flagged as archived.
    matches, mode = journal.search_entries("achilles", db_path=jdb)
    assert mode == "fts"
    assert [m["text"] for m in matches] == [
        "left achilles flared on the hill repeats"]
    assert matches[0]["archived"] == 1


def test_search_stems_words(jdb):
    journal.save_entry("kept running through the rain", source="chat")
    matches, mode = journal.search_entries("runs", db_path=jdb)
    assert mode == "fts"
    assert [m["text"] for m in matches] == ["kept running through the rain"]


def test_search_fts_syntax_is_inert(jdb):
    journal.save_entry("knee felt fine on the tempo", source="chat")
    hostile = ['knee" OR "x', "NEAR(knee, 2)", "text: knee", "(knee", "knee*"]
    for q in hostile:
        matches, _mode = journal.search_entries(q, db_path=jdb)
        assert isinstance(matches, list)  # no OperationalError escapes
    # Operators quoted as data: the NEAR() query must NOT match the knee entry
    # as syntax would — 'NEAR(knee,' and '2)' are literal tokens, absent here.
    matches, _ = journal.search_entries("NEAR(knee, 2)", db_path=jdb)
    assert matches == []
    # A plain-token query still hits.
    matches, _ = journal.search_entries('knee" OR "x tempo', db_path=jdb)
    assert matches == []  # "x is a literal token that matches nothing
    matches, _ = journal.search_entries("knee tempo", db_path=jdb)
    assert [m["text"] for m in matches] == ["knee felt fine on the tempo"]
    for empty in ("()", "   ", '"'):
        with pytest.raises(ValueError, match="no searchable words"):
            journal.search_entries(empty, db_path=jdb)


def test_delete_entry_removes_from_search(jdb):
    saved = journal.save_entry("calf strain after the intervals", source="chat")
    matches, _ = journal.search_entries("calf strain", db_path=jdb)
    assert len(matches) == 1
    assert journal.delete_entry(saved["entry_id"], db_path=jdb)
    matches, _ = journal.search_entries("calf strain", db_path=jdb)
    assert matches == []


def test_search_like_fallback_without_fts(jdb):
    journal.save_entry("knee felt fine on the tempo", source="chat")
    journal.save_entry("totally unrelated line", source="chat")
    with db.connect(jdb) as conn:
        for trig in ("coach_journal_fts_ai", "coach_journal_fts_ad",
                     "coach_journal_fts_au"):
            conn.execute(f"DROP TRIGGER {trig}")
        conn.execute("DROP TABLE coach_journal_fts")
    matches, mode = journal.search_entries("knee tempo", db_path=jdb)
    assert mode == "like"
    assert [m["text"] for m in matches] == ["knee felt fine on the tempo"]
    # LIKE wildcards in the query are escaped — '%' must not match everything.
    matches, mode = journal.search_entries("100%", db_path=jdb)
    assert mode == "like"
    assert matches == []


def test_duplicate_event_seq_raises_integrity_error(jdb):
    journal.save_entry("first", source="report_card", source_key="123", seq=1)
    with pytest.raises(sqlite3.IntegrityError):
        journal.save_entry("dupe", source="report_card", source_key="123", seq=1)
    # A different seq for the same event is fine (reflect writes up to 2).
    journal.save_entry("second", source="report_card", source_key="123", seq=2)
    # Chat entries carry no source_key, so they can never collide.
    journal.save_entry("chat a", source="chat")
    journal.save_entry("chat b", source="chat")


def test_has_event(jdb):
    assert not journal.has_event("brief", "2026-07-23", db_path=jdb)
    journal.save_entry("m", source="brief", source_key="2026-07-23")
    assert journal.has_event("brief", "2026-07-23", db_path=jdb)


def test_list_entries_days_window_and_limit(jdb):
    journal.save_entry("recent", source="chat", entry_date="2026-07-22")
    with db.connect(jdb) as conn:
        conn.execute(
            "INSERT INTO coach_journal (created_at, entry_date, source, seq, text) "
            "VALUES ('2020-01-01T00:00:00', '2020-01-01', 'chat', 1, 'ancient')")
    assert len(journal.list_entries(db_path=jdb)) == 2
    windowed = journal.list_entries(days=365, db_path=jdb)
    assert [e["text"] for e in windowed] == ["recent"]
    assert len(journal.list_entries(limit=1, db_path=jdb)) == 1


def test_delete_entry(jdb):
    saved = journal.save_entry("to delete", source="chat")
    assert journal.delete_entry(saved["entry_id"], db_path=jdb)
    assert not journal.delete_entry(saved["entry_id"], db_path=jdb)
    assert journal.list_entries(db_path=jdb) == []


def test_render_journal_block_formats_dates_and_skips_blanks():
    entries = [
        {"entry_date": "2026-07-18", "text": "blamed the heat again"},
        {"entry_date": "not-a-date", "text": "odd date survives verbatim"},
        {"entry_date": "2026-07-19", "text": "   "},
    ]
    block = journal.render_journal_block(entries, "Alex")
    assert block == (
        "- Jul 18: blamed the heat again\n"
        "- not-a-date: odd date survives verbatim"
    )
    assert journal.render_journal_block([], "Alex") == ""
