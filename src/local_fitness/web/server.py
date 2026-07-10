"""FastAPI app hosting the fitness MCP transport + liveness probe.

Bound to 127.0.0.1 only by default — never expose this to the network
without authentication (LOCAL_FITNESS_API_TOKEN; see serve()). The agent's
run_sql tool will execute any SELECT, so a malicious caller could
exfiltrate data through the MCP transport if left unauthenticated.

Endpoints:
  GET  /health   — liveness probe (public)
  ANY  /mcp/*    — the authenticated MCP streamable-HTTP transport (the
                   API surface — see agent/tools.py's mcp__fitness__* tools)

The UI (React/Vite SPA + its ~20 REST routes) was retired 2026-07-09 —
MCP (Claude Code/Desktop, opencode, any streamable-HTTP client) is the only
client surface now. See docs/plans/2026-07-09-mcp-speed-and-ui-retirement-design.md.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import sys
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .. import db

LOG = logging.getLogger(__name__)

# Bearer token gating /mcp/*. When unset AND the server binds to loopback
# only, requests are accepted (host-CLI dev convenience). When binding to a
# non-loopback host (container behind Traefik), `serve()` refuses to start
# without one — see the startup check.
API_TOKEN = os.environ.get("LOCAL_FITNESS_API_TOKEN") or None

# Per-IP rate limits for Claude-cost endpoints. The web-server process holds
# no Claude inference (synthesis happens in the MCP client / scheduled job),
# so the prefix tuple is empty — the rate-limit middleware no-ops. Kept in
# place so re-adding a Claude-cost path is a one-line change: just add its
# prefix here. Bucket is an in-memory deque of recent request timestamps;
# refilled by elapsed time.
RATE_LIMITED_PREFIXES: tuple[str, ...] = ()
RATE_LIMIT_WINDOW_SEC = 60.0
RATE_LIMIT_MAX_REQUESTS = 20  # 20 requests per IP per minute on Claude-cost endpoints
_rate_buckets: dict[str, deque[float]] = defaultdict(deque)
_rate_lock = asyncio.Lock()


# Standalone MCP server: the same fitness tools, reachable from interactive
# Claude sessions (Claude Code/Desktop) and other MCP clients (opencode,
# etc.) over streamable-HTTP at /mcp/. Built once at import; mounted below
# and run in the lifespan. See docs/plans/2026-06-16-fitness-mcp-server-design.md.
from . import mcp_server  # noqa: E402

_MCP_SERVER, _MCP_MANAGER = mcp_server.build_session_manager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_schema()
    # Any `in_progress` row at boot must be from a prior crashed/killed
    # process — mark them so downstream readers don't see a stuck run.
    orphaned = db.mark_orphaned_runs()
    if orphaned:
        LOG.info("Marked %d orphaned ingest_runs row(s) at startup", orphaned)
    # REQUIRED: start the MCP streamable-HTTP session manager's task group, or
    # every /mcp request raises "Task group is not initialized" (mounting alone
    # does not start it).
    async with _MCP_MANAGER.run():
        yield


app = FastAPI(title="local-fitness", lifespan=lifespan)

# Mount the MCP server BEFORE any route is registered — Starlette matches
# routes in registration order. The live path is /mcp/ (trailing slash).
# Auth is enforced by require_api_token (gated via _is_public_path); the
# bearer middleware runs before the router dispatches into this mounted
# sub-app.
app.mount("/mcp", app=_MCP_MANAGER.handle_request)


# ---------- Security middleware -----------------------------------------------
# Order: outermost wraps innermost. Defined later in the file = outer.
# Rate-limit runs OUTSIDE auth so a flood with bad tokens is still capped.

def _is_public_path(path: str) -> bool:
    """Routes anyone can hit: only the liveness probe. Deny-by-default for
    everything else — there's no SPA shell left that needs to load
    unauthenticated, so an explicit whitelist (not a blanket-public
    fallthrough) is the right posture per CLAUDE.md's security defaults."""
    return path == "/health"


@app.middleware("http")
async def require_api_token(request: Request, call_next):
    """Bearer-token gate for everything except the explicit public paths.

    Off when ``LOCAL_FITNESS_API_TOKEN`` is unset (dev convenience on
    loopback). On when set: every non-public request must carry
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
    its own tooling. The bucket is purely in-memory; restart resets state,
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
    # Don't advertise the server stack (uvicorn's header is suppressed in serve()).
    response.headers["Server"] = "fitness"
    # HSTS — the proxy terminates TLS; browsers ignore this over plain HTTP. No
    # includeSubDomains/preload (intranet host, self-signed cert).
    response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self' data:; "
        "connect-src 'self'",
    )
    return response


@app.get("/health")
async def api_health() -> dict:
    """Lightweight liveness probe — used by Traefik's healthcheck.
    Does not touch DB or external services so it stays fast even when
    the agent is busy or Garmin is unreachable."""
    return {"status": "ok"}


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def serve(host: str | None = None, port: int = 8765, reload: bool = False) -> None:
    """Start uvicorn. CLI entry point uses this.

    Host defaults to LOCAL_FITNESS_HOST env var if set, else 127.0.0.1.
    The Dockerfile sets it to 0.0.0.0 so the container exposes the port
    to the Docker network; host CLI keeps the loopback-only default.

    Refuses to start on a non-loopback host without ``LOCAL_FITNESS_API_TOKEN``
    set. The /mcp/ transport exposes all wellness data and can rewrite agent
    memory — so unauthenticated LAN exposure is a no.
    """
    import uvicorn
    resolved_host = host or os.environ.get("LOCAL_FITNESS_HOST", "127.0.0.1")

    if resolved_host not in _LOOPBACK_HOSTS and API_TOKEN is None:
        LOG.error(
            "Refusing to bind %s:%d without LOCAL_FITNESS_API_TOKEN — "
            "/mcp/ would be reachable on the LAN with no authentication. "
            "Set the env var (e.g. python -c 'import secrets; print(secrets.token_urlsafe(32))') "
            "and restart, or bind to 127.0.0.1 for loopback-only.",
            resolved_host, port,
        )
        sys.exit(1)
    if API_TOKEN is None:
        LOG.warning(
            "Server binding to loopback (%s) without LOCAL_FITNESS_API_TOKEN — "
            "/mcp/ is open. Fine for host-CLI dev; set the token before exposing.",
            resolved_host,
        )
    LOG.info("Serving on http://%s:%d", resolved_host, port)
    uvicorn.run(
        "local_fitness.web.server:app" if reload else app,
        host=resolved_host,
        port=port,
        reload=reload,
        log_level="info",
        server_header=False,  # don't advertise "Server: uvicorn"; middleware sets our own
    )
