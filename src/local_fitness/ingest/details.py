"""On-demand fetch of one activity's per-sample HR trace.

Deliberately NOT part of the daily sync. `daily.py` pulls a summary row plus
per-lap splits for every activity it sees; this module pulls the ~1700-sample
detail payload for exactly one activity, and only when something actually asks
for it (today: the report card's per-tenth-mile HR chart). Two reasons that
split is load-bearing rather than stylistic:

- **Volume.** 747 activities x ~1700 samples to serve a feature that reads one
  activity at a time is a backfill nobody asked for.
- **Rate limiting.** Repeated per-activity detail calls are the same shape that
  produced `Mobile login returned 429` before the token cache landed. One call,
  cached to SQLite forever after, keeps that surface at its minimum.

Every function here is best-effort by contract: a missing credential, an
expired token, no network, or a Garmin payload that simply has no HR channel
all return "no samples" rather than raising. The report card degrades to its
per-mile chart, which is the behavior that existed before this module.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import NamedTuple

LOG = logging.getLogger(__name__)


class HrSample(NamedTuple):
    """One point on an activity's trace.

    A NamedTuple rather than a dict so it stays cheap and tuple-comparable at
    ~1700 per activity, while `s.elapsed_s` beats `s[2]` at the call sites that
    compute pace. `elapsed_s` is None on a payload with no duration channel —
    the HR bars still render, only the pace line drops out.
    """
    distance_m: float
    hr: int
    elapsed_s: float | None

# Garmin caps its own chart resolution; 2000 covers a marathon at ~8s spacing
# and is the library's default. Named here so the intent is explicit rather
# than inherited silently from garminconnect's signature.
MAX_CHART_SAMPLES = 2000

# The two metric channels we need, by `metricDescriptors[].key`. Reading them
# by NAME is not optional: `activityDetailMetrics[].metrics` is a positional
# array whose column order varies by device and activity type, so a hardcoded
# index would silently read cadence as heart rate on some other watch.
_HR_KEY = "directHeartRate"
_DISTANCE_KEY = "sumDistance"
# Preference order for the time channel behind the pace line. `sumDuration`
# first because it is what the activity summary's average pace is computed
# from — the chart must not contradict the "9:28/mi" printed in the table
# above it. `sumMovingDuration` excludes pauses and would read faster.
_DURATION_KEYS = ("sumDuration", "sumElapsedDuration", "sumMovingDuration")


def parse_hr_samples(details: dict) -> list[HrSample]:
    """`HrSample`s from a Garmin activity-details payload, in order, skipping
    samples missing distance or HR.

    Pure — no I/O, no SDK, unit-testable with a hand-built dict. Returns an
    empty list for any payload shape that doesn't carry both required channels
    (an indoor activity with no distance, a device with no optical HR, or a
    truncated response), which the caller treats as "no trace available". The
    duration channel is OPTIONAL: without it every sample carries
    `elapsed_s=None` and the chart drops its pace line but keeps the HR bars.
    """
    descriptors = details.get("metricDescriptors") or []
    index_of: dict[str, int] = {}
    for d in descriptors:
        key, idx = d.get("key"), d.get("metricsIndex")
        if isinstance(key, str) and isinstance(idx, int):
            index_of[key] = idx

    hr_i, dist_i = index_of.get(_HR_KEY), index_of.get(_DISTANCE_KEY)
    if hr_i is None or dist_i is None:
        return []
    dur_i = next((index_of[k] for k in _DURATION_KEYS if k in index_of), None)

    needed = max(i for i in (hr_i, dist_i, dur_i) if i is not None)
    out: list[HrSample] = []
    for sample in details.get("activityDetailMetrics") or []:
        metrics = sample.get("metrics") or []
        if len(metrics) <= needed:
            continue
        hr, dist = metrics[hr_i], metrics[dist_i]
        # A zero HR is Garmin's "no reading", not a real heartbeat; a null
        # distance is a sample recorded before GPS lock. Both are dropped
        # rather than plotted as a dip to zero.
        if hr is None or dist is None or hr <= 0:
            continue
        t = None if dur_i is None else metrics[dur_i]
        out.append(HrSample(float(dist), int(hr),
                            None if t is None else float(t)))
    return out


def load_cached_hr_samples(
    conn: sqlite3.Connection, activity_id: int
) -> list[HrSample]:
    """Previously-fetched samples for this activity, or `[]` if none stored.

    A missing `activity_hr_samples` table returns `[]` rather than raising:
    `init_schema()` creates it on every documented entry point, but a
    connection opened against a pre-0.25.0 DB by some path that skipped that
    call must degrade to the per-lap chart, not sink the card.
    """
    try:
        rows = conn.execute(
            "SELECT distance_meters, hr, elapsed_seconds FROM activity_hr_samples "
            "WHERE activity_id = ? ORDER BY sample_index",
            (activity_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        LOG.warning("activity_hr_samples unavailable — run `fitness setup` to "
                    "create it; falling back to per-lap splits", exc_info=True)
        return []
    return [
        HrSample(
            float(r["distance_meters"]), int(r["hr"]),
            None if r["elapsed_seconds"] is None else float(r["elapsed_seconds"]),
        )
        for r in rows
        if r["distance_meters"] is not None and r["hr"] is not None
    ]


def store_hr_samples(
    conn: sqlite3.Connection, activity_id: int, samples: list[HrSample]
) -> None:
    """Persist a fetched trace. `INSERT OR REPLACE` so a re-fetch is idempotent
    rather than a duplicate-key failure."""
    conn.executemany(
        "INSERT OR REPLACE INTO activity_hr_samples "
        "(activity_id, sample_index, distance_meters, hr, elapsed_seconds) "
        "VALUES (?,?,?,?,?)",
        [(activity_id, i, s.distance_m, s.hr, s.elapsed_s)
         for i, s in enumerate(samples)],
    )


def fetch_hr_samples(activity_id: int) -> list[HrSample]:
    """Fetch one activity's HR trace from Garmin. Best-effort: returns `[]` on
    any failure (no credential, expired token, offline, malformed payload).

    The garminconnect client is imported inside the body, matching `daily.py`'s
    own deferred-import posture — the always-running web server must not pay
    that import cost for a stdio-only PDF feature it never invokes.
    """
    try:
        from .daily import _client

        details = _client().get_activity_details(
            activity_id, maxchart=MAX_CHART_SAMPLES, maxpoly=0
        )
        return parse_hr_samples(details or {})
    except Exception:  # noqa: BLE001 — advisory data; the card renders without it
        LOG.warning(
            "HR-trace fetch failed for activity %s (card will use per-mile splits)",
            activity_id, exc_info=True,
        )
        return []


def get_hr_samples(
    conn: sqlite3.Connection, activity_id: int, *, allow_fetch: bool = True
) -> list[HrSample]:
    """Cached-then-fetched HR trace for one activity.

    Cache hit returns immediately with no network call. On a miss, fetches once
    and stores the result; a failed fetch stores nothing, so the next render
    retries rather than pinning an empty trace forever. `allow_fetch=False`
    restricts this to the cache, which is what tests and any offline caller
    want.
    """
    cached = load_cached_hr_samples(conn, activity_id)
    if cached or not allow_fetch:
        return cached

    samples: list[HrSample] = fetch_hr_samples(activity_id)
    if samples:
        try:
            store_hr_samples(conn, activity_id, samples)
            conn.commit()
        except sqlite3.Error:  # includes a missing table on a pre-0.25.0 DB
            # A cache-write failure must not lose the trace we already have.
            LOG.warning("HR-trace cache write failed for %s", activity_id, exc_info=True)
    return samples


__all__ = [
    "HrSample", "parse_hr_samples", "load_cached_hr_samples", "store_hr_samples",
    "fetch_hr_samples", "get_hr_samples", "MAX_CHART_SAMPLES",
]
