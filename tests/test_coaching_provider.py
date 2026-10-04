"""Provider routing exercised through each production generator and cache."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from local_fitness.agent import (
    coach,
    codex_model,
    plan_coach,
    reflect,
    report_card,
    workout_coach,
)

_REAL_TEXT = codex_model.generate_codex_text
PROFILE = coach.load_profile("hardass")
WORKOUT = {"type": "easy", "distance_mi": 3.0, "description": "Easy, HR below 140."}
READ = ("DISTANCE: Held the prescribed distance.\nPACE: Kept it easy.\n"
        "HEART RATE: Obeyed the cap.\nSTIMULUS: Recover before the next outing.")


def card():
    return report_card.build_card(
        {"activity_id": 123, "date": "2026-09-01", "activity_name": "Easy run",
         "activity_type": "running", "distance_meters": 5000,
         "duration_seconds": 1800, "avg_pace_sec_per_km": 360, "avg_hr": 120},
        [], None, {"mode": "rolling_60d", "n": 20, "pool": "running",
                   "median_distance_m": 5000, "median_pace_sec_per_km": 360,
                   "median_hr": 125, "median_load": 30},
    )


@pytest.mark.parametrize("surface", ["plan", "workout", "reflect"])
def test_every_surface_uses_codex_without_a_claude_call(monkeypatch, surface):
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_PROVIDER", "codex")
    monkeypatch.setenv("LOCAL_FITNESS_CODEX_MODEL", "gpt-brief")
    monkeypatch.setenv("LOCAL_FITNESS_CODEX_COACH_MODEL", "gpt-coach")
    monkeypatch.setattr(codex_model, "generate_codex_text", _REAL_TEXT)
    output_text = READ if surface == "workout" else "NONE" if surface == "reflect" else "Keep it easy."
    observed = {}

    def run(argv, **kwargs):
        observed.update(argv=argv, **kwargs)
        schema = json.loads(Path(argv[argv.index("--output-schema") + 1]).read_text())
        assert schema["properties"] == {"text": {"type": "string"}}
        assert schema["required"] == ["text"]
        Path(argv[argv.index("--output-last-message") + 1]).write_text(
            json.dumps({"text": output_text}))
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(codex_model.subprocess, "run", run)
    if surface == "plan":
        result = asyncio.run(plan_coach.generate_coaching_line(
            PROFILE, WORKOUT, [], 100, None, "custom", timeout=19))
    elif surface == "workout":
        result = asyncio.run(workout_coach.generate_read(PROFILE, card(), timeout=19))
        assert workout_coach.parse_read(result)["pace"] == "Kept it easy."
    else:
        result = asyncio.run(reflect.generate_reflection(
            PROFILE, {"kind": "daily brief"}, "", [], timeout=19))
        assert reflect.parse_reflection(result) == []
    assert result == output_text
    assert observed["argv"][-3:] == ["--model", "gpt-coach", "-"]
    assert observed["timeout"] == 19
    assert "Do not inspect files" in observed["input"]
    assert "--ignore-user-config" in observed["argv"]


@pytest.mark.parametrize("raw, error", [
    ("not json", "invalid JSON"), ("[]", "invalid text"),
    ('{"text": 3}', "invalid text"), ('{"text": "  "}', "empty text"),
])
def test_invalid_codex_prose_fails_before_caching(monkeypatch, raw, error):
    monkeypatch.setattr(codex_model, "_run_codex", lambda *a, **k: raw)
    with pytest.raises(RuntimeError, match=error):
        _REAL_TEXT("system", "user")


def test_provider_override_and_model_resolution(monkeypatch):
    assert codex_model.coaching_provider() == "claude"
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_PROVIDER", "codex")
    assert codex_model.coaching_provider() == "codex"
    assert codex_model.coaching_model() is None
    monkeypatch.setenv("LOCAL_FITNESS_CODEX_MODEL", "gpt-brief")
    assert codex_model.coaching_model() == "gpt-brief"
    monkeypatch.setenv("LOCAL_FITNESS_CODEX_COACH_MODEL", "gpt-coach")
    assert codex_model.coaching_model() == "gpt-coach"
    assert codex_model.coaching_model("gpt-explicit") == "gpt-explicit"
    monkeypatch.setenv("LOCAL_FITNESS_COACH_PROVIDER", " CLAUDE ")
    assert codex_model.coaching_provider() == "claude"
    monkeypatch.setenv("LOCAL_FITNESS_COACH_PROVIDER", "invalid")
    with pytest.raises(ValueError, match="invalid coaching provider"):
        codex_model.coaching_cache_model()


def test_workout_cache_separates_provider_and_codex_model(monkeypatch):
    sample = card()
    claude = workout_coach.read_cache_key(PROFILE, sample)
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_PROVIDER", "codex")
    codex = workout_coach.read_cache_key(PROFILE, sample)
    monkeypatch.setenv("LOCAL_FITNESS_CODEX_COACH_MODEL", "gpt-other")
    other = workout_coach.read_cache_key(PROFILE, sample)
    assert len({claude, codex, other}) == 3


def test_plan_cache_regenerates_on_provider_and_model_change(monkeypatch, tmp_path):
    calls = []

    async def generate(*args, **kwargs):
        calls.append(codex_model.coaching_cache_model())
        return "generated " + calls[-1]

    monkeypatch.setattr(plan_coach, "generate_coaching_line", generate)

    def cached():
        return asyncio.run(plan_coach.generate_coaching_line_cached(
            PROFILE, WORKOUT, [], 100, None, "custom", cache_path=tmp_path / "cache.json"))

    assert cached() == "generated default"
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_PROVIDER", "codex")
    assert cached() == "generated codex:cli-default"
    monkeypatch.setenv("LOCAL_FITNESS_CODEX_COACH_MODEL", "gpt-other")
    assert cached() == "generated codex:gpt-other"
    assert cached() == "generated codex:gpt-other"
    assert calls == ["default", "codex:cli-default", "codex:gpt-other"]


def test_failed_codex_generation_does_not_create_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_PROVIDER", "codex")

    def failed(*args, **kwargs):
        raise RuntimeError("authentication failed")

    monkeypatch.setattr(codex_model, "generate_codex_text", failed)
    path = tmp_path / "cache.json"
    with pytest.raises(RuntimeError, match="authentication failed"):
        asyncio.run(plan_coach.generate_coaching_line_cached(
            PROFILE, WORKOUT, [], 100, None, "custom", cache_path=path))
    assert not path.exists()
