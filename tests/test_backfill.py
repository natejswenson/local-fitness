"""Tests for ingest/backfill.py — the Garmin 'Request your data' ZIP loader.

Mock-free: a real tmp SQLite DB (via ``monkeypatch.setattr(db,
"DEFAULT_DB_PATH", ...)``) plus fabricated ZIPs containing the Garmin export
member names ``backfill`` routes on. All JSON contents are invented — never
derived from real data.

We exercise the pure transform/merge/dedup logic:
  * UDS  → daily_metrics (floors cm→floor math, body-battery stat lookup,
           stress TOTAL aggregator, active-calorie int coercion)
  * sleep → total excludes awake; 0-seconds collapse to NULL
  * VO2   → prefer-running dedup, else highest; COALESCE first-wins
  * training status → COALESCE first-wins
  * activities → cm→m distance/elevation, m·s⁻¹→pace, ms→s, activityType
           dict→typeKey, cadence preference, HR-zone fan-out, three
           top-level container shapes
  * member routing (MACOSX/dir skip, unknown → skipped, malformed → errors)
  * COALESCE first-wins across two backfill runs
"""
from __future__ import annotations

import json
import zipfile
from datetime import datetime

import pytest

from local_fitness import db
from local_fitness.ingest import backfill


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """A freshly-init'd tmp DB; backfill() resolves to it via DEFAULT_DB_PATH."""
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    return p


def _make_zip(tmp_path, members: dict[str, object], name: str = "export.zip"):
    """Write a ZIP whose member names are Garmin-export-shaped.

    Values that are str/bytes are written verbatim (for malformed-JSON cases);
    anything else is json-dumped.
    """
    zip_path = tmp_path / name
    with zipfile.ZipFile(zip_path, "w") as zf:
        for member, payload in members.items():
            if isinstance(payload, (str, bytes)):
                zf.writestr(member, payload)
            else:
                zf.writestr(member, json.dumps(payload))
    return zip_path


def _row(db_path, date):
    with db.connect(db_path) as conn:
        return conn.execute(
            "SELECT * FROM daily_metrics WHERE date = ?", (date,)
        ).fetchone()


# -- top-level / routing ----------------------------------------------------

def test_missing_zip_raises(fresh_db, tmp_path):
    with pytest.raises(FileNotFoundError):
        backfill.backfill(tmp_path / "does_not_exist.zip")


def test_records_ingest_run(fresh_db, tmp_path):
    zp = _make_zip(tmp_path, {"DI_CONNECT/whatever.txt": "ignored"})
    backfill.backfill(zp)
    with db.connect(fresh_db) as conn:
        run = conn.execute(
            "SELECT status, source FROM ingest_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
    assert run["status"] == "success"
    assert run["source"] == "backfill"


def test_routing_skips_dirs_macosx_and_unknown(fresh_db, tmp_path):
    members = {
        "DI_CONNECT/": "",  # directory entry → continue
        "__MACOSX/._foo": "junk",  # MACOSX → continue
        "DI_CONNECT/notes.txt": "not json, not matched",  # unknown → skipped
    }
    counts = backfill.backfill(_make_zip(tmp_path, members))
    # dir + MACOSX are 'continue' (not counted); only notes.txt counts as skipped
    assert counts["skipped"] == 1
    assert counts["errors"] == 0


def test_malformed_json_counts_as_error(fresh_db, tmp_path):
    members = {
        "DI_CONNECT/DI-Connect-Aggregator/UDSFile_1_2.json": b"{ this is not json",
    }
    counts = backfill.backfill(_make_zip(tmp_path, members))
    assert counts["errors"] == 1
    assert counts["uds_files"] == 0


def test_non_list_payload_returns_zero(fresh_db, tmp_path):
    # A JSON object (not a list) for each list-expecting ingester → 0 rows, no error.
    members = {
        "DI_CONNECT/DI-Connect-Aggregator/UDSFile_1_2.json": {"not": "a list"},
        "DI_CONNECT/DI-Connect-Wellness/x_sleepData.json": {"not": "a list"},
        "DI_CONNECT/DI-Connect-Metrics/MetricsMaxMetData_x.json": {"not": "a list"},
        "DI_CONNECT/DI-Connect-Metrics/TrainingHistory_x.json": {"not": "a list"},
    }
    counts = backfill.backfill(_make_zip(tmp_path, members))
    assert counts["errors"] == 0
    assert counts["uds_days"] == 0
    assert counts["sleep_nights"] == 0
    assert counts["vo2_rows"] == 0
    assert counts["training_status_rows"] == 0


# -- UDS daily aggregates ---------------------------------------------------

def _uds_member(name="DI_CONNECT/DI-Connect-Aggregator/UDSFile_1_2.json"):
    return name


def test_uds_full_transform(fresh_db, tmp_path):
    day = {
        "calendarDate": "2026-01-15T00:00:00.0",  # truncated to 10 chars
        "currentDayRestingHeartRate": 48,
        "totalSteps": 12000,
        "activeKilocalories": 540.9,  # → int 540
        "floorsAscendedInMeters": 30.48,  # / 3.048 = 10 floors
        "moderateIntensityMinutes": 25,
        "vigorousIntensityMinutes": 10,
        "bodyBattery": {
            "chargedValue": 70,
            "drainedValue": 55,
            "bodyBatteryStatList": [
                {"bodyBatteryStatType": "LOWEST", "statsValue": 18},
                {"bodyBatteryStatType": "HIGHEST", "statsValue": 92},
            ],
        },
        "allDayStress": {
            "aggregatorList": [
                {"type": "AWAKE", "averageStressLevel": 99, "maxStressLevel": 99},
                {"type": "TOTAL", "averageStressLevel": 33, "maxStressLevel": 88},
            ]
        },
        "respiration": {"avgWakingRespirationValue": 14.5},
    }
    counts = backfill.backfill(
        _make_zip(tmp_path, {_uds_member(): [day]})
    )
    assert counts["uds_files"] == 1
    assert counts["uds_days"] == 1

    r = _row(fresh_db, "2026-01-15")
    assert r["rhr"] == 48
    assert r["steps"] == 12000
    assert r["active_calories"] == 540  # int() truncation
    assert r["floors_climbed"] == 10
    assert r["body_battery_min"] == 18
    assert r["body_battery_max"] == 92
    assert r["body_battery_charged"] == 70
    assert r["body_battery_drained"] == 55
    assert r["avg_stress"] == 33  # TOTAL aggregator, not AWAKE
    assert r["max_stress"] == 88
    assert r["respiration_avg"] == 14.5
    assert r["intensity_minutes_moderate"] == 25
    assert r["intensity_minutes_vigorous"] == 10
    # raw_json preserved
    assert json.loads(r["raw_json"])["calendarDate"].startswith("2026-01-15")


def test_uds_rhr_fallback_and_missing_floors(fresh_db, tmp_path):
    day = {
        "calendarDate": "2026-02-01",
        "restingHeartRate": 52,  # fallback when currentDayRestingHeartRate absent
        # floorsAscendedInMeters absent → floors stays None
    }
    backfill.backfill(_make_zip(tmp_path, {_uds_member(): [day]}))
    r = _row(fresh_db, "2026-02-01")
    assert r["rhr"] == 52
    assert r["floors_climbed"] is None


def test_uds_skips_non_dict_and_missing_date(fresh_db, tmp_path):
    payload = [
        "i am not a dict",
        {"no": "calendarDate here"},
        {"calendarDate": "2026-03-03", "totalSteps": 8000},
    ]
    counts = backfill.backfill(_make_zip(tmp_path, {_uds_member(): payload}))
    # only the one valid day is counted
    assert counts["uds_days"] == 1
    assert _row(fresh_db, "2026-03-03")["steps"] == 8000


def test_uds_no_updates_when_all_values_none(fresh_db, tmp_path):
    # A day with only a date and all-None metrics still inserts the row (updates empty).
    day = {"calendarDate": "2026-04-04", "activeKilocalories": None}
    counts = backfill.backfill(_make_zip(tmp_path, {_uds_member(): [day]}))
    assert counts["uds_days"] == 1
    r = _row(fresh_db, "2026-04-04")
    assert r is not None
    assert r["active_calories"] is None


# -- sleep ------------------------------------------------------------------

def _sleep_member(name="DI_CONNECT/DI-Connect-Wellness/1_2_uid_sleepData.json"):
    return name


def test_sleep_total_excludes_awake(fresh_db, tmp_path):
    night = {
        "calendarDate": "2026-01-20",
        "deepSleepSeconds": 3600,
        "lightSleepSeconds": 7200,
        "remSleepSeconds": 5400,
        "awakeSleepSeconds": 600,
        "sleepScores": {"overallScore": 84, "feedback": "GOOD"},
    }
    counts = backfill.backfill(_make_zip(tmp_path, {_sleep_member(): [night]}))
    assert counts["sleep_files"] == 1
    assert counts["sleep_nights"] == 1
    r = _row(fresh_db, "2026-01-20")
    assert r["sleep_seconds"] == 3600 + 7200 + 5400  # awake excluded
    assert r["sleep_deep_seconds"] == 3600
    assert r["sleep_light_seconds"] == 7200
    assert r["sleep_rem_seconds"] == 5400
    assert r["sleep_awake_seconds"] == 600
    assert r["sleep_score"] == 84
    assert r["sleep_quality"] == "GOOD"


def test_sleep_zero_stages_collapse_to_null(fresh_db, tmp_path):
    night = {
        "calendarDate": "2026-01-21",
        # all stage fields absent → 0 → None, so no columns written
    }
    counts = backfill.backfill(_make_zip(tmp_path, {_sleep_member(): [night]}))
    assert counts["sleep_nights"] == 1
    r = _row(fresh_db, "2026-01-21")
    assert r is not None
    assert r["sleep_seconds"] is None
    assert r["sleep_deep_seconds"] is None


def test_sleep_skips_non_dict_and_missing_date(fresh_db, tmp_path):
    payload = [42, {"missing": "date"}, {"calendarDate": "2026-01-22", "deepSleepSeconds": 100}]
    counts = backfill.backfill(_make_zip(tmp_path, {_sleep_member(): payload}))
    assert counts["sleep_nights"] == 1
    assert _row(fresh_db, "2026-01-22")["sleep_deep_seconds"] == 100


# -- VO2 max ----------------------------------------------------------------

def _vo2_member(name="DI_CONNECT/DI-Connect-Metrics/MetricsMaxMetData_x_uid.json"):
    return name


def test_vo2_prefers_running_over_higher_other(fresh_db, tmp_path):
    # Same date: a higher cycling reading first, then a lower RUNNING reading.
    # prefer-running must win even though it's the lower number.
    payload = [
        {"calendarDate": "2026-05-01", "sport": "CYCLING", "vo2MaxValue": 55.0},
        {"calendarDate": "2026-05-01", "sport": "RUNNING", "vo2MaxValue": 50.0},
    ]
    counts = backfill.backfill(_make_zip(tmp_path, {_vo2_member(): payload}))
    assert counts["vo2_rows"] == 1
    assert _row(fresh_db, "2026-05-01")["vo2_max"] == 50.0


def test_vo2_later_higher_value_overwrites_regardless_of_type(fresh_db, tmp_path):
    # Within a single export batch, "prefer running" does NOT make a running
    # reading sticky: a later, HIGHER non-running reading still wins via the
    # `v > existing` clause in _ingest_vo2. (Running stickiness ACROSS separate
    # exports is provided independently by the COALESCE write, not by this loop.)
    payload = [
        {"calendarDate": "2026-05-02", "sport": "RUNNING", "vo2MaxValue": 49.0},
        {"calendarDate": "2026-05-02", "sport": "CYCLING", "vo2MaxValue": 60.0},
    ]
    backfill.backfill(_make_zip(tmp_path, {_vo2_member(): payload}))
    assert _row(fresh_db, "2026-05-02")["vo2_max"] == 60.0  # 60 > 49 → highest wins


def test_vo2_running_value_survives_later_lower_nonrunning(fresh_db, tmp_path):
    # Genuine within-batch prefer-running behavior: once a RUNNING reading is
    # set, a later LOWER non-running reading does not displace it — the
    # `v > existing` clause is false and the `sport == "RUNNING"` clause doesn't
    # fire for the cycling entry, so the running value persists.
    payload = [
        {"calendarDate": "2026-05-05", "sport": "RUNNING", "vo2MaxValue": 50.0},
        {"calendarDate": "2026-05-05", "sport": "CYCLING", "vo2MaxValue": 45.0},
    ]
    backfill.backfill(_make_zip(tmp_path, {_vo2_member(): payload}))
    assert _row(fresh_db, "2026-05-05")["vo2_max"] == 50.0


def test_vo2_highest_when_no_running(fresh_db, tmp_path):
    payload = [
        {"calendarDate": "2026-05-03", "sport": "CYCLING", "vo2MaxValue": 44.0},
        {"calendarDate": "2026-05-03", "sport": "SWIMMING", "vo2MaxValue": 47.0},
        {"calendarDate": "2026-05-03", "sport": "HIKING", "vo2MaxValue": 45.0},
    ]
    backfill.backfill(_make_zip(tmp_path, {_vo2_member(): payload}))
    assert _row(fresh_db, "2026-05-03")["vo2_max"] == 47.0


def test_vo2_skips_missing_value_or_date_or_nondict(fresh_db, tmp_path):
    payload = [
        "nope",
        {"calendarDate": "2026-05-04"},  # no vo2MaxValue
        {"sport": "RUNNING", "vo2MaxValue": 50.0},  # no calendarDate
        {"calendarDate": "2026-05-04", "vo2MaxValue": 51.0},  # valid (no sport key)
    ]
    counts = backfill.backfill(_make_zip(tmp_path, {_vo2_member(): payload}))
    assert counts["vo2_rows"] == 1
    assert _row(fresh_db, "2026-05-04")["vo2_max"] == 51.0


# -- training status --------------------------------------------------------

def _ts_member(name="DI_CONNECT/DI-Connect-Metrics/TrainingHistory_x_uid.json"):
    return name


def test_training_status_ingest(fresh_db, tmp_path):
    payload = [
        {"calendarDate": "2026-06-01", "trainingStatus": "PRODUCTIVE"},
        "skip-nondict",
        {"calendarDate": "2026-06-02"},  # missing status → skipped
        {"trainingStatus": "RECOVERY"},  # missing date → skipped
    ]
    counts = backfill.backfill(_make_zip(tmp_path, {_ts_member(): payload}))
    assert counts["training_status_rows"] == 1
    assert _row(fresh_db, "2026-06-01")["training_status"] == "PRODUCTIVE"


# -- activities -------------------------------------------------------------

def _act_member(name="DI_CONNECT/DI-Connect-Fitness/me_0_summarizedActivities.json"):
    return name


def _sample_activity(**overrides):
    # local epoch ms for 2026-03-10 (some local time); > 1e11 so it's used.
    ts = int(datetime(2026, 3, 10, 7, 30, 0).timestamp() * 1000)
    act = {
        "activityId": 111,
        "startTimeLocal": ts,
        "duration": 1800000,  # ms → 1800 s
        "movingDuration": 1700000,  # ms → 1700 s
        "distance": 500000,  # cm → 5000 m
        "elevationGain": 10000,  # cm → 100 m
        "elevationLoss": 8000,  # cm → 80 m
        "avgSpeed": 50.0,  # → pace 100/50 = 2.0 sec/km (per code's math)
        "avgHr": 150.7,  # → int 150
        "maxHr": 175.2,  # → int 175
        "calories": 420.8,  # → int 420
        "aerobicTrainingEffect": 3.4,
        "anaerobicTrainingEffect": 0.5,
        "activityTrainingLoad": 95.0,
        "avgDoubleCadence": 178.6,  # preferred over run/bike cadence
        "avgRunCadence": 89.0,
        "vO2MaxValue": 51.5,
        "temperature": 12.0,
        "activityType": {"typeKey": "running"},
        "name": "Morning Run",
        "hrTimeInZone_0": 0,  # 0 → not inserted
        "hrTimeInZone_1": 300,
        "hrTimeInZone_2": 900,
    }
    act.update(overrides)
    return act


def _activities(db_path, activity_id):
    with db.connect(db_path) as conn:
        return conn.execute(
            "SELECT * FROM activities WHERE activity_id = ?", (activity_id,)
        ).fetchone()


def test_activities_wrapped_in_export_list(fresh_db, tmp_path):
    payload = [{"summarizedActivitiesExport": [_sample_activity()]}]
    counts = backfill.backfill(_make_zip(tmp_path, {_act_member(): payload}))
    assert counts["activity_files"] == 1
    assert counts["activity_rows"] == 1

    a = _activities(fresh_db, 111)
    assert a["date"] == "2026-03-10"
    assert a["start_time"].startswith("2026-03-10T07:30")
    assert a["activity_type"] == "running"
    assert a["activity_name"] == "Morning Run"
    assert a["duration_seconds"] == 1800
    assert a["moving_seconds"] == 1700
    assert a["distance_meters"] == 5000.0
    assert a["elevation_gain_meters"] == 100.0
    assert a["elevation_loss_meters"] == 80.0
    assert a["avg_pace_sec_per_km"] == pytest.approx(2.0)
    assert a["avg_hr"] == 150
    assert a["max_hr"] == 175
    assert a["calories"] == 420
    assert a["aerobic_te"] == 3.4
    assert a["anaerobic_te"] == 0.5
    assert a["training_load"] == 95.0
    assert a["avg_cadence"] == 178  # avgDoubleCadence, int()
    assert a["vo2_max_estimate"] == 51.5
    assert a["weather_temp_c"] == 12.0

    # HR zones: zone 0 (0 secs) skipped, zones 1 & 2 inserted
    with db.connect(fresh_db) as conn:
        zones = conn.execute(
            "SELECT zone, seconds_in_zone FROM activity_hr_zones "
            "WHERE activity_id = 111 ORDER BY zone"
        ).fetchall()
    assert [(z["zone"], z["seconds_in_zone"]) for z in zones] == [(1, 300), (2, 900)]


def test_activities_dict_container_shape(fresh_db, tmp_path):
    payload = {"summarizedActivitiesExport": [_sample_activity(activityId=222)]}
    counts = backfill.backfill(_make_zip(tmp_path, {_act_member(): payload}))
    assert counts["activity_rows"] == 1
    a = _activities(fresh_db, 222)
    assert a is not None
    # Prove a real transform ran through this entry point, not just a row count.
    assert a["activity_type"] == "running"
    assert a["distance_meters"] == 5000.0


def test_activities_plain_list_shape(fresh_db, tmp_path):
    payload = [_sample_activity(activityId=333)]
    counts = backfill.backfill(_make_zip(tmp_path, {_act_member(): payload}))
    assert counts["activity_rows"] == 1
    a = _activities(fresh_db, 333)
    assert a is not None
    assert a["activity_type"] == "running"
    assert a["distance_meters"] == 5000.0


def test_activities_string_activity_type_and_name_fallback(fresh_db, tmp_path):
    act = _sample_activity(activityId=444)
    act["activityType"] = "cycling"  # already a string, not a dict
    del act["name"]
    act["activityName"] = "Evening Ride"
    backfill.backfill(_make_zip(tmp_path, {_act_member(): [act]}))
    a = _activities(fresh_db, 444)
    assert a["activity_type"] == "cycling"
    assert a["activity_name"] == "Evening Ride"


def test_activities_cadence_fallback_chain(fresh_db, tmp_path):
    # No double cadence → falls to run cadence; then to bike cadence.
    act = _sample_activity(activityId=555)
    del act["avgDoubleCadence"]
    payload = [act]
    backfill.backfill(_make_zip(tmp_path, {_act_member(): payload}))
    assert _activities(fresh_db, 555)["avg_cadence"] == 89  # avgRunCadence

    act2 = _sample_activity(activityId=556)
    del act2["avgDoubleCadence"]
    del act2["avgRunCadence"]
    act2["avgBikeCadence"] = 70.0
    backfill.backfill(_make_zip(tmp_path, {_act_member(): [act2]}, name="b.zip"))
    assert _activities(fresh_db, 556)["avg_cadence"] == 70  # avgBikeCadence


def test_activities_begin_timestamp_fallback_and_no_timestamp(fresh_db, tmp_path):
    # No startTimeLocal → uses beginTimestamp.
    ts = int(datetime(2026, 3, 11, 6, 0, 0).timestamp() * 1000)
    act = _sample_activity(activityId=666)
    del act["startTimeLocal"]
    act["beginTimestamp"] = ts
    backfill.backfill(_make_zip(tmp_path, {_act_member(): [act]}))
    assert _activities(fresh_db, 666)["date"] == "2026-03-11"

    # Neither timestamp / too-small value → date "" and start_time None.
    act2 = _sample_activity(activityId=777)
    act2["startTimeLocal"] = 123  # < 1e11
    backfill.backfill(_make_zip(tmp_path, {_act_member(): [act2]}, name="c.zip"))
    a2 = _activities(fresh_db, 777)
    assert a2["date"] == ""
    assert a2["start_time"] is None


def test_activities_skips_nondict_and_missing_id(fresh_db, tmp_path):
    payload = ["nope", {"no": "activityId"}, _sample_activity(activityId=888)]
    counts = backfill.backfill(_make_zip(tmp_path, {_act_member(): payload}))
    assert counts["activity_rows"] == 1
    assert _activities(fresh_db, 888) is not None


def test_activities_null_distance_and_speed(fresh_db, tmp_path):
    act = _sample_activity(activityId=999)
    act["distance"] = None
    act["elevationGain"] = None
    act["elevationLoss"] = None
    act["avgSpeed"] = 0  # falsy → pace None
    backfill.backfill(_make_zip(tmp_path, {_act_member(): [act]}))
    a = _activities(fresh_db, 999)
    assert a["distance_meters"] is None
    assert a["elevation_gain_meters"] is None
    assert a["avg_pace_sec_per_km"] is None


def test_activities_non_list_export_returns_zero(fresh_db, tmp_path):
    # summarizedActivitiesExport present but not a list → 0 rows.
    payload = {"summarizedActivitiesExport": {"not": "a list"}}
    counts = backfill.backfill(_make_zip(tmp_path, {_act_member(): payload}))
    assert counts["activity_rows"] == 0


def test_activities_unrecognized_top_level_returns_zero(fresh_db, tmp_path):
    # A bare int payload matches none of the container shapes → 0 rows, no error.
    payload = 12345
    counts = backfill.backfill(_make_zip(tmp_path, {_act_member(): payload}))
    assert counts["errors"] == 0
    assert counts["activity_rows"] == 0


# -- COALESCE first-wins across runs ----------------------------------------

def test_coalesce_first_wins_across_backfills(fresh_db, tmp_path):
    # First backfill seeds rhr=50 for the date.
    first = {"calendarDate": "2026-07-01", "currentDayRestingHeartRate": 50}
    backfill.backfill(_make_zip(tmp_path, {_uds_member(): [first]}, name="first.zip"))
    assert _row(fresh_db, "2026-07-01")["rhr"] == 50

    # Second backfill tries rhr=99 for the same date — COALESCE keeps the first.
    second = {
        "calendarDate": "2026-07-01",
        "currentDayRestingHeartRate": 99,
        "totalSteps": 6000,  # a previously-NULL column DOES get filled
    }
    backfill.backfill(_make_zip(tmp_path, {_uds_member(): [second]}, name="second.zip"))
    r = _row(fresh_db, "2026-07-01")
    assert r["rhr"] == 50  # first wins
    assert r["steps"] == 6000  # null column backfilled


def test_sleep_then_uds_merge_same_date(fresh_db, tmp_path):
    # Sleep file and UDS file for the same date in one ZIP both contribute
    # disjoint columns to the same daily_metrics row.
    members = {
        _uds_member(): [{"calendarDate": "2026-07-05", "totalSteps": 9000}],
        _sleep_member(): [
            {"calendarDate": "2026-07-05", "deepSleepSeconds": 3600,
             "lightSleepSeconds": 3600, "remSleepSeconds": 1800}
        ],
    }
    backfill.backfill(_make_zip(tmp_path, members))
    r = _row(fresh_db, "2026-07-05")
    assert r["steps"] == 9000
    assert r["sleep_seconds"] == 3600 + 3600 + 1800
