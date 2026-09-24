"""Readable projections preserve computed meaning, dates and session identity."""
from copy import deepcopy

from local_fitness.agent import chat_views


def snapshot(**updates):
    return {"date": "2026-09-24", "metrics": [], "training_load": {},
            "recent_workouts": [], **updates}


def test_snapshot_dates_each_value_and_never_compares_provisional_today():
    payload = snapshot(metrics=[
        {"metric": "rhr", "value": 50, "baseline": 50, "delta_pct": 0,
         "provisional_today_excluded": True, "provisional_today_value": 54},
        {"metric": "steps", "value": 0, "partial_today_excluded": True},
        {"metric": "sleep_seconds", "value": 27000, "provisional_today": True},
        {"metric": "active_calories", "value": 0, "partial_today": True},
    ], training_load={"ctl": 30, "atl": 35, "tsb": -5, "interpretation": "neutral",
                      "as_of": "2026-09-24", "current_form_date": "2026-09-23"})
    before = deepcopy(payload)
    text = chat_views.render_snapshot(payload)
    assert "| Resting heart rate | 50 bpm | 2026-09-23 | usual 50 bpm · +0% |" in text
    assert "| Resting heart rate | 54 bpm | 2026-09-24 | provisional · excluded from comparison |" in text
    assert "| Steps | 0 | 2026-09-23 |" in text
    assert "| Sleep | 7h 30m | 2026-09-24 | provisional |" in text
    assert "| Active calories | 0 kcal | 2026-09-24 | partial total |" in text
    assert "Current form · 2026-09-23" in text
    assert "Current form · 2026-09-24" not in text
    assert payload == before


def test_only_provisional_data_is_not_an_empty_database():
    text = chat_views.render_snapshot(snapshot(metrics=[{
        "metric": "rhr", "value": None, "provisional_today_value": 54,
        "provisional_today_excluded": True}]))
    assert "54 bpm" in text and "provisional" in text
    assert "No daily readings" not in text
    assert "Resting heart rate (2026-09-23)" in text


def test_empty_snapshot_is_actionable_without_a_wall_of_nulls():
    text = chat_views.render_snapshot(snapshot(metrics=[{"metric": "rhr", "value": None}]))
    assert "No daily readings available. Ask to sync Garmin" in text
    assert "No training-load data yet." in text
    assert "No workouts logged yet." in text
    assert "None" not in text and "| ---" not in text


def test_snapshot_uses_labels_units_and_keeps_names_literal():
    text = chat_views.render_snapshot(snapshot(
        metrics=[{"metric": "sleep_deep_seconds", "value": 3600},
                 {"metric": "steps", "value": 9000, "arrow": "↑", "treatment": "trend_arrow"},
                 {"metric": "avg_spo2", "value": 98}],
        recent_workouts=[{"date": "2026-09-23", "activity_name": "[click](https://invalid)\n# Surprise",
                          "distance_meters": 5000, "avg_hr": 140,
                          "duration_formatted": "30:00", "pace_min_per_mi": "9:39"}],
        user_notes=["private preference"], memory_status="unavailable",
        latest_brief_date="2026-09-21", brief_stale_days=3,
        training_load={"baseline_stale_days": 4, "as_of": "2026-09-20"},
        data_as_of="2026-09-24T09:00:00"))
    assert "1h 00m" in text and "98%" in text and "7-day trend ↑" in text
    assert "5 km" in text and "140 bpm" in text and "9:39/mi" in text
    assert "\\[click\\]" in text and "\n# Surprise" not in text
    assert "private preference" not in text
    assert "preferences are temporarily unavailable" in text
    assert "3 days old" in text and "4 days stale" in text
    assert "Last sync covering today: 2026-09-24T09:00:00" in text


def test_plan_without_active_plan_still_surfaces_pending_draft():
    text = chat_views.render_plan_status({"active": False, "pending_draft": {
        "title": "Autumn 10k", "workout_count": 20}})
    assert "No active training plan" in text
    assert "Draft awaiting a decision" in text and "Autumn 10k · 20 sessions" in text
    assert "not active" in text
    assert "create a plan" not in text
    assert "check for a draft" in chat_views.render_plan_progress({"active": False})
    assert "create a plan" in chat_views.render_plan_status({"active": False, "pending_draft": None})


def test_status_keeps_prescription_verdict_and_scope():
    text = chat_views.render_plan_status({"active": True, "as_of": "2026-09-24",
        "data_through": "2026-09-22", "goal_type": "10k", "race_date": "2026-10-10",
        "target_time_formatted": "50:00", "adherence_pct": 0, "sessions_adherence_pct": 0,
        "today": {"date": "2026-09-24", "seq": 2, "type": "easy",
                  "target_distance_m": 5000, "target_duration_sec": 1800,
                  "target_pace_sec_per_km": 360, "target_hr_max": 140,
                  "description": "Comfortable effort", "verdict": "pending"}})
    assert "Today · 2026-09-24 · session 2" in text
    assert "easy · 5 km · 30:00 · 9:39/mi · HR ≤ 140 bpm · pending" in text
    assert "Comfortable effort" in text
    assert "Whole-plan adherence · All days: 0% · Sessions: 0%" in text
    assert "Data does not cover today" in text
    assert "single-session summary" in text
    assert "Last graded: no session recorded" in text


def test_progress_displays_daily_actuals_once_on_double_day():
    workouts = [{"date": "2026-09-24", "seq": seq, "type": kind,
                 "target_distance_mi": target, "actual_distance_mi": 7,
                 "description": "Run | walk\nEasy", "verdict": verdict}
                for seq, kind, target, verdict in [(1, "easy", 3, "done"), (2, "easy", 4, "partial")]]
    payload = {"active": True, "as_of": "2026-09-24", "data_through": "2026-09-24",
               "workout_window": {"full": False, "start": "2026-09-10", "end": "2026-10-01"},
               "workouts": workouts, "this_week": {"week_actual_mi": 7, "week_planned_mi": 8,
                                                   "week_run_mi": 5, "week_walk_mi": 2}}
    before = deepcopy(payload)
    text = chat_views.render_plan_progress(payload)
    assert "Session 1 · easy · 3 mi · done" in text
    assert "Session 2 · easy · 4 mi · partial" in text
    assert text.count("Day total: 7 mi") == 1
    assert "shared across sessions" in text
    assert "Trailing 7 days" in text
    assert "Displayed window: 2026-09-10 to 2026-10-01" in text
    assert "full plan" in text
    assert payload == before
    payload["workout_window"]["full"] = True
    full = chat_views.render_plan_progress(payload)
    assert "Full plan:" in full and "Ask for the full plan" not in full


def test_empty_window_and_unknown_frontier_do_not_imply_rest_or_failure():
    text = chat_views.render_plan_progress({"active": True, "data_through": None,
                                          "as_of": "2026-09-24", "workouts": []})
    assert "No prescribed sessions in this window" in text
    assert "Daily data through unknown" in text
    assert "Data does not cover today" in text
    assert "rest day" not in text and "missed" not in text
    assert "Projected finish unavailable" in text


def test_projection_keeps_basis_and_uncertainty_beside_estimate():
    text = chat_views.render_plan_progress({"active": True,
        "predicted_finish_formatted": "52:00", "projection_confidence": "low",
        "projection_basis": {"date": "2026-09-01", "distance_mi": 2,
                             "pace_min_per_mi": "8:00", "extrapolation_ratio": 3.1},
        "goal_gap": {"gap_formatted": "+2:00"}})
    assert "Projected finish: 52:00 · confidence low (estimate)" in text
    assert "Based on 2026-09-01 · 2 mi · 8:00/mi · extrapolation 3.1×" in text
    assert "Projected gap: +2:00 (positive = slower than goal)" in text
