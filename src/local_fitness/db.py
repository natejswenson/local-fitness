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


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _default_db_path() -> Path:
    """Resolve the SQLite path. Honor LOCAL_FITNESS_DATA_DIR for container
    deployments where /data is a bind-mounted volume; default to a
    project-relative `./data/` directory when unset."""
    override = os.environ.get("LOCAL_FITNESS_DATA_DIR")
    if override:
        return Path(override) / "fitness.db"
    return _PROJECT_ROOT / "data" / "fitness.db"


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

-- Per-sample HR trace, fetched on demand (never by the daily sync) for the
-- one activity a report card is being rendered for. Garmin's activity-details
-- endpoint returns ~1700 samples for a 3-mile run, which is why this is NOT
-- pulled for every activity: 747 activities x ~1700 rows to serve a feature
-- that reads one activity at a time is a backfill nobody asked for, and the
-- repeated detail calls are exactly the shape that trips the 429 the token
-- cache was added to avoid. `distance_meters` is cumulative from the start of
-- the activity — the binner needs distance, not time, to place a sample in a
-- tenth-of-a-mile bucket.
CREATE TABLE IF NOT EXISTS activity_hr_samples (
    activity_id      INTEGER NOT NULL,
    sample_index     INTEGER NOT NULL,
    distance_meters  REAL,
    hr               INTEGER,
    PRIMARY KEY (activity_id, sample_index)
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

CREATE TABLE IF NOT EXISTS observations (
    observation_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_on     TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    obs_type        TEXT NOT NULL,
    value_num       REAL,
    value_text      TEXT,
    activity_id     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_obs_date ON observations(observed_on);
CREATE INDEX IF NOT EXISTS idx_obs_type ON observations(obs_type);

CREATE TABLE IF NOT EXISTS training_plans (
    plan_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    status               TEXT NOT NULL,          -- 'draft' | 'active' | 'archived'
    goal_type            TEXT NOT NULL,          -- '5k'|'10k'|'half'|'full'|'custom'
    goal_distance_m      REAL,                   -- nullable: 'custom' may have no canonical distance
    race_date            TEXT NOT NULL,          -- ISO YYYY-MM-DD
    target_time_seconds  INTEGER,                -- nullable for 'just finish'
    title                TEXT,
    ability_snapshot     TEXT,                   -- JSON: AI's current-ability estimate at creation
    created_at           TEXT NOT NULL,          -- ISO timestamp
    committed_at         TEXT                    -- ISO timestamp when draft -> active
);
CREATE INDEX IF NOT EXISTS idx_plans_status ON training_plans(status);
-- Single-active invariant enforced by the DB: a commit race fails loudly rather
-- than silently creating two active plans.
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_plan
    ON training_plans(status) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS plan_workouts (
    workout_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id                INTEGER NOT NULL,     -- -> training_plans.plan_id
    date                   TEXT NOT NULL,        -- ISO YYYY-MM-DD
    seq                    INTEGER NOT NULL DEFAULT 1,  -- intra-day order (AM/PM double-days)
    week_index             INTEGER NOT NULL,     -- 1-based week within the plan
    type                   TEXT NOT NULL,        -- easy|long|tempo|interval|rest|race|cross
    target_distance_m      REAL,                 -- null for rest / by-feel
    target_pace_sec_per_km REAL,                 -- null for rest / easy-by-feel
    target_duration_sec    INTEGER,              -- used for interval/tempo/cross adherence
    description            TEXT NOT NULL         -- prose prescription
);
CREATE INDEX IF NOT EXISTS idx_plan_workouts_plan ON plan_workouts(plan_id);
CREATE INDEX IF NOT EXISTS idx_plan_workouts_date ON plan_workouts(date);
-- One prescription per (plan, date, seq): validate_plan_input dedups at
-- propose time; this makes the invariant structural, so a validation bypass
-- fails loudly instead of leaving update_active_workout's UPDATE hitting
-- two rows.
CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_workouts_day
    ON plan_workouts(plan_id, date, seq);
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


@contextmanager
def connect_readonly(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open the DB in SQLite read-only mode (engine-enforced, not keyword-based).

    Used by the run_sql tool so ANY write/DDL (INSERT/UPDATE/DELETE/DROP/...)
    raises `sqlite3.OperationalError: attempt to write a readonly database`
    regardless of how the query is phrased — the read-only URI is the real
    gate, not a denylist. Mirrors `connect`'s `row_factory = sqlite3.Row`, but
    opens `mode=ro` and never commits. Extension loading is left disabled
    (SQLite's default) so `load_extension` stays blocked.
    """
    path = db_path or get_db_path()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_schema(db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        # `activities.source` can't live in SCHEMA: SQLite has no
        # `ADD COLUMN IF NOT EXISTS`, so a second executescript would raise
        # "duplicate column". Guard the ALTER on table_info to stay idempotent.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(activities)")}
        if "source" not in cols:
            conn.execute("ALTER TABLE activities ADD COLUMN source TEXT DEFAULT 'garmin'")


def last_known_daily_date(
    db_path: Path | None = None, conn: sqlite3.Connection | None = None
) -> str | None:
    """Most recent date with any wellness row in `daily_metrics`.

    Used as the resume point for live pulls — honest about what data we
    actually hold, regardless of whether it came from a backfill ZIP or a
    daily pull. The previous query (status='success' AND source='daily')
    was blind to backfill rows, causing the first live pull after a
    backfill to re-fetch 5 years.

    Accepts an already-open ``conn`` to let hot-path callers share one
    connection instead of opening a fresh one per lookup; behavior is
    unchanged when omitted.
    """
    if conn is not None:
        row = conn.execute("SELECT MAX(date) AS d FROM daily_metrics").fetchone()
        return row["d"] if row and row["d"] else None
    with connect(db_path) as c:
        row = c.execute("SELECT MAX(date) AS d FROM daily_metrics").fetchone()
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


def get_setting(
    key: str,
    default: str | None = None,
    db_path: Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> str | None:
    """Fetch a single user setting (e.g., 'user_name'). Returns default if unset.

    Accepts an already-open ``conn`` to let hot-path callers share one
    connection instead of opening a fresh one per lookup; behavior is
    unchanged when omitted.
    """
    if conn is not None:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
    with connect(db_path) as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str, db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def all_settings(
    db_path: Path | None = None, conn: sqlite3.Connection | None = None
) -> dict[str, str]:
    """Accepts an already-open ``conn`` to let hot-path callers share one
    connection instead of opening a fresh one per lookup; behavior is
    unchanged when omitted."""
    if conn is not None:
        rows = conn.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
        return {r["key"]: r["value"] for r in rows}
    with connect(db_path) as c:
        rows = c.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
    return {r["key"]: r["value"] for r in rows}
