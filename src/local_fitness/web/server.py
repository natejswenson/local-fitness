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
  POST /api/chat                    — streaming agent chat (NDJSON)
  GET  /                            — serve the SPA index.html
  GET  /assets/*                    — serve built frontend assets
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import date, timedelta
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

LOG = logging.getLogger(__name__)

WEB_DIST = Path(__file__).resolve().parents[3] / "web" / "dist"
BRIEFINGS_DIR = Path.home() / "localrepo" / "local-fitness" / "briefings"


# Per-session ClaudeSDKClient so multi-turn chat keeps context.
_chat_sessions: dict[str, ClaudeSDKClient] = {}
_session_lock = asyncio.Lock()


def _options(model: str) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        mcp_servers={agent_tools.SERVER_NAME: agent_tools.make_server()},
        allowed_tools=agent_tools.allowed_tool_names(),
        system_prompt=prompts.SYSTEM_PROMPT,
        model=model,
        permission_mode="bypassPermissions",
        max_turns=50,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_schema()
    yield
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
async def api_brief(regenerate: bool = False) -> dict:
    today = date.today().isoformat()
    path = BRIEFINGS_DIR / f"{today}.md"
    if path.exists() and not regenerate:
        return {"date": today, "markdown": path.read_text(encoding="utf-8"), "cached": True}
    return {"date": today, "markdown": None, "cached": False}


class BriefGenerateRequest(BaseModel):
    model: str = "claude-sonnet-4-6"


@app.post("/api/brief/generate")
async def api_brief_generate(req: BriefGenerateRequest) -> dict:
    path = await asyncio.to_thread(briefing_mod.generate_and_save, model=req.model)
    return {"date": date.today().isoformat(), "markdown": path.read_text(encoding="utf-8"), "cached": False}


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


def serve(host: str = "127.0.0.1", port: int = 8765, reload: bool = False) -> None:
    """Start uvicorn. CLI entry point uses this."""
    import uvicorn
    LOG.info("Serving on http://%s:%d", host, port)
    uvicorn.run(
        "local_fitness.web.server:app" if reload else app,
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )
