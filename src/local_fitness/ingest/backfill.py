"""Backfill historical Garmin data from the official 'Request your data' ZIP export.

The ZIP layout has changed over the years; this parser walks all members and
dispatches based on filename pattern + content shape. We use INSERT OR IGNORE
for daily rows so that a later live pull (which is more authoritative for
recent data) never gets overwritten — backfill only fills empty cells via
COALESCE updates.

NOTE: this is a best-effort skeleton. The real export format will be confirmed
once Nate's ZIP arrives — fields may need remapping. Detailed format probing
is logged in DEBUG so we can iterate quickly.
"""
from __future__ import annotations

import json
import logging
import zipfile
from datetime import datetime
from pathlib import Path

from .. import db

LOG = logging.getLogger(__name__)


def backfill(zip_path: Path) -> dict:
    db.init_schema()
    if not zip_path.exists():
        raise FileNotFoundError(zip_path)

    counts = {
        "wellness_files": 0,
        "wellness_rows": 0,
        "activities_files": 0,
        "activities_rows": 0,
        "fit_files_skipped": 0,
        "other_skipped": 0,
        "errors": 0,
    }

    with zipfile.ZipFile(zip_path) as zf:
        members = zf.namelist()
        LOG.info("Backfill: %d entries in %s", len(members), zip_path.name)
        for member in members:
            try:
                lower = member.lower()
                if member.endswith("/") or "MACOSX" in member:
                    continue
                if "wellness" in lower and lower.endswith(".json"):
                    n = _ingest_wellness_json(zf.read(member))
                    counts["wellness_files"] += 1
                    counts["wellness_rows"] += n
                elif "summarizedactivities" in lower and lower.endswith(".json"):
                    n = _ingest_activities_json(zf.read(member))
                    counts["activities_files"] += 1
                    counts["activities_rows"] += n
                elif lower.endswith(".fit"):
                    counts["fit_files_skipped"] += 1
                else:
                    counts["other_skipped"] += 1
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


def _ingest_wellness_json(raw: bytes) -> int:
    data = json.loads(raw)
    if isinstance(data, dict):
        entries = [data]
    elif isinstance(data, list):
        entries = data
    else:
        return 0

    n = 0
    field_map = {
        "totalSteps": "steps",
        "restingHeartRate": "rhr",
        "averageStressLevel": "avg_stress",
        "maxStressLevel": "max_stress",
        "moderateIntensityMinutes": "intensity_minutes_moderate",
        "vigorousIntensityMinutes": "intensity_minutes_vigorous",
        "activeKilocalories": "active_calories",
        "floorsAscended": "floors_climbed",
    }

    with db.connect() as conn:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            cdate = entry.get("calendarDate") or entry.get("summaryDate") or entry.get("date")
            if not cdate:
                continue
            cdate = str(cdate)[:10]
            conn.execute(
                "INSERT OR IGNORE INTO daily_metrics (date, raw_json) VALUES (?, ?)",
                (cdate, json.dumps(entry)),
            )
            updates = []
            params: list = []
            for src, dst in field_map.items():
                if entry.get(src) is not None:
                    updates.append(f"{dst} = COALESCE({dst}, ?)")
                    params.append(entry[src])
            if updates:
                params.append(cdate)
                conn.execute(
                    f"UPDATE daily_metrics SET {', '.join(updates)} WHERE date = ?",
                    params,
                )
            n += 1
    return n


def _ingest_activities_json(raw: bytes) -> int:
    data = json.loads(raw)
    if (
        isinstance(data, list)
        and data
        and isinstance(data[0], dict)
        and "summarizedActivitiesExport" in data[0]
    ):
        activities = data[0]["summarizedActivitiesExport"]
    elif isinstance(data, dict) and "summarizedActivitiesExport" in data:
        activities = data["summarizedActivitiesExport"]
    elif isinstance(data, list):
        activities = data
    else:
        return 0

    n = 0
    with db.connect() as conn:
        for act in activities:
            if not isinstance(act, dict):
                continue
            activity_id = act.get("activityId")
            if not activity_id:
                continue

            start_time = act.get("startTimeLocal") or act.get("beginTimestamp")
            if isinstance(start_time, (int, float)) and start_time > 10**12:
                start_time = datetime.fromtimestamp(start_time / 1000).isoformat()
            cdate = (str(start_time) if start_time else "")[:10]

            avg_speed = act.get("avgSpeed") or act.get("averageSpeed")
            duration = act.get("duration")
            if isinstance(duration, (int, float)) and duration > 1e6:
                duration = duration / 1000  # ms → s

            atype = act.get("activityType")
            if isinstance(atype, dict):
                atype = atype.get("typeKey")

            row = {
                "activity_id": activity_id,
                "date": cdate,
                "start_time": str(start_time) if start_time else None,
                "activity_type": atype,
                "activity_name": act.get("name") or act.get("activityName"),
                "duration_seconds": int(duration) if duration is not None else None,
                "moving_seconds": act.get("movingDuration"),
                "distance_meters": act.get("distance"),
                "avg_hr": act.get("avgHr") or act.get("averageHR"),
                "max_hr": act.get("maxHr") or act.get("maxHR"),
                "avg_pace_sec_per_km": (1000.0 / avg_speed) if avg_speed else None,
                "elevation_gain_meters": act.get("elevationGain"),
                "elevation_loss_meters": act.get("elevationLoss"),
                "calories": act.get("calories"),
                "aerobic_te": act.get("aerobicTrainingEffect"),
                "anaerobic_te": act.get("anaerobicTrainingEffect"),
                "training_load": act.get("activityTrainingLoad") or act.get("trainingLoad"),
                "avg_cadence": act.get("avgRunCadence") or act.get("avgBikeCadence"),
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
            n += 1
    return n
