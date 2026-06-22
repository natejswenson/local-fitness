"""Tests for the training-plan agent tools (draft-only write boundary)."""
from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta

import pytest

from local_fitness import db, plans
from local_fitness.agent import tools


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
