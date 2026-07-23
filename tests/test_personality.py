"""The tunable personality spec — parse fail-open, patch validation/apply,
rendering, and the resolve-time attach/mismatch/kill-switch rules."""
from __future__ import annotations

import json

from local_fitness import db
from local_fitness.agent import coach, personality, prompts


def _spec(**kw) -> personality.PersonalitySpec:
    base = {"base_profile": "hardass", "identity": "You are the mirror."}
    base.update(kw)
    return personality.PersonalitySpec(**base)


# --- parse_spec (fail-open) -------------------------------------------------


def test_parse_malformed_json_returns_none_never_raises():
    assert personality.parse_spec("{not json") is None
    assert personality.parse_spec('"a string"') is None
    assert personality.parse_spec('{"identity": ""}') is None
    assert personality.parse_spec(None) is None
    assert personality.parse_spec("") is None


def test_parse_oversized_spec_is_ignored():
    huge = json.dumps({"identity": "x" * (personality.SPEC_MAX_BYTES + 1)})
    assert personality.parse_spec(huge) is None


def test_parse_round_trips_a_valid_spec():
    spec = _spec(catchphrases=("The log doesn't lie.",),
                 principles=("Comfort is the enemy.",),
                 never_do=("Never mock injury.",),
                 intensity={"sleep": "low"}, updated_at="2026-07-23T08:00:00")
    parsed = personality.parse_spec(personality.spec_to_json(spec))
    assert parsed == spec


def test_parse_drops_bad_intensity_entries_but_keeps_the_spec():
    raw = json.dumps({
        "base_profile": "hardass", "identity": "prose",
        "intensity": {"sleep": "low", "BAD SLUG": "high", "recovery": "nope"},
    })
    parsed = personality.parse_spec(raw)
    assert parsed is not None
    assert dict(parsed.intensity) == {"sleep": "low"}


def test_kill_switch_disables_parsing(monkeypatch):
    raw = personality.spec_to_json(_spec())
    monkeypatch.setenv("LOCAL_FITNESS_COACH_SPEC", "0")
    assert personality.parse_spec(raw) is None
    monkeypatch.setenv("LOCAL_FITNESS_COACH_SPEC", "1")
    assert personality.parse_spec(raw) is not None


# --- validate_patch / apply_patch -------------------------------------------


def test_validate_rejects_unknown_fields_and_bad_values():
    clean, errors = personality.validate_patch({
        "identity": "  ok prose  ",
        "sock_color": "red",
        "add_catchphrase": "x" * 121,
        "set_intensity": {"sleep": "loud", "BAD!": "low", "praise": "off"},
    })
    assert clean["identity"] == "ok prose"
    assert clean["set_intensity"] == {"praise": "off"}
    assert any("unknown field 'sock_color'" in e for e in errors)
    assert any("too long" in e for e in errors)
    assert any("bad intensity level 'loud'" in e for e in errors)
    assert any("bad intensity topic" in e for e in errors)


def test_apply_patch_list_ops_are_idempotent_and_case_insensitive():
    spec = _spec(catchphrases=("Earn the rest day.",))
    added = personality.apply_patch(spec, {"add_catchphrase": "earn the rest day."})
    assert added.catchphrases == ("Earn the rest day.",)  # no dup
    removed = personality.apply_patch(spec, {"remove_catchphrase": "EARN THE REST DAY."})
    assert removed.catchphrases == ()


def test_apply_patch_medium_clears_an_intensity_override():
    spec = _spec(intensity={"sleep": "brutal"})
    out = personality.apply_patch(spec, {"set_intensity": {"sleep": "medium"}})
    assert dict(out.intensity) == {}
    out = personality.apply_patch(spec, {"set_intensity": {"sleep": "off"}})
    assert dict(out.intensity) == {"sleep": "off"}


def test_seed_then_patch_round_trip():
    profile = coach.load_profile("hardass")
    seed = personality.seed_from_profile(profile)
    assert seed.identity == profile.persona
    assert seed.base_profile == "hardass"
    tuned = personality.apply_patch(seed, {
        "identity": "New identity prose.",
        "add_never_do": "Never lecture about sleep.",
    })
    assert tuned.identity == "New identity prose."
    assert tuned.never_do == ("Never lecture about sleep.",)


# --- render_spec_persona ----------------------------------------------------


def test_render_includes_every_tuned_section_with_instructions():
    spec = _spec(
        principles=("The log doesn't lie.",),
        catchphrases=("Earn the rest day.",),
        never_do=("Never mock injury.",),
        intensity={"step_goal_nagging": "low", "quality_day_misses": "brutal"},
    )
    text = personality.render_spec_persona(spec)
    assert text.startswith("You are the mirror.")
    assert '## Signature lines' in text and '"Earn the rest day."' in text
    assert "## Principles" in text
    assert "## Never do" in text
    assert "- step_goal_nagging: low — mention only when it genuinely changes" \
        in text
    assert "- quality_day_misses: brutal — maximum pressure" in text


def test_render_untuned_seed_is_exactly_the_profile_prose():
    profile = coach.load_profile("neutral")
    seed = personality.seed_from_profile(profile)
    assert personality.render_spec_persona(seed) == profile.persona


# --- resolve-time behavior --------------------------------------------------


def test_resolve_attaches_a_matching_spec(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    db.set_setting("coach_profile", "hardass")
    db.set_setting(personality.SPEC_KEY, personality.spec_to_json(
        _spec(identity="Tuned voice prose.")))
    profile = coach.resolve_coach_profile()
    assert profile.spec is not None
    assert profile.effective_persona == "Tuned voice prose."
    # ...and the voice block speaks it on every surface.
    block = prompts.coach_voice_block("Alex", profile)
    assert "Tuned voice prose." in block
    assert "tuned conversationally" in block


def test_resolve_ignores_a_mismatched_spec(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    db.set_setting("coach_profile", "supportive")
    db.set_setting(personality.SPEC_KEY, personality.spec_to_json(
        _spec(identity="Hardass tuning.")))  # base_profile=hardass
    profile = coach.resolve_coach_profile()
    assert profile.spec is None
    assert profile.effective_persona == profile.persona
    # The spec survives for a switch back.
    assert db.get_setting(personality.SPEC_KEY) is not None


def test_resolve_respects_the_spec_kill_switch(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    db.set_setting(personality.SPEC_KEY, personality.spec_to_json(
        _spec(identity="Tuned.")))
    monkeypatch.setenv("LOCAL_FITNESS_COACH_SPEC", "0")
    assert coach.resolve_coach_profile().spec is None


def test_untuned_profile_renders_byte_identically_to_before():
    """Virtual seeding: no stored spec → effective_persona IS the file prose,
    so 0.31.0 changes nothing for an untuned clone."""
    for name in sorted(coach.PROFILE_NAMES):
        profile = coach.load_profile(name)
        assert profile.spec is None
        assert profile.effective_persona == profile.persona
