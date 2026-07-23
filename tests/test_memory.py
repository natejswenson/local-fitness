"""memory.render_memory_for_prompt — the one resolver every surface calls."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from local_fitness import db
from local_fitness.agent import journal, ledger, memory


@pytest.fixture
def mdb(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    return p


def _seed_steps(p, hits: int, goal: int = 8000):
    today = date.today()
    with db.connect(p) as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('daily_step_goal', ?)",
            (str(goal),))
        for back in range(1, hits + 1):
            conn.execute(
                "INSERT INTO daily_metrics (date, steps) VALUES (?, ?)",
                ((today - timedelta(days=back)).isoformat(), goal + 100))


def test_empty_db_renders_empty_string(mdb):
    assert memory.render_memory_for_prompt(user_name="Alex") == ""


def test_ledger_and_journal_both_land_in_the_block(mdb):
    _seed_steps(mdb, 4)
    journal.save_entry("blamed the heat again", source="chat",
                       entry_date="2026-07-18")
    text = memory.render_memory_for_prompt(user_name="Alex")
    assert "goal hit 4 days running" in text
    assert "Your journal (what you wrote down):" in text
    assert "blamed the heat again" in text


def test_kill_switch_empties_the_resolver(mdb, monkeypatch):
    _seed_steps(mdb, 4)
    journal.save_entry("a memory", source="chat")
    monkeypatch.setenv("LOCAL_FITNESS_COACH_MEMORY", "0")
    assert memory.render_memory_for_prompt(user_name="Alex") == ""
    assert not memory.memory_enabled()
    monkeypatch.setenv("LOCAL_FITNESS_COACH_MEMORY", "off")
    assert not memory.memory_enabled()
    monkeypatch.setenv("LOCAL_FITNESS_COACH_MEMORY", "1")
    assert memory.memory_enabled()


def test_exclude_source_key_drops_only_that_events_entries(mdb):
    journal.save_entry("about THIS card", source="report_card",
                       source_key="42", entry_date="2026-07-22")
    journal.save_entry("about another card", source="report_card",
                       source_key="43", entry_date="2026-07-21")
    text = memory.render_memory_for_prompt(
        exclude_source_key=("report_card", "42"), user_name="Alex")
    assert "about another card" in text
    assert "about THIS card" not in text


def test_compact_variant_is_hard_capped_and_drops_patterns(mdb):
    _seed_steps(mdb, 4)
    today = date.today()
    with db.connect(mdb) as conn:
        for back in range(1, 6):
            conn.execute(
                "INSERT INTO observations (observed_on, created_at, obs_type, "
                "value_num) VALUES (?, ?, 'soreness', 8)",
                ((today - timedelta(days=back)).isoformat(), "t"))
    for i in range(10):
        journal.save_entry("m" * journal.ENTRY_MAX_CHARS, source="chat",
                           entry_date=f"2026-07-{i + 10:02d}")
    compact = memory.render_memory_for_prompt(compact=True, user_name="Alex")
    full = memory.render_memory_for_prompt(user_name="Alex")
    assert len(compact) <= memory.COMPACT_MAX_CHARS
    assert "Soreness" in full
    assert "Soreness" not in compact  # patterns are full-surface only
    # Whole-line truncation: a compact block never ends mid-entry.
    assert all(len(line) <= journal.ENTRY_MAX_CHARS + 20
               for line in compact.splitlines())


def test_journal_entry_counts_full_vs_compact(mdb):
    for i in range(12):
        journal.save_entry(f"memory {i}", source="chat",
                           entry_date=f"2026-06-{i + 1:02d}")
    full = memory.render_memory_for_prompt(user_name="Alex")
    compact = memory.render_memory_for_prompt(compact=True, user_name="Alex")
    assert sum(1 for line in full.splitlines() if "memory " in line) == memory.FULL_ENTRIES
    assert sum(1 for line in compact.splitlines() if "memory " in line) == memory.COMPACT_ENTRIES


def test_resolver_never_raises(mdb, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(ledger, "compute_relationship_ledger", boom)
    assert memory.render_memory_for_prompt(user_name="Alex") == ""
