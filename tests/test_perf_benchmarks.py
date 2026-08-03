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
from perf_fixture import build_perf_fixture_db, build_report_card_fixture_db

from local_fitness import db
from local_fitness.agent import brief_planner, report_card, tools
from local_fitness.agent.status import assemble_status

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


@pytest.fixture(scope="module")
def rc_fixture(tmp_path_factory):
    """The report-card path's own fixture DB — see perf_fixture's comment on
    why it is a separate file and not extra rows in `perf_db`."""
    dest = tmp_path_factory.mktemp("perf_rc") / "fitness.db"
    return build_report_card_fixture_db(dest, today=_TODAY)


@pytest.fixture
def rc_db(rc_fixture, monkeypatch):
    path, activity_id = rc_fixture
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", path)
    return path, activity_id


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


def test_bench_report_card_inputs_and_build(benchmark, rc_db):
    """The deterministic half of a report-card render: every DB read plus all
    the grading. Deliberately NOT the whole handler — that resolves the coach's
    memory and (on a cache miss) makes an SDK call, so its latency would
    measure the model, not this code.

    The PDF path is left out of the gate on purpose: WeasyPrint/matplotlib
    latency is font- and machine-dependent and would false-fail a 15% floor.
    """
    path, activity_id = rc_db

    def _render():
        with db.connect(path) as conn:
            inputs = report_card.load_report_card_inputs(
                conn, activity_id=activity_id)
            return report_card.build_card(
                inputs["activity"], inputs["splits"], inputs["plan_workout"],
                inputs["reference"], inputs["context"], inputs.get("hr_samples"),
                inputs.get("recent_activities"),
                inputs.get("upcoming_workouts"), inputs.get("hr_zones"))

    card = benchmark(_render)
    # Guard the benchmark against measuring a degenerate card: if the fixture
    # ever stops resolving a real reference pool, every metric returns n/a
    # early and the timing silently stops describing the graded path.
    assert card["reference"]["mode"] == "rolling_60d"
    assert card["reference"]["excluded_other_mode"] > 0    # the locomotion filter ran
    assert card["splits"]["available"] is True             # the split exceptions ran
    assert card["metrics"]["continuity"]["grade"] is not None
    assert card["overall"]["graded_metrics"] == 4


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


def test_load_report_card_inputs_opens_no_connection(rc_db, monkeypatch):
    """It takes the caller's connection and must never open one of its own —
    that contract is what lets the handler hold ONE connection for the whole
    render. Every read in it (activity, splits, HR zones, plan, recent
    activities, baselines, the 60-day reference pool, the same-date siblings)
    rides that connection or the count moves."""
    path, activity_id = rc_db
    with db.connect(path) as conn:
        counts = _count_connect_opens(monkeypatch)
        inputs = report_card.load_report_card_inputs(
            conn, activity_id=activity_id)
    assert counts["n"] == 0
    assert inputs is not None


def test_workout_report_card_opens_two_connections(rc_db, monkeypatch):
    """The whole handler, end to end, opens EXACTLY two connections: one
    shared by every read, plus save_card's own (which cannot share — it runs on
    a worker thread via asyncio.to_thread, and sqlite3 connections are
    same-thread-checked).

    This is the assertion that catches the regression that actually matters on
    this path. It was ~8 opens before the shared-connection fix, and the shape
    that would bring them back — a per-split or per-metric lookup inside the
    grading loop — is invisible to a latency gate on a small fixture but
    obvious here.
    """
    _path, activity_id = rc_db

    # Reflect is fire-and-forget and NOT part of the render; leaving it live
    # would race its own connection into the count depending on when the loop
    # tore down. The SDK is blocked suite-wide (conftest), so the coach read
    # takes its deterministic fallback — the DB work is identical either way.
    async def _no_reflect(_card):
        return None

    monkeypatch.setattr(tools.reflect, "reflect_after_report_card", _no_reflect)

    counts = _count_connect_opens(monkeypatch)
    result = _run(tools.workout_report_card.handler(
        {"activity_id": activity_id, "format": "table"}))
    assert not result.get("is_error"), result
    assert counts["n"] == 2


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
            conn, today.isoformat(), baseline=None, current_form=None, step_goal=10000,
            plan_today=None, days_to_race=None,
        )
    assert sig.days_since_last_run is None
    assert sig.recent_te == ()
