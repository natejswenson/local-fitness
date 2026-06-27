"""Tests for agent/brief_planner.py — the deterministic brief planner.

Three layers, all pure (no model):
  1. trigger predicates — each fires EXACTLY on its documented condition
  2. suggest_tone — every per-mandate tone branch reproduced
  3. assemble_brief_context — deterministic, priority-ordered, mandates always present

The planner is the tested HALF of the agent/code separation; these assert the
real transformed values (tones, fired triggers, ordering), not stand-ins.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from local_fitness import db
from local_fitness.agent import brief_planner as bp
from local_fitness.agent.coach import CoachProfile
from local_fitness.agent.schemas import BriefContext, GroundedValue

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from eval_fixtures import build_fixture_db  # noqa: E402

_FIXED = date(2026, 6, 26)


def _profile(harshness: int) -> CoachProfile:
    return CoachProfile(name="t", harshness=harshness, warmth=5, push=5,
                        roast_threshold=0.85, praise_threshold=0.95, persona="x")


def _gv(name, value):
    return GroundedValue(name=name, value=float(value), unit="none", display=str(value))


# === 1. trigger predicates ================================================

@pytest.mark.parametrize("pct,fires", [(6.0, True), (-6.0, True), (5.0, False),
                                       (-4.9, False), (None, False)])
def test_ctl_shifted(pct, fires):
    assert bp.ctl_shifted(pct) is fires


@pytest.mark.parametrize("a,b,fires", [(2, 6, True), (6, 2, True), (3, 0, True),
                                       (5, 4, False), (2, 2, False)])
def test_run_count_shifted(a, b, fires):
    assert bp.run_count_shifted(a, b) is fires


@pytest.mark.parametrize("te,fires", [((0.5, 0.7, 0.9), True), ((0.5, 0.7), False),
                                      ((0.5, 0.7, 1.2), False), ((), False)])
def test_te_collapsing(te, fires):
    assert bp.te_collapsing(te) is fires


@pytest.mark.parametrize("days,fires", [(5, True), (9, True), (None, True),
                                        (4, False), (0, False)])
def test_long_run_absence(days, fires):
    assert bp.long_run_absence(days) is fires


def test_conditioning_fires_is_an_or():
    # Exactly one sub-condition true → fires.
    assert bp.conditioning_fires(bp.Signals(days_since_last_run=6, runs_14d=2, runs_prior_14d=2))
    # All false: recent run, equal counts, no ctl/te signal.
    assert not bp.conditioning_fires(
        bp.Signals(days_since_last_run=1, runs_14d=4, runs_prior_14d=4,
                   ctl_pct_change_14d=0.0, recent_te=(2.0, 2.0, 2.0)))


@pytest.mark.parametrize("rhr,mean,days,fires", [
    (56, 52, 3, True),   # +4 bpm, 3 days
    (56, 52, 2, False),  # elevated but only 2 days
    (53, 52, 4, False),  # only +1 bpm
    (None, 52, 4, False),
    (56, None, 4, False),
])
def test_rhr_elevated(rhr, mean, days, fires):
    assert bp.rhr_elevated(rhr, mean, days) is fires


@pytest.mark.parametrize("rhr,mean,score,stress,green", [
    (48, 53, 85, 18, True),
    (53, 53, 85, 18, False),   # at baseline, not below
    (48, 53, 60, 18, False),   # sleep score too low
    (48, 53, 85, 45, False),   # stress too high
    (48, 53, None, None, True), # missing sleep/stress → not disqualifying
])
def test_rhr_green(rhr, mean, score, stress, green):
    assert bp.rhr_green(rhr, mean, score, stress) is green


@pytest.mark.parametrize("today_s,base_s,score,poor", [
    (None, None, 58, True),         # score < 65
    (19800, 27000, 80, True),       # 2h short of average
    (26000, 27000, 80, False),      # ~17m short, fine score
    (None, None, 70, False),
])
def test_sleep_poor(today_s, base_s, score, poor):
    assert bp.sleep_poor(today_s, base_s, score) is poor


@pytest.mark.parametrize("nights,stress,low", [(3, 20, True), (2, 45, True),
                                               (2, 20, False), (0, None, False)])
def test_bb_or_stress_low(nights, stress, low):
    assert bp.bb_or_stress_low(nights, stress) is low


def test_recovery_anomaly():
    assert bp.recovery_anomaly(({"date": "2026-06-20"},)) is True
    assert bp.recovery_anomaly(()) is False


def test_recovery_fires_and_all_green():
    fatigued = bp.Signals(rhr_today=58, rhr_baseline_mean=52, rhr_days_elevated=4,
                          sleep_score_today=58)
    assert bp.recovery_fires(fatigued) and not bp.recovery_all_green(fatigued)
    green = bp.Signals(rhr_today=48, rhr_baseline_mean=53, sleep_score_today=85,
                       stress_7d_avg=18)
    # rhr_green makes recovery "fire", but with no reds it's all-green → rolled in.
    assert bp.recovery_fires(green) and bp.recovery_all_green(green)
    flat = bp.Signals(rhr_today=52, rhr_baseline_mean=52)
    assert not bp.recovery_fires(flat) and not bp.recovery_all_green(flat)


# === 2. suggest_tone — every per-mandate branch ===========================

def test_workout_tone_branches():
    p = _profile(6)
    assert bp.suggest_tone("workout", [_gv("recovery_red", 1)], p) == "caution"
    assert bp.suggest_tone("workout", [_gv("tsb", -25)], p) == "caution"
    assert bp.suggest_tone("workout", [_gv("ctl_pct_change_14d", -12),
                                       _gv("days_since_last_run", 6)], p) == "critical"
    assert bp.suggest_tone("workout", [_gv("tsb", 8)], p) == "positive"
    assert bp.suggest_tone("workout", [_gv("rhr_green", 1)], p) == "positive"
    assert bp.suggest_tone("workout", [_gv("tsb", 1)], p) == "neutral"


def test_conditioning_tone_branches():
    p = _profile(6)
    assert bp.suggest_tone("conditioning", [_gv("days_since_last_run", 6)], p) == "critical"
    assert bp.suggest_tone("conditioning", [_gv("ctl_pct_change_14d", -12),
                                            _gv("days_since_last_run", 1)], p) == "critical"
    assert bp.suggest_tone("conditioning", [_gv("ctl_pct_change_14d", 12),
                                            _gv("days_since_last_run", 1)], p) == "positive"
    assert bp.suggest_tone("conditioning", [_gv("ctl_pct_change_14d", 1),
                                            _gv("days_since_last_run", 1)], p) == "neutral"


def test_recovery_tone_branches():
    p = _profile(6)
    crit = [_gv("rhr_delta_bpm", 6), _gv("rhr_days_elevated", 4), _gv("sleep_score", 58)]
    assert bp.suggest_tone("recovery", crit, p) == "critical"
    caution = [_gv("rhr_delta_bpm", 4), _gv("rhr_days_elevated", 4)]
    assert bp.suggest_tone("recovery", caution, p) == "caution"
    positive = [_gv("rhr_delta_bpm", -4)]
    assert bp.suggest_tone("recovery", positive, p) == "positive"
    neutral = [_gv("rhr_delta_bpm", 1)]
    assert bp.suggest_tone("recovery", neutral, p) == "neutral"


def test_steps_tone_branches_and_harsh_gate():
    harsh, soft = _profile(9), _profile(1)
    over = [_gv("frac_of_goal", 1.2), _gv("avg_frac_of_goal", 1.1)]
    assert bp.suggest_tone("steps", over, harsh) == "positive"
    slipping = [_gv("frac_of_goal", 1.1), _gv("avg_frac_of_goal", 0.8)]
    assert bp.suggest_tone("steps", slipping, harsh) == "caution"
    missed = [_gv("frac_of_goal", 0.4), _gv("avg_frac_of_goal", 0.5)]
    assert bp.suggest_tone("steps", missed, harsh) == "critical"   # harshness ≥ 6
    assert bp.suggest_tone("steps", missed, soft) == "caution"     # softer profile


def test_suggest_tone_unknown_category_is_neutral():
    assert bp.suggest_tone("mystery", [], _profile(6)) == "neutral"


# === 3. assemble_brief_context ============================================

def _build(scenario, tmp_path):
    p = build_fixture_db(scenario, tmp_path / scenario / "fitness.db", today=_FIXED)
    return p


def test_assemble_is_deterministic(tmp_path):
    p = _build("fatigued_recovery", tmp_path)
    a = bp.assemble_brief_context(db_path=p, today=_FIXED.isoformat())
    b = bp.assemble_brief_context(db_path=p, today=_FIXED.isoformat())
    assert a.model_dump() == b.model_dump()
    assert isinstance(a, BriefContext)


def test_mandates_always_present_and_priority_ordered(tmp_path):
    for scenario in ("green_light", "sparse", "fatigued_recovery", "taper_plan"):
        p = _build(scenario, tmp_path)
        ctx = bp.assemble_brief_context(db_path=p, today=_FIXED.isoformat())
        cats = [c.category for c in ctx.candidates]
        assert cats[0] == "workout" and "steps" in cats   # workout leads, steps present
        ranks = [bp._PRIORITY[c] for c in cats]
        assert ranks == sorted(ranks)                     # priority-ordered


def test_fatigued_has_critical_recovery_card(tmp_path):
    p = _build("fatigued_recovery", tmp_path)
    ctx = bp.assemble_brief_context(db_path=p, today=_FIXED.isoformat())
    rec = next(c for c in ctx.candidates if c.category == "recovery")
    assert rec.suggested_tone in ("caution", "critical")
    assert "rhr_elevated" in rec.fired_triggers
    workout = next(c for c in ctx.candidates if c.category == "workout")
    assert workout.suggested_tone == "caution"  # red flags → ease off


def test_green_light_rolls_recovery_into_workout(tmp_path):
    p = _build("green_light", tmp_path)
    ctx = bp.assemble_brief_context(db_path=p, today=_FIXED.isoformat())
    cats = [c.category for c in ctx.candidates]
    assert "recovery" not in cats   # all-green → no standalone recovery card
    workout = next(c for c in ctx.candidates if c.category == "workout")
    assert workout.suggested_tone == "positive"


def test_taper_plan_folds_plan_and_flags_race_week(tmp_path):
    p = _build("taper_plan", tmp_path)
    ctx = bp.assemble_brief_context(db_path=p, today=_FIXED.isoformat())
    assert ctx.plan_today is not None and ctx.plan_today["active"] is True
    assert ctx.days_to_race == 10
    workout = next(c for c in ctx.candidates if c.category == "workout")
    assert "active_plan" in workout.fired_triggers
    assert any(c.category == "wildcard" and "race_week" in c.fired_triggers
               for c in ctx.candidates)


def test_payload_groundedvalues_present(tmp_path):
    p = _build("fatigued_recovery", tmp_path)
    ctx = bp.assemble_brief_context(db_path=p, today=_FIXED.isoformat())
    names = {g.name for g in ctx.snapshot}
    assert {"rhr", "steps"} <= names                  # snapshot carries citable numbers
    tl = {g.name for g in ctx.training_load}
    assert tl == {"ctl", "atl", "tsb"}
    assert ctx.step_goal == 10000


def test_snapshot_exposes_baseline_reference_values(tmp_path):
    # Baselines must be citable so the toolless generator quotes the REAL "52
    # baseline" instead of deriving it (which grounding can't trace).
    p = _build("fatigued_recovery", tmp_path)
    ctx = bp.assemble_brief_context(db_path=p, today=_FIXED.isoformat())
    by_name = {g.name: g for g in ctx.snapshot}
    assert "rhr_baseline" in by_name
    assert by_name["rhr_baseline"].value == 52.0        # the fixture's rhr_60day_mean
    assert by_name["rhr_baseline"].display == "52 bpm"
    assert "sleep_baseline" in by_name and "stress_baseline" in by_name


def test_empty_db_does_not_raise(tmp_path, monkeypatch):
    p = tmp_path / "empty.db"
    db.init_schema(p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "notes.md"))
    ctx = bp.assemble_brief_context(db_path=p, today=_FIXED.isoformat())
    cats = [c.category for c in ctx.candidates]
    assert "workout" in cats and "steps" in cats      # mandates survive empty DB


def test_continuity_extracts_recent_headlines(tmp_path):
    p = _build("sparse", tmp_path)
    briefs = [{"takeaways": [{"headline": "Yesterday: easy 5k"}]},
              {"takeaways": [{"headline": "Two days ago: rest"}]}]
    ctx = bp.assemble_brief_context(db_path=p, today=_FIXED.isoformat(), recent_briefs=briefs)
    assert ctx.continuity == ["Yesterday: easy 5k", "Two days ago: rest"]


def test_conditioning_candidate_none_when_quiet():
    quiet = bp.Signals(days_since_last_run=1, runs_14d=4, runs_prior_14d=4,
                       ctl_pct_change_14d=0.0, recent_te=(2.0, 2.0, 2.0))
    assert bp._conditioning_candidate(quiet, _profile(6)) is None


def test_conditioning_candidate_labels_every_fired_trigger():
    loud = bp.Signals(ctl_pct_change_14d=10.0, runs_14d=1, runs_prior_14d=6,
                      recent_te=(0.5, 0.6, 0.7), days_since_last_run=6)
    c = bp._conditioning_candidate(loud, _profile(6))
    assert set(c.fired_triggers) == {"ctl_shifted", "run_count_shifted",
                                     "te_collapsing", "long_run_absence"}


def test_steps_candidate_flags_avg_slipping():
    sig = bp.Signals(steps_yesterday=11000, steps_7d_avg=8000, step_goal=10000)
    c = bp._steps_candidate(sig, _profile(6))
    assert "avg_slipping" in c.fired_triggers and c.suggested_tone == "caution"


def test_recovery_chart_picks_lead_signal():
    assert bp._recovery_chart(["sleep_poor"]) == "sleep_seconds"
    assert bp._recovery_chart(["bb_or_stress_low"]) == "body_battery_max"
    assert bp._recovery_chart(["rhr_elevated"]) == "rhr"


def test_rhr_anomalies_needs_mean_and_sd():
    assert bp._rhr_anomalies({}, _FIXED, {"rhr_60day_mean": 52, "rhr_60day_sd": None}) == []
    assert bp._rhr_anomalies({}, _FIXED, None) == []


def test_ctl_pct_change_computed_from_baseline_history(tmp_path):
    p = tmp_path / "db.db"
    db.init_schema(p)
    today, ago = _FIXED.isoformat(), (_FIXED - timedelta(days=14)).isoformat()
    with db.connect(p) as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES ('daily_step_goal', '10000')")
        conn.execute("INSERT INTO daily_metrics (date, rhr) VALUES (?, 55)", (today,))
        conn.execute("INSERT INTO baselines (date, ctl) VALUES (?, 12.0)", (today,))
        conn.execute("INSERT INTO baselines (date, ctl) VALUES (?, 10.0)", (ago,))
    ctx = bp.assemble_brief_context(db_path=p, today=today)
    workout = next(c for c in ctx.candidates if c.category == "workout")
    ctl_pct = next(g for g in workout.metrics if g.name == "ctl_pct_change_14d")
    assert ctl_pct.value == 20.0   # (12 - 10) / 10 * 100


def test_non_int_step_goal_defaults_to_10000(tmp_path):
    p = tmp_path / "db.db"
    db.init_schema(p)
    with db.connect(p) as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES ('daily_step_goal', 'abc')")
    ctx = bp.assemble_brief_context(db_path=p, today=_FIXED.isoformat())
    assert ctx.step_goal == 10000


def test_brief_planner_imports_no_claude_sdk():
    """Invariant: the deterministic planner must not import the Claude Agent SDK."""
    src = Path(bp.__file__).read_text()
    assert "claude_agent_sdk" not in src and "from claude" not in src
