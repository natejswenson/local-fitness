"""Security regression tests — path traversal, auth gate, rate limit.

These are the issues found in the 2026-05-04 audit. Each test pins down
the specific failure mode so a future refactor can't quietly reintroduce
the bug.
"""
from __future__ import annotations

import importlib

import httpx
import pytest


@pytest.fixture
def anyio_backend() -> str:
    """anyio's pytest plugin needs this to know which backend to drive."""
    return "asyncio"


@pytest.fixture
def app_with_token(monkeypatch):
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
def app_no_token(monkeypatch):
    """Server without auth (covers the loopback-only / dev path)."""
    monkeypatch.delenv("LOCAL_FITNESS_API_TOKEN", raising=False)
    from local_fitness.web import server as srv
    importlib.reload(srv)
    return srv


@pytest.mark.anyio
async def test_spa_fallback_blocks_path_traversal(app_no_token, monkeypatch):
    """Confirmed exploit from the 2026-05-04 audit: GET /../../pyproject.toml
    used to return 200 with the file. The fix resolves the candidate path
    and rejects any escape from WEB_DIST. We probe via raw ASGI with `..`
    segments preserved (httpx's outer client normalizes; the ASGI scope
    skips that and reaches the route handler with the raw path).
    """
    if not app_no_token.WEB_DIST.exists():
        pytest.skip("web/dist not built — run `cd web && pnpm build` first")

    async def asgi_get(path: str) -> tuple[int, bytes]:
        body_chunks: list[bytes] = []
        status_code = {"v": 0}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            if msg["type"] == "http.response.start":
                status_code["v"] = msg["status"]
            elif msg["type"] == "http.response.body":
                body_chunks.append(msg.get("body", b""))

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
        await app_no_token.app(scope, receive, send)
        return status_code["v"], b"".join(body_chunks)

    # Each of these would have served arbitrary disk content pre-fix.
    for traversal in [
        "/../../pyproject.toml",
        "/../../../README.md",
        "/foo/../../../README.md",
        "/../../data/fitness.db",
        "/../../.env",
    ]:
        status, body = await asgi_get(traversal)
        # The handler now returns the SPA index.html (HTML) on traversal
        # rather than the requested file. Confirm: 200, but body is the
        # SPA shell (small) rather than e.g. pyproject.toml (~700 bytes
        # of TOML) or README.md (~6 KB of markdown starting with `#`).
        assert status == 200, f"{traversal} returned {status}"
        assert b"[project]" not in body, f"{traversal} leaked pyproject.toml"
        assert b"# local-fitness" not in body, f"{traversal} leaked README.md"
        assert body.lstrip().startswith(b"<!"), (
            f"{traversal} returned non-HTML body — possible regression"
        )


@pytest.mark.anyio
async def test_api_requires_bearer_when_token_set(app_with_token):
    transport = httpx.ASGITransport(app=app_with_token.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        # No token → 401 on /api/* (except /health and /api/auth/verify)
        r = await c.get("/api/today")
        assert r.status_code == 401
        # Wrong token → 401
        r = await c.get("/api/today", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401
        # Correct token → not 401 (may be 500 on empty DB but auth passed)
        r = await c.get("/api/status", headers={"Authorization": "Bearer test-token-fixed"})
        assert r.status_code != 401, f"correct token still got {r.status_code}: {r.text}"


@pytest.mark.anyio
async def test_health_is_public(app_with_token):
    transport = httpx.ASGITransport(app=app_with_token.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_auth_verify_path_is_public(app_with_token):
    """The login screen needs to probe with whatever token the user typed,
    so the verify endpoint must reach the auth middleware as a normal
    request (it returns 401 on bad tokens, 200 on good ones)."""
    transport = httpx.ASGITransport(app=app_with_token.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/auth/verify")
        assert r.status_code == 401
        r = await c.get(
            "/api/auth/verify",
            headers={"Authorization": "Bearer test-token-fixed"},
        )
        assert r.status_code == 200
        assert r.json() == {"ok": True, "auth_required": True}


@pytest.mark.anyio
async def test_dashboards_require_auth(app_with_token):
    """All three dashboard endpoints land under /api/, so the bearer
    middleware should gate them automatically. Pinning the contract so
    a future endpoint move can't quietly drop auth."""
    transport = httpx.ASGITransport(app=app_with_token.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        for path in (
            "/api/activity-heatmap",
            "/api/strength-volume",
            "/api/pace-efficiency",
        ):
            r = await c.get(path)
            assert r.status_code == 401, f"{path} returned {r.status_code}"
            r = await c.get(
                path, headers={"Authorization": "Bearer test-token-fixed"}
            )
            assert r.status_code == 200, f"{path} with token returned {r.status_code}: {r.text}"
            body = r.json()
            assert "values" in body, f"{path} response missing `values`: {body}"


@pytest.mark.anyio
async def test_security_headers_present(app_no_token):
    transport = httpx.ASGITransport(app=app_no_token.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/health")
        assert r.headers.get("x-content-type-options") == "nosniff"
        assert r.headers.get("x-frame-options") == "DENY"
        assert r.headers.get("referrer-policy") == "no-referrer"


def test_serve_refuses_non_loopback_without_token(monkeypatch):
    """Startup safety: binding to 0.0.0.0 without a token must hard-fail
    (the whole point of the audit)."""
    monkeypatch.delenv("LOCAL_FITNESS_API_TOKEN", raising=False)
    from local_fitness.web import server as srv
    importlib.reload(srv)
    with pytest.raises(SystemExit):
        srv.serve(host="0.0.0.0", port=18999)
