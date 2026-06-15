"""Tests for the brief pre-fetch bundle (call-and-unwrap of the tool handlers)."""
from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta

import pytest

from local_fitness import db
from local_fitness.agent import briefing_data


@pytest.fixture
def empty_db(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "notes.md"))
    db.init_schema(p)
    return p


@pytest.fixture
def seeded_db(empty_db):
    today = date.today()
    with db.connect(empty_db) as conn:
        for i in range(40):
            d = (today - timedelta(days=i)).isoformat()
            conn.execute(
                "INSERT INTO daily_metrics (date, rhr, sleep_seconds, sleep_score, "
                "avg_stress, body_battery_min, body_battery_max, steps) "
                "VALUES (?, 50, 27000, 80, 25, 20, 85, 9000)",
                (d,),
            )
            conn.execute(
                "INSERT INTO baselines (date, rhr_60day_mean, rhr_60day_sd, ctl, atl, tsb) "
                "VALUES (?, 50, 2.0, 40, 38, 2)",
                (d,),
            )
    return empty_db


EXPECTED_KEYS = {
    "get_today_status", "training_load_status", "find_anomalies.rhr",
    "query_workouts.14d", "get_training_plan_status",
    "get_metric_trend.sleep_seconds", "get_metric_trend.steps",
    "get_metric_trend.rhr", "get_metric_trend.body_battery_max",
    "get_metric_trend.avg_stress",
}


def test_bundle_has_all_keys_incl_recovery_trends(seeded_db):
    bundle = asyncio.run(briefing_data.gather_brief_context())
    assert set(bundle) == EXPECTED_KEYS
    # the recovery trends the HR mandate needs are present (design RC-1 fix)
    assert "get_metric_trend.body_battery_max" in bundle
    assert "get_metric_trend.avg_stress" in bundle


def test_bundle_equals_tool_handler_output(seeded_db):
    """Call-and-unwrap: each bundle value equals the tool handler's own output
    (byte-identical to what the model fetches today)."""
    from local_fitness.agent import tools

    bundle = asyncio.run(briefing_data.gather_brief_context())
    today_via_tool = json.loads(
        asyncio.run(tools.get_today_status.handler({}))["content"][0]["text"]
    )
    assert bundle["get_today_status"] == today_via_tool


def test_bundle_on_empty_db_does_not_raise(empty_db):
    """A fresh clone (no rows) must not break prefetch — empty windows come
    back as {'error': ...} envelopes, carried verbatim (design RC-4)."""
    bundle = asyncio.run(briefing_data.gather_brief_context())
    assert set(bundle) == EXPECTED_KEYS
    # at least one trend reports no data rather than raising
    assert any(
        isinstance(v, dict) and "error" in v
        for k, v in bundle.items() if k.startswith("get_metric_trend")
    )


def test_render_is_compact_json(seeded_db):
    bundle = asyncio.run(briefing_data.gather_brief_context())
    s = briefing_data.render_for_prompt(bundle)
    assert "\n" not in s
    assert json.loads(s) == bundle  # round-trips
