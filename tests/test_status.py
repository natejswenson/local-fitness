"""Tests for agent/status.py (assemble_status) + the daily_snapshot tool +
the coach MCP prompt's notes-once invariant.

``assemble_status`` is the single source of the daily snapshot: a pure read
that must never raise on an empty/new DB. The coach prompt embeds the user's
saved notes exactly once (via the persona, not the rendered snapshot).
"""
from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest

from local_fitness import db
from local_fitness.agent import status as status_mod
from local_fitness.agent import tools
from local_fitness.agent.status import assemble_status


@pytest.fixture
def empty_db(tmp_path, monkeypatch):
    """A freshly-init'd DB with no metrics/activities/baselines."""
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    db.init_schema(p)
    return p


@pytest.fixture
def seeded_status_db(tmp_path, monkeypatch):
    """Seeded with today's daily_metrics + a baselines row + one workout."""
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    db.init_schema(p)
    today = date.today().isoformat()
    with db.connect(p) as conn:
        conn.execute(
            "INSERT INTO daily_metrics (date, rhr, sleep_seconds, sleep_score, "
            "avg_stress, body_battery_min, body_battery_max, steps) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (today, 55, 27000, 80, 30, 20, 90, 9000),
        )
        # Baseline rhr_mean = 50 → today's 55 is +10% with an up arrow.
        conn.execute(
            "INSERT INTO baselines (date, rhr_60day_mean, rhr_60day_sd, "
            "body_battery_max_60day_mean, ctl, atl, tsb) "
            "VALUES (?, 50.0, 2.0, 88.0, 40.0, 45.0, -5.0)",
            (today,),
        )
        conn.execute(
            "INSERT INTO activities (activity_id, date, start_time, activity_type, "
            "activity_name, duration_seconds, distance_meters, avg_hr, training_load) "
            "VALUES (1, ?, ?, 'running', 'Morning Run', 3600, 10000, 150, 80.0)",
            (today, today + "T07:00:00"),
        )
    return p


def test_assemble_status_empty_db_well_formed(empty_db):
    status = assemble_status()  # must not raise
    assert set(status.keys()) >= {
        "date", "metrics", "training_load", "recent_workouts", "user_notes"
    }
    assert status["date"] == date.today().isoformat()
    assert isinstance(status["metrics"], list) and status["metrics"]
    tl = status["training_load"]
    assert tl["ctl"] is None and tl["atl"] is None and tl["tsb"] is None
    assert tl["interpretation"] == "no training-load data yet"
    assert status["recent_workouts"] == []
    assert status["user_notes"] == []


def test_daily_snapshot_tool_empty_db(empty_db):
    result = asyncio.run(tools.daily_snapshot.handler({}))
    assert not result.get("is_error")
    payload = json.loads(result["content"][0]["text"])  # valid JSON, no raise
    assert payload["date"] == date.today().isoformat()
    assert "metrics" in payload


def test_assemble_status_baseline_delta_and_workout(seeded_status_db):
    status = assemble_status()

    rhr_row = next(m for m in status["metrics"] if m["metric"] == "rhr")
    assert rhr_row["treatment"] == "baseline_delta"
    assert rhr_row["value"] == 55
    assert rhr_row["baseline"] == 50.0
    # (55 - 50) / 50 * 100 = +10.0
    assert rhr_row["delta_pct"] == 10.0
    assert rhr_row["arrow"] == "↑"

    assert status["recent_workouts"]
    w = status["recent_workouts"][0]
    # 10000 m → 6.21 mi (units display = miles)
    assert w["distance_mi"] == pytest.approx(6.21, abs=0.01)


def test_coach_prompt_renders_each_note_once(seeded_status_db):
    # Save exactly one user note via the real tool (writes to the env-pointed file).
    saved = asyncio.run(tools.save_user_note.handler({"note": "lead with the workout card"}))
    assert not saved.get("is_error")

    from mcp import types
    from local_fitness.web import mcp_server

    server = mcp_server.build_server()
    handler = server.request_handlers[types.GetPromptRequest]
    req = types.GetPromptRequest(
        method="prompts/get",
        params=types.GetPromptRequestParams(name="coach", arguments=None),
    )
    res = asyncio.run(handler(req))
    text = res.root.messages[0].content.text
    assert text.count("lead with the workout card") == 1


def test_coach_prompt_includes_output_formatting_contract(seeded_status_db):
    # The coach prompt must steer the model toward narrow/monospace-friendly
    # layouts so its reply renders cleanly in the MCP client.
    from mcp import types
    from local_fitness.web import mcp_server

    server = mcp_server.build_server()
    handler = server.request_handlers[types.GetPromptRequest]
    req = types.GetPromptRequest(
        method="prompts/get",
        params=types.GetPromptRequestParams(name="coach", arguments=None),
    )
    res = asyncio.run(handler(req))
    text = res.root.messages[0].content.text
    # The contract is inherited via the embedded system_prompt persona.
    assert "Formatting your chat replies" in text
    assert "NOT one wide grid" in text


# --- pure direction/slope/interpretation helpers ---------------------------

def test_arrow_directions():
    assert status_mod._arrow(1) == "↑"
    assert status_mod._arrow(-1) == "↓"
    assert status_mod._arrow(0) == "→"


def test_slope_arrow_too_few_points_returns_none():
    assert status_mod._slope_arrow([]) is None
    assert status_mod._slope_arrow([5.0]) is None


def test_slope_arrow_reads_trend_direction():
    assert status_mod._slope_arrow([1.0, 2.0, 3.0]) == "↑"
    assert status_mod._slope_arrow([3.0, 2.0, 1.0]) == "↓"
    assert status_mod._slope_arrow([2.0, 2.0, 2.0]) == "→"


def test_tsb_interpretation_all_bands():
    assert status_mod._tsb_interpretation(None) == "no training-load data yet"
    assert status_mod._tsb_interpretation(-25) == "very fatigued"
    assert status_mod._tsb_interpretation(-15) == "fatigued"
    assert status_mod._tsb_interpretation(10) == "fresh"
    assert status_mod._tsb_interpretation(0) == "neutral"


# --- assemble_status: trend slope + pace-bearing workout -------------------

@pytest.fixture
def trend_status_db(tmp_path, monkeypatch):
    """Seeded with several days of trend metrics + a paced workout, so the
    trend-slope path (>=2 points) and the formatted-pace field both fire."""
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    db.init_schema(p)
    from datetime import timedelta

    today = date.today()
    with db.connect(p) as conn:
        # In date order (oldest→newest) steps DECLINE → a down slope arrow.
        # offset 0 is today; older days carry the higher counts.
        for offset, steps in enumerate((9000, 10000, 11000, 12000)):
            d = (today - timedelta(days=offset)).isoformat()
            conn.execute(
                "INSERT INTO daily_metrics (date, steps, sleep_score, max_stress) "
                "VALUES (?, ?, ?, ?)",
                (d, steps, 70 + offset, 40 - offset),
            )
        # Workout carries a real pace so the formatted pace field is emitted.
        conn.execute(
            "INSERT INTO activities (activity_id, date, start_time, activity_type, "
            "activity_name, duration_seconds, distance_meters, avg_hr, "
            "avg_pace_sec_per_km, training_load) "
            "VALUES (1, ?, ?, 'running', 'Paced Run', 1800, 5000, 150, 300.0, 50.0)",
            (today.isoformat(), today.isoformat() + "T07:00:00"),
        )
    return p


def test_assemble_status_trend_slope_and_pace(trend_status_db):
    status = assemble_status()

    steps_row = next(m for m in status["metrics"] if m["metric"] == "steps")
    assert steps_row["treatment"] == "trend_arrow"
    # Descending steps across the window → a downward slope arrow (covers
    # both the >=2-point slope path and the negative-direction arrow).
    assert steps_row["arrow"] == "↓"

    w = status["recent_workouts"][0]
    # 300 sec/km → ~8:03 min/mi; the formatted-pace field is present.
    assert "pace_min_per_mi" in w
    assert w["duration_formatted"] == "30:00"


def test_assemble_status_seeded_tsb_interpretation(seeded_status_db):
    # The seeded baseline carries tsb=-5.0 → "neutral" via _training_load.
    status = assemble_status()
    assert status["training_load"]["tsb"] == -5.0
    assert status["training_load"]["interpretation"] == "neutral"
