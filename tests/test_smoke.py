"""Smoke tests — verify imports + schema init work end-to-end."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from local_fitness import db
from local_fitness.agent import tools as agent_tools
from local_fitness.ingest import baselines


def test_imports():
    """All modules import without errors."""
    from local_fitness.agent import briefing, chat, prompts  # noqa
    from local_fitness.ingest import auth, backfill, daily  # noqa
    from local_fitness import cli  # noqa


def test_schema_init(tmp_path: Path):
    db_path = tmp_path / "test.db"
    db.init_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
        tables = {r[0] for r in cur.fetchall()}
    assert {
        "daily_metrics", "body_battery_samples", "stress_samples",
        "activities", "activity_hr_zones", "activity_splits",
        "baselines", "ingest_runs",
    } <= tables


def test_baselines_empty_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", db_path)
    db.init_schema(db_path)
    n = baselines.recompute(lookback_days=30)
    assert n == 31  # inclusive range


def test_tool_schemas_well_formed():
    """Every tool has a name + description + handler."""
    assert len(agent_tools.ALL_TOOLS) == 21
    for t in agent_tools.ALL_TOOLS:
        assert t.name
        assert t.description
        assert t.handler is not None
    names = agent_tools.allowed_tool_names()
    assert all(n.startswith("mcp__fitness__") for n in names)
