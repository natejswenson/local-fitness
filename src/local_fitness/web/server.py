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
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import db
from ..agent import briefing as briefing_mod
from ..agent import prompts
from ..agent import tools as agent_tools
from ..ingest import baselines as baselines_mod
from ..ingest import daily as daily_ingest

LOG = logging.getLogger(__name__)

WEB_DIST = Path(__file__).resolve().parents[3] / "web" / "dist"
# Honor the same env override as briefing.py — /briefings inside the
# container, host path otherwise.
BRIEFINGS_DIR = Path(
    os.environ.get(
        "LOCAL_FITNESS_BRIEFINGS_DIR",
        str(Path.home() / "localrepo" / "local-fitness" / "briefings"),
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
    await asyncio.to_thread(briefing_mod.generate_and_save, model=req.model)
    brief = briefing_mod.load_today()
    return {
        "date": date.today().isoformat(),
        "brief": brief.model_dump() if brief else None,
        "cached": False,
        "data_through_date": db.last_known_daily_date(),
    }


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


# ---------------------------------------------------------------- Static SPA --

# Mount static frontend if built. /assets is served by StaticFiles, then a
# catch-all returns index.html for client-side routing (React Router).

if WEB_DIST.exists():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @app.get("/")
    async def root() -> FileResponse:
        return FileResponse(WEB_DIST / "index.html")

    @app.get("/{full_path:path}", response_model=None)
    async def spa_fallback(full_path: str, request: Request):
        if full_path.startswith("api/"):
            return JSONResponse({"error": "not found"}, status_code=404)
        target = WEB_DIST / full_path
        if target.is_file():
            return FileResponse(target)
        return FileResponse(WEB_DIST / "index.html")
else:
    @app.get("/")
    async def root_no_build() -> dict:
        return {
            "status": "API only",
            "message": "Frontend not built. Run `cd web && pnpm install && pnpm build`, "
                       "or use Vite dev server at http://localhost:5173 for development.",
        }


def serve(host: str | None = None, port: int = 8765, reload: bool = False) -> None:
    """Start uvicorn. CLI entry point uses this.

    Host defaults to LOCAL_FITNESS_HOST env var if set, else 127.0.0.1.
    The Dockerfile sets it to 0.0.0.0 so the container exposes the port
    to the Docker network; host CLI keeps the loopback-only default.
    """
    import uvicorn
    resolved_host = host or os.environ.get("LOCAL_FITNESS_HOST", "127.0.0.1")
    LOG.info("Serving on http://%s:%d", resolved_host, port)
    uvicorn.run(
        "local_fitness.web.server:app" if reload else app,
        host=resolved_host,
        port=port,
        reload=reload,
        log_level="info",
    )
