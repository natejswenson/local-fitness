"""The report card's verbal read: prompt assembly, caching, fallback, and the
GPA explainer that has to reconstruct the printed number.

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


def a_card(activity=None, **kw):
    return rc.build_card({**ACTIVITY, **(activity or {})}, kw.pop("splits", []),
                         kw.pop("plan", None), kw.pop("reference", REF),
                         kw.pop("context", {}), kw.pop("hr_samples", None))


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
        return "generated read"

    monkeypatch.setattr(workout_coach, "generate_read", _gen)
    card = a_card()
    first = asyncio.run(workout_coach.generate_read_cached(
        PROFILE, card, cache_path=path))
    second = asyncio.run(workout_coach.generate_read_cached(
        PROFILE, card, cache_path=path))
    assert first == second == "generated read"
    assert len(calls) == 1


def test_changed_input_misses_the_cache_and_regenerates(tmp_path, monkeypatch):
    path = tmp_path / "cache.json"
    calls = []

    async def _gen(profile, card, **k):
        calls.append(card["activity"]["activity_id"])
        return f"read for {card['activity']['activity_id']}"

    monkeypatch.setattr(workout_coach, "generate_read", _gen)
    asyncio.run(workout_coach.generate_read_cached(
        PROFILE, a_card(), cache_path=path))
    out = asyncio.run(workout_coach.generate_read_cached(
        PROFILE, a_card({"activity_id": 2}), cache_path=path))
    assert out == "read for 2"
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
        return "fresh read"

    monkeypatch.setattr(workout_coach, "generate_read", _gen)
    assert asyncio.run(workout_coach.generate_read_cached(
        PROFILE, a_card(), cache_path=path)) == "fresh read"
    assert json.loads(path.read_text())["text"] == "fresh read"


def test_cache_write_failure_still_returns_the_generated_text(tmp_path, monkeypatch):
    async def _gen(*a, **k):
        return "fresh read"

    monkeypatch.setattr(workout_coach, "generate_read", _gen)
    # A directory where the cache file should go: write fails, read succeeds.
    bad = tmp_path / "cache.json"
    bad.mkdir()
    assert asyncio.run(workout_coach.generate_read_cached(
        PROFILE, a_card(), cache_path=bad)) == "fresh read"


# --- fallback ---------------------------------------------------------------

def test_fallback_names_the_grade_and_the_weakest_metric():
    # Distance far under expectation and pace far off → D/F territory.
    card = a_card({"distance_meters": 4000, "avg_hr": 150})
    text = workout_coach.fallback_read(card)
    assert card["overall"]["grade"] in text
    assert "Morning Run" in text
    assert "weakest" in text.lower()


def test_fallback_survives_a_card_with_no_grades_at_all():
    # The insufficient-history card carries explicit `"grade": None`, which is
    # what made an earlier `.get("grade", "")` default raise AttributeError.
    card = a_card(reference={"mode": "insufficient_data", "n": 1, "pool": "running"})
    assert all(m.get("grade") is None for m in card["metrics"].values())
    text = workout_coach.fallback_read(card)
    assert "n/a" in text


def test_fallback_is_deterministic():
    card = a_card()
    assert workout_coach.fallback_read(card) == workout_coach.fallback_read(card)


def test_fallback_mentions_significant_hr_drift():
    card = a_card()
    card["splits"] = {"available": True, "unit": "Mile", "rows": [],
                      "hr_drift_pct": 9.2}
    assert "9.2%" in workout_coach.fallback_read(card)


# --- gpa_explainer: the printed number must be reconstructible --------------

def test_explainer_arithmetic_reproduces_the_printed_gpa():
    card = a_card()
    text = rc.gpa_explainer(card)
    gpa = card["overall"]["gpa"]
    assert f"= {gpa:.2f}" in text
    assert f"→ {card['overall']['grade']}" in text
    # Every graded metric is listed with the weight actually applied.
    for label in ("Distance", "Pace", "Avg HR", "Training Load"):
        assert label in text
    assert "30%" in text and "25%" in text and "15%" in text


def test_explainer_states_that_plus_minus_does_not_move_the_number():
    # The card shows B- and B+ next to a GPA that scores both as 3.0; without
    # saying so, a reader doing the arithmetic cannot reproduce the number.
    text = rc.gpa_explainer(a_card())
    assert "does not move the number" in text


def test_explainer_renormalizes_weights_when_a_metric_is_na():
    # Load ungraded → the remaining three must be rescaled to sum to 100%,
    # not printed as the static 30/30/25 that would sum to 85%.
    card = a_card()
    card["metrics"]["load"]["grade"] = None
    card["overall"] = rc.overall_grade(card["metrics"])
    text = rc.gpa_explainer(card)
    assert "Training Load" not in text
    assert "rescaled to 100%" in text
    pcts = [int(p.rstrip("%")) for p in text.split() if p.endswith("%") and p[0].isdigit()]
    assert sum(pcts) == 100


def test_explainer_is_empty_when_nothing_was_graded():
    card = a_card(reference={"mode": "insufficient_data", "n": 1, "pool": "running"})
    assert rc.gpa_explainer(card) == ""


def test_markdown_card_leads_with_the_read_then_the_grade():
    card = a_card()
    card["coach_read"] = "That was a controlled effort and you know it."
    md = rc.render_markdown(card)
    assert card["coach_read"] in md
    # The read comes before the grade heading — a person wants to be told how
    # it went before being shown the table that proves it.
    assert md.index(card["coach_read"]) < md.index("## Overall:")
    assert "GPA is a weighted 4.0 scale" in md


def test_markdown_card_without_a_read_opens_on_the_grade():
    md = rc.render_markdown(a_card())
    assert "## Overall:" in md
    # No stray empty block where the read would have been.
    assert "\n\n\n" not in md
