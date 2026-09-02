"""Tests for the agent prompt builders in agent/prompts.py."""
from __future__ import annotations

import ast
import pathlib

import pytest

from local_fitness import config
from local_fitness.agent import coach, plan_coach, prompts, workout_coach
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


def test_system_prompt_has_charts_bullet():
    # 3b: chart-rendering guidance (CLAUDE.md's most emphatic convention)
    # must reach the connecting LLM via the delivered instructions/prompt.
    p = prompts.system_prompt("Dana")
    assert "Charts" in p
    assert "fenced code block" in p
    assert "chart" in p.lower()


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





# --- the single-voice gate --------------------------------------------------
# Every prompt surface that speaks to the user must compose the SAME voice
# definition. They did not: measured 2026-07-22, `plan_coach` and
# `workout_coach` carried persona + dials but omitted the profile heading and
# the notes-precedence rule, and hardcoded the user's name into their prompt
# text instead of taking it. This enumerates the surfaces so a new one cannot
# quietly skip the voice and an existing one cannot drift back.
#
# `briefing_prompt` is deliberately NOT in the voice list: it is the USER
# message, paired with `system_prompt`, which is where the voice lives. Its
# profile-sensitivity is the harsh-block gate, tested separately below.

_ALL_PROFILES = sorted(coach.PROFILE_NAMES)
_NOTES = "[1] a saved preference sentinel"

_TODAY = {"type": "easy", "distance_mi": 4.0, "pace_min_per_mi": "9:30",
          "description": "keep HR under 140"}
_WEEK = [{"date": "2026-07-08", "type": "easy", "planned_mi": 4.0,
          "actual_mi": 2.96, "verdict": "partial"}]
_CARD = {
    "activity": {"activity_id": 1, "date": "2026-07-21",
                 "activity_name": "Run", "activity_type": "running"},
    "overall": {"grade": "B", "gpa": 3.0},
    "intent": "easy", "intent_source": "plan",
    "reference": {"mode": "rolling_60d", "n": 12, "pool": "running"},
    "metrics": {}, "splits": {"available": False, "unit": "Mile", "rows": []},
}


@pytest.fixture
def no_saved_notes(monkeypatch):
    """Isolate the PROMPT text from the user's own saved notes.

    `system_prompt` and `brief_v2_system_prompt` inject `render_for_prompt()`
    themselves, and a real note legitimately contains the user's name ("When
    Nate misses goals, be snarky…"). That is user data, not tracked code, and
    must not be confused with a hardcoded name in the prompt.
    """
    from local_fitness import notes as notes_mod
    monkeypatch.setattr(notes_mod, "render_for_prompt", lambda *a, **k: "")


def _voice_surfaces(name: str, notes: str | None = None,
                    memory: str | None = None):
    """(profile, [(label, system_prompt_text)]) for every voice-bearing surface."""
    p = coach.load_profile(name)
    return p, [
        ("system_prompt", prompts.system_prompt("Alex", p, memory)),
        ("brief_v2_system_prompt",
         prompts.brief_v2_system_prompt("Alex", p, memory)),
        ("plan_coach", plan_coach.build_prompt(
            p, _TODAY, _WEEK, 75, 71, "10k",
            notes_text=notes, user_name="Alex", memory_text=memory)[0]),
        ("workout_coach", workout_coach.build_prompt(
            p, _CARD, notes_text=notes, user_name="Alex",
            memory_text=memory)[0]),
    ]


@pytest.mark.parametrize("profile_name", _ALL_PROFILES)
def test_every_voice_surface_carries_the_active_persona_and_dials(
    profile_name, no_saved_notes
):
    profile, surfaces = _voice_surfaces(profile_name)
    for label, text in surfaces:
        assert profile.persona in text, f"{label} dropped the persona"
        assert profile.dials_line in text, f"{label} dropped the dials line"
        assert profile.name in text, f"{label} never names the active profile"


@pytest.mark.parametrize("profile_name", _ALL_PROFILES)
def test_every_voice_surface_addresses_the_configured_user(
    profile_name, no_saved_notes
):
    _profile, surfaces = _voice_surfaces(profile_name)
    for label, text in surfaces:
        assert "Alex" in text, f"{label} ignores the configured user_name"


@pytest.mark.parametrize("profile_name", _ALL_PROFILES)
def test_no_voice_surface_hardcodes_a_personal_name(profile_name, no_saved_notes):
    """The bug this gate exists for: both PDF coach prompts opened with "You
    are Nate's running coach" no matter who was configured."""
    _profile, surfaces = _voice_surfaces(profile_name)
    for label, text in surfaces:
        assert "Nate" not in text, f"{label} hardcodes a personal name"


@pytest.mark.parametrize("profile_name", _ALL_PROFILES)
def test_every_voice_surface_says_notes_outrank_the_profile(
    profile_name, no_saved_notes
):
    """A saved note ("stop roasting my steps") must carry the same authority on
    every surface. Two of them never said so."""
    _profile, surfaces = _voice_surfaces(profile_name)
    for label, text in surfaces:
        assert "REFINE" in text, f"{label} omits the notes-precedence rule"


def test_the_two_coach_surfaces_carry_supplied_notes(no_saved_notes):
    _profile, surfaces = _voice_surfaces("hardass", _NOTES)
    checked = 0
    for label, text in surfaces:
        if label in ("plan_coach", "workout_coach"):
            assert _NOTES in text, f"{label} dropped the supplied notes"
            assert "What Alex has told you" in text, f"{label} mislabels the notes"
            checked += 1
    assert checked == 2


def test_flipping_the_profile_changes_every_voice_surface(no_saved_notes):
    """Not merely 'a profile is present' — the ACTIVE one has to be the one
    that lands, on all four."""
    soft = dict(_voice_surfaces("supportive")[1])
    hard = dict(_voice_surfaces("hardass")[1])
    for label in soft:
        assert soft[label] != hard[label], f"{label} is profile-insensitive"


def test_briefing_prompt_is_profile_sensitive_via_the_harsh_gate():
    """The brief's USER message doesn't restate the persona (that is the paired
    system prompt's job) — it swaps its goal-miss mandates on
    `includes_harsh_block`."""
    hard = prompts.briefing_prompt("Alex", 10000, "", coach.load_profile("hardass"))
    soft = prompts.briefing_prompt("Alex", 10000, "", coach.load_profile("supportive"))
    assert hard != soft
    assert "Be harsh" in hard
    assert "Be harsh" not in soft
    assert "Alex" in hard and "Alex" in soft


def test_voice_block_compact_variant_is_shorter_but_keeps_the_substance():
    """The V2 brief prompt is deliberately the shrunk one; its voice block must
    stay smaller while still carrying persona, dials and the precedence rule."""
    p = coach.load_profile("hardass")
    full = prompts.coach_voice_block("Alex", p)
    compact = prompts.coach_voice_block("Alex", p, compact=True)
    assert len(compact) < len(full)
    for block in (full, compact):
        assert p.persona in block
        assert p.dials_line in block
        assert "REFINE" in block


def test_user_notes_block_is_empty_without_notes():
    assert prompts.user_notes_block("Alex", None) == ""
    assert prompts.user_notes_block("Alex", "") == ""


def test_notes_prompt_section_matches_the_live_tool_schema():
    """The 'Managing preferences conversationally' section teaches the
    model a call shape by example — it must match what the registered
    tools actually accept, read straight off their input_schema rather
    than hardcoded, so a half-done rename ships silently and every
    model-initiated write errors. On dev this section still teaches
    update_user_note(line=N, ...) / delete_user_note(line=N) against
    tools that no longer take `line` at all."""
    from local_fitness.agent import tools

    p = prompts.system_prompt("Dana")
    section = p.split("# Managing preferences conversationally", 1)[1]
    section = section.split("\n#", 1)[0]  # up to the next section heading

    for param in tools.update_user_note.input_schema:
        assert param in section, f"{param} (update_user_note) not taught in the prompt"
    for param in tools.delete_user_note.input_schema:
        assert param in section, f"{param} (delete_user_note) not taught in the prompt"
    assert "line=" not in section


# --- coach memory on the voice surfaces (0.30.0) ----------------------------

_MEMORY = "- Plan: 3 missed sessions in the last 14 days (last: Jul 19 interval)."


def test_every_voice_surface_carries_supplied_memory(no_saved_notes):
    """All four surfaces must inject the memory block when memory text is
    supplied — a surface that drops it silently loses every callback."""
    _p, surfaces = _voice_surfaces("hardass", memory=_MEMORY)
    for label, text in surfaces:
        assert _MEMORY in text, f"{label} dropped the supplied memory"
        assert "What you remember about Alex" in text, (
            f"{label} mislabels the memory section")


def test_no_memory_means_no_memory_section(no_saved_notes):
    """Empty memory (disabled, fresh DB) must remove the SECTION, not render
    an empty header — an empty 'what you remember' invites invention."""
    _p, surfaces = _voice_surfaces("hardass", memory=None)
    for label, text in surfaces:
        assert "What you remember" not in text, (
            f"{label} renders a memory header with no memory")


def test_memory_block_carries_the_grounding_contract():
    """The header must forbid invented callbacks — that sentence is the whole
    safety story for handing the model a 'memory'."""
    full = prompts.coach_memory_block("Alex", _MEMORY)
    compact = prompts.coach_memory_block("Alex", _MEMORY, compact=True)
    for block in (full, compact):
        assert _MEMORY in block
        assert "NEVER" in block
    assert len(compact) < len(full)
    assert prompts.coach_memory_block("Alex", None) == ""
    assert prompts.coach_memory_block("Alex", "") == ""


def test_memory_block_carries_the_relationship_doctrine():
    """The offensive half of "using your memory" ('a fact is a receipt —
    spend it', 'a promise is a debt', a broken streak named back once) used
    to live only in coach_profiles/hardass.md's prose — so a conversational
    tune (personality.render_spec_persona REPLACES the profile prose
    wholesale) silently deleted it from every surface the moment Nate tuned
    the Sarge persona. Living in coach_memory_block instead makes it
    tune-proof by construction: no spec patch can reach into a different
    module's string. Non-compact only — V2 must not grow."""
    full = prompts.coach_memory_block("Alex", _MEMORY)
    assert "is a debt you collect on" in full
    assert "streak that broke" in full
    compact = prompts.coach_memory_block("Alex", _MEMORY, compact=True)
    assert "is a debt you collect on" not in compact
    assert len(compact) < len(full)


def test_system_prompt_carries_capture_and_recall_instructions(no_saved_notes):
    """The MCP instructions payload must keep both halves of conversation
    memory: the capture directive (save durable facts + session notes) and
    the retrieval directive (search before claiming not to remember) —
    either one silently dropping breaks 'the coach remembers'."""
    text = prompts.system_prompt("Alex", coach.resolve_coach_profile())
    assert "save_coach_memory" in text
    assert "recall_coach_memories" in text
    assert "session note" in text
    normalized = " ".join(text.split())
    assert "Never say you don't remember without searching" in normalized
    # Retrieval stays grounded, like the injected block's contract.
    assert "never cite a memory the search didn't return" in normalized


def test_system_prompt_carries_report_card_directives(no_saved_notes):
    """The coach must reach for list_report_cards/get_report_card on rating
    or trend questions rather than guessing from prose memory — and never
    state a rating that didn't come from a tool call or the memory section's
    computed summary line.

    The scale note is pinned too: a star row reads as a review score unless the
    prompt says otherwise, and "5 stars" meaning "you did what was prescribed"
    rather than "this was a great run" is the distinction the whole
    compliance/stimulus partition rests on.
    """
    text = prompts.system_prompt("Alex", coach.resolve_coach_profile())
    normalized = " ".join(text.split())
    assert "list_report_cards" in text
    assert "get_report_card" in text
    assert "NEVER state a rating that did not come from" in normalized
    assert "compliance score" in normalized
    assert "NOT a verdict on how good the run was" in normalized


def test_system_prompt_orients_toward_the_plan_write_path(no_saved_notes):
    """The persona had NO plan section while plan tools were the single largest
    usage cluster — 62 of 247 recorded tool calls — and 880 tokens went to
    sections pointing at tools called zero times. These are the four facts an
    agent cannot recover from the tool descriptions alone, because each is a
    constraint that spans two tools."""
    text = prompts.system_prompt("Alex", coach.resolve_coach_profile())
    normalized = " ".join(text.split())
    # The one-day editor, and the cap that is invisible to the grader in prose.
    assert "update_plan_workout" in text
    assert "hr_max" in text
    # It cannot move or add a day, so a swap is two calls.
    assert "cannot add a day or move one" in normalized
    # Restructuring goes through a draft, never day-by-day patching.
    assert "propose_training_plan" in text
    assert "commit_training_plan" in text
    assert "Never walk an active plan into a new shape one day at a time" in normalized
    # A draft left open is destroyed by the next proposal.
    assert "pending_draft" in text
    assert "discard_training_plan_draft" in text
    assert "silently archives it" in normalized
    # plan_chart is THE planned-vs-actual view; it had 0 recorded calls.
    assert "plan_chart" in text


def test_system_prompt_stays_under_its_size_ceiling(no_saved_notes):
    """The persona is delivered on every /coach invocation (and again via the
    MCP instructions payload), so its length is a real per-session cost.

    0.45.0 compressed two sections whose tools had never been called and spent
    part of the saving on the plan section above, taking it 14,920 -> ~12,000
    chars. This ceiling is a ratchet against silently growing it back: raising
    it is allowed, but it should be a deliberate edit with a reason, not a
    side effect."""
    text = prompts.system_prompt("Alex", coach.resolve_coach_profile())
    assert len(text) < 13_000, (
        f"system prompt grew to {len(text)} chars; compress or raise the "
        f"ceiling deliberately"
    )


# --- source guard: no personal name in executable prompt text ---------------

_PROMPT_MODULES = (
    "agent/prompts.py", "agent/plan_coach.py", "agent/workout_coach.py",
    "agent/brief_planner.py", "agent/briefing.py",
    "agent/ledger.py", "agent/journal.py", "agent/memory.py",
    "agent/reflect.py",
)
_SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "local_fitness"


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """id() of every Constant node that is a module/class/function docstring."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(id(body[0].value))
    return out


@pytest.mark.parametrize("rel", _PROMPT_MODULES)
def test_prompt_modules_carry_no_hardcoded_personal_name(rel):
    """CLAUDE.md forbids personal data in tracked code, and a name baked into a
    prompt is exactly that — a stranger's clone was told it was Nate's coach.

    Parsed with `ast` rather than grepped: comments and docstrings may name him
    (they are documentation), but a string literal that reaches a model may not.
    """
    source = (_SRC / rel).read_text(encoding="utf-8")
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    offenders = [
        (node.lineno, node.value[:70])
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in docstrings and "Nate" in node.value
    ]
    assert offenders == [], f"{rel} hardcodes a personal name: {offenders}"


def test_default_user_name_is_generic():
    """The default is what a fresh clone gets; it must never be a real name."""
    assert config.DEFAULT_USER_NAME == "the user"
    assert prompts.DEFAULT_USER_NAME is config.DEFAULT_USER_NAME
