"""Tests for coach tone profiles — load/resolve/override/clamp + import safety,
plus the per-profile expected-outcome scorer wired into the suite."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from local_fitness import db
from local_fitness.agent import coach, prompts

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture
def dbp(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    for env in ("LOCAL_FITNESS_COACH_PROFILE", "LOCAL_FITNESS_COACH_HARSHNESS"):
        monkeypatch.delenv(env, raising=False)
    return p


# --- loading ---------------------------------------------------------------

def test_all_shipped_profiles_load_with_sane_dials():
    for name in coach.PROFILE_NAMES:
        p = coach.load_profile(name)
        assert p.name == name
        assert p.persona.strip()
        assert 0 <= p.harshness <= 10 and 0 <= p.warmth <= 10 and 0 <= p.push <= 10
        assert 0.0 <= p.roast_threshold <= 1.20 and 0.0 <= p.praise_threshold <= 1.20


def test_harsh_block_gating_matches_harshness():
    # adaptive(6)/hardass(9) include the harsh block; neutral(5)/supportive(1) don't
    assert coach.load_profile("adaptive").includes_harsh_block
    assert coach.load_profile("hardass").includes_harsh_block
    assert not coach.load_profile("neutral").includes_harsh_block
    assert not coach.load_profile("supportive").includes_harsh_block


def test_unknown_profile_falls_back_to_adaptive():
    assert coach.load_profile("nope").name == "adaptive"
    assert coach.load_profile("").name == "adaptive"


def test_import_fallback_when_profile_dir_missing(monkeypatch):
    # a missing/broken coach_profiles dir must NOT crash — in-code fallback,
    # and the fallback persona keeps "roast" so the prompt scorer survives.
    monkeypatch.setattr(coach, "_PROFILE_DIR", Path("/nonexistent/coach_profiles"))
    p = coach.load_profile("adaptive")
    assert p.name == "adaptive" and p.persona.strip()
    assert "roast" in p.persona.lower()
    # system_prompt still renders with the fallback
    assert "roast" in prompts.system_prompt("Nate", p).lower()


# --- resolve + overrides ---------------------------------------------------

def test_resolve_default_is_adaptive(dbp):
    assert coach.resolve_coach_profile().name == "adaptive"


def test_resolve_picks_profile_from_setting(dbp):
    db.set_setting("coach_profile", "hardass")
    assert coach.resolve_coach_profile().name == "hardass"


def test_env_selects_profile(dbp, monkeypatch):
    monkeypatch.setenv("LOCAL_FITNESS_COACH_PROFILE", "supportive")
    assert coach.resolve_coach_profile().name == "supportive"


def test_db_overrides_env_for_profile(dbp, monkeypatch):
    monkeypatch.setenv("LOCAL_FITNESS_COACH_PROFILE", "supportive")
    db.set_setting("coach_profile", "hardass")
    assert coach.resolve_coach_profile().name == "hardass"


def test_dial_override_changes_one_dial(dbp):
    db.set_setting("coach_profile", "hardass")
    db.set_setting("coach_harshness", "4")
    p = coach.resolve_coach_profile()
    assert p.name == "hardass"
    assert p.harshness == 4  # overridden
    assert p.warmth == 1     # native hardass value, untouched


def test_dial_override_out_of_range_falls_back(dbp):
    db.set_setting("coach_profile", "hardass")
    db.set_setting("coach_harshness", "99")  # out of [0,10] → native value
    assert coach.resolve_coach_profile().harshness == 9


def test_threshold_override_out_of_range_falls_back(dbp):
    db.set_setting("coach_profile", "supportive")
    db.set_setting("coach_roast_threshold", "5.0")  # out of [0,1.2] → native 0.0
    assert coach.resolve_coach_profile().roast_threshold == 0.0


# --- per-profile expected-outcome scorer (Layer 1, CI-gating) --------------

def test_all_profiles_pass_expected_outcomes():
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    import score_profiles
    checks = score_profiles.build_checks()
    failed = [desc for desc, ok in checks if not ok]
    assert not failed, f"profiles failed expected-outcome checks: {failed}"
