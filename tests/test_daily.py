"""Tests for ingest/daily.py — Garmin daily pull transforms + status machine.

The Garmin network client is the only real-world dependency and it's passed
into ``_ingest_day`` / ``_ingest_activity_range`` as a param, and produced by
``_client`` in ``pull()`` — both clean seams. We hand-roll a tiny ``FakeGarmin``
stub (no mock library) that returns fabricated dicts, and exercise:

  * ``_to_int`` / ``_to_real`` coercion
  * the two ingest transforms against a real tmp SQLite DB
  * ``pull()``'s gap-math, freshness window, deferred cap, and the
    success/partial/auth_failure/not_configured/skipped status state machine
    plus the ``ingest_runs`` lifecycle

What we deliberately do NOT cover: ``_client()``'s *real network* login and
``client.get_*`` calls, and ``time.sleep`` throttling (patched to a no-op). We
DO cover ``_tokenstore_path()`` resolution and that ``_client`` threads that
path into ``client.login()`` (the session-token-reuse seam that stops the
per-pull 429).
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from garminconnect import GarminConnectAuthenticationError

from local_fitness import db
from local_fitness.ingest import daily


# --------------------------------------------------------------------------- #
# Fixtures + fake Garmin client
# --------------------------------------------------------------------------- #
@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    # time.sleep is pure throttle glue — never block the suite on it.
    monkeypatch.setattr(daily.time, "sleep", lambda *a, **k: None)
    return p


SUMMARY = {
    "restingHeartRate": 52,
    "averageStressLevel": 30,
    "maxStressLevel": 88,
    "totalSteps": "9000",  # string → exercises _to_int coercion
    "activeKilocalories": 450,
    "floorsAscended": 12,
    "averageSpo2": 97,
    "avgWakingRespirationValue": "14.5",  # string → _to_real coercion
    "moderateIntensityMinutes": 20,
    "vigorousIntensityMinutes": 5,
}

SLEEP = {
    "dailySleepDTO": {
        "sleepTimeSeconds": 27000,
        "deepSleepSeconds": 6000,
        "lightSleepSeconds": 15000,
        "remSleepSeconds": 5000,
        "awakeSleepSeconds": 1000,
        "sleepScores": {"overall": {"value": 82, "qualifierKey": "good"}},
    }
}

BODY_BATTERY = [
    {
        "min": 20,
        "max": 95,
        "charged": 60,
        "drained": 40,
        "bodyBatteryValuesArray": [
            [1700000000000, 50],
            [1700000300000, 55],
            ["malformed"],  # len < 2 → skipped by the guard
        ],
    }
]

MAX_METRICS = [{"generic": {"vo2MaxValue": 52.0}}]

STRESS = {
    "stressValuesArray": [
        [1700000000000, 25],
        [1700000300000, 30],
        ["bad"],  # len < 2 → skipped
    ]
}

ACTIVITY = {
    "activityId": 111,
    "startTimeLocal": "2026-06-20 07:00:00",
    "activityType": {"typeKey": "running"},
    "activityName": "Morning Run",
    "duration": 3600.0,
    "movingDuration": 3500,
    "distance": 10000.0,
    "averageHR": 150,
    "maxHR": 175,
    "averageSpeed": 2.5,  # m/s → pace 1000/2.5 = 400 sec/km
    "elevationGain": 100.0,
    "elevationLoss": 95.0,
    "calories": 600,
    "aerobicTrainingEffect": 3.5,
    "anaerobicTrainingEffect": 0.5,
    "activityTrainingLoad": 120.0,
    "averageRunningCadenceInStepsPerMinute": 170,
    "vO2MaxValue": 52.0,
    "temperature": 18.0,
    "weatherTypeDTO": {"desc": "Clear"},
}

HR_ZONES = [
    {"zoneNumber": 1, "secsInZone": 600},
    {"zoneNumber": 2, "secsInZone": 1200},
    "bad",  # non-dict → skipped
]

SPLITS = {
    "lapDTOs": [
        {
            "distance": 1000.0,
            "duration": 400,
            "averageHR": 150,
            "averageSpeed": 2.5,  # pace 400 sec/km
            "elevationGain": 10.0,
        },
        "bad",  # non-dict → skipped
    ]
}


class FakeGarmin:
    """Hand-rolled stand-in for ``garminconnect.Garmin``.

    Returns fabricated payloads. ``poison_bb_dates`` makes ``get_body_battery``
    hand back a sample with a non-numeric timestamp so ``_ingest_day`` raises
    *outside* ``_safe`` (the sample loop is unguarded) — the seam we use to make
    a single day fail without touching the source.
    """

    def __init__(
        self,
        *,
        summary=SUMMARY,
        sleep=SLEEP,
        body_battery=BODY_BATTERY,
        max_metrics=MAX_METRICS,
        stress=STRESS,
        activities=None,
        hr_zones=HR_ZONES,
        splits=SPLITS,
        poison_bb_dates=None,
        auth_fail_sleep_dates=None,
    ):
        self._summary = summary
        self._sleep = sleep
        self._body_battery = body_battery
        self._max_metrics = max_metrics
        self._stress = stress
        self._activities = activities if activities is not None else [ACTIVITY]
        self._hr_zones = hr_zones
        self._splits = splits
        self._poison = set(poison_bb_dates or ())
        # Dates on which get_sleep_data raises an auth error mid-run (token
        # expiry), letting us prove the run aborts instead of swallowing it.
        self._auth_fail_sleep = set(auth_fail_sleep_dates or ())
        self.summary_calls: list[str] = []
        self.activity_range_calls: list[tuple[str, str]] = []

    # wellness ---------------------------------------------------------------
    def get_user_summary(self, cdate):
        self.summary_calls.append(cdate)
        return self._summary

    def get_sleep_data(self, cdate):
        if cdate in self._auth_fail_sleep:
            raise GarminConnectAuthenticationError("token expired mid-run")
        return self._sleep

    def get_body_battery(self, start, end):
        if start in self._poison:
            return [{"bodyBatteryValuesArray": [["not-a-number", 50]]}]
        return self._body_battery

    def get_max_metrics(self, cdate):
        return self._max_metrics

    def get_stress_data(self, cdate):
        return self._stress

    # activities -------------------------------------------------------------
    def get_activities_by_date(self, start, end):
        self.activity_range_calls.append((start, end))
        return self._activities

    def get_activity_hr_in_timezones(self, activity_id):
        return self._hr_zones

    def get_activity_splits(self, activity_id):
        return self._splits


# --------------------------------------------------------------------------- #
# _to_int / _to_real coercion
# --------------------------------------------------------------------------- #
def test_to_int_coercion():
    assert daily._to_int(None) is None
    assert daily._to_int(5) == 5
    assert daily._to_int("7") == 7
    assert daily._to_int(3.9) == 3  # truncates
    assert daily._to_int("nope") is None
    assert daily._to_int([1, 2]) is None  # TypeError path


def test_to_real_coercion():
    assert daily._to_real(None) is None
    assert daily._to_real(2) == 2.0
    assert daily._to_real("14.5") == 14.5
    assert daily._to_real("nope") is None
    assert daily._to_real({}) is None  # TypeError path


# --------------------------------------------------------------------------- #
# _ingest_day
# --------------------------------------------------------------------------- #
def test_ingest_day_full_transform(seeded_db):
    d = date(2026, 6, 20)
    fake = FakeGarmin()
    with db.connect(seeded_db) as conn:
        daily._ingest_day(fake, conn, d)

    with db.connect(seeded_db) as conn:
        row = conn.execute(
            "SELECT * FROM daily_metrics WHERE date = ?", (d.isoformat(),)
        ).fetchone()
        bb_n = conn.execute(
            "SELECT COUNT(*) AS n FROM body_battery_samples WHERE date = ?",
            (d.isoformat(),),
        ).fetchone()["n"]
        stress_n = conn.execute(
            "SELECT COUNT(*) AS n FROM stress_samples WHERE date = ?",
            (d.isoformat(),),
        ).fetchone()["n"]

    assert row["rhr"] == 52
    assert row["sleep_seconds"] == 27000
    assert row["sleep_deep_seconds"] == 6000
    assert row["sleep_score"] == 82
    assert row["sleep_quality"] == "good"
    assert row["steps"] == 9000  # string coerced
    assert row["avg_stress"] == 30
    assert row["max_stress"] == 88
    assert row["body_battery_min"] == 20
    assert row["body_battery_max"] == 95
    assert row["body_battery_charged"] == 60
    assert row["respiration_avg"] == 14.5
    assert row["vo2_max"] == 52.0
    assert row["intensity_minutes_moderate"] == 20
    # Sample arrays: the malformed entries are skipped by the length guard.
    assert bb_n == 2
    assert stress_n == 2


def test_ingest_day_all_core_endpoints_fail_raises_and_writes_no_row(seeded_db):
    """Both core endpoints None → raise DayIngestFailure, write NO row.

    An all-NULL row would mark the gap permanently filled and let pull() report
    success on a day it saved nothing.
    """
    d = date(2026, 6, 21)
    fake = FakeGarmin(
        summary=None,
        sleep=None,
        body_battery=None,
        max_metrics=None,
        stress=None,
    )
    with db.connect(seeded_db) as conn:
        with pytest.raises(daily.DayIngestFailure):
            daily._ingest_day(fake, conn, d)

    with db.connect(seeded_db) as conn:
        row = conn.execute(
            "SELECT * FROM daily_metrics WHERE date = ?", (d.isoformat(),)
        ).fetchone()
    assert row is None  # the date is left missing so a later pull retries it


def test_ingest_day_partial_endpoints_still_write(seeded_db):
    """Summary succeeds but the rest fail → row IS written with what arrived.

    Only when EVERY core endpoint fails do we skip the write; a single working
    endpoint keeps the day best-effort.
    """
    d = date(2026, 6, 21)
    fake = FakeGarmin(
        summary={"restingHeartRate": 48, "totalSteps": 5000},
        sleep=None,
        body_battery=None,
        max_metrics=None,
        stress=None,
    )
    with db.connect(seeded_db) as conn:
        daily._ingest_day(fake, conn, d)

    with db.connect(seeded_db) as conn:
        row = conn.execute(
            "SELECT * FROM daily_metrics WHERE date = ?", (d.isoformat(),)
        ).fetchone()
    assert row is not None
    assert row["rhr"] == 48
    assert row["steps"] == 5000
    assert row["sleep_seconds"] is None  # sleep endpoint failed


def test_ingest_day_coalesce_never_clobbers_finalized_values(seeded_db):
    """A freshness re-pull with a failed endpoint must not wipe a good column.

    Seed a full row, then re-ingest the same date with sleep fetch failing
    (summary still works). The finalized sleep values must survive.
    """
    d = date(2026, 6, 22)
    with db.connect(seeded_db) as conn:
        daily._ingest_day(FakeGarmin(), conn, d)  # full row, sleep_seconds=27000
    with db.connect(seeded_db) as conn:
        daily._ingest_day(
            FakeGarmin(summary={"restingHeartRate": 55}, sleep=None), conn, d
        )
    with db.connect(seeded_db) as conn:
        row = conn.execute(
            "SELECT rhr, sleep_seconds, sleep_score FROM daily_metrics WHERE date = ?",
            (d.isoformat(),),
        ).fetchone()
    assert row["rhr"] == 55  # fresh non-NULL value wins
    assert row["sleep_seconds"] == 27000  # NOT clobbered to NULL by the failed fetch
    assert row["sleep_score"] == 82


def test_ingest_day_insert_or_replace_overwrites(seeded_db):
    d = date(2026, 6, 22)
    with db.connect(seeded_db) as conn:
        daily._ingest_day(FakeGarmin(summary={"restingHeartRate": 99}), conn, d)
    with db.connect(seeded_db) as conn:
        daily._ingest_day(FakeGarmin(summary={"restingHeartRate": 52}), conn, d)
    with db.connect(seeded_db) as conn:
        rows = conn.execute(
            "SELECT rhr FROM daily_metrics WHERE date = ?", (d.isoformat(),)
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["rhr"] == 52


# --------------------------------------------------------------------------- #
# _ingest_activity_range
# --------------------------------------------------------------------------- #
def test_ingest_activity_range_full_transform(seeded_db):
    fake = FakeGarmin(
        activities=[
            ACTIVITY,
            "not-a-dict",  # skipped
            {"activityName": "no id"},  # missing activityId → skipped
        ]
    )
    with db.connect(seeded_db) as conn:
        n = daily._ingest_activity_range(
            fake, conn, date(2026, 6, 20), date(2026, 6, 20)
        )
    assert n == 1  # only the valid activity counted

    with db.connect(seeded_db) as conn:
        act = conn.execute(
            "SELECT * FROM activities WHERE activity_id = 111"
        ).fetchone()
        zones = conn.execute(
            "SELECT * FROM activity_hr_zones WHERE activity_id = 111 ORDER BY zone"
        ).fetchall()
        splits = conn.execute(
            "SELECT * FROM activity_splits WHERE activity_id = 111"
        ).fetchall()

    assert act["date"] == "2026-06-20"
    assert act["activity_type"] == "running"
    assert act["distance_meters"] == 10000.0
    assert act["avg_hr"] == 150
    assert act["avg_pace_sec_per_km"] == pytest.approx(400.0)
    assert act["avg_cadence"] == 170
    assert act["weather_conditions"] == "Clear"
    assert act["training_load"] == 120.0
    # malformed zone/lap entries dropped.
    assert len(zones) == 2
    assert zones[0]["seconds_in_zone"] == 600
    assert len(splits) == 1
    assert splits[0]["avg_pace_sec_per_km"] == pytest.approx(400.0)


def test_ingest_activity_range_zero_speed_pace_is_null(seeded_db):
    act = dict(ACTIVITY, activityId=222, averageSpeed=0)
    fake = FakeGarmin(activities=[act], hr_zones=None, splits=None)
    with db.connect(seeded_db) as conn:
        n = daily._ingest_activity_range(
            fake, conn, date(2026, 6, 20), date(2026, 6, 20)
        )
    assert n == 1
    with db.connect(seeded_db) as conn:
        row = conn.execute(
            "SELECT avg_pace_sec_per_km FROM activities WHERE activity_id = 222"
        ).fetchone()
    assert row["avg_pace_sec_per_km"] is None  # guarded division-by-zero


def test_ingest_activity_range_non_list_returns_zero(seeded_db):
    # A truthy non-list payload survives `_safe(...) or []` and hits the
    # isinstance guard's early return.
    fake = FakeGarmin(activities={"unexpected": "shape"})
    with db.connect(seeded_db) as conn:
        n = daily._ingest_activity_range(
            fake, conn, date(2026, 6, 20), date(2026, 6, 20)
        )
    assert n == 0


# --------------------------------------------------------------------------- #
# pull() — status state machine + gap math + ingest_runs lifecycle
# --------------------------------------------------------------------------- #
def _latest_run(p):
    with db.connect(p) as conn:
        return conn.execute(
            "SELECT * FROM ingest_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()


def test_pull_success_full_window(seeded_db, monkeypatch):
    today = date(2026, 6, 25)
    # Shrink the backfill horizon so the gap-aware target list is just a few
    # days, not five years back to the Instinct launch.
    monkeypatch.setattr(daily, "EARLIEST_BACKFILL_DATE", today - timedelta(days=2))
    fake = FakeGarmin()
    monkeypatch.setattr(daily, "_client", lambda *a, **k: fake)

    res = daily.pull(through=today)

    assert res["status"] == "success"
    assert res["days_pulled"] == 3  # today, -1, -2
    assert res["gap_days_remaining"] == 0
    assert res["deferred_count"] == 0
    assert res["last_date"] == today.isoformat()
    assert res["activities_loaded"] == 1
    # Activities pulled once across the bounding range.
    assert fake.activity_range_calls == [
        ((today - timedelta(days=2)).isoformat(), today.isoformat())
    ]
    run = _latest_run(seeded_db)
    assert run["status"] == "success"
    assert run["completed_at"] is not None
    assert run["last_date_fetched"] == today.isoformat()
    assert run["error_message"] is None
    assert run["source"] == "daily"


def test_pull_partial_when_days_deferred(seeded_db, monkeypatch):
    today = date(2026, 6, 25)
    monkeypatch.setattr(daily, "EARLIEST_BACKFILL_DATE", today - timedelta(days=4))
    fake = FakeGarmin(activities=[])
    monkeypatch.setattr(daily, "_client", lambda *a, **k: fake)

    # 5 missing days, cap at 2 → 3 deferred, gap remains → partial.
    res = daily.pull(through=today, max_days=2)

    assert res["status"] == "partial"
    assert res["days_pulled"] == 2
    assert res["deferred_count"] == 3
    assert res["gap_days_remaining"] == 3
    assert "still missing" in res["error"]
    # Most-recent-first: the two newest dates were pulled.
    assert res["last_date"] == today.isoformat()
    run = _latest_run(seeded_db)
    assert run["status"] == "partial"


def test_pull_partial_when_a_day_fails(seeded_db, monkeypatch):
    today = date(2026, 6, 25)
    monkeypatch.setattr(daily, "EARLIEST_BACKFILL_DATE", today - timedelta(days=2))
    # Poison one day's body-battery payload so _ingest_day raises for it.
    bad_day = (today - timedelta(days=1)).isoformat()
    fake = FakeGarmin(activities=[], poison_bb_dates={bad_day})
    monkeypatch.setattr(daily, "_client", lambda *a, **k: fake)

    res = daily.pull(through=today)

    assert res["status"] == "partial"
    assert res["days_pulled"] == 2  # 3 targeted, 1 failed
    assert bad_day in res["error"]
    assert "failed" in res["error"]
    run = _latest_run(seeded_db)
    assert run["status"] == "partial"


def test_pull_all_endpoints_fail_never_reports_success(seeded_db, monkeypatch):
    """A run where every day's core endpoints fail must not report success.

    Before the fix, each day wrote an all-NULL row, missing_daily_dates saw the
    rows → gap_after=0, days_failed stayed empty → status='success' on a pull
    that saved nothing. Now the days land in days_failed, no rows are written,
    and the dates stay missing.
    """
    today = date(2026, 6, 25)
    monkeypatch.setattr(daily, "EARLIEST_BACKFILL_DATE", today - timedelta(days=2))
    fake = FakeGarmin(
        summary=None, sleep=None, body_battery=None, max_metrics=None,
        stress=None, activities=[],
    )
    monkeypatch.setattr(daily, "_client", lambda *a, **k: fake)

    res = daily.pull(through=today)

    assert res["status"] != "success"
    assert res["status"] == "partial"
    assert res["days_pulled"] == 0  # nothing saved
    assert res["gap_days_remaining"] == 3  # all three dates still missing
    assert "failed" in res["error"]
    # No rows written for any of the failed dates.
    with db.connect(seeded_db) as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM daily_metrics").fetchone()["n"]
    assert n == 0


def test_pull_mid_run_auth_expiry_aborts_and_flags_auth_failure(seeded_db, monkeypatch):
    """An endpoint auth error mid-loop aborts the run instead of being swallowed.

    _safe used to catch GarminConnectAuthenticationError under its blanket
    `except Exception`, so the per-day `raise` handler was unreachable for
    endpoint calls: the loop kept going, wrote empty days, and could still end
    'success'. Now the auth error propagates, the run is flagged auth_failure,
    and no further day is attempted.
    """
    today = date(2026, 6, 25)
    monkeypatch.setattr(daily, "EARLIEST_BACKFILL_DATE", today - timedelta(days=2))
    # Dates run most-recent-first: today, -1, -2. Fail on day 2 (-1).
    fail_day = (today - timedelta(days=1)).isoformat()
    oldest_day = (today - timedelta(days=2)).isoformat()
    fake = FakeGarmin(activities=[], auth_fail_sleep_dates={fail_day})
    monkeypatch.setattr(daily, "_client", lambda *a, **k: fake)

    res = daily.pull(through=today)

    assert res["status"] == "auth_failure"
    # Day 3 (the oldest) was never attempted — the run aborted on day 2.
    assert oldest_day not in fake.summary_calls
    assert today.isoformat() in fake.summary_calls  # day 1 did run


def test_pull_force_from_targets_full_range(seeded_db, monkeypatch):
    today = date(2026, 6, 25)
    force_from = date(2026, 6, 23)
    # Keep the EARLIEST horizon tight so post-pull gap math is clean.
    monkeypatch.setattr(daily, "EARLIEST_BACKFILL_DATE", force_from)
    fake = FakeGarmin(activities=[])
    monkeypatch.setattr(daily, "_client", lambda *a, **k: fake)

    res = daily.pull(through=today, force_from=force_from)

    assert res["status"] == "success"
    assert res["days_pulled"] == 3  # 23, 24, 25 inclusive
    assert res["gap_days_remaining"] == 0


def test_pull_skipped_when_no_targets(seeded_db, monkeypatch):
    today = date(2026, 6, 25)
    monkeypatch.setattr(daily, "EARLIEST_BACKFILL_DATE", today - timedelta(days=2))
    # Drop the freshness window to 0 and pre-seed every date so the target
    # list is genuinely empty → the early "skipped" return.
    monkeypatch.setattr(daily, "FRESHNESS_WINDOW_DAYS", 0)
    with db.connect(seeded_db) as conn:
        for i in range(3):
            d = (today - timedelta(days=i)).isoformat()
            conn.execute("INSERT INTO daily_metrics (date, rhr) VALUES (?, ?)", (d, 50))

    # _client must never be reached on the skipped path.
    def _boom(*a, **k):
        raise AssertionError("_client should not be called when skipped")

    monkeypatch.setattr(daily, "_client", _boom)

    res = daily.pull(through=today)

    assert res["status"] == "skipped"
    assert res["days_pulled"] == 0
    assert res["gap_days_remaining"] == 0
    assert res["last_date"] == today.isoformat()
    # No ingest_runs row is created on the skipped path.
    with db.connect(seeded_db) as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM ingest_runs").fetchone()["n"]
    assert n == 0


def test_pull_auth_failure_mfa(seeded_db, monkeypatch):
    today = date(2026, 6, 25)
    monkeypatch.setattr(daily, "EARLIEST_BACKFILL_DATE", today - timedelta(days=2))

    def _raise(*a, **k):
        raise GarminConnectAuthenticationError("Garmin requested mfa verification")

    monkeypatch.setattr(daily, "_client", _raise)

    res = daily.pull(through=today)

    assert res["status"] == "auth_failure"
    assert res["error"].startswith("mfa_required:")
    assert res["days_pulled"] == 0
    assert res["last_date"] is None
    run = _latest_run(seeded_db)
    assert run["status"] == "auth_failure"
    assert run["last_date_fetched"] is None
    assert run["completed_at"] is not None


def test_pull_auth_failure_bad_credentials(seeded_db, monkeypatch):
    today = date(2026, 6, 25)
    monkeypatch.setattr(daily, "EARLIEST_BACKFILL_DATE", today - timedelta(days=2))

    def _raise(*a, **k):
        raise GarminConnectAuthenticationError("401 unauthorized")

    monkeypatch.setattr(daily, "_client", _raise)

    res = daily.pull(through=today)

    assert res["status"] == "auth_failure"
    assert res["error"].startswith("credentials_invalid:")


def test_pull_not_configured(seeded_db, monkeypatch):
    today = date(2026, 6, 25)
    monkeypatch.setattr(daily, "EARLIEST_BACKFILL_DATE", today - timedelta(days=2))

    def _raise(*a, **k):
        raise RuntimeError("Garmin credentials not stored. Run `fitness setup` first.")

    monkeypatch.setattr(daily, "_client", _raise)

    res = daily.pull(through=today)

    assert res["status"] == "not_configured"
    assert "credentials" in res["error"].lower()
    run = _latest_run(seeded_db)
    assert run["status"] == "not_configured"


def test_pull_runtime_mfa_required(seeded_db, monkeypatch):
    today = date(2026, 6, 25)
    monkeypatch.setattr(daily, "EARLIEST_BACKFILL_DATE", today - timedelta(days=2))

    def _raise(*a, **k):
        raise RuntimeError("mfa_required: no interactive callback available")

    monkeypatch.setattr(daily, "_client", _raise)

    res = daily.pull(through=today)

    assert res["status"] == "auth_failure"
    assert res["error"].startswith("mfa_required:")


def test_pull_generic_failure(seeded_db, monkeypatch):
    today = date(2026, 6, 25)
    monkeypatch.setattr(daily, "EARLIEST_BACKFILL_DATE", today - timedelta(days=2))

    def _raise(*a, **k):
        raise ValueError("network exploded")

    monkeypatch.setattr(daily, "_client", _raise)

    res = daily.pull(through=today)

    assert res["status"] == "failure"  # no last_ok → failure, not partial
    assert "network exploded" in res["error"]
    run = _latest_run(seeded_db)
    assert run["status"] == "failure"
    assert run["last_date_fetched"] is None


def test_pull_unknown_runtime_failure(seeded_db, monkeypatch):
    today = date(2026, 6, 25)
    monkeypatch.setattr(daily, "EARLIEST_BACKFILL_DATE", today - timedelta(days=2))

    def _raise(*a, **k):
        raise RuntimeError("some other runtime problem")

    monkeypatch.setattr(daily, "_client", _raise)

    res = daily.pull(through=today)

    assert res["status"] == "failure"
    assert "some other runtime problem" in res["error"]


# --------------------------------------------------------------------------- #
# _tokenstore_path() + _client() session-token wiring
# --------------------------------------------------------------------------- #
class _LoginRecorder:
    """Stand-in for ``garminconnect.Garmin`` that records the tokenstore arg
    passed to ``login()`` — the seam these tests assert on. No mock library,
    matching this module's hand-rolled convention."""

    def __init__(self, *args, **kwargs):
        self.login_calls: list = []

    def login(self, tokenstore=None):
        self.login_calls.append(tokenstore)
        return None, None


def test_tokenstore_path_default_when_unset(monkeypatch, tmp_path):
    # Clearing the override is required for determinism — a shell- or CI-set
    # GARMINTOKENS would otherwise win and make this assert the wrong path.
    monkeypatch.delenv("GARMINTOKENS", raising=False)
    monkeypatch.setattr(daily.Path, "home", lambda: tmp_path)

    assert daily._tokenstore_path() == str(
        tmp_path / ".garminconnect" / "garmin_tokens.json"
    )


def test_tokenstore_path_honors_env_override(monkeypatch):
    monkeypatch.setenv("GARMINTOKENS", "/custom/loc/garmin_tokens.json")
    assert daily._tokenstore_path() == "/custom/loc/garmin_tokens.json"


def test_client_passes_default_tokenstore_to_login(monkeypatch, tmp_path):
    monkeypatch.delenv("GARMINTOKENS", raising=False)
    monkeypatch.setattr(daily.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(daily.auth, "get_credentials", lambda: ("e@x.com", "pw"))
    rec = _LoginRecorder()
    monkeypatch.setattr(daily, "Garmin", lambda *a, **k: rec)

    client = daily._client()

    # Fails if _client regresses to a no-arg login() (the original 429 bug).
    expected = str(tmp_path / ".garminconnect" / "garmin_tokens.json")
    assert rec.login_calls == [expected]
    assert client is rec


def test_client_passes_env_override_to_login(monkeypatch):
    # A regression where _client ignored the override and hardcoded the default
    # would pass the _tokenstore_path() unit tests but fail here.
    monkeypatch.setenv("GARMINTOKENS", "/custom/loc/garmin_tokens.json")
    monkeypatch.setattr(daily.auth, "get_credentials", lambda: ("e@x.com", "pw"))
    rec = _LoginRecorder()
    monkeypatch.setattr(daily, "Garmin", lambda *a, **k: rec)

    daily._client()

    assert rec.login_calls == ["/custom/loc/garmin_tokens.json"]
