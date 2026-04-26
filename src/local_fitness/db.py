"""SQLite schema, connection helpers, and run-state queries.

Schema is idempotent — `init_schema()` is safe to call repeatedly.
All tables use TEXT for dates (ISO YYYY-MM-DD) for SQLite portability.
Raw JSON is preserved on every wellness/activity row so we can re-derive
new fields later without re-pulling from Garmin.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DEFAULT_DB_PATH = Path.home() / "localrepo" / "local-fitness" / "data" / "fitness.db"

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


def last_successful_daily_date(db_path: Path | None = None) -> str | None:
    """Latest date for which the `daily` ingest run succeeded."""
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT MAX(last_date_fetched) AS d "
            "FROM ingest_runs "
            "WHERE status = 'success' AND source = 'daily'"
        ).fetchone()
    return row["d"] if row and row["d"] else None
