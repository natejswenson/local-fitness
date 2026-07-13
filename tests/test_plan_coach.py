"""Tests for agent/plan_coach.py — the Training Plan section's coaching-line
generator (Claude call + deterministic fallback).
"""
from __future__ import annotations

import asyncio

import pytest

from local_fitness.agent import coach, plan_coach

_PROFILE = coach.load_profile("hardass")

_TODAY_EASY = {
    "type": "easy",
    "distance_mi": 4.0,
    "pace_min_per_mi": "9:30",
    "description": "keep HR under 140",
}

_LAST_7_DAYS = [
    {"date": "2026-07-09", "type": "easy", "planned_mi": 4.0, "actual_mi": None, "verdict": "pending"},
    {"date": "2026-07-08", "type": "easy", "planned_mi": 4.0, "actual_mi": 2.96, "verdict": "partial"},
    {"date": "2026-07-07", "type": "rest", "planned_mi": None, "actual_mi": None, "verdict": "compliant"},
    {"date": "2026-07-06", "type": "tempo", "planned_mi": 3.0, "actual_mi": 3.05, "verdict": "done"},
    {"date": "2026-07-03", "type": "long", "planned_mi": 6.0, "actual_mi": 0.0, "verdict": "missed"},
]


# --- build_prompt: pure assembly -------------------------------------------

def test_build_prompt_includes_prescription_and_description():
    system, user = plan_coach.build_prompt(_PROFILE, _TODAY_EASY, _LAST_7_DAYS, 75, 71, "10k")
    assert "easy 4.0 mi @ 9:30/mi" in user
    assert "keep HR under 140" in user


def test_build_prompt_omits_description_line_when_absent():
    workout = {"type": "rest", "distance_mi": None, "pace_min_per_mi": None, "description": ""}
    _, user = plan_coach.build_prompt(_PROFILE, workout, [], 75, 71, "10k")
    assert "Prescription notes" not in user


def test_build_prompt_includes_adherence_and_days_to_race():
    _, user = plan_coach.build_prompt(_PROFILE, _TODAY_EASY, _LAST_7_DAYS, 75, 71, "10k")
    assert "75%" in user
    assert "71 days to the 10k." in user


def test_build_prompt_no_race_date_uses_goal_only_phrasing():
    _, user = plan_coach.build_prompt(_PROFILE, _TODAY_EASY, _LAST_7_DAYS, 75, None, "base building")
    assert "days to the" not in user
    assert "Goal: base building." in user


def test_build_prompt_lists_last_7_days_most_recent_first():
    _, user = plan_coach.build_prompt(_PROFILE, _TODAY_EASY, _LAST_7_DAYS, 75, 71, "10k")
    assert "2026-07-08: easy" in user
    assert "planned 4.0 mi, actual 2.96 mi, verdict partial" in user
    assert "2026-07-07: rest" in user
    assert "planned —, actual —, verdict compliant" in user


def test_build_prompt_empty_history_omits_history_section():
    _, user = plan_coach.build_prompt(_PROFILE, _TODAY_EASY, [], 75, 71, "10k")
    assert "Last 7 days" not in user


def test_build_prompt_system_prompt_carries_coach_voice():
    system, _ = plan_coach.build_prompt(_PROFILE, _TODAY_EASY, _LAST_7_DAYS, 75, 71, "10k")
    assert _PROFILE.dials_line in system
    assert _PROFILE.persona in system
    assert "no headline" in system.lower() or "no markdown" in system.lower()


# --- 3a: notes parity + metric-translation block ---------------------------

def test_build_prompt_metric_translation_block_always_present():
    # Always present, regardless of whether notes_text is provided.
    system, _ = plan_coach.build_prompt(_PROFILE, _TODAY_EASY, _LAST_7_DAYS, 75, 71, "10k")
    assert "CTL" in system and "fitness" in system
    assert "ATL" in system and "fatigue" in system
    assert "TSB" in system and "freshness" in system

    system_with_notes, _ = plan_coach.build_prompt(
        _PROFILE, _TODAY_EASY, _LAST_7_DAYS, 75, 71, "10k",
        notes_text="[1] stop roasting my steps",
    )
    assert "CTL" in system_with_notes


def test_build_prompt_appends_notes_text_to_system_prompt():
    system, _ = plan_coach.build_prompt(
        _PROFILE, _TODAY_EASY, _LAST_7_DAYS, 75, 71, "10k",
        notes_text="[1] stop roasting my steps",
    )
    assert "stop roasting my steps" in system


def test_build_prompt_omits_notes_section_when_none():
    system, _ = plan_coach.build_prompt(_PROFILE, _TODAY_EASY, _LAST_7_DAYS, 75, 71, "10k")
    assert "What Nate has told you" not in system


def test_build_prompt_omits_notes_section_when_empty_string():
    system, _ = plan_coach.build_prompt(
        _PROFILE, _TODAY_EASY, _LAST_7_DAYS, 75, 71, "10k", notes_text="",
    )
    assert "What Nate has told you" not in system


def test_build_prompt_remains_pure_and_deterministic_with_notes():
    a, _ = plan_coach.build_prompt(
        _PROFILE, _TODAY_EASY, _LAST_7_DAYS, 75, 71, "10k", notes_text="[1] note",
    )
    b, _ = plan_coach.build_prompt(
        _PROFILE, _TODAY_EASY, _LAST_7_DAYS, 75, 71, "10k", notes_text="[1] note",
    )
    assert a == b


# --- fallback_coaching_line: pure, deterministic ---------------------------

def test_fallback_partial_prior_day():
    line = plan_coach.fallback_coaching_line(_TODAY_EASY, _LAST_7_DAYS, 71, "10k")
    assert line.startswith("Yesterday came up short of the prescription.")
    assert "Today: easy 4.0 mi @ 9:30/mi." in line
    assert "keep HR under 140" in line
    assert "71 days to your 10k." in line


@pytest.mark.parametrize(
    "verdict,expected_prefix",
    [
        ("done", "You hit yesterday's session clean."),
        ("missed", "Yesterday was a skip."),
        ("compliant", "Yesterday was a scheduled rest day."),
    ],
)
def test_fallback_verdict_phrases(verdict, expected_prefix):
    history = [{"date": "2026-07-08", "type": "x", "planned_mi": None, "actual_mi": None, "verdict": verdict}]
    line = plan_coach.fallback_coaching_line(_TODAY_EASY, history, 71, "10k")
    assert line.startswith(expected_prefix)


def test_fallback_all_pending_history_has_no_verdict_phrase():
    history = [{"date": "2026-07-09", "type": "easy", "planned_mi": 4.0, "actual_mi": None, "verdict": "pending"}]
    line = plan_coach.fallback_coaching_line(_TODAY_EASY, history, 71, "10k")
    assert line.startswith("Today: easy 4.0 mi @ 9:30/mi.")


def test_fallback_empty_history_does_not_raise():
    line = plan_coach.fallback_coaching_line(_TODAY_EASY, [], 71, "10k")
    assert "Today: easy 4.0 mi @ 9:30/mi." in line


def test_fallback_no_race_date_uses_working_toward_phrasing():
    line = plan_coach.fallback_coaching_line(_TODAY_EASY, [], None, "base building")
    assert "Working toward your base building." in line
    assert "days to your" not in line


def test_fallback_is_pure():
    a = plan_coach.fallback_coaching_line(_TODAY_EASY, _LAST_7_DAYS, 71, "10k")
    b = plan_coach.fallback_coaching_line(_TODAY_EASY, _LAST_7_DAYS, 71, "10k")
    assert a == b


# --- generate_coaching_line: SDK dispatch (mocked at the SDK boundary) -----

class _FakeAssistantMessage:
    def __init__(self, text):
        self.content = [_FakeTextBlock(text)]


class _FakeTextBlock:
    def __init__(self, text):
        self.text = text


@pytest.fixture
def patched_sdk(monkeypatch):
    """Patches claude_agent_sdk's AssistantMessage/TextBlock/query so
    generate_coaching_line's isinstance checks pass against fakes, and
    records the (prompt, options) query() was actually called with."""
    import claude_agent_sdk

    calls = []
    chunks = ["Go hit your easy 4 and don't slide again."]

    async def fake_query(*, prompt, options):
        calls.append({"prompt": prompt, "options": options})
        for chunk in chunks:
            yield _FakeAssistantMessage(chunk)

    monkeypatch.setattr(claude_agent_sdk, "AssistantMessage", _FakeAssistantMessage)
    monkeypatch.setattr(claude_agent_sdk, "TextBlock", _FakeTextBlock)
    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    return calls


def test_generate_coaching_line_defaults_to_briefing_default_model(patched_sdk):
    from local_fitness.agent import briefing

    text = asyncio.run(
        plan_coach.generate_coaching_line(_PROFILE, _TODAY_EASY, _LAST_7_DAYS, 75, 71, "10k")
    )
    assert text == "Go hit your easy 4 and don't slide again."
    assert len(patched_sdk) == 1
    assert patched_sdk[0]["options"].model == briefing.DEFAULT_MODEL


def test_generate_coaching_line_respects_explicit_model_override(patched_sdk):
    asyncio.run(
        plan_coach.generate_coaching_line(
            _PROFILE, _TODAY_EASY, _LAST_7_DAYS, 75, 71, "10k", model="claude-haiku-4-5"
        )
    )
    assert patched_sdk[0]["options"].model == "claude-haiku-4-5"


def test_generate_coaching_line_prompt_contains_prescription(patched_sdk):
    asyncio.run(
        plan_coach.generate_coaching_line(_PROFILE, _TODAY_EASY, _LAST_7_DAYS, 75, 71, "10k")
    )
    assert "easy 4.0 mi @ 9:30/mi" in patched_sdk[0]["prompt"]


def test_generate_coaching_line_plumbs_notes_text_to_build_prompt(patched_sdk):
    # 3a: generate_coaching_line's notes_text param must reach build_prompt,
    # landing in the assembled system prompt threaded to the SDK options.
    asyncio.run(
        plan_coach.generate_coaching_line(
            _PROFILE, _TODAY_EASY, _LAST_7_DAYS, 75, 71, "10k",
            notes_text="[1] stop roasting my steps",
        )
    )
    assert "stop roasting my steps" in patched_sdk[0]["options"].system_prompt


def test_generate_coaching_line_defaults_notes_text_to_none(patched_sdk):
    asyncio.run(
        plan_coach.generate_coaching_line(_PROFILE, _TODAY_EASY, _LAST_7_DAYS, 75, 71, "10k")
    )
    assert "What Nate has told you" not in patched_sdk[0]["options"].system_prompt


def test_generate_coaching_line_empty_response_raises(monkeypatch):
    import claude_agent_sdk

    async def empty_query(*, prompt, options):
        return
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(claude_agent_sdk, "AssistantMessage", _FakeAssistantMessage)
    monkeypatch.setattr(claude_agent_sdk, "TextBlock", _FakeTextBlock)
    monkeypatch.setattr(claude_agent_sdk, "query", empty_query)

    with pytest.raises(RuntimeError, match="empty response"):
        asyncio.run(
            plan_coach.generate_coaching_line(_PROFILE, _TODAY_EASY, _LAST_7_DAYS, 75, 71, "10k")
        )


def test_generate_coaching_line_times_out(monkeypatch):
    import claude_agent_sdk

    async def hanging_query(*, prompt, options):
        await asyncio.sleep(10)
        yield _FakeAssistantMessage("too late")  # pragma: no cover

    monkeypatch.setattr(claude_agent_sdk, "AssistantMessage", _FakeAssistantMessage)
    monkeypatch.setattr(claude_agent_sdk, "TextBlock", _FakeTextBlock)
    monkeypatch.setattr(claude_agent_sdk, "query", hanging_query)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(
            plan_coach.generate_coaching_line(
                _PROFILE, _TODAY_EASY, _LAST_7_DAYS, 75, 71, "10k", timeout=0.05
            )
        )
