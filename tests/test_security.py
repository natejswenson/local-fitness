"""Security regression tests — path traversal, auth gate, rate limit.

These are the issues found in the 2026-05-04 audit. Each test pins down
the specific failure mode so a future refactor can't quietly reintroduce
the bug.
"""
from __future__ import annotations

import importlib
import sqlite3

import httpx
import pytest

from local_fitness import db


@pytest.fixture
def anyio_backend() -> str:
    """anyio's pytest plugin needs this to know which backend to drive."""
    return "asyncio"


@pytest.fixture
def hermetic_db(tmp_path, monkeypatch):
    """Point the server at a schema-initialized temp DB.

    The routes call `db.connect()` (no path) → `db.DEFAULT_DB_PATH`, resolved
    live per request. Without this, the auth/route tests silently depended on
    a developer's real `data/fitness.db` and exploded in CI with
    `no such table: daily_metrics` (the failing path raises straight through
    httpx.ASGITransport rather than becoming a 500).
    """
    db_path = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", db_path)
    db.init_schema(db_path)
    return db_path


@pytest.fixture
def app_with_token(monkeypatch, hermetic_db):
    """Load the server with a fixed API token so the auth middleware is on.

    Reload after env mutation because module import already captured
    `API_TOKEN` from os.environ at import time.
    """
    monkeypatch.setenv("LOCAL_FITNESS_API_TOKEN", "test-token-fixed")
    from local_fitness.web import server as srv
    importlib.reload(srv)
    yield srv
    monkeypatch.delenv("LOCAL_FITNESS_API_TOKEN", raising=False)
    importlib.reload(srv)


@pytest.fixture
def app_no_token(monkeypatch, hermetic_db):
    """Server without auth (covers the loopback-only / dev path)."""
    monkeypatch.delenv("LOCAL_FITNESS_API_TOKEN", raising=False)
    from local_fitness.web import server as srv
    importlib.reload(srv)
    return srv


# test_spa_fallback_blocks_path_traversal (deleted): exercised the SPA
# catch-all's WEB_DIST containment check. Part B removes that entire route
# (no file-serving route left in the app) along with web/, so the concern
# is moot, not merely untested — see docs/plans/2026-07-09-mcp-speed-and-
# ui-retirement-design.md.

# test_api_requires_bearer_when_token_set (deleted): asserted 401/!=401
# against /api/today and /api/status, both removed by Part B. Superseded by
# test_mcp_endpoint_requires_bearer above plus the /mcp/-targeted
# test_auth_rejects_without_bearer / test_auth_rejects_wrong_token /
# test_auth_accepts_valid_token below, which pin the same rejection- and
# acceptance-side behavior against the route that actually survives.


@pytest.mark.anyio
async def test_mcp_endpoint_requires_bearer(app_with_token):
    """The MCP server at /mcp/ lives OUTSIDE /api/ but must be auth-gated —
    _is_public_path defaults non-/api/ paths to public, so a regression that
    drops the explicit /mcp gate would silently expose the whole tool surface.
    The 401 short-circuits in the bearer middleware before the mount, so no
    session-manager lifespan is needed here."""
    transport = httpx.ASGITransport(app=app_with_token.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert r.status_code == 401, f"/mcp/ not gated: {r.status_code}"
        r = await c.post("/mcp/", headers={"Authorization": "Bearer wrong"},
                         json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert r.status_code == 401
        # bare /mcp (no slash) is also gated
        r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert r.status_code == 401


@pytest.mark.anyio
async def test_mcp_write_tool_requires_bearer(app_with_token):
    """A WRITE tool call (log_observation) over /mcp without the bearer token
    must 401 in the middleware BEFORE the MCP mount dispatches it — so an
    unauthenticated client can never mutate the DB. The middleware short-circuits
    before the session manager, so no lifespan is needed."""
    transport = httpx.ASGITransport(app=app_with_token.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "log_observation",
                "arguments": {"obs_type": "weight", "value": 165},
            },
        }
        r = await c.post("/mcp/", json=body)
        assert r.status_code == 401, f"unauthed write tool not gated: {r.status_code}"
        r = await c.post("/mcp/", headers={"Authorization": "Bearer wrong"}, json=body)
        assert r.status_code == 401


@pytest.mark.anyio
async def test_health_is_public(app_with_token):
    transport = httpx.ASGITransport(app=app_with_token.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


# test_auth_verify_path_is_public (deleted): asserted 401/200 + a specific
# JSON body against /api/auth/verify, removed by Part B (no login screen
# left to probe a freshly-pasted token). See the /mcp/-targeted tests below
# for the surviving middleware behavior this test was really pinning.

# test_dashboards_require_auth (deleted): asserted 401 unauthed / 200 +
# "values" authed against /api/activity-heatmap, /api/strength-volume,
# /api/pace-efficiency — all three removed by Part B, no replacement (no
# rebuild of the retired dashboard-specific visualizations planned).


# --- /mcp/ auth-middleware behavior (salvaged + rewritten from the deleted
# tests/test_web_api.py, which pinned the same behavior against /api/* routes
# Part B removes) — /mcp/ is the only generic authed endpoint that survives,
# so these target it directly via a TestClient context manager (drives the
# ASGI lifespan, unlike the bare ASGITransport fixtures above, which are
# sufficient for a 401-only check but not for a call that actually reaches
# the mount) -----------------------------------------------------------------

def _mcp_init_body() -> dict:
    return {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "1"}},
    }


_MCP_HDRS = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}


def test_auth_rejects_without_bearer(app_with_token):
    """No token configured → request proceeds unauthenticated" is the OTHER
    fixture below; this pins the rejection side: a token IS configured and
    no Authorization header is sent → 401."""
    from starlette.testclient import TestClient

    with TestClient(app_with_token.app, base_url="http://localhost") as c:
        r = c.post("/mcp/", json=_mcp_init_body(), headers=_MCP_HDRS)
        assert r.status_code == 401


def test_auth_rejects_wrong_token(app_with_token):
    from starlette.testclient import TestClient

    with TestClient(app_with_token.app, base_url="http://localhost") as c:
        r = c.post("/mcp/", json=_mcp_init_body(),
                   headers={**_MCP_HDRS, "Authorization": "Bearer wrong"})
        assert r.status_code == 401


def test_auth_accepts_valid_token(app_with_token):
    """Valid bearer token → request is accepted (reaches the MCP mount and
    initializes, not just "not 401")."""
    from starlette.testclient import TestClient

    with TestClient(app_with_token.app, base_url="http://localhost") as c:
        r = c.post("/mcp/", json=_mcp_init_body(),
                   headers={**_MCP_HDRS, "Authorization": "Bearer test-token-fixed"})
        assert r.status_code == 200, r.text


def test_auth_verify_open_when_no_token(app_no_token):
    """No token configured → request proceeds unauthenticated (host-CLI dev
    convenience on loopback)."""
    from starlette.testclient import TestClient

    with TestClient(app_no_token.app, base_url="http://localhost") as c:
        r = c.post("/mcp/", json=_mcp_init_body(), headers=_MCP_HDRS)
        assert r.status_code == 200, r.text


@pytest.mark.anyio
async def test_root_public(app_no_token):
    """No token configured, and GET / has no route left (SPA retired) —
    falls straight through to FastAPI's 404, not the auth gate."""
    transport = httpx.ASGITransport(app=app_no_token.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/")
        assert r.status_code == 404


@pytest.mark.anyio
async def test_root_public_even_with_token(app_with_token):
    """Token configured, no Authorization header sent, GET / — deny-by-
    default means `/` is no longer in _is_public_path's whitelist, so the
    bearer check now runs and rejects the missing header BEFORE routing
    ever gets a chance to 404 on the (also-deleted) route."""
    transport = httpx.ASGITransport(app=app_with_token.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/")
        assert r.status_code == 401



@pytest.mark.anyio
async def test_security_headers_present(app_no_token):
    transport = httpx.ASGITransport(app=app_no_token.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/health")
        assert r.headers.get("x-content-type-options") == "nosniff"
        assert r.headers.get("x-frame-options") == "DENY"
        assert r.headers.get("referrer-policy") == "no-referrer"
        # Hardening: no stack disclosure, and HSTS present.
        assert r.headers.get("server") == "fitness"
        assert "max-age=" in r.headers.get("strict-transport-security", "")


@pytest.mark.anyio
async def test_csp_blocks_inline_scripts(app_no_token):
    """AI-authored plan strings render in the SPA; a strict script-src is the
    defense-in-depth against a stored-XSS / token-theft sink."""
    transport = httpx.ASGITransport(app=app_no_token.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/health")
        csp = r.headers.get("content-security-policy", "")
        assert "script-src 'self'" in csp
        assert "'unsafe-inline'" not in csp.split("style-src")[0]  # not on script-src


# test_plan_endpoints_require_auth (deleted): asserted 401/200/404/422
# against /api/plan* routes, all removed by Part B — plan reads/writes now
# happen exclusively through the authenticated MCP tool surface (same trust
# boundary as every other plan-write tool), not a separate REST path.

# test_plan_components_have_no_raw_html_sink (deleted): asserted
# web/src/components/TrainingPlan.tsx exists and has no
# dangerouslySetInnerHTML sink. Part B deletes web/ entirely, so the file
# under test no longer exists — the XSS/stored-injection concern this test
# guarded is moot once there's no JSX rendering AI-authored plan strings.


def test_serve_refuses_non_loopback_without_token(monkeypatch):
    """Startup safety: binding to 0.0.0.0 without a token must hard-fail
    (the whole point of the audit)."""
    monkeypatch.delenv("LOCAL_FITNESS_API_TOKEN", raising=False)
    from local_fitness.web import server as srv
    importlib.reload(srv)
    with pytest.raises(SystemExit):
        srv.serve(host="0.0.0.0", port=18999)


# --- run_sql read-only enforcement (FATAL from the 2026-06 audit) -------------
#
# The old guard scanned for space-padded keywords (`" delete "`) after a
# startswith("with"|"select") gate. A `WITH ... \ndelete\nfrom ...` payload
# slipped both checks and committed on a read-WRITE connection. The fix opens
# run_sql on an engine-level read-only connection so ANY write fails regardless
# of phrasing. These tests pin that down so a refactor can't reintroduce it.


@pytest.fixture
def run_sql_db(tmp_path, monkeypatch):
    """A schema-initialized temp DB with one observation row, wired so that
    db.connect() / db.connect_readonly() (no path) both resolve to it."""
    from local_fitness import db as dbmod

    db_path = tmp_path / "fitness.db"
    monkeypatch.setattr(dbmod, "DEFAULT_DB_PATH", db_path)
    dbmod.init_schema(db_path)
    with dbmod.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO observations "
            "(observed_on, created_at, obs_type, value_num, value_text, activity_id) "
            "VALUES ('2026-01-01', '2026-01-01T00:00:00', 'weight', 165, NULL, NULL)"
        )
    return db_path


def _obs_count(db_path) -> int:
    from local_fitness import db as dbmod

    with dbmod.connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM observations").fetchone()["c"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload",
    [
        # WITH-prefixed write with newline/tab after the keyword — the exact
        # denylist bypass from the audit. Must NOT mutate the DB.
        "WITH a AS (SELECT 1)\ndelete\nfrom observations",
        "WITH a AS (SELECT 1)\tinsert into observations (observed_on, created_at, "
        "obs_type, value_num) values ('2026-02-02','2026-02-02T00:00:00','weight',1)",
        "WITH a AS (SELECT 1)\nupdate observations set value_num = 999",
    ],
)
async def test_run_sql_write_attempts_do_not_mutate(run_sql_db, payload):
    from local_fitness.agent import tools

    before = _obs_count(run_sql_db)
    result = await tools.run_sql.handler({"query": payload})
    # Either the denylist rejects it up front, or the read-only connection does.
    # Both surface as is_error; what matters is the row count is unchanged.
    assert result.get("is_error") is True, f"write payload not rejected: {result}"
    assert _obs_count(run_sql_db) == before, f"DB mutated by: {payload!r}"


@pytest.mark.anyio
async def test_run_sql_readonly_blocks_write_that_slips_denylist(run_sql_db):
    """Defense-in-depth check: the read-only connection itself (db.connect_readonly)
    rejects a write that the keyword denylist never sees, raising rather than
    committing. We exercise the engine gate directly so the test can't pass just
    because the denylist happened to catch the phrasing."""
    from local_fitness import db as dbmod

    before = _obs_count(run_sql_db)
    with pytest.raises(sqlite3.OperationalError) as exc:
        with dbmod.connect_readonly(run_sql_db) as conn:
            conn.execute("DELETE FROM observations")
    assert "readonly" in str(exc.value).lower()
    assert _obs_count(run_sql_db) == before


@pytest.mark.anyio
async def test_run_sql_bounds_long_query_by_deadline(run_sql_db, monkeypatch):
    """A heavy recursive CTE must return the time-budget error rather than hang
    the event loop. We force a tiny deadline so the test needs no real sleep:
    the progress handler trips on the first check and SQLite raises
    OperationalError('interrupted'), mapped to a clean budget message."""
    from local_fitness.agent import tools

    # Negative budget => the deadline is already in the past => the progress
    # handler aborts on its first invocation.
    monkeypatch.setattr(tools, "_RUN_SQL_TIME_BUDGET_S", -1.0)
    monkeypatch.setattr(tools, "_RUN_SQL_PROGRESS_OPS", 1)
    heavy = (
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
        "SELECT x FROM c"
    )
    result = await tools.run_sql.handler({"query": heavy})
    assert result.get("is_error") is True
    assert "time budget" in result["content"][0]["text"]


def test_is_public_path_uppercase_api_not_public():
    """Case-sensitivity is moot post-deny-by-default (every path denies
    unless it's the literal "/health" whitelist entry), but this keeps the
    historical regression pinned: an uppercase /API/* path was never
    treated as the public /api/ prefix, and still isn't."""
    from local_fitness.web import server as srv

    assert srv._is_public_path("/API/TODAY") is False
    assert srv._is_public_path("/Api/Plan") is False
    assert srv._is_public_path("/MCP/") is False
    assert srv._is_public_path("/health") is True


def test_is_public_path_deny_by_default():
    """No SPA shell to serve unauthenticated means an arbitrary unknown path
    denies by default now, where it used to fall through to public — see
    CLAUDE.md's "whitelist explicitly" security convention."""
    from local_fitness.web import server as srv

    assert srv._is_public_path("/foo") is False
    assert srv._is_public_path("/") is False
    assert srv._is_public_path("/assets/index.js") is False
