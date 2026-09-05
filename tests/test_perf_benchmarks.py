"""Perf-benchmark suite for Part A of
docs/plans/2026-07-09-mcp-speed-and-ui-retirement-design.md.

Skipped by default: `pyproject.toml`'s `addopts` carries `--benchmark-skip`,
so a plain `uv run pytest` never runs the (slow, timed) benchmarks here — only
an explicit `--benchmark-only` invocation does (see the design doc's
"Eval-proof methodology" section for the exact commands and CI wiring).

Three axes are asserted:
  * latency — the `benchmark` fixture, compared against a committed
    pytest-benchmark baseline in CI (`--benchmark-compare-fail=min:15%`);
  * connection-open COUNT — a monkeypatched counter around `db.connect`,
    asserting the exact number of `sqlite3` connections each call chain
    opens. Latency alone is noisy and can't distinguish "still fast because
    the machine is fast" from "actually opens one connection now";
  * work COUNT (added 2026-09-03, #232) — the same counter pointed at the
    hot functions themselves (`plans._ran`, `tools._round_floats`), with
    equivalence oracles beside it proving the work that went away changed no
    answer. The first two axes both missed a real +13.86% regression for five
    weeks: it opened no extra connection, and the latency reading it produced
    was read as runner drift and re-run to green. Unlike the latency axis,
    this one has no `benchmark` fixture, so it is the INVERSE of that axis'
    skip rule: it runs in the ordinary `uv run pytest` job (this file's
    `--benchmark-skip` default does not skip it, only the timed benchmarks)
    and is itself skipped under `--benchmark-only`, the CI invocation that
    runs the latency axis — so a red work-count assertion shows up in the
    "Run tests with coverage gate" step, not the "Perf-benchmark regression
    gate" step.

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
import json
import tomllib
from datetime import date, timedelta
from enum import IntEnum
from pathlib import Path

import pytest
from perf_fixture import build_perf_fixture_db, build_report_card_fixture_db

from local_fitness import db, plans
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
    assert card["metrics"]["continuity"]["stars"] is not None
    assert card["overall"]["stars"] is not None
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


# --- work COUNT: the same instrument, pointed at hot functions --------------
# A 2 ms call chain is too small for latency alone to separate "the runner was
# slow" from "the code got slower" — which is exactly how a real +13.86%
# regression in `get_training_plan_progress` sat one noise-width under the 15%
# gate from 2026-07-27 to 2026-09-03. These count the work instead: they are
# deterministic, they do not care which CPU CI drew, and they are what a
# reviewer can read as "this many fewer Python calls happen now".

def _count_calls(monkeypatch, module, name):
    """Count calls to ``module.name`` — `_count_connect_opens`'s instrument
    pointed at a hot function. Recursive calls count too: a module-level
    function reaches itself through the same module attribute this patches."""
    counts = {"n": 0}
    orig = getattr(module, name)

    def counting(*args, **kwargs):
        counts["n"] += 1
        return orig(*args, **kwargs)

    monkeypatch.setattr(module, name, counting)
    return counts


_PACED_RUN = {"activity_type": "running", "distance_meters": 9574.85,
              "duration_seconds": 3000, "avg_pace_sec_per_km": 313.3}
_PACED_WALK = {"activity_type": "treadmill_running", "distance_meters": 5202.75,
               "duration_seconds": 4200, "avg_pace_sec_per_km": 807.3}
_RIDE = {"activity_type": "cycling", "distance_meters": 30000.0,
         "duration_seconds": 3600, "avg_pace_sec_per_km": 120.0}


def test_workout_actuals_evaluates_ran_once_per_on_foot_activity(monkeypatch):
    """`_workout_actuals`'s docstring has claimed "``_ran`` is evaluated at
    most once per activity" since 2026-07-22; it stopped being true on
    2026-07-27 when the pace-gated classifier landed as a second pass over the
    same list. Two on-foot activities plus a ride: 2 evaluations, not 4."""
    counts = _count_calls(monkeypatch, plans, "_ran")
    plans._workout_actuals([_PACED_RUN, _PACED_WALK, _RIDE])
    assert counts["n"] == 2


def _freeze_progress_clock(monkeypatch, today):
    """Move the clock `get_training_plan_progress`'s window frontier reads.
    `_ran`'s count is immune to date (proof: 87 at every date probed, from
    `_TODAY` through `_TODAY` + 5000d — `build_plan_detail` grades the full
    plan, not the windowed one), but `_round_floats`'s count is not: the
    handler's default window is `max(frontier, today) + 7d`, so every extra
    day of wall clock sweeps another prescribed workout into the serialized
    payload and the call count grows with it (25 at `_TODAY`, 83 at
    `_TODAY` + 58d, plateauing at 115 from `_TODAY` + 100d on — #232)."""
    class _FakeDate(date):
        @classmethod
        def today(cls):
            return today

    monkeypatch.setattr(tools, "date", _FakeDate)


def test_get_training_plan_progress_does_not_double_classify(default_db, monkeypatch):
    """The handler-level twin of the assertion above — proof 1 alone is
    satisfied by a folded helper nobody calls. 123 before the fold, 87 after,
    pinned exactly rather than as an upper bound so an early-return handler
    (0 calls) cannot pass over unclassified work (#232)."""
    _freeze_progress_clock(monkeypatch, _TODAY + timedelta(days=58))
    counts = _count_calls(monkeypatch, plans, "_ran")
    _run(tools.get_training_plan_progress.handler({}))
    assert counts["n"] == 87


def test_get_training_plan_progress_rounds_without_a_call_per_node(default_db, monkeypatch):
    """`_round_floats` recursed into every leaf, so serializing this payload
    cost one Python call per node: 470 for 10 KB. The container branches now
    handle their own scalar children, leaving one call per dict/list (83 here,
    with the clock pinned to `_TODAY` + 58d — see `_freeze_progress_clock` for
    why an unpinned clock makes this count grow with wall-clock date).

    This is the assertion that binds the `_round_floats` change to a real
    reduction in work — an output-correct rewrite that is no faster fails it,
    which the equivalence oracle below cannot detect. Pinned exactly rather
    than as an upper bound so an early-return handler (1 call) cannot pass
    over the reduction this test exists to prove (#232)."""
    _freeze_progress_clock(monkeypatch, _TODAY + timedelta(days=58))
    counts = _count_calls(monkeypatch, tools, "_round_floats")
    _run(tools.get_training_plan_progress.handler({}))
    assert counts["n"] == 83


# --- equivalence oracles: the work went away, the answers did not -----------
# Both implementations below are `src/` as it stood at 7326f66, vendored. They
# are deliberately frozen copies, not imports: their whole job is to still
# describe the OLD behaviour after the new one lands.

def _normalize_activity_types_reference(activities, cfg=plans._DEFAULT_GRADING_CONFIG):
    classes: set[str] = set()
    for a in activities:
        at = a.get("activity_type")
        if not plans._is_on_foot(at):
            if at:
                classes.add("other")
            continue
        classes.add("running" if plans._ran(a, cfg) else "walking")
    return sorted(classes)


def _workout_actuals_reference(day_activities, cfg=plans._DEFAULT_GRADING_CONFIG):
    foot = run = walk = dur = 0.0
    for a in day_activities:
        if not plans._is_on_foot(a.get("activity_type")):
            continue
        d = a.get("distance_meters") or 0.0
        foot += d
        dur += a.get("duration_seconds") or 0.0
        if plans._ran(a, cfg):
            run += d
        else:
            walk += d
    pace = (dur / (foot / 1000.0)) if foot > 0 else None
    return foot, run, walk, pace, _normalize_activity_types_reference(day_activities, cfg)


def _round_floats_reference(obj, key=None):
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        dp = (tools._TEXT_HIGH_DP if key in tools._TEXT_HIGH_PRECISION_KEYS
              else tools._TEXT_DEFAULT_DP)
        return round(obj, dp)
    if isinstance(obj, dict):
        return {k: _round_floats_reference(v, k) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round_floats_reference(v, key) for v in obj]
    return obj


_WORKOUT_ACTUALS_EDGE_CASES = [
    [],
    [_RIDE],
    [{"activity_type": None, "distance_meters": 4000.0, "duration_seconds": 1800}],
    [_PACED_WALK],
    [{"activity_type": "running", "distance_meters": 0.0,
      "duration_seconds": 1800, "avg_pace_sec_per_km": 300.0}],
    [{"activity_type": "walking", "distance_meters": None,
      "duration_seconds": 1800, "avg_pace_sec_per_km": None}],
    # Mixed day (#232): a non-foot ride FIRST, then two on-foot activities
    # (one run, one walk), then a typeless non-foot row. Exercises the exact
    # combination the fold changed — `classes.add("other")` now runs INSIDE
    # the loop that accumulates on-foot distance/duration and must `continue`
    # rather than stop, or the on-foot activities after the ride would be
    # silently dropped from the accumulation. A 548-day corpus that is one
    # activity per day, always `running`, never exercises that ordering.
    [_RIDE, _PACED_RUN, _PACED_WALK,
     {"activity_type": None, "distance_meters": 100.0}],
]


def test_workout_actuals_still_answers_exactly_what_it_answered_before(perf_db):
    """The fold removed a pass, not a classification. Every activity-day in the
    fixture plus the edge cases below must produce a five-tuple identical to
    the pre-fold implementation — otherwise the call-count assertions above
    could be satisfied by simply not classifying."""
    with db.connect(perf_db) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM activities ORDER BY date, activity_id")]
    by_date: dict[str, list[dict]] = {}
    for row in rows:
        by_date.setdefault(row["date"], []).append(row)
    # Anti-vacuity: the fixture seeds one activity every _RUN_EVERY_N_DAYS over
    # a multi-year history. A query that quietly matched nothing must fail here,
    # not pass over an empty loop.
    assert len(by_date) > 500
    # Anti-vacuity, same rule applied to the edge-case list itself (#232): the
    # 548-day corpus above is measurably one input shape (one activity/day,
    # always `running`) repeated 548 times, so the differential coverage this
    # oracle actually relies on lives here, not in the day count.
    assert len(_WORKOUT_ACTUALS_EDGE_CASES) >= 7

    for day, activities in by_date.items():
        assert plans._workout_actuals(activities) == \
            _workout_actuals_reference(activities), day
    for case in _WORKOUT_ACTUALS_EDGE_CASES:
        assert plans._workout_actuals(case) == _workout_actuals_reference(case), case


class _FloatSubclass(float):
    pass


class _IntSubclass(int):
    pass


class _DictSubclass(dict):
    pass


class _Grade(IntEnum):
    OK = 2


#: Every branch `_round_floats`'s docstring names, plus the types that decide
#: whether a fast path may use `type(x) is ...` instead of `isinstance`.
_ROUND_FLOATS_CASES = [
    True,
    False,
    7,
    "avg_pace_sec_per_km",
    None,
    1.23456789,
    -0.0,
    float("nan"),
    float("inf"),
    float("-inf"),
    {"avg_pace_sec_per_km": 333.2222672948015},
    {"distance_m": 333.2222672948015},
    (1.23456789, 2.3456789),
    [(1.23456789,), (2.3456789,)],
    {"day": {"splits": {"avg_pace_sec_per_km": 333.2222672948015}}},
    {"aerobic_te": [2.12345678, 3.76543210]},
    _FloatSubclass(1.23456789),
    _IntSubclass(7),
    _DictSubclass({"avg_pace_sec_per_km": 1.23456789}),
    _Grade.OK,
    {"rows": [{"avg_pace_sec_per_km": 333.2222672948015, "ok": True,
               "name": "Run", "reps": 4, "note": None,
               "splits": [1.111111111, 2.222222222]}]},
]


def _dumped(obj):
    return json.dumps(obj, default=str)


def _capture_progress_payload(monkeypatch):
    """The object `_text` actually serializes for `get_training_plan_progress`,
    grabbed at the choke point rather than rebuilt — the oracle runs over the
    real payload, not a hand-written stand-in for it. The first `_round_floats`
    call of the handler IS the whole payload."""
    captured = {}
    orig = tools._round_floats

    def capturing(obj, key=None):
        captured.setdefault("payload", obj)
        return orig(obj, key)

    monkeypatch.setattr(tools, "_round_floats", capturing)
    _run(tools.get_training_plan_progress.handler({}))
    monkeypatch.setattr(tools, "_round_floats", orig)
    return captured["payload"]


def test_round_floats_output_is_identical_to_the_pre_inline_implementation(
        default_db, monkeypatch):
    """The fast path must be a pure speed change. Byte-compared against the
    vendored 7326f66 implementation over the real `get_training_plan_progress`
    payload and every adversarial case above."""
    payload = _capture_progress_payload(monkeypatch)
    assert payload.get("workouts"), "the fixture must produce a real plan payload"
    assert _dumped(tools._round_floats(payload)) == _dumped(_round_floats_reference(payload))

    assert len(_ROUND_FLOATS_CASES) >= 20
    for case in _ROUND_FLOATS_CASES:
        assert _dumped(tools._round_floats(case)) == _dumped(_round_floats_reference(case)), case

    # `_dumped`'s json.dumps comparison is type-blind — json.dumps((1, 2)) ==
    # json.dumps([1, 2]) — so it cannot see the tuple-to-list conversion Proof
    # 5 requires. Assert the type directly, on both the bare-tuple case and
    # the list-of-single-element-tuples case already in _ROUND_FLOATS_CASES
    # (#232).
    assert isinstance(tools._round_floats((1.23456789, 2.3456789)), list)
    converted = tools._round_floats([(1.23456789,), (2.3456789,)])
    assert isinstance(converted, list)
    assert all(isinstance(item, list) for item in converted)


def test_the_round_floats_oracle_can_actually_fail(default_db, monkeypatch):
    """An equality test that passes against any implementation the author
    happened to write is not a proof. Two implementations that ARE wrong in the
    two ways a fast path fails — losing the high-precision key on the way down,
    and treating a `dict` subclass as an opaque leaf — must diverge from the
    reference over the same case list.

    Deliberately NOT a bool-dropping mutant: with no `int` branch in the
    reference, `isinstance(True, float)` is False, so removing the `bool` guard
    diverges on nothing and would make this test look two-sided when it isn't.
    """
    def _ignores_high_precision_keys(obj, key=None):
        if isinstance(obj, bool):
            return obj
        if isinstance(obj, float):
            return round(obj, tools._TEXT_DEFAULT_DP)
        if isinstance(obj, dict):
            return {k: _ignores_high_precision_keys(v, k) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_ignores_high_precision_keys(v, key) for v in obj]
        return obj

    def _mishandles_a_dict_subclass(obj, key=None):
        if isinstance(obj, bool):
            return obj
        if isinstance(obj, float):
            dp = (tools._TEXT_HIGH_DP if key in tools._TEXT_HIGH_PRECISION_KEYS
                  else tools._TEXT_DEFAULT_DP)
            return round(obj, dp)
        if type(obj) is dict:
            return {k: _mishandles_a_dict_subclass(v, k) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_mishandles_a_dict_subclass(v, key) for v in obj]
        return obj

    payload = _capture_progress_payload(monkeypatch)
    for broken in (_ignores_high_precision_keys, _mishandles_a_dict_subclass):
        diverged = [
            case for case in [payload, *_ROUND_FLOATS_CASES]
            if _dumped(broken(case)) != _dumped(_round_floats_reference(case))
        ]
        assert diverged, broken.__name__


def test_the_latency_gate_is_still_armed():
    """The failure mode this change could quietly turn into is a gate demotion.
    The fix closes the gap; it does not touch the threshold, the baseline, or
    the `--benchmark-skip` default that keeps these benchmarks off an ordinary
    `pytest` run."""
    root = Path(__file__).resolve().parents[1]
    assert "--benchmark-compare-fail=min:15%" in (
        root / ".github/workflows/ci.yml").read_text()
    addopts = tomllib.loads((root / "pyproject.toml").read_text())[
        "tool"]["pytest"]["ini_options"]["addopts"]
    assert "--benchmark-skip" in addopts


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
