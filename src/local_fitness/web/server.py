"""FastAPI app exposing the fitness DB + agent over HTTP.

Bound to 127.0.0.1 only — never expose this to the network without
authentication. The agent has read-only DB access via tools but `run_sql`
will execute any SELECT, so a malicious caller could exfiltrate data.

Endpoints:
  GET  /api/status                  — DB row counts + last ingest
  GET  /api/today                   — today's metrics + last 7 days + baseline
  GET  /api/baseline                — current baseline row
  GET  /api/metric/{name}?days=N    — daily time series for a metric
  GET  /api/training-load?days=N    — CTL/ATL/TSB time series
  GET  /api/workouts?type&days&limit — filtered workout list
  GET  /api/workout/{id}            — single workout + splits + zones
  GET  /api/brief                   — today's briefing markdown (cached if exists)
  POST /api/brief/generate          — force-regenerate today's briefing
  POST /api/sync                    — kick off a background pull (throttled)
  GET  /api/sync/status             — running flag + last completed run info
  POST /api/chat                    — streaming agent chat (NDJSON)
  GET  /                            — serve the SPA index.html
  GET  /assets/*                    — serve built frontend assets
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sys
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import db, notes as user_notes_mod
from ..agent import briefing as briefing_mod
from ..agent import prompts
from ..agent import tools as agent_tools
from ..ingest import baselines as baselines_mod
from ..ingest import daily as daily_ingest

LOG = logging.getLogger(__name__)

# Bearer token gating /api/* endpoints. When unset AND the server binds to
# loopback only, requests are accepted (host-CLI dev convenience). When
# binding to a non-loopback host (container behind Traefik), `serve()`
# refuses to start without one — see the startup check.
API_TOKEN = os.environ.get("LOCAL_FITNESS_API_TOKEN") or None

# Per-IP rate limits for the expensive endpoints — chat and brief
# generation both hit Claude. Anyone on the LAN with API token access
# could otherwise loop these and drain the subscription. Bucket is an
# in-memory deque of recent request timestamps; refilled by elapsed time.
RATE_LIMITED_PREFIXES = ("/api/chat", "/api/brief/generate")
RATE_LIMIT_WINDOW_SEC = 60.0
RATE_LIMIT_MAX_REQUESTS = 20  # 20 requests per IP per minute on Claude-cost endpoints
_rate_buckets: dict[str, deque[float]] = defaultdict(deque)
_rate_lock = asyncio.Lock()

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
WEB_DIST = _PROJECT_ROOT / "web" / "dist"
# Honor the same env override as briefing.py — /briefings inside the
# container, project-relative `./briefings/` otherwise.
BRIEFINGS_DIR = Path(
    os.environ.get(
        "LOCAL_FITNESS_BRIEFINGS_DIR",
        str(_PROJECT_ROOT / "briefings"),
    )
)

# Auto-sync settings — bite-sized: never pull more than this many days at once,
# and don't pull more than once per this many minutes from the UI trigger.
SYNC_MAX_DAYS = 30
SYNC_THROTTLE_SECONDS = 15 * 60

# Responsive-retry backoffs after a transient failure (Garmin 429, network
# blip). Recovers in seconds–minutes rather than waiting the full throttle
# window. Auth failures and not_configured do NOT auto-retry.
SYNC_RETRY_BACKOFFS = [60, 5 * 60, 15 * 60]


# Per-session ClaudeSDKClient so multi-turn chat keeps context.
_chat_sessions: dict[str, ClaudeSDKClient] = {}
_session_lock = asyncio.Lock()

# Auto-sync background-task state. We keep only the running flag in memory;
# completion history is read from the persistent ingest_runs table so the
# state survives server restarts.
_sync_running = False
_sync_started_at: datetime | None = None
_sync_lock = asyncio.Lock()
_sync_task: asyncio.Task | None = None
_retry_task: asyncio.Task | None = None
_retry_count = 0


def _options(model: str) -> ClaudeAgentOptions:
    user_name = db.get_setting("user_name", prompts.DEFAULT_USER_NAME)
    return ClaudeAgentOptions(
        mcp_servers={agent_tools.SERVER_NAME: agent_tools.make_server()},
        allowed_tools=agent_tools.allowed_tool_names(),
        system_prompt=prompts.system_prompt(user_name),
        model=model,
        permission_mode="bypassPermissions",
        max_turns=50,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_schema()
    # Any `in_progress` row at boot must be from a prior crashed/killed
    # process — mark them so the SyncIndicator can surface "Sync interrupted"
    # rather than silently rendering nothing.
    orphaned = db.mark_orphaned_runs()
    if orphaned:
        LOG.info("Marked %d orphaned ingest_runs row(s) at startup", orphaned)
    yield

    # Cancel any pending retry timer so we don't leave a dangling task.
    global _retry_task, _sync_task
    if _retry_task and not _retry_task.done():
        _retry_task.cancel()
    # Give an in-flight sync up to 2s to wrap (so daily.pull's finally
    # block can land its closing UPDATE). If it's stuck deep in garth,
    # let it ride — orphan recovery on next boot will reconcile.
    if _sync_task and not _sync_task.done():
        try:
            await asyncio.wait_for(_sync_task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

    # Tear down any live chat sessions on shutdown
    async with _session_lock:
        for sid, client in list(_chat_sessions.items()):
            try:
                await client.__aexit__(None, None, None)
            except Exception:
                LOG.warning("Failed to close chat session %s on shutdown", sid)
            _chat_sessions.pop(sid, None)


app = FastAPI(title="local-fitness", lifespan=lifespan)

# CORS for dev mode (Vite on :5173 → API on :8765)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- Security middleware -----------------------------------------------
# Order: outermost wraps innermost. Defined later in the file = outer.
# Rate-limit runs OUTSIDE auth so a flood with bad tokens is still capped.

def _is_public_path(path: str) -> bool:
    """Routes anyone can hit: liveness probe and the SPA shell + its
    assets. Everything under /api/ requires the token, including
    /api/auth/verify — that endpoint's whole purpose is to bounce a
    bad-token request to 401 so the login screen can re-prompt."""
    if path == "/health":
        return True
    if not path.startswith("/api/"):
        return True  # SPA shell, /assets/*, etc. — handled by static routes
    return False


@app.middleware("http")
async def require_api_token(request: Request, call_next):
    """Bearer-token gate for /api/* endpoints.

    Off when ``LOCAL_FITNESS_API_TOKEN`` is unset (dev convenience on
    loopback). On when set: every /api/* request must carry
    ``Authorization: Bearer <token>``. Constant-time comparison prevents
    timing-side-channel guessing.
    """
    path = request.url.path
    if API_TOKEN is None or _is_public_path(path):
        return await call_next(request)
    auth_header = request.headers.get("authorization", "")
    expected = f"Bearer {API_TOKEN}"
    if not secrets.compare_digest(auth_header, expected):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    """Per-IP token bucket on Claude-cost endpoints.

    Loopback IPs are exempt — host-CLI dev should never get throttled by
    its own UI. The bucket is purely in-memory; restart resets state,
    which is fine for a single-instance personal app.
    """
    path = request.url.path
    if not any(path.startswith(p) for p in RATE_LIMITED_PREFIXES):
        return await call_next(request)
    client_ip = request.client.host if request.client else "unknown"
    if client_ip in ("127.0.0.1", "::1", "localhost"):
        return await call_next(request)
    now = time.monotonic()
    cutoff = now - RATE_LIMIT_WINDOW_SEC
    async with _rate_lock:
        bucket = _rate_buckets[client_ip]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= RATE_LIMIT_MAX_REQUESTS:
            retry_after = int(bucket[0] + RATE_LIMIT_WINDOW_SEC - now) + 1
            LOG.warning("Rate limit hit for %s on %s (retry in %ds)", client_ip, path, retry_after)
            return JSONResponse(
                {"error": "rate_limited", "retry_after_seconds": retry_after},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        bucket.append(now)
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Defense-in-depth headers. Cheap to add, no functional cost."""
    response: Response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    return response


# ---------- Auth-verify probe -------------------------------------------------
# The login screen calls this to validate a freshly-pasted token before
# storing it in localStorage. Returns 200 when the request reaches the
# handler (auth middleware passed), 401 otherwise. Intentionally trivial.

@app.get("/api/auth/verify")
async def api_auth_verify(request: Request) -> dict:
    # By the time we get here, the auth middleware has already validated
    # the token (or there's no token configured). Either way it's "ok".
    return {"ok": True, "auth_required": API_TOKEN is not None}


# ---------------------------------------------------------------- API: data --

@app.get("/health")
async def api_health() -> dict:
    """Lightweight liveness probe — used by Traefik's healthcheck.
    Does not touch DB or external services so it stays fast even when
    the agent is busy or Garmin is unreachable."""
    return {"status": "ok"}


@app.get("/api/config")
async def api_config() -> dict:
    settings = db.all_settings()
    return {
        "user_name": settings.get("user_name", prompts.DEFAULT_USER_NAME),
        "settings": settings,
    }


@app.get("/api/status")
async def api_status() -> dict:
    with db.connect() as conn:
        counts = {}
        for table in ("daily_metrics", "activities", "baselines",
                      "body_battery_samples", "stress_samples"):
            counts[table] = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        date_range = conn.execute(
            "SELECT MIN(date) AS first, MAX(date) AS last FROM daily_metrics"
        ).fetchone()
        last_run = conn.execute(
            "SELECT * FROM ingest_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
    return {
        "row_counts": counts,
        "date_range": dict(date_range) if date_range else None,
        "last_ingest_run": dict(last_run) if last_run else None,
    }


@app.get("/api/today")
async def api_today() -> dict:
    today = date.today().isoformat()
    week_ago = (date.today() - timedelta(days=14)).isoformat()
    with db.connect() as conn:
        recent = [dict(r) for r in conn.execute(
            "SELECT date, sleep_seconds, sleep_score, rhr, avg_stress, max_stress, "
            "body_battery_min, body_battery_max, steps, training_status, "
            "intensity_minutes_moderate, intensity_minutes_vigorous "
            "FROM daily_metrics WHERE date >= ? ORDER BY date DESC",
            (week_ago,),
        ).fetchall()]
        baseline = conn.execute(
            "SELECT * FROM baselines WHERE date <= ? ORDER BY date DESC LIMIT 1",
            (today,),
        ).fetchone()
        latest = conn.execute(
            "SELECT * FROM daily_metrics ORDER BY date DESC LIMIT 1"
        ).fetchone()
    return {
        "today": today,
        "latest": dict(latest) if latest else None,
        "recent_14d": recent,
        "baseline": dict(baseline) if baseline else None,
    }


_ALLOWED_METRICS = {
    "rhr", "sleep_seconds", "sleep_score",
    "avg_stress", "max_stress",
    "body_battery_min", "body_battery_max",
    "body_battery_charged", "body_battery_drained",
    "steps", "vo2_max", "active_calories",
    "intensity_minutes_moderate", "intensity_minutes_vigorous",
    "respiration_avg",
}


@app.get("/api/metric/{name}")
async def api_metric(name: str, days: int = Query(90, ge=1, le=2000)) -> dict:
    if name not in _ALLOWED_METRICS:
        raise HTTPException(400, f"unknown metric '{name}'")
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            f"SELECT date, {name} AS value FROM daily_metrics "
            f"WHERE date >= ? AND {name} IS NOT NULL ORDER BY date",
            (cutoff,),
        ).fetchall()]
        baseline_col = f"{name}_60day_mean"
        # Only RHR and sleep_seconds have baseline columns
        baseline = None
        if name in ("rhr", "sleep_seconds"):
            row = conn.execute(
                f"SELECT date, {baseline_col} AS value FROM baselines "
                f"WHERE date >= ? AND {baseline_col} IS NOT NULL ORDER BY date",
                (cutoff,),
            ).fetchall()
            baseline = [dict(r) for r in row]
    return {"metric": name, "days": days, "values": rows, "baseline": baseline}


@app.get("/api/training-load")
async def api_training_load(days: int = Query(180, ge=7, le=2000)) -> dict:
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT date, ctl, atl, tsb FROM baselines "
            "WHERE date >= ? AND ctl IS NOT NULL ORDER BY date",
            (cutoff,),
        ).fetchall()]
    return {"days": days, "values": rows}


@app.get("/api/workouts")
async def api_workouts(
    activity_type: str | None = None,
    days: int | None = Query(None, ge=1, le=2000),
    limit: int = Query(100, ge=1, le=1000),
) -> dict:
    where: list[str] = []
    params: list = []
    if activity_type:
        where.append("activity_type LIKE ?")
        params.append(f"%{activity_type}%")
    if days:
        where.append("date >= ?")
        params.append((date.today() - timedelta(days=days)).isoformat())
    sql_where = (" WHERE " + " AND ".join(where)) if where else ""
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            f"""SELECT activity_id, date, start_time, activity_type, activity_name,
                       duration_seconds, distance_meters, avg_hr, max_hr,
                       avg_pace_sec_per_km, elevation_gain_meters,
                       aerobic_te, anaerobic_te, training_load
                FROM activities {sql_where}
                ORDER BY start_time DESC LIMIT ?""",
            (*params, limit),
        ).fetchall()]
    return {"workouts": rows}


@app.get("/api/workout/{activity_id}")
async def api_workout(activity_id: int) -> dict:
    with db.connect() as conn:
        act = conn.execute(
            "SELECT * FROM activities WHERE activity_id = ?", (activity_id,)
        ).fetchone()
        if not act:
            raise HTTPException(404, "workout not found")
        zones = [dict(r) for r in conn.execute(
            "SELECT zone, seconds_in_zone FROM activity_hr_zones "
            "WHERE activity_id = ? ORDER BY zone",
            (activity_id,),
        ).fetchall()]
        splits = [dict(r) for r in conn.execute(
            "SELECT * FROM activity_splits WHERE activity_id = ? ORDER BY split_index",
            (activity_id,),
        ).fetchall()]
    activity = dict(act)
    activity.pop("raw_json", None)
    return {"activity": activity, "hr_zones": zones, "splits": splits}


# ---------------------------------------------------------------- API: dashboards --
# Custom dashboards beyond the per-metric / single-workout endpoints.
# Heatmap, strength tracker, and pace-efficiency views.

def _percentile_ranks(
    pairs: list[tuple[str, float]], lower_is_better: bool,
) -> dict[str, float]:
    """Map date → percent-rank (0..100). 0% is the BEST value for the
    metric (so "top 4%" reads naturally regardless of which direction
    is "good"). Ties resolve by the standard PERCENT_RANK formula —
    earlier-sorted rows get the lower rank.
    """
    if not pairs:
        return {}
    sorted_pairs = sorted(pairs, key=lambda p: p[1], reverse=not lower_is_better)
    n = len(sorted_pairs)
    if n == 1:
        return {sorted_pairs[0][0]: 0.0}
    return {d: (i / (n - 1)) * 100.0 for i, (d, _) in enumerate(sorted_pairs)}


@app.get("/api/activity-heatmap")
async def api_activity_heatmap(days: int = Query(365, ge=7, le=2000)) -> dict:
    """Per-day data for a calendar heatmap, enriched with everything that
    informs the cell color.

    Spine is `daily_metrics` (every day the watch was worn) so rest-day
    wellness is included alongside active days — no follow-up fetch.
    Each row carries:
      - activity_count, total_load, total_duration_seconds, dominant_type,
        activities[]  (only populated for active days; rest days get 0/None)
      - wellness        — RHR, sleep, body battery, stress, steps
      - baseline        — 60-day means for delta-vs-baseline color cues
      - load_state      — CTL / ATL / TSB on that date (Banister)
      - recovery_pct    — percentile rank within the visible window for
                          each recovery marker (0% = best, 100% = worst)

    Recovery percentiles are computed across ALL days with daily_metrics
    in the window, not just training days, so "top 4% RHR" means
    "lower than 96% of recent days" — period — without bias from the
    training-day population.

    Response is ≈250 KB at the 2y window. Within budget.
    """
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            """
            SELECT dm.date,
                   dm.rhr,
                   dm.sleep_seconds,
                   dm.sleep_score,
                   dm.body_battery_max,
                   dm.body_battery_min,
                   dm.avg_stress,
                   dm.steps,
                   b.rhr_60day_mean              AS rhr_60d,
                   b.sleep_seconds_60day_mean    AS sleep_seconds_60d,
                   b.body_battery_max_60day_mean AS body_battery_max_60d,
                   b.stress_60day_mean           AS stress_60d,
                   b.ctl,
                   b.atl,
                   b.tsb,
                   ag.activity_count,
                   ag.total_load,
                   ag.total_duration_seconds
            FROM daily_metrics dm
            LEFT JOIN baselines b ON b.date = dm.date
            LEFT JOIN (
                SELECT date,
                       COUNT(*)                              AS activity_count,
                       COALESCE(SUM(training_load), 0)       AS total_load,
                       COALESCE(SUM(duration_seconds), 0)    AS total_duration_seconds
                FROM activities
                WHERE date >= ?
                GROUP BY date
            ) ag ON ag.date = dm.date
            WHERE dm.date >= ?
            ORDER BY dm.date
            """,
            (cutoff, cutoff),
        ).fetchall()]

        activity_rows = conn.execute(
            """
            SELECT activity_id, date, activity_type, activity_name,
                   duration_seconds, distance_meters, training_load,
                   avg_hr, max_hr, avg_pace_sec_per_km
            FROM activities
            WHERE date >= ?
            ORDER BY date, start_time
            """,
            (cutoff,),
        ).fetchall()

        type_rows = conn.execute(
            """
            SELECT date, activity_type, COUNT(*) AS n
            FROM activities
            WHERE date >= ?
            GROUP BY date, activity_type
            ORDER BY date, n DESC
            """,
            (cutoff,),
        ).fetchall()

    activities_by_date: dict[str, list[dict]] = {}
    for ar in activity_rows:
        bucket = activities_by_date.setdefault(ar["date"], [])
        if len(bucket) < 5:  # cap so a freak day can't bloat the response
            bucket.append({
                "activity_id": ar["activity_id"],
                "type": ar["activity_type"],
                "name": ar["activity_name"],
                "duration_seconds": ar["duration_seconds"],
                "distance_meters": ar["distance_meters"],
                "training_load": ar["training_load"],
                "avg_hr": ar["avg_hr"],
                "max_hr": ar["max_hr"],
                "avg_pace_sec_per_km": ar["avg_pace_sec_per_km"],
            })

    dominant_by_date: dict[str, str] = {}
    for tr in type_rows:
        dominant_by_date.setdefault(tr["date"], tr["activity_type"])

    # Recovery-marker percentile ranks across every daily_metrics row in
    # the window. NULLs are excluded from each metric's population so a
    # missing sleep value doesn't shift the percentile of days that DO
    # have sleep recorded.
    rhr_pcts = _percentile_ranks(
        [(r["date"], r["rhr"]) for r in rows if r["rhr"] is not None],
        lower_is_better=True,
    )
    sleep_pcts = _percentile_ranks(
        [(r["date"], r["sleep_seconds"]) for r in rows if r["sleep_seconds"] is not None],
        lower_is_better=False,
    )
    bb_pcts = _percentile_ranks(
        [(r["date"], r["body_battery_max"]) for r in rows if r["body_battery_max"] is not None],
        lower_is_better=False,
    )
    stress_pcts = _percentile_ranks(
        [(r["date"], r["avg_stress"]) for r in rows if r["avg_stress"] is not None],
        lower_is_better=True,
    )

    enriched: list[dict] = []
    for r in rows:
        d = r["date"]
        enriched.append({
            "date": d,
            "activity_count": r["activity_count"] or 0,
            "total_load": r["total_load"] or 0,
            "total_duration_seconds": r["total_duration_seconds"] or 0,
            "dominant_type": dominant_by_date.get(d),
            "activities": activities_by_date.get(d, []),
            "wellness": {
                "rhr": r["rhr"],
                "sleep_seconds": r["sleep_seconds"],
                "sleep_score": r["sleep_score"],
                "body_battery_max": r["body_battery_max"],
                "body_battery_min": r["body_battery_min"],
                "avg_stress": r["avg_stress"],
                "steps": r["steps"],
            },
            "baseline": {
                "rhr_60d": r["rhr_60d"],
                "sleep_seconds_60d": r["sleep_seconds_60d"],
                "body_battery_max_60d": r["body_battery_max_60d"],
                "stress_60d": r["stress_60d"],
            },
            "load_state": {
                "ctl": r["ctl"],
                "atl": r["atl"],
                "tsb": r["tsb"],
            },
            "recovery_pct": {
                "rhr": rhr_pcts.get(d),
                "sleep_seconds": sleep_pcts.get(d),
                "body_battery_max": bb_pcts.get(d),
                "avg_stress": stress_pcts.get(d),
            },
        })

    return {
        "days": days,
        "start_date": cutoff,
        "end_date": date.today().isoformat(),
        "values": enriched,
    }


# Garmin-reported strength activity types. Loose match against the
# union; not a hardcoded set so "strength_training" / "indoor_climbing"
# / future variants pass without code change.
_STRENGTH_TYPE_PATTERNS = ("strength_training", "weight_training", "strength")


@app.get("/api/strength-volume")
async def api_strength_volume(weeks: int = Query(104, ge=1, le=520)) -> dict:
    """Weekly aggregates of strength-tagged activities.

    Default lookback is 104 weeks (2 years) because Garmin Instinct
    Solar doesn't natively log strength frequently — short windows
    often look empty even when historical data is rich. Returns
    sessions / total duration / total load per week, plus the most
    recent session date so the frontend can show a freshness signal.
    """
    cutoff = (date.today() - timedelta(weeks=weeks)).isoformat()
    type_filter = " OR ".join(["activity_type LIKE ?"] * len(_STRENGTH_TYPE_PATTERNS))
    type_params = [f"%{p}%" for p in _STRENGTH_TYPE_PATTERNS]
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            f"""
            SELECT
              strftime('%Y-%W', date) AS iso_week,
              MIN(date) AS week_start,
              COUNT(*) AS sessions,
              COALESCE(SUM(duration_seconds), 0) / 60.0 AS total_duration_min,
              COALESCE(SUM(training_load), 0) AS total_load,
              COALESCE(SUM(calories), 0) AS total_calories
            FROM activities
            WHERE date >= ? AND ({type_filter})
            GROUP BY iso_week
            ORDER BY week_start
            """,
            (cutoff, *type_params),
        ).fetchall()]
        last_session = conn.execute(
            f"""
            SELECT date FROM activities
            WHERE {type_filter}
            ORDER BY date DESC LIMIT 1
            """,
            type_params,
        ).fetchone()
    return {
        "weeks": weeks,
        "start_date": cutoff,
        "end_date": date.today().isoformat(),
        "values": rows,
        "last_session_date": last_session["date"] if last_session else None,
        "total_sessions": sum(r["sessions"] for r in rows),
    }


@app.get("/api/pace-efficiency")
async def api_pace_efficiency(
    days: int = Query(180, ge=7, le=2000),
    min_distance_km: float = Query(1.0, ge=0.0, le=200.0),
) -> dict:
    """Per-run HR/pace efficiency series with TSB overlay.

    The "efficiency" signal is HR per km/h: `avg_hr * (avg_pace_sec_per_km
    / 3600)`. Lower = better (less HR for the same speed). Trends UP
    when fatigue or detraining set in — overlaying TSB (negative =
    accumulated fatigue) lets the chart show whether the rise tracks
    intentional load or unrecovered drift.

    Filtered to running-family types and a minimum distance so 5-minute
    treadmill warm-ups don't pollute the trend. Distance and pace are
    bounds-checked at the SQL level.
    """
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    min_distance_m = min_distance_km * 1000.0
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            """
            SELECT a.date,
                   a.start_time,
                   a.activity_type,
                   a.activity_name,
                   a.avg_hr,
                   a.avg_pace_sec_per_km,
                   a.distance_meters,
                   a.duration_seconds,
                   a.training_load,
                   b.tsb,
                   b.ctl,
                   b.atl
            FROM activities a
            LEFT JOIN baselines b ON b.date = a.date
            WHERE a.date >= ?
              AND a.activity_type LIKE '%running%'
              AND a.avg_hr IS NOT NULL
              AND a.avg_pace_sec_per_km IS NOT NULL
              AND a.avg_pace_sec_per_km > 0
              AND a.distance_meters >= ?
            ORDER BY a.start_time
            """,
            (cutoff, min_distance_m),
        ).fetchall()]
    # Compute the efficiency ratio in Python so it stays explicit.
    for r in rows:
        pace_sec = r["avg_pace_sec_per_km"] or 0
        hr = r["avg_hr"] or 0
        # HR per km/h. (km/h = 3600 / pace_sec_per_km)
        r["hr_per_kmh"] = round(hr * pace_sec / 3600.0, 2) if pace_sec else None
    return {
        "days": days,
        "min_distance_km": min_distance_km,
        "start_date": cutoff,
        "end_date": date.today().isoformat(),
        "values": rows,
    }


# ---------------------------------------------------------------- API: brief --

@app.get("/api/brief")
async def api_brief() -> dict:
    """Return today's structured Brief if cached, else null.

    Also returns `data_through_date` so the UI can detect when newer
    data has landed since the brief was generated and offer a
    regenerate prompt.
    """
    brief = briefing_mod.load_today()
    data_through = db.last_known_daily_date()
    if brief:
        return {
            "date": brief.date,
            "brief": brief.model_dump(),
            "cached": True,
            "data_through_date": data_through,
        }
    return {
        "date": date.today().isoformat(),
        "brief": None,
        "cached": False,
        "data_through_date": data_through,
    }


class BriefGenerateRequest(BaseModel):
    model: str = "claude-sonnet-4-6"


@app.post("/api/brief/generate")
async def api_brief_generate(req: BriefGenerateRequest) -> dict:
    """Force-regenerate today's brief and return the structured object."""
    t_endpoint_start = time.perf_counter()
    try:
        await asyncio.to_thread(briefing_mod.generate_and_save, model=req.model)
        brief = briefing_mod.load_today()
        return {
            "date": date.today().isoformat(),
            "brief": brief.model_dump() if brief else None,
            "cached": False,
            "data_through_date": db.last_known_daily_date(),
        }
    finally:
        total_ms = (time.perf_counter() - t_endpoint_start) * 1000
        LOG.info(
            "endpoint_timing path=/api/brief/generate total_ms=%.1f model=%s",
            total_ms,
            req.model,
        )


@app.post("/api/brief/generate/stream")
async def api_brief_generate_stream(req: BriefGenerateRequest) -> StreamingResponse:
    """Stream takeaways as they're parsed from the model's accumulating output.

    Wire format: NDJSON (one ``{...}\\n`` event per line). Matches the chat
    endpoint's pattern so the frontend reuses the same fetch+reader idiom
    (avoids EventSource / iOS-Safari SSE quirks). The final ``done`` event
    carries the full Brief and the data_through_date so the UI can update
    its cached state without a follow-up GET.
    """
    t_endpoint_start = time.perf_counter()
    model = req.model

    async def event_stream() -> AsyncIterator[bytes]:
        cancelled = False
        try:
            async for evt in briefing_mod.generate_streaming(model=model):
                if evt.get("type") == "done":
                    # Annotate the final event with the data-through date so
                    # the client can refresh its staleness banner.
                    evt = {**evt, "data_through_date": db.last_known_daily_date()}
                yield (json.dumps(evt) + "\n").encode("utf-8")
        except asyncio.CancelledError:
            # Client disconnected mid-stream. CancelledError is a BaseException
            # so the previous `except Exception` missed it — leaving 200s+
            # regens that completed without saving a brief and without any
            # error log. Log explicitly, then re-raise so FastAPI cleans up.
            cancelled = True
            LOG.warning("Brief stream cancelled by client disconnect")
            raise
        except Exception as e:
            LOG.exception("Brief stream failed")
            yield (json.dumps({"type": "error", "message": str(e)}) + "\n").encode("utf-8")
        finally:
            total_ms = (time.perf_counter() - t_endpoint_start) * 1000
            LOG.info(
                "endpoint_timing path=/api/brief/generate/stream total_ms=%.1f model=%s cancelled=%s",
                total_ms,
                model,
                cancelled,
            )

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={
            # Defeat any reverse-proxy buffering (Traefik passes through fine
            # but explicit beats implicit; also a hint to nginx-style proxies).
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
        },
    )


# ---------------------------------------------------------------- API: sync --

def _last_sync_run() -> dict | None:
    """Read the most recent daily-source run from ingest_runs."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM ingest_runs WHERE source = 'daily' "
            "ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def _sync_state_dict() -> dict:
    """Snapshot the current sync state for the API."""
    last = _last_sync_run()
    last_completed = last["completed_at"] if last else None
    last_status = last["status"] if last else None
    next_eligible_at: str | None = None
    seconds_until_eligible = 0
    if last_completed:
        try:
            done = datetime.fromisoformat(last_completed)
            eligible = done + timedelta(seconds=SYNC_THROTTLE_SECONDS)
            if datetime.now() < eligible:
                next_eligible_at = eligible.isoformat()
                seconds_until_eligible = int((eligible - datetime.now()).total_seconds())
        except ValueError:
            pass

    # Data freshness is the truth the UI cares about: "what's the most
    # recent day we have wellness data for?" — independent of whether a
    # particular run succeeded.
    data_through = db.last_known_daily_date()
    days_behind = 0
    if data_through:
        try:
            dt = date.fromisoformat(data_through)
            days_behind = max(0, (date.today() - dt).days)
        except ValueError:
            pass

    return {
        "is_running": _sync_running,
        "started_at": _sync_started_at.isoformat() if _sync_started_at else None,
        "last_status": last_status,
        "last_completed_at": last_completed,
        "last_date_fetched": last["last_date_fetched"] if last else None,
        "last_error": last["error_message"] if last else None,
        "throttle_seconds": SYNC_THROTTLE_SECONDS,
        "next_eligible_at": next_eligible_at,
        "seconds_until_eligible": seconds_until_eligible,
        "max_days_per_pull": SYNC_MAX_DAYS,
        "data_through_date": data_through,
        "days_behind": days_behind,
    }


def _is_transient(status: str | None, error: str | None) -> bool:
    """True for 429s, network blips, and similar — worth retrying soon."""
    if status not in ("partial", "failure"):
        return False
    if not error:
        return False
    err = error.lower()
    return any(
        marker in err
        for marker in (
            "429", "rate limit", "rate-limit",
            "timeout", "timed out",
            "connection", "connect error", "network",
            "temporarily", "service unavailable", "503", "502",
        )
    )


async def _run_sync():
    """Worker: call daily.pull in a thread, then recompute baselines on new data.

    On transient failure (429 / network), arm a backoff retry. On success or
    user-actionable failure (auth_failure, not_configured), reset retry count.
    """
    global _sync_running, _sync_started_at, _retry_count
    try:
        result = await asyncio.to_thread(daily_ingest.pull, max_days=SYNC_MAX_DAYS)
        LOG.info("Auto-sync result: %s", result)
        status = result.get("status")
        if status == "success":
            _retry_count = 0
            if result.get("days_pulled", 0) > 0:
                await asyncio.to_thread(baselines_mod.recompute, lookback_days=90)
                LOG.info("Auto-sync recomputed baselines after %d new days", result["days_pulled"])
        elif _is_transient(status, result.get("error")):
            _schedule_retry()
        else:
            # auth_failure / not_configured / non-transient failure — don't
            # auto-retry; user has to act. Reset count so future success
            # has a clean slate.
            _retry_count = 0
    except Exception:
        LOG.exception("Auto-sync worker crashed")
    finally:
        _sync_running = False
        _sync_started_at = None


def _schedule_retry() -> None:
    """Arm a one-shot retry on transient failure with exponential backoff."""
    global _retry_count, _retry_task
    delay = SYNC_RETRY_BACKOFFS[min(_retry_count, len(SYNC_RETRY_BACKOFFS) - 1)]
    _retry_count += 1
    LOG.info("Scheduling sync retry in %ds (attempt %d)", delay, _retry_count)

    async def _retry_after_delay() -> None:
        try:
            await asyncio.sleep(delay)
            await _trigger_sync(force=True)
        except asyncio.CancelledError:
            LOG.debug("Retry task cancelled")
            raise

    if _retry_task and not _retry_task.done():
        _retry_task.cancel()
    _retry_task = asyncio.create_task(_retry_after_delay())


async def _trigger_sync(force: bool = False) -> dict:
    """Internal: kick off a sync if eligible (or forced).

    Used by both the public `/api/sync` endpoint and the auto-retry loop.
    `force=True` bypasses throttle but still respects the already-running
    guard.
    """
    global _sync_running, _sync_started_at, _sync_task
    async with _sync_lock:
        if _sync_running:
            return {"started": False, "reason": "already_running", "state": _sync_state_dict()}
        if not force:
            last = _last_sync_run()
            if last and last.get("completed_at"):
                try:
                    done = datetime.fromisoformat(last["completed_at"])
                    if datetime.now() - done < timedelta(seconds=SYNC_THROTTLE_SECONDS):
                        return {"started": False, "reason": "throttled", "state": _sync_state_dict()}
                except ValueError:
                    pass
        _sync_running = True
        _sync_started_at = datetime.now()
        _sync_task = asyncio.create_task(_run_sync())
    return {"started": True, "state": _sync_state_dict()}


@app.post("/api/sync")
async def api_sync(force: bool = Query(False)):
    """Kick off a background pull from Garmin if not running and not throttled.

    `?force=true` bypasses the throttle (used by the Retry button after a
    failure or for manual user-triggered refresh). Returns immediately;
    poll /api/sync/status to see when it completes.
    """
    return await _trigger_sync(force=force)


@app.get("/api/sync/status")
async def api_sync_status():
    return _sync_state_dict()


# ---------------------------------------------------------------- API: chat --

class ChatRequest(BaseModel):
    session_id: str
    message: str
    model: str = "claude-sonnet-4-6"


async def _get_or_create_session(session_id: str, model: str) -> ClaudeSDKClient:
    async with _session_lock:
        client = _chat_sessions.get(session_id)
        if client is None:
            client = ClaudeSDKClient(options=_options(model))
            await client.__aenter__()
            _chat_sessions[session_id] = client
            LOG.info("Created chat session %s (model=%s)", session_id[:8], model)
        return client


def _ndjson(event: dict) -> bytes:
    return (json.dumps(event) + "\n").encode("utf-8")


@app.post("/api/chat")
async def api_chat(req: ChatRequest) -> StreamingResponse:
    client = await _get_or_create_session(req.session_id, req.model)

    async def stream() -> AsyncIterator[bytes]:
        try:
            await client.query(req.message)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            yield _ndjson({"type": "text", "text": block.text})
                        elif isinstance(block, ToolUseBlock):
                            yield _ndjson({"type": "tool_use", "name": block.name, "input": block.input})
                        elif isinstance(block, ThinkingBlock):
                            yield _ndjson({"type": "thinking", "text": block.thinking})
            yield _ndjson({"type": "done"})
        except Exception as e:
            LOG.exception("Chat stream error")
            yield _ndjson({"type": "error", "message": str(e)})

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.post("/api/chat/{session_id}/end")
async def api_chat_end(session_id: str) -> dict:
    async with _session_lock:
        client = _chat_sessions.pop(session_id, None)
    if client:
        try:
            await client.__aexit__(None, None, None)
        except Exception:
            pass
        return {"closed": True}
    return {"closed": False}


# ---------------------------------------------------------------- API: notes --
# Durable user preferences saved by the chat agent (or manually via the API
# below). Read by both prompts.system_prompt() and the future Settings UI.


class NoteCreate(BaseModel):
    text: str


@app.get("/api/notes")
async def api_notes_list() -> dict:
    """All notes, in on-disk order. ``line`` is the 0-indexed bullet
    position — pass it back to DELETE to remove a specific note."""
    notes = user_notes_mod.read_notes()
    return {
        "notes": [
            {"line": n.line, "timestamp": n.timestamp, "text": n.text} for n in notes
        ]
    }


@app.post("/api/notes")
async def api_notes_create(body: NoteCreate) -> dict:
    """Manually add a note (e.g. from a Settings UI). The agent's
    ``save_user_note`` MCP tool is the in-chat path; this is the
    out-of-band path for direct adds."""
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="text is required")
    n = user_notes_mod.append_note(body.text)
    return {"saved": True, "timestamp": n.timestamp, "text": n.text}


@app.delete("/api/notes/{line_index}")
async def api_notes_delete(line_index: int) -> dict:
    """Delete the note at ``line_index`` (matches the ``line`` field
    returned by GET /api/notes)."""
    ok = user_notes_mod.delete_note(line_index)
    if not ok:
        raise HTTPException(status_code=404, detail="no note at that line")
    return {"deleted": True}


# ---------------------------------------------------------------- Static SPA --

# Mount static frontend if built. /assets is served by StaticFiles, then a
# catch-all returns index.html for client-side routing (React Router).

if WEB_DIST.exists():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")
    _WEB_DIST_RESOLVED = WEB_DIST.resolve()
    _SPA_INDEX = _WEB_DIST_RESOLVED / "index.html"

    @app.get("/")
    async def root() -> FileResponse:
        return FileResponse(_SPA_INDEX)

    @app.get("/{full_path:path}", response_model=None)
    async def spa_fallback(full_path: str, request: Request):
        if full_path.startswith("api/"):
            return JSONResponse({"error": "not found"}, status_code=404)
        # Resolve the requested path and confirm it's still inside WEB_DIST.
        # Without this, `..` segments in the URL escape the doc root and
        # `FileResponse` happily serves anything the process can read —
        # confirmed exploitable in the 2026-05-04 audit (curl --path-as-is).
        candidate = (WEB_DIST / full_path).resolve()
        try:
            candidate.relative_to(_WEB_DIST_RESOLVED)
        except ValueError:
            # Escaped — treat as a SPA route request and return index.html.
            # Don't surface 403 / 404; the SPA's client-side router decides.
            return FileResponse(_SPA_INDEX)
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_SPA_INDEX)
else:
    @app.get("/")
    async def root_no_build() -> dict:
        return {
            "status": "API only",
            "message": "Frontend not built. Run `cd web && pnpm install && pnpm build`, "
                       "or use Vite dev server at http://localhost:5173 for development.",
        }


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def serve(host: str | None = None, port: int = 8765, reload: bool = False) -> None:
    """Start uvicorn. CLI entry point uses this.

    Host defaults to LOCAL_FITNESS_HOST env var if set, else 127.0.0.1.
    The Dockerfile sets it to 0.0.0.0 so the container exposes the port
    to the Docker network; host CLI keeps the loopback-only default.

    Refuses to start on a non-loopback host without ``LOCAL_FITNESS_API_TOKEN``
    set. The /api/* endpoints expose all wellness data, the chat endpoint
    can drain Claude subscription quota, and the user-notes endpoints can
    rewrite agent memory — so unauthenticated LAN exposure is a no.
    """
    import uvicorn
    resolved_host = host or os.environ.get("LOCAL_FITNESS_HOST", "127.0.0.1")

    if resolved_host not in _LOOPBACK_HOSTS and API_TOKEN is None:
        LOG.error(
            "Refusing to bind %s:%d without LOCAL_FITNESS_API_TOKEN — "
            "/api/* would be reachable on the LAN with no authentication. "
            "Set the env var (e.g. python -c 'import secrets; print(secrets.token_urlsafe(32))') "
            "and restart, or bind to 127.0.0.1 for loopback-only.",
            resolved_host, port,
        )
        sys.exit(1)
    if API_TOKEN is None:
        LOG.warning(
            "Server binding to loopback (%s) without LOCAL_FITNESS_API_TOKEN — "
            "/api/* is open. Fine for host-CLI dev; set the token before exposing.",
            resolved_host,
        )
    LOG.info("Serving on http://%s:%d", resolved_host, port)
    uvicorn.run(
        "local_fitness.web.server:app" if reload else app,
        host=resolved_host,
        port=port,
        reload=reload,
        log_level="info",
    )
