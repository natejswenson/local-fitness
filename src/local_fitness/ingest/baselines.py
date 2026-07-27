"""Compute rolling 60-day baselines plus Banister CTL/ATL/TSB training load.

CTL (chronic training load) = 42-day exponentially weighted moving average of
training load — your "fitness".
ATL (acute training load) = 7-day EWMA — your "fatigue".
TSB (training stress balance) = CTL - ATL — your "form".

We walk forward from the earliest activity so the EWMA is correctly seeded,
then write baselines + load for every date in the lookback window.
"""
from __future__ import annotations

import logging
import math
from collections import deque
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

from .. import db

LOG = logging.getLogger(__name__)

WINDOW_DAYS = 60
RECOMPUTE_LOOKBACK_DAYS = 90
ATL_TC = 7
CTL_TC = 42

# daily_metrics columns that get a rolling 60-day mean, in the order their
# `*_60day_mean` values appear in the baselines INSERT below.
MEAN_COLUMNS = (
    "rhr",
    "body_battery_max",
    "body_battery_min",
    "sleep_seconds",
    "avg_stress",
)
# The subset that also gets a rolling 60-day standard deviation.
SD_COLUMNS = ("rhr", "sleep_seconds")

_BASELINE_INSERT = """
    INSERT OR REPLACE INTO baselines (
        date, rhr_60day_mean, rhr_60day_sd,
        body_battery_max_60day_mean, body_battery_min_60day_mean,
        sleep_seconds_60day_mean, sleep_seconds_60day_sd,
        stress_60day_mean, ctl, atl, tsb
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _ewma_factor(tc: int) -> float:
    return 1 - math.exp(-1.0 / tc)


def _sd(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    m = sum(vals) / len(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))


def build_baseline_rows(
    metric_rows: Iterable[Any],
    start: date,
    today: date,
    load_by_date: dict[str, tuple[float, float, float]],
) -> list[tuple]:
    """Roll a 60-day window across ``[start, today]`` in ONE pass.

    Pure (no I/O): ``metric_rows`` is every ``daily_metrics`` row any target
    day's window can see — dates in ``[start - WINDOW_DAYS, today)`` — sorted
    ascending by date. Rows may be ``sqlite3.Row`` or plain dicts.

    Replaces the previous per-day pair of window queries (an ``AVG`` scan plus
    a second scan of the same rows for SD), which cost O(lookback) round trips:
    3 statements per day, so a backdated manual workout that widens the
    lookback to ~6 years issued ~6.5k statements to write ~2.2k rows.

    Two pointers walk the sorted rows as the window slides, so each row is
    admitted and evicted exactly once. Mean accumulators are running
    sums/counts; since every baselined column is an INTEGER, those sums stay
    exact Python ints and the resulting means are bit-identical to SQLite's
    ``AVG`` (which likewise skips NULLs and divides by its own non-NULL count).
    SD keeps the actual window values in a deque and re-runs ``_sd`` over them
    rather than a sum-of-squares shortcut: the shortcut suffers catastrophic
    cancellation on near-constant data (a window of identical sleep_seconds
    would yield ~2.7e-4, or a negative variance, instead of exactly 0.0), and
    only two columns need it.
    """
    rows = list(metric_rows)
    dates = [r["date"] for r in rows]
    n = len(rows)

    lo = hi = 0
    sums: dict[str, Any] = dict.fromkeys(MEAN_COLUMNS, 0)
    counts: dict[str, int] = dict.fromkeys(MEAN_COLUMNS, 0)
    windows: dict[str, deque] = {c: deque() for c in SD_COLUMNS}

    out: list[tuple] = []
    d = start
    while d <= today:
        d_str = d.isoformat()
        window_start = (d - timedelta(days=WINDOW_DAYS)).isoformat()

        # Admit everything now inside the window's right edge (date < d)...
        while hi < n and dates[hi] < d_str:
            row = rows[hi]
            for c in MEAN_COLUMNS:
                v = row[c]
                if v is not None:
                    sums[c] += v
                    counts[c] += 1
            for c in SD_COLUMNS:
                v = row[c]
                if v is not None:
                    windows[c].append(v)
            hi += 1
        # ...and evict what fell off the left edge (date < d - WINDOW_DAYS).
        while lo < hi and dates[lo] < window_start:
            row = rows[lo]
            for c in MEAN_COLUMNS:
                v = row[c]
                if v is not None:
                    sums[c] -= v
                    counts[c] -= 1
            for c in SD_COLUMNS:
                if row[c] is not None:
                    windows[c].popleft()
            lo += 1

        means = {
            c: (sums[c] / counts[c] if counts[c] else None) for c in MEAN_COLUMNS
        }
        ctl, atl, tsb = load_by_date.get(d_str, (None, None, None))
        out.append((
            d_str,
            means["rhr"], _sd(list(windows["rhr"])),
            means["body_battery_max"], means["body_battery_min"],
            means["sleep_seconds"], _sd(list(windows["sleep_seconds"])),
            means["avg_stress"], ctl, atl, tsb,
        ))
        d += timedelta(days=1)

    return out


def recompute(through: date | None = None, lookback_days: int = RECOMPUTE_LOOKBACK_DAYS) -> int:
    today = through or date.today()
    start = today - timedelta(days=lookback_days)

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT date, COALESCE(SUM(training_load), 0) AS tss "
            "FROM activities GROUP BY date ORDER BY date"
        ).fetchall()
        tss_by_date = {r["date"]: r["tss"] for r in rows}

        if not tss_by_date:
            LOG.info("No activities yet; skipping training load.")
            load_by_date: dict[str, tuple[float, float, float]] = {}
        else:
            first = date.fromisoformat(min(tss_by_date.keys()))
            ctl = atl = 0.0
            cf = _ewma_factor(CTL_TC)
            af = _ewma_factor(ATL_TC)
            load_by_date = {}
            d = first
            while d <= today:
                tss = tss_by_date.get(d.isoformat(), 0.0)
                ctl = ctl + (tss - ctl) * cf
                atl = atl + (tss - atl) * af
                load_by_date[d.isoformat()] = (ctl, atl, ctl - atl)
                d += timedelta(days=1)

        # ONE fetch of every row any target day's window can see, then the
        # pure single-pass roll (see build_baseline_rows). The old shape was
        # an AVG scan + a second SD scan of the SAME window per target day —
        # 3 statements/day, so a backdated manual workout widening the
        # lookback to years issued thousands of statements for one write.
        metric_rows = conn.execute(
            "SELECT date, rhr, body_battery_max, body_battery_min, "
            "sleep_seconds, avg_stress FROM daily_metrics "
            "WHERE date >= ? AND date < ? ORDER BY date",
            (
                (start - timedelta(days=WINDOW_DAYS)).isoformat(),
                today.isoformat(),
            ),
        ).fetchall()
        baseline_rows = build_baseline_rows(metric_rows, start, today, load_by_date)
        conn.executemany(_BASELINE_INSERT, baseline_rows)
        updates = len(baseline_rows)

    LOG.info("Recomputed baselines for %d dates through %s", updates, today)
    return updates
