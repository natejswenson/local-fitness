"""Tests for plans.py pure logic — validation, adherence, grading, projection."""
from __future__ import annotations

import pytest

from local_fitness import plans


# --- helpers ---------------------------------------------------------------

def _wk(date="2026-07-01", seq=1, week_index=1, type="easy",
        target_distance_m=6000.0, target_pace_sec_per_km=None,
        target_duration_sec=None, description="6km easy"):
    return dict(date=date, seq=seq, week_index=week_index, type=type,
                target_distance_m=target_distance_m,
                target_pace_sec_per_km=target_pace_sec_per_km,
                target_duration_sec=target_duration_sec, description=description)


def _run(dist, duration=1800, atype="running"):
    return {"activity_type": atype, "distance_meters": dist, "duration_seconds": duration}


def _act(atype, duration=1800, dist=0.0):
    return {"activity_type": atype, "distance_meters": dist, "duration_seconds": duration}


# --- Task 1.1: validation --------------------------------------------------

def test_validate_rejects_empty_workouts():
    err = plans.validate_plan_input("10k", "2026-09-14", workouts=[], created_date="2026-06-15")
    assert err and "workout" in err.lower()


def test_validate_rejects_bad_goal_type():
    err = plans.validate_plan_input("marathon", "2026-09-14",
        workouts=[_wk()], created_date="2026-06-15")
    assert err and "goal_type" in err


def test_validate_rejects_bad_workout_type():
    err = plans.validate_plan_input("10k", "2026-09-14",
        workouts=[_wk(type="sprintz")], created_date="2026-06-15")
    assert err and "type" in err


def test_validate_rejects_nonfinite_distance():
    err = plans.validate_plan_input("10k", "2026-09-14",
        workouts=[_wk(target_distance_m=float("inf"))], created_date="2026-06-15")
    assert err


def test_validate_rejects_negative_numeric():
    err = plans.validate_plan_input("10k", "2026-09-14",
        workouts=[_wk(target_distance_m=-1.0)], created_date="2026-06-15")
    assert err


def test_validate_rejects_wrong_typed_numeric_field():
    # A string where a number is expected must yield the clean indexed error,
    # not a raw TypeError out of math.isfinite().
    err = plans.validate_plan_input("10k", "2026-09-14",
        workouts=[_wk(target_distance_m="fast")], created_date="2026-06-15")
    assert err == "workout 0: target_distance_m must be a number"


def test_validate_rejects_bool_numeric_field():
    # bool is an int subclass in Python; it must be rejected as not-a-number.
    err = plans.validate_plan_input("10k", "2026-09-14",
        workouts=[_wk(target_distance_m=True)], created_date="2026-06-15")
    assert err == "workout 0: target_distance_m must be a number"


def test_validate_rejects_non_string_description():
    # A dict/list description must yield the clean indexed error, not a raw
    # AttributeError out of .strip().
    err = plans.validate_plan_input("10k", "2026-09-14",
        workouts=[_wk(description={"oops": 1})], created_date="2026-06-15")
    assert err == "workout 0: description must be a string"


def test_validate_rejects_bad_date():
    err = plans.validate_plan_input("10k", "2026-13-99",
        workouts=[_wk()], created_date="2026-06-15")
    assert err


def test_validate_rejects_duplicate_date_seq():
    err = plans.validate_plan_input("10k", "2026-09-14",
        workouts=[_wk(date="2026-07-01", seq=1), _wk(date="2026-07-01", seq=1)],
        created_date="2026-06-15")
    assert err and "duplicate" in err.lower()


def test_validate_allows_same_date_distinct_seq():
    err = plans.validate_plan_input("10k", "2026-09-14",
        workouts=[_wk(date="2026-07-01", seq=1), _wk(date="2026-07-01", seq=2)],
        created_date="2026-06-15")
    assert err is None


def test_validate_rejects_workout_after_race():
    err = plans.validate_plan_input("10k", "2026-09-14",
        workouts=[_wk(date="2026-09-20")], created_date="2026-06-15")
    assert err


def test_validate_rejects_workout_before_created():
    err = plans.validate_plan_input("10k", "2026-09-14",
        workouts=[_wk(date="2026-06-01")], created_date="2026-06-15")
    assert err


def test_validate_rejects_too_many():
    wks = [_wk(date=f"2026-07-{(i % 28) + 1:02d}", seq=i) for i in range(plans.MAX_WORKOUTS + 1)]
    err = plans.validate_plan_input("10k", "2026-09-14", workouts=wks, created_date="2026-06-15")
    assert err


def test_validate_accepts_good_plan():
    err = plans.validate_plan_input("10k", "2026-09-14",
        workouts=[_wk(date="2026-07-01"),
                  _wk(date="2026-07-02", type="rest", target_distance_m=None)],
        created_date="2026-06-15")
    assert err is None


# --- Task 1.2: type-aware adherence ---------------------------------------

def test_rest_day_always_compliant():
    assert plans.classify_workout({"type": "rest"}, []) == "compliant"
    assert plans.classify_workout({"type": "rest"}, [_run(5000)]) == "compliant"


def test_easy_distance_thresholds():
    w = {"type": "easy", "target_distance_m": 6000.0}
    assert plans.classify_workout(w, [_run(6000)]) == "done"
    assert plans.classify_workout(w, [_run(3000)]) == "partial"
    assert plans.classify_workout(w, []) == "missed"


def test_easy_null_target_any_run_done():
    w = {"type": "easy", "target_distance_m": None}
    assert plans.classify_workout(w, [_run(4000)]) == "done"
    assert plans.classify_workout(w, []) == "missed"


def test_multiple_runs_summed():
    w = {"type": "long", "target_distance_m": 10000.0}
    assert plans.classify_workout(w, [_run(6000), _run(5000)]) == "done"


def test_interval_graded_on_duration():
    w = {"type": "interval", "target_duration_sec": 3600}
    assert plans.classify_workout(w, [_run(4000, duration=3600)]) == "done"
    assert plans.classify_workout(w, [_run(2000, duration=600)]) == "partial"
    assert plans.classify_workout(w, []) == "missed"


def test_tempo_graded_on_duration():
    w = {"type": "tempo", "target_duration_sec": 2400}
    assert plans.classify_workout(w, [_run(5000, duration=2400)]) == "done"
    assert plans.classify_workout(w, []) == "missed"


def test_cross_matches_non_running_only():
    w = {"type": "cross", "target_duration_sec": 1800}
    assert plans.classify_workout(w, [_act("cycling", duration=2000)]) == "done"
    assert plans.classify_workout(w, [_run(5000, duration=1800)]) == "missed"


# --- walks count on easy/recovery days, never on quality/long days --------

def test_easy_counts_walking():
    w = {"type": "easy", "target_distance_m": 6000.0}
    assert plans.classify_workout(w, [_run(6200, atype="walking")]) == "done"
    assert plans.classify_workout(w, [_run(3000, atype="walking")]) == "partial"


def test_easy_null_target_walk_counts():
    w = {"type": "easy", "target_distance_m": None}
    assert plans.classify_workout(w, [_run(4000, atype="walking")]) == "done"


def test_long_does_not_count_walking():
    w = {"type": "long", "target_distance_m": 6000.0}
    assert plans.classify_workout(w, [_run(6200, atype="walking")]) == "missed"


def test_tempo_does_not_count_walking():
    w = {"type": "tempo", "target_duration_sec": 2400}
    assert plans.classify_workout(w, [_run(6200, duration=2400, atype="walking")]) == "missed"


# --- Task 1.3: data-frontier grading --------------------------------------

def test_future_or_unsynced_day_is_pending():
    w = {"type": "easy", "target_distance_m": 6000.0, "date": "2026-07-10"}
    assert plans.grade_workout(w, [], frontier="2026-07-08") == "pending"


def test_day_equal_frontier_is_pending():
    w = {"type": "easy", "target_distance_m": 6000.0, "date": "2026-07-08"}
    assert plans.grade_workout(w, [], frontier="2026-07-08") == "pending"


def test_past_day_is_graded():
    w = {"type": "easy", "target_distance_m": 6000.0, "date": "2026-07-05"}
    assert plans.grade_workout(w, [], frontier="2026-07-08") == "missed"


def test_no_frontier_means_pending():
    w = {"type": "easy", "target_distance_m": 6000.0, "date": "2026-07-05"}
    assert plans.grade_workout(w, [], frontier=None) == "pending"


# --- outcome-based pending: a completed today grades; partial/missed held ---

def test_today_with_run_grades_done_not_pending():
    # today (== frontier) with a qualifying run grades done, not pending
    w = {"type": "easy", "target_distance_m": 6000.0, "date": "2026-07-08"}
    assert plans.grade_workout(w, [_run(6000)], frontier="2026-07-08") == "done"


def test_today_walk_on_easy_grades_done():
    w = {"type": "easy", "target_distance_m": 6000.0, "date": "2026-07-08"}
    assert plans.grade_workout(w, [_run(6200, atype="walking")], frontier="2026-07-08") == "done"


def test_today_partial_is_held_pending():
    # a half-done easy run today must NOT count 0.5 prematurely — held pending
    w = {"type": "easy", "target_distance_m": 6000.0, "date": "2026-07-08"}
    assert plans.grade_workout(w, [_run(3000)], frontier="2026-07-08") == "pending"


def test_today_rest_is_compliant_not_pending():
    w = {"type": "rest", "date": "2026-07-08"}
    assert plans.grade_workout(w, [], frontier="2026-07-08") == "compliant"


def test_past_partial_before_frontier_grades_partial():
    w = {"type": "easy", "target_distance_m": 6000.0, "date": "2026-07-05"}
    assert plans.grade_workout(w, [_run(3000)], frontier="2026-07-08") == "partial"


def test_no_frontier_grades_a_done_day():
    # benign change: with no daily frontier, a day with a qualifying run still
    # grades done (the old rule held every day pending)
    w = {"type": "easy", "target_distance_m": 6000.0, "date": "2026-07-05"}
    assert plans.grade_workout(w, [_run(6000)], frontier=None) == "done"


# --- GradingConfig threading (configurable knobs) -------------------------

def test_default_gradingconfig_reproduces_current_behavior():
    w = {"type": "easy", "target_distance_m": 6000.0}
    assert (plans.classify_workout(w, [_run(6000)])
            == plans.classify_workout(w, [_run(6000)], plans.GradingConfig()) == "done")


def test_count_walks_easy_toggle():
    w = {"type": "easy", "target_distance_m": 6000.0}
    walk = [_run(6200, atype="walking")]
    assert plans.classify_workout(w, walk) == "done"  # default True
    off = plans.GradingConfig(count_walks_easy=False)
    assert plans.classify_workout(w, walk, off) == "missed"  # walk no longer counts


def test_custom_fractions_shift_distance_bands():
    w = {"type": "easy", "target_distance_m": 10000.0}
    cfg = plans.GradingConfig(done_fraction=0.5, partial_fraction=0.2)
    assert plans.classify_workout(w, [_run(6000)], cfg) == "done"     # 0.60 ≥ 0.5
    assert plans.classify_workout(w, [_run(3000)], cfg) == "partial"  # 0.30 ∈ [0.2,0.5)
    assert plans.classify_workout(w, [_run(1000)], cfg) == "missed"   # 0.10 < 0.2


def test_partial_fraction_threads_into_duration_band():
    w = {"type": "interval", "target_duration_sec": 3600}
    run = [_run(4000, duration=1500)]  # 1500/3600 = 0.4167
    assert plans.classify_workout(w, run) == "done"  # default 0.40: 0.417 ≥ 0.40
    cfg = plans.GradingConfig(partial_fraction=0.5)
    assert plans.classify_workout(w, run, cfg) == "partial"  # 0.417 < 0.5


def test_count_walks_mileage_toggle():
    workouts = [_wk(date="2026-07-01", week_index=1, target_distance_m=5000.0)]
    abd = {"2026-07-01": [_run(4000, atype="walking")]}
    assert plans.weekly_mileage(workouts, abd)[0]["actual_km"] == 0.0  # running-only default
    cfg = plans.GradingConfig(count_walks_mileage=True)
    assert plans.weekly_mileage(workouts, abd, cfg)[0]["actual_km"] == 4.0  # walk included


# --- Task 1.4: Riegel + weekly mileage ------------------------------------

def test_riegel_projection():
    secs = plans.riegel_predict(best_distance_m=10000, best_time_s=3000, target_distance_m=21097.5)
    assert 6500 < secs < 7200


def test_riegel_none_without_effort():
    assert plans.riegel_predict(None, None, 10000.0) is None
    assert plans.riegel_predict(10000, 3000, None) is None


# --- WS2 2b: goal_gap -------------------------------------------------------
# (docs/plans/2026-07-12-deterministic-intelligence-and-ux-design.md, WS2)

def test_goal_gap_none_when_predicted_finish_none():
    assert plans.goal_gap(None, 3000) is None


def test_goal_gap_none_when_target_time_none():
    assert plans.goal_gap(3100.0, None) is None


def test_goal_gap_none_when_target_time_zero():
    # storable (validate_plan_input rejects only negatives) but meaningless —
    # a bare zero would divide gap_pct by zero.
    assert plans.goal_gap(3100.0, 0) is None


def test_goal_gap_none_when_target_time_negative():
    assert plans.goal_gap(3100.0, -1) is None


def test_goal_gap_positive_when_slower_than_goal():
    gap = plans.goal_gap(3100.0, 3000)
    assert gap is not None
    assert gap["gap_seconds"] == 100.0
    assert gap["gap_pct"] == pytest.approx(3.3333, rel=1e-3)
    assert gap["on_pace"] is False


def test_goal_gap_negative_when_faster_than_goal():
    gap = plans.goal_gap(2850.0, 3000)
    assert gap is not None
    assert gap["gap_seconds"] == -150.0
    assert gap["gap_pct"] == pytest.approx(-5.0)
    assert gap["on_pace"] is True


def test_goal_gap_on_pace_true_at_exact_boundary():
    # predicted finish == target time -> gap 0, on_pace True (<=, inclusive).
    gap = plans.goal_gap(3000.0, 3000)
    assert gap == {"gap_seconds": 0.0, "gap_pct": 0.0, "on_pace": True}


def test_weekly_mileage_rollup():
    workouts = [
        {"week_index": 1, "target_distance_m": 6000.0, "date": "2026-07-01"},
        {"week_index": 1, "target_distance_m": 10000.0, "date": "2026-07-03"},
        {"week_index": 2, "target_distance_m": 8000.0, "date": "2026-07-08"},
    ]
    activities_by_date = {"2026-07-01": [_run(6000)], "2026-07-03": [_run(9000)]}
    rows = plans.weekly_mileage(workouts, activities_by_date)
    assert rows[0] == {"week": 1, "planned_km": 16.0, "actual_km": 15.0}
    assert rows[1] == {"week": 2, "planned_km": 8.0, "actual_km": 0.0}


def _weeks_to_workouts(week_totals_km):
    return [
        {"week_index": i + 1, "target_distance_m": t * 1000.0,
         "date": f"2026-07-{i + 1:02d}", "type": "long"}
        for i, t in enumerate(week_totals_km)
    ]


def test_score_plan_good_build_and_taper():
    s = plans.score_plan(_weeks_to_workouts([20, 23, 26, 18]))
    assert s["ramp_ok"] and s["has_taper"] and s["score"] == 1.0


def test_score_plan_flags_mileage_spike():
    s = plans.score_plan(_weeks_to_workouts([20, 40]))  # 100% week-over-week jump
    assert not s["ramp_ok"]


def test_score_plan_flags_no_taper():
    s = plans.score_plan(_weeks_to_workouts([20, 23, 26, 30]))  # peaks at the race week
    assert not s["has_taper"]


def test_score_plan_empty():
    s = plans.score_plan([])
    assert s["nonempty"] is False and s["score"] < 1.0


def test_weekly_mileage_dedups_same_date():
    # two workouts share a date; actual distance for that date counts once
    workouts = [
        {"week_index": 1, "target_distance_m": 3000.0, "date": "2026-07-01", "seq": 1},
        {"week_index": 1, "target_distance_m": 4000.0, "date": "2026-07-01", "seq": 2},
    ]
    activities_by_date = {"2026-07-01": [_run(5000)]}
    rows = plans.weekly_mileage(workouts, activities_by_date)
    assert rows[0] == {"week": 1, "planned_km": 7.0, "actual_km": 5.0}


# --- pace-gated locomotion --------------------------------------------------
# `activity_type` is Garmin's LABEL and it lies: walking-desk sessions log as
# `treadmill_running`. Before 2026-07-22 every distance/duration helper here
# was a substring match on that label, so walks counted as runs.

_REAL_RUN = {"activity_type": "treadmill_running", "distance_meters": 9574.85,
             "duration_seconds": 3822, "avg_pace_sec_per_km": 399.2}
_MISLABELLED_WALK = {"activity_type": "treadmill_running", "distance_meters": 5202.75,
                     "duration_seconds": 5670, "avg_pace_sec_per_km": 1090.5}


def test_running_distance_excludes_a_walk_wearing_a_running_label():
    """The live 2026-07-21 case: an interval day reported 9.18 mi actual when
    the run was 5.95 mi and the rest was a 29:15/mi walking-pad session."""
    day = [_REAL_RUN, _MISLABELLED_WALK]
    assert plans._running_distance(day) == pytest.approx(9574.85)
    assert plans._foot_distance(day) == pytest.approx(14777.60)


def test_walking_distance_is_the_complement_so_the_two_reconcile():
    day = [_REAL_RUN, _MISLABELLED_WALK]
    assert (plans._running_distance(day) + plans._walking_distance(day)
            == pytest.approx(plans._foot_distance(day)))


def test_running_duration_excludes_the_mislabelled_walk():
    """Duration is the GRADED field for tempo/interval, and this walk ran
    1:34:30 — long enough on its own to satisfy any rep target."""
    assert plans._running_duration([_REAL_RUN, _MISLABELLED_WALK]) == 3822


def test_an_interval_day_is_not_completed_by_a_long_walk():
    workout = {"type": "interval", "target_duration_sec": 1200}
    assert plans.classify_workout(workout, [_MISLABELLED_WALK]) == "missed"
    assert plans.classify_workout(workout, [_REAL_RUN]) == "done"


def test_a_long_run_is_not_satisfied_by_walking_miles():
    workout = {"type": "long", "target_distance_m": 12874.8}
    # 14.8 km of foot distance, but only 9.6 km of it was run.
    assert plans.classify_workout(workout, [_REAL_RUN, _MISLABELLED_WALK]) == "partial"


def test_an_easy_day_still_counts_a_walk_as_active_recovery():
    """The gate must not break deliberate walk days: easy/recovery grading is
    foot-based on purpose, which is what makes a prescribed walk gradeable."""
    workout = {"type": "easy", "target_distance_m": 4828.0}
    assert plans.classify_workout(workout, [_MISLABELLED_WALK]) == "done"


def test_pace_gate_can_be_turned_off():
    cfg = plans.GradingConfig(pace_gated_locomotion=False)
    day = [_REAL_RUN, _MISLABELLED_WALK]
    assert plans._running_distance(day, cfg) == pytest.approx(14777.60)


def test_a_paceless_row_falls_back_to_the_label_rather_than_vanishing():
    """A manual entry with no pace has an unknowable mode; dropping it from
    mileage entirely would be worse than trusting the label."""
    manual = {"activity_type": "running", "distance_meters": 5000.0,
              "duration_seconds": 1500, "avg_pace_sec_per_km": None}
    assert plans._running_distance([manual]) == pytest.approx(5000.0)


def test_a_genuinely_labelled_walk_that_was_actually_run_counts_as_running():
    """The gate is symmetric: it corrects the label in both directions."""
    fast = {"activity_type": "walking", "distance_meters": 5000.0,
            "duration_seconds": 1500, "avg_pace_sec_per_km": 300.0}
    assert plans._running_distance([fast]) == pytest.approx(5000.0)
    assert plans._walking_distance([fast]) == 0.0


def test_load_activities_by_date_selects_the_pace_the_gate_needs(tmp_path):
    """Regression: the gate shipped as a silent no-op because this query did
    not select avg_pace_sec_per_km, so every row fell back to the label."""
    import sqlite3
    from local_fitness import db as db_mod

    path = tmp_path / "t.db"
    db_mod.init_schema(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO activities (activity_id, date, activity_type, "
            "distance_meters, duration_seconds, avg_pace_sec_per_km) "
            "VALUES (1, '2026-07-21', 'treadmill_running', 5202.75, 5670, 1090.5)")
    by_date = plans.load_activities_by_date("2026-07-01", "2026-07-31", db_path=path)
    row = by_date["2026-07-21"][0]
    assert row["avg_pace_sec_per_km"] == pytest.approx(1090.5)
    assert plans._running_distance([row]) == 0.0


def test_a_bike_ride_is_never_run_distance():
    """Regression (shipped in 0.27.0, caught by the perf gate's review): the
    pace gate answers run-vs-walk, not foot-vs-wheel. A 30km ride paces at
    ~2:00/mi, so gating on pace alone counted it as 30km of RUNNING."""
    bike = {"activity_type": "cycling", "distance_meters": 30000.0,
            "duration_seconds": 3600, "avg_pace_sec_per_km": 120.0}
    assert plans._ran(bike) is False
    assert plans._running_distance([bike]) == 0.0
    assert plans._walking_distance([bike]) == 0.0
    assert plans._foot_distance([bike]) == 0.0


def test_a_bike_ride_does_not_satisfy_a_long_run():
    workout = {"type": "long", "target_distance_m": 12874.8}
    bike = {"activity_type": "cycling", "distance_meters": 30000.0,
            "duration_seconds": 3600, "avg_pace_sec_per_km": 120.0}
    assert plans.classify_workout(workout, [bike]) == "missed"


def test_workout_actuals_returns_foot_run_and_walk_in_one_pass():
    day = [_REAL_RUN, _MISLABELLED_WALK,
           {"activity_type": "cycling", "distance_meters": 30000.0,
            "duration_seconds": 3600, "avg_pace_sec_per_km": 120.0}]
    foot, run, walk, pace, types = plans._workout_actuals(day)
    assert foot == pytest.approx(14777.60)
    assert run == pytest.approx(9574.85)
    assert walk == pytest.approx(5202.75)
    assert run + walk == pytest.approx(foot)
    assert pace is not None
    assert types == ["other", "running"]


def test_workout_actuals_pace_is_none_without_foot_distance():
    assert plans._workout_actuals([])[3] is None
