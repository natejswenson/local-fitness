"""Tests for db.py — schema init, settings, and run-state helpers."""
from __future__ import annotations

from datetime import date

import pytest

from local_fitness import db


@pytest.fixture
def dbp(tmp_path):
    p = tmp_path / "fitness.db"
    db.init_schema(p)
    return p


def test_init_schema_idempotent(dbp):
    # Calling again must not raise (the guarded ALTER for activities.source
    # would otherwise blow up with "duplicate column" on a second init).
    db.init_schema(dbp)
    with db.connect(dbp) as conn:
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "observations" in tables
        act_cols = {r["name"] for r in conn.execute("PRAGMA table_info(activities)")}
    assert "source" in act_cols


def test_init_schema_adds_archived_and_fts(dbp):
    with db.connect(dbp) as conn:
        jcols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(coach_journal)")}
        master = {
            (r["type"], r["name"])
            for r in conn.execute(
                "SELECT type, name FROM sqlite_master "
                "WHERE name LIKE 'coach_journal_fts%'")
        }
    assert "archived" in jcols
    assert ("table", "coach_journal_fts") in master
    assert {("trigger", "coach_journal_fts_ai"),
            ("trigger", "coach_journal_fts_ad"),
            ("trigger", "coach_journal_fts_au")} <= master


def test_init_schema_fts_idempotent_and_in_sync(dbp):
    from local_fitness.agent import journal

    journal.save_entry("a memory", source="chat", db_path=dbp)
    db.init_schema(dbp)  # second init: no raise, no index churn
    with db.connect(dbp) as conn:
        n_rows = conn.execute("SELECT COUNT(*) FROM coach_journal").fetchone()[0]
        # _docsize is the real indexed-document count; COUNT(*) on an
        # external-content vtable reads through to the content table.
        n_fts = conn.execute(
            "SELECT COUNT(*) FROM coach_journal_fts_docsize").fetchone()[0]
    assert (n_rows, n_fts) == (1, 1)


def test_backfill_indexes_preexisting_rows(dbp):
    # Simulate a pre-0.33.0 DB: rows written with no triggers and an empty
    # (or missing) FTS index — the count-mismatch rebuild must pick them up.
    with db.connect(dbp) as conn:
        for trig in ("coach_journal_fts_ai", "coach_journal_fts_ad",
                     "coach_journal_fts_au"):
            conn.execute(f"DROP TRIGGER {trig}")
        conn.execute("DROP TABLE coach_journal_fts")
        for i, text in enumerate(
                ["plantar fasciitis flared", "skipped the tempo again"], 1):
            conn.execute(
                "INSERT INTO coach_journal "
                "(created_at, entry_date, source, seq, text) "
                "VALUES ('2026-01-01T00:00:00', '2026-01-01', 'chat', ?, ?)",
                (i, text))
    db.init_schema(dbp)
    from local_fitness.agent import journal

    matches, mode = journal.search_entries("plantar", db_path=dbp)
    assert mode == "fts"
    assert [m["text"] for m in matches] == ["plantar fasciitis flared"]
    with db.connect(dbp) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM coach_journal_fts_docsize").fetchone()[0] == 2


def test_settings_roundtrip(dbp):
    assert db.get_setting("user_name", db_path=dbp) is None
    assert db.get_setting("user_name", default="nobody", db_path=dbp) == "nobody"
    db.set_setting("user_name", "Dana", db_path=dbp)
    assert db.get_setting("user_name", db_path=dbp) == "Dana"
    # ON CONFLICT update path
    db.set_setting("user_name", "Sam", db_path=dbp)
    assert db.get_setting("user_name", db_path=dbp) == "Sam"
    assert db.all_settings(db_path=dbp) == {"user_name": "Sam"}


def test_last_known_daily_date(dbp):
    assert db.last_known_daily_date(db_path=dbp) is None
    with db.connect(dbp) as conn:
        conn.execute("INSERT INTO daily_metrics (date, rhr) VALUES ('2026-06-01', 50)")
        conn.execute("INSERT INTO daily_metrics (date, rhr) VALUES ('2026-06-03', 51)")
    assert db.last_known_daily_date(db_path=dbp) == "2026-06-03"


def test_missing_daily_dates(dbp):
    with db.connect(dbp) as conn:
        conn.execute("INSERT INTO daily_metrics (date, rhr) VALUES ('2026-06-02', 50)")
    missing = db.missing_daily_dates(date(2026, 6, 1), date(2026, 6, 3), db_path=dbp)
    assert missing == [date(2026, 6, 1), date(2026, 6, 3)]


def test_mark_orphaned_runs(dbp):
    with db.connect(dbp) as conn:
        conn.execute(
            "INSERT INTO ingest_runs (started_at, status) VALUES ('2026-06-01T00:00:00', 'in_progress')"
        )
    assert db.mark_orphaned_runs(db_path=dbp) == 1
    # second call: nothing left in_progress
    assert db.mark_orphaned_runs(db_path=dbp) == 0
    with db.connect(dbp) as conn:
        row = conn.execute("SELECT status FROM ingest_runs").fetchone()
    assert row["status"] == "orphaned"


def test_connect_rolls_back_on_error(dbp):
    with pytest.raises(ValueError):
        with db.connect(dbp) as conn:
            conn.execute("INSERT INTO settings (key, value) VALUES ('k', 'v')")
            raise ValueError("boom")
    # the insert must have been rolled back
    assert db.get_setting("k", db_path=dbp) is None


def test_get_db_path_creates_parent(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", target)
    assert db.get_db_path() == target
    assert target.parent.exists()


def test_default_db_path_honors_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_FITNESS_DATA_DIR", str(tmp_path))
    assert db._default_db_path() == tmp_path / "fitness.db"
    monkeypatch.delenv("LOCAL_FITNESS_DATA_DIR")
    assert db._default_db_path().name == "fitness.db"


def test_training_plan_tables_exist(dbp):
    with db.connect(dbp) as conn:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"training_plans", "plan_workouts"} <= tables


def test_one_active_plan_unique_index(dbp):
    import sqlite3

    # First active plan is fine.
    with db.connect(dbp) as conn:
        conn.execute(
            "INSERT INTO training_plans (status, goal_type, race_date, created_at) "
            "VALUES ('active', '10k', '2026-09-14', '2026-06-15T00:00:00')"
        )
    # A second active plan must violate the partial unique index.
    with pytest.raises(sqlite3.IntegrityError):
        with db.connect(dbp) as conn:
            conn.execute(
                "INSERT INTO training_plans (status, goal_type, race_date, created_at) "
                "VALUES ('active', '5k', '2026-10-01', '2026-06-15T00:00:00')"
            )
    # Archived/draft rows are unconstrained — many allowed.
    with db.connect(dbp) as conn:
        conn.execute(
            "INSERT INTO training_plans (status, goal_type, race_date, created_at) "
            "VALUES ('draft', '5k', '2026-10-01', '2026-06-15T00:00:00')"
        )
        conn.execute(
            "INSERT INTO training_plans (status, goal_type, race_date, created_at) "
            "VALUES ('archived', 'half', '2026-11-01', '2026-06-15T00:00:00')"
        )


def test_plan_workouts_columns(dbp):
    with db.connect(dbp) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(plan_workouts)")}
    assert {
        "workout_id",
        "plan_id",
        "date",
        "seq",
        "week_index",
        "type",
        "target_distance_m",
        "target_pace_sec_per_km",
        "target_duration_sec",
        "description",
    } <= cols
