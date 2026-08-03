"""Tests for the report-card cache-warming script.

Same precedent as ``test_calibrate_report_card.py``: a script that decides
whether to spend money needs its own suite before its output is trusted.

The load-bearing properties are the two that make a survey safe to run
casually — it costs NOTHING (no SDK call escapes) and writes NOTHING (no row is
touched) — plus the pre-call gate, because a ceiling that only warns is not a
ceiling. Nate's standing rule for autonomous LLM spend is "quote the estimate
up front AND enforce a hard cap in code".

No network and no real data: the SDK is already blocked by conftest's autouse
fixture, and these build their own temp DB.
"""
from __future__ import annotations

import pytest
import warm_report_cards as warm

from local_fitness import db
from local_fitness.agent import card_store, workout_coach


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    db.init_schema(p)
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    return p


def _store_card(activity_id: int, activity_date: str, *, read_cache_key: str):
    """A minimal stored card row — enough for list_cards/load_read."""
    card = {
        "activity_id": activity_id,
        "activity": {"activity_id": activity_id, "date": activity_date},
        "activity_date": activity_date,
        "intent": "easy run", "intent_class": "easy", "intent_source": "plan",
        "overall": {"grade": "A", "gpa": 4.0, "capped_by": None},
        "metrics": {
            "distance": {"grade": "A"}, "pace": {"grade": "A"},
            "hr": {"grade": "A"}, "continuity": {"grade": "A"},
        },
        "coach_read": {
            "distance": "d", "pace": "p", "hr": "h", "stimulus": "s",
        },
    }
    card_store.save_card(card, read_cache_key=read_cache_key)
    return card


def test_no_database_is_a_clean_skip(tmp_path, monkeypatch, capsys):
    """A fresh clone has no DB. Not an error, and nothing to warm."""
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "absent.db")
    assert warm.main([]) == 0
    assert "nothing to warm" in capsys.readouterr().out.lower()


def test_no_stored_cards_is_a_clean_skip(db_path, capsys):
    assert warm.main([]) == 0
    assert "no stored cards" in capsys.readouterr().out.lower()


def test_a_survey_never_calls_the_sdk(db_path, monkeypatch, capsys):
    """THE safety property. The survey drives the real handler; if a single
    generation escaped, running this script to 'just look' would spend."""
    _store_card(-101, "2026-07-20", read_cache_key="stale-key")

    calls = []

    async def _boom(*a, **kw):
        calls.append(1)
        raise AssertionError("the survey called generate_read_cached")

    monkeypatch.setattr(workout_coach, "generate_read_cached", _boom)
    warm.main([])          # no --yes
    assert calls == [], "a survey escaped an SDK call"


def test_a_survey_never_writes_a_row(db_path, capsys):
    """The survey deliberately trips the handler's fail-silent fallback, whose
    read_key is NULL. save_card's contract says that can't overwrite a
    real-read row — but the survey stubs the write out anyway, so this is inert
    twice over. Pinned because a regression here would silently replace real
    coach reads with deterministic templates across the whole corpus."""
    _store_card(-102, "2026-07-21", read_cache_key="stale-key")
    before = card_store.load_read(-102)
    assert before is not None and before[0] == "stale-key"

    warm.main([])

    after = card_store.load_read(-102)
    assert after == before, "the survey mutated a stored card"


def test_the_cost_ceiling_refuses_rather_than_warns(db_path, capsys):
    """A cap that prints a warning and proceeds is not a cap. Exit 2, and the
    refusal must happen BEFORE --yes is honoured."""
    for i in range(4):
        _store_card(-200 - i, f"2026-07-1{i}", read_cache_key="stale-key")

    rc = warm.main(["--max-calls", "2", "--yes"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "REFUSING" in out
    assert "--max-calls=2" in out


def test_the_estimate_is_quoted_before_anything_is_spent(db_path, capsys):
    _store_card(-301, "2026-07-22", read_cache_key="stale-key")
    warm.main([])
    out = capsys.readouterr().out
    assert "Estimated cost:" in out
    assert "Claude call" in out
    # And it must say plainly that nothing happened.
    assert "nothing spent" in out.lower()


def test_a_warm_card_is_not_listed_as_stale(tmp_path, monkeypatch, capsys):
    """The survey's whole value is telling warm from stale. A version that
    reported every card stale would pass every safety test above while being
    useless — and would quote a cost many times too high.

    Uses a fabricated scenario DB so there is a genuinely renderable activity;
    the minimal row fixtures above cannot reach the key-match fast path."""
    from report_cards import build_scenario_db

    p = build_scenario_db("obedient_easy_clean", tmp_path / "fitness.db")
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)

    async def _boom(*a, **kw):
        raise AssertionError("regenerated a card whose key already matched")

    # First render with a stubbed generator writes a card + key legitimately.
    async def _read(*a, **kw):
        return {"distance": "d", "pace": "p", "hr": "h", "stimulus": "s"}

    monkeypatch.setattr(workout_coach, "generate_read_cached", _read)
    import asyncio as _aio

    from local_fitness.agent import tools as _tools
    _aio.run(_tools.workout_report_card.handler(
        {"activity_id": 1, "format": "table"}))
    assert card_store.load_read(1) is not None, "setup did not store a card"

    # Now the card is warm: a survey must reach no generation at all.
    monkeypatch.setattr(workout_coach, "generate_read_cached", _boom)
    rc = warm.main(["--days", "3650"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "All stored cards are warm" in out
    assert "Estimated cost" not in out, "a warm card was quoted as costing money"


def test_default_max_calls_is_a_real_ceiling():
    """Documented as belt-and-braces against a wrong --days on a big corpus."""
    assert isinstance(warm.DEFAULT_MAX_CALLS, int)
    assert 0 < warm.DEFAULT_MAX_CALLS <= 100


def test_seconds_per_call_matches_the_measured_latency():
    """Quoted wall clock has to track workout_coach's measured median (10.0s,
    Sonnet + effort=low + thinking disabled). A stale constant here quotes a
    wrong number at the person deciding whether to run it."""
    assert warm.SECONDS_PER_CALL == pytest.approx(10.0, abs=2.0)
    assert warm.SECONDS_PER_CALL < workout_coach.DEFAULT_TIMEOUT_S
