"""Daily pull from Garmin Connect via the unofficial garminconnect library.

Catches up since the last successful run — safe to invoke even if the laptop
was closed for days. Each day's wellness + the activity range are wrapped in
defensive try/except so a single missing endpoint doesn't poison the run.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable

from garminconnect import Garmin, GarminConnectAuthenticationError

from .. import db
from . import auth

LOG = logging.getLogger(__name__)

# Instinct Solar launched Sept 2020; nothing earlier exists for Nate.
EARLIEST_BACKFILL_DATE = date(2020, 9, 1)


def _client() -> Garmin:
    creds = auth.get_credentials()
    if not creds:
        raise RuntimeError("Garmin credentials not stored. Run `fitness setup` first.")
    email, password = creds
    client = Garmin(email, password, prompt_mfa=lambda: input("Garmin MFA code: "))
    client.login()
    return client


def _safe(call: Callable, *args, **kwargs) -> Any:
    try:
        return call(*args, **kwargs)
    except Exception as e:
        LOG.warning("API call %s failed: %s", getattr(call, "__name__", call), e)
        return None


def _to_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_real(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ingest_day(client: Garmin, conn, cdate: date) -> None:
    cdate_str = cdate.isoformat()

    summary = _safe(client.get_user_summary, cdate_str) or {}
    sleep = _safe(client.get_sleep_data, cdate_str) or {}
    sleep_dto = sleep.get("dailySleepDTO", {}) if isinstance(sleep, dict) else {}
    sleep_scores = sleep_dto.get("sleepScores") if isinstance(sleep_dto, dict) else {}
    overall = (sleep_scores or {}).get("overall") if isinstance(sleep_scores, dict) else {}

    bb = _safe(client.get_body_battery, cdate_str, cdate_str)
    bb_first = bb[0] if isinstance(bb, list) and bb else {}

    max_metrics = _safe(client.get_max_metrics, cdate_str)
    vo2 = None
    if isinstance(max_metrics, list) and max_metrics:
        generic = (max_metrics[0] or {}).get("generic") or {}
        vo2 = generic.get("vo2MaxValue")

    daily = {
        "date": cdate_str,
        "sleep_seconds": _to_int(sleep_dto.get("sleepTimeSeconds")),
        "sleep_deep_seconds": _to_int(sleep_dto.get("deepSleepSeconds")),
        "sleep_light_seconds": _to_int(sleep_dto.get("lightSleepSeconds")),
        "sleep_rem_seconds": _to_int(sleep_dto.get("remSleepSeconds")),
        "sleep_awake_seconds": _to_int(sleep_dto.get("awakeSleepSeconds")),
        "sleep_score": _to_int((overall or {}).get("value")),
        "sleep_quality": (overall or {}).get("qualifierKey") if isinstance(overall, dict) else None,
        "rhr": _to_int(summary.get("restingHeartRate")),
        "avg_stress": _to_int(summary.get("averageStressLevel")),
        "max_stress": _to_int(summary.get("maxStressLevel")),
        "body_battery_min": _to_int(bb_first.get("min") if isinstance(bb_first, dict) else None),
        "body_battery_max": _to_int(bb_first.get("max") if isinstance(bb_first, dict) else None),
        "body_battery_charged": _to_int(bb_first.get("charged") if isinstance(bb_first, dict) else None),
        "body_battery_drained": _to_int(bb_first.get("drained") if isinstance(bb_first, dict) else None),
        "steps": _to_int(summary.get("totalSteps")),
        "active_calories": _to_int(summary.get("activeKilocalories")),
        "floors_climbed": _to_int(summary.get("floorsAscended")),
        "avg_spo2": _to_int(summary.get("averageSpo2")),
        "respiration_avg": _to_real(summary.get("avgWakingRespirationValue")),
        "vo2_max": _to_real(vo2),
        "training_status": None,
        "fitness_age": None,
        "intensity_minutes_moderate": _to_int(summary.get("moderateIntensityMinutes")),
        "intensity_minutes_vigorous": _to_int(summary.get("vigorousIntensityMinutes")),
        "raw_json": json.dumps({"summary": summary, "sleep": sleep, "body_battery": bb}),
    }

    cols = ", ".join(daily.keys())
    placeholders = ", ".join(f":{k}" for k in daily.keys())
    conn.execute(
        f"INSERT OR REPLACE INTO daily_metrics ({cols}) VALUES ({placeholders})",
        daily,
    )

    if isinstance(bb, list):
        for entry in bb:
            if not isinstance(entry, dict):
                continue
            for sample in entry.get("bodyBatteryValuesArray") or []:
                if not (isinstance(sample, (list, tuple)) and len(sample) >= 2):
                    continue
                ts, val = sample[0], sample[1]
                conn.execute(
                    "INSERT OR REPLACE INTO body_battery_samples (date, timestamp, value) VALUES (?, ?, ?)",
                    (cdate_str, datetime.fromtimestamp(ts / 1000).isoformat(), val),
                )

    stress = _safe(client.get_stress_data, cdate_str)
    if isinstance(stress, dict):
        for sample in stress.get("stressValuesArray") or []:
            if not (isinstance(sample, (list, tuple)) and len(sample) >= 2):
                continue
            ts, val = sample[0], sample[1]
            conn.execute(
                "INSERT OR REPLACE INTO stress_samples (date, timestamp, value) VALUES (?, ?, ?)",
                (cdate_str, datetime.fromtimestamp(ts / 1000).isoformat(), val),
            )


def _ingest_activity_range(client: Garmin, conn, start: date, end: date) -> int:
    activities = _safe(client.get_activities_by_date, start.isoformat(), end.isoformat()) or []
    if not isinstance(activities, list):
        return 0
    n = 0
    for act in activities:
        if not isinstance(act, dict):
            continue
        activity_id = act.get("activityId")
        if not activity_id:
            continue
        avg_speed = _to_real(act.get("averageSpeed"))
        row = {
            "activity_id": activity_id,
            "date": (act.get("startTimeLocal") or "")[:10],
            "start_time": act.get("startTimeLocal"),
            "activity_type": (act.get("activityType") or {}).get("typeKey"),
            "activity_name": act.get("activityName"),
            "duration_seconds": _to_int(act.get("duration")),
            "moving_seconds": _to_int(act.get("movingDuration")),
            "distance_meters": _to_real(act.get("distance")),
            "avg_hr": _to_int(act.get("averageHR")),
            "max_hr": _to_int(act.get("maxHR")),
            "avg_pace_sec_per_km": (1000.0 / avg_speed) if avg_speed else None,
            "elevation_gain_meters": _to_real(act.get("elevationGain")),
            "elevation_loss_meters": _to_real(act.get("elevationLoss")),
            "calories": _to_int(act.get("calories")),
            "aerobic_te": _to_real(act.get("aerobicTrainingEffect")),
            "anaerobic_te": _to_real(act.get("anaerobicTrainingEffect")),
            "training_load": _to_real(act.get("activityTrainingLoad")),
            "avg_cadence": _to_int(
                act.get("averageRunningCadenceInStepsPerMinute")
                or act.get("averageBikingCadenceInRevPerMinute")
            ),
            "vo2_max_estimate": _to_real(act.get("vO2MaxValue")),
            "weather_temp_c": _to_real(act.get("temperature")),
            "weather_conditions": (act.get("weatherTypeDTO") or {}).get("desc")
            if act.get("weatherTypeDTO")
            else None,
            "raw_json": json.dumps(act),
        }
        cols = ", ".join(row.keys())
        placeholders = ", ".join(f":{k}" for k in row.keys())
        conn.execute(
            f"INSERT OR REPLACE INTO activities ({cols}) VALUES ({placeholders})",
            row,
        )

        zones = _safe(client.get_activity_hr_in_timezones, activity_id)
        if isinstance(zones, list):
            for i, z in enumerate(zones, 1):
                if not isinstance(z, dict):
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO activity_hr_zones (activity_id, zone, seconds_in_zone) "
                    "VALUES (?, ?, ?)",
                    (activity_id, _to_int(z.get("zoneNumber")) or i, _to_int(z.get("secsInZone"))),
                )

        splits = _safe(client.get_activity_splits, activity_id)
        if isinstance(splits, dict):
            for i, lap in enumerate(splits.get("lapDTOs") or []):
                if not isinstance(lap, dict):
                    continue
                lap_speed = _to_real(lap.get("averageSpeed"))
                conn.execute(
                    "INSERT OR REPLACE INTO activity_splits "
                    "(activity_id, split_index, distance_meters, duration_seconds, "
                    "avg_hr, avg_pace_sec_per_km, elevation_gain_meters) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        activity_id,
                        i,
                        _to_real(lap.get("distance")),
                        _to_int(lap.get("duration")),
                        _to_int(lap.get("averageHR")),
                        (1000.0 / lap_speed) if lap_speed else None,
                        _to_real(lap.get("elevationGain")),
                    ),
                )
        n += 1
        time.sleep(0.3)
    return n


def pull(through: date | None = None, force_from: date | None = None) -> dict:
    """Pull daily wellness + activities since last successful ingest.

    Returns a summary dict suitable for CLI output.
    """
    db.init_schema()
    today = through or date.today()

    if force_from:
        start = force_from
    else:
        last = db.last_successful_daily_date()
        start = (date.fromisoformat(last) + timedelta(days=1)) if last else EARLIEST_BACKFILL_DATE

    if start > today:
        LOG.info("Already up to date through %s", today)
        return {"days_pulled": 0, "status": "skipped", "last_date": last if not force_from else None}

    LOG.info("Pulling Garmin data %s through %s", start, today)

    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO ingest_runs (started_at, status, source) VALUES (?, 'in_progress', 'daily')",
            (datetime.now().isoformat(),),
        )
        run_id = cur.lastrowid

    last_ok = None
    error = None
    days = 0
    activities_loaded = 0

    try:
        client = _client()
        d = start
        while d <= today:
            with db.connect() as conn:
                _ingest_day(client, conn, d)
            days += 1
            last_ok = d
            d += timedelta(days=1)
            time.sleep(0.5)
        with db.connect() as conn:
            activities_loaded = _ingest_activity_range(client, conn, start, today)
        status = "success"
    except GarminConnectAuthenticationError as e:
        status = "failure"
        error = f"auth: {e}"
        LOG.error("Garmin auth failed: %s", e)
    except Exception as e:
        status = "partial" if last_ok else "failure"
        error = str(e)
        LOG.exception("Pull failed at %s", last_ok or start)

    with db.connect() as conn:
        conn.execute(
            "UPDATE ingest_runs SET completed_at = ?, status = ?, last_date_fetched = ?, "
            "error_message = ? WHERE run_id = ?",
            (
                datetime.now().isoformat(),
                status,
                last_ok.isoformat() if last_ok else None,
                error,
                run_id,
            ),
        )

    return {
        "days_pulled": days,
        "activities_loaded": activities_loaded,
        "status": status,
        "last_date": last_ok.isoformat() if last_ok else None,
        "error": error,
    }
