---
ticket: "N/A (interactive design)"
title: "Coach profile carries into tool-driven Claude Code fitness chat (live per-connect MCP server instructions)"
date: "2026-06-23"
source: "design"
---

# Coach profile in tool-driven Claude Code chat

The 0.10.0 coach tone profiles already reach the two fitness MCP **prompts**
(`/mcp__fitness__coach`, `/mcp__fitness__brief` — verified: `_coach_prompt`
renders the live `hardass` voice). They do **not** reach the **tool-driven**
path: when Claude Code answers a fitness question by calling the MCP tools
(`get_today_status`, `daily_snapshot`, …) rather than the slash command, no
coach persona is in CC's context, so the reply uses CC's default voice.

This closes that gap by advertising the coach persona as the MCP server's
top-level `instructions`, resolved **live at each client connect**, so every
fitness interaction (tools included) adopts the profile's tone.

## Load-bearing assumption (client behavior)

The feature rests on one assumption about an **external** client we cannot
verify from this repo: **Claude Code surfaces an MCP server's top-level
`instructions` as persistent context** for the connection, so it shapes
tool-driven answers (not just the initialize handshake metadata).

- **Verification step (required before claiming done):** in a real Claude Code
  session, set `coach_profile=hardass`, then ask CC a data question that goes
  through the `mcp__fitness__*` tools (not the `/coach` slash command) and
  confirm the answer adopts the hardass tone; flip to `supportive` and confirm
  the tone changes.
- **Fallback / blast radius:** the explicit `/mcp__fitness__coach` slash command
  remains the guaranteed path — it injects the persona directly regardless of
  how a client treats `instructions`. If CC (or another client) ignores
  `instructions`, this feature is a **no-op but harmless** (no error, no
  behavior change, the slash command still works).

## The change

MCP `instructions` are read by the low-level `Server.create_initialization_options()`
(it reads `self.instructions`), and **both transports call that method at
client-connect time** — verified:

- stdio: `run_stdio` calls `server.run(read, write, server.create_initialization_options())`
  (`web/mcp_server.py:381`).
- streamable-HTTP: `StreamableHTTPSessionManager` calls
  `self.app.create_initialization_options()` per request/connect — stateless at
  `.venv/.../mcp/server/streamable_http_manager.py:196`, stateful at `:276`.

So we resolve the persona **lazily, per connect**, by wrapping the instance's
`create_initialization_options` in `build_server()` (after
`_register_prompts_and_resources`). Each call first refreshes `instructions`
from the **live** profile, then delegates to the original:

```python
_orig = instance.create_initialization_options
def _with_coach_persona(*a, **k):
    try:
        instance.instructions = prompts.system_prompt(
            _user_name(), coach.resolve_coach_profile()
        )
    except Exception:
        instance.instructions = None  # fail-open: never break the handshake
    return _orig(*a, **k)
instance.create_initialization_options = _with_coach_persona
```

Key properties:

- **Import-safe / fresh-clone-safe.** Installing the wrap is pure — it does
  **no DB I/O** at build/import time. `web/server.py` builds the server at module
  top-level (`_MCP_SERVER, _MCP_MANAGER = mcp_server.build_session_manager()`,
  `server.py:104`), *before* `db.init_schema()` runs in the FastAPI `lifespan`
  (`server.py:109`). A static `instance.instructions = system_prompt(...)` at
  build time would read the `settings` table before the schema exists →
  `OperationalError: no such table: settings` on a stranger's clone. The wrap
  defers the read to connect-time, which for the **HTTP** path is **after**
  `init_schema` (the lifespan runs before any request reaches
  `create_initialization_options`).
  - **stdio caveat (corrected):** `fitness mcp-stdio` (`cli.py:60` → `run_stdio`)
    does **not** call `db.init_schema()`. So on a *fresh* clone the first stdio
    connect's resolution hits the missing `settings` table — and stdio
    import-safety therefore rests on the **fail-open** branch below (it advertises
    `instructions=None`, handshake still succeeds), NOT on init ordering.
    Implementation should add `db.init_schema()` to the `mcp-stdio` command so
    stdio actually gets the persona on a fresh clone (parity with HTTP); without
    it, stdio degrades gracefully to no-persona until the DB is initialized.
- **Fail-open.** If `resolve_coach_profile()` (or `system_prompt`) raises — e.g. a
  DB error (incl. the fresh-clone stdio case above) — the `except Exception` sets
  `instructions=None` and the original `create_initialization_options()` still
  returns; the MCP handshake succeeds with no persona rather than crashing. The
  `except` must catch broadly so nothing escapes `create_initialization_options`.
- **Live per connect.** Because resolution runs on **every** connect, the
  advertised persona always reflects the current `coach_profile`. A
  `fitness config set coach_profile …` takes effect on the next CC (re)connect —
  **no server restart**. One seam (`create_initialization_options`) covers both
  transports.

**One source of truth:** the value is the same `prompts.system_prompt(...)` the
brief and the `/coach` prompt already use — automatically profile-aware (the
profile's voice + dials), carrying the jargon translation, the chat-reply
formatting rules, and the user's saved notes. No new persona text.

## Live-per-connect resolution (no staleness tradeoff)

Resolution is **live at each client connect**, so a profile change reflects on
the next connect — no restart, and consistent with the slash-command prompts
(which also re-resolve live every call). There is no tool-driven-vs-prompt
"split-brain" within a session: both paths resolve the same live profile.

**Residual (honest, inherent):** a profile change does **not** affect an
already-open Claude Code session until it reconnects / re-initializes. This is
inherent to MCP `instructions`, which are advertised at the `initialize`
handshake and not renegotiated mid-session — not something this design can
engineer around. New connects (the common case after a config change) pick up
the new profile immediately. (In the deployed `stateless_http=True` mode there
is no persistent session — every request re-resolves — so this residual only
bites the stdio path.)

**Why the per-request shared-state mutation is race-free (load-bearing for
maintainers).** The wrap mutates `instance.instructions` — a single attribute on
the process-wide shared `Server` — on every connect, and stateless-HTTP requests
run concurrently. This is safe ONLY because `Server.create_initialization_options`
(and the `get_capabilities` it calls) are **synchronous, await-free**: the
wrapper's `set self.instructions` → `_orig()` → read-and-snapshot-by-value into
the per-request `InitializationOptions` happens entirely within one synchronous
frame, so the event loop cannot interleave another request's mutation between the
set and the read. **Invariant to preserve:** never introduce an `await` between
setting `instructions` and the `_orig()` snapshot (e.g. an async DB resolve) —
that would open a real TOCTOU race where one request advertises another's profile.

**Per-request cost (honest).** Under `stateless_http=True` the wrap re-runs
`resolve_coach_profile()` (a `db.all_settings` read + the profile `.md` read) and
`system_prompt()` (a `data/user_notes.md` read) on **every tool call**, not once
per session — a few SQLite opens + 2 file reads, plus the ~6–8 KB persona, per
request. Negligible for a single loopback user; stated so it isn't mistaken for
per-session cost.

## Content note (system_prompt reuse, accepted)

`system_prompt` is **reused unchanged** — genuinely no prompt-function edit, so
`score_prompt.py` / the brief A/B are unaffected (keep it that way).

Advertising its **full body** as server `instructions` carries a mild content
mismatch, acknowledged and accepted as the cost of one-source-of-truth (do NOT
trim — YAGNI):

- It contains a section headed *"Formatting your chat replies (NOT the JSON
  brief)"* and a *"Managing preferences conversationally"* block. These are
  **adequate / relevant** here: CC has the `mcp__fitness__*` read/write tools, so
  the formatting guidance and notes-management guidance apply to tool-driven
  chat.
- The *"NOT the JSON brief"* framing is mildly off-context for a non-brief
  client, and the body is ~6–8 KB (incl. injected user notes), sent on every
  initialize. Accepted tradeoff, not engineered around.

## API surface

- `web/mcp_server.build_server() -> Server` — unchanged signature; now wraps
  `instance.create_initialization_options` before returning (pure install, no DB
  I/O).
- No new tool, prompt, resource, endpoint, or argument. The MCP initialize
  payload gains the `instructions` string, resolved live per connect.

## Invariants

Checkable by inspection:

- `build_server()` / `build_session_manager()` do **no DB I/O at import/build
  time** — importing/building must not touch the `settings` table (fresh-clone
  import safety).
- The `create_initialization_options` wrap is **fail-open**: on any resolution
  error, `instructions` becomes `None` and the call still returns.
- The resolved `instructions` reflect the **live** `coach_profile` at connect
  time — value is exactly `system_prompt(_user_name(), resolve_coach_profile())`,
  no separate persona text to drift from the brief/slash-command voice.
- No prompt-function edit (`system_prompt`/`briefing_prompt` bodies unchanged) →
  `score_prompt.py` and the brief A/B are unaffected.

## Testing strategy

New assertions in `tests/test_mcp_server.py` (or `test_coach.py`); existing
MCP-server tests stay green. `uv run pytest -x`.

- **Fresh-clone / import-safety** — building the server (or importing the module)
  against an **uninitialized** DB does **not** touch the `settings` table and does
  **not** crash (no `OperationalError`). The DB read only happens when
  `create_initialization_options()` is called.
- **Live resolution (needs an initialized test DB)** — with `coach_profile=hardass`
  set in the DB, `wrapped.create_initialization_options().instructions` contains
  hardass voice markers (+ the CTL/ATL/TSB jargon translation + a `mcp__fitness__`
  tool reference); with `supportive` it does **not** contain the hardass markers.
- **Change-between-calls (regression guard, hard assertion)** — call
  `create_initialization_options()`, change `coach_profile` in the DB, call
  again, and assert the **two results differ** (e.g. hardass markers present then
  absent). This is a distinct assertion from the single-call marker check: it is
  what fails if a future maintainer "optimizes" by caching `instructions` at build
  time, defeating live-per-connect — the single-call test would still pass.
- **Fail-open** — if `resolve_coach_profile` raises (monkeypatched), `instructions`
  resolves to `None` and `create_initialization_options()` still returns normally.

No prompt A/B gate (no prompt edit — `score_prompt.py` unchanged). Rebuild the
container so the deployed `/mcp/` endpoint advertises the persona; then run the
**verification step** above in a real CC session to confirm the load-bearing
assumption holds (and optionally inspect the initialize handshake).

## Obligations

- Version bump (`0.10.0 → 0.11.0`) + CHANGELOG + devlog (functionality change to
  the MCP surface).
- No `.env.example` change (no new env var). No auth/SQL surface → `test_security.py`
  untouched.

## Quality-gate provenance

Reviewed via `/quality-gate` (artifact type: design) on `general-purpose` agents
(the `crucible-*` agent types are not installed here; Opus recall guarantee not
enforced; findings still code-grounded and empirically reproduced). Two red-team
rounds + a tightened look-harder pass. Terminal verdict **PASS (clean-pass)**:
0 Fatal / 0 Significant on a fresh round, confirmed by look-harder. Score 4 → 0.

Round 1 caught a **Fatal I would have shipped**: the original design set
`instance.instructions` at `build_server()` time, but `build_server` runs at
*import* (`server.py:104`), **before** `db.init_schema()` (FastAPI lifespan) — so
the `settings`-table read would crash a fresh clone with `no such table:
settings` (reproduced), violating the repo's clone-must-run invariant. The fix
reframed the mechanism to **lazy per-connect resolution** (wrap
`create_initialization_options` so the persona resolves at connect-time, after
init, fail-open) — which also dissolved the original Significant (staleness
split-brain), since per-connect resolution is live and consistent with the
slash-command prompts. Look-harder verified idempotency (fresh instance per
build → no double-wrap) and that the per-request shared-state mutation is
race-free (set→snapshot is synchronous/await-free), and corrected the stdio
import-safety rationale (rests on fail-open, since `mcp-stdio` doesn't init the
schema).
