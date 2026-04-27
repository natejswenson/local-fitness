"""SQLite schema, connection helpers, and run-state queries.

Schema is idempotent — `init_schema()` is safe to call repeatedly.
All tables use TEXT for dates (ISO YYYY-MM-DD) for SQLite portability.
Raw JSON is preserved on every wellness/activity row so we can re-derive
new fields later without re-pulling from Garmin.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import date as date_cls, datetime, timedelta
from pathlib import Path
from typing import Iterator


def _default_db_path() -> Path:
    """Resolve the SQLite path. Honor LOCAL_FITNESS_DATA_DIR for container
    deployments where /data is a bind-mounted volume; default to the
    host-CLI path when unset."""
    override = os.environ.get("LOCAL_FITNESS_DATA_DIR")
    if override:
        return Path(override) / "fitness.db"
    return Path.home() / "localrepo" / "local-fitness" / "data" / "fitness.db"


DEFAULT_DB_PATH = _default_db_path()

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_metrics (
    date                          TEXT PRIMARY KEY,
    sleep_seconds                 INTEGER,
    sleep_deep_seconds            INTEGER,
    sleep_light_seconds           INTEGER,
    sleep_rem_seconds             INTEGER,
    sleep_awake_seconds           INTEGER,
    sleep_score                   INTEGER,
    sleep_quality                 TEXT,
    rhr                           INTEGER,
    avg_stress                    INTEGER,
    max_stress                    INTEGER,
    body_battery_min              INTEGER,
    body_battery_max              INTEGER,
    body_battery_charged          INTEGER,
    body_battery_drained          INTEGER,
    steps                         INTEGER,
    active_calories               INTEGER,
    floors_climbed                INTEGER,
    avg_spo2                      INTEGER,
    respiration_avg               REAL,
    vo2_max                       REAL,
    training_status               TEXT,
    fitness_age                   INTEGER,
    intensity_minutes_moderate    INTEGER,
    intensity_minutes_vigorous    INTEGER,
    raw_json                      TEXT
);

CREATE TABLE IF NOT EXISTS body_battery_samples (
    date         TEXT NOT NULL,
    timestamp    TEXT NOT NULL,
    value        INTEGER,
    PRIMARY KEY (date, timestamp)
);

CREATE TABLE IF NOT EXISTS stress_samples (
    date         TEXT NOT NULL,
    timestamp    TEXT NOT NULL,
    value        INTEGER,
    PRIMARY KEY (date, timestamp)
);

CREATE TABLE IF NOT EXISTS activities (
    activity_id            INTEGER PRIMARY KEY,
    date                   TEXT NOT NULL,
    start_time             TEXT,
    activity_type          TEXT,
    activity_name          TEXT,
    duration_seconds       INTEGER,
    moving_seconds         INTEGER,
    distance_meters        REAL,
    avg_hr                 INTEGER,
    max_hr                 INTEGER,
    avg_pace_sec_per_km    REAL,
    elevation_gain_meters  REAL,
    elevation_loss_meters  REAL,
    calories               INTEGER,
    aerobic_te             REAL,
    anaerobic_te           REAL,
    training_load          REAL,
    avg_cadence            INTEGER,
    vo2_max_estimate       REAL,
    weather_temp_c         REAL,
    weather_conditions     TEXT,
    raw_json               TEXT
);
CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(date);
CREATE INDEX IF NOT EXISTS idx_activities_type ON activities(activity_type);

CREATE TABLE IF NOT EXISTS activity_hr_zones (
    activity_id      INTEGER NOT NULL,
    zone             INTEGER NOT NULL,
    seconds_in_zone  INTEGER,
    PRIMARY KEY (activity_id, zone)
);

CREATE TABLE IF NOT EXISTS activity_splits (
    activity_id            INTEGER NOT NULL,
    split_index            INTEGER NOT NULL,
    distance_meters        REAL,
    duration_seconds       INTEGER,
    avg_hr                 INTEGER,
    avg_pace_sec_per_km    REAL,
    elevation_gain_meters  REAL,
    PRIMARY KEY (activity_id, split_index)
);

CREATE TABLE IF NOT EXISTS baselines (
    date                          TEXT PRIMARY KEY,
    rhr_60day_mean                REAL,
    rhr_60day_sd                  REAL,
    body_battery_max_60day_mean   REAL,
    body_battery_min_60day_mean   REAL,
    sleep_seconds_60day_mean      REAL,
    sleep_seconds_60day_sd        REAL,
    stress_60day_mean             REAL,
    ctl                           REAL,
    atl                           REAL,
    tsb                           REAL
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at          TEXT NOT NULL,
    completed_at        TEXT,
    status              TEXT NOT NULL,
    last_date_fetched   TEXT,
    error_message       TEXT,
    source              TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_completed ON ingest_runs(completed_at);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
"""


def get_db_path() -> Path:
    path = DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = db_path or get_db_path()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema(db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


def last_known_daily_date(db_path: Path | None = None) -> str | None:
    """Most recent date with any wellness row in `daily_metrics`.

    Used as the resume point for live pulls — honest about what data we
    actually hold, regardless of whether it came from a backfill ZIP or a
    daily pull. The previous query (status='success' AND source='daily')
    was blind to backfill rows, causing the first live pull after a
    backfill to re-fetch 5 years.
    """
    with connect(db_path) as conn:
        row = conn.execute("SELECT MAX(date) AS d FROM daily_metrics").fetchone()
    return row["d"] if row and row["d"] else None


def missing_daily_dates(
    start: date_cls, end: date_cls, db_path: Path | None = None
) -> list[date_cls]:
    """Dates in [start, end] (inclusive) that have no row in daily_metrics."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT date FROM daily_metrics WHERE date >= ? AND date <= ?",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    present = {r["date"] for r in rows}
    out: list[date_cls] = []
    d = start
    while d <= end:
        if d.isoformat() not in present:
            out.append(d)
        d += timedelta(days=1)
    return out


def mark_orphaned_runs(db_path: Path | None = None) -> int:
    """Close out any in_progress runs from prior crashed/killed processes.

    Called at server startup. Any `in_progress` row at boot must be
    orphaned — no Python process is running it. Returns the row count.
    """
    now = datetime.now().isoformat()
    with connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE ingest_runs "
            "SET completed_at = ?, status = 'orphaned', "
            "    error_message = 'Process exited before run completed' "
            "WHERE completed_at IS NULL AND status = 'in_progress'",
            (now,),
        )
        return cur.rowcount


def get_setting(key: str, default: str | None = None, db_path: Path | None = None) -> str | None:
    """Fetch a single user setting (e.g., 'user_name'). Returns default if unset."""
    with connect(db_path) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str, db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def all_settings(db_path: Path | None = None) -> dict[str, str]:
    with connect(db_path) as conn:
        rows = conn.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
    return {r["key"]: r["value"] for r in rows}
