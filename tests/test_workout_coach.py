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
    "STIMULUS: banked what it should."
)


def a_card(activity=None, **kw):
    return rc.build_card({**ACTIVITY, **(activity or {})}, kw.pop("splits", []),
                         kw.pop("plan", None), kw.pop("reference", REF),
                         kw.pop("context", {}), kw.pop("hr_samples", None),
                         kw.pop("recent_activities", None),
                         kw.pop("upcoming_workouts", None),
                         kw.pop("hr_zones", None))


# --- build_prompt: what the model is actually told --------------------------

def test_prompt_carries_the_computed_grades_and_forbids_re_grading():
    card = a_card()
    system, user = workout_coach.build_prompt(PROFILE, card)
    # The persona and dials are threaded in, so the read speaks in his voice.
    assert PROFILE.persona in system
    assert PROFILE.dials_line in system
    # The verdicts are stated as decided, and the model is told not to revise.
    assert "not yours to revise" in system
    # SEVERITY, not the score — the prompt must not hand the model the value
    # it is forbidden to repeat. This is the invariant, not a style choice.
    assert workout_coach.star_severity(card["overall"]["stars"]) in user
    assert f"{card['overall']['stars']:.2f}" not in user
    for label in ("Distance", "Pace", "Avg HR"):
        assert label in user
    # Training load is NOT among the per-metric verdicts any more (0.40.0) — it
    # rides in its own block, explicitly marked unjudged, so the model cannot
    # read it as a fourth thing it is being asked to grade.
    assert "Training load" not in user.split("Training stimulus")[0]
    assert "Training stimulus (REPORTED, not graded" in user
    assert "do not ask for more work" in user


def test_the_user_prompt_never_contains_a_rating():
    """The root cause of the leak: the prompt used to print every letter
    ("Distance: D- — actual 5.95 mi…") in the same context that forbade
    repeating them, and a leaked read regenerated to the SAME letter. The star
    rubric must not reintroduce the same shape with numbers."""
    for card in (a_card(), _card_with_ungraded_pace()):
        _system, user = workout_coach.build_prompt(PROFILE, card)
        assert workout_coach.find_grade_leak({"user": user}) is None, user


@pytest.mark.parametrize(("score", "word"), [
    (5.0, "dead on"), (4.90, "dead on"), (4.89, "on target"),
    (4.25, "on target"), (4.24, "slightly off target"),
    (3.50, "slightly off target"), (3.49, "off target"),
    (2.50, "off target"), (2.49, "well off target"),
    (1.50, "well off target"), (1.49, "missed badly"), (1.0, "missed badly"),
])
def test_star_severity_band_boundaries(score, word):
    assert workout_coach.star_severity(score) == word


def test_star_severity_handles_unrated():
    assert workout_coach.star_severity(None) == "n/a"


def test_star_severity_is_the_one_definition():
    """The same vocabulary renders into the coach's MEMORY block via
    ledger.report_card_facts. Two tables would drift, and the drift would be
    invisible — the prompt and the memory would simply disagree about what a
    given run was called."""
    assert workout_coach.star_severity(4.5) == rc.star_verdict(4.5)


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
    assert set(out) == {"distance", "pace", "hr", "stimulus"}
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
    assert "double your median day" in out["stimulus"]


def test_prompt_stimulus_block_carries_zones_and_training_effect():
    """The facts the STIMULUS paragraph is written from. Aerobic share is the one
    number that makes "this really was easy" checkable rather than asserted — a
    97%-aerobic run and a 30%-aerobic one can share an average HR."""
    card = a_card({"aerobic_te": 2.0, "anaerobic_te": 0.4})
    card["stimulus"]["zones"] = {
        "seconds_by_zone": {1: 372.0, 2: 2490.0, 3: 86.0},
        "total_seconds": 2948.0, "aerobic_pct": 97,
    }
    _system, user = workout_coach.build_prompt(PROFILE, card)
    assert "97% of the run sat in the aerobic zones (Z1-Z2)." in user
    assert "Aerobic training effect: 2.0 on a 0-5 scale." in user
    assert "Anaerobic training effect: 0.4 on a 0-5 scale." in user


def test_prompt_flags_a_higher_than_intended_stimulus():
    """The one stimulus case the read SHOULD push on — it borrows from the next
    session. Everything below the spike ceiling must not be nagged about."""
    card = a_card({"training_load": 500})
    _system, user = workout_coach.build_prompt(PROFILE, card)
    assert "HIGHER than the day intended" in user
    assert "SPIKE" in user


def test_fallback_stimulus_distinguishes_no_history_from_no_load():
    """Two different facts that were being conflated: a load figure the watch
    never recorded, versus one it recorded but nothing comparable to scale it
    against. The second used to claim the watch recorded nothing."""
    no_history = a_card(reference={"mode": "insufficient_data", "n": 1,
                                   "pool": "running"})
    assert no_history["stimulus"]["level"] is None
    out = workout_coach.fallback_read(no_history)["stimulus"]
    assert "Training load 100" in out
    assert "Not enough comparable history" in out

    no_load = a_card({"training_load": None})
    assert (workout_coach.fallback_read(no_load)["stimulus"]
            == "No training-load figure recorded for this run.")


def test_fallback_stimulus_says_a_low_number_is_not_a_shortfall():
    """The template has to carry the 0.40.0 reading too — a failed Claude call
    must not silently revert to scolding a correctly-run easy day."""
    card = a_card({"training_load": 20, "avg_hr": 120}, plan={"type": "easy", "seq": 1})
    assert card["stimulus"]["level"] == "LOW"
    out = workout_coach.fallback_read(card)["stimulus"]
    assert "Low stimulus" in out
    assert "not a shortfall" in out


def test_fallback_is_deterministic():
    card = a_card()
    assert workout_coach.fallback_read(card) == workout_coach.fallback_read(card)


# --- parse_read: four labelled sections or nothing --------------------------

def test_parse_splits_labelled_sections():
    out = workout_coach.parse_read(FOUR_SECTIONS)
    assert out == {
        "distance": "covered the ground.", "pace": "too quick.",
        "hr": "stayed low.", "stimulus": "banked what it should.",
    }


def test_parse_tolerates_model_cosmetics():
    # Blank lines, bold markers, bullets and mixed case are all cosmetic — none
    # change the content, and regenerating over a stray asterisk is wasteful.
    messy = (
        "**DISTANCE:** covered the ground.\n\n"
        "- Pace: too quick.\n\n"
        "  heart rate: stayed low.\n\n"
        "STIMULUS: banked what it should.\n"
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
    "DISTANCE: a\nPACE: b\nHEART RATE: c\nSTIMULUS:",     # stimulus empty
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
    for label in ("DISTANCE:", "PACE:", "HEART RATE:", "STIMULUS:"):
        assert label in system
    assert "Do not name it, do not count it" in system
    assert "never about a score" in system
    # The ban must not demonstrate itself: spelling out "A", "B-", "C+" put
    # four grades into the context alongside the instruction not to say one.
    # The star wording is held to the same rule — it names the concept once and
    # never shows a value.
    assert workout_coach.find_grade_leak({"system": system}) is None
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


def test_upcoming_prescriptions_carry_the_numbers_from_raw_plan_rows():
    """`upcoming_workouts` come straight from plans.get_active_plan(), which
    carries target_distance_m / target_pace_sec_per_km / target_duration_sec.
    Reading only the display keys dropped every number out of this block and
    left the model a bare date and workout type."""
    card = a_card(upcoming_workouts=[{
        "date": "2026-07-22", "type": "tempo", "seq": 1,
        "target_distance_m": 9656.06, "target_pace_sec_per_km": 359.86,
        "target_duration_sec": 2700, "description": "3 mi @ tempo"}])
    _, user = workout_coach.build_prompt(PROFILE, card)
    assert ("2026-07-22: tempo · 6.0 mi · @ 9:39/mi · 45 min · 3 mi @ tempo"
            in user)


def test_upcoming_prescription_prefers_already_converted_fields():
    """A caller that hands over an augmented row is not re-converted."""
    card = a_card(upcoming_workouts=[{
        "date": "2026-07-22", "type": "interval", "distance_mi": 5.0,
        "pace_min_per_mi": "7:30", "target_distance_m": 1.0,
        "target_pace_sec_per_km": 999.0}])
    _, user = workout_coach.build_prompt(PROFILE, card)
    assert "2026-07-22: interval · 5.0 mi · @ 7:30/mi" in user


def test_prompt_omits_the_history_blocks_when_there_is_none():
    _, user = workout_coach.build_prompt(PROFILE, a_card())
    assert "What led into this run" not in user
    assert "What this was setting up for" not in user


def test_markdown_card_renders_four_labelled_paragraphs():
    card = a_card()
    card["coach_read"] = {
        "distance": "You covered the ground.", "pace": "Too quick.",
        "hr": "Stayed low.", "stimulus": "Banked what it should.",
    }
    md = rc.render_markdown(card)
    assert md.index("## Overall:") < md.index("You covered the ground.")
    for label in ("**Distance**", "**Pace**", "**Heart Rate**", "**Stimulus**"):
        assert label in md
    # Distance's paragraph precedes pace's, matching the table's order.
    assert md.index("You covered the ground.") < md.index("Too quick.")
    # The yardstick is stated once, below the table. It came back when the
    # reference pool started excluding rows: which median a run is measured
    # against is now a decision the reader cannot reconstruct from the numbers.
    assert "Graded against your **60-day rolling median**" in md
    assert md.index("| Rating |") < md.index("Graded against your")


def test_markdown_card_without_a_read_opens_on_the_rating():
    md = rc.render_markdown(a_card())
    assert "## Overall:" in md
    assert "**Distance** —" not in md
    # And it states what a 5 means — a star row reads as a review score
    # otherwise, which is a different claim from the one this card makes.
    assert "compliance score" in md


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
    "HEART RATE: Stayed low.\nSTIMULUS: Banked it."
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


# --- the letter-grade guard -------------------------------------------------
# `_GRADE_TONE` forbids naming a letter grade; the model complies ~97% of the
# time (measured 2026-07-22: 3 leaks in 96 paragraphs across 3 real cards). The
# code enforces what the prompt can only ask for.
#
# These two lists ARE the specification of `_GRADE_LEAK`. The false-positive
# list is the important half: a bare "A" is almost always the article, and
# firing on it would throw away a clean read and pay for another generation.
# Extend BOTH before touching the pattern.

_REAL_LEAKS = [
    "F is F. 9:25 best mile against a 6:58 target isn't a miss.",
    "F. Target 6:58/mi, your best mile was 9:25 — over two minutes slow.",
    "B+ on paper, but that's the tell.",
    "You got a B for that.",
    "That's a D- effort and you know it.",
    "Combine that with F-grade pace and the session was a write-off.",
    "2:27 slow, an F, no rounding it up.",
    "112 bpm against a ceiling — the C+ says so.",
]

_LOOKALIKES = [
    "A blown interval session that's also light on load.",
    "81 against a 105 target — you left work on the table.",
    "136 bpm average sits under the 145 floor.",
    "A full minute per mile too hot for a recovery day.",
    "Mile 3 at 14:49 — you were shuffling, not running.",
    "An easy day is supposed to be easy.",
    "Target 6:58/mi, best mile 9:25.",
    "Your 5K pace is not your 10K pace.",
    "A solid negative split, front half to back half.",
]


@pytest.mark.parametrize("text", _REAL_LEAKS)
def test_grade_leak_is_detected(text):
    assert workout_coach.find_grade_leak({"pace": text}) is not None


@pytest.mark.parametrize("text", _LOOKALIKES)
def test_grade_lookalikes_do_not_fire(text):
    assert workout_coach.find_grade_leak({"pace": text}) is None


def test_find_grade_leak_returns_the_offending_token():
    assert workout_coach.find_grade_leak({"hr": "the C+ says so"}) == "C+"


def test_find_grade_leak_scans_every_section():
    sections = {"distance": "clean", "pace": "clean",
                "hr": "clean", "load": "that's a D- effort"}
    assert workout_coach.find_grade_leak(sections) == "D-"


def test_find_grade_leak_clean_read_returns_none():
    assert workout_coach.find_grade_leak(
        {k: "All clean prose, no letters here." for k, _ in workout_coach.READ_SECTIONS}
    ) is None


def test_find_grade_leak_tolerates_empty_sections():
    assert workout_coach.find_grade_leak({"pace": "", "hr": None}) is None


# --- retry-once behaviour ---------------------------------------------------

def _read(pace: str) -> str:
    return (f"DISTANCE: clean\nPACE: {pace}\n"
            "HEART RATE: clean\nSTIMULUS: clean")


def _stub_generate(monkeypatch, texts):
    """Replace generate_read with a scripted sequence; count the calls."""
    calls = {"n": 0}

    async def fake(*_a, **_k):
        calls["n"] += 1
        return texts[min(calls["n"] - 1, len(texts) - 1)]

    monkeypatch.setattr(workout_coach, "generate_read", fake)
    return calls


def test_a_leaked_read_is_regenerated_once(monkeypatch, tmp_path):
    calls = _stub_generate(monkeypatch, [_read("an F, no rounding it up"),
                                         _read("2:27 slower than the ask")])
    sections = asyncio.run(workout_coach.generate_read_cached(
        PROFILE, a_card(), cache_path=tmp_path / "c.json"))
    assert calls["n"] == 2
    assert sections["pace"] == "2:27 slower than the ask"


def test_a_clean_read_is_not_regenerated(monkeypatch, tmp_path):
    calls = _stub_generate(monkeypatch, [_read("2:27 slower than the ask")])
    asyncio.run(workout_coach.generate_read_cached(
        PROFILE, a_card(), cache_path=tmp_path / "c.json"))
    assert calls["n"] == 1


def test_two_leaks_keeps_the_first_and_stops(monkeypatch, tmp_path):
    """One retry, never a loop — a pathological card must not spend unbounded
    time, and the first read is no worse than the second."""
    calls = _stub_generate(monkeypatch, [_read("an F, first"), _read("a D-, second")])
    sections = asyncio.run(workout_coach.generate_read_cached(
        PROFILE, a_card(), cache_path=tmp_path / "c.json"))
    assert calls["n"] == 2
    assert sections["pace"] == "an F, first"


def test_a_failed_retry_keeps_the_first_read(monkeypatch, tmp_path):
    calls = {"n": 0}

    async def fake(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _read("an F, no rounding it up")
        raise RuntimeError("SDK down")

    monkeypatch.setattr(workout_coach, "generate_read", fake)
    sections = asyncio.run(workout_coach.generate_read_cached(
        PROFILE, a_card(), cache_path=tmp_path / "c.json"))
    assert calls["n"] == 2
    assert sections["pace"] == "an F, no rounding it up"


def test_the_clean_retry_is_what_gets_cached(monkeypatch, tmp_path):
    """The cache must hold the read that was RETURNED — otherwise the next
    render replays the leak the retry just paid to remove."""
    cache = tmp_path / "c.json"
    _stub_generate(monkeypatch, [_read("an F, no rounding it up"),
                                 _read("2:27 slower than the ask")])
    asyncio.run(workout_coach.generate_read_cached(PROFILE, a_card(), cache_path=cache))

    calls = _stub_generate(monkeypatch, [_read("should not be called")])
    again = asyncio.run(workout_coach.generate_read_cached(
        PROFILE, a_card(), cache_path=cache))
    assert calls["n"] == 0, "second render should hit the cache"
    assert again["pace"] == "2:27 slower than the ask"


# --- read_cache_key: the ONE key definition ---------------------------------

def test_read_cache_key_matches_the_file_cache_key(tmp_path, monkeypatch):
    """The factored helper and generate_read_cached must hash identically —
    the card_store fast path compares its stored key against the helper's, so
    any byte drift silently disables read reuse forever."""
    async def _gen(profile, card, **k):
        return FOUR_SECTIONS

    monkeypatch.setattr(workout_coach, "generate_read", _gen)
    cache = tmp_path / "cache.json"
    card = a_card()
    asyncio.run(workout_coach.generate_read_cached(
        PROFILE, card, cache_path=cache))
    entry = json.loads(cache.read_text())
    assert entry["key"] == workout_coach.read_cache_key(PROFILE, card)


def test_read_cache_key_model_none_is_the_literal_default():
    """model=None hashes the literal "default", NOT DEFAULT_MODEL — the byte
    layout generate_read_cached has always used."""
    card = a_card()
    assert (workout_coach.read_cache_key(PROFILE, card)
            == workout_coach.read_cache_key(PROFILE, card, model="default"))
    assert (workout_coach.read_cache_key(PROFILE, card)
            != workout_coach.read_cache_key(
                PROFILE, card, model=workout_coach.DEFAULT_MODEL))


def test_read_cache_key_separates_same_day_twin_activities():
    """Two sessions with identical prompts but different activity_ids must not
    share a key — the double-day guard."""
    assert (workout_coach.read_cache_key(PROFILE, a_card())
            != workout_coach.read_cache_key(
                PROFILE, a_card({"activity_id": 2})))


def test_read_cache_key_changes_with_notes_and_memory():
    base = workout_coach.read_cache_key(PROFILE, a_card())
    assert base != workout_coach.read_cache_key(
        PROFILE, a_card(), notes_text="- be nicer about hills")
    assert base != workout_coach.read_cache_key(
        PROFILE, a_card(), memory_text="- missed Tuesday")
