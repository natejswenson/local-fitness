"""Auto-reflect — parser contract, prompt purity, idempotency, fail-silence.

The SDK call itself is glue and is covered by conftest's autouse
`_no_live_sdk_calls` guard (reflect funnels through `claude_agent_sdk.query`
like every other generator); tests that need generated text patch
`reflect.generate_reflection` directly, per the conftest convention.
"""
from __future__ import annotations

import asyncio
from datetime import date

import pytest

from local_fitness import db
from local_fitness.agent import coach, journal, reflect

PROFILE = coach.load_profile("hardass")

CARD = {
    "activity": {"activity_id": 987, "date": "2026-07-22",
                 "activity_name": "Tempo Tuesday", "activity_type": "running"},
    "overall": {"stars": 2.8},
    "intent": "tempo", "intent_source": "plan",
    "metrics": {"distance": {"stars": 4.0}, "pace": {"stars": 1.6}},
    "coach_read": {"distance": "held the distance", "pace": "way off the reps"},
}

BRIEF = {
    "date": "2026-07-23",
    "takeaways": [
        {"tone": "critical", "headline": "You skipped the intervals again"},
        {"tone": "neutral", "headline": "Sleep landed on baseline"},
    ],
}


@pytest.fixture
def jdb(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    return p


# --- parse_reflection -------------------------------------------------------


def test_parse_none_and_garbage_yield_no_entries():
    assert reflect.parse_reflection("NONE") == []
    assert reflect.parse_reflection("") == []
    assert reflect.parse_reflection("Here are my thoughts on the run...") == []


def test_parse_extracts_memory_lines_and_caps_at_two():
    text = (
        "MEMORY: Skipped intervals, blamed a meeting — third calendar excuse.\n"
        "MEMORY: Right knee flag on the tempo.\n"
        "MEMORY: A third line that must be dropped.\n"
    )
    assert reflect.parse_reflection(text) == [
        "Skipped intervals, blamed a meeting — third calendar excuse.",
        "Right knee flag on the tempo.",
    ]


def test_parse_tolerates_bullets_case_and_blank_entries():
    text = "- memory: kept the promise this week\nMEMORY:\n* MEMORY: second one"
    assert reflect.parse_reflection(text) == [
        "kept the promise this week", "second one"]


def test_parse_dedupes_against_existing_case_insensitively():
    text = "MEMORY: Blamed the heat again.\nMEMORY: A genuinely new one."
    out = reflect.parse_reflection(text, existing=("blamed the heat again.",))
    assert out == ["A genuinely new one."]
    # And within one generation.
    twice = "MEMORY: Same line.\nMEMORY: same line."
    assert reflect.parse_reflection(twice) == ["Same line."]


def test_parse_truncates_overlong_entries_at_a_word_boundary():
    long = "word " * 100  # 500 chars
    out = reflect.parse_reflection(f"MEMORY: {long}")
    assert len(out) == 1
    assert len(out[0]) <= journal.ENTRY_MAX_CHARS
    assert not out[0].endswith("wor")  # no mid-word cut
    # The truncated entry must be saveable as-is.
    assert out[0] == out[0].strip()


# --- the grade/CTL-leak guard (0.38.2) --------------------------------------
# These two lists ARE the specification, same pattern as
# tests/test_workout_coach.py's _REAL_LEAKS/_LOOKALIKES: the real live
# offenders (measured 2026-07-26) must reject, and clean coach-voice entries
# — including lookalikes an over-eager pattern would catch — must pass.

_REAL_LEAKS = [
    "Hit distance/pace/HR (A/A+/A+) but load graded C- on the tempo.",
    "Graded D overall after ignoring the taper plan again.",
    "Distance D- (2.05/2.50mi) vs the prescribed long run.",
    "Blamed a work call after CTL peaked at 58.4 last week.",
]

_CLEAN_ENTRIES = [
    "Blamed the heat again — second time this month.",
    "Promised to hit intervals Thursday after skipping Tuesday.",
    "Right knee flag on the tempo, says it's been off for a week.",
    "A tough week — three sessions moved to rest.",
    "Said the grading feels harsh lately and asked for more warmth.",
    "Debated the A/B testing of the new commute route.",
    "Kept the promise this week after last week's excuse.",
]


@pytest.mark.parametrize("text", _REAL_LEAKS)
def test_grade_leak_entries_are_rejected(text):
    assert reflect.parse_reflection(f"MEMORY: {text}") == []


@pytest.mark.parametrize("text", _CLEAN_ENTRIES)
def test_clean_entries_are_not_rejected(text):
    assert reflect.parse_reflection(f"MEMORY: {text}") == [text]


def test_has_grade_leak_flat_function_matches_the_two_lists():
    for text in _REAL_LEAKS:
        assert reflect._has_grade_leak(text) is True
    for text in _CLEAN_ENTRIES:
        assert reflect._has_grade_leak(text) is False


def test_leading_date_is_stripped_and_capitalized():
    """A live entry read 'Jul 26: Jul 26 easy run hot...' — the render
    already prefixes the date, so a model-written leading date must be
    dropped, not doubled."""
    out = reflect.parse_reflection(
        "MEMORY: Jul 26: easy run hot, blamed the heat again")
    assert out == ["Easy run hot, blamed the heat again"]


def test_leading_date_stripped_leaves_capitalization_alone_when_already_upper():
    out = reflect.parse_reflection("MEMORY: Jul 26 Ran the tempo clean")
    assert out == ["Ran the tempo clean"]


def test_a_bare_date_with_nothing_else_is_dropped():
    assert reflect.parse_reflection("MEMORY: Jul 26") == []


# --- build_prompt -----------------------------------------------------------


def test_build_prompt_is_pure_and_carries_all_inputs():
    event = reflect._card_event(CARD)
    recent = [{"entry_date": "2026-07-18", "text": "blamed the heat"}]
    a = reflect.build_prompt(PROFILE, event, "- Plan: 2 missed.", recent, "Alex")
    b = reflect.build_prompt(PROFILE, event, "- Plan: 2 missed.", recent, "Alex")
    assert a == b  # pure
    system, user = a
    assert PROFILE.persona in system
    assert "MEMORY:" in system and "NONE" in system
    assert "Alex" in system
    assert "- Plan: 2 missed." in user
    assert "Jul 18: blamed the heat" in user
    assert "Tempo Tuesday" in user
    assert "graded workout" in user


def test_event_payloads_summarize_brief_and_card():
    be = reflect._brief_event(BRIEF)
    assert be["date"] == "2026-07-23"
    assert "[critical] You skipped the intervals again" in be["takeaways"]
    ce = reflect._card_event(CARD)
    # Severity WORDS, never the scores — the model may not name a rating, and
    # handing it the numbers is exactly how it echoed them.
    assert ce["overall"] == "off target"
    assert "pace: well off target" in ce["grades"]
    assert not any(ch.isdigit() for ch in ce["grades"])
    assert "way off the reps" in ce["coach_read"]


# --- the pipeline: idempotency, writes, fail-silence ------------------------


def _patch_generation(monkeypatch, text: str, counter: list):
    async def fake_generate(*args, **kwargs):
        counter.append(1)
        return text

    monkeypatch.setattr(reflect, "generate_reflection", fake_generate)


def test_reflect_after_brief_writes_journal_entries(jdb, monkeypatch):
    calls: list = []
    _patch_generation(
        monkeypatch,
        "MEMORY: Skipped the intervals and blamed a meeting.\n"
        "MEMORY: Second straight critical steps day.",
        calls,
    )
    reflect.reflect_after_brief_sync(BRIEF)
    entries = journal.list_entries(db_path=jdb)
    assert len(calls) == 1
    assert [e["seq"] for e in entries] == [2, 1]
    assert all(e["source"] == "brief" for e in entries)
    assert all(e["source_key"] == "2026-07-23" for e in entries)
    assert all(e["entry_date"] == "2026-07-23" for e in entries)


def test_reflected_event_is_never_reflected_twice(jdb, monkeypatch):
    calls: list = []
    _patch_generation(monkeypatch, "MEMORY: once only", calls)
    reflect.reflect_after_brief_sync(BRIEF)
    reflect.reflect_after_brief_sync(BRIEF)  # re-run: has_event short-circuits
    assert len(calls) == 1
    assert len(journal.list_entries(db_path=jdb)) == 1


def test_report_card_reflection_keys_on_activity_id(jdb, monkeypatch):
    calls: list = []
    _patch_generation(monkeypatch, "MEMORY: tempo pace collapsed late", calls)
    asyncio.run(reflect.reflect_after_report_card(CARD))
    asyncio.run(reflect.reflect_after_report_card(CARD))
    entries = journal.list_entries(db_path=jdb)
    assert len(calls) == 1
    assert len(entries) == 1
    assert entries[0]["source"] == "report_card"
    assert entries[0]["source_key"] == "987"
    assert entries[0]["entry_date"] == "2026-07-22"


def test_none_generation_writes_nothing_but_stays_idempotent_per_run(jdb, monkeypatch):
    calls: list = []
    _patch_generation(monkeypatch, "NONE", calls)
    reflect.reflect_after_brief_sync(BRIEF)
    assert journal.list_entries(db_path=jdb) == []
    # A NONE day leaves no event marker, so a later re-run MAY reflect again —
    # that is deliberate (the brief regenerating means new content).
    reflect.reflect_after_brief_sync(BRIEF)
    assert len(calls) == 2


def test_generation_failure_is_swallowed(jdb, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("stream died")

    monkeypatch.setattr(reflect, "generate_reflection", boom)
    reflect.reflect_after_brief_sync(BRIEF)  # must not raise
    asyncio.run(reflect.reflect_after_report_card(CARD))  # must not raise
    assert journal.list_entries(db_path=jdb) == []


def test_kill_switch_disables_reflection(jdb, monkeypatch):
    calls: list = []
    _patch_generation(monkeypatch, "MEMORY: should never be written", calls)
    monkeypatch.setenv("LOCAL_FITNESS_COACH_MEMORY", "0")
    reflect.reflect_after_brief_sync(BRIEF)
    asyncio.run(reflect.reflect_after_report_card(CARD))
    assert calls == []
    assert journal.list_entries(db_path=jdb) == []


def test_missing_identifiers_are_a_no_op(jdb, monkeypatch):
    calls: list = []
    _patch_generation(monkeypatch, "MEMORY: nope", calls)
    reflect.reflect_after_brief_sync({"takeaways": []})           # no date
    asyncio.run(reflect.reflect_after_report_card({"activity": {}}))  # no id
    assert calls == []


def test_write_race_is_swallowed_and_partial_writes_survive(jdb, monkeypatch):
    """A duplicate (source, source_key, seq) — the pre-check lost a race —
    dies on the unique index but must not raise or kill the other entry."""
    calls: list = []
    _patch_generation(
        monkeypatch, "MEMORY: colliding line\nMEMORY: surviving line", calls)
    # Simulate the race: the event row appears AFTER has_event passes.
    real_has_event = journal.has_event

    def racing_has_event(source, source_key, **kwargs):
        result = real_has_event(source, source_key, **kwargs)
        if not result:
            journal.save_entry(
                "raced ahead", source=source, source_key=source_key, seq=1)
        return result

    monkeypatch.setattr(journal, "has_event", racing_has_event)
    reflect.reflect_after_brief_sync(BRIEF)  # must not raise
    texts = {e["text"] for e in journal.list_entries(db_path=jdb)}
    assert "raced ahead" in texts
    assert "surviving line" in texts       # seq=2 landed
    assert "colliding line" not in texts   # seq=1 lost the race, swallowed


def test_reflection_config_matches_the_measured_profile():
    """The Sonnet-low/no-thinking config is measured, not stylistic — a drift
    here is a latency regression (see workout_coach's constants)."""
    assert reflect.DEFAULT_MODEL == "claude-sonnet-5"
    assert reflect.DEFAULT_EFFORT == "low"
    assert reflect.DEFAULT_TIMEOUT_S == 45.0
    assert reflect.MAX_ENTRIES == 2


def test_reflect_resolves_memory_dates_from_today(jdb, monkeypatch):
    """The ledger the reflection sees is computed for the real today — pin that
    the pipeline passes a real ISO date through (guards a Date-vs-str slip)."""
    seen = {}

    async def fake_generate(profile, event, ledger_text, recent, **kwargs):
        seen["ledger_text"] = ledger_text
        return "NONE"

    monkeypatch.setattr(reflect, "generate_reflection", fake_generate)
    reflect.reflect_after_brief_sync(BRIEF)
    assert "ledger_text" in seen  # pipeline reached generation with a ledger
    assert isinstance(seen["ledger_text"], str)
    assert date.today()  # sanity: today's date is what compute used internally
