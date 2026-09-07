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


def _wk(date="2026-07-01", seq=1, week_index=1, type="easy",  # noqa: A002 — mirrors the plan_workouts column names
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


def test_discard_draft_archives(dbp):
    pid = plans.insert_draft(_plan(), [_wk()], db_path=dbp)
    plans.discard_draft(pid, db_path=dbp)
    assert plans.get_plan(pid, db_path=dbp)["status"] == "archived"


def test_discard_draft_missing_raises(dbp):
    with pytest.raises(plans.PlanNotFoundError):
        plans.discard_draft(9999, db_path=dbp)


def test_discard_draft_refuses_active(dbp):
    pid = plans.insert_draft(_plan(), [_wk()], db_path=dbp)
    plans.commit_plan(pid, now="t", db_path=dbp)
    with pytest.raises(plans.NotDraftError):
        plans.discard_draft(pid, db_path=dbp)
    assert plans.get_plan(pid, db_path=dbp)["status"] == "active"  # untouched


def test_abandon_active_plan_archives_and_returns_id(dbp):
    pid = plans.insert_draft(_plan(), [_wk()], db_path=dbp)
    plans.commit_plan(pid, now="t", db_path=dbp)
    archived_id = plans.abandon_active_plan(db_path=dbp)
    assert archived_id == pid
    assert plans.get_plan(pid, db_path=dbp)["status"] == "archived"
    assert plans.get_active_plan(db_path=dbp) is None


def test_abandon_active_plan_no_active_raises(dbp):
    plans.insert_draft(_plan(), [_wk()], db_path=dbp)  # a draft, never committed
    with pytest.raises(plans.NoActivePlanError):
        plans.abandon_active_plan(db_path=dbp)


def test_cannot_force_second_active_plan(dbp):
    """The partial unique index is the race backstop: even a direct UPDATE
    that would create a second active plan must fail loudly (design H5)."""
    import sqlite3

    a = plans.insert_draft(_plan(), [_wk()], db_path=dbp)
    plans.commit_plan(a, now="t", db_path=dbp)
    b = plans.insert_draft(_plan(), [_wk()], db_path=dbp)
    with pytest.raises(sqlite3.IntegrityError):
        with db.connect(dbp) as conn:
            conn.execute("UPDATE training_plans SET status='active' WHERE plan_id=?", (b,))
    # The original active plan is untouched.
    assert plans.get_active_plan(db_path=dbp)["plan_id"] == a


def test_adherence_immune_to_plan_edits(dbp):
    """A missed day stays missed even if the plan rows are edited — verdicts
    come from the activities join, not AI-authored plan fields (design H2)."""
    plan = {
        "plan_id": 1, "goal_type": "10k", "race_date": "2026-09-14",
        "workouts": [_wk(date="2026-07-01", type="easy", target_distance_m=6000.0)],
    }
    # No activity on that date, and the day is before the frontier → missed.
    detail = plans.build_plan_detail(plan, frontier="2026-07-08", activities_by_date={})
    assert detail["workouts"][0]["verdict"] == "missed"


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


def test_build_plan_detail_attaches_actuals():
    """Each graded workout carries actual distance + aggregate pace from the
    matched activities, so the schedule can show Target vs Actual."""
    plan = {
        "plan_id": 1, "goal_type": "10k", "goal_distance_m": 10000.0,
        "race_date": "2026-09-14",
        "workouts": [
            _wk(date="2026-07-01", target_distance_m=6000.0),  # ran it
            _wk(date="2026-07-02", target_distance_m=6000.0),  # skipped
        ],
    }
    # 6km in 1800s → 300 s/km pace
    activities_by_date = {"2026-07-01": [_run(6000, duration=1800)]}
    detail = plans.build_plan_detail(plan, frontier="2026-07-08",
                                     activities_by_date=activities_by_date)
    w0, w1 = detail["workouts"]
    assert w0["actual_distance_m"] == 6000.0
    assert abs(w0["actual_pace_sec_per_km"] - 300.0) < 0.01
    assert w1["actual_distance_m"] == 0.0
    assert w1["actual_pace_sec_per_km"] is None


def test_build_plan_detail_surfaces_walk_on_easy_day():
    """A recovery walk satisfies an easy day (done) and is surfaced as a walk."""
    plan = {
        "plan_id": 1, "goal_type": "half", "race_date": "2026-09-14",
        "workouts": [_wk(date="2026-07-01", type="easy", target_distance_m=4828.0)],
    }
    activities_by_date = {"2026-07-01": [_run(6213, duration=3959, atype="walking")]}
    detail = plans.build_plan_detail(plan, frontier="2026-07-08",
                                     activities_by_date=activities_by_date)
    w0 = detail["workouts"][0]
    assert w0["verdict"] == "done"
    assert w0["actual_distance_m"] == 6213
    assert w0["actual_activity_types"] == ["walking"]


def test_build_plan_detail_surfaces_walk_on_long_day_still_missed():
    """A long day walked (not run) is missed, but the walk is still surfaced."""
    plan = {
        "plan_id": 1, "goal_type": "half", "race_date": "2026-09-14",
        "workouts": [_wk(date="2026-07-01", type="long", target_distance_m=12000.0)],
    }
    activities_by_date = {"2026-07-01": [_run(6213, duration=3959, atype="walking")]}
    detail = plans.build_plan_detail(plan, frontier="2026-07-08",
                                     activities_by_date=activities_by_date)
    w0 = detail["workouts"][0]
    assert w0["verdict"] == "missed"            # walks don't satisfy a long run
    assert w0["actual_distance_m"] == 6213       # but the walk is reflected
    assert w0["actual_activity_types"] == ["walking"]


# --- the quality-day pace cap, end to end (#242) ---------------------------

def _insert_activity(conn, aid, day, atype, dist, dur, pace, splits=()):
    conn.execute(
        "INSERT INTO activities (activity_id, date, activity_type, "
        "distance_meters, duration_seconds, avg_pace_sec_per_km) "
        "VALUES (?, ?, ?, ?, ?, ?)", (aid, day, atype, dist, dur, pace))
    for i, (sd, sp) in enumerate(splits):
        conn.execute(
            "INSERT INTO activity_splits (activity_id, split_index, "
            "distance_meters, duration_seconds, avg_pace_sec_per_km) "
            "VALUES (?, ?, ?, ?, ?)", (aid, i, sd, sd / 1000.0 * sp, sp))


def test_load_activities_by_date_attaches_splits(dbp):
    """The cap reads splits, and this is the ONLY place they are fetched — for
    the reason avg_pace_sec_per_km is fetched here (see the query's comment): a
    gate a caller can forget to wire ships as a silent no-op."""
    with db.connect(dbp) as conn:
        _insert_activity(conn, 1, "2026-07-01", "running", 8000.0, 2400, 300.0,
                         splits=[(1609.344, 290.0), (1609.344, 310.0)])
        _insert_activity(conn, 2, "2026-07-02", "running", 5000.0, 1500, 300.0)

    by_date = plans.load_activities_by_date("2026-07-01", "2026-07-31", db_path=dbp)
    with_splits = by_date["2026-07-01"][0]
    assert [s["avg_pace_sec_per_km"] for s in with_splits["splits"]] == [290.0, 310.0]
    assert with_splits["splits"][0]["distance_meters"] == pytest.approx(1609.344)
    # A splitless activity carries the key with an empty list, never a missing
    # key: the cap's `any(a.get("splits"))` short-circuit reads it on every day.
    assert by_date["2026-07-02"][0]["splits"] == []


def test_load_activities_by_date_keeps_each_activitys_splits_to_itself(dbp):
    """Two runs on one day, one fast and one slow. Attaching the day's splits to
    every row would let a walk-warmup lap fail the run beside it."""
    with db.connect(dbp) as conn:
        _insert_activity(conn, 1, "2026-07-01", "running", 8000.0, 2400, 300.0,
                         splits=[(1609.344, 290.0)])
        _insert_activity(conn, 2, "2026-07-01", "running", 3000.0, 1500, 500.0,
                         splits=[(1609.344, 500.0)])

    day = plans.load_activities_by_date("2026-07-01", "2026-07-01", db_path=dbp)["2026-07-01"]
    by_id = {a["activity_id"]: a for a in day}
    assert [s["avg_pace_sec_per_km"] for s in by_id[1]["splits"]] == [290.0]
    assert [s["avg_pace_sec_per_km"] for s in by_id[2]["splits"]] == [500.0]


def test_build_plan_detail_caps_a_quality_day_on_rep_pace(dbp):
    """The #242 shape through the assembly path: a tempo prescribing 7:48/mi and
    4.5 mi, run 4.01 mi at 10:07/mi. Volume alone says done (89% of target, over
    DONE_FRACTION); the reps say it was not a tempo.

    Asserted against the same plan grading an obedient run, so what moves the
    verdict is only the pace — and adherence moves with it, which is the
    inflation the issue reports."""
    target_pace = 468.0 / 1.609344            # 7:48/mi
    plan = {
        "plan_id": 1, "goal_type": "half", "race_date": "2026-09-14",
        "workouts": [_wk(date="2026-07-01", type="tempo",
                         target_distance_m=4.5 * 1609.344,
                         target_pace_sec_per_km=target_pace,
                         description="3x1mi @ 7:48/mi")],
    }
    with db.connect(dbp) as conn:
        _insert_activity(
            conn, 1, "2026-07-01", "running", 4.01 * 1609.344, 2540,
            633.0 / 1.609344,                 # 10:33/mi overall — still a run
            splits=[(1609.344, p / 1.609344) for p in (625.0, 607.0, 790.0)])
    by_date = plans.load_activities_by_date("2026-07-01", "2026-07-31", db_path=dbp)

    detail = plans.build_plan_detail(plan, frontier="2026-07-08",
                                     activities_by_date=by_date)
    assert detail["workouts"][0]["verdict"] == "missed"
    assert detail["adherence_pct"] == 0

    # Same prescription, reps at target: done, and adherence with it.
    obedient = {"2026-07-01": [dict(by_date["2026-07-01"][0],
                                    distance_meters=4.5 * 1609.344,
                                    splits=[{"distance_meters": 1609.344,
                                             "avg_pace_sec_per_km": target_pace}] * 3)]}
    done = plans.build_plan_detail(plan, frontier="2026-07-08",
                                   activities_by_date=obedient)
    assert done["workouts"][0]["verdict"] == "done"
    assert done["adherence_pct"] == 100


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


# --- active-plan prescription edits (update_active_workout) ------------------

def _active(dbp, workouts=None):
    pid = plans.insert_draft(
        _plan(),
        workouts or [_wk(date="2026-07-01"),
                     _wk(date="2026-07-02", type="long", target_distance_m=12000.0, description="12km long")],
        db_path=dbp,
    )
    plans.commit_plan(pid, now="2026-06-15T00:00:00", db_path=dbp)
    return pid


def test_update_active_workout_represcribes_day(dbp):
    _active(dbp)
    row = plans.update_active_workout(
        "2026-07-01", {"type": "long", "target_distance_m": 9656.0, "description": "moved up"}, db_path=dbp)
    assert row["type"] == "long" and row["target_distance_m"] == 9656.0 and row["description"] == "moved up"
    with db.connect(dbp) as conn:  # the other day is untouched
        other = conn.execute("SELECT type FROM plan_workouts WHERE date='2026-07-02'").fetchone()
    assert other["type"] == "long"


def test_update_active_workout_rest_nulls_distance(dbp):
    _active(dbp)
    row = plans.update_active_workout(
        "2026-07-01", {"type": "rest", "target_distance_m": None, "target_pace_sec_per_km": None}, db_path=dbp)
    assert row["type"] == "rest" and row["target_distance_m"] is None and row["target_pace_sec_per_km"] is None


def test_update_active_workout_no_active_raises(dbp):
    plans.insert_draft(_plan(), [_wk()], db_path=dbp)  # a draft, never committed
    with pytest.raises(plans.NoActivePlanError):
        plans.update_active_workout("2026-07-01", {"type": "easy"}, db_path=dbp)


def test_update_active_workout_unknown_date_raises(dbp):
    _active(dbp)
    with pytest.raises(ValueError, match="no workout"):
        plans.update_active_workout("2099-01-01", {"type": "easy"}, db_path=dbp)


def test_update_active_workout_rejects_non_editable_cols(dbp):
    _active(dbp)
    with pytest.raises(ValueError, match="non-editable"):
        plans.update_active_workout("2026-07-01", {"date": "2026-07-05"}, db_path=dbp)
    with pytest.raises(ValueError, match="non-editable"):
        plans.update_active_workout("2026-07-01", {"plan_id": 99}, db_path=dbp)


def test_update_active_workout_rejects_bad_type(dbp):
    _active(dbp)
    with pytest.raises(ValueError, match="unknown workout type"):
        plans.update_active_workout("2026-07-01", {"type": "sprint"}, db_path=dbp)


def test_update_active_workout_only_touches_active_plan(dbp):
    pid_active = _active(dbp)
    pid_draft = plans.insert_draft(_plan(title="draft"),
                                   [_wk(date="2026-07-01", description="draft day")], db_path=dbp)
    plans.update_active_workout("2026-07-01", {"description": "active edit"}, db_path=dbp)
    with db.connect(dbp) as conn:
        a = conn.execute("SELECT description FROM plan_workouts WHERE plan_id=? AND date='2026-07-01'", (pid_active,)).fetchone()
        d = conn.execute("SELECT description FROM plan_workouts WHERE plan_id=? AND date='2026-07-01'", (pid_draft,)).fetchone()
    assert a["description"] == "active edit"
    assert d["description"] == "draft day"  # the draft's day is left alone
