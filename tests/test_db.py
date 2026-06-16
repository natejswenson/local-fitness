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
