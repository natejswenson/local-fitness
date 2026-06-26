# Changelog

All notable changes to local-fitness are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.15.0] - 2026-06-26

### Added
- **Public-share readiness hardening** (from a comprehensive readiness audit):
  CI now **compiles the Docker image** on every push/PR (`docker-build` job) so
  the container deploy can't silently break, runs on the same **Node 26 +
  corepack** as the image, and gained least-privilege `permissions`, a
  `concurrency` cancel, a pnpm cache, and Codecov upload + badge. Added
  **CodeQL** (python + js/ts) and **dependency-review** workflows; enabled
  secret-scanning / push-protection / Dependabot security updates. Fixed a
  shipped prompt that hardcoded the owner's name (now `{user_name}`), made the
  `ab_brief --run` flakiness honest in its docstring, anchored the prompt
  scorer's tone check to the enumerated schema block, and corrected doc drift
  (tool count 25→27, stale "43% gate", `.env.example` allow-list comment) +
  a `docs/` index. Added the repo's **first frontend tests** (Vitest + 5
  auth-path cases, run in CI) and a regression net over `briefing.py`'s
  streaming loop (30→82%), lifting total coverage to ~93%.
- **`chart` `line` style — a clean box-drawing line chart.** Drawn with 1-cell
  box-drawing glyphs (`─ ╭ ╮ ╰ ╯ │`) that connect into a smooth curve, with a
  y-axis + baseline. Two things keep it clean rather than stair-stepped: the
  series is **heavily smoothed** (centered moving average scaled to the window)
  and **down-sampled to a lower column count**, so each change is a gentle slope
  instead of a one-column riser. Box-drawing renders reliably everywhere (a
  braille prototype was smoother in principle but font-dependent, so it was
  dropped). Monochrome by design — a colored line needs chunky double-width
  emoji; `calendar` is the style for color. (`agent/charts.py` `render_line`.)
  (Earlier emoji / braille / under-smoothed line prototypes from this same
  `[Unreleased]` cycle were replaced.)

### Fixed
- **Calendar chart alignment.** The heat-grid mixed cell widths — ASCII `· `
  pads and an `M T W T F S S` header are narrower than the double-width emoji
  squares, so columns didn't line up. Every grid cell is now a single emoji
  (`⬛` for out-of-window days instead of dots), and the un-alignable ASCII
  weekday header is dropped in favor of a `rows = weeks (Mon→Sun)` note in the
  legend. Rows are now uniform 7-cell weeks that align cleanly. Regression test
  asserts every grid row is exactly 7 emoji cells with no ASCII pad.

## [0.14.0] - 2026-06-26

### Added
- **`chart` calendar style — the new default, fixes the truncation/compression
  bug.** Multi-week colored charts were rendered one row per day, so a 60-day
  window was 60 lines that the terminal truncated to a cramped ~14-line slice.
  The new `calendar` style (`agent/charts.py` `render_calendar`) lays the data
  out as a week-stacked emoji heat-grid — one colored square per day, weeks
  stacked top→bottom, Mon→Sun left→right — so any window stays compact (90 days
  ≈ 13 rows) and fully visible. Missing in-window days render as ⬜; the
  right-hand weekly column is a sum for additive metrics (steps, intensity
  minutes) and the mean of present days otherwise. `calendar` is now the tool
  default; `bar` (one row per day) remains for short ≤2-week windows, alongside
  `combo` and `spark`.

## [0.13.0] - 2026-06-25

### Added
- **`chart` MCP tool — terminal graphs from your data.** A new read-only tool
  (`mcp__fitness__chart`) renders any daily metric, the training-load series
  (`ctl`/`atl`/`tsb` = fitness/fatigue/freshness), or the derived
  `intensity_minutes_weighted` (Garmin "active minutes" = moderate + 2×vigorous)
  as a terminal chart. Three styles: `bar` (emoji-color horizontal, default),
  `combo` (2D vertical bars with a least-squares trend line overlaid — handles
  negative series like TSB), and `spark` (one-line sparkline). A prototype
  against the live terminal established that ANSI color is stripped on the way
  to the display, so color is carried by emoji glyphs, not escape codes. The
  renderers live in `agent/charts.py` as pure functions. The tool is available
  to the chat/coach loop but deliberately excluded from the brief's tool set
  (the brief renders its own UI cards), mirroring the `daily_snapshot`
  precedent.

### Changed
- **Test coverage raised 65% → ~90%** with mock-free tests (real tmp SQLite +
  hand-rolled fake Garmin clients + `ASGITransport`/`CliRunner`) for the
  previously-thin I/O edges: `ingest/backfill.py` (8→100%), `ingest/daily.py`
  (10→92%), `web/server.py` (55→92%), `web/mcp_server.py` (71→95%), `cli.py`
  (39→81%), `ingest/auth.py` (25→86%), plus `agent/briefs.py`/`units.py`/
  `status.py` to 100% and `briefing.py`'s pure partial-JSON parser. The SDK
  message-stream and uvicorn/transport glue are left untested on purpose
  (YAGNI — those tests would only assert mocks replay themselves). CI
  `--cov-fail-under` raised 43 → 85 to lock in the gain.

### Removed
- **YAGNI cleanup for the public repo** (~400 LOC): the three one-off
  `scripts/phase0_*.py` probes; dead code in `server.py` (`BRIEFINGS_DIR`,
  a duplicate of `briefs._default_briefings_dir`), `tools.py`, `coach.py`,
  `ingest/auth.py` (`clear_credentials`); an orphaned `StatCard.tsx` + unused
  `deltaText` in the web app; and four unused single-knob `config.py` accessors.

### Fixed
- **Repo tidied for public consumption**: dropped the owner's LAN host
  `fitness.home.local` from the shipped `_DEFAULT_ALLOWED_HOSTS` default
  (now `127.0.0.1,localhost`; add your own via `LOCAL_FITNESS_MCP_ALLOWED_HOSTS`);
  clone-agnostic `./data` volume examples in `docs/deployment.md`; a header
  marking root `CLAUDE.md` as maintainer-internal.

## [0.12.1] - 2026-06-25

### Fixed
- **Frontend rendered a black screen — `react` and `react-dom` versions had
  drifted apart.** Dependabot's react bump (#15) moved `react` to 19.2.6 but
  left `react-dom` at 19.2.5. React 19 requires the two packages to be the
  exact same version and throws *Minified React error #527* on mount when they
  differ, so the SPA mounted nothing and left the dark `bg-bg` background
  showing. `tsc -b` + `vite build` never execute the app, so CI stayed green
  and the broken bundle shipped to the container. Pinned both packages to
  19.2.7 so they move in lockstep. Verified the live container renders via a
  headless-Chrome DOM probe (root no longer empty; only the expected auth-gate
  401 remains).

## [0.12.0] - 2026-06-24

### Changed
- **Adopted a `feature/* → dev → main` branch model** (mirrors the
  `natejswenson.io` workflow, adapted for a public repo). `main` is the
  default/production branch and `dev` is integration; both are protected
  (CI `validate` green + a PR required, linear history, squash-only,
  branch auto-deleted on merge, 0 required reviews so a green PR
  self-merges via native auto-merge). `enforce_admins` is off as a
  deliberate solo-dev break-glass path. The old `master` branch was
  renamed to `main`.
- **Release is now auto-tagged on a `dev → main` promotion.** `release.yml`
  stays version-driven and is retargeted to `[main]`: a promotion that
  bumps this version (with a matching CHANGELOG entry) auto-cuts the tag;
  a no-bump promotion is an idempotent no-op. CI runs on `[main, dev]`.
- **Dependabot now targets `dev`** on all four ecosystems, so dependency
  bumps flow through the same promotion path.

### Fixed
- **Container build under `node:26`**: the base-image bump dropped the
  bundled `corepack` shim, breaking `corepack enable`. Install
  `corepack@latest` explicitly in the web-builder stage. (CI didn't catch
  it — CI runs `pnpm build` on the host, not the Docker image.)

## [0.11.0] - 2026-06-23

### Added
- **The coach profile now carries into tool-driven Claude Code chat**, not just
  the `/mcp__fitness__coach` slash command. The fitness MCP server advertises the
  resolved coach persona as its top-level `instructions`, so when Claude Code
  answers a fitness question by calling the MCP tools (rather than the slash
  command), the reply adopts your selected `coach_profile`'s voice.

  Resolution is **live, per client connect** (a `create_initialization_options`
  wrap): a `fitness config set coach_profile …` takes effect on the next
  connect — no server restart, consistent with the slash-command prompts. The
  wrap is **import-safe** (it does no DB I/O at server-build time, which on the
  HTTP path runs before the schema is initialized) and **fail-open** (a
  resolution error advertises no persona rather than breaking the MCP handshake).
  `fitness mcp-stdio` now initializes the schema for parity. Reuses the existing
  `system_prompt` unchanged (no prompt edit; `score_prompt.py` untouched).

  Designed and `/quality-gate`-reviewed first
  (`docs/plans/2026-06-23-mcp-server-coach-persona-design.md`); the gate caught a
  clone-breaking import-time crash in the first approach.

## [0.10.0] - 2026-06-23

### Added
- **Selectable coach tone profiles for the daily brief.** The coaching voice is
  now a profile you pick instead of one hardcoded blend:
  - `supportive` — always upbeat and encouraging; frames every read as a
    bounce-back, never roasts;
  - `neutral` — emotion out of it, tells you how it is against your goals;
  - `hardass` — cynical and relentless; rips you for anything short of
    overachieving and always pushes for more;
  - `adaptive` *(default)* — today's "supportive when trending well, roast when
    slipping" behavior, unchanged for a fresh clone.

  Each profile is a fully-fleshed `agent/coach_profiles/<name>.md` (voice body +
  numeric dials) with tunable characteristics: `harshness`/`warmth`/`push` (0–10
  prose calibration) and `roast_threshold`/`praise_threshold` (fractions of goal).
  The thresholds carry **deterministic** behavior — for goal-based mandates
  (steps, plan adherence) a harsh profile assembles the harsh-tone imperative
  block and a soft profile omits it (gated on `harshness`), which is unit-tested.
  Select with `uv run fitness config set coach_profile hardass` or
  `LOCAL_FITNESS_COACH_PROFILE`; override any dial (`coach_harshness`, …) the same
  way — resolution is settings DB > env > the profile's own value.

  Verification (every profile against expected outcomes, not eyeballed): a new
  deterministic `scripts/score_profiles.py` (27 checks, CI-gated via
  `test_coach.py`) asserts each profile keeps the schema/tone/jargon contracts
  and that the harsh-block gating is correct per profile; `scripts/ab_brief.py
  --profile <name>` runs a generative A/B per profile; the adaptive default's
  cross-model A/B is `consistent` and `score_prompt.py` stays green unchanged.
  Designed and `/quality-gate`-reviewed first
  (`docs/plans/2026-06-23-coach-tone-profiles-design.md`).

## [0.9.0] - 2026-06-23

### Added
- **Grading and projection behavior is now user-configurable** instead of
  hardcoded to one runner's preferences. Five knobs, each defaulting to the
  previous hardcoded value (so a fresh clone is unchanged):
  - `count_walks_easy` (default `true`) — do recovery walks satisfy an
    easy/recovery prescription;
  - `count_walks_mileage` (default `false`) — include walking in the weekly
    mileage rollup;
  - `grade_done_fraction` / `grade_partial_fraction` (`0.80` / `0.40`) — the
    done/partial grade bands;
  - `riegel_lookback_days` (`120`) — lookback window for the projected finish.

  Resolution precedence is **settings DB > env var > default**: set a value live
  with `uv run fitness config set <key> <value>`, or in `.env`
  (`LOCAL_FITNESS_COUNT_WALKS_EASY`, etc.; documented in `.env.example`). Values
  are validated — a blank or unrecognized value falls back to the default, and an
  inverted fraction pair (`partial > done`) or out-of-range fraction reverts both
  to defaults so the grade bands can't invert; a nonsense lookback clamps to the
  default. A new `config.py` accessor resolves the knobs; a `GradingConfig`
  dataclass threads them into the (still pure) grading functions in `plans.py`,
  resolved once per request by the brief, the plan tool, and the web plan route —
  so the brief and the tab grade consistently. Designed and `/quality-gate`-
  reviewed first (`docs/plans/2026-06-23-configurable-grading-design.md`).

## [0.8.0] - 2026-06-23

### Fixed
- **Training-plan grading: a completed workout today no longer shows
  `pending`.** `grade_workout` is now outcome-based — it grades first and holds
  `pending` only when the verdict is negative (`missed`/`partial`) AND the day's
  data window is still open. A synced workout grades immediately, even today;
  rest days resolve to `compliant` instead of lingering `pending`. (Holding
  `partial` too prevents a mid-day half-done run from prematurely counting 0.5
  in adherence and then self-healing.)
- **A recovery walk is now reflected in the plan.** Easy/recovery days count
  walking distance toward the prescription (active recovery is the intent);
  `long`/`tempo`/`interval`/`race` stay running-only. Per-workout actuals are
  now foot-based (running + walking) on every day and carry a normalized
  `actual_activity_types` (e.g. `["walking"]`), so a walk is visible regardless
  of verdict. The plan tab's Actual-cell coloring is now driven by the backend
  `verdict` (red only when `missed`) instead of recomputing a pace/distance
  miss — so a walk-counted `done` day no longer paints red on walking pace.
  Weekly mileage intentionally stays running-only (it's a run-volume metric,
  distinct from recovery-day adherence).

Designed and `/quality-gate`-reviewed first
(`docs/plans/2026-06-23-plan-grading-fixes-design.md`; 4 rounds + look-harder,
5→0). Frontend coloring verified by screenshot of the plan tab.

## [0.7.0] - 2026-06-22

### Added
- **`get_training_plan_progress` MCP tool** — returns the full graded training
  plan day-by-day (every prescribed workout with its verdict:
  done/partial/missed/compliant/pending), plus goal, days-to-race, adherence %,
  and projected finish. Fills the gap that previously forced ad-hoc `sqlite3`
  spelunking to answer "show my plan through today". Implemented as a deliberate
  projection over `build_plan_detail` with a no-active-plan guard and a
  `.get`-hardened `days_to_race`; kept out of the brief's read-only allow-list
  so the brief stays cheap. Designed and `/quality-gate`-reviewed first
  (`docs/plans/2026-06-22-fitness-qa-clean-output-design.md`).

### Changed
- The shared chat-formatting block in `system_prompt()` now tells the agent to
  prefer the structured `mcp__fitness__*` tools, never shell out to
  `sqlite3`/Bash for a DB read, and present answers cleanly instead of narrating
  the lookup. Mirrored as a new "Answering fitness questions" section in
  `CLAUDE.md` for the in-repo Claude Code surface. (Verified: static prompt
  scorer green; the edit lives in the chat-only block and introduced no new
  brief A/B divergence — the `ab_brief.py` `_generate` path fails identically
  with and without the edit due to a pre-existing harness flake, unrelated to
  this change.)

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
