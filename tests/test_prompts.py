"""Tests for the agent prompt builders in agent/prompts.py."""
from __future__ import annotations

from local_fitness.agent import prompts
from local_fitness.agent.schemas import (
    BriefContext,
    CandidateTakeaway,
    GroundedValue,
    TakeawayMetric,
)


def _ctx() -> BriefContext:
    return BriefContext(
        date="2026-06-26", user_name="Nate",
        candidates=[CandidateTakeaway(
            category="workout", fired_triggers=["workout_mandate"],
            metrics=[GroundedValue(name="tsb", value=3.2, unit="none", display="+3.2")],
            suggested_tone="positive",
            chart_metric=TakeawayMetric(metric="tsb", days=30),
            evidence="TSB +3.2 — fresh")],
        step_goal=10000)


def test_v2_system_prompt_is_toolless_but_keeps_voice():
    sp = prompts.brief_v2_system_prompt("Nate", prompts.ADAPTIVE)
    assert "mcp__fitness__" not in sp          # no tool-orchestration
    assert "NO tools" in sp and "Use ONLY the numbers provided" in sp
    assert prompts.ADAPTIVE.persona.split("\n", 1)[0] in sp  # voice/persona kept
    for j in ("CTL", "ATL", "TSB"):
        assert j in sp                          # metric translation kept


def test_v2_system_prompt_is_shorter_than_v1():
    # The shrink: V1's tool + chat-formatting + preferences sections are gone.
    assert len(prompts.brief_v2_system_prompt("Nate", prompts.ADAPTIVE)) < \
        len(prompts.system_prompt("Nate", prompts.ADAPTIVE))


def test_v2_user_prompt_embeds_context_and_keeps_schema():
    up = prompts.brief_v2_user_prompt(_ctx(), "Nate", 10000, "", prompts.ADAPTIVE)
    assert '"category": "workout"' in up        # the serialized candidate
    assert "+3.2" in up                         # the citable display value
    assert "cite ONLY these numbers" in up      # grounding instruction
    for field in ("headline", "summary", "tone", "metric", "details"):
        assert field in up                      # output schema kept
    assert "takeaways" in up


def test_v2_user_prompt_drops_v1_orchestration_and_chart_map():
    up = prompts.brief_v2_user_prompt(_ctx(), "Nate", 10000, "", prompts.ADAPTIVE)
    assert "get_training_plan_status" not in up    # no Step-1 tool list
    assert "get_metric_trend" not in up
    assert 'metric: ctl, days: 60' not in up       # no chart-metric map


def test_v2_user_prompt_continuity_section_is_conditional():
    without = prompts.brief_v2_user_prompt(_ctx(), "Nate", 10000, "", prompts.ADAPTIVE)
    assert "Recent briefs" not in without
    withc = prompts.brief_v2_user_prompt(_ctx(), "Nate", 10000,
                                         "2026-06-25:\n  Easy 5k done", prompts.ADAPTIVE)
    assert "Recent briefs" in withc and "Easy 5k done" in withc


def test_v2_user_prompt_hardens_thin_data():
    up = prompts.brief_v2_user_prompt(_ctx(), "Nate", 10000, "", prompts.ADAPTIVE)
    assert "When the data is thin" in up
    assert "do NOT estimate" in up and "BY FEEL" in up
    # Still keeps the 3-5 / mandate contract (don't drop below the count gate).
    assert "Still produce the required workout + steps takeaways" in up


def test_v2_user_prompt_persist_via_tool_swaps_the_tail():
    # In-process (default): the generator's return value IS the brief.
    inproc = prompts.brief_v2_user_prompt(_ctx(), "Nate", 10000, "", prompts.ADAPTIVE)
    assert "Return ONLY the JSON object" in inproc
    assert "call the `save_brief` tool" not in inproc
    # MCP (persist_via_tool): the external agent composes, then calls save_brief.
    mcp = prompts.brief_v2_user_prompt(_ctx(), "Nate", 10000, "", prompts.ADAPTIVE,
                                       persist_via_tool=True)
    assert "call the `save_brief` tool" in mcp
    assert "Do NOT call any other tool" in mcp
    assert "Return ONLY the JSON object — no fence" not in mcp
    # Same body either way — only the tail differs (voice + schema shared).
    assert "cite ONLY these numbers" in mcp and '"category": "workout"' in mcp


def test_v2_user_prompt_steps_harsh_gate():
    from dataclasses import replace
    harsh = replace(prompts.ADAPTIVE, harshness=9)
    soft = replace(prompts.ADAPTIVE, harshness=1)
    assert "be sharp and harsh" in prompts.brief_v2_user_prompt(_ctx(), "Nate", 10000, "", harsh)
    assert "never roast" in prompts.brief_v2_user_prompt(_ctx(), "Nate", 10000, "", soft)


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
