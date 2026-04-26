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
from datetime import date, timedelta

from .. import db

LOG = logging.getLogger(__name__)

WINDOW_DAYS = 60
RECOMPUTE_LOOKBACK_DAYS = 90
ATL_TC = 7
CTL_TC = 42


def _ewma_factor(tc: int) -> float:
    return 1 - math.exp(-1.0 / tc)


def _sd(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    m = sum(vals) / len(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))


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

        updates = 0
        d = start
        while d <= today:
            d_str = d.isoformat()
            window_start = (d - timedelta(days=WINDOW_DAYS)).isoformat()

            stats = conn.execute(
                """
                SELECT
                    AVG(rhr)              AS rhr_mean,
                    AVG(body_battery_max) AS bbmax_mean,
                    AVG(body_battery_min) AS bbmin_mean,
                    AVG(sleep_seconds)    AS sleep_mean,
                    AVG(avg_stress)       AS stress_mean
                FROM daily_metrics
                WHERE date >= ? AND date < ?
                """,
                (window_start, d_str),
            ).fetchone()

            sd_rows = conn.execute(
                "SELECT rhr, sleep_seconds FROM daily_metrics WHERE date >= ? AND date < ?",
                (window_start, d_str),
            ).fetchall()
            rhr_sd = _sd([r["rhr"] for r in sd_rows])
            sleep_sd = _sd([r["sleep_seconds"] for r in sd_rows])

            ctl, atl, tsb = load_by_date.get(d_str, (None, None, None))

            conn.execute(
                """
                INSERT OR REPLACE INTO baselines (
                    date, rhr_60day_mean, rhr_60day_sd,
                    body_battery_max_60day_mean, body_battery_min_60day_mean,
                    sleep_seconds_60day_mean, sleep_seconds_60day_sd,
                    stress_60day_mean, ctl, atl, tsb
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    d_str,
                    stats["rhr_mean"], rhr_sd,
                    stats["bbmax_mean"], stats["bbmin_mean"],
                    stats["sleep_mean"], sleep_sd,
                    stats["stress_mean"], ctl, atl, tsb,
                ),
            )
            updates += 1
            d += timedelta(days=1)

    LOG.info("Recomputed baselines for %d dates through %s", updates, today)
    return updates
