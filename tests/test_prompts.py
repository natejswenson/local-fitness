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


def test_briefing_prompt_prefetch_branch():
    """Both prompt branches must hold the schema lock + mandates (design QG-1).
    The pre-fetch branch injects the data and tells the model not to re-fetch."""
    bundle = '{"get_today_status": {"latest": {"rhr": 50}}}'
    p = prompts.briefing_prompt("Dana", prefetched=bundle)
    low = p.lower()
    # data injected + don't-refetch instruction
    assert bundle in p
    assert "already gathered" in low and "do not call those tools again" in low
    # schema lock + mandates still present in the pre-fetch branch
    assert "non-negotiable" in low and "takeaways" in low
    assert "steps mandate" in low and "workout mandate" in low

    # empty branch keeps the gather-via-tools list
    e = prompts.briefing_prompt("Dana", prefetched="")
    assert "call (in any sensible order)" in e.lower()
    assert "get_today_status" in e


def test_briefing_prompt_folds_in_training_plan():
    """The active plan rides inside the workout takeaway, recovery wins, and
    there is no parallel 'training plan' card (design §4c)."""
    p = prompts.briefing_prompt("Dana")
    low = p.lower()
    assert "get_training_plan_status" in low
    assert "adherence" in low
    assert "precedence" in low          # recovery takes precedence over the schedule
    assert "active: false" in low       # the no-plan branch is explicit
    # no separate card — folded into the workout slot
    assert "do not add a separate" in low


def test_module_constants_built():
    assert isinstance(prompts.SYSTEM_PROMPT, str) and prompts.SYSTEM_PROMPT
    assert isinstance(prompts.BRIEFING_PROMPT, str) and prompts.BRIEFING_PROMPT


def test_system_prompt_is_cache_stable(monkeypatch):
    """The system prompt is the cached prefix every brief/chat turn reuses.
    It must contain no runtime-volatile content, or the SDK cache busts on
    every turn (design #5)."""
    import inspect

    src = inspect.getsource(prompts.system_prompt).lower()
    for marker in ("datetime", "date.today", "time.time", "time.monotonic", "uuid", "random."):
        assert marker not in src, f"system_prompt() must stay cache-stable; found '{marker}'"

    # Byte-identical across calls with the same notes → stable cacheable prefix.
    monkeypatch.setattr(prompts.user_notes_mod, "render_for_prompt", lambda *a, **k: "[0] roast me")
    assert prompts.system_prompt("Dana") == prompts.system_prompt("Dana")
