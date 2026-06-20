# Changelog

All notable changes to local-fitness are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.0] - 2026-06-20

### Changed
- **Daily brief is ~2.5–3× faster (~230s → ~82–97s) with equal-or-better
  quality.** Measurement (`scripts/phase0_*`) found the brief's wall-clock is
  dominated by extended thinking (~93% of output tokens), not tools or
  round-trips. The SDK `thinking.budget_tokens` knob is ignored on the Claude
  Code CLI / Max-OAuth path, but reasoning `effort` propagates — so the brief
  composer now runs at `effort="low"` by default. A blind LLM-judge A/B rated
  low-effort briefs as good or better than the prior default on specificity,
  coach-voice, non-repetition, and no-dead-weight. Tunable via
  `LOCAL_FITNESS_BRIEF_EFFORT` (low|medium|high|max).
- A fan-out (map-reduce) architecture was designed and quality-gated, then
  **rejected by Phase 0 measurement** (concurrent `query()` parallelizes only
  1.44× at 3-wide, under the 1.7× kill criterion). Design + outcome retained in
  `docs/plans/2026-06-19-brief-fanout-and-cli-ux-design.md`.

### Added
- **Deterministic table rendering.** Shared `agent/render.py` `render_table`
  (now the single source for the coach snapshot table in `mcp_server`) and a
  `fix_table_row_breaks` repair applied at the brief save gate — eliminates the
  collapsed-markdown-table defect (`|---|---|n| RHR |`) the model emits more
  often at lower effort.
- Brief token-usage instrumentation (`brief_usage` log line) for latency
  attribution.

## [0.5.0] - 2026-06-18

### Changed
- **Agent-first architecture.** The web-server process no longer runs any
  Claude inference. All synthesis — the daily brief, conversational coaching,
  plan drafting/revision, dashboard insights — moves to a client agent (Claude
  Code / Desktop / Mobile) talking to the fitness MCP. The server keeps the
  deterministic compute (baselines, CTL/ATL/TSB, plan grading, status) and
  serves it over REST + MCP. The UI reads the same data as before; what changes
  is *who writes the brief* and *where you converse with the coach*.
- **Single brief write gate.** New Claude-free `agent/briefs.py` owns brief
  I/O; `save_brief()` is the one validate-and-atomic-write path, shared by the
  scheduled composer, the new `save_brief` MCP tool, and `ab_brief.py`.
- **`/api/brief` falls back to the most recent brief** (`load_latest()`) when
  today's hasn't been written, so the Today tab never goes empty while any
  brief exists. The stale-brief banner is now informational.
- **The UI is a viewer.** Today shows the agent-written brief (no Generate
  button, no embedded chat); Training Plan reviews + commits a draft the agent
  writes (drafting moves to the MCP client); Dashboards keep every chart and
  range toggle but drop the per-panel insight chats and the model toggle.

### Added
- **`brief` MCP prompt** + `save_brief` MCP tool, so an MCP client can compose
  and persist a brief through the same integrity gate the scheduled job uses.
- **`GET /api/plan/draft`** — lets the plan viewer show a pending draft without
  a chat surface.
- **`ops/` launchd job** (`install-launchd.sh` / `uninstall-launchd.sh` +
  plist template) that runs the daily `fitness brief` composer at 06:30 with
  next-wake catch-up. Documented `CLAUDE_CODE_OAUTH_TOKEN` (needed only by the
  scheduled composer, not the server) in `.env.example` + `docs/deployment.md`.

### Removed
- The server-side Claude loops: `agent/chat.py`, the `/api/chat*` and
  `/api/brief/generate*` endpoints, the `chat`/`ask` CLI commands, and the
  `ChatPanel` / `DashboardInsight` frontend components.

### Security
- **`run_sql` is now read-only at the SQLite engine, not by keyword matching.**
  The MCP `run_sql` tool opens a `mode=ro` connection (`db.connect_readonly`), so
  any INSERT/UPDATE/DELETE/DDL fails at the engine regardless of phrasing. This
  closes a bypass where a `WITH`-prefixed query with a newline/tab after the
  write keyword (`WITH a AS (SELECT 1)\ndelete\nfrom …`) slipped the prefix and
  space-padded-keyword denylist and committed. The denylist stays as
  defense-in-depth.
- **`run_sql` is time-bounded and non-blocking.** A `set_progress_handler`
  deadline (5s) aborts runaway queries with a clean error, and execution is
  offloaded via `asyncio.to_thread`, so a heavy query can no longer freeze the
  single-threaded server (authenticated DoS).
- **MCP tools validate window/numeric inputs.** Date-window tools
  (`get_metric`, `get_metric_trend`, `query_workouts`, `find_anomalies`,
  `recovery_pattern`, `correlate`, `list_observations`) reject out-of-range /
  non-int `days`/`lookback_days`/`lag_days` via `_validate_days` instead of
  raising `OverflowError`; the plan validator rejects wrong-typed workout fields
  with clean indexed errors instead of `TypeError`/`AttributeError`.
- **`_is_public_path` is case-normalized** so an uppercase `/API/…` can't be
  treated as a public (SPA) path while bypassing the lowercase `/api/` gate.
- `run_sql` no longer echoes raw SQLite exception strings.

### Fixed
- **Stale-brief banner could never clear in the evening.** The server runs in
  UTC, so its daily pull writes a `daily_metrics` row for "tomorrow" once UTC
  rolls over — making `data_through_date` one day ahead of a just-written
  brief's local date, so `isBriefStale` stayed true forever. The banner now
  clamps the data frontier to the *viewer's* local day: a row for a day that
  hasn't finished in your timezone isn't "newer data." Genuinely stale briefs
  still flag.
- Container build: build the SPA on Debian (glibc) instead of Alpine (musl) and
  pin pnpm so Vite 8's rolldown native binding installs; harden uv/pnpm fetch
  against a flaky build network.

### Added
- **"Ask your coach" is now an actionable button.** The brief banner, the
  empty-brief state, and the empty training-plan state each copy a ready-to-paste
  MCP prompt to the clipboard (a web page can't launch a Claude client, so it
  hands you the prompt to paste into Desktop / Code / Mobile).

## [0.4.0] - 2026-06-17

### Added
- **Training plans.** A `/plan` tab where you pick a goal (5K / 10K / Half /
  Full / Custom), a race date, and a target time; the agent drafts a periodized
  plan from your Garmin history, you riff with it in chat, and commit it. The
  committed plan is tracked (goal header with a Riegel predicted finish,
  schedule with per-day adherence, **Target/Actual** distance + pace columns,
  planned-vs-actual weekly mileage, CTL trajectory) and folded into the daily
  brief's workout takeaway (recovery takes precedence over the schedule on
  red-flag days). The Today tab shows a **Today's Goal** card read
  deterministically from `/api/plan`.
- Two tables (`training_plans`, `plan_workouts`) with a partial unique index
  enforcing a single active plan at the DB level.
- Three DRAFT-ONLY agent tools (`propose_training_plan`, `revise_training_plan`,
  `get_training_plan_status`) — the agent only writes drafts; activating or
  deleting a plan is a human action via REST (`GET /api/plan`,
  `POST /api/plan/{id}/commit`, `DELETE /api/plan/{id}`).
- `plans.score_plan` — a deterministic plan-quality gate (safe ≤15%/week ramp +
  taper into the race).
- `scripts/ab_brief.py` — a cross-model A/B simulation harness for prompt
  changes (dry-run by default, hard generation cap, cost-free `--mock` mode).
- A `Content-Security-Policy` header (`script-src 'self'`) as defense-in-depth
  against XSS from AI-authored plan strings.

### Notes
- Integrates the training-plans feature (previously the unmerged
  `design/training-plans` branch) alongside the MCP work from 0.2.0–0.3.1.
  Adherence is computed from the activities join (immune to plan-row edits) and
  graded against the data frontier so Garmin lag never shows a false "missed".
  The reverted brief-pre-fetch experiment from that branch is not included.

## [0.3.1] - 2026-06-17

### Fixed
- **`notes.append_note` return contract** — it hardcoded `line=-1`, so
  `save_user_note` reported the wrong index and a follow-up update/delete using
  it silently no-op'd. Now returns the index `read_notes()` assigns.
- **Manual-workout partial-failure / duplicate-on-retry** — the row committed,
  then `baselines.recompute()` ran unguarded; a recompute failure raised as if
  the write failed, and a retry inserted a second negative-id workout,
  double-counting training load. Recompute failure now returns partial-success
  (`logged`/`deleted: true, recompute_failed: true`). `log_manual_workout` also
  rejects non-positive duration and future dates; `log_observation` validates
  `observed_on` the same way.
- MCP `serverInfo` version + `__version__` now track the package version.

### Changed
- **Coach output renders cleanly in a narrow chat.** The shared `system_prompt`
  now carries an output-formatting contract steering every conversational reply
  (free chat *and* `/fitness:coach`) away from wide markdown tables (which wrap
  into mush in a monospace MCP pane) toward compact per-item lines and
  phase-grouped sections — e.g. a training plan renders as
  `Wk 5 · Jul 13 · Build · long 8mi · threshold 4×6min` lines, not a 6-column
  grid. Scoped to conversational prose only; the structured JSON brief and its
  schema are unchanged (prompt scorer still 11/11).

## [0.3.0] - 2026-06-16

### Added
- **`coach` MCP prompt** — `/fitness:coach` in an MCP client assembles the full
  daily snapshot (per-metric vs-baseline read, training load, recent workouts in
  miles) *and* the coach persona + your saved notes server-side, in one
  round-trip with no tool-call latency or server-side Claude cost. The coaching
  synthesis that previously lived only in the brief loop now travels to the MCP.
- **`daily_snapshot` tool** — one call returns the assembled status (collapses
  the get_today_status + training_load_status + query_workouts + notes chain).
- **`fitness://schema` and `fitness://brief/latest` MCP resources** — the
  queryable-column reference (single-sourced from `QUERYABLE_SCHEMA`, so it can't
  drift from `run_sql`) and the most recent saved brief rendered to markdown.
- **Write surface** — `log_observation` / `list_observations` /
  `delete_observation` (RPE, soreness, weight, mood, feeling, injury, free
  notes; validated against `OBS_TYPES`, soft-referenced to a workout) and
  `log_manual_workout` / `delete_manual_workout` (non-Garmin workouts stored in
  `activities` with a negative synthetic id under `BEGIN IMMEDIATE` and
  `source='manual'`, feeding CTL/ATL/TSB via `baselines.recompute()` with a
  widened lookback so backdated workouts rewrite their own date's row).
- **Server-side miles** — `query_workouts` / `get_workout_detail` / the snapshot
  add `distance_mi`, `pace_min_per_mi`, and formatted duration alongside the raw
  values (`agent/units.py`). `LOCAL_FITNESS_DISPLAY_UNITS` (default `miles`).

### Changed
- Brief generation is now restricted to a read-only tool allow-list
  (`read_only_tool_names()`), so it structurally cannot invoke a write tool; its
  tool set is otherwise unchanged. Chat and the web agent keep the full set.

### Database
- New `observations` table (idempotent DDL; `activity_id` is a soft reference,
  no enforced FK) and a guarded `activities.source` column; `init_schema()`
  stays idempotent across calls.

## [0.2.0] - 2026-06-16

### Added
- **MCP server** — the fitness tools are now reachable from interactive Claude
  sessions (Claude Code / Desktop / other local agents) over the Model Context
  Protocol. Deployed endpoint at `/mcp/` (streamable-HTTP, behind the existing
  `LOCAL_FITNESS_API_TOKEN` bearer gate); local `fitness mcp-stdio` for
  auth-free laptop use. Connect: `claude mcp add --transport http fitness
  https://fitness.home.local/mcp/ --header "Authorization: Bearer $TOKEN"`.
  Implemented by reusing the SDK's already-built tool `Server`
  (`web/mcp_server.py`) over a new transport — one source of truth, no schema
  or handler duplication, so it auto-tracks `agent/tools.py::ALL_TOOLS`.
- **`LOCAL_FITNESS_MCP_ALLOWED_HOSTS`** env var — host allowlist for the MCP
  transport's DNS-rebinding guard (must include the served host or `/mcp/`
  returns 421). Defaults to `fitness.home.local,127.0.0.1,localhost`.

### Security
- `/mcp` and `/mcp/*` are explicitly auth-gated in `_is_public_path` (they live
  outside `/api/`, which defaults to public) — regression-tested in
  `tests/test_security.py`.

## [0.1.0] - 2026-06-06

First documented release. The version was already `0.1.0` in `pyproject.toml`;
this entry inaugurates the changelog and adds the "treat the agent as code"
quality infra. No runtime/app behaviour changed — these are dev-side guardrails,
so the version is documented rather than bumped.

### Added
- `scripts/score_prompt.py` — an eval that scores `agent/prompts.py` against
  grounded pass/fail checks (never-fabricate rule, CTL/ATL/TSB translation,
  roast-when-slipping tone, MCP-tool references, user-notes injection, the
  briefing schema-lock) and exits non-zero on failure so CI can gate on it.
  Its highest-value check cross-validates that every metric/tone the briefing
  prompt advertises is a member of the `Tone`/`MetricName` enums in
  `agent/schemas.py` — catching prompt↔schema drift that would otherwise break
  briefs silently.
- `tests/` — pytest suite covering the deterministic, network-free core
  (`db`, `notes`, `agent/schemas`, `agent/prompts`, `ingest/baselines`, the
  `agent/tools` handlers, and the scorer). `pyproject.toml` enforces a
  whole-repo coverage gate via `--cov-fail-under` (floor 43%; actual ~46%).
  The Garmin-ingest, Claude chat/briefing, and FastAPI-route layers are
  largely excluded from exercise by design (network/SDK).
- Made `tests/test_security.py` hermetic: the auth/route cases were silently
  depending on a developer's real `data/fitness.db` and only failed once CI
  ran them on a fresh clone (`no such table: daily_metrics`). They now run
  against a schema-initialized temp DB.
- `.github/workflows/ci.yml` — runs ruff, the test suite with the coverage
  gate, and the prompt scorer on every push and PR to `master` (uv toolchain).
- `.github/workflows/release.yml` — after CI is green on `master`, cuts a
  GitHub Release + tag for the `pyproject.toml` version if it isn't already
  released (idempotent, notes pulled from this changelog). Bumping the version
  is what ships a release; a normal merge is a no-op.
- `requirements`/dev deps: `pytest-cov` and `coverage` added to the dev group.
- This `CHANGELOG.md`.
