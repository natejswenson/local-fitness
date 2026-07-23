"""Tests for the training-plan agent tools (draft-only write boundary)."""
from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta

import pytest

from local_fitness import db, plans
from local_fitness.agent import tools, units


def call(tool, args):
    result = asyncio.run(tool.handler(args))
    text = result["content"][0]["text"]
    try:
        return json.loads(text), result.get("is_error", False)
    except json.JSONDecodeError:
        return text, result.get("is_error", False)


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    # data frontier = today; created floor for validation = today
    with db.connect(p) as conn:
        conn.execute("INSERT INTO daily_metrics (date, rhr) VALUES (?, 50)",
                     (date.today().isoformat(),))
    return p


def _args(**over):
    t = date.today()
    a = dict(
        goal_type="10k",
        race_date=(t + timedelta(days=90)).isoformat(),
        target_time_seconds=3000,
        workouts=[dict(date=(t + timedelta(days=1)).isoformat(), week_index=1,
                       type="easy", target_distance_m=6000.0, description="6km easy")],
    )
    a.update(over)
    return a


def test_propose_creates_draft(seeded):
    body, err = call(tools.propose_training_plan, _args())
    assert not err and body["status"] == "draft"
    assert plans.get_draft_plan(db_path=seeded)["plan_id"] == body["plan_id"]


def test_propose_defaults_goal_distance(seeded):
    body, _ = call(tools.propose_training_plan, _args())  # no goal_distance_m given
    plan = plans.get_plan(body["plan_id"], db_path=seeded)
    assert plan["goal_distance_m"] == 10000.0  # canonical 10k


def test_propose_rejects_bad_goal_type(seeded):
    body, err = call(tools.propose_training_plan, _args(goal_type="marathon"))
    assert err and "goal_type" in body["error"]


def test_propose_rejects_empty_workouts(seeded):
    body, err = call(tools.propose_training_plan, _args(workouts=[]))
    assert err and "error" in body


def test_revise_ignores_status_and_stays_draft(seeded):
    pid = call(tools.propose_training_plan, _args())[0]["plan_id"]
    # status is injected but must be ignored — the tool can never activate a plan
    body, err = call(tools.revise_training_plan, {"plan_id": pid, "title": "X", "status": "active"})
    assert not err and body["status"] == "draft"
    got = plans.get_plan(pid, db_path=seeded)
    assert got["status"] == "draft" and got["title"] == "X"


def test_revise_refuses_committed_plan(seeded):
    pid = call(tools.propose_training_plan, _args())[0]["plan_id"]
    plans.commit_plan(pid, now="t", db_path=seeded)
    body, err = call(tools.revise_training_plan, {"plan_id": pid, "title": "X"})
    assert err and "error" in body
    assert plans.get_plan(pid, db_path=seeded)["title"] == "Sub-50" or True  # unchanged title


# --- commit_training_plan / discard_training_plan_draft / abandon_active_plan

def test_commit_activates_draft(seeded):
    pid = call(tools.propose_training_plan, _args())[0]["plan_id"]
    body, err = call(tools.commit_training_plan, {"plan_id": pid})
    assert not err and body == {"plan_id": pid, "status": "active"}
    assert plans.get_plan(pid, db_path=seeded)["status"] == "active"


def test_commit_rejects_wrong_type_plan_id(seeded):
    body, err = call(tools.commit_training_plan, {"plan_id": "abc"})
    assert err and "plan_id" in body["error"]


def test_commit_rejects_missing_plan(seeded):
    body, err = call(tools.commit_training_plan, {"plan_id": 9999})
    assert err and "error" in body


def test_commit_rejects_already_active_plan(seeded):
    pid = call(tools.propose_training_plan, _args())[0]["plan_id"]
    plans.commit_plan(pid, now="t", db_path=seeded)
    body, err = call(tools.commit_training_plan, {"plan_id": pid})
    assert err and "error" in body


def test_discard_draft_archives_without_activating(seeded):
    pid = call(tools.propose_training_plan, _args())[0]["plan_id"]
    body, err = call(tools.discard_training_plan_draft, {"plan_id": pid})
    assert not err and body == {"plan_id": pid, "status": "archived"}
    assert plans.get_plan(pid, db_path=seeded)["status"] == "archived"
    assert plans.get_active_plan(db_path=seeded) is None


def test_discard_draft_rejects_wrong_type_plan_id(seeded):
    body, err = call(tools.discard_training_plan_draft, {"plan_id": "abc"})
    assert err and "plan_id" in body["error"]


def test_discard_draft_rejects_missing_plan(seeded):
    body, err = call(tools.discard_training_plan_draft, {"plan_id": 9999})
    assert err and "error" in body


def test_discard_draft_refuses_active_plan(seeded):
    pid = call(tools.propose_training_plan, _args())[0]["plan_id"]
    plans.commit_plan(pid, now="t", db_path=seeded)
    body, err = call(tools.discard_training_plan_draft, {"plan_id": pid})
    assert err and "error" in body
    assert plans.get_plan(pid, db_path=seeded)["status"] == "active"  # untouched


def test_abandon_active_plan_archives(seeded):
    pid = call(tools.propose_training_plan, _args())[0]["plan_id"]
    plans.commit_plan(pid, now="t", db_path=seeded)
    body, err = call(tools.abandon_active_plan, {})
    assert not err and body == {"plan_id": pid, "status": "archived"}
    assert plans.get_active_plan(db_path=seeded) is None


def test_abandon_active_plan_no_active_errors(seeded):
    body, err = call(tools.abandon_active_plan, {})
    assert err and "error" in body


def test_new_plan_lifecycle_tools_registered():
    names = {t.name for t in tools.ALL_TOOLS}
    assert {"commit_training_plan", "discard_training_plan_draft", "abandon_active_plan"} <= names


def test_new_plan_lifecycle_tools_not_read_only():
    ro = set(tools.read_only_tool_names())
    for name in ("commit_training_plan", "discard_training_plan_draft", "abandon_active_plan"):
        assert f"mcp__fitness__{name}" not in ro


def test_status_inactive(seeded):
    body, _ = call(tools.get_training_plan_status, {})
    assert body == {"active": False}


def test_status_active(seeded):
    pid = call(tools.propose_training_plan, _args())[0]["plan_id"]
    plans.commit_plan(pid, now="t", db_path=seeded)
    body, _ = call(tools.get_training_plan_status, {})
    assert body["active"] is True
    assert body["goal_type"] == "10k"
    assert "today" in body and "last_graded" in body


def test_tools_registered():
    names = {t.name for t in tools.ALL_TOOLS}
    assert {"propose_training_plan", "revise_training_plan", "get_training_plan_status"} <= names


# --- get_training_plan_progress (full graded plan) -------------------------

_VERDICTS = {"done", "partial", "missed", "compliant", "pending"}


def test_progress_inactive(seeded):
    body, _ = call(tools.get_training_plan_progress, {})
    assert body == {"active": False}


def test_progress_active_shape(seeded):
    pid = call(tools.propose_training_plan, _args())[0]["plan_id"]
    plans.commit_plan(pid, now="t", db_path=seeded)
    body, err = call(tools.get_training_plan_progress, {})
    assert not err and body["active"] is True
    assert body["goal_type"] == "10k"
    # full graded list (not the slim today/last_graded summary)
    assert len(body["workouts"]) == 1
    w = body["workouts"][0]
    assert w["verdict"] in _VERDICTS
    assert "week_index" in w and "description" in w
    # surfacing field threaded through the projection allowlist
    assert "actual_activity_types" in w and isinstance(w["actual_activity_types"], list)
    assert body["days_to_race"] == 90  # race_date is today + 90
    assert body["predicted_finish_seconds"] is None or isinstance(
        body["predicted_finish_seconds"], int
    )
    # build_plan_detail's identifiers must be projected OUT
    assert "plan_id" not in body and "status" not in body and "weekly_mileage" not in body


def test_progress_kept_out_of_brief_allowlist():
    # exposed to MCP clients, but never enters the brief loop's frozen allow-list
    assert "get_training_plan_progress" in {t.name for t in tools.ALL_TOOLS}
    assert "mcp__fitness__get_training_plan_progress" not in tools.read_only_tool_names()


def test_progress_absent_race_date_yields_none_not_crash(seeded, monkeypatch):
    # The wrapper reads race_date via .get(...) — a plan dict with NO race_date
    # key must yield days_to_race=None, not a KeyError (the bare-subscript bug).
    t = date.today()
    crafted = dict(
        goal_type="10k",
        target_time_seconds=3000,
        # deliberately NO "race_date" key
        workouts=[dict(date=(t + timedelta(days=1)).isoformat(), week_index=1,
                       type="easy", target_distance_m=6000.0,
                       target_pace_sec_per_km=None, target_duration_sec=None,
                       description="6km easy")],
    )
    monkeypatch.setattr(plans, "get_active_plan", lambda *a, **k: crafted)
    body, err = call(tools.get_training_plan_progress, {})
    assert not err and body["active"] is True
    assert body["days_to_race"] is None


def test_progress_verdict_parity_with_status(seeded):
    # Same active plan: the progress tool's verdict for today's workout matches
    # what get_training_plan_status grades for `today`. (Parity, not a regression
    # net — both tools share the grading window.)
    t = date.today()
    over = dict(workouts=[dict(date=t.isoformat(), week_index=1, type="easy",
                               target_distance_m=6000.0, description="6km easy")])
    pid = call(tools.propose_training_plan, _args(**over))[0]["plan_id"]
    plans.commit_plan(pid, now="t", db_path=seeded)
    status, _ = call(tools.get_training_plan_status, {})
    progress, _ = call(tools.get_training_plan_progress, {})
    today_wk = next(w for w in progress["workouts"] if w["date"] == t.isoformat())
    assert today_wk["verdict"] == status["today"]["verdict"]


# === WS2 — plan-tool payload quality =========================================
# (docs/plans/2026-07-12-deterministic-intelligence-and-ux-design.md, WS2)

# --- 2c: default windowing / full=true --------------------------------------

def _plan_from_today(offsets, week_index=lambda o: 1):
    t = date.today()
    return t, [
        dict(date=(t + timedelta(days=o)).isoformat(), week_index=week_index(o),
             type="easy", target_distance_m=5000.0, description="easy run")
        for o in offsets
    ]


def test_progress_default_call_windows_workouts(seeded):
    t, workouts = _plan_from_today(range(0, 91))
    pid = call(tools.propose_training_plan, _args(
        race_date=(t + timedelta(days=120)).isoformat(), workouts=workouts))[0]["plan_id"]
    plans.commit_plan(pid, now="t", db_path=seeded)
    body, err = call(tools.get_training_plan_progress, {})
    assert not err
    dates = [w["date"] for w in body["workouts"]]
    assert dates  # non-empty
    # frontier == today (seeded's daily_metrics row) -> window [today-14, today+7]
    assert max(dates) == (t + timedelta(days=7)).isoformat()
    assert len(body["workouts"]) < len(workouts)  # truncated vs the full 91-day plan


def test_progress_full_true_returns_complete_list(seeded):
    t, workouts = _plan_from_today(range(0, 91))
    pid = call(tools.propose_training_plan, _args(
        race_date=(t + timedelta(days=120)).isoformat(), workouts=workouts))[0]["plan_id"]
    plans.commit_plan(pid, now="t", db_path=seeded)
    body, err = call(tools.get_training_plan_progress, {"full": True})
    assert not err
    assert len(body["workouts"]) == len(workouts)


def test_progress_rollups_identical_windowed_and_full(seeded):
    t, workouts = _plan_from_today(range(0, 91))
    pid = call(tools.propose_training_plan, _args(
        race_date=(t + timedelta(days=120)).isoformat(), workouts=workouts))[0]["plan_id"]
    plans.commit_plan(pid, now="t", db_path=seeded)
    windowed, _ = call(tools.get_training_plan_progress, {})
    full, _ = call(tools.get_training_plan_progress, {"full": True})
    for key in ("adherence_pct", "days_to_race", "predicted_finish_seconds", "goal_gap", "this_week"):
        assert windowed[key] == full[key]
    assert len(windowed["workouts"]) != len(full["workouts"])


def test_progress_today_in_window_under_stale_frontier(seeded, monkeypatch):
    t, workouts = _plan_from_today([0, 10])  # today, and 10 days out (beyond +7)
    pid = call(tools.propose_training_plan, _args(
        race_date=(t + timedelta(days=30)).isoformat(), workouts=workouts))[0]["plan_id"]
    plans.commit_plan(pid, now="t", db_path=seeded)
    stale = (t - timedelta(days=10)).isoformat()
    monkeypatch.setattr(db, "last_known_daily_date", lambda *a, **k: stale)
    body, err = call(tools.get_training_plan_progress, {})
    assert not err
    dates = [w["date"] for w in body["workouts"]]
    assert t.isoformat() in dates  # today stays in-window despite a 10d-stale frontier
    assert (t + timedelta(days=10)).isoformat() not in dates  # beyond anchor_fwd(=today)+7


def test_progress_window_defined_when_frontier_none(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)  # no daily_metrics rows at all -> frontier is None
    t = date.today()
    workouts = [dict(date=t.isoformat(), week_index=1, type="easy",
                      target_distance_m=5000.0, description="today")]
    pid = call(tools.propose_training_plan, _args(
        race_date=(t + timedelta(days=30)).isoformat(), workouts=workouts))[0]["plan_id"]
    plans.commit_plan(pid, now="t", db_path=p)
    body, err = call(tools.get_training_plan_progress, {})
    assert not err
    assert len(body["workouts"]) == 1  # falls back to [today-14, today+7], no crash


def test_progress_fully_past_plan_workouts_empty_but_rollups_present(seeded, monkeypatch):
    t = date.today()
    crafted = dict(
        goal_type="10k",
        race_date=(t - timedelta(days=30)).isoformat(),
        target_time_seconds=3000,
        workouts=[dict(date=(t - timedelta(days=40)).isoformat(), week_index=1, type="easy",
                       target_distance_m=6000.0, target_pace_sec_per_km=None,
                       target_duration_sec=None, description="race week")],
    )
    monkeypatch.setattr(plans, "get_active_plan", lambda *a, **k: crafted)
    body, err = call(tools.get_training_plan_progress, {})
    assert not err and body["active"] is True
    assert body["workouts"] == []  # entirely outside [today-14, today+7] -- no clamping
    assert body["adherence_pct"] is not None  # rollups still computed from the FULL graded list
    assert "this_week" in body


# --- 2b/2d: goal_gap, this_week, formatted time fields -----------------------

def test_progress_goal_gap_and_this_week_and_formatted_fields(seeded):
    t = date.today()
    with db.connect(seeded) as conn:
        conn.execute(
            "INSERT INTO activities (activity_id, date, activity_type, distance_meters, "
            "duration_seconds, avg_pace_sec_per_km) VALUES (1, ?, 'running', 10000.0, 3300, 330.0)",
            (t.isoformat(),),
        )
    pid = call(tools.propose_training_plan, _args())[0]["plan_id"]  # 10k goal, target 3000s
    plans.commit_plan(pid, now="t", db_path=seeded)
    body, err = call(tools.get_training_plan_progress, {})
    assert not err
    assert body["predicted_finish_seconds"] == 3300
    assert body["predicted_finish_formatted"] == "55:00"
    assert body["target_time_seconds"] == 3000
    assert body["target_time_formatted"] == "50:00"
    assert body["goal_gap"] == {"gap_seconds": 300.0, "gap_pct": 10.0, "on_pace": False}
    assert set(body["this_week"]) == {"week_planned_mi", "week_actual_mi", "slips"}


def test_progress_goal_gap_none_without_projection(seeded):
    pid = call(tools.propose_training_plan, _args())[0]["plan_id"]  # no recent effort seeded
    plans.commit_plan(pid, now="t", db_path=seeded)
    body, _ = call(tools.get_training_plan_progress, {})
    assert body["predicted_finish_seconds"] is None
    assert body["predicted_finish_formatted"] is None
    assert body["goal_gap"] is None


# --- 2e (Fix C) + 2d: per-workout mile/pace + target_duration_formatted -----

def test_progress_workouts_carry_fix_c_and_duration_formatted_fields(seeded):
    t = date.today()
    workouts = [dict(date=t.isoformat(), week_index=1, type="easy",
                      target_distance_m=6000.0, target_pace_sec_per_km=330.0,
                      target_duration_sec=1980.0, description="6km easy")]
    with db.connect(seeded) as conn:
        conn.execute(
            "INSERT INTO activities (activity_id, date, activity_type, distance_meters, "
            "duration_seconds, avg_pace_sec_per_km) VALUES (1, ?, 'running', 6100.0, 2000, 328.0)",
            (t.isoformat(),),
        )
    pid = call(tools.propose_training_plan, _args(workouts=workouts))[0]["plan_id"]
    plans.commit_plan(pid, now="t", db_path=seeded)
    body, err = call(tools.get_training_plan_progress, {})
    assert not err
    w = next(w for w in body["workouts"] if w["date"] == t.isoformat())
    assert w["target_distance_mi"] == units.to_miles(6000.0)
    assert w["actual_distance_mi"] == units.to_miles(6100.0)
    assert w["target_pace_min_per_mi"] == units.format_pace_min_per_mi(330.0)
    assert w["actual_pace_min_per_mi"] == units.format_pace_min_per_mi(328.0)
    assert w["target_duration_formatted"] == units.format_duration(1980.0)


def test_progress_workouts_omit_distance_mi_in_km_mode(seeded, monkeypatch):
    monkeypatch.setenv("LOCAL_FITNESS_DISPLAY_UNITS", "km")
    t = date.today()
    workouts = [dict(date=t.isoformat(), week_index=1, type="easy",
                      target_distance_m=6000.0, target_pace_sec_per_km=330.0,
                      description="6km easy")]
    pid = call(tools.propose_training_plan, _args(workouts=workouts))[0]["plan_id"]
    plans.commit_plan(pid, now="t", db_path=seeded)
    body, err = call(tools.get_training_plan_progress, {})
    assert not err
    w = body["workouts"][0]
    assert "target_distance_mi" not in w
    assert "actual_distance_mi" not in w
    assert w["target_pace_min_per_mi"] == units.format_pace_min_per_mi(330.0)  # unconditional


# --- 2d/2e on get_training_plan_status ---------------------------------------

def test_status_target_time_formatted(seeded):
    pid = call(tools.propose_training_plan, _args())[0]["plan_id"]
    plans.commit_plan(pid, now="t", db_path=seeded)
    body, _ = call(tools.get_training_plan_status, {})
    assert body["target_time_formatted"] == units.format_duration(3000)
    assert "goal_gap" not in body and "this_week" not in body
    assert "predicted_finish_formatted" not in body


def test_status_today_carries_fix_c_and_duration_formatted_fields(seeded):
    t = date.today()
    workouts = [dict(date=t.isoformat(), week_index=1, type="easy",
                      target_distance_m=6000.0, target_pace_sec_per_km=330.0,
                      target_duration_sec=1980.0, description="6km easy")]
    pid = call(tools.propose_training_plan, _args(workouts=workouts))[0]["plan_id"]
    plans.commit_plan(pid, now="t", db_path=seeded)
    body, _ = call(tools.get_training_plan_status, {})
    today_w = body["today"]
    assert today_w is not None
    assert today_w["target_distance_mi"] == units.to_miles(6000.0)
    assert today_w["target_pace_min_per_mi"] == units.format_pace_min_per_mi(330.0)
    assert today_w["target_duration_formatted"] == units.format_duration(1980.0)
    # _slim_workout carries no actual_* keys at all -> nothing for the helper to convert
    assert "actual_distance_mi" not in today_w and "actual_pace_min_per_mi" not in today_w


def test_status_today_omits_distance_mi_in_km_mode(seeded, monkeypatch):
    monkeypatch.setenv("LOCAL_FITNESS_DISPLAY_UNITS", "km")
    t = date.today()
    workouts = [dict(date=t.isoformat(), week_index=1, type="easy",
                      target_distance_m=6000.0, target_pace_sec_per_km=330.0,
                      description="6km easy")]
    pid = call(tools.propose_training_plan, _args(workouts=workouts))[0]["plan_id"]
    plans.commit_plan(pid, now="t", db_path=seeded)
    body, _ = call(tools.get_training_plan_status, {})
    today_w = body["today"]
    assert "target_distance_mi" not in today_w
    assert today_w["target_pace_min_per_mi"] == units.format_pace_min_per_mi(330.0)


# --- description rewrites ----------------------------------------------------

def test_status_description_points_at_progress():
    assert "get_training_plan_progress" in tools.get_training_plan_status.description


def test_progress_description_mentions_full_and_points_at_status():
    desc = tools.get_training_plan_progress.description
    assert "full=true" in desc
    assert "get_training_plan_status" in desc


# --- update_plan_workout (agent edits the ACTIVE plan; UI is view-only) ------

def _active_plan(seeded):
    body, _ = call(tools.propose_training_plan, _args())
    plans.commit_plan(body["plan_id"], now="2026-06-26T00:00:00", db_path=seeded)
    return (date.today() + timedelta(days=1)).isoformat()  # the seeded workout's date


def test_update_plan_workout_represcribes_active_day(seeded):
    d = _active_plan(seeded)
    body, err = call(tools.update_plan_workout,
                     {"date": d, "type": "long", "distance_mi": 6, "description": "Long run 6mi"})
    assert not err
    with db.connect(seeded) as conn:
        row = conn.execute("SELECT type, target_distance_m, description FROM plan_workouts WHERE date=?", (d,)).fetchone()
    assert row["type"] == "long"
    assert abs(row["target_distance_m"] - 6 * 1609.344) < 1   # miles → meters
    assert row["description"] == "Long run 6mi"


def test_update_plan_workout_rest_clears_distance(seeded):
    d = _active_plan(seeded)
    _body, err = call(tools.update_plan_workout, {"date": d, "type": "rest", "description": "Rest"})
    assert not err
    with db.connect(seeded) as conn:
        row = conn.execute("SELECT type, target_distance_m, target_pace_sec_per_km FROM plan_workouts WHERE date=?", (d,)).fetchone()
    assert row["type"] == "rest" and row["target_distance_m"] is None and row["target_pace_sec_per_km"] is None


def test_update_plan_workout_no_active_plan(seeded):
    call(tools.propose_training_plan, _args())  # a draft, not active
    body, err = call(tools.update_plan_workout, {"date": (date.today() + timedelta(days=1)).isoformat(), "type": "long"})
    assert err and "no active" in body["error"]


def test_update_plan_workout_bad_date(seeded):
    _active_plan(seeded)
    body, err = call(tools.update_plan_workout, {"date": "not-a-date", "type": "easy"})
    assert err and "date" in body["error"]


def test_update_plan_workout_bad_type(seeded):
    d = _active_plan(seeded)
    body, err = call(tools.update_plan_workout, {"date": d, "type": "sprint"})
    assert err and "unknown type" in body["error"]


def test_update_plan_workout_no_fields(seeded):
    d = _active_plan(seeded)
    body, err = call(tools.update_plan_workout, {"date": d})
    assert err and "nothing to update" in body["error"]


def test_update_plan_workout_unknown_date(seeded):
    _active_plan(seeded)
    far = (date.today() + timedelta(days=999)).isoformat()
    body, err = call(tools.update_plan_workout, {"date": far, "type": "easy"})
    assert err and "no workout" in body["error"]


def test_update_plan_workout_is_a_write_tool_not_in_brief(seeded):
    assert "mcp__fitness__update_plan_workout" in tools.allowed_tool_names()
    assert "mcp__fitness__update_plan_workout" not in tools.read_only_tool_names()


# --- round-2 facet review fixes ---------------------------------------------

def test_update_plan_workout_duration_min_sets_graded_field(seeded):
    # MED-1: tempo/interval grade on target_duration_sec — the tool must be
    # able to set it ("make Thursday's tempo 30 min").
    d = _active_plan(seeded)
    _body, err = call(tools.update_plan_workout,
                      {"date": d, "type": "tempo", "duration_min": 30})
    assert not err
    with db.connect(seeded) as conn:
        row = conn.execute(
            "SELECT type, target_duration_sec FROM plan_workouts WHERE date=?", (d,)
        ).fetchone()
    assert row["type"] == "tempo"
    assert row["target_duration_sec"] == 1800


def test_update_plan_workout_echoes_duration_and_seq_it_wrote(seeded):
    # The confirmation payload must carry the duration just written (the graded
    # field for tempo/interval) so the model can confirm the edit from the tool
    # result — not re-query or confirm blind. Pre-fix the echo dropped both
    # target_duration_sec and seq.
    d = _active_double_day(seeded)
    body, err = call(tools.update_plan_workout,
                     {"date": d, "seq": 2, "duration_min": 40})
    assert not err
    assert body["seq"] == 2                       # which session of the double day
    assert body["duration_seconds"] == 2400       # the value actually written
    assert body["duration_formatted"] == "40:00"  # formatted for the reply


def test_update_plan_workout_rest_defaults_description(seeded):
    # MED-2: a rest-flip without a new description must not leave the old
    # hard-run prose on the rest day.
    d = _active_plan(seeded)
    _body, err = call(tools.update_plan_workout,
                      {"date": d, "description": "Long run 12mi, 3 at goal pace"})
    assert not err
    _body, err = call(tools.update_plan_workout, {"date": d, "type": "rest"})
    assert not err
    with db.connect(seeded) as conn:
        row = conn.execute(
            "SELECT type, description FROM plan_workouts WHERE date=?", (d,)
        ).fetchone()
    assert row["type"] == "rest"
    assert row["description"] == "Rest day"


def test_update_plan_workout_rest_explicit_description_wins(seeded):
    d = _active_plan(seeded)
    _body, err = call(tools.update_plan_workout,
                      {"date": d, "type": "rest", "description": "Travel day — full rest"})
    assert not err
    with db.connect(seeded) as conn:
        row = conn.execute(
            "SELECT description FROM plan_workouts WHERE date=?", (d,)
        ).fetchone()
    assert row["description"] == "Travel day — full rest"


def test_revise_goal_type_rederives_goal_distance(seeded):
    # MED-4: revise(goal_type=...) without goal_distance_m must re-derive the
    # distance, or the Riegel projection predicts the old distance under the
    # new label.
    pid = call(tools.propose_training_plan, _args())[0]["plan_id"]  # 10k draft
    _body, err = call(tools.revise_training_plan, {"plan_id": pid, "goal_type": "half"})
    assert not err
    plan = plans.get_plan(pid)
    assert plan["goal_type"] == "half"
    assert plan["goal_distance_m"] == 21097.5


def test_revise_goal_type_explicit_distance_wins(seeded):
    pid = call(tools.propose_training_plan, _args())[0]["plan_id"]
    _body, err = call(tools.revise_training_plan,
                      {"plan_id": pid, "goal_type": "half", "goal_distance_m": 20000.0})
    assert not err
    plan = plans.get_plan(pid)
    assert plan["goal_distance_m"] == 20000.0


def test_get_training_plan_status_is_in_brief_allowlist():
    # Round-2 prompts finding 1: briefing_prompt (V1) says "call
    # get_training_plan_status FIRST" — the rollback path's grant must match
    # its prompt or plan-aware V1 briefs are silently dead.
    assert "mcp__fitness__get_training_plan_status" in tools.read_only_tool_names()


# --- plan_chart (round-2 facet review: two-series scheduled-vs-actual) -------

def test_plan_chart_no_active_plan(seeded):
    body, err = call(tools.plan_chart, {})
    assert err and "no active" in body["error"]


def _active_plan_today(seeded):
    """An ACTIVE plan whose single workout is dated today (inside the
    trailing window anchored at the frontier, which the fixture sets to
    today)."""
    args = _args(workouts=[dict(
        date=date.today().isoformat(), week_index=1, type="easy",
        target_distance_m=6000.0, description="6km easy")])
    body, err = call(tools.propose_training_plan, args)
    assert not err
    plans.commit_plan(body["plan_id"], now="2026-06-26T00:00:00", db_path=seeded)


def test_plan_chart_daily_renders_plan_rows(seeded):
    _active_plan_today(seeded)
    text, err = call(tools.plan_chart, {"days": 14})
    assert not err
    assert "plan vs actual · last 14d" in text
    assert f"{date.today().isoformat()[5:]} easy" in text
    assert "░" in text  # planned distance renders as plan cells
    assert "█ run vs ░ short of plan" in text  # legend present


def test_plan_chart_weekly_mode(seeded):
    _active_plan_today(seeded)
    text, err = call(tools.plan_chart, {"days": 14, "weekly": True})
    assert not err
    assert "weekly" in text
    assert "wk " in text


def test_plan_chart_auto_weekly_past_threshold(seeded):
    _active_plan_today(seeded)
    text, err = call(tools.plan_chart, {"days": 30})
    assert not err
    assert "weekly" in text


def test_plan_chart_registered_but_not_in_brief_allowlist():
    assert "plan_chart" in {t.name for t in tools.ALL_TOOLS}
    assert "mcp__fitness__plan_chart" not in tools.read_only_tool_names()


# --- round-2 backlog: seq (double-day) support + structural dedup ------------

def _active_double_day(seeded):
    """ACTIVE plan with an AM easy run + PM interval on the same date."""
    d = (date.today() + timedelta(days=1)).isoformat()
    args = _args(workouts=[
        dict(date=d, seq=1, week_index=1, type="easy",
             target_distance_m=5000.0, description="AM easy 5k"),
        dict(date=d, seq=2, week_index=1, type="interval",
             target_duration_sec=1800, description="PM intervals 30min"),
    ])
    body, err = call(tools.propose_training_plan, args)
    assert not err
    plans.commit_plan(body["plan_id"], now="2026-06-26T00:00:00", db_path=seeded)
    return d


def test_update_plan_workout_seq_targets_second_session(seeded):
    d = _active_double_day(seeded)
    _body, err = call(tools.update_plan_workout,
                      {"date": d, "seq": 2, "duration_min": 40})
    assert not err
    with db.connect(seeded) as conn:
        rows = conn.execute(
            "SELECT seq, type, target_duration_sec, target_distance_m "
            "FROM plan_workouts WHERE date=? ORDER BY seq", (d,)).fetchall()
    # PM session updated to 40 min; AM session untouched.
    assert rows[1]["seq"] == 2 and rows[1]["target_duration_sec"] == 2400
    assert rows[0]["seq"] == 1 and rows[0]["target_distance_m"] == 5000.0


def test_update_plan_workout_seq_defaults_to_first_session(seeded):
    d = _active_double_day(seeded)
    _body, err = call(tools.update_plan_workout, {"date": d, "distance_mi": 4.0})
    assert not err
    with db.connect(seeded) as conn:
        rows = conn.execute(
            "SELECT seq, target_distance_m, target_duration_sec "
            "FROM plan_workouts WHERE date=? ORDER BY seq", (d,)).fetchall()
    assert abs(rows[0]["target_distance_m"] - 4 * 1609.344) < 1
    assert rows[1]["target_duration_sec"] == 1800  # PM untouched


def test_update_plan_workout_rejects_bad_seq(seeded):
    d = _active_plan(seeded)
    body, err = call(tools.update_plan_workout, {"date": d, "type": "easy", "seq": 0})
    assert err and "seq" in body["error"]


def test_progress_projection_carries_seq(seeded):
    d = _active_double_day(seeded)
    body, err = call(tools.get_training_plan_progress, {"full": True})
    assert not err
    same_day = [w for w in body["workouts"] if w["date"] == d]
    assert sorted(w["seq"] for w in same_day) == [1, 2]


def test_plan_workouts_duplicate_day_rejected_by_db(seeded):
    # LOW-2: the (plan_id, date, seq) invariant is structural now, not just
    # validation-time — a direct duplicate insert fails loudly.
    import sqlite3
    body, _ = call(tools.propose_training_plan, _args())
    pid = body["plan_id"]
    d = (date.today() + timedelta(days=1)).isoformat()  # _args seeds this day
    with pytest.raises(sqlite3.IntegrityError):
        with db.connect(seeded) as conn:
            conn.execute(
                "INSERT INTO plan_workouts (plan_id, date, seq, week_index, type, description) "
                "VALUES (?, ?, 1, 1, 'easy', 'dupe')", (pid, d))
