"""Backfill historical Garmin data from the official 'Request your data' ZIP.

Verified against a real export 2026-04-26 — the structure is:

  DI_CONNECT/DI-Connect-Aggregator/UDSFile_<start>_<end>.json
      User Daily Summary — ~100 days per file. THE main daily aggregate
      (steps, RHR, body battery, stress, calories, intensity minutes,
      respiration, hydration). One file per ~3 month window.

  DI_CONNECT/DI-Connect-Wellness/<start>_<end>_<userid>_sleepData.json
      Per-night sleep with stages (deep/light/REM/awake), sleep scores,
      respiration, restless-moment count.

  DI_CONNECT/DI-Connect-Fitness/<email>_0_summarizedActivities.json
      Single big file (~35MB for 3 years) with all activities. Wrapped in
      a one-element list with key 'summarizedActivitiesExport'.

  DI_CONNECT/DI-Connect-Metrics/MetricsMaxMetData_<dates>_<userid>.json
      VO2 max time series.

  DI_CONNECT/DI-Connect-Metrics/TrainingHistory_<dates>_<userid>.json
      Daily training_status (PRODUCTIVE / MAINTAINING / RECOVERY / etc).

We INSERT OR IGNORE the daily row first, then COALESCE-UPDATE individual
columns so that whichever file fills a column first wins. This matches our
design: backfill never overwrites live-pulled data, and within backfill the
order across file types doesn't matter.

CTL/ATL/TSB are NOT loaded from the MetricsAcuteTrainingLoad files — we
re-derive them from activity training_load via the Banister model in
ingest.baselines, which keeps the going-forward computation consistent
with the historical period.
"""
from __future__ import annotations

import json
import logging
import zipfile
from datetime import datetime
from pathlib import Path

from .. import db

LOG = logging.getLogger(__name__)

METERS_PER_FLOOR = 3.048  # US standard 10ft floor; Garmin reports meters


def backfill(zip_path: Path) -> dict:
    db.init_schema()
    if not zip_path.exists():
        raise FileNotFoundError(zip_path)

    counts = {
        "uds_files": 0, "uds_days": 0,
        "sleep_files": 0, "sleep_nights": 0,
        "vo2_files": 0, "vo2_rows": 0,
        "training_status_files": 0, "training_status_rows": 0,
        "activity_files": 0, "activity_rows": 0,
        "skipped": 0, "errors": 0,
    }

    with zipfile.ZipFile(zip_path) as zf:
        members = zf.namelist()
        LOG.info("Backfill: %d entries in %s", len(members), zip_path.name)
        for member in members:
            try:
                if member.endswith("/") or "MACOSX" in member:
                    continue
                base = member.rsplit("/", 1)[-1].lower()

                if "udsfile_" in base and base.endswith(".json"):
                    n = _ingest_uds(zf.read(member))
                    counts["uds_files"] += 1
                    counts["uds_days"] += n
                elif "sleepdata" in base and base.endswith(".json"):
                    n = _ingest_sleep(zf.read(member))
                    counts["sleep_files"] += 1
                    counts["sleep_nights"] += n
                elif "metricsmaxmetdata" in base and base.endswith(".json"):
                    n = _ingest_vo2(zf.read(member))
                    counts["vo2_files"] += 1
                    counts["vo2_rows"] += n
                elif "traininghistory" in base and base.endswith(".json"):
                    n = _ingest_training_status(zf.read(member))
                    counts["training_status_files"] += 1
                    counts["training_status_rows"] += n
                elif "summarizedactivities" in base and base.endswith(".json"):
                    n = _ingest_activities(zf.read(member))
                    counts["activity_files"] += 1
                    counts["activity_rows"] += n
                else:
                    counts["skipped"] += 1
            except Exception as e:
                LOG.warning("Failed parsing %s: %s", member, e)
                counts["errors"] += 1

    with db.connect() as conn:
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO ingest_runs (started_at, completed_at, status, source) "
            "VALUES (?, ?, 'success', 'backfill')",
            (now, now),
        )

    return counts


# -- UDSFile (daily aggregates) ---------------------------------------------

def _bb_stat(body_battery: dict, stat_type: str) -> int | None:
    for s in (body_battery or {}).get("bodyBatteryStatList") or []:
        if s.get("bodyBatteryStatType") == stat_type:
            v = s.get("statsValue")
            return int(v) if v is not None else None
    return None


def _stress_total(all_day_stress: dict) -> dict:
    """Return the TOTAL aggregator dict (or empty)."""
    for agg in (all_day_stress or {}).get("aggregatorList") or []:
        if agg.get("type") == "TOTAL":
            return agg
    return {}


def _ingest_uds(raw: bytes) -> int:
    data = json.loads(raw)
    if not isinstance(data, list):
        return 0
    n = 0
    with db.connect() as conn:
        for day in data:
            if not isinstance(day, dict):
                continue
            cdate = day.get("calendarDate")
            if not cdate:
                continue
            cdate = str(cdate)[:10]
            bb = day.get("bodyBattery") or {}
            stress = _stress_total(day.get("allDayStress") or {})
            resp = day.get("respiration") or {}
            floors_meters = day.get("floorsAscendedInMeters")
            floors = round(floors_meters / METERS_PER_FLOOR) if floors_meters else None

            conn.execute(
                "INSERT OR IGNORE INTO daily_metrics (date, raw_json) VALUES (?, ?)",
                (cdate, json.dumps(day)),
            )
            updates: list[tuple[str, object]] = []
            for col, val in [
                ("rhr", day.get("currentDayRestingHeartRate") or day.get("restingHeartRate")),
                ("avg_stress", stress.get("averageStressLevel")),
                ("max_stress", stress.get("maxStressLevel")),
                ("body_battery_min", _bb_stat(bb, "LOWEST")),
                ("body_battery_max", _bb_stat(bb, "HIGHEST")),
                ("body_battery_charged", bb.get("chargedValue")),
                ("body_battery_drained", bb.get("drainedValue")),
                ("steps", day.get("totalSteps")),
                ("active_calories", int(day["activeKilocalories"]) if day.get("activeKilocalories") is not None else None),
                ("floors_climbed", floors),
                ("respiration_avg", resp.get("avgWakingRespirationValue")),
                ("intensity_minutes_moderate", day.get("moderateIntensityMinutes")),
                ("intensity_minutes_vigorous", day.get("vigorousIntensityMinutes")),
            ]:
                if val is not None:
                    updates.append((col, val))
            if updates:
                set_clause = ", ".join(f"{col} = COALESCE({col}, ?)" for col, _ in updates)
                params = [v for _, v in updates] + [cdate]
                conn.execute(
                    f"UPDATE daily_metrics SET {set_clause} WHERE date = ?",
                    params,
                )
            n += 1
    return n


# -- sleepData --------------------------------------------------------------

def _ingest_sleep(raw: bytes) -> int:
    data = json.loads(raw)
    if not isinstance(data, list):
        return 0
    n = 0
    with db.connect() as conn:
        for night in data:
            if not isinstance(night, dict):
                continue
            cdate = night.get("calendarDate")
            if not cdate:
                continue
            cdate = str(cdate)[:10]
            deep = night.get("deepSleepSeconds") or 0
            light = night.get("lightSleepSeconds") or 0
            rem = night.get("remSleepSeconds") or 0
            awake = night.get("awakeSleepSeconds") or 0
            total = deep + light + rem  # exclude awake (matches Garmin convention)
            scores = night.get("sleepScores") or {}

            conn.execute(
                "INSERT OR IGNORE INTO daily_metrics (date) VALUES (?)",
                (cdate,),
            )
            updates: list[tuple[str, object]] = []
            for col, val in [
                ("sleep_seconds", total or None),
                ("sleep_deep_seconds", deep or None),
                ("sleep_light_seconds", light or None),
                ("sleep_rem_seconds", rem or None),
                ("sleep_awake_seconds", awake or None),
                ("sleep_score", scores.get("overallScore")),
                ("sleep_quality", scores.get("feedback")),
            ]:
                if val is not None:
                    updates.append((col, val))
            if updates:
                set_clause = ", ".join(f"{col} = COALESCE({col}, ?)" for col, _ in updates)
                params = [v for _, v in updates] + [cdate]
                conn.execute(
                    f"UPDATE daily_metrics SET {set_clause} WHERE date = ?",
                    params,
                )
            n += 1
    return n


# -- VO2 max ----------------------------------------------------------------

def _ingest_vo2(raw: bytes) -> int:
    data = json.loads(raw)
    if not isinstance(data, list):
        return 0
    by_date: dict[str, float] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        cdate = entry.get("calendarDate")
        v = entry.get("vo2MaxValue")
        if not cdate or v is None:
            continue
        # Prefer running VO2 for a runner; otherwise take the highest reading.
        sport = (entry.get("sport") or "").upper()
        cdate = str(cdate)[:10]
        existing = by_date.get(cdate)
        if existing is None or sport == "RUNNING" or v > existing:
            by_date[cdate] = float(v)
    n = 0
    with db.connect() as conn:
        for cdate, vo2 in by_date.items():
            conn.execute(
                "INSERT OR IGNORE INTO daily_metrics (date) VALUES (?)",
                (cdate,),
            )
            conn.execute(
                "UPDATE daily_metrics SET vo2_max = COALESCE(vo2_max, ?) WHERE date = ?",
                (vo2, cdate),
            )
            n += 1
    return n


# -- TrainingHistory --------------------------------------------------------

def _ingest_training_status(raw: bytes) -> int:
    data = json.loads(raw)
    if not isinstance(data, list):
        return 0
    n = 0
    with db.connect() as conn:
        for entry in data:
            if not isinstance(entry, dict):
                continue
            cdate = entry.get("calendarDate")
            status = entry.get("trainingStatus")
            if not cdate or not status:
                continue
            cdate = str(cdate)[:10]
            conn.execute(
                "INSERT OR IGNORE INTO daily_metrics (date) VALUES (?)",
                (cdate,),
            )
            conn.execute(
                "UPDATE daily_metrics SET training_status = COALESCE(training_status, ?) WHERE date = ?",
                (status, cdate),
            )
            n += 1
    return n


# -- summarizedActivities ---------------------------------------------------

def _ingest_activities(raw: bytes) -> int:
    data = json.loads(raw)
    if isinstance(data, list) and data and isinstance(data[0], dict) and "summarizedActivitiesExport" in data[0]:
        activities = data[0]["summarizedActivitiesExport"]
    elif isinstance(data, dict) and "summarizedActivitiesExport" in data:
        activities = data["summarizedActivitiesExport"]
    elif isinstance(data, list):
        activities = data
    else:
        return 0
    if not isinstance(activities, list):
        return 0

    n = 0
    with db.connect() as conn:
        for act in activities:
            if not isinstance(act, dict):
                continue
            activity_id = act.get("activityId")
            if not activity_id:
                continue

            # Times: beginTimestamp is GMT epoch ms; startTimeLocal is local epoch ms (float)
            local_ms = act.get("startTimeLocal") or act.get("beginTimestamp")
            if isinstance(local_ms, (int, float)) and local_ms > 1e11:
                start_dt = datetime.fromtimestamp(local_ms / 1000)
                start_iso = start_dt.isoformat()
                cdate = start_dt.date().isoformat()
            else:
                start_iso = None
                cdate = ""

            duration_ms = act.get("duration")
            duration_s = int(duration_ms / 1000) if isinstance(duration_ms, (int, float)) else None
            moving_ms = act.get("movingDuration")
            moving_s = int(moving_ms / 1000) if isinstance(moving_ms, (int, float)) else None

            # Export uses unusual units: distance & elevation in cm, avgSpeed in
            # m/s ÷ 10. Live garminconnect API returns meters / m·s⁻¹, so we
            # normalise here so the activities table is unit-consistent across
            # both ingest paths.
            distance_m = act["distance"] / 100 if act.get("distance") is not None else None
            elev_gain_m = act["elevationGain"] / 100 if act.get("elevationGain") is not None else None
            elev_loss_m = act["elevationLoss"] / 100 if act.get("elevationLoss") is not None else None
            avg_speed_export = act.get("avgSpeed")
            avg_pace = (100.0 / avg_speed_export) if avg_speed_export else None

            atype = act.get("activityType")
            if isinstance(atype, dict):
                atype = atype.get("typeKey")

            # avgDoubleCadence is steps/min total; avgRunCadence is cycles/min
            # (one foot only). Prefer the total — that's what watch displays.
            cadence = (
                act.get("avgDoubleCadence")
                or act.get("avgRunCadence")
                or act.get("avgBikeCadence")
            )

            row = {
                "activity_id": activity_id,
                "date": cdate,
                "start_time": start_iso,
                "activity_type": atype,
                "activity_name": act.get("name") or act.get("activityName"),
                "duration_seconds": duration_s,
                "moving_seconds": moving_s,
                "distance_meters": distance_m,
                "avg_hr": int(act["avgHr"]) if act.get("avgHr") is not None else None,
                "max_hr": int(act["maxHr"]) if act.get("maxHr") is not None else None,
                "avg_pace_sec_per_km": avg_pace,
                "elevation_gain_meters": elev_gain_m,
                "elevation_loss_meters": elev_loss_m,
                "calories": int(act["calories"]) if act.get("calories") is not None else None,
                "aerobic_te": act.get("aerobicTrainingEffect"),
                "anaerobic_te": act.get("anaerobicTrainingEffect"),
                "training_load": act.get("activityTrainingLoad"),
                "avg_cadence": int(cadence) if cadence is not None else None,
                "vo2_max_estimate": act.get("vO2MaxValue"),
                "weather_temp_c": act.get("temperature"),
                "weather_conditions": None,
                "raw_json": json.dumps(act),
            }
            cols = ", ".join(row.keys())
            placeholders = ", ".join(f":{k}" for k in row.keys())
            conn.execute(
                f"INSERT OR REPLACE INTO activities ({cols}) VALUES ({placeholders})",
                row,
            )

            # HR zones — flat keys hrTimeInZone_0 through hrTimeInZone_4 (seconds)
            for i in range(6):
                key = f"hrTimeInZone_{i}"
                if key in act and act[key] is not None:
                    secs = int(act[key])
                    if secs > 0:
                        conn.execute(
                            "INSERT OR REPLACE INTO activity_hr_zones "
                            "(activity_id, zone, seconds_in_zone) VALUES (?, ?, ?)",
                            (activity_id, i, secs),
                        )
            n += 1
    return n
