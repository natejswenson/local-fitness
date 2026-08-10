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

import logging
import os
import sqlite3
import threading
from datetime import date
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings

from .. import config, db, notes
from ..agent import brief_planner, briefs, coach, memory, prompts
from ..agent import tools as agent_tools
from ..agent.briefs import DEFAULT_BRIEFINGS_DIR
from ..agent.render import render_table
from ..agent.schemas import Brief
from ..agent.status import assemble_status

_LOG = logging.getLogger(__name__)

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
    """The single resolver (DB > env > default) — see ``config.user_name``."""
    return config.user_name()


def _render_status(status: dict[str, Any]) -> str:
    """Readable markdown rendering of ``assemble_status()`` for the coach
    prompt. Snapshot table, training-load read, and recent workouts in miles.
    The active user notes are NOT rendered here — they're already in the
    persona (``system_prompt`` injects them via ``render_for_prompt``).

    Rounded through ``agent_tools._round_floats`` first. That helper's own
    docstring calls it "the ONE choke point every tool payload flows through",
    but it only ran inside ``_text``/``_err`` — so this renderer, which formats
    ``assemble_status()`` straight to markdown, bypassed it. The same data
    therefore reached the model two ways: ``daily_snapshot`` returned
    ``tsb: -0.08`` while the ``/coach`` prompt printed
    ``TSB -0.077230305434135``. Raw float64 in a prompt is noise the model has
    to re-round before it can speak, and it invites a spurious-precision
    read-back."""
    status = agent_tools._round_floats(status)
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
        # sleep_seconds carries a pre-formatted "7h 33m" shape (units.format_hm,
        # via status._metric_rows) — use it over the raw seconds int when present.
        value_str = "—" if value is None else str(m.get("value_formatted") or value)
        treatment = m.get("treatment")
        if treatment == "baseline_delta":
            baseline = m.get("baseline_formatted") or m.get("baseline")
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
    # A table of nothing but dashes is what "no daily_metrics row for today
    # yet" looks like from here — indistinguishable, without a line saying so,
    # from a genuinely flat day. assemble_status always emits one row per
    # DAILY_NUMERIC_METRICS, so all-None values IS the missing-row case.
    if metrics and all(m.get("value") is None for m in metrics):
        lines.append("")
        lines.append(
            f"No Garmin data for {status.get('date', '')} yet — run "
            "sync_garmin_data to refresh."
        )
    lines.append("")

    # Training-load read. The as_of date rides along because CTL/ATL/TSB come
    # from the latest baselines row on/before today, which may be days old —
    # and TSB decays daily even with zero workouts, so an undated read of a
    # stale row states the wrong freshness with full confidence.
    tl = status.get("training_load") or {}
    lines.append("## Training load")
    as_of = tl.get("as_of")
    as_of_str = f" (as of {as_of})" if as_of else ""
    lines.append(
        f"CTL (fitness): {tl.get('ctl')} · ATL (fatigue): {tl.get('atl')} · "
        f"TSB (freshness): {tl.get('tsb')}{as_of_str} — "
        f"{tl.get('interpretation', '')}"
    )
    baseline_stale = tl.get("baseline_stale_days")
    if isinstance(baseline_stale, int) and baseline_stale > 0:
        lines.append(
            f"⚠ Training load is {baseline_stale} day(s) stale (newest "
            f"baselines: {as_of}) — TSB decays daily, so the freshness read "
            "above is out of date. Run sync_garmin_data to refresh."
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

    # Brief freshness — surface a failing nightly generation in the coach's
    # own snapshot (the brief resource already banners it; this covers the
    # /coach prompt path). Only rendered when there's something to flag.
    stale_days = status.get("brief_stale_days")
    if stale_days is not None and stale_days > 0:
        lines.append(
            f"⚠ Morning brief is {stale_days} day(s) stale (newest: "
            f"{status.get('latest_brief_date')}) — the nightly generation has "
            "likely been failing. Worth mentioning to the runner."
        )
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
    """Markdown rendering of a persisted ``Brief`` (latest morning brief).

    When the brief is older than today, a stale warning leads the render:
    the resource always serves the most-recent brief on disk, so without the
    banner a client on a failed-generation morning silently reads a days-old
    brief as if it were current (2026-07-19 facet review — three consecutive
    missed briefs were invisible from the MCP surface)."""
    lines: list[str] = [f"# Morning brief — {brief.date}", ""]
    try:
        stale_days = (date.today() - date.fromisoformat(brief.date)).days
    except ValueError:
        stale_days = 0
    if stale_days > 0:
        lines.append(
            f"> ⚠️ STALE — this brief is {stale_days} day(s) old "
            f"(written for {brief.date}; today is {date.today().isoformat()}). "
            "The nightly generation has likely been failing; run "
            "`fitness brief` and check `logs/brief.launchd.err.log`."
        )
        lines.append("")
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
    # render_for_prompt(); do NOT append them again here. Memory is resolved
    # here (fail-silent, "" when disabled/empty) because the prompt builders
    # are pure — see prompts.system_prompt's memory_text note.
    persona = prompts.system_prompt(
        _user_name(), coach.resolve_coach_profile(),
        memory.render_memory_for_prompt(user_name=_user_name()))
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
    system = prompts.brief_v2_system_prompt(
        user_name, profile,
        memory.render_memory_for_prompt(compact=True, user_name=user_name))
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


# --- persona memo (0.36.0) --------------------------------------------------
# In stateless HTTP mode ``create_initialization_options`` runs PER REQUEST,
# and resolving the persona live cost 6 db.connect() opens plus a full
# relationship-ledger compute per request — ~5x the tool call it wrapped. The
# memo key covers every input the persona can change through:
#
#   * DB state (settings, journal, plans, observations, report cards, synced
#     metrics) → ``PRAGMA data_version`` on a dedicated read-only monitor
#     connection. data_version — NOT a MAX(rowid)-style key — because settings
#     UPSERTs and journal archived-flips UPDATE in place and never move a
#     rowid; data_version increments on ANY other connection's commit.
#     The monitor connection must NEVER write: data_version only reports
#     changes made by OTHER connections.
#   * The notes FILE (invisible to data_version) → (st_mtime_ns, st_size).
#   * The ledger's as-of-yesterday facts → today's ISO date in the key.
#
# Known non-invalidators, by convention process-stable: env-var changes
# (LOCAL_FITNESS_COACH_*, _COACH_MEMORY, _NOTES_PATH) need a restart, exactly
# as before this cache existed for everything env-driven.
_PERSONA_CACHE: dict[str, Any] = {"key": None, "instructions": None}
_PERSONA_LOCK = threading.Lock()
_MONITOR_CONN: sqlite3.Connection | None = None
_MONITOR_PATH: str | None = None


def _persona_cache_clear() -> None:
    """Reset memo + monitor connection (tests, and safe to call anytime)."""
    global _MONITOR_CONN, _MONITOR_PATH
    with _PERSONA_LOCK:
        _PERSONA_CACHE["key"] = None
        _PERSONA_CACHE["instructions"] = None
        if _MONITOR_CONN is not None:
            try:
                _MONITOR_CONN.close()
            except Exception:
                pass
        _MONITOR_CONN = None
        _MONITOR_PATH = None


def _data_version() -> int | None:
    """The DB-change component of the memo key; ``None`` → do not cache.

    ``None`` covers the fresh-clone path (no DB file yet — ``build_server``
    runs before ``db.init_schema()``) and any monitor-connection failure; the
    caller then resolves live every time, which is exactly the pre-memo
    behavior. The monitor re-opens when ``db.get_db_path()`` changes — the
    path is resolvable at runtime (env, tests), and a monitor pinned to the
    first-seen file would silently watch the wrong database. Includes the
    path in the returned key material via the reopen, so two DBs can't alias
    each other's data_version counters across a swap."""
    global _MONITOR_CONN, _MONITOR_PATH
    try:
        path = str(db.get_db_path())
        if _MONITOR_CONN is not None and _MONITOR_PATH != path:
            try:
                _MONITOR_CONN.close()
            except Exception:
                pass
            _MONITOR_CONN = None
        if _MONITOR_CONN is None:
            if not db.get_db_path().exists():
                return None
            _MONITOR_CONN = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            _MONITOR_PATH = path
        return _MONITOR_CONN.execute("PRAGMA data_version").fetchone()[0]
    except Exception:
        try:
            if _MONITOR_CONN is not None:
                _MONITOR_CONN.close()
        except Exception:
            pass
        _MONITOR_CONN = None
        _MONITOR_PATH = None
        return None


def _notes_stat() -> tuple:
    try:
        st = notes._default_notes_path().stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return ("missing",)


def _persona_cache_key() -> tuple | None:
    dv = _data_version()
    if dv is None:
        return None
    # The DB path rides in the key directly: data_version is a per-connection
    # counter with no cross-file meaning, so without the path two databases
    # could alias each other's counters across a runtime path swap.
    return (date.today().isoformat(), str(db.get_db_path()), dv, _notes_stat())


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
            # Memoized (see _PERSONA_CACHE above). Key is computed BEFORE the
            # resolve: a write landing between the two caches FRESH data under
            # a pre-write key, so the next request misses and re-resolves —
            # self-correcting in the safe direction, never serving stale.
            key = _persona_cache_key()
            with _PERSONA_LOCK:
                hit = key is not None and _PERSONA_CACHE["key"] == key
                if hit:
                    instance.instructions = _PERSONA_CACHE["instructions"]
            if not hit:
                # ONE connection for the whole resolve (was 6 opens, with
                # _user_name() computed twice).
                with db.connect() as conn:
                    user_name = config.user_name(conn=conn)
                    instructions = prompts.system_prompt(
                        user_name, coach.resolve_coach_profile(conn=conn),
                        memory.render_memory_for_prompt(
                            conn=conn, user_name=user_name),
                    )
                instance.instructions = instructions
                if key is not None:
                    with _PERSONA_LOCK:
                        _PERSONA_CACHE["key"] = key
                        _PERSONA_CACHE["instructions"] = instructions
        except Exception:
            # Fail-open: never break the handshake. But a persistent failure
            # (corrupt user_notes, a settings-table problem) silently strips
            # the ENTIRE rendering contract — lead-with-answer, table shape,
            # miles, the coach voice — from every future session, and without a
            # log there's no signal pointing at why. Sibling fail-open paths
            # (notes.py, branding.py) all log; this one didn't. Synchronous log
            # only — the RACE-FREE note above forbids an await here.
            _LOG.warning(
                "coach persona resolution failed; serving no MCP instructions",
                exc_info=True,
            )
            instance.instructions = None
        return _orig(*args, **kwargs)

    instance.create_initialization_options = _with_coach_persona


def build_server(extra_tools: list | None = None) -> Server:
    """The reused, fully-wired low-level MCP Server (one source of truth).

    The SDK's ``create_sdk_mcp_server`` only wires the TOOL handlers; we register
    the coach PROMPT and the schema/brief RESOURCES on the same instance here so
    both transports (stdio + streamable-HTTP) advertise all three primitives, and
    install the live coach-persona ``instructions`` wrap.

    ``extra_tools`` is forwarded to ``agent_tools.make_server()`` — ONLY
    ``run_stdio()`` passes ``LOCAL_ONLY_TOOLS`` here. ``build_session_manager()``
    below calls this argument-free, which is the load-bearing line that keeps
    ``agent_tools.LOCAL_ONLY_TOOLS`` (see its definition for the current
    membership and the rule that decides it: a tool handing back a filesystem
    path a remote caller can't retrieve is local-only) off the streamable-HTTP
    /mcp/ transport. Today that set is generate_brief_report + workout_report_card
    (both write PDFs); chart's png format (the former generate_chart) is NOT
    in it — an inline image block needs no file retrieval. If a future edit ever passes
    ``LOCAL_ONLY_TOOLS`` there too "for consistency", the HTTP transport
    silently regains tools this whole boundary exists to keep off it."""
    instance = agent_tools.make_server(extra_tools=extra_tools)["instance"]
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


def _prewarm_matplotlib() -> None:
    """Best-effort background warm-up (Fix 11): matplotlib's own import costs
    157-210ms, paid today on whichever of chart-png/PDF-rendering tools
    a stdio session calls first (the lazy `import matplotlib` in
    visuals.py) — a warm process renders in 42-60ms. Importing it here, off
    the request path, absorbs that cost while the client is still doing its
    initialize handshake so the first real tool call already sees it warm.
    Runs in a daemon thread so it can never block process shutdown, and any
    failure (missing/broken install) is swallowed rather than raised — a
    failed pre-warm just means visuals.py's own lazy import pays the cost
    later, exactly like before this fix existed."""
    try:
        import matplotlib

        matplotlib.use("Agg")
    except Exception:
        _LOG.debug(
            "matplotlib pre-warm failed; falling back to on-demand import",
            exc_info=True,
        )


async def run_stdio() -> None:
    """Serve the same tools over stdio (local, auth-free), PLUS
    ``agent_tools.LOCAL_ONLY_TOOLS`` — the PDF-writing tools
    (generate_brief_report + workout_report_card) — reachable here and ONLY
    here, never over the streamable-HTTP /mcp/ transport (see build_server's
    extra_tools note and the design doc). No HTTP, so the Host/Origin and
    trailing-slash gotchas of the HTTP path do not apply."""
    from mcp.server.stdio import stdio_server

    threading.Thread(target=_prewarm_matplotlib, daemon=True).start()

    server = build_server(extra_tools=agent_tools.LOCAL_ONLY_TOOLS)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
