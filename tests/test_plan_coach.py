"""Tests for agent/plan_coach.py — the Training Plan section's coaching-line
generator (Claude call + deterministic fallback).
"""
from __future__ import annotations

import asyncio
import json

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


def test_build_prompt_names_the_rest_day_free_adherence_when_given_one():
    """Both numbers, on one line: 75% counts four rest days at full credit
    while only half the prescribed running happened."""
    _, user = plan_coach.build_prompt(
        _PROFILE, _TODAY_EASY, _LAST_7_DAYS, 75, 71, "10k",
        sessions_adherence_pct=50)
    assert ("Plan adherence over the last graded stretch: 75%. Excluding rest "
            "days, 50% of prescribed sessions.") in user


def test_build_prompt_adherence_line_is_unchanged_without_the_sessions_number():
    """None (an un-wired caller, or a window with no prescribed session) must
    reproduce the previous line byte for byte — the prompt hash is the disk
    cache's key."""
    _, user = plan_coach.build_prompt(
        _PROFILE, _TODAY_EASY, _LAST_7_DAYS, 75, 71, "10k")
    assert "Plan adherence over the last graded stretch: 75%.\n" in user
    assert "Excluding rest days" not in user
    baseline = plan_coach.build_prompt(
        _PROFILE, _TODAY_EASY, _LAST_7_DAYS, 75, 71, "10k",
        sessions_adherence_pct=None)
    assert baseline == plan_coach.build_prompt(
        _PROFILE, _TODAY_EASY, _LAST_7_DAYS, 75, 71, "10k")


def test_build_prompt_sessions_adherence_of_zero_is_still_stated():
    """0 is the number that most needs saying — a falsy-check here would drop
    'you ran none of it' and leave only the flattering total."""
    _, user = plan_coach.build_prompt(
        _PROFILE, _TODAY_EASY, _LAST_7_DAYS, 43, 71, "10k",
        sessions_adherence_pct=0)
    assert "Excluding rest days, 0% of prescribed sessions." in user


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
    # target_date is the day after the graded partial → "Yesterday".
    line = plan_coach.fallback_coaching_line(
        _TODAY_EASY, _LAST_7_DAYS, 71, "10k", target_date="2026-07-09")
    assert line == (
        "Yesterday came up short of the prescription. 71 days to your 10k."
    )


def test_fallback_never_restates_the_prescription_or_description():
    """The PDF's Today callout prints the prescription and the description
    directly above this line; repeating either made the same instruction
    appear three times on one card."""
    line = plan_coach.fallback_coaching_line(
        _TODAY_EASY, _LAST_7_DAYS, 71, "10k", target_date="2026-07-09")
    assert "4.0 mi" not in line
    assert "9:30" not in line
    assert "keep HR under 140" not in line.lower()


@pytest.mark.parametrize(
    "verdict,expected_prefix",
    [
        ("done", "Yesterday you hit the session clean."),
        ("missed", "Yesterday was a skip."),
        ("compliant", "Yesterday was a scheduled rest day."),
    ],
)
def test_fallback_verdict_phrases(verdict, expected_prefix):
    history = [{"date": "2026-07-08", "type": "x", "planned_mi": None, "actual_mi": None, "verdict": verdict}]
    line = plan_coach.fallback_coaching_line(
        _TODAY_EASY, history, 71, "10k", target_date="2026-07-09")
    assert line.startswith(expected_prefix)


def test_fallback_today_graded_run_is_not_called_yesterday():
    """The bug: last_7_days INCLUDES target_date, so a run that has already
    synced and graded on the report's own date is the first non-pending entry.
    A hardcoded "Yesterday" credited today's run to yesterday; the reference
    must resolve to today."""
    history = [{"date": "2026-07-09", "type": "tempo", "planned_mi": 5.0,
                "actual_mi": 5.1, "verdict": "done"}]
    line = plan_coach.fallback_coaching_line(
        _TODAY_EASY, history, 71, "10k", target_date="2026-07-09")
    assert line.startswith("Today's session is already in the book.")
    assert "yesterday" not in line.lower()


def test_fallback_today_rest_day_reads_as_today():
    """A rest day grades 'compliant' unconditionally, all day — so on a rest
    day the first non-pending entry is today, and it must not say 'Yesterday
    was a scheduled rest day.'"""
    history = [{"date": "2026-07-09", "type": "rest", "planned_mi": None,
                "actual_mi": None, "verdict": "compliant"}]
    line = plan_coach.fallback_coaching_line(
        _TODAY_EASY, history, 71, "10k", target_date="2026-07-09")
    assert line.startswith("Today is a scheduled rest day.")


def test_fallback_lagging_frontier_names_the_actual_day():
    """When the frontier lags, the first non-pending entry can be several days
    old — "Yesterday" would misattribute a days-old result. It must name the
    real day instead."""
    history = [
        {"date": "2026-07-09", "type": "easy", "planned_mi": 4.0, "actual_mi": None, "verdict": "pending"},
        {"date": "2026-07-08", "type": "easy", "planned_mi": 4.0, "actual_mi": None, "verdict": "pending"},
        {"date": "2026-07-06", "type": "tempo", "planned_mi": 3.0, "actual_mi": 0.0, "verdict": "missed"},
    ]
    line = plan_coach.fallback_coaching_line(
        _TODAY_EASY, history, 71, "10k", target_date="2026-07-09")
    assert line.startswith("Jul 6 was a skip.")
    assert "yesterday" not in line.lower()


def test_fallback_without_target_date_names_absolute_day():
    """No target_date (legacy caller) → the day is named by its absolute date,
    never a possibly-wrong relative word."""
    line = plan_coach.fallback_coaching_line(_TODAY_EASY, _LAST_7_DAYS, 71, "10k")
    assert line.startswith("Jul 8 came up short of the prescription.")


def test_fallback_missing_prior_date_omits_the_verdict_phrase():
    history = [{"type": "x", "planned_mi": None, "actual_mi": None, "verdict": "done"}]
    line = plan_coach.fallback_coaching_line(
        _TODAY_EASY, history, 71, "10k", target_date="2026-07-09")
    assert line == "71 days to your 10k."


def test_fallback_all_pending_history_has_no_verdict_phrase():
    history = [{"date": "2026-07-09", "type": "easy", "planned_mi": 4.0, "actual_mi": None, "verdict": "pending"}]
    line = plan_coach.fallback_coaching_line(
        _TODAY_EASY, history, 71, "10k", target_date="2026-07-09")
    assert line == "71 days to your 10k."


def test_fallback_empty_history_does_not_raise():
    line = plan_coach.fallback_coaching_line(_TODAY_EASY, [], 71, "10k")
    assert line == "71 days to your 10k."


def test_fallback_no_race_date_uses_working_toward_phrasing():
    line = plan_coach.fallback_coaching_line(_TODAY_EASY, [], None, "base building")
    assert "Working toward your base building." in line
    assert "days to your" not in line


def test_fallback_is_pure():
    a = plan_coach.fallback_coaching_line(
        _TODAY_EASY, _LAST_7_DAYS, 71, "10k", target_date="2026-07-09")
    b = plan_coach.fallback_coaching_line(
        _TODAY_EASY, _LAST_7_DAYS, 71, "10k", target_date="2026-07-09")
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


def test_generate_coaching_line_does_not_follow_the_brief_generators_model(patched_sdk):
    """Decoupled on purpose (#241). briefing.DEFAULT_MODEL also drives the
    eval'd daily brief, where a model change has to clear the scorer, a
    cross-model A/B and the invention-rate gate — so following it meant this
    call could never be tuned, and it inherited the SDK's default effort along
    with the model. 23 of 30 evening briefs timed out on that config."""
    from local_fitness.agent import briefing

    text = asyncio.run(
        plan_coach.generate_coaching_line(_PROFILE, _TODAY_EASY, _LAST_7_DAYS, 75, 71, "10k")
    )
    assert text == "Go hit your easy 4 and don't slide again."
    assert len(patched_sdk) == 1
    assert patched_sdk[0]["options"].model != briefing.DEFAULT_MODEL
    assert patched_sdk[0]["options"].model == plan_coach.DEFAULT_MODEL


def test_generate_coaching_line_disables_thinking_and_runs_at_low_effort(patched_sdk):
    """The three settings whose absence caused the timeouts. Sonnet runs
    adaptive thinking whenever `thinking` is unset, so the model ID alone would
    have been a latency regression rather than a fix — the same finding
    workout_coach measured (median 142.9s -> 10.0s)."""
    asyncio.run(
        plan_coach.generate_coaching_line(_PROFILE, _TODAY_EASY, _LAST_7_DAYS, 75, 71, "10k")
    )
    options = patched_sdk[0]["options"]
    assert options.effort == "low"
    assert options.thinking == {"type": "disabled"}
    assert options.max_turns == 1        # single-shot; no tool loop


def test_generate_coaching_line_waits_the_modules_own_ceiling(patched_sdk, monkeypatch):
    """The ceiling is a named constant above the old 30.0 literal, and it is
    what an argument-free call actually waits. 45s matches reflect (a short
    generation inside a scheduled job), not workout_coach's interactive 90s."""
    seen: list[float] = []
    real_wait_for = asyncio.wait_for

    async def recording_wait_for(coro, timeout):
        seen.append(timeout)
        return await real_wait_for(coro, timeout)

    monkeypatch.setattr(plan_coach.asyncio, "wait_for", recording_wait_for)
    asyncio.run(
        plan_coach.generate_coaching_line(_PROFILE, _TODAY_EASY, _LAST_7_DAYS, 75, 71, "10k")
    )
    assert seen == [45.0]
    assert plan_coach.DEFAULT_TIMEOUT_S == 45.0


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


# --- 4a: ground_coaching_line — advisory grounding of the PDF coaching line -

_PLAN_SECTION = {
    "adherence_pct": 75,
    "days_to_race": 71,
    "goal_type": "10k",
    "week_planned_mi": 13.7,
    "week_actual_mi": 6.9,
    "slips": 2,
    "today": {
        "type": "easy",
        "distance_mi": 2.49,
        "pace_min_per_mi": "9:23",
        "description": "keep HR under 140",
    },
    "last_7_days": _LAST_7_DAYS,
}


def test_ground_coaching_line_flags_an_invented_adherence_number():
    # 80% is a subtle corruption of the real 75% adherence: close enough to
    # read as the same metric (within the NEARBY band) but not equal (past
    # the EXACT band) -> flagged, not silently accepted.
    text = "You're running at 80% adherence this week -- keep it up."
    flags = plan_coach.ground_coaching_line(text, _PLAN_SECTION)
    assert len(flags) == 1
    assert flags[0].nearest_metric == "adherence_pct"
    assert flags[0].takeaway_index == 0
    assert flags[0].token == "80%"


def test_ground_coaching_line_passes_faithful_citations_including_pace_string():
    # Cites the real adherence (75%) and the real pace string "9:23/mi" --
    # the tokenizer splits "9:23" into (9, 23), the same numeric tokens the
    # pool derives from today's pace_min_per_mi ("9:23") -- both match their
    # pool entries exactly, so this is a faithful citation, not a flag.
    text = "Solid 75% adherence lately. Today's easy goes out around 9:23/mi."
    flags = plan_coach.ground_coaching_line(text, _PLAN_SECTION)
    assert flags == []


def test_ground_coaching_line_accepts_a_faithful_sessions_adherence_citation():
    # The sessions number joins the pool only when the plan section carries
    # it — the same condition under which build_prompt puts it in the prompt.
    section = {**_PLAN_SECTION, "sessions_adherence_pct": 50}
    assert plan_coach.ground_coaching_line(
        "75% overall, but 50% of the sessions you were actually given.",
        section) == []


def test_ground_coaching_line_flags_a_corrupted_sessions_adherence():
    # 52% against a real 50%: past the EXACT band, inside NEARBY -> flagged,
    # and attributed to the sessions number rather than to adherence_pct.
    section = {**_PLAN_SECTION, "sessions_adherence_pct": 50}
    flags = plan_coach.ground_coaching_line(
        "Only 52% of your prescribed sessions got done.", section)
    assert [(f.token, f.nearest_metric, f.delta) for f in flags] == [
        ("52%", "sessions_adherence_pct", 2.0)]


def test_ground_coaching_line_sessions_pool_entry_is_absent_when_unwired():
    # Same prose, a section that never carried the number: 52 is now 30% off
    # the nearest pool entry (75% adherence), which reads as an unrelated
    # quantity and is ignored rather than misattributed.
    assert plan_coach.ground_coaching_line(
        "Only 52% of your prescribed sessions got done.", _PLAN_SECTION) == []


def test_ground_coaching_line_empty_list_on_numberless_prose():
    text = "Keep showing up and trust the process."
    assert plan_coach.ground_coaching_line(text, _PLAN_SECTION) == []


def test_ground_coaching_line_empty_pool_guard_returns_empty_list():
    # A plan_section with no citable numbers at all (e.g. malformed/partial
    # data) must short-circuit to [] rather than raise inside _nearest, which
    # documents a non-empty-pool precondition.
    empty_section = {"today": {}}
    text = "You're crushing it at 80% adherence."
    assert plan_coach.ground_coaching_line(text, empty_section) == []


def test_ground_coaching_line_never_raises_on_malformed_plan_section():
    # A structurally-wrong plan_section (wrong types) must downgrade to "no
    # flags" rather than propagate -- the module's advisory-signal contract.
    malformed = {"adherence_pct": "not-a-number", "today": "not-a-dict"}
    assert plan_coach.ground_coaching_line("80% adherence", malformed) == []


def test_ground_coaching_line_days_to_race_cited_in_prose_typically_skipped():
    # grounding's time-window rule skips a number immediately followed by
    # "days" -- "71 days to your 10k" reads as a window, not a metric claim,
    # so it produces no flag even though 71 IS in the pool (contrast: an
    # invented, non-window-worded citation of a nearby-but-wrong number would
    # still flag).
    text = "71 days to your 10k -- stay the course."
    assert plan_coach.ground_coaching_line(text, _PLAN_SECTION) == []


# --- generate_coaching_line_cached (2026-07-19 facet review) -----------------
# 9 PDF renders in one day fired 9 identical SDK round-trips for the same
# one-line paragraph. The cache keys on the pure build_prompt output, so
# identical inputs reuse the line and any input change regenerates.

def _fake_generator(lines):
    """A stand-in for generate_coaching_line that pops from ``lines`` and
    counts calls."""
    calls = {"n": 0}

    # **_kw, not an enumerated signature: this double stands in for a real
    # function whose keyword surface grows (user_name landed 2026-07-22), and a
    # double that has to be edited for every new kwarg is a double that fails
    # for reasons unrelated to what the test is checking.
    async def fake(profile, today_workout, last_7_days, adherence_pct,
                   days_to_race, goal_type, *, model=None,
                   timeout=plan_coach.DEFAULT_TIMEOUT_S,
                   notes_text=None, **_kw):
        calls["n"] += 1
        result = lines[calls["n"] - 1]
        if isinstance(result, Exception):
            raise result
        return result

    return fake, calls


def test_cached_line_reused_for_identical_inputs(monkeypatch, tmp_path):
    fake, calls = _fake_generator(["Get out the door."])
    monkeypatch.setattr(plan_coach, "generate_coaching_line", fake)
    cache = tmp_path / "cache.json"

    args = (_PROFILE, _TODAY_EASY, _LAST_7_DAYS, 83, 12, "10k")
    first = asyncio.run(plan_coach.generate_coaching_line_cached(*args, cache_path=cache))
    second = asyncio.run(plan_coach.generate_coaching_line_cached(*args, cache_path=cache))
    assert first == second == "Get out the door."
    assert calls["n"] == 1  # second render never touched the SDK


def test_cache_key_does_not_move_when_the_default_model_changes(monkeypatch, tmp_path):
    """model=None hashes the literal "default", NOT DEFAULT_MODEL — the byte
    layout generate_coaching_line_cached has always used. Guard, not a fix:
    it is what let #241's model swap land without evicting the live cache's
    stored lines. An explicit model is still its own key."""
    fake, calls = _fake_generator(["Get out the door.", "Regenerated."])
    monkeypatch.setattr(plan_coach, "generate_coaching_line", fake)
    cache = tmp_path / "cache.json"
    args = (_PROFILE, _TODAY_EASY, _LAST_7_DAYS, 83, 12, "10k")

    asyncio.run(plan_coach.generate_coaching_line_cached(*args, cache_path=cache))
    # A caller naming the model "default" hits the very same entry.
    hit = asyncio.run(plan_coach.generate_coaching_line_cached(
        *args, model="default", cache_path=cache))
    assert hit == "Get out the door."
    assert calls["n"] == 1
    # Naming the real model is a different key — it must not silently reuse it.
    other = asyncio.run(plan_coach.generate_coaching_line_cached(
        *args, model=plan_coach.DEFAULT_MODEL, cache_path=cache))
    assert other == "Regenerated."
    assert calls["n"] == 2


def test_cache_regenerates_when_any_input_changes(monkeypatch, tmp_path):
    fake, calls = _fake_generator(["Line A.", "Line B."])
    monkeypatch.setattr(plan_coach, "generate_coaching_line", fake)
    cache = tmp_path / "cache.json"

    a = asyncio.run(plan_coach.generate_coaching_line_cached(
        _PROFILE, _TODAY_EASY, _LAST_7_DAYS, 83, 12, "10k", cache_path=cache))
    b = asyncio.run(plan_coach.generate_coaching_line_cached(
        _PROFILE, _TODAY_EASY, _LAST_7_DAYS, 90, 12, "10k", cache_path=cache))
    assert (a, b) == ("Line A.", "Line B.")
    assert calls["n"] == 2


def test_generation_failure_is_not_cached(monkeypatch, tmp_path):
    fake, calls = _fake_generator([RuntimeError("SDK down"), "Recovered line."])
    monkeypatch.setattr(plan_coach, "generate_coaching_line", fake)
    cache = tmp_path / "cache.json"
    args = (_PROFILE, _TODAY_EASY, _LAST_7_DAYS, 83, 12, "10k")

    with pytest.raises(RuntimeError, match="SDK down"):
        asyncio.run(plan_coach.generate_coaching_line_cached(*args, cache_path=cache))
    assert not cache.exists()  # the failure left nothing behind

    recovered = asyncio.run(plan_coach.generate_coaching_line_cached(*args, cache_path=cache))
    assert recovered == "Recovered line."
    assert calls["n"] == 2
    # And the recovery IS cached for the next render.
    again = asyncio.run(plan_coach.generate_coaching_line_cached(*args, cache_path=cache))
    assert again == "Recovered line."
    assert calls["n"] == 2


def test_corrupt_cache_file_is_ignored_and_rewritten(monkeypatch, tmp_path):
    fake, calls = _fake_generator(["Clean line."])
    monkeypatch.setattr(plan_coach, "generate_coaching_line", fake)
    cache = tmp_path / "cache.json"
    cache.write_text("{ not json at all", encoding="utf-8")

    args = (_PROFILE, _TODAY_EASY, _LAST_7_DAYS, 83, 12, "10k")
    line = asyncio.run(plan_coach.generate_coaching_line_cached(*args, cache_path=cache))
    assert line == "Clean line."
    assert calls["n"] == 1
    # Cache healed: the entry now round-trips, in the v2 multi-entry shape.
    import json as _json
    data = _json.loads(cache.read_text(encoding="utf-8"))
    assert data["version"] == 2
    assert [e["line"] for e in data["entries"].values()] == ["Clean line."]


# --------------------------------------------------------------------------- #
# 0.36.0 multi-entry cache (S5)
# --------------------------------------------------------------------------- #
def test_two_alternating_keys_both_stay_cached(monkeypatch, tmp_path):
    """THE thrash case the v2 format exists for: rendering two brief dates
    alternately must not evict each other (single-entry 'latest key wins'
    made every alternation a live SDK call)."""
    fake, calls = _fake_generator(["Line for easy.", "Line for long."])
    monkeypatch.setattr(plan_coach, "generate_coaching_line", fake)
    cache = tmp_path / "cache.json"
    args_a = (_PROFILE, _TODAY_EASY, _LAST_7_DAYS, 83, 12, "10k")
    today_long = {"type": "long", "distance_mi": 10.0,
                  "pace_min_per_mi": "10:15", "description": ""}
    args_b = (_PROFILE, today_long, _LAST_7_DAYS, 83, 12, "10k")

    a1 = asyncio.run(plan_coach.generate_coaching_line_cached(*args_a, cache_path=cache))
    b1 = asyncio.run(plan_coach.generate_coaching_line_cached(*args_b, cache_path=cache))
    a2 = asyncio.run(plan_coach.generate_coaching_line_cached(*args_a, cache_path=cache))
    b2 = asyncio.run(plan_coach.generate_coaching_line_cached(*args_b, cache_path=cache))
    assert (a1, b1) == (a2, b2) == ("Line for easy.", "Line for long.")
    assert calls["n"] == 2  # exactly one generation per distinct key


def test_v1_single_entry_file_reads_as_a_hit(monkeypatch, tmp_path):
    """An upgrade must keep the v1 file's one hit — regenerating on the first
    post-upgrade render would be a silent SDK call for a line we have."""
    fake, calls = _fake_generator(["Should never generate."])
    monkeypatch.setattr(plan_coach, "generate_coaching_line", fake)
    args = (_PROFILE, _TODAY_EASY, _LAST_7_DAYS, 83, 12, "10k")
    import hashlib
    system_prompt, user_prompt = plan_coach.build_prompt(*args)
    key = hashlib.sha256(
        "\x00".join([system_prompt, user_prompt, "default"]).encode("utf-8")
    ).hexdigest()
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({"key": key, "line": "V1 cached line."}))

    line = asyncio.run(plan_coach.generate_coaching_line_cached(*args, cache_path=cache))
    assert line == "V1 cached line."
    assert calls["n"] == 0


def test_cache_evicts_oldest_ts_at_the_cap(tmp_path):
    entries = {
        f"key{i}": {"line": f"line {i}", "ts": f"2026-07-{i + 1:02d}T00:00:00+00:00"}
        for i in range(plan_coach.CACHE_MAX_ENTRIES)
    }
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({"version": 2, "entries": entries}))
    plan_coach._write_cached_line(cache, "fresh", "the newest line")
    kept = plan_coach._load_cache_entries(cache)
    assert len(kept) == plan_coach.CACHE_MAX_ENTRIES
    assert "fresh" in kept
    assert "key0" not in kept  # oldest ts evicted
    assert "key1" in kept
