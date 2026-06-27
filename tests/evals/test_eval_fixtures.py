"""Deterministic property checks for the golden brief-eval fixtures.

These are pure pytest — no model, no flake. They prove the fabricated fixtures
are (a) reproducible and (b) actually drive the brief composer into the intended
trigger states (verified through ``assemble_status`` / ``plans``, the same reads
the composer makes), so a baseline capture and the Phase-3 shadow-run exercise
the branches the prompt can take.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from local_fitness import db, plans
from local_fitness.agent.status import assemble_status

from eval_fixtures import SCENARIOS, build_fixture_db

_FIXED = date(2026, 6, 26)


def _dump(path) -> dict[str, list]:
    """Ordered row contents of the data tables — the determinism fingerprint."""
    with db.connect(path) as conn:
        return {
            "daily": [tuple(r) for r in conn.execute(
                "SELECT date, rhr, sleep_seconds, sleep_score, avg_stress, "
                "body_battery_max, body_battery_min, steps FROM daily_metrics "
                "ORDER BY date").fetchall()],
            "baselines": [tuple(r) for r in conn.execute(
                "SELECT date, ctl, atl, tsb, rhr_60day_mean FROM baselines "
                "ORDER BY date").fetchall()],
            "activities": [tuple(r) for r in conn.execute(
                "SELECT activity_id, date, activity_type, aerobic_te, "
                "distance_meters, training_load FROM activities "
                "ORDER BY activity_id").fetchall()],
            "settings": [tuple(r) for r in conn.execute(
                "SELECT key, value FROM settings ORDER BY key").fetchall()],
        }


@pytest.fixture
def at(tmp_path, monkeypatch):
    """Point the process DB pointer at a freshly-built fixture, return a builder."""
    def _build(scenario, *, today=_FIXED):
        p = build_fixture_db(scenario, tmp_path / scenario / "fitness.db", today=today)
        monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
        monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "notes.md"))
        return p
    return _build


def test_every_scenario_builds_nonempty(tmp_path):
    for s in SCENARIOS:
        p = build_fixture_db(s, tmp_path / s / "fitness.db", today=_FIXED)
        assert p.exists() and p.stat().st_size > 0


def test_unknown_scenario_raises(tmp_path):
    with pytest.raises(ValueError, match="unknown scenario"):
        build_fixture_db("bogus", tmp_path / "x.db", today=_FIXED)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_build_is_deterministic(scenario, tmp_path):
    """Same (scenario, today) → byte-identical row contents across two builds."""
    a = build_fixture_db(scenario, tmp_path / "a" / "fitness.db", today=_FIXED)
    b = build_fixture_db(scenario, tmp_path / "b" / "fitness.db", today=_FIXED)
    assert _dump(a) == _dump(b)


def test_today_is_injected(tmp_path):
    """The most-recent daily_metrics row lands on the injected ``today``."""
    other = date(2025, 1, 15)
    p = build_fixture_db("green_light", tmp_path / "fitness.db", today=other)
    with db.connect(p) as conn:
        latest = conn.execute("SELECT MAX(date) FROM daily_metrics").fetchone()[0]
    assert latest == other.isoformat()


def test_green_light_is_a_push_day(at):
    at("green_light")
    s = assemble_status(_FIXED.isoformat())
    tl = s["training_load"]
    assert (tl["ctl"], tl["atl"], tl["tsb"]) == (14.2, 11.0, 3.2)  # positive freshness
    rhr = next(m for m in s["metrics"] if m["metric"] == "rhr")
    assert rhr["value"] == 48 and rhr["delta_pct"] < 0  # below the 53 baseline
    assert len(s["recent_workouts"]) == 4  # recent runs present → CTL-climbing story


def test_fatigued_recovery_is_red(at):
    at("fatigued_recovery")
    s = assemble_status(_FIXED.isoformat())
    tl = s["training_load"]
    assert tl["tsb"] == -22.0 and tl["interpretation"] == "very fatigued"
    rhr = next(m for m in s["metrics"] if m["metric"] == "rhr")
    assert rhr["value"] == 58 and rhr["delta_pct"] > 0  # elevated over baseline


def test_sliding_fitness_has_a_run_gap(at):
    p = at("sliding_fitness")
    # Last run is 6 days back → fires the "5+ days since last run" conditioning
    # trigger; CTL (9.1) sits well under the ~13 it used to hold.
    with db.connect(p) as conn:
        last = conn.execute("SELECT MAX(date) FROM activities").fetchone()[0]
    assert last == (_FIXED - timedelta(days=6)).isoformat()
    s = assemble_status(_FIXED.isoformat())
    assert s["training_load"]["ctl"] == 9.1


def test_missed_steps_under_goal(at):
    p = at("missed_steps")
    with db.connect(p) as conn:
        yest = conn.execute(
            "SELECT steps FROM daily_metrics WHERE date = ?",
            ((_FIXED - timedelta(days=1)).isoformat(),),
        ).fetchone()[0]
    assert yest < 10000  # yesterday well under the 10k goal


def test_sparse_does_not_raise_and_has_no_load(at):
    at("sparse")
    s = assemble_status(_FIXED.isoformat())  # must not raise on the near-empty DB
    tl = s["training_load"]
    assert tl["ctl"] is None and tl["interpretation"] == "no training-load data yet"
    assert s["recent_workouts"] == []


def test_taper_plan_is_active_with_session_today(tmp_path):
    p = build_fixture_db("taper_plan", tmp_path / "fitness.db", today=_FIXED)
    active = plans.get_active_plan(p)
    assert active is not None
    assert active["race_date"] == (_FIXED + timedelta(days=10)).isoformat()
    with db.connect(p) as conn:
        today_row = conn.execute(
            "SELECT type, target_distance_m FROM plan_workouts WHERE date = ?",
            (_FIXED.isoformat(),),
        ).fetchone()
    assert today_row is not None
    assert today_row["type"] == "easy" and today_row["target_distance_m"] == 5000.0


def test_only_taper_plan_has_an_active_plan(tmp_path):
    """The plan trigger must be isolated to taper_plan — others leave no plan so
    the composer can't accidentally fold plan content into them."""
    for s in SCENARIOS:
        p = build_fixture_db(s, tmp_path / s / "fitness.db", today=_FIXED)
        is_active = plans.get_active_plan(p) is not None
        assert is_active == (s == "taper_plan"), s
