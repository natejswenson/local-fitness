"""Per-sample HR trace: Garmin payload parsing, distance binning, caching, and
the chart-series choice that sits on top of them.

The binner is the part worth the most scrutiny — it is the only place where a
1700-sample stream becomes ~31 numbers a reader will draw conclusions from, so
every case here pins a value that would change if the bucketing were wrong.
"""
from __future__ import annotations

import sqlite3

import pytest

from local_fitness import db
from local_fitness.agent import report_card as rc
from local_fitness.agent import visuals
from local_fitness.ingest import details

MILE_M = 1609.344


def _payload(descriptors, samples):
    return {
        "metricDescriptors": descriptors,
        "activityDetailMetrics": [{"metrics": s} for s in samples],
    }


HR_DESC = [
    {"key": "sumDistance", "metricsIndex": 0},
    {"key": "directHeartRate", "metricsIndex": 1},
]


# --- parse_hr_samples: read channels BY NAME, never by position -------------

def test_parse_reads_channels_by_descriptor_index_not_position():
    # The channel order is device-dependent. Here HR sits at index 0 and
    # distance at 2, with a decoy at 1 — a positional reader would return
    # (100.0, 3) pairs, i.e. cadence as heart rate.
    descriptors = [
        {"key": "directHeartRate", "metricsIndex": 0},
        {"key": "directRunCadence", "metricsIndex": 1},
        {"key": "sumDistance", "metricsIndex": 2},
    ]
    got = details.parse_hr_samples(_payload(descriptors, [[145.0, 180.0, 500.0]]))
    assert got == [(500.0, 145)]


def test_parse_drops_zero_hr_and_null_distance_samples():
    # Zero HR is Garmin's "no reading" and a null distance is a pre-GPS-lock
    # sample. Both must vanish, not plot as a dip to zero.
    samples = [[100.0, 0.0], [None, 150.0], [300.0, 148.0], [400.0, None]]
    assert details.parse_hr_samples(_payload(HR_DESC, samples)) == [(300.0, 148)]


def test_parse_returns_empty_when_a_channel_is_absent():
    # An indoor activity with no distance channel: no trace, not a crash.
    only_hr = [{"key": "directHeartRate", "metricsIndex": 0}]
    assert details.parse_hr_samples(_payload(only_hr, [[150.0]])) == []


def test_parse_returns_empty_on_empty_and_malformed_payloads():
    assert details.parse_hr_samples({}) == []
    # A sample row shorter than the descriptor indices claims — truncated
    # response — is skipped rather than raising IndexError.
    assert details.parse_hr_samples(_payload(HR_DESC, [[500.0]])) == []


def test_parse_preserves_sample_order():
    samples = [[100.0, 120.0], [200.0, 130.0], [300.0, 140.0]]
    got = details.parse_hr_samples(_payload(HR_DESC, samples))
    assert got == [(100.0, 120), (200.0, 130), (300.0, 140)]


# --- bin_hr_trace: the 1700-samples-to-31-bars reduction --------------------

def test_bin_averages_within_a_bucket_rather_than_taking_the_last_value():
    # Three samples inside the first tenth (0-160.9m) averaging 130. A binner
    # that kept the last sample would report 150 and turn the chart into a
    # sampling artifact.
    samples = [(10.0, 120), (50.0, 120), (100.0, 150)]
    rows = rc.bin_hr_trace(samples, bin_mi=0.1)
    assert len(rows) == 1
    assert rows[0]["avg_hr"] == 130
    assert rows[0]["samples"] == 3


def test_bin_places_samples_in_the_bucket_their_distance_falls_in():
    tenth = 0.1 * MILE_M
    samples = [
        (0.0, 100),                 # bucket 0
        (tenth * 0.99, 110),        # bucket 0 (just under the boundary)
        (tenth * 1.01, 140),        # bucket 1
        (tenth * 2.5, 160),         # bucket 2
    ]
    rows = rc.bin_hr_trace(samples, bin_mi=0.1)
    assert [r["index"] for r in rows] == [1, 2, 3]
    assert [r["avg_hr"] for r in rows] == [105, 140, 160]
    assert rows[0]["start_mi"] == 0.0
    assert rows[1]["start_mi"] == 0.1


def test_bin_flags_a_short_trailing_bucket_partial():
    # A 3.06-mile run ends 0.06 into its 31st tenth. That bucket is real data
    # but not a full interval, and must be flagged so the chart can dim it.
    samples = [(0.0, 130), (3.00 * MILE_M, 150), (3.06 * MILE_M, 155)]
    rows = rc.bin_hr_trace(samples, bin_mi=0.1)
    assert rows[0]["partial"] is False
    last = rows[-1]
    assert last["partial"] is True
    assert last["end_mi"] == pytest.approx(3.06, abs=0.005)


def test_bin_skips_gaps_rather_than_plotting_zeros():
    # A GPS dropout / paused watch leaves an empty bucket between two real
    # ones. It must be absent, not a zero-height bar implying a dead heart.
    tenth = 0.1 * MILE_M
    samples = [(0.0, 130), (tenth * 3.5, 150)]
    rows = rc.bin_hr_trace(samples, bin_mi=0.1)
    assert [r["index"] for r in rows] == [1, 4]


def test_bin_handles_empty_and_degenerate_input():
    assert rc.bin_hr_trace([]) == []
    assert rc.bin_hr_trace([(100.0, 150)], bin_mi=0) == []
    # Negative cumulative distance is not physical; drop rather than bucket
    # into a negative index.
    assert rc.bin_hr_trace([(-5.0, 150)]) == []


def test_bin_width_is_configurable_and_changes_the_bucketing():
    samples = [(0.0, 100), (0.15 * MILE_M, 160)]
    tenths = rc.bin_hr_trace(samples, bin_mi=0.1)
    quarters = rc.bin_hr_trace(samples, bin_mi=0.25)
    assert len(tenths) == 2          # two separate tenths
    assert len(quarters) == 1        # both inside the first quarter mile
    assert quarters[0]["avg_hr"] == 130


# --- cache: fetch once, reuse forever, never pin a failure ------------------

@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "fitness.db"
    db.init_schema(path)
    with db.connect(path) as c:
        yield c


def test_store_then_load_round_trips_the_trace(conn):
    samples = [(0.0, 120), (100.0, 130), (200.0, 140)]
    details.store_hr_samples(conn, 42, samples)
    assert details.load_cached_hr_samples(conn, 42) == samples


def test_store_is_idempotent_on_refetch(conn):
    details.store_hr_samples(conn, 42, [(0.0, 120), (100.0, 130)])
    details.store_hr_samples(conn, 42, [(0.0, 125), (100.0, 135)])
    # INSERT OR REPLACE: the second write wins, no duplicate-key failure and
    # no doubled rows.
    assert details.load_cached_hr_samples(conn, 42) == [(0.0, 125), (100.0, 135)]


def test_get_hr_samples_uses_cache_and_makes_no_network_call(conn, monkeypatch):
    details.store_hr_samples(conn, 42, [(0.0, 120)])

    def _boom(_):
        raise AssertionError("fetch attempted despite a warm cache")

    monkeypatch.setattr(details, "fetch_hr_samples", _boom)
    assert details.get_hr_samples(conn, 42) == [(0.0, 120)]


def test_get_hr_samples_fetches_on_miss_and_caches_the_result(conn, monkeypatch):
    calls = []

    def _fetch(activity_id):
        calls.append(activity_id)
        return [(0.0, 118), (200.0, 141)]

    monkeypatch.setattr(details, "fetch_hr_samples", _fetch)
    assert details.get_hr_samples(conn, 7) == [(0.0, 118), (200.0, 141)]
    # Second call is served from the cache written by the first.
    assert details.get_hr_samples(conn, 7) == [(0.0, 118), (200.0, 141)]
    assert calls == [7]


def test_failed_fetch_caches_nothing_so_the_next_render_retries(conn, monkeypatch):
    calls = []

    def _fetch(activity_id):
        calls.append(activity_id)
        return []

    monkeypatch.setattr(details, "fetch_hr_samples", _fetch)
    assert details.get_hr_samples(conn, 9) == []
    assert details.get_hr_samples(conn, 9) == []
    # Two attempts, not one — an empty result must never be pinned as "known
    # to have no trace", or a transient outage disables the chart forever.
    assert calls == [9, 9]


def test_allow_fetch_false_stays_offline(conn, monkeypatch):
    monkeypatch.setattr(
        details, "fetch_hr_samples",
        lambda _: (_ for _ in ()).throw(AssertionError("fetched")))
    assert details.get_hr_samples(conn, 11, allow_fetch=False) == []


def test_fetch_hr_samples_swallows_client_failure(monkeypatch):
    # No credential / offline / expired token: best-effort contract means an
    # empty trace, never a raise that would sink the whole card.
    import local_fitness.ingest.daily as daily

    monkeypatch.setattr(
        daily, "_client",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no credential")))
    assert details.fetch_hr_samples(123) == []


def test_cache_write_failure_still_returns_the_fetched_trace(conn, monkeypatch):
    monkeypatch.setattr(details, "fetch_hr_samples", lambda _: [(0.0, 130)])

    def _boom(*a, **k):
        raise sqlite3.Error("disk full")

    monkeypatch.setattr(details, "store_hr_samples", _boom)
    # We already have the data in hand; a caching problem must not lose it.
    assert details.get_hr_samples(conn, 13) == [(0.0, 130)]


# --- chart series selection -------------------------------------------------

def _card(trace=None, splits_rows=None, avg_hr=140):
    return {
        "activity": {"avg_hr": avg_hr},
        "hr_trace": trace or [],
        "splits": {
            "available": bool(splits_rows),
            "unit": "Mile",
            "rows": splits_rows or [],
            "hr_drift_pct": None,
        },
    }


TRACE = [
    {"index": 1, "start_mi": 0.0, "end_mi": 0.1, "avg_hr": 130,
     "samples": 20, "partial": False},
    {"index": 2, "start_mi": 0.1, "end_mi": 0.16, "avg_hr": 150,
     "samples": 9, "partial": True},
]
SPLIT_ROWS = [
    {"index": 1, "avg_hr": 128, "partial": False},
    {"index": 2, "avg_hr": 149, "partial": True},
]


def test_chart_prefers_the_trace_and_puts_bars_on_a_distance_axis():
    s = visuals.hr_chart_series(_card(trace=TRACE, splits_rows=SPLIT_ROWS))
    assert s["source"] == "trace"
    # Positions are real miles, not bar ordinals dressed up as distance.
    assert s["positions"] == [0.0, 0.1]
    assert s["values"] == [130, 150]
    assert s["partials"] == [False, True]
    assert s["xlabel"] == "Distance (miles)"
    assert s["xmax"] == 0.16


def test_chart_title_states_the_resolution_actually_binned():
    s = visuals.hr_chart_series(_card(trace=TRACE))
    assert s["title"] == f"Heart rate every {rc.HR_TRACE_BIN_MI:g} mi"


def test_chart_falls_back_to_splits_when_no_trace():
    s = visuals.hr_chart_series(_card(splits_rows=SPLIT_ROWS))
    assert s["source"] == "splits"
    assert s["values"] == [128, 149]
    assert s["xlabel"] == "Mile"
    assert s["title"] == "Heart rate by mile"


def test_chart_returns_none_when_neither_series_exists():
    assert visuals.hr_chart_series(_card()) is None


def test_render_split_hr_png_draws_a_labeled_chart_from_a_trace():
    png = visuals.render_split_hr_png(_card(trace=TRACE))
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


def test_render_split_hr_png_raises_without_a_series():
    # The caller treats any exception as "skip the chart"; raising is the
    # contract, not returning an empty/blank PNG that would render as a
    # mysterious white box on the page.
    with pytest.raises(ValueError):
        visuals.render_split_hr_png(_card())


def test_missing_table_degrades_to_no_trace_instead_of_raising(tmp_path):
    """A pre-0.25.0 DB opened by a path that skipped init_schema must fall back
    to the per-lap chart, not sink the card with an OperationalError."""
    bare = tmp_path / "old.db"
    with sqlite3.connect(bare) as raw:
        raw.execute("CREATE TABLE activities (activity_id INTEGER PRIMARY KEY)")
    with db.connect(bare) as c:
        assert details.load_cached_hr_samples(c, 1) == []
        assert details.get_hr_samples(c, 1, allow_fetch=False) == []
