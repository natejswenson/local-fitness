"""Tests for plans.py persistence + assembly (uses a real temp SQLite DB)."""
from __future__ import annotations

import pytest

from local_fitness import db, plans


@pytest.fixture
def dbp(tmp_path):
    p = tmp_path / "fitness.db"
    db.init_schema(p)
    return p


def _plan(**over):
    base = dict(goal_type="10k", race_date="2026-09-14", target_time_seconds=3000,
                goal_distance_m=10000.0, title="Sub-50", ability_snapshot={"vo2": 48},
                created_at="2026-06-15T00:00:00")
    base.update(over)
    return base


def _wk(date="2026-07-01", seq=1, week_index=1, type="easy",
        target_distance_m=6000.0, description="6km easy", **over):
    w = dict(date=date, seq=seq, week_index=week_index, type=type,
             target_distance_m=target_distance_m, target_pace_sec_per_km=None,
             target_duration_sec=None, description=description)
    w.update(over)
    return w


def _run(dist, duration=1800, atype="running"):
    return {"activity_type": atype, "distance_meters": dist, "duration_seconds": duration}


# --- persistence -----------------------------------------------------------

def test_insert_and_get_draft(dbp):
    pid = plans.insert_draft(_plan(), [_wk(date="2026-07-01")], db_path=dbp)
    got = plans.get_plan(pid, db_path=dbp)
    assert got["status"] == "draft"
    assert got["goal_type"] == "10k"
    assert got["ability_snapshot"] == {"vo2": 48}  # round-tripped JSON
    assert len(got["workouts"]) == 1


def test_insert_draft_archives_prior_draft(dbp):
    pid1 = plans.insert_draft(_plan(), [_wk()], db_path=dbp)
    pid2 = plans.insert_draft(_plan(), [_wk()], db_path=dbp)
    assert plans.get_plan(pid1, db_path=dbp)["status"] == "archived"
    assert plans.get_plan(pid2, db_path=dbp)["status"] == "draft"


def test_revise_replaces_workouts_atomically(dbp):
    pid = plans.insert_draft(_plan(), [_wk(date="2026-07-01")], db_path=dbp)
    plans.revise_draft(pid, fields={"title": "New"},
                       workouts=[_wk(date="2026-07-02"), _wk(date="2026-07-03")], db_path=dbp)
    got = plans.get_plan(pid, db_path=dbp)
    assert got["title"] == "New"
    assert [w["date"] for w in got["workouts"]] == ["2026-07-02", "2026-07-03"]


def test_revise_refuses_non_draft(dbp):
    pid = plans.insert_draft(_plan(), [_wk()], db_path=dbp)
    plans.commit_plan(pid, now="2026-06-15T00:00:00", db_path=dbp)
    with pytest.raises(plans.NotDraftError):
        plans.revise_draft(pid, fields={"title": "x"}, workouts=None, db_path=dbp)


def test_revise_rejects_non_editable_field(dbp):
    pid = plans.insert_draft(_plan(), [_wk()], db_path=dbp)
    with pytest.raises(ValueError):
        plans.revise_draft(pid, fields={"status": "active"}, workouts=None, db_path=dbp)
    assert plans.get_plan(pid, db_path=dbp)["status"] == "draft"


def test_commit_archives_prior_active(dbp):
    a = plans.insert_draft(_plan(), [_wk()], db_path=dbp)
    plans.commit_plan(a, now="2026-06-15T00:00:00", db_path=dbp)
    b = plans.insert_draft(_plan(), [_wk()], db_path=dbp)
    plans.commit_plan(b, now="2026-06-16T00:00:00", db_path=dbp)
    assert plans.get_plan(a, db_path=dbp)["status"] == "archived"
    assert plans.get_plan(b, db_path=dbp)["status"] == "active"
    assert plans.get_active_plan(db_path=dbp)["plan_id"] == b


def test_commit_rejects_nondraft(dbp):
    pid = plans.insert_draft(_plan(), [_wk()], db_path=dbp)
    plans.commit_plan(pid, now="t", db_path=dbp)
    with pytest.raises(plans.NotDraftError):
        plans.commit_plan(pid, now="t", db_path=dbp)


def test_commit_missing_raises(dbp):
    with pytest.raises(plans.PlanNotFoundError):
        plans.commit_plan(9999, now="t", db_path=dbp)


def test_delete_archives(dbp):
    pid = plans.insert_draft(_plan(), [_wk()], db_path=dbp)
    plans.commit_plan(pid, now="t", db_path=dbp)
    plans.delete_plan(pid, db_path=dbp)
    assert plans.get_plan(pid, db_path=dbp)["status"] == "archived"
    assert plans.get_active_plan(db_path=dbp) is None


def test_delete_missing_raises(dbp):
    with pytest.raises(plans.PlanNotFoundError):
        plans.delete_plan(9999, db_path=dbp)


def test_get_draft_plan(dbp):
    assert plans.get_draft_plan(db_path=dbp) is None
    pid = plans.insert_draft(_plan(), [_wk()], db_path=dbp)
    assert plans.get_draft_plan(db_path=dbp)["plan_id"] == pid


# --- assembly --------------------------------------------------------------

def test_build_plan_detail_grades_and_rolls_up():
    plan = {
        "plan_id": 1, "goal_type": "10k", "goal_distance_m": 10000.0,
        "race_date": "2026-09-14", "target_time_seconds": 3000,
        "workouts": [
            _wk(date="2026-07-01", target_distance_m=6000.0),   # done
            _wk(date="2026-07-02", target_distance_m=6000.0),   # missed
            _wk(date="2026-07-10", target_distance_m=6000.0),   # pending (>= frontier)
        ],
    }
    activities_by_date = {"2026-07-01": [_run(6000)]}
    detail = plans.build_plan_detail(plan, frontier="2026-07-08",
                                     activities_by_date=activities_by_date,
                                     best_effort={"distance_m": 5000, "time_s": 1400})
    verdicts = [w["verdict"] for w in detail["workouts"]]
    assert verdicts == ["done", "missed", "pending"]
    # adherence ignores pending: 1 done / 2 graded = 50%
    assert detail["adherence_pct"] == 50
    assert detail["predicted_finish_seconds"] is not None
    assert detail["weekly_mileage"][0]["planned_km"] == 18.0


def test_build_plan_status_inactive():
    assert plans.build_plan_status(None, frontier="2026-07-08",
                                   activities_by_date={}, today="2026-07-05") == {"active": False}


def test_build_plan_status_active_slices():
    plan = {
        "plan_id": 1, "goal_type": "10k", "race_date": "2026-09-14",
        "target_time_seconds": 3000,
        "workouts": [
            _wk(date="2026-07-04", target_distance_m=6000.0, description="x" * 400),  # last graded
            _wk(date="2026-07-08", target_distance_m=8000.0, type="tempo"),           # today
        ],
    }
    activities_by_date = {"2026-07-04": [_run(6000)]}
    status = plans.build_plan_status(plan, frontier="2026-07-08",
                                     activities_by_date=activities_by_date, today="2026-07-08")
    assert status["active"] is True
    assert status["days_to_race"] > 0
    assert status["last_graded"]["verdict"] == "done"
    assert len(status["last_graded"]["description"]) <= 130  # capped
    assert status["today"]["type"] == "tempo"
