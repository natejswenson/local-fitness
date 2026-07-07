# MCP clients couldn't sync their own data (`sync_garmin_data` tool)

**2026-07-07 · v0.16.0**

Nate flagged an opencode session (`ses_0c26a33b1ffe6b0N1UklRZYfFX`) that
couldn't refresh Garmin data via its MCP connection. Traced it to a real gap,
not a permissions bug.

## What was actually going on

`fitness mcp-stdio` (the entrypoint every MCP client — opencode, Claude
Desktop — talks to) serves whatever's in `agent/tools.py`'s `ALL_TOOLS`. That
list had 29 tools, all read-only or DB-local writes (notes, observations,
manual workouts, plan edits). Nothing in it ever called out to Garmin
Connect. The only two surfaces that could trigger a live pull were the CLI
(`fitness pull`) and the web UI's `POST /api/sync`. `training_load_status`
even said the quiet part out loud in its own error message: *"no
training-load data yet — pull activities and run recompute-baselines"* — an
instruction the calling agent had no tool to act on.

opencode's `fitness` agent config isn't the culprit — its `tools` block
denies `bash`/`edit`/`write`/etc. but never touches MCP tool calls, so it
would've picked up a sync tool automatically the moment the server exposed
one.

## The fix

Added `sync_garmin_data` to `agent/tools.py`, mirroring `web/server.py`'s
`_run_sync`: run `ingest.daily.pull(max_days=SYNC_MAX_DAYS)` in a thread
(same 30-day bite-sized cap as the UI's auto-sync), and only recompute
baselines when the pull actually lands new days at a clean `"success"`
status — a `"partial"` pull surfaces as an error without triggering a
recompute, same as the UI. `pull()` never raises for auth/config failures
(it catches and returns a status), so the tool just relays whatever it gets
back — `mfa_required`/`credentials_invalid`/`not_configured` all come
through as a normal tool error the agent can read and relay to the user.

Deliberately left off `_READ_ONLY_TOOL_NAMES` (the brief loop's explicit
allow-list) — the brief must stay side-effect-free, and that list is an
allow- not a deny-list, so a new tool is excluded by default unless added.

No wiring needed on the opencode side: `run_stdio()` in `web/mcp_server.py`
serves `ALL_TOOLS` as-is, so the new tool showed up as `fitness_sync_garmin_data`
over the real stdio transport on the first restart — verified with a live
`ClientSession` handshake against `fitness mcp-stdio`, not just a unit test.

## Shape of the change

- `agent/tools.py` — `sync_garmin_data` tool + `SYNC_MAX_DAYS` constant
  (mirrors the web layer's), added to `ALL_TOOLS`.
- `tests/test_tools.py` — success (recomputes baselines), skipped (no
  recompute), partial (error, no recompute), auth-failure (error) cases, plus
  an allow-list regression check and a full-tool-set membership check.
- `tests/test_smoke.py` — tool-count assertion bumped 29 → 30.
- CLAUDE.md — new bullet under *What's already wired* documenting the tool
  and the fact that `ALL_TOOLS` reaches `mcp-stdio` with no separate wiring.

Full suite green (813 passed, 92.94% coverage), ruff clean.
