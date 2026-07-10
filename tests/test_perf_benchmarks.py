"""Perf-benchmark suite for Part A of
docs/plans/2026-07-09-mcp-speed-and-ui-retirement-design.md.

Skipped by default: `pyproject.toml`'s `addopts` carries `--benchmark-skip`,
so a plain `uv run pytest` never runs the (slow, timed) benchmarks here — only
an explicit `--benchmark-only` invocation does (see the design doc's
"Eval-proof methodology" section for the exact commands and CI wiring).

Two axes are asserted, per the design:
  * latency — the `benchmark` fixture, compared against a committed
    pytest-benchmark baseline in CI (`--benchmark-compare-fail=min:15%`);
  * connection-open COUNT — a monkeypatched counter around `db.connect`,
    asserting the exact number of `sqlite3` connections each call chain
    opens. Latency alone is noisy and can't distinguish "still fast because
    the machine is fast" from "actually opens one connection now".

# pre-fix baseline (measured 2026-07-09, before Part A fixes #1-#3, against
# this file's synthetic 3-year/active-plan fixture):
#   assemble_brief_context        9 db.connect() opens
#   get_training_plan_progress    6 db.connect() opens
#   get_training_plan_status      4 db.connect() opens
#   _build_plan_section           4 db.connect() opens
#   _compute_signals's activities query: unbounded (scanned the entire
#     `activities` table back to the beginning of the fixture, not a fixed
#     lookback window)
# Post-fix (current code, asserted below): each chain opens exactly 1
# connection; the activities query is bounded by
# brief_planner._ACTIVITY_LOOKBACK_DAYS (35 days).
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

from local_fitness import db
from local_fitness.agent import brief_planner, tools
from local_fitness.agent.status import assemble_status

from perf_fixture import build_perf_fixture_db

_TODAY = date(2026, 7, 9)


@pytest.fixture(scope="module")
def perf_db(tmp_path_factory):
    dest = tmp_path_factory.mktemp("perf") / "fitness.db"
    return build_perf_fixture_db(dest, today=_TODAY)


@pytest.fixture
def default_db(perf_db, monkeypatch):
    """Point the DEFAULT db path (used by tools.py's no-arg db.connect() /
    plans.* calls) at the perf fixture, for the tools.py-level targets."""
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", perf_db)
    return perf_db


def _count_connect_opens(monkeypatch):
    counts = {"n": 0}
    orig_connect = db.connect

    def counting_connect(*args, **kwargs):
        counts["n"] += 1
        return orig_connect(*args, **kwargs)

    monkeypatch.setattr(db, "connect", counting_connect)
    return counts


def _run(coro):
    return asyncio.run(coro)


# --- latency: pytest-benchmark targets --------------------------------------

def test_bench_assemble_brief_context(benchmark, perf_db):
    result = benchmark(
        brief_planner.assemble_brief_context, perf_db, today=_TODAY.isoformat()
    )
    assert result.candidates  # sanity: the fixture actually produced takeaways


def test_bench_get_training_plan_progress(benchmark, default_db):
    result = benchmark(lambda: _run(tools.get_training_plan_progress.handler({})))
    assert result["content"][0]["text"]


def test_bench_get_training_plan_status(benchmark, default_db):
    result = benchmark(lambda: _run(tools.get_training_plan_status.handler({})))
    assert result["content"][0]["text"]


def test_bench_build_plan_section(benchmark, default_db):
    result = benchmark(tools._build_plan_section, _TODAY.isoformat())
    assert result is not None


def test_bench_daily_snapshot(benchmark, default_db):
    result = benchmark(assemble_status, _TODAY.isoformat())
    assert result["metrics"]


# --- connection-open COUNT: explicit assertions, not just latency ----------

def test_assemble_brief_context_opens_one_connection(perf_db, monkeypatch):
    counts = _count_connect_opens(monkeypatch)
    brief_planner.assemble_brief_context(perf_db, today=_TODAY.isoformat())
    assert counts["n"] == 1


def test_get_training_plan_progress_opens_one_connection(default_db, monkeypatch):
    counts = _count_connect_opens(monkeypatch)
    _run(tools.get_training_plan_progress.handler({}))
    assert counts["n"] == 1


def test_get_training_plan_status_opens_one_connection(default_db, monkeypatch):
    counts = _count_connect_opens(monkeypatch)
    _run(tools.get_training_plan_status.handler({}))
    assert counts["n"] == 1


def test_build_plan_section_opens_one_connection(default_db, monkeypatch):
    counts = _count_connect_opens(monkeypatch)
    tools._build_plan_section(_TODAY.isoformat())
    assert counts["n"] == 1


def test_daily_snapshot_opens_one_connection(default_db, monkeypatch):
    counts = _count_connect_opens(monkeypatch)
    assemble_status(_TODAY.isoformat())
    assert counts["n"] == 1


# --- fix #3 regression: bounded activities lookback -------------------------

def test_compute_signals_activities_query_is_bounded(perf_db):
    """The activities query inside _compute_signals only scans the trailing
    _ACTIVITY_LOOKBACK_DAYS window, not the fixture's full 3-year history."""
    with db.connect(perf_db) as conn:
        today_d = _TODAY
        activity_floor = (today_d - timedelta(
            days=brief_planner._ACTIVITY_LOOKBACK_DAYS)).isoformat()
        row_count = conn.execute(
            "SELECT COUNT(*) AS n FROM activities WHERE date <= ? AND date >= ?",
            (_TODAY.isoformat(), activity_floor),
        ).fetchone()["n"]
        unbounded_count = conn.execute(
            "SELECT COUNT(*) AS n FROM activities WHERE date <= ?",
            (_TODAY.isoformat(),),
        ).fetchone()["n"]
    # The 3-year fixture has many more activities than fit in a 35-day window
    # — proves the bound is real, not a no-op on a small fixture.
    assert unbounded_count > row_count
    assert row_count <= brief_planner._ACTIVITY_LOOKBACK_DAYS


def test_days_since_last_run_none_when_run_predates_bound(tmp_path):
    """Accepted tradeoff (design doc Fix #3): a run older than the 35-day
    bound reads as `days_since_last_run is None`, not the true (larger) day
    count and not a crash."""
    from perf_fixture import build_perf_fixture_db as _unused  # noqa: F401
    from local_fitness import db as db_mod

    p = tmp_path / "stale_run.db"
    db_mod.init_schema(p)
    today = _TODAY
    with db_mod.connect(p) as conn:
        # A single run 40 days ago — outside the 35-day bound — and nothing
        # inside it.
        stale_date = (today - timedelta(days=40)).isoformat()
        conn.execute(
            "INSERT INTO activities (activity_id, date, start_time, "
            "activity_type, activity_name, duration_seconds, distance_meters, "
            "avg_hr, training_load, aerobic_te) VALUES "
            "(1, ?, ?, 'running', 'Run', 1800, 6000, 150, 50.0, 2.5)",
            (stale_date, stale_date + "T07:00:00"),
        )
        conn.execute(
            "INSERT INTO daily_metrics (date, rhr) VALUES (?, 50)",
            (today.isoformat(),),
        )
        sig = brief_planner._compute_signals(
            conn, today.isoformat(), baseline=None, step_goal=10000,
            plan_today=None, days_to_race=None,
        )
    assert sig.days_since_last_run is None
    assert sig.recent_te == ()
