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
from local_fitness.agent import briefs
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


def test_workouts_14d_carries_actual_runs_not_just_counts(tmp_path):
    # The generator must be able to cite yesterday's concrete run (distance/TE),
    # not just "13 runs in 14 days". fatigued_recovery seeds a 16km run yesterday.
    p = _build("fatigued_recovery", tmp_path)
    ctx = bp.assemble_brief_context(db_path=p, today=_FIXED.isoformat())
    assert ctx.workouts_14d, "workouts_14d must carry the actual workout list"
    latest = ctx.workouts_14d[0]
    assert latest["date"] == (_FIXED - timedelta(days=1)).isoformat()
    assert latest["type"] == "running"
    assert latest["distance_mi"] == pytest.approx(9.94, abs=0.05)  # 16000 m
    assert latest["aerobic_te"] == pytest.approx(4.2, abs=0.05)


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


def test_rhr_anomalies_attaches_sd_distance_and_direction():
    # rhr 60 vs mean 52 / sd 2 -> (60-52)/2 = 4.0 SD above, well past the
    # >2*sd trigger condition; direction "above" (WS1 sd_position attach).
    today = _FIXED
    rows = {today.isoformat(): {"rhr": 60}}
    out = bp._rhr_anomalies(rows, today, {"rhr_60day_mean": 52, "rhr_60day_sd": 2})
    assert len(out) == 1
    assert out[0]["sd_distance"] == 4.0
    assert out[0]["direction"] == "above"


def test_ctl_at_or_before_returns_latest_row_on_or_before_anchor(tmp_path):
    p = tmp_path / "db.db"
    db.init_schema(p)
    with db.connect(p) as conn:
        conn.execute("INSERT INTO baselines (date, ctl) VALUES ('2026-06-01', 8.0)")
        conn.execute("INSERT INTO baselines (date, ctl) VALUES ('2026-06-10', 10.0)")
        conn.execute("INSERT INTO baselines (date, ctl) VALUES ('2026-06-20', 15.0)")
        # No row exactly on the anchor date — the at-or-before lookup must
        # fall back to the latest row strictly before it (06-10), not the
        # nearest-in-either-direction row (06-20).
        assert bp.ctl_at_or_before(conn, "2026-06-15") == 10.0
        # Exact match on the anchor date is used directly.
        assert bp.ctl_at_or_before(conn, "2026-06-10") == 10.0
        # No row on or before the anchor at all -> None.
        assert bp.ctl_at_or_before(conn, "2026-05-01") is None


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


# --- 3c: _hm delegates to units.format_hm -----------------------------------

def test_hm_delegates_to_units_format_hm():
    from local_fitness.agent import units

    for seconds in (27180, 25500, 2700, 0):
        assert bp._hm(seconds) == units.format_hm(seconds)


def test_hm_keeps_its_own_empty_string_on_none_contract():
    # _hm's own boundary keeps ""-on-None (unlike format_hm's None-on-None) —
    # grounding's pool expects a plain string, not None.
    from local_fitness.agent import units

    assert bp._hm(None) == ""
    assert units.format_hm(None) is None


# === 4. run classification is pace-gated, not label-gated ==================
# Garmin files walking-desk sessions as `treadmill_running`. Before this gate
# every walk reset days_since_last_run and inflated runs_14d, so the brief
# could never say "you haven't actually run in a week".
# 560 sec/km = 15:01/mi (a walk); 350 sec/km = 9:23/mi (a run); the ceiling is
# a 13:00 mile (interpret.RUN_PACE_CEILING_SEC_PER_MI).
_WALK_PACE = 560.0
_RUN_PACE = 350.0


@pytest.mark.parametrize("typ,pace,is_run", [
    ("treadmill_running", _WALK_PACE, False),  # THE bug: walking-pad session
    ("treadmill_running", _RUN_PACE, True),
    ("running", _WALK_PACE, False),            # label loses to measurement
    ("running", None, True),                   # paceless -> label fallback
    ("running", 0.0, True),                    # 0 is "no usable pace", not fast
    ("walking", _RUN_PACE, True),              # mislabelled the other way
    ("walking", None, False),                  # paceless walk stays a walk
    ("cycling", 120.0, False),                 # 3:13/mi, but not on foot
    ("indoor_cycling", None, False),
    (None, _RUN_PACE, False),
])
def test_running_gates_on_measured_pace_then_falls_back_to_the_label(typ, pace, is_run):
    assert bp._running({"activity_type": typ, "avg_pace_sec_per_km": pace}) is is_run


def _activity_db(tmp_path, rows, *, today=_FIXED, name="acts.db"):
    """A DB carrying only activities — the run-classifier's whole input.

    ``rows`` are ``(days_ago, activity_type, avg_pace_sec_per_km)``.
    """
    p = tmp_path / name
    db.init_schema(p)
    with db.connect(p) as conn:
        for i, (days_ago, typ, pace) in enumerate(rows, start=1):
            adate = (today - timedelta(days=days_ago)).isoformat()
            conn.execute(
                "INSERT INTO activities (activity_id, date, start_time, activity_type, "
                "activity_name, duration_seconds, distance_meters, avg_pace_sec_per_km, "
                "aerobic_te) VALUES (?, ?, ?, ?, 'Session', 2800, 8000, ?, 2.5)",
                (i, adate, adate + "T07:00:00", typ, pace),
            )
    return p


def _signals(tmp_path, rows, *, today=_FIXED, name="acts.db"):
    p = _activity_db(tmp_path, rows, today=today, name=name)
    with db.connect(p) as conn:
        return bp._compute_signals(conn, today.isoformat(), None, 10000, None, None)


def test_walking_pad_sessions_do_not_count_as_runs_or_reset_the_gap(tmp_path):
    # Six walking-pad sessions labelled `treadmill_running` (one a day for the
    # last six days) and one real run eight days back. Label-only classifying
    # read this as "ran yesterday, 7 runs in 14 days"; it is one run, eight
    # days ago — which is exactly what fires long_run_absence.
    sig = _signals(tmp_path, [(d, "treadmill_running", _WALK_PACE) for d in range(1, 7)]
                   + [(8, "running", _RUN_PACE)])
    assert sig.days_since_last_run == 8
    assert sig.runs_14d == 1
    assert sig.runs_prior_14d == 0
    assert bp.long_run_absence(sig.days_since_last_run) is True


def test_walking_pad_sessions_are_kept_out_of_recent_te(tmp_path):
    # recent_te reads the last 5 RUNS. With walks admitted it was the walks' TE
    # (2.5 each) and te_collapsing could never fire; with them excluded the one
    # real run's TE is the only entry.
    sig = _signals(tmp_path, [(d, "treadmill_running", _WALK_PACE) for d in range(1, 6)]
                   + [(7, "running", _RUN_PACE)])
    assert sig.recent_te == (2.5,)


def test_paceless_rows_fall_back_to_the_label_and_still_count(tmp_path):
    # A manual entry / bad sync has no pace. It must not vanish from the run
    # history — the label is all we have, so it is what decides.
    sig = _signals(tmp_path, [(2, "running", None), (4, "walking", None)])
    assert sig.days_since_last_run == 2
    assert sig.runs_14d == 1


def test_a_fast_session_mislabelled_walking_counts_as_a_run(tmp_path):
    sig = _signals(tmp_path, [(3, "walking", _RUN_PACE)])
    assert sig.days_since_last_run == 3
    assert sig.runs_14d == 1


def test_a_bike_ride_is_never_a_run_however_fast(tmp_path):
    # 120 sec/km is a 3:13 mile — well inside the run ceiling. The pace gate
    # answers run-vs-walk, not foot-vs-wheel, so on-foot has to be checked
    # first or a 30 km ride reads as the week's fastest run.
    sig = _signals(tmp_path, [(1, "cycling", 120.0)])
    assert sig.days_since_last_run is None
    assert sig.runs_14d == 0


def test_signals_query_selects_pace_so_the_gate_is_not_a_silent_no_op(tmp_path):
    # Regression net for the failure mode that shipped once in plans.py: drop
    # avg_pace_sec_per_km from the SELECT and every row falls back to the label,
    # leaving the gate green but inert. Asserted end-to-end through the public
    # entry point, so the column has to survive the real query.
    p = _activity_db(tmp_path, [(1, "treadmill_running", _WALK_PACE),
                                (9, "running", _RUN_PACE)], name="e2e.db")
    ctx = bp.assemble_brief_context(db_path=p, today=_FIXED.isoformat())
    workout = next(c for c in ctx.candidates if c.category == "workout")
    days_since = next(g for g in workout.metrics if g.name == "days_since_last_run")
    assert days_since.value == 9.0 and days_since.display == "9"
    conditioning = next(c for c in ctx.candidates if c.category == "conditioning")
    assert "long_run_absence" in conditioning.fired_triggers


# === 5. freshness + continuity fields ======================================

def _stale_db(tmp_path, *, frontier_days_ago: int, tsb: float | None):
    """A DB whose data frontier and baselines row both stopped ``n`` days ago."""
    p = tmp_path / "stale.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    db.init_schema(p)
    frontier = (_FIXED - timedelta(days=frontier_days_ago)).isoformat()
    with db.connect(p) as conn:
        conn.execute("INSERT INTO daily_metrics (date, rhr, steps) VALUES (?, 54, 9000)",
                     (frontier,))
        conn.execute(
            "INSERT INTO baselines (date, rhr_60day_mean, ctl, atl, tsb) "
            "VALUES (?, 52.0, 40.0, 55.0, ?)", (frontier, tsb))
    return p


def test_context_carries_data_frontier_baseline_staleness_and_tsb_zone(tmp_path):
    p = _stale_db(tmp_path, frontier_days_ago=3, tsb=-25.0)
    ctx = bp.assemble_brief_context(db_path=p, today=_FIXED.isoformat())
    assert ctx.data_frontier == "2026-06-23"          # _FIXED (06-26) minus 3
    assert ctx.baseline_stale_days == 3
    assert ctx.tsb_zone == "very fatigued"            # tsb -25 < TSB_VERY_FATIGUED


def test_tsb_zone_reads_the_zone_not_the_none_sentence(tmp_path):
    # interpret.tsb_zone(None) returns the sentence "no training-load data yet".
    # That is prose, not a zone label, so an absent TSB leaves the field None
    # and exclude_none keeps it out of the generator's prompt entirely.
    p = _stale_db(tmp_path, frontier_days_ago=0, tsb=None)
    ctx = bp.assemble_brief_context(db_path=p, today=_FIXED.isoformat())
    assert ctx.tsb_zone is None
    assert ctx.baseline_stale_days == 0

    fresh = _stale_db(tmp_path / "b", frontier_days_ago=0, tsb=8.0)
    assert bp.assemble_brief_context(db_path=fresh,
                                     today=_FIXED.isoformat()).tsb_zone == "fresh"


def test_empty_db_leaves_every_freshness_field_none(tmp_path, monkeypatch):
    p = tmp_path / "empty.db"
    db.init_schema(p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "notes.md"))
    monkeypatch.setattr(briefs, "DEFAULT_BRIEFINGS_DIR", tmp_path / "no-briefings")
    ctx = bp.assemble_brief_context(db_path=p, today=_FIXED.isoformat())
    assert ctx.data_frontier is None
    assert ctx.baseline_stale_days is None
    assert ctx.tsb_zone is None
    assert ctx.brief_stale_days is None


def test_brief_stale_days_counts_days_since_the_newest_saved_brief(tmp_path, monkeypatch):
    # The orphaned-sync signal: the pull advanced but no brief saved. A brief
    # dated two days before `today` reads 2, not 0 and not None.
    out = tmp_path / "briefings"
    out.mkdir()
    for offset in (2, 5):
        d = (_FIXED - timedelta(days=offset)).isoformat()
        (out / f"{d}.json").write_text('{"takeaways": []}', encoding="utf-8")
    monkeypatch.setattr(briefs, "DEFAULT_BRIEFINGS_DIR", out)
    p = _stale_db(tmp_path, frontier_days_ago=0, tsb=-2.0)
    ctx = bp.assemble_brief_context(db_path=p, today=_FIXED.isoformat())
    assert ctx.brief_stale_days == 2


def test_assemble_brief_context_still_opens_exactly_one_connection(tmp_path, monkeypatch):
    """The freshness fields must ride the connection already open. Duplicated
    from tests/test_perf_benchmarks.py deliberately: that file's copy runs on
    the synthetic perf fixture, this one guards the fields added here."""
    p = _build("fatigued_recovery", tmp_path)
    opens = {"n": 0}
    real_connect = db.connect

    def counting_connect(*a, **k):
        opens["n"] += 1
        return real_connect(*a, **k)

    monkeypatch.setattr(db, "connect", counting_connect)
    ctx = bp.assemble_brief_context(db_path=p, today=_FIXED.isoformat())
    assert opens["n"] == 1
    assert ctx.data_frontier is not None   # the added read actually ran


def test_new_context_fields_round_trip_and_default_to_none():
    """Stored eval fixtures predate these fields; the defaults are what keeps
    baseline.json validating, and exclude_none is what keeps them out of the
    V2 prompt until they are populated."""
    bare = BriefContext(date="2026-06-26", user_name="Nate", candidates=[])
    assert (bare.data_frontier, bare.baseline_stale_days,
            bare.brief_stale_days, bare.tsb_zone) == (None, None, None, None)
    assert "tsb_zone" not in bare.model_dump_json(exclude_none=True)

    full = BriefContext(date="2026-06-26", user_name="Nate", candidates=[],
                        data_frontier="2026-06-24", baseline_stale_days=2,
                        brief_stale_days=1, tsb_zone="fatigued")
    again = BriefContext.model_validate_json(full.model_dump_json())
    assert again.data_frontier == "2026-06-24"
    assert again.baseline_stale_days == 2
    assert again.brief_stale_days == 1
    assert again.tsb_zone == "fatigued"
