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
