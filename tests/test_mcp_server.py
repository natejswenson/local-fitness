"""Tests for the standalone MCP server (web/mcp_server.py).

Each test pins one of the SDK-level gotchas the design's red-team verified:
F1 (schema serialization), S1 (tool-call content shape), F3 (SPA catch-all
must not shadow /mcp/), the auth gate, and the DNS-rebinding Host allowlist.
"""
from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from pathlib import Path

from mcp import types

from datetime import date as _dt_date

from local_fitness import db
from local_fitness.agent import tools as agent_tools
from local_fitness.web import mcp_server


def _seed_db() -> Path:
    d = Path(tempfile.mkdtemp())
    p = d / "fitness.db"
    db.DEFAULT_DB_PATH = p
    # Keep the brief prompt's _recent_briefs_summary() off the real briefings/.
    from local_fitness.agent import briefs as _briefs

    _briefs.DEFAULT_BRIEFINGS_DIR = d / "briefings"
    db.init_schema(p)
    with db.connect(p) as conn:
        conn.execute(
            "INSERT INTO daily_metrics (date, steps, rhr, sleep_seconds) VALUES (?,?,?,?)",
            ("2026-06-15", 11000, 50, 27000),
        )
    return p


# --- 3c: _render_status renders sleep_seconds via units.format_hm ---------

def test_render_status_sleep_row_uses_format_hm():
    # 3c: the sleep_seconds row must render as "7h 33m" (units.format_hm),
    # not the raw seconds int and not format_duration's "7:33:00" shape.
    status = {
        "date": "2026-07-12",
        "metrics": [
            {
                "metric": "sleep_seconds", "value": 27180, "treatment": "baseline_delta",
                "baseline": 25500.0, "delta_pct": 6.6, "arrow": "↑",
                "value_formatted": "7h 33m", "baseline_formatted": "7h 05m",
            },
        ],
        "training_load": {"ctl": None, "atl": None, "tsb": None,
                           "interpretation": "no training-load data yet"},
        "recent_workouts": [],
    }
    text = mcp_server._render_status(status)
    assert "7h 33m" in text
    assert "7h 05m" in text
    assert "27180" not in text
    assert "25500" not in text


def test_render_status_falls_back_to_raw_value_when_unformatted():
    # A metric with no value_formatted key (every metric but sleep_seconds)
    # renders its raw value exactly as before.
    status = {
        "date": "2026-07-12",
        "metrics": [
            {"metric": "steps", "value": 9000, "treatment": "raw"},
        ],
        "training_load": {"ctl": None, "atl": None, "tsb": None,
                           "interpretation": "no training-load data yet"},
        "recent_workouts": [],
    }
    text = mcp_server._render_status(status)
    assert "9000" in text


# --- prompts: coach + brief both advertised and resolve -------------------

def test_list_prompts_includes_coach_and_brief():
    _seed_db()
    server = mcp_server.build_server()
    handler = server.request_handlers[types.ListPromptsRequest]
    res = asyncio.run(handler(types.ListPromptsRequest(method="prompts/list")))
    names = {p.name for p in res.root.prompts}
    assert {"coach", "brief"} <= names


def test_brief_prompt_resolves_with_instructions_and_save_brief():
    _seed_db()
    server = mcp_server.build_server()
    handler = server.request_handlers[types.GetPromptRequest]
    req = types.GetPromptRequest(
        method="prompts/get",
        params=types.GetPromptRequestParams(name="brief", arguments=None),
    )
    res = asyncio.run(handler(req))  # must not raise on a seeded DB
    msg = res.root.messages[0]
    assert msg.role == "user"
    text = msg.content.text
    # Briefing-instruction markers: the takeaways schema + JSON language.
    assert "takeaways" in text
    assert "JSON" in text
    # References the persistence tool the agent must call.
    assert "save_brief" in text


def test_brief_prompt_is_v2_context_driven():
    """Phase 2: the MCP `brief` prompt composes from the deterministic planner's
    BriefContext (reasoning-in-code) with a persist-via-tool tail — NOT the V1
    `briefing_prompt()` tool-orchestration text."""
    _seed_db()
    server = mcp_server.build_server()
    handler = server.request_handlers[types.GetPromptRequest]
    req = types.GetPromptRequest(
        method="prompts/get",
        params=types.GetPromptRequestParams(name="brief", arguments=None),
    )
    text = asyncio.run(handler(req)).root.messages[0].content.text
    # Embeds the serialized BriefContext (a candidate) + the V2 voice mandates.
    assert '"category"' in text
    assert "How each takeaway should read" in text
    # Persist-via-tool tail, and the system prompt folded into the user text.
    assert "call the `save_brief` tool" in text
    assert "You have NO tools" in text  # brief_v2_system_prompt folded in
    # NOT the V1 monolith's Step-1 tool orchestration.
    assert "get_training_plan_status" not in text
    assert "# Step 1 — gather the data" not in text


def test_coach_prompt_still_resolves():
    _seed_db()
    server = mcp_server.build_server()
    handler = server.request_handlers[types.GetPromptRequest]
    req = types.GetPromptRequest(
        method="prompts/get",
        params=types.GetPromptRequestParams(name="coach", arguments=None),
    )
    res = asyncio.run(handler(req))
    assert res.root.messages[0].role == "user"
    assert res.root.messages[0].content.text


def test_coach_prompt_with_focus_argument():
    _seed_db()
    server = mcp_server.build_server()
    handler = server.request_handlers[types.GetPromptRequest]
    req = types.GetPromptRequest(
        method="prompts/get",
        params=types.GetPromptRequestParams(
            name="coach", arguments={"focus": "recovery"}
        ),
    )
    res = asyncio.run(handler(req))
    text = res.root.messages[0].content.text
    assert "Focus" in text
    assert "recovery" in text


def test_daily_step_goal_fallback_on_bad_setting():
    p = _seed_db()
    db.set_setting("daily_step_goal", "not-a-number", db_path=p)
    assert mcp_server._daily_step_goal() == 10000


def test_unknown_prompt_raises():
    import pytest

    _seed_db()
    server = mcp_server.build_server()
    handler = server.request_handlers[types.GetPromptRequest]
    req = types.GetPromptRequest(
        method="prompts/get",
        params=types.GetPromptRequestParams(name="does_not_exist", arguments=None),
    )
    with pytest.raises(ValueError):
        asyncio.run(handler(req))


# --- resources: schema + latest-brief advertised and readable -------------

def test_list_resources_advertises_schema_and_brief():
    _seed_db()
    server = mcp_server.build_server()
    handler = server.request_handlers[types.ListResourcesRequest]
    res = asyncio.run(handler(types.ListResourcesRequest(method="resources/list")))
    uris = {str(r.uri) for r in res.root.resources}
    assert {mcp_server._SCHEMA_URI, mcp_server._BRIEF_LATEST_URI} <= uris


def test_read_schema_resource_renders_tables():
    _seed_db()
    server = mcp_server.build_server()
    handler = server.request_handlers[types.ReadResourceRequest]
    req = types.ReadResourceRequest(
        method="resources/read",
        params=types.ReadResourceRequestParams(uri=mcp_server._SCHEMA_URI),
    )
    res = asyncio.run(handler(req))
    contents = res.root.contents[0]
    assert "text/markdown" in (contents.mimeType or "")
    assert "Fitness DB schema" in contents.text
    assert "run_sql" in contents.text


def test_read_brief_resource_empty_on_fresh_clone(monkeypatch, tmp_path):
    # mcp_server binds its own DEFAULT_BRIEFINGS_DIR at import; point it at a
    # non-existent dir to exercise the fresh-clone empty render.
    _seed_db()
    monkeypatch.setattr(mcp_server, "DEFAULT_BRIEFINGS_DIR", tmp_path / "nope")
    server = mcp_server.build_server()
    handler = server.request_handlers[types.ReadResourceRequest]
    req = types.ReadResourceRequest(
        method="resources/read",
        params=types.ReadResourceRequestParams(uri=mcp_server._BRIEF_LATEST_URI),
    )
    res = asyncio.run(handler(req))
    assert "No brief generated yet" in res.root.contents[0].text


def test_read_brief_resource_renders_persisted_brief(monkeypatch, tmp_path):
    # Drop a real Brief JSON in the briefings dir → exercises the glob +
    # most-recent pick (_latest_brief_markdown) and the _render_brief path.
    _seed_db()
    bdir = tmp_path / "briefings"
    bdir.mkdir()
    # The NEWEST file is invalid → exercises the `except (OSError, ValueError):
    # continue` skip branch in _latest_brief_markdown; the loop then falls
    # through to the older, valid file.
    (bdir / "2026-06-20.json").write_text("{ not valid json", encoding="utf-8")
    (bdir / "2026-06-01.json").write_text(
        json.dumps({
            "date": "2026-06-01",
            "user_name": "Nate",
            "generated_at": "2026-06-01T06:30:00",
            "takeaways": [{
                "headline": "Rest day earned",
                "summary": "TSB positive, RHR steady.",
                "tone": "positive",
                "details": "Full deep-dive markdown here.",
            }],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(mcp_server, "DEFAULT_BRIEFINGS_DIR", bdir)
    server = mcp_server.build_server()
    handler = server.request_handlers[types.ReadResourceRequest]
    req = types.ReadResourceRequest(
        method="resources/read",
        params=types.ReadResourceRequestParams(uri=mcp_server._BRIEF_LATEST_URI),
    )
    res = asyncio.run(handler(req))
    text = res.root.contents[0].text
    assert "Morning brief — 2026-06-01" in text
    assert "Rest day earned" in text
    assert "Full deep-dive markdown here." in text


def test_read_unknown_resource_raises():
    import pytest

    _seed_db()
    server = mcp_server.build_server()
    handler = server.request_handlers[types.ReadResourceRequest]
    req = types.ReadResourceRequest(
        method="resources/read",
        params=types.ReadResourceRequestParams(uri="fitness://nope"),
    )
    with pytest.raises(ValueError):
        asyncio.run(handler(req))


# --- F1: every tool is exposed and tools/list JSON-serializes -------------

def test_all_tools_exposed_and_list_serializes():
    server = mcp_server.build_server()
    handler = server.request_handlers[types.ListToolsRequest]
    res = asyncio.run(handler(types.ListToolsRequest(method="tools/list")))
    served = {t.name for t in res.root.tools}
    assert served == {t.name for t in agent_tools.ALL_TOOLS}
    # The F1 regression: raw-Python-type shorthand schemas must serialize.
    dumped = res.root.model_dump_json()
    assert '"inputSchema"' in dumped
    gm = next(t for t in res.root.tools if t.name == "get_metric")
    assert gm.inputSchema["properties"]["metric"] == {"type": "string"}
    assert gm.inputSchema["properties"]["days"] == {"type": "integer"}


# --- S1: tools/call returns correct unwrapped text content ----------------

def test_tool_call_returns_unwrapped_content():
    _seed_db()
    server = mcp_server.build_server()
    handler = server.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name="get_today_status", arguments={}),
    )
    res = asyncio.run(handler(req))
    result = res.root  # CallToolResult
    assert result.isError is not True
    assert result.content and result.content[0].type == "text"
    payload = json.loads(result.content[0].text)
    # Fix B (2026-07-10 doc): get_today_status now delegates to
    # status.assemble_status(); "recent_days" doesn't exist on that shape
    # anymore. "training_load" is a key assemble_status() always guarantees
    # (present even on an empty DB — see status.assemble_status's docstring).
    assert "training_load" in payload  # the real handler's shape, not re-wrapped


# --- allowed_hosts env parsing --------------------------------------------

def test_allowed_hosts_default_includes_served_host(monkeypatch):
    monkeypatch.delenv("LOCAL_FITNESS_MCP_ALLOWED_HOSTS", raising=False)
    assert "127.0.0.1" in mcp_server.allowed_hosts()
    assert "localhost" in mcp_server.allowed_hosts()
    monkeypatch.setenv("LOCAL_FITNESS_MCP_ALLOWED_HOSTS", "a.local, b.local")
    assert mcp_server.allowed_hosts() == ["a.local", "b.local"]


# --- Integration: mount + lifespan + auth + Host (F3, auth, 421) ----------

def _make_app(token: str | None, hosts: list[str]):
    from contextlib import asynccontextmanager

    import secrets

    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, PlainTextResponse

    _server, manager = mcp_server.build_session_manager(hosts=hosts)

    @asynccontextmanager
    async def lifespan(app):
        async with manager.run():  # REQUIRED — else /mcp 500s
            yield

    app = FastAPI(lifespan=lifespan)

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        path = request.url.path
        gated = path == "/mcp" or path.startswith("/mcp/")
        if token and gated:
            if not secrets.compare_digest(
                request.headers.get("authorization", ""), f"Bearer {token}"
            ):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    # F3: mount BEFORE the SPA catch-all so it isn't shadowed.
    app.mount("/mcp", app=manager.handle_request)

    @app.get("/{full_path:path}")
    async def spa(full_path: str):
        return PlainTextResponse("SPA-SHELL", media_type="text/html")

    return app


def _init_body() -> str:
    return json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "1"}},
    })


_HDRS = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream",
         "Host": "fitness.home.local"}


def test_mcp_requires_token():
    from starlette.testclient import TestClient
    app = _make_app(token="secret", hosts=["fitness.home.local", "testserver"])
    with TestClient(app) as client:
        r = client.post("/mcp/", content=_init_body(), headers=_HDRS)
        assert r.status_code == 401


def test_mcp_initialize_succeeds_with_token_and_host():
    from starlette.testclient import TestClient
    app = _make_app(token="secret", hosts=["fitness.home.local", "testserver"])
    auth = {**_HDRS, "Authorization": "Bearer secret"}
    with TestClient(app) as client:
        r = client.post("/mcp/", content=_init_body(), headers=auth)
        assert r.status_code == 200, r.text          # not 421 (Host ok), not 500 (lifespan ok)
        assert "application/json" in r.headers.get("content-type", "")  # POST-only JSON (S3)


def test_bad_host_is_rejected():
    from starlette.testclient import TestClient
    app = _make_app(token="secret", hosts=["fitness.home.local"])  # testserver NOT allowed
    auth = {**_HDRS, "Authorization": "Bearer secret", "Host": "evil.example.com"}
    with TestClient(app) as client:
        r = client.post("/mcp/", content=_init_body(), headers=auth)
        assert r.status_code == 421  # DNS-rebinding guard fires on disallowed Host


# --- LOCAL_ONLY_TOOLS: structural HTTP/stdio transport split (INV-T9/T10) --

def _notif_initialized_body() -> str:
    return json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})


def _tools_list_body() -> str:
    return json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})


def test_mcp_http_transport_excludes_local_only_tools():
    # INV-T9 (rewritten per Fix A, 2026-07-10 doc): an authed tools/list call
    # over the real /mcp/ HTTP transport must exclude generate_brief_report
    # (a PDF isn't representable as MCP ImageContent, and a phone-triggered
    # call over this transport would get back a container-internal path with
    # no way to retrieve the file) but now MUST include generate_chart — its
    # inline image content block sidesteps that problem, so it's reachable
    # over the network transport too.
    from starlette.testclient import TestClient
    app = _make_app(token="secret", hosts=["fitness.home.local", "testserver"])
    auth = {**_HDRS, "Authorization": "Bearer secret"}
    with TestClient(app) as client:
        init = client.post("/mcp/", content=_init_body(), headers=auth)
        assert init.status_code == 200, init.text
        client.post("/mcp/", content=_notif_initialized_body(), headers=auth)
        r = client.post("/mcp/", content=_tools_list_body(), headers=auth)
        assert r.status_code == 200, r.text
        payload = json.loads(r.text)
        names = {t["name"] for t in payload["result"]["tools"]}
        assert names == {t.name for t in agent_tools.ALL_TOOLS}
        assert "generate_brief_report" not in names
        assert "generate_chart" in names


def test_build_server_with_local_only_tools_serves_both():
    # INV-T10: the exact call run_stdio() makes — build_server(extra_tools=
    # agent_tools.LOCAL_ONLY_TOOLS) — serves ALL_TOOLS plus whatever's still
    # local-only — the two PDF writers, generate_brief_report and
    # workout_report_card. Both are reachable HERE (stdio) and nowhere else;
    # build_session_manager() calls build_server() argument-free, so the
    # networked /mcp/ transport structurally cannot serve them.
    server = mcp_server.build_server(extra_tools=agent_tools.LOCAL_ONLY_TOOLS)
    handler = server.request_handlers[types.ListToolsRequest]
    res = asyncio.run(handler(types.ListToolsRequest(method="tools/list")))
    served = {t.name for t in res.root.tools}
    assert served == {t.name for t in agent_tools.ALL_TOOLS} | {
        "generate_brief_report", "workout_report_card"}


def test_spa_catchall_does_not_shadow_mcp():
    # F3: the /mcp Mount MUST be registered BEFORE the SPA catch-all
    # GET /{full_path:path}, or the catch-all wins for GET /mcp/ and returns
    # the HTML shell. Tested statically (route order) — a live GET to /mcp/
    # would open a non-terminating SSE stream and hang the client.
    from starlette.routing import Mount, Route
    app = _make_app(token=None, hosts=["fitness.home.local"])
    routes = app.router.routes
    mcp_idx = next(i for i, r in enumerate(routes)
                   if isinstance(r, Mount) and r.path == "/mcp")
    catchall_idx = next(i for i, r in enumerate(routes)
                        if isinstance(r, Route) and "{full_path" in r.path)
    assert mcp_idx < catchall_idx, "MCP mount must precede the SPA catch-all"


# --- coach persona advertised as live MCP instructions --------------------

def test_build_server_import_safe_on_uninitialized_db(monkeypatch, tmp_path):
    # build_server runs at IMPORT (before init_schema); the persona wrap must
    # not read the settings table at build. Point at an uninitialized DB: build
    # + the connect-time resolution must not crash — fail-open to no persona.
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "fresh.db")
    server = mcp_server.build_server()                 # pure build, no DB I/O
    opts = server.create_initialization_options()      # settings table missing
    assert opts.instructions is None                   # fail-open, no OperationalError


def test_instructions_reflect_live_profile():
    p = _seed_db()
    db.set_setting("coach_profile", "hardass", db_path=p)
    server = mcp_server.build_server()
    instr = server.create_initialization_options().instructions
    assert instr and "mcp__fitness__" in instr
    assert all(j in instr for j in ("CTL", "ATL", "TSB"))  # jargon translation retained
    assert any(m in instr.lower() for m in
               ("that's on you", "the log doesn't lie", "accountability mirror"))


def test_instructions_change_between_calls():
    # regression guard: live per-connect, NOT cached at build. Changing the
    # setting between two calls must change the advertised instructions.
    p = _seed_db()
    server = mcp_server.build_server()
    db.set_setting("coach_profile", "hardass", db_path=p)
    hard = server.create_initialization_options().instructions
    db.set_setting("coach_profile", "supportive", db_path=p)
    supp = server.create_initialization_options().instructions
    assert hard != supp
    assert "the log doesn't lie" not in supp.lower()


def test_instructions_fail_open_on_resolve_error(monkeypatch, caplog):
    _seed_db()
    server = mcp_server.build_server()

    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(mcp_server.coach, "resolve_coach_profile", _boom)
    with caplog.at_level(logging.WARNING, logger="local_fitness.web.mcp_server"):
        opts = server.create_initialization_options()  # must not raise
    assert opts.instructions is None
    # Fail-open must be OBSERVABLE — a silent None strip left the maintainer no
    # signal. The warning names the failure so the degradation is diagnosable.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("coach persona resolution failed" in r.message for r in warnings)
    # The exception is attached (exc_info) so the root cause is in the log.
    assert any(r.exc_info for r in warnings)


def test_read_brief_resource_stale_brief_carries_warning_banner(monkeypatch, tmp_path):
    # A brief older than today must lead with a STALE banner — without it, a
    # client on a failed-generation morning silently reads a days-old brief
    # as current (2026-07-19 facet review: three consecutive missed briefs
    # were invisible from the MCP surface).
    _seed_db()
    bdir = tmp_path / "briefings"
    bdir.mkdir()
    (bdir / "2026-06-01.json").write_text(
        json.dumps({
            "date": "2026-06-01",
            "user_name": "Nate",
            "takeaways": [{
                "headline": "Old news",
                "summary": "s",
                "tone": "positive",
                "details": "d",
            }],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(mcp_server, "DEFAULT_BRIEFINGS_DIR", bdir)
    md = mcp_server._latest_brief_markdown()
    stale_days = (_dt_date.today() - _dt_date(2026, 6, 1)).days
    assert f"STALE — this brief is {stale_days} day(s) old" in md
    assert "written for 2026-06-01" in md
    # Banner precedes the first takeaway so it can't be missed.
    assert md.index("STALE") < md.index("Old news")


def test_read_brief_resource_todays_brief_has_no_stale_banner(monkeypatch, tmp_path):
    _seed_db()
    bdir = tmp_path / "briefings"
    bdir.mkdir()
    today = _dt_date.today().isoformat()
    (bdir / f"{today}.json").write_text(
        json.dumps({
            "date": today,
            "user_name": "Nate",
            "takeaways": [{
                "headline": "Fresh",
                "summary": "s",
                "tone": "positive",
                "details": "d",
            }],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(mcp_server, "DEFAULT_BRIEFINGS_DIR", bdir)
    md = mcp_server._latest_brief_markdown()
    assert "STALE" not in md
    assert "Fresh" in md
