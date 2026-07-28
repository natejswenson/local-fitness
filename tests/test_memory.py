"""memory.render_memory_for_prompt — the one resolver every surface calls."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from local_fitness import db
from local_fitness.agent import card_store, journal, ledger, memory


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


def test_archived_entries_never_reach_injection(mdb):
    # 61 writes: the oldest archives. Injection (and thus the prompt-hash
    # caches keyed on it) must only ever see the hot set.
    journal.save_entry("the one that gets archived", source="chat",
                       entry_date="2026-01-01")
    for i in range(journal.JOURNAL_CAP):
        journal.save_entry(f"hot memory {i}", source="chat",
                           entry_date="2026-07-01")
    text = memory.render_memory_for_prompt(user_name="Alex")
    assert "the one that gets archived" not in text
    assert "hot memory" in text


def _minimal_card(activity_id: int, activity_date: str, gpa: float,
                   grade: str) -> dict:
    return {
        "activity": {"activity_id": activity_id, "date": activity_date},
        "overall": {"grade": grade, "gpa": gpa, "capped_by": None},
        "metrics": {},
        "intent": "easy", "intent_class": "easy", "intent_source": "plan",
        "coach_read": None,
    }


def test_card_aggregate_lands_in_memory_text(mdb):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    two_back = (date.today() - timedelta(days=2)).isoformat()
    card_store.save_card(
        _minimal_card(1, yesterday, 3.5, "A"), read_cache_key="k1", db_path=mdb)
    card_store.save_card(
        _minimal_card(2, two_back, 3.0, "B"), read_cache_key="k2", db_path=mdb)
    text = memory.render_memory_for_prompt(user_name="Alex")
    assert "Report cards:" in text


def test_saving_a_card_for_today_leaves_memory_text_byte_identical(mdb):
    # Seed enough back-dated cards to clear the render floor first.
    for i in range(1, 4):
        d = (date.today() - timedelta(days=i)).isoformat()
        card_store.save_card(
            _minimal_card(i, d, 3.0, "B"), read_cache_key=f"k{i}", db_path=mdb)
    before = memory.render_memory_for_prompt(user_name="Alex")
    today = date.today().isoformat()
    card_store.save_card(
        _minimal_card(99, today, 4.0, "A"), read_cache_key="k99", db_path=mdb)
    after = memory.render_memory_for_prompt(user_name="Alex")
    assert before == after


def test_prior_day_card_flips_memory_once_then_converges(mdb):
    for i in range(1, 4):
        d = (date.today() - timedelta(days=i)).isoformat()
        card_store.save_card(
            _minimal_card(i, d, 3.0, "B"), read_cache_key=f"k{i}", db_path=mdb)
    m1 = memory.render_memory_for_prompt(user_name="Alex")

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    card_store.save_card(
        _minimal_card(50, yesterday, 4.0, "A"),
        read_cache_key="first-key", db_path=mdb)
    m2 = memory.render_memory_for_prompt(user_name="Alex")
    assert m2 != m1

    # Re-render of the same card (a different read_cache_key, same grades)
    # must not move the aggregate any further.
    card_store.save_card(
        _minimal_card(50, yesterday, 4.0, "A"),
        read_cache_key="second-key", db_path=mdb)
    m3 = memory.render_memory_for_prompt(user_name="Alex")
    assert m3 == m2


def _fake_ledger_with_many_notables(n: int) -> dict:
    """A ledger whose rendered block alone exceeds COMPACT_MAX_CHARS, so the
    compact capping logic actually has to make a choice."""
    notables = [
        {"date": f"2026-07-{(i % 28) + 1:02d}", "type": "interval",
         "kind": "quality_done"} for i in range(n)
    ]
    return {"as_of": "2026-07-23", "plan": {}, "steps": {}, "patterns": [],
            "notables": notables, "cards": None}


def test_compact_never_ends_with_bare_header(mdb, monkeypatch):
    """0.38.2 bug: a long ledger could pop every journal line via one
    whole-text cap, leaving the 'Your journal' header standing with nothing
    under it — the V2 brief (the highest-frequency surface) shipped an empty
    banner that invited invention instead of no callback at all."""
    monkeypatch.setattr(
        ledger, "compute_relationship_ledger",
        lambda **kw: _fake_ledger_with_many_notables(14))
    journal.save_entry("kept the promise this week", source="chat",
                       entry_date="2026-07-22")
    text = memory.render_memory_for_prompt(compact=True, user_name="Alex")
    assert len(text) <= memory.COMPACT_MAX_CHARS
    if "Your journal" in text:
        after_header = text.split("Your journal (what you wrote down):\n", 1)[1]
        assert after_header.strip() != ""


def test_compact_keeps_a_journal_line_when_present(mdb, monkeypatch):
    """Same heavy-ledger scenario: the journal's newest line must survive
    the cap even though the ledger alone would exceed the whole budget."""
    monkeypatch.setattr(
        ledger, "compute_relationship_ledger",
        lambda **kw: _fake_ledger_with_many_notables(14))
    journal.save_entry("kept the promise this week", source="chat",
                       entry_date="2026-07-22")
    text = memory.render_memory_for_prompt(compact=True, user_name="Alex")
    assert "kept the promise this week" in text
    assert len(text) <= memory.COMPACT_MAX_CHARS


def test_compact_with_no_journal_uses_the_full_budget_on_ledger(mdb, monkeypatch):
    """No journal entries: the whole COMPACT_MAX_CHARS budget goes to the
    ledger, same as before this fix — no journal reserve is carved out for
    nothing. Uses a bigger ledger than the other tests so the difference
    between a 400ish-char and a 600-char cap is actually visible."""
    monkeypatch.setattr(
        ledger, "compute_relationship_ledger",
        lambda **kw: _fake_ledger_with_many_notables(20))
    text = memory.render_memory_for_prompt(compact=True, user_name="Alex")
    assert "Your journal" not in text
    assert len(text) <= memory.COMPACT_MAX_CHARS
    assert len(text) > memory.COMPACT_MAX_CHARS - memory._JOURNAL_RESERVE_CHARS


def test_resolver_never_raises(mdb, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(ledger, "compute_relationship_ledger", boom)
    assert memory.render_memory_for_prompt(user_name="Alex") == ""
