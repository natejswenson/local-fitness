"""Tests for ingest/baselines.py — rolling baselines + CTL/ATL/TSB."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from local_fitness import db
from local_fitness.ingest import baselines


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    return p


def test_ewma_factor_monotonic():
    # Shorter time constant → larger smoothing factor (more responsive).
    assert baselines._ewma_factor(7) > baselines._ewma_factor(42)


def test_sd_needs_two_points():
    assert baselines._sd([5.0]) is None
    assert baselines._sd([]) is None
    assert baselines._sd([2.0, 4.0]) == pytest.approx(1.4142135, rel=1e-4)
    # None values are filtered out
    assert baselines._sd([2.0, None, 4.0]) == pytest.approx(1.4142135, rel=1e-4)


def test_recompute_empty_db_writes_window(seeded_db):
    through = date(2026, 6, 6)
    n = baselines.recompute(through=through, lookback_days=10)
    assert n == 11  # inclusive
    # No activities → CTL/ATL/TSB stay NULL
    with db.connect(seeded_db) as conn:
        row = conn.execute(
            "SELECT ctl, atl, tsb FROM baselines WHERE date = ?", (through.isoformat(),)
        ).fetchone()
    assert row["ctl"] is None


def test_recompute_with_activities_and_metrics(seeded_db):
    through = date(2026, 6, 6)
    with db.connect(seeded_db) as conn:
        # Daily metrics across the baseline window for mean/SD.
        for i in range(70):
            d = (through - timedelta(days=i)).isoformat()
            conn.execute(
                "INSERT INTO daily_metrics (date, rhr, sleep_seconds, body_battery_max, "
                "body_battery_min, avg_stress) VALUES (?, ?, ?, ?, ?, ?)",
                (d, 50 + (i % 3), 27000, 90, 20, 30),
            )
        # A handful of runs feeding training load.
        for i in range(10):
            d = (through - timedelta(days=i)).isoformat()
            conn.execute(
                "INSERT INTO activities (activity_id, date, activity_type, training_load) "
                "VALUES (?, ?, 'running', ?)",
                (i, d, 80.0),
            )

    n = baselines.recompute(through=through, lookback_days=30)
    assert n == 31
    with db.connect(seeded_db) as conn:
        row = conn.execute(
            "SELECT rhr_60day_mean, rhr_60day_sd, ctl, atl, tsb "
            "FROM baselines WHERE date = ?", (through.isoformat(),)
        ).fetchone()
    assert row["rhr_60day_mean"] is not None
    assert row["rhr_60day_sd"] is not None
    assert row["ctl"] is not None and row["atl"] is not None
    # Fresh load: ATL (7-day) climbs faster than CTL (42-day) on a run streak,
    # so TSB (= CTL - ATL) is negative.
    assert row["tsb"] == pytest.approx(row["ctl"] - row["atl"])
    assert row["tsb"] < 0


def _reference_baselines_rows(db_path, start, today):
    """The RETIRED per-day algorithm (AVG scan + SD scan per target day),
    kept here as the equivalence oracle for the single-pass rewrite. Any
    divergence between this and ``build_baseline_rows`` is a regression in
    the rewrite, not a fixture quirk."""
    out = {}
    with db.connect(db_path) as conn:
        d = start
        while d <= today:
            d_str = d.isoformat()
            window_start = (d - timedelta(days=baselines.WINDOW_DAYS)).isoformat()
            stats = conn.execute(
                "SELECT AVG(rhr) AS rhr_mean, AVG(body_battery_max) AS bbmax_mean, "
                "AVG(body_battery_min) AS bbmin_mean, AVG(sleep_seconds) AS sleep_mean, "
                "AVG(avg_stress) AS stress_mean FROM daily_metrics "
                "WHERE date >= ? AND date < ?",
                (window_start, d_str),
            ).fetchone()
            sd_rows = conn.execute(
                "SELECT rhr, sleep_seconds FROM daily_metrics WHERE date >= ? AND date < ?",
                (window_start, d_str),
            ).fetchall()
            out[d_str] = (
                stats["rhr_mean"],
                baselines._sd([r["rhr"] for r in sd_rows]),
                stats["bbmax_mean"], stats["bbmin_mean"], stats["sleep_mean"],
                baselines._sd([r["sleep_seconds"] for r in sd_rows]),
                stats["stress_mean"],
            )
            d += timedelta(days=1)
    return out


def test_single_pass_recompute_matches_the_per_day_sql_reference(seeded_db):
    """Bit-identical, not approximately equal: every baselined column is an
    INTEGER, so the rolling int sums divide to the same float SQLite's AVG
    produces, and SD runs the same _sd over the same values."""
    through = date(2026, 6, 6)
    with db.connect(seeded_db) as conn:
        for i in range(80):  # spans past the 60-day window edge
            d = (through - timedelta(days=i)).isoformat()
            conn.execute(
                "INSERT INTO daily_metrics (date, rhr, sleep_seconds, body_battery_max, "
                "body_battery_min, avg_stress) VALUES (?, ?, ?, ?, ?, ?)",
                # Nulls sprinkled in: per-column NULL-skipping must match AVG.
                (d, None if i % 7 == 0 else 48 + (i % 5),
                 26000 + 100 * (i % 4), 88 + (i % 6),
                 None if i % 11 == 0 else 15 + (i % 3), 25 + (i % 9)),
            )
    baselines.recompute(through=through, lookback_days=30)
    expected = _reference_baselines_rows(seeded_db, through - timedelta(days=30), through)
    with db.connect(seeded_db) as conn:
        rows = conn.execute(
            "SELECT date, rhr_60day_mean, rhr_60day_sd, body_battery_max_60day_mean, "
            "body_battery_min_60day_mean, sleep_seconds_60day_mean, "
            "sleep_seconds_60day_sd, stress_60day_mean FROM baselines ORDER BY date"
        ).fetchall()
    assert len(rows) == 31
    for row in rows:
        exp = expected[row["date"]]
        got = (row["rhr_60day_mean"], row["rhr_60day_sd"],
               row["body_battery_max_60day_mean"], row["body_battery_min_60day_mean"],
               row["sleep_seconds_60day_mean"], row["sleep_seconds_60day_sd"],
               row["stress_60day_mean"])
        assert got == exp, f"divergence on {row['date']}"


def test_recompute_statement_count_is_constant_in_lookback(seeded_db, monkeypatch):
    """The rewrite's whole point: 3 statements total (tss rollup, metrics
    fetch, one executemany) no matter how far back the lookback reaches —
    the retired shape was 1 + 2 * lookback_days."""
    with db.connect(seeded_db) as conn:
        conn.execute(
            "INSERT INTO daily_metrics (date, rhr) VALUES ('2026-06-01', 50)")

    counts = {}
    real_connect = db.connect

    class _TracedConn:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, *a, **k):
            counts["execute"] = counts.get("execute", 0) + 1
            return self._conn.execute(*a, **k)

        def executemany(self, *a, **k):
            counts["executemany"] = counts.get("executemany", 0) + 1
            return self._conn.executemany(*a, **k)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    from contextlib import contextmanager

    @contextmanager
    def traced_connect(*a, **k):
        with real_connect(*a, **k) as conn:
            yield _TracedConn(conn)

    monkeypatch.setattr(db, "connect", traced_connect)
    for lookback in (30, 365):
        counts.clear()
        baselines.recompute(through=date(2026, 6, 6), lookback_days=lookback)
        assert counts == {"execute": 2, "executemany": 1}, (
            f"lookback={lookback}: {counts}")
