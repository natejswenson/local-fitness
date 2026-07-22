"""The report card's verbal read: prompt assembly, caching, and fallback.

`build_prompt` is pure and is also the cache key, so its content is pinned
here rather than smoke-tested — a change to what the model is told is a change
to what gets cached.

No test in this file makes a live SDK call; conftest's autouse guard blocks
that outright, and the generating function is patched wherever specific text
matters.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from local_fitness.agent import report_card as rc
from local_fitness.agent import workout_coach
from local_fitness.agent.coach import CoachProfile

PROFILE = CoachProfile(
    name="hardass", harshness=9, warmth=1, push=9,
    roast_threshold=0.9, praise_threshold=1.0,
    persona="You are a hard-nosed coach who is never satisfied.",
)

REF = {
    "mode": "rolling_60d", "n": 20, "pool": "running",
    "median_distance_m": 10000.0, "median_pace_sec_per_km": 300.0,
    "median_hr": 150.0, "median_load": 100.0,
}
ACTIVITY = {
    "activity_id": 1, "date": "2026-07-19", "activity_name": "Morning Run",
    "activity_type": "running", "distance_meters": 10000, "duration_seconds": 3000,
    "avg_pace_sec_per_km": 300, "avg_hr": 150, "training_load": 100,
}


FOUR_SECTIONS = (
    "DISTANCE: covered the ground.\n"
    "PACE: too quick.\n"
    "HEART RATE: stayed low.\n"
    "TRAINING LOAD: banked what it should."
)


def a_card(activity=None, **kw):
    return rc.build_card({**ACTIVITY, **(activity or {})}, kw.pop("splits", []),
                         kw.pop("plan", None), kw.pop("reference", REF),
                         kw.pop("context", {}), kw.pop("hr_samples", None),
                         kw.pop("recent_activities", None),
                         kw.pop("upcoming_workouts", None))


# --- build_prompt: what the model is actually told --------------------------

def test_prompt_carries_the_computed_grades_and_forbids_re_grading():
    card = a_card()
    system, user = workout_coach.build_prompt(PROFILE, card)
    # The persona and dials are threaded in, so the read speaks in his voice.
    assert PROFILE.persona in system
    assert PROFILE.dials_line in system
    # The grades are stated as decided, and the model is told not to revise.
    assert "not yours to revise" in system
    assert f"Overall grade {card['overall']['grade']}" in user
    for label in ("Distance", "Pace", "Avg HR", "Training Load"):
        assert label in user


def test_prompt_states_the_yardstick_without_markdown_emphasis():
    # The read is a plain paragraph; handing the model ** invites it to echo
    # asterisks into prose that is never markdown-rendered.
    _, user = workout_coach.build_prompt(PROFILE, a_card())
    assert "60-day rolling median" in user
    assert "**" not in user


def test_prompt_includes_metric_translation_contract():
    system, _ = workout_coach.build_prompt(PROFILE, a_card())
    assert "CTL" in system and "fitness" in system
    assert "TSB" in system and "freshness" in system


def test_saved_notes_are_threaded_into_the_system_prompt():
    system, _ = workout_coach.build_prompt(
        PROFILE, a_card(), notes_text="- stop roasting my step count")
    assert "stop roasting my step count" in system
    assert "prefer the newer note" in system


def test_prompt_omits_notes_section_entirely_when_there_are_none():
    system, _ = workout_coach.build_prompt(PROFILE, a_card(), notes_text="")
    assert "What Nate has told you" not in system


def test_prompt_reports_ungraded_metrics_as_na_rather_than_silently_dropping():
    card = a_card(reference={"mode": "insufficient_data", "n": 2, "pool": "running"})
    _, user = workout_coach.build_prompt(PROFILE, card)
    assert user.count("n/a") >= 3


def test_prompt_surfaces_a_load_spike_and_hr_drift():
    card = a_card({"training_load": 500})
    assert card["metrics"]["load"]["spike"] is True
    _, user = workout_coach.build_prompt(PROFILE, card)
    assert "SPIKE" in user

    drift_card = a_card()
    drift_card["splits"] = {
        "available": True, "unit": "Mile", "hr_drift_pct": 7.5,
        "rows": [{"index": 1, "avg_hr": 140, "pace_min_per_mi": "8:00",
                  "partial": False}],
    }
    _, user = workout_coach.build_prompt(PROFILE, drift_card)
    assert "+7.5%" in user


def test_prompt_is_deterministic_for_identical_cards():
    # This is load-bearing: build_prompt's hash IS the cache key, so any
    # instability here would silently defeat caching.
    assert workout_coach.build_prompt(PROFILE, a_card()) == \
        workout_coach.build_prompt(PROFILE, a_card())


# --- cache ------------------------------------------------------------------

def test_cache_hit_reuses_text_without_calling_the_generator(tmp_path, monkeypatch):
    path = tmp_path / "cache.json"
    calls = []

    async def _gen(*a, **k):
        calls.append(1)
        return FOUR_SECTIONS

    monkeypatch.setattr(workout_coach, "generate_read", _gen)
    card = a_card()
    first = asyncio.run(workout_coach.generate_read_cached(
        PROFILE, card, cache_path=path))
    second = asyncio.run(workout_coach.generate_read_cached(
        PROFILE, card, cache_path=path))
    assert first == second
    assert first["pace"] == "too quick."
    assert len(calls) == 1


def test_changed_input_misses_the_cache_and_regenerates(tmp_path, monkeypatch):
    path = tmp_path / "cache.json"
    calls = []

    async def _gen(profile, card, **k):
        aid = card["activity"]["activity_id"]
        calls.append(aid)
        return FOUR_SECTIONS.replace("too quick.", f"too quick for {aid}.")

    monkeypatch.setattr(workout_coach, "generate_read", _gen)
    asyncio.run(workout_coach.generate_read_cached(
        PROFILE, a_card(), cache_path=path))
    out = asyncio.run(workout_coach.generate_read_cached(
        PROFILE, a_card({"activity_id": 2}), cache_path=path))
    assert out["pace"] == "too quick for 2."
    assert calls == [1, 2]


def test_a_failed_generation_is_never_cached(tmp_path, monkeypatch):
    path = tmp_path / "cache.json"

    async def _boom(*a, **k):
        raise RuntimeError("stream died")

    monkeypatch.setattr(workout_coach, "generate_read", _boom)
    with pytest.raises(RuntimeError):
        asyncio.run(workout_coach.generate_read_cached(
            PROFILE, a_card(), cache_path=path))
    # Nothing written — a transient failure must not pin anything to the card.
    assert not path.exists()


def test_corrupt_cache_file_is_ignored_not_fatal(tmp_path, monkeypatch):
    path = tmp_path / "cache.json"
    path.write_text("{not json", encoding="utf-8")

    async def _gen(*a, **k):
        return FOUR_SECTIONS

    monkeypatch.setattr(workout_coach, "generate_read", _gen)
    out = asyncio.run(workout_coach.generate_read_cached(
        PROFILE, a_card(), cache_path=path))
    assert out["distance"] == "covered the ground."
    assert json.loads(path.read_text())["text"] == FOUR_SECTIONS


def test_cache_write_failure_still_returns_the_generated_text(tmp_path, monkeypatch):
    async def _gen(*a, **k):
        return FOUR_SECTIONS

    monkeypatch.setattr(workout_coach, "generate_read", _gen)
    # A directory where the cache file should go: write fails, read succeeds.
    bad = tmp_path / "cache.json"
    bad.mkdir()
    out = asyncio.run(workout_coach.generate_read_cached(
        PROFILE, a_card(), cache_path=bad))
    assert out["hr"] == "stayed low."


# --- fallback: still four sections, deterministic ---------------------------

def test_fallback_returns_all_four_sections():
    card = a_card({"distance_meters": 4000, "avg_hr": 150})
    out = workout_coach.fallback_read(card)
    assert set(out) == {"distance", "pace", "hr", "load"}
    assert all(v.strip() for v in out.values())


def test_fallback_states_each_metric_actual_and_target():
    out = workout_coach.fallback_read(a_card())
    assert "6.21 mi" in out["distance"]
    assert "/mi" in out["pace"]
    assert "bpm" in out["hr"]


def test_fallback_survives_a_card_with_no_grades_at_all():
    # The insufficient-history card carries explicit `"grade": None`, which is
    # what made an earlier `.get("grade", "")` default raise AttributeError.
    card = a_card(reference={"mode": "insufficient_data", "n": 1, "pool": "running"})
    assert all(m.get("grade") is None for m in card["metrics"].values())
    out = workout_coach.fallback_read(card)
    assert all("Not enough comparable history" in v for v in out.values())


def test_fallback_flags_a_load_spike():
    out = workout_coach.fallback_read(a_card({"training_load": 500}))
    assert "double your median day" in out["load"]


def test_fallback_is_deterministic():
    card = a_card()
    assert workout_coach.fallback_read(card) == workout_coach.fallback_read(card)


# --- parse_read: four labelled sections or nothing --------------------------

def test_parse_splits_labelled_sections():
    out = workout_coach.parse_read(FOUR_SECTIONS)
    assert out == {
        "distance": "covered the ground.", "pace": "too quick.",
        "hr": "stayed low.", "load": "banked what it should.",
    }


def test_parse_tolerates_model_cosmetics():
    # Blank lines, bold markers, bullets and mixed case are all cosmetic — none
    # change the content, and regenerating over a stray asterisk is wasteful.
    messy = (
        "**DISTANCE:** covered the ground.\n\n"
        "- Pace: too quick.\n\n"
        "  heart rate: stayed low.\n\n"
        "TRAINING LOAD: banked what it should.\n"
    )
    out = workout_coach.parse_read(messy)
    assert out["distance"] == "covered the ground."
    assert out["pace"] == "too quick."
    assert out["hr"] == "stayed low."


def test_parse_collapses_wrapped_paragraphs():
    wrapped = FOUR_SECTIONS.replace(
        "PACE: too quick.", "PACE: too quick\nby a full minute\nper mile.")
    assert workout_coach.parse_read(wrapped)["pace"] == "too quick by a full minute per mile."


@pytest.mark.parametrize("bad", [
    "",
    "DISTANCE: a. PACE: b. HEART RATE: c.",                    # load missing
    "DISTANCE: a\nPACE: b\nHEART RATE: c\nTRAINING LOAD:",     # load empty
])
def test_parse_rejects_incomplete_reads(bad):
    # A half-parsed read would render a card with a blank section. Raising
    # sends the caller to the deterministic template instead.
    with pytest.raises(ValueError):
        workout_coach.parse_read(bad)


def test_unparseable_generation_is_never_cached(tmp_path, monkeypatch):
    path = tmp_path / "cache.json"

    async def _gen(*a, **k):
        return "just some prose with no labels at all"

    monkeypatch.setattr(workout_coach, "generate_read", _gen)
    with pytest.raises(ValueError):
        asyncio.run(workout_coach.generate_read_cached(
            PROFILE, a_card(), cache_path=path))
    assert not path.exists()


def test_cached_text_that_no_longer_parses_is_a_miss_not_a_failure(tmp_path, monkeypatch):
    """An older-format cache entry must regenerate, not poison every render."""
    import hashlib

    system, user = workout_coach.build_prompt(PROFILE, a_card())
    key = hashlib.sha256("\x00".join(
        [system, user, "default", "1"]).encode("utf-8")).hexdigest()
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"key": key, "text": "old single-paragraph read"}))

    async def _gen(*a, **k):
        return FOUR_SECTIONS

    monkeypatch.setattr(workout_coach, "generate_read", _gen)
    out = asyncio.run(workout_coach.generate_read_cached(
        PROFILE, a_card(), cache_path=path))
    assert out["pace"] == "too quick."


# --- prompt + rendering -----------------------------------------------------

def test_prompt_demands_four_labelled_sections_and_forbids_letter_grades():
    system, _ = workout_coach.build_prompt(PROFILE, a_card())
    for label in ("DISTANCE:", "PACE:", "HEART RATE:", "TRAINING LOAD:"):
        assert label in system
    assert "NEVER state a letter grade" in system
    assert "45 words per paragraph" in system


def test_prompt_excludes_the_training_load_model_numbers():
    """Nate: the CTL/ATL/TSB sentence "doesn't matter". Handing those over just
    invited a freshness lecture in place of a distance verdict."""
    card = a_card(context={"ctl": 57.0, "atl": 87.0, "tsb": -30.0})
    system, user = workout_coach.build_prompt(PROFILE, card)
    assert "Do NOT discuss CTL" in system
    assert "57" not in user.split("Metric grades")[0]
    assert "freshness (TSB) -30" not in user


def test_prompt_carries_past_runs_and_upcoming_prescriptions():
    """Hindsight and foresight: a pace that is fine in isolation reads
    differently as the third hard day in a row."""
    card = a_card(
        recent_activities=[{
            "date": "2026-07-17", "activity_type": "running",
            "distance_meters": 8046, "avg_pace_sec_per_km": 300,
            "avg_hr": 150, "training_load": 95}],
        upcoming_workouts=[{
            "date": "2026-07-22", "type": "interval", "distance_mi": 5.0,
            "pace_min_per_mi": "7:30", "description": "6x800"}],
    )
    _, user = workout_coach.build_prompt(PROFILE, card)
    assert "2026-07-17" in user and "5.00 mi" in user
    assert "2026-07-22" in user and "interval" in user and "6x800" in user


def test_prompt_omits_the_history_blocks_when_there_is_none():
    _, user = workout_coach.build_prompt(PROFILE, a_card())
    assert "What led into this run" not in user
    assert "What this was setting up for" not in user


def test_markdown_card_renders_four_labelled_paragraphs():
    card = a_card()
    card["coach_read"] = {
        "distance": "You covered the ground.", "pace": "Too quick.",
        "hr": "Stayed low.", "load": "Banked what it should.",
    }
    md = rc.render_markdown(card)
    assert md.index("## Overall:") < md.index("You covered the ground.")
    for label in ("**Distance**", "**Pace**", "**Heart Rate**", "**Training Load**"):
        assert label in md
    # Distance's paragraph precedes pace's, matching the table's order.
    assert md.index("You covered the ground.") < md.index("Too quick.")
    # The yardstick is stated once, below the table. It came back when the
    # reference pool started excluding rows: which median a run is measured
    # against is now a decision the reader cannot reconstruct from the numbers.
    assert "Graded against your **60-day rolling median**" in md
    assert md.index("| Grade |") < md.index("Graded against your")


def test_markdown_card_without_a_read_opens_on_the_grade():
    md = rc.render_markdown(a_card())
    assert "## Overall:" in md
    assert "**Distance** —" not in md


# === the SDK call's shape ==================================================
# This module's read is *phrasing*: every judgment on the card was decided in
# Python before the prompt existed, and the prompt forbids re-deriving them.
# The options below are what sizes the call to that job.

class _FakeTextBlock:
    def __init__(self, text):
        self.text = text


class _FakeAssistantMessage:
    def __init__(self, text):
        self.content = [_FakeTextBlock(text)]


_WELL_FORMED = (
    "DISTANCE: You covered it.\nPACE: Too quick.\n"
    "HEART RATE: Stayed low.\nTRAINING LOAD: Banked it."
)


@pytest.fixture
def patched_sdk(monkeypatch):
    """Records the (prompt, options) query() was actually called with, and
    makes generate_read's isinstance checks pass against the fakes."""
    import claude_agent_sdk

    calls = []

    async def fake_query(*, prompt, options):
        calls.append({"prompt": prompt, "options": options})
        yield _FakeAssistantMessage(_WELL_FORMED)

    monkeypatch.setattr(claude_agent_sdk, "AssistantMessage", _FakeAssistantMessage)
    monkeypatch.setattr(claude_agent_sdk, "TextBlock", _FakeTextBlock)
    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    return calls


def test_read_does_not_follow_the_brief_generators_model(patched_sdk):
    """Decoupled on purpose. briefing.DEFAULT_MODEL also drives the daily
    brief, where a model change is a prompt change that has to clear the
    scorer first — coupling meant this call could not be tuned at all."""
    from local_fitness.agent import briefing

    asyncio.run(workout_coach.generate_read(PROFILE, a_card()))
    assert patched_sdk[0]["options"].model == workout_coach.DEFAULT_MODEL
    assert workout_coach.DEFAULT_MODEL != briefing.DEFAULT_MODEL


def test_read_disables_thinking_and_runs_at_low_effort(patched_sdk):
    """Load-bearing, not polish: this model runs adaptive thinking whenever
    `thinking` is unset, so moving the model ID forward without these would
    have made an already-67s call slower."""
    asyncio.run(workout_coach.generate_read(PROFILE, a_card()))
    options = patched_sdk[0]["options"]
    assert options.effort == "low"
    assert options.thinking == {"type": "disabled"}
    assert options.max_turns == 1        # single-shot; no tool loop


def test_read_respects_an_explicit_model_override(patched_sdk):
    asyncio.run(workout_coach.generate_read(
        PROFILE, a_card(), model="claude-haiku-4-5"))
    assert patched_sdk[0]["options"].model == "claude-haiku-4-5"


# === ungraded metrics reach the prompt with their own reason ===============

def _card_with_ungraded_pace():
    card = a_card()
    card["metrics"]["pace"] = {
        "grade": None, "actual": 399.2, "expected": 300.0, "deviation": None,
        "reference": "plan", "actual_display": "10:42/mi avg",
        "note": ("interval day, no splits recorded — average pace can't be "
                 "graded against a rep target"),
    }
    return card


def test_prompt_gives_the_metrics_own_reason_not_a_generic_one():
    """"not enough to grade" is true of a thin reference pool and wrong for an
    interval day with no splits — and the model will invent the difference."""
    _, user_prompt = workout_coach.build_prompt(PROFILE, _card_with_ungraded_pace())
    assert "no splits recorded" in user_prompt
    assert "Pace: n/a — not enough to grade" not in user_prompt


def test_prompt_labels_a_split_derived_pace():
    """A bare "8:03/mi" beside a rep target would read as the whole run's
    average, which is exactly the confusion the split-grading fix removes."""
    card = a_card()
    card["metrics"]["pace"]["actual_display"] = "8:03/mi best mile"
    _, user_prompt = workout_coach.build_prompt(PROFILE, card)
    assert "actual 8:03/mi best mile" in user_prompt


def test_fallback_read_carries_the_metrics_reason_through():
    out = workout_coach.fallback_read(_card_with_ungraded_pace())
    assert out["pace"].startswith("10:42/mi avg.")
    assert "no splits recorded" in out["pace"]
