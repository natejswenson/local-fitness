# Retiring the web UI

**2026-07-09**

Nate stopped using the web UI months ago — every interaction with the coach
goes through Claude or opencode over MCP now. Part B of the MCP-speed/UI-
retirement design (`docs/plans/2026-07-09-mcp-speed-and-ui-retirement-design.md`):
delete the UI outright, keep and improve the API.

## What's gone

- The entire `web/` directory — React/Vite SPA, ~3,800 LOC of TSX, its build
  tooling (`package.json`, `vite.config.ts`, `tsconfig*.json`, `eslint.config.js`,
  `pnpm-lock.yaml`).
- Every UI-only REST route in `src/local_fitness/web/server.py` — ~20 routes
  covering today's snapshot, metric trends, training load, workouts, three
  custom dashboards, plan CRUD, the sync-orchestration subsystem (six
  private functions, six module-level state vars, three constants), and
  notes CRUD (already redundant with the MCP note tools).
- SPA static serving: the `StaticFiles` mount, the path-traversal-guarded
  `spa_fallback` catch-all, `root_no_build()`, and the `WEB_DIST`/
  `_PROJECT_ROOT` constants that fed them.
- Docker's `web-builder` stage, CI's Node/pnpm steps, `dependabot.yml`'s npm
  entry, `codeql.yml`'s javascript-typescript matrix leg.
- `fitness serve --open` — opened a browser to a page that no longer exists.

`src/local_fitness/web/server.py` itself stays: it still hosts the
authenticated MCP streamable-HTTP transport at `/mcp/` plus `/health`. "Keep
and improve the API" was the point — not remove the server.

## Closing the gap this surfaced

A 2026-07-06 design for `commit_training_plan`/`discard_training_plan_draft`
existed on disk but was never implemented — those actions only ever had a
UI path (the commit/delete buttons). Retiring the UI without shipping them
would have stranded plan activation entirely. Implemented both as-is from
that design, plus a third tool this design adds: `abandon_active_plan`
(archive the active plan, nothing queued to replace it, no undo) — the
2026-07-06 design deliberately left this one UI-only "on the reasoning that
the UI existed as a fallback for that bigger-blast-radius action." That
reasoning stopped holding the moment the UI was gone.

## The security flip

`_is_public_path` used to default to **public** for anything outside
`/api/`/`/mcp/` — justified because an unauthenticated browser tab has to
load *something* before it can prompt for a token. With no SPA left, that
justification is gone, so the fallthrough flips to **deny**. Only `/health`
stays whitelisted. Verified end-to-end in a real container: `/health` → 200
unauthed, `/` → 401, `/mcp/` → 401 without a bearer token.

## Bugs caught during implementation

- **`actions/upload-artifact@v4` silently drops dotfiles/dotdirs by
  default** — unrelated to Part B directly, but hit while re-verifying
  Part A's perf-benchmark baseline: `.benchmarks/` never made it into the
  artifact until `include-hidden-files: true` was added. The pytest log
  said "Saved benchmark data" and the very next step said "No files were
  found" — same job, same workspace, no error, just a silent no-op upload.
- **A Docker-captured pytest-benchmark baseline isn't safe just because
  the machine-id string matches.** Captured the Part A baseline in a local
  `linux/amd64` container on Apple Silicon — same `Linux-CPython-3.12-64bit`
  directory pytest-benchmark groups by — but Docker Desktop's emulated CPU
  ("VirtualApple @ 2.50GHz" reported inside the container) is dramatically
  slower than real Azure/GitHub metal, so every benchmark target "regressed"
  27-42% against it on pure hardware noise. Fixed by capturing the real
  baseline from a genuine `ubuntu-latest` CI run instead (downloaded via a
  new `--benchmark-autosave` + artifact-upload step in `ci.yml`) and
  promoting that to the committed `0001`.
- **`workflow_dispatch` workflows aren't dispatchable until they exist on
  the repo's default branch.** `capture-perf-baseline.yml` (added in Part A)
  can't be triggered via `gh workflow run` from a feature branch — GitHub
  requires the workflow file to already be on `main` first, which this
  repo's `dev → main` promotion only does deliberately and rarely. Worked
  around it for the initial baseline; documented the gap in the workflow
  file itself for next time.

## Verified

956 → 889 tests (net: -60 net after deleting `test_web_api.py` (~47 tests),
`test_web_plan.py`, `test_web_brief.py`, six broken/rotted `test_security.py`
cases, adding back a handful salvaged and rewritten against `/mcp/`, plus new
plan-lifecycle coverage for the three new tools). 93.37% coverage (gate is
85%). Docker image builds and, in a real container run: `/health` 200,
unauthed `/` and `/mcp/` both 401, correctly refuses to bind `0.0.0.0`
without `LOCAL_FITNESS_API_TOKEN` set.

## Explicitly out of scope

- No rebuild of the retired dashboard-specific visualizations
  (activity-heatmap, strength-volume, pace-efficiency) as MCP tools —
  `generate_chart` already covers ad-hoc single-metric trend questions;
  revisit only if a real need surfaces.
- `plans.delete_plan` is now dead code (its only caller, `DELETE
  /api/plan/{id}`, is gone) — left in place rather than deleted; flagged as
  an open call, not decided here.
