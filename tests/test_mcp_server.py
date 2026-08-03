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
from datetime import date as _dt_date
from pathlib import Path

import pytest
from mcp import types

from local_fitness import db
from local_fitness.agent import tools as agent_tools
from local_fitness.web import mcp_server


@pytest.fixture(autouse=True)
def _isolate_persona_cache():
    """The persona memo + its data_version monitor are module-global; without
    a per-test reset, a hit cached against one test's tmp DB leaks into the
    next (and the monitor would watch a dead file)."""
    mcp_server._persona_cache_clear()
    yield
    mcp_server._persona_cache_clear()


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


def test_render_status_rounds_floats_like_every_tool_payload():
    """The /coach prompt and daily_snapshot must show the SAME numbers.

    `_round_floats` calls itself "the ONE choke point every tool payload flows
    through", but it only ran inside `_text`/`_err` — and this renderer formats
    `assemble_status()` straight to markdown. Live, the prompt printed
    `TSB -0.077230305434135` and `baseline 31.616666666666667` where the tool
    returned `-0.08` and `31.62`."""
    status = {
        "date": "2026-08-03",
        "metrics": [
            {"metric": "avg_stress", "value": 27, "treatment": "baseline_delta",
             "baseline": 31.616666666666667, "delta_pct": -14.6, "arrow": "↓"},
        ],
        "training_load": {
            "ctl": 58.957722002523454, "atl": 59.03495230795759,
            "tsb": -0.077230305434135, "interpretation": "neutral"},
        "recent_workouts": [],
    }
    text = mcp_server._render_status(status)
    assert "58.96" in text and "59.03" in text and "-0.08" in text
    assert "31.62" in text
    # The raw float64 tails must be gone, not merely accompanied.
    for raw in ("58.957722002523454", "-0.077230305434135",
                "31.616666666666667", "59.03495230795759"):
        assert raw not in text, f"raw float leaked into the coach prompt: {raw}"


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


# --- U6: the snapshot must date its training load and flag a frozen frontier

_STALE_WARNING = (
    "⚠ Training load is 5 day(s) stale (newest baselines: 2026-07-21) — "
    "TSB decays daily, so the freshness read above is out of date. "
    "Run sync_garmin_data to refresh."
)


def _status_with_load(training_load: dict, *, metrics: list | None = None) -> dict:
    return {
        "date": "2026-07-26",
        "metrics": metrics if metrics is not None else [
            {"metric": "steps", "value": 9000, "treatment": "raw"},
        ],
        "training_load": training_load,
        "recent_workouts": [],
    }


def test_render_status_dates_training_load_and_warns_when_baselines_are_stale():
    # A 5-day-old baselines row served undated reads as today's freshness.
    text = mcp_server._render_status(_status_with_load({
        "ctl": 40.0, "atl": 45.0, "tsb": -5.0,
        "as_of": "2026-07-21", "baseline_stale_days": 5,
        "interpretation": "slightly fatigued",
    }))
    assert (
        "CTL (fitness): 40.0 · ATL (fatigue): 45.0 · TSB (freshness): -5.0 "
        "(as of 2026-07-21) — slightly fatigued"
    ) in text
    assert _STALE_WARNING in text


def test_render_status_dates_training_load_without_warning_when_current():
    text = mcp_server._render_status(_status_with_load({
        "ctl": 40.0, "atl": 45.0, "tsb": -5.0,
        "as_of": "2026-07-26", "baseline_stale_days": 0,
        "interpretation": "slightly fatigued",
    }))
    assert "(as of 2026-07-26)" in text
    assert "⚠ Training load is" not in text
    assert "sync_garmin_data" not in text


def test_render_status_omits_as_of_and_warning_when_no_baselines_row():
    # The empty-DB payload: as_of/baseline_stale_days are both None, so the
    # line must render exactly as it did before this field existed.
    text = mcp_server._render_status(_status_with_load({
        "ctl": None, "atl": None, "tsb": None,
        "as_of": None, "baseline_stale_days": None,
        "interpretation": "no training-load data yet",
    }))
    assert (
        "CTL (fitness): None · ATL (fatigue): None · TSB (freshness): None "
        "— no training-load data yet"
    ) in text
    assert "as of" not in text
    assert "⚠ Training load is" not in text


def test_render_status_explains_an_all_dashes_metrics_table():
    # assemble_status emits one row per metric even with no daily_metrics row
    # for today, so all-None values IS "Garmin hasn't synced today".
    text = mcp_server._render_status(_status_with_load(
        {"ctl": 40.0, "atl": 45.0, "tsb": -5.0, "as_of": "2026-07-26",
         "baseline_stale_days": 0, "interpretation": "slightly fatigued"},
        metrics=[
            {"metric": "steps", "value": None, "treatment": "trend_arrow",
             "arrow": None},
            {"metric": "rhr", "value": None, "treatment": "baseline_delta",
             "baseline": 53.0, "delta_pct": None, "arrow": None},
        ],
    ))
    assert (
        "No Garmin data for 2026-07-26 yet — run sync_garmin_data to refresh."
    ) in text


def test_render_status_has_no_missing_data_line_when_any_metric_has_a_value():
    text = mcp_server._render_status(_status_with_load(
        {"ctl": 40.0, "atl": 45.0, "tsb": -5.0, "as_of": "2026-07-26",
         "baseline_stale_days": 0, "interpretation": "slightly fatigued"},
        metrics=[
            {"metric": "steps", "value": 9000, "treatment": "raw"},
            {"metric": "rhr", "value": None, "treatment": "baseline_delta",
             "baseline": 53.0, "delta_pct": None, "arrow": None},
        ],
    ))
    assert "No Garmin data for" not in text


def test_render_status_has_no_missing_data_line_when_metrics_list_is_empty():
    # An empty list is a different (degenerate) payload than "rows, all None";
    # the explanatory line would be guessing, so it must not appear.
    text = mcp_server._render_status(_status_with_load(
        {"ctl": None, "atl": None, "tsb": None, "as_of": None,
         "baseline_stale_days": None, "interpretation": "no training-load data yet"},
        metrics=[],
    ))
    assert "No Garmin data for" not in text


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
        params=types.CallToolRequestParams(name="daily_snapshot", arguments={}),
    )
    res = asyncio.run(handler(req))
    result = res.root  # CallToolResult
    assert result.isError is not True
    assert result.content and result.content[0].type == "text"
    payload = json.loads(result.content[0].text)
    # daily_snapshot delegates to
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
    import secrets
    from contextlib import asynccontextmanager

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


# --------------------------------------------------------------------------- #
# 0.36.0 persona memo (S1)
# --------------------------------------------------------------------------- #
def _count_profile_resolves(monkeypatch):
    calls = []
    real = mcp_server.coach.resolve_coach_profile

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(mcp_server.coach, "resolve_coach_profile", counting)
    return calls


def test_persona_resolved_once_across_stateless_handshakes(monkeypatch):
    """The whole point of the memo: N handshakes with nothing changed = ONE
    live resolution (was one full 6-connect resolve per request)."""
    _seed_db()
    server = mcp_server.build_server()
    calls = _count_profile_resolves(monkeypatch)
    first = server.create_initialization_options().instructions
    second = server.create_initialization_options().instructions
    third = server.create_initialization_options().instructions
    assert first == second == third
    assert first is not None and "running coach" in first
    assert len(calls) == 1


def test_persona_reresolves_after_a_journal_write(monkeypatch):
    """Any DB commit bumps data_version on the monitor connection — the memo
    must not serve a persona whose memory block predates a new journal entry."""
    p = _seed_db()
    server = mcp_server.build_server()
    calls = _count_profile_resolves(monkeypatch)
    before = server.create_initialization_options().instructions
    from local_fitness.agent import journal

    journal.save_entry("Nate said the 5k goal moved to October.",
                       source="chat", db_path=p)
    after = server.create_initialization_options().instructions
    assert len(calls) == 2  # second handshake missed and re-resolved
    assert "October" in after and "October" not in before


def test_persona_reresolves_after_a_notes_file_change(monkeypatch, tmp_path):
    """Notes are file-backed — invisible to data_version — so the key carries
    the notes file's (mtime_ns, size)."""
    _seed_db()
    notes_file = tmp_path / "user_notes.md"
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(notes_file))
    server = mcp_server.build_server()
    calls = _count_profile_resolves(monkeypatch)
    server.create_initialization_options()
    notes_file.write_text("- [2026-07-26T10:00:00] go easy on hill weeks\n")
    server.create_initialization_options()
    assert len(calls) == 2


def test_persona_reresolves_on_day_rollover(monkeypatch):
    """The ledger's as-of-yesterday facts move at midnight with zero DB
    writes — the date rides in the key so the memo can't outlive the day."""
    _seed_db()
    server = mcp_server.build_server()
    calls = _count_profile_resolves(monkeypatch)
    server.create_initialization_options()

    class _Tomorrow(_dt_date):
        @classmethod
        def today(cls):
            return _dt_date(2027, 1, 1)

    monkeypatch.setattr(mcp_server, "date", _Tomorrow)
    server.create_initialization_options()
    assert len(calls) == 2


def test_persona_missing_db_never_caches_and_still_hands_shakes(monkeypatch, tmp_path):
    """Fresh-clone path: no DB file → key is None → live resolve every time,
    never a crash, never a cache entry pinned to nothing."""
    db.DEFAULT_DB_PATH = tmp_path / "never_created.db"
    server = mcp_server.build_server()
    opts = server.create_initialization_options()  # must not raise
    assert mcp_server._PERSONA_CACHE["key"] is None
    assert opts is not None


def test_persona_failure_is_not_cached(monkeypatch):
    """A failed resolve must stay fail-open AND retry on the next handshake —
    caching the failure would strip the coach voice until restart."""
    _seed_db()
    server = mcp_server.build_server()
    real = mcp_server.coach.resolve_coach_profile
    state = {"boom": True}

    def flaky(*a, **k):
        if state["boom"]:
            raise RuntimeError("transient db hiccup")
        return real(*a, **k)

    monkeypatch.setattr(mcp_server.coach, "resolve_coach_profile", flaky)
    assert server.create_initialization_options().instructions is None
    state["boom"] = False
    recovered = server.create_initialization_options().instructions
    assert recovered is not None and "running coach" in recovered


# --- Fix 11: matplotlib pre-warm on mcp-stdio start -------------------------

def test_prewarm_matplotlib_swallows_import_failure(monkeypatch, caplog):
    """A broken/missing matplotlib install must never take the process down
    — visuals.py's own lazy `import matplotlib` is the real fallback path,
    so a pre-warm failure here has to be silent-and-logged, not raised."""
    import builtins

    real_import = builtins.__import__

    def boom_import(name, *args, **kwargs):
        if name == "matplotlib":
            raise ImportError("simulated broken install")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", boom_import)
    with caplog.at_level(logging.DEBUG, logger=mcp_server.__name__):
        mcp_server._prewarm_matplotlib()  # must not raise
    assert any("pre-warm failed" in r.message for r in caplog.records)


def test_run_stdio_prewarms_matplotlib_in_a_background_daemon_thread(monkeypatch):
    """run_stdio() must pay matplotlib's 157-210ms import cost off the
    request path — in a background thread, not inline before the handshake
    — and that thread must be a daemon so it can never block process exit."""
    import contextlib
    import threading as threading_mod

    import mcp.server.stdio as stdio_mod

    spawned = []
    real_thread = threading_mod.Thread

    def capturing_thread(*args, **kwargs):
        t = real_thread(*args, **kwargs)
        spawned.append(kwargs)
        return t

    monkeypatch.setattr(mcp_server.threading, "Thread", capturing_thread)

    @contextlib.asynccontextmanager
    async def fake_stdio_server():
        yield (object(), object())

    monkeypatch.setattr(stdio_mod, "stdio_server", fake_stdio_server)

    class _FakeServer:
        def create_initialization_options(self):
            return object()

        async def run(self, read, write, opts):
            return None

    monkeypatch.setattr(mcp_server, "build_server", lambda **kwargs: _FakeServer())

    asyncio.run(mcp_server.run_stdio())

    assert len(spawned) == 1
    assert spawned[0].get("target") is mcp_server._prewarm_matplotlib
    assert spawned[0].get("daemon") is True
