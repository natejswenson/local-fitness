"""Expose the fitness tools as a standalone MCP server.

Lets interactive Claude sessions (Claude Code, Claude Desktop, other local
agents) query the fitness DB directly over the Model Context Protocol. The
SDK's in-process ``create_sdk_mcp_server`` (used by the brief/chat agent loop)
cannot be reached by an external client — but it returns a fully-wired
low-level ``mcp`` ``Server`` whose tool schemas and content handling are
already correct. We REUSE that exact server instance over a different
transport (streamable-HTTP for the deployed endpoint, stdio for local use),
so there is one source of truth for the tools and no schema/return-shape
reimplementation.

Design: ``docs/plans/2026-06-16-fitness-mcp-server-design.md``.
"""
from __future__ import annotations

import os
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings

from .. import db
from ..agent import brief_planner, briefs, coach, prompts
from ..agent import tools as agent_tools
from ..agent.render import render_table
from ..agent.briefs import DEFAULT_BRIEFINGS_DIR
from ..agent.schemas import Brief
from ..agent.status import assemble_status

# MCP resource URIs. The schema doc and the latest brief are the two read-only
# resources advertised to clients; the coach prompt is the one slash-command.
_SCHEMA_URI = "fitness://schema"
_BRIEF_LATEST_URI = "fitness://brief/latest"

# Host allowlist for the streamable-HTTP transport's DNS-rebinding guard.
# Empty allowlist + protection-on (the SDK default) returns 421 for every
# request, so the served host MUST be present. The default works for a fresh
# clone on loopback; add your own served host (e.g. an internal hostname behind
# a reverse proxy) via the LOCAL_FITNESS_MCP_ALLOWED_HOSTS env var.
_DEFAULT_ALLOWED_HOSTS = "127.0.0.1,localhost"


def allowed_hosts() -> list[str]:
    raw = os.environ.get("LOCAL_FITNESS_MCP_ALLOWED_HOSTS", _DEFAULT_ALLOWED_HOSTS)
    return [h.strip() for h in raw.split(",") if h.strip()]


def _user_name() -> str:
    """Source user_name exactly like briefing.py does (settings → fallback)."""
    return db.get_setting("user_name", prompts.DEFAULT_USER_NAME)


def _render_status(status: dict[str, Any]) -> str:
    """Readable markdown rendering of ``assemble_status()`` for the coach
    prompt. Snapshot table, training-load read, and recent workouts in miles.
    The active user notes are NOT rendered here — they're already in the
    persona (``system_prompt`` injects them via ``render_for_prompt``)."""
    lines: list[str] = []
    lines.append(f"## Daily snapshot — {status.get('date', '')}")
    lines.append("")

    # Snapshot table of today's metrics — built via the shared render_table so
    # the brief and the coach snapshot use one table renderer (one look).
    metrics = status.get("metrics") or []
    rows: list[list[str]] = []
    for m in metrics:
        name = m.get("metric", "")
        value = m.get("value")
        value_str = "—" if value is None else str(value)
        treatment = m.get("treatment")
        if treatment == "baseline_delta":
            baseline = m.get("baseline")
            delta_pct = m.get("delta_pct")
            arrow = m.get("arrow") or ""
            if baseline is not None and delta_pct is not None:
                read = f"{arrow} {delta_pct:+}% vs baseline {baseline}"
            elif baseline is not None:
                read = f"baseline {baseline}"
            else:
                read = "no baseline yet"
        elif treatment == "trend_arrow":
            arrow = m.get("arrow")
            read = f"7-day trend {arrow}" if arrow else "trend: too few points"
        else:
            read = ""
        rows.append([name, value_str, read])
    lines.append(render_table(["Metric", "Value", "Read"], rows))
    lines.append("")

    # Training-load read.
    tl = status.get("training_load") or {}
    lines.append("## Training load")
    lines.append(
        f"CTL (fitness): {tl.get('ctl')} · ATL (fatigue): {tl.get('atl')} · "
        f"TSB (freshness): {tl.get('tsb')} — {tl.get('interpretation', '')}"
    )
    lines.append("")

    # Recent workouts (miles / formatted convenience fields from status.py).
    workouts = status.get("recent_workouts") or []
    lines.append("## Recent workouts")
    if not workouts:
        lines.append("No workouts logged yet.")
    else:
        for w in workouts:
            parts: list[str] = [str(w.get("date", ""))]
            atype = w.get("activity_name") or w.get("activity_type")
            if atype:
                parts.append(str(atype))
            if w.get("distance_mi") is not None:
                parts.append(f"{w['distance_mi']} mi")
            if w.get("duration_formatted"):
                parts.append(str(w["duration_formatted"]))
            if w.get("pace_min_per_mi"):
                parts.append(f"{w['pace_min_per_mi']} /mi")
            if w.get("avg_hr") is not None:
                parts.append(f"{w['avg_hr']} bpm avg")
            lines.append(f"- {' · '.join(parts)}")
    lines.append("")

    return "\n".join(lines)


def _render_schema_resource() -> str:
    """Markdown rendering of ``tools.QUERYABLE_SCHEMA`` (tables + columns) plus
    the run_sql usage note. Single source of truth — rendered from the constant,
    never hand-copied."""
    lines: list[str] = ["# Fitness DB schema", ""]
    lines.append(
        "Read-only SQLite schema queryable via the `run_sql` tool. "
        "`run_sql` accepts a single read-only `SELECT` or `WITH` query only — "
        "no INSERT/UPDATE/DELETE/DDL. Values must be parameterized."
    )
    lines.append("")
    for table, cols in agent_tools.QUERYABLE_SCHEMA.items():
        lines.append(f"## `{table}`")
        lines.append(", ".join(f"`{c}`" for c in cols))
        lines.append("")
    return "\n".join(lines)


def _render_brief(brief: Brief) -> str:
    """Markdown rendering of a persisted ``Brief`` (latest morning brief)."""
    lines: list[str] = [f"# Morning brief — {brief.date}", ""]
    if brief.generated_at:
        lines.append(f"_Generated {brief.generated_at}_")
        lines.append("")
    for tk in brief.takeaways:
        lines.append(f"## {tk.headline}")
        lines.append(f"*{tk.tone}* — {tk.summary}")
        lines.append("")
        if tk.details:
            lines.append(tk.details)
            lines.append("")
    return "\n".join(lines)


def _latest_brief_markdown() -> str:
    """Glob the briefings dir for ``*.json``, pick the most recent by filename
    date, deserialize the ``Brief`` model and render to markdown. Graceful on a
    missing/empty dir (fresh clone) — never raises. ``load_today`` only loads
    TODAY's file, so we do our own glob + pick-most-recent here."""
    empty = "# Morning brief\n\nNo brief generated yet."
    briefings_dir = DEFAULT_BRIEFINGS_DIR
    if not briefings_dir.exists():
        return empty
    # Filenames are YYYY-MM-DD.json — lexical sort == chronological.
    candidates = sorted(briefings_dir.glob("*.json"), key=lambda p: p.name)
    for path in reversed(candidates):
        try:
            brief = Brief.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # skip unparseable/partial files, try the next most recent
        return _render_brief(brief)
    return empty


def _coach_prompt(arguments: dict[str, str] | None) -> types.GetPromptResult:
    """The running-coach persona pre-filled with today's snapshot."""
    # The persona ALREADY embeds the user's saved notes via
    # render_for_prompt(); do NOT append them again here.
    persona = prompts.system_prompt(_user_name(), coach.resolve_coach_profile())
    snapshot = _render_status(assemble_status())
    text = (
        f"{persona}\n\n"
        f"# Today's data (already retrieved — no tool call needed for this)\n"
        f"{snapshot}"
    )
    if arguments:
        focus = (arguments.get("focus") or "").strip()
        if focus:
            text += (
                f"\n# Focus\n{_user_name()} wants you to focus on: {focus}. "
                f"Lead with that.\n"
            )
    return types.GetPromptResult(
        description="Running-coach persona with today's fitness snapshot.",
        messages=[
            types.PromptMessage(
                role="user",
                content=types.TextContent(type="text", text=text),
            )
        ],
    )


def _daily_step_goal() -> int:
    """Source daily_step_goal exactly like briefing.generate_streaming does
    (settings → parse-with-fallback)."""
    try:
        return int(db.get_setting("daily_step_goal", "10000") or "10000")
    except ValueError:
        return 10000


def _brief_prompt() -> types.GetPromptResult:
    """V2 brief composition for an external MCP agent: the deterministic planner
    pre-assembles today's typed ``BriefContext`` (the same one the in-process
    composer uses), rendered through the V2 prompt with a persist-via-tool tail —
    the agent writes the brief from the context and calls ``save_brief``.

    Reasoning-in-code is ported here (the agent reads a pre-reasoned context
    instead of orchestrating tools). Grounding is NOT — an externally-composed
    brief is ungrounded by construction: this prompt handler returns text and
    never sees the agent's composition (which lands later at the separate,
    Claude-free ``save_brief`` tool, in a different stateless request). No Claude
    loop enters the import graph.

    The MCP prompt message has no system channel, so the V2 system prompt is
    folded into the user text (same pattern as ``_coach_prompt``)."""
    user_name = _user_name()
    profile = coach.resolve_coach_profile()
    recent_briefs_summary = briefs._recent_briefs_summary()
    context = brief_planner.assemble_brief_context()
    system = prompts.brief_v2_system_prompt(user_name, profile)
    user = prompts.brief_v2_user_prompt(
        context, user_name, _daily_step_goal(), recent_briefs_summary, profile,
        persist_via_tool=True,
    )
    text = f"{system}\n\n{user}"
    return types.GetPromptResult(
        description="Compose + save today's brief.",
        messages=[
            types.PromptMessage(
                role="user",
                content=types.TextContent(type="text", text=text),
            )
        ],
    )


def _register_prompts_and_resources(instance: Server) -> None:
    """Register the coach prompt + the schema/brief resources on the low-level
    Server BEFORE it's returned, so BOTH the stdio and streamable-HTTP
    transports advertise these capabilities (the SDK only built tool handlers)."""

    @instance.list_prompts()
    async def _list_prompts() -> list[types.Prompt]:
        return [
            types.Prompt(
                name="coach",
                description=(
                    "Load the running-coach persona pre-filled with today's "
                    "fitness snapshot (metrics, training load, recent workouts, "
                    "and the user's saved coaching preferences). Optional `focus` "
                    "argument steers the coach toward a specific topic."
                ),
                arguments=[
                    types.PromptArgument(
                        name="focus",
                        required=False,
                        description=(
                            "Optional topic to steer the coach toward "
                            "(e.g. 'sleep', 'today's workout', 'recovery')."
                        ),
                    )
                ],
            ),
            types.Prompt(
                name="brief",
                description=(
                    "Compose today's structured JSON brief (the Brief schema). "
                    "Resolves today's data + recent-brief continuity "
                    "server-side; after composing, call the save_brief tool to "
                    "persist it."
                ),
                arguments=[],
            ),
        ]

    @instance.get_prompt()
    async def _get_prompt(
        name: str, arguments: dict[str, str] | None
    ) -> types.GetPromptResult:
        if name == "coach":
            return _coach_prompt(arguments)
        if name == "brief":
            return _brief_prompt()
        raise ValueError(f"unknown prompt: {name!r}")

    @instance.list_resources()
    async def _list_resources() -> list[types.Resource]:
        return [
            types.Resource(
                uri=_SCHEMA_URI,  # type: ignore[arg-type]
                name="Fitness DB schema",
                description=(
                    "Tables and columns queryable via the run_sql tool, plus its "
                    "read-only usage note."
                ),
                mimeType="text/markdown",
            ),
            types.Resource(
                uri=_BRIEF_LATEST_URI,  # type: ignore[arg-type]
                name="Latest morning brief",
                description=(
                    "The most recent persisted morning brief, rendered as markdown."
                ),
                mimeType="text/markdown",
            ),
        ]

    @instance.read_resource()
    async def _read_resource(uri) -> list[ReadResourceContents]:
        uri_str = str(uri)
        if uri_str == _SCHEMA_URI:
            text = _render_schema_resource()
        elif uri_str == _BRIEF_LATEST_URI:
            text = _latest_brief_markdown()
        else:
            raise ValueError(f"unknown resource: {uri_str!r}")
        return [ReadResourceContents(content=text, mime_type="text/markdown")]


def _install_coach_persona(instance: Server) -> None:
    """Advertise the resolved coach persona as the server's MCP ``instructions``,
    resolved LIVE at each client connect — so tool-driven Claude Code fitness chat
    adopts the active ``coach_profile`` (not just the ``/coach`` slash command).

    We wrap ``create_initialization_options`` rather than setting ``instructions``
    eagerly: ``build_server`` runs at import (``web/server.py`` builds the session
    manager at module top-level), BEFORE ``db.init_schema()`` in the FastAPI
    lifespan — so an eager ``resolve_coach_profile()`` (a ``settings``-table read)
    would crash a fresh clone with ``no such table: settings``. Deferring the read
    to connect-time runs it after init and reflects the live profile.

    Fail-open: any resolution error advertises no persona (``instructions=None``)
    rather than breaking the MCP handshake — this is also what keeps the stdio
    path (``mcp-stdio``, which may run before init on a fresh clone) safe.

    RACE-FREE — do not break: ``create_initialization_options`` is synchronous, so
    the set→``_orig()`` snapshot-by-value happens in one frame and concurrent
    stateless-HTTP requests cannot interleave. NEVER introduce an ``await`` between
    setting ``instructions`` and the ``_orig()`` call (e.g. an async DB resolve);
    that would open a real TOCTOU race across concurrent connections.
    """
    _orig = instance.create_initialization_options

    def _with_coach_persona(*args, **kwargs):
        try:
            instance.instructions = prompts.system_prompt(
                _user_name(), coach.resolve_coach_profile()
            )
        except Exception:
            instance.instructions = None  # fail-open: never break the handshake
        return _orig(*args, **kwargs)

    instance.create_initialization_options = _with_coach_persona


def build_server() -> Server:
    """The reused, fully-wired low-level MCP Server (one source of truth).

    The SDK's ``create_sdk_mcp_server`` only wires the TOOL handlers; we register
    the coach PROMPT and the schema/brief RESOURCES on the same instance here so
    both transports (stdio + streamable-HTTP) advertise all three primitives, and
    install the live coach-persona ``instructions`` wrap."""
    instance = agent_tools.make_server()["instance"]
    _register_prompts_and_resources(instance)
    _install_coach_persona(instance)
    return instance


def build_session_manager(
    *,
    stateless_http: bool = True,
    json_response: bool = True,
    hosts: list[str] | None = None,
) -> tuple[Server, StreamableHTTPSessionManager]:
    """Build the reused Server + a streamable-HTTP session manager.

    ``json_response=True`` keeps POST tool-call replies as plain JSON (no
    long-lived SSE stream) so they pass cleanly through the existing
    ``BaseHTTPMiddleware`` auth/rate-limit stack. ``stateless_http=True`` drops
    per-session state (each tool call is self-contained). NOTE: the caller MUST
    run ``session_manager.run()`` in the host app's lifespan, or every request
    raises ``RuntimeError("Task group is not initialized")`` — mounting alone
    does not start it.
    """
    server = build_server()
    manager = StreamableHTTPSessionManager(
        app=server,
        stateless=stateless_http,
        json_response=json_response,
        security_settings=TransportSecuritySettings(
            allowed_hosts=hosts if hosts is not None else allowed_hosts(),
        ),
    )
    return server, manager


async def run_stdio() -> None:
    """Serve the same tools over stdio (local, auth-free). No HTTP, so the
    Host/Origin and trailing-slash gotchas of the HTTP path do not apply."""
    from mcp.server.stdio import stdio_server

    server = build_server()
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
