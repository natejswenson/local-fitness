"""Tests for the agent prompt builders in agent/prompts.py."""
from __future__ import annotations

from local_fitness.agent import prompts


def test_system_prompt_core_contract():
    p = prompts.system_prompt("Dana")
    assert "Dana" in p
    assert "Never fabricate numbers" in p
    assert "mcp__fitness__" in p
    # jargon translation present
    for j in ("CTL", "ATL", "TSB"):
        assert j in p


def test_system_prompt_default_user_name():
    p = prompts.system_prompt()
    assert prompts.DEFAULT_USER_NAME in p


def test_system_prompt_has_chat_formatting_contract():
    # Steers conversational replies away from wide tables that wrap in a
    # narrow display, while leaving the JSON brief schema untouched.
    p = prompts.system_prompt("Dana")
    assert "Formatting your chat replies" in p
    assert "NOT one wide grid" in p
    assert "JSON brief" in p  # scopes the rule away from the structured brief


def test_system_prompt_injects_notes(monkeypatch):
    monkeypatch.setattr(
        prompts.user_notes_mod, "render_for_prompt", lambda *a, **k: "[0] roast me"
    )
    p = prompts.system_prompt("Dana")
    assert "roast me" in p
    assert "What Dana has told you" in p


def test_system_prompt_no_notes_section_when_empty(monkeypatch):
    monkeypatch.setattr(
        prompts.user_notes_mod, "render_for_prompt", lambda *a, **k: ""
    )
    p = prompts.system_prompt("Dana")
    assert "has told you" not in p


def test_briefing_prompt_schema_lock():
    p = prompts.briefing_prompt("Dana")
    low = p.lower()
    assert "non-negotiable" in low
    assert "exactly one key" in low
    assert "takeaways" in low


def test_briefing_prompt_recent_continuity_section():
    p = prompts.briefing_prompt("Dana", recent_briefs_summary="Fitness sliding")
    assert "recent briefs" in p.lower()
    assert "Fitness sliding" in p


def test_briefing_prompt_no_continuity_when_empty():
    p = prompts.briefing_prompt("Dana", recent_briefs_summary="   ")
    assert "recent briefs" not in p.lower()


def test_module_constants_built():
    assert isinstance(prompts.SYSTEM_PROMPT, str) and prompts.SYSTEM_PROMPT
    assert isinstance(prompts.BRIEFING_PROMPT, str) and prompts.BRIEFING_PROMPT
