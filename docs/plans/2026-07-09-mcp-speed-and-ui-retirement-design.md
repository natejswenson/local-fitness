---
ticket: "N/A"
title: "MCP tool speed/efficiency + retire the web UI"
date: "2026-07-09"
source: "design"
---

# MCP tool speed/efficiency, and retiring the web UI

## Goal

Two related changes, scoped together because the second makes the MCP tool
surface the *only* interface, which raises the bar on the first:

1. **Speed up the MCP tool surface** (`agent/tools.py`'s ~32 `@tool`
   functions), with every claimed improvement proven by a new, CI-gated
   before/after benchmark — not eyeballed.
2. **Retire the web UI** (`web/src/` React/Vite frontend + the REST routes
   in `web/server.py` that exist only to serve it). Nate no longer uses it
   directly; all interaction is via Claude Code / opencode as MCP clients.
   `server.py` itself is **not** retired — it still hosts the authenticated
   MCP streamable-HTTP transport — but everything UI-only comes out.

## Background / why now

Investigation (4 parallel research + repo-audit agents) overturned most of
the intuitive assumptions for part 1, and surfaced a real functional gap
for part 2 that the original scope didn't anticipate. Both are covered
below.

## Part A — MCP tool speed and efficiency

### What's NOT the problem (ruled out by investigation)

- **Connection pooling.** SQLite opens a connection in-process in ~5-20μs
  (no socket/auth handshake) — pooling exists to amortize network-handshake
  cost (e.g. Postgres), which doesn't apply here. Chasing it is a premature
  optimization for this app's scale.
- **Response payload size.** Already well-managed: `query_workouts` trims
  columns and defaults `limit=50`, `get_workout_detail` strips `raw_json`,
  `run_sql` caps at 500 rows with a wall-clock deadline, and
  `get_training_plan_progress` projects a slim dict rather than the raw
  `build_plan_detail` spread. No eval budget spent here.
- **Missing indexes.** `idx_activities_date`, `idx_activities_type`,
  `idx_plan_workouts_date`, `idx_obs_date` already cover the heaviest
  date-range filters. No missing-index finding.

### What is the problem (concrete, file:line)

1. **The daily automated hot path opens ~8 separate DB connections per
   run.** `agent/brief_planner.py`'s `assemble_brief_context()` — called
   twice by the unattended 06:30 `fitness brief` job
   (`agent/briefing.py:547,600`) and once per ad-hoc `get_brief_context`/
   `get_training_plan_progress`/`get_training_plan_status` MCP call
   (`agent/tools.py:1417-1457`, `:1684-1711`) — chains: `db.get_setting`
   (conn #1) → `db.get_setting` (#2) → `resolve_coach_profile` →
   `db.all_settings` (#3, `agent/coach.py:163`) → `_plan_today` →
   `plans.get_active_plan` (#4) → `db.last_known_daily_date` (#5) →
   `plans.load_activities_by_date` (#6) → `resolve_grading_config` →
   `db.all_settings` (#7, `brief_planner.py:568-577`) → one final batched
   `with db.connect()` block (#8, `brief_planner.py:662`). Every `connect()`
   (not `connect_readonly()`) re-executes two write PRAGMAs
   (`db.py:206-219`) even for pure reads.
2. **Real N+1 in `agent/status.py`'s `_metric_rows` (line 128-138).** For
   each of 3 `_TREND_METRICS` (steps, sleep_score, max_stress), it runs a
   separate `SELECT {metric} FROM daily_metrics WHERE date >= ? ...` over
   the *identical* 7-day window — 3 near-duplicate scans that are one query.
   Hit by every `daily_snapshot` call and inside the brief's shared block.
3. **Unbounded query in `brief_planner._compute_signals` (line 501-503).**
   `SELECT date, activity_type, aerobic_te FROM activities WHERE date <= ?
   ORDER BY date DESC` has **no lower bound** — it materializes Nate's
   entire multi-year activity history every single brief, even though only
   the trailing 28 days (`runs_14d`/`runs_prior_14d`) and the last 5 TE
   values are used. The index makes the scan cheap; Python-side
   materialization cost still grows unbounded with account age. Harmless
   today (small dataset), a silent scaling bug otherwise.
4. **No existing perf instrumentation anywhere in the repo** — this starts
   from zero. The only `time.perf_counter()` usage
   (`agent/briefing.py:646,677,703,782`) times Claude's streaming
   generation loop, not DB/tool latency.

### Fixes (in priority order)

1. **Thread one shared connection through `assemble_brief_context`,
   `get_training_plan_progress`, and `get_training_plan_status`** instead
   of opening ~8. Collapses connection-open + PRAGMA-execution overhead on
   the one path that runs unattended every morning. `resolve_grading_config`,
   `_plan_today`, and the settings reads all need an optional
   `conn: sqlite3.Connection | None = None` parameter that, when passed,
   is used directly instead of opening a fresh one — additive, not a
   breaking signature change (existing callers that pass nothing keep
   today's behavior).
2. **Collapse `_metric_rows`'s 3-query loop into 1 query** selecting all
   three `_TREND_METRICS` columns over the shared window in a single
   `SELECT`.
3. **Bound `_compute_signals`'s activities query** to a fixed lookback
   (35 days — covers the 28-day windows plus slack) instead of unbounded.
4. **Verify/add `PRAGMA busy_timeout`** in `db.connect()`. Unrelated to
   the latency work, but a real, cheap correctness win the research
   surfaced: its absence — not connection-per-call — is the documented
   cause of most `SQLITE_BUSY` failures under WAL.

### Eval-proof methodology (the hard requirement)

The existing prompt-quality scorer (`tests/evals/` + `capture_baseline.py`)
measures generation grounding, not latency — this needs a **new, separate**
harness, same convention, different axis:

- **`pytest-benchmark`**, wrapping `assemble_brief_context`,
  `get_training_plan_progress`, `get_training_plan_status`, and
  `daily_snapshot`/`assemble_status()` as benchmark targets.
- Run against a **realistic-scale fixture DB** (multi-year history), not
  the small existing eval fixtures — fix #3's bug is invisible at small
  scale by construction, so the eval must include a fixture large enough
  to make the before/after delta real.
- Baseline JSON committed alongside the existing `capture_baseline.py`
  convention (`tests/evals/perf_baseline.json` or similar), compared on
  every run.
- CI-gated with `pytest --benchmark-compare-fail=min:15%` (gate on `min`,
  not `mean` — less noisy on shared CI runners per pytest-benchmark
  guidance).
- **Connection-open count is an explicit assertion, not just latency** —
  a monkeypatched counter around `db.connect`/`connect_readonly` so a
  regression test can assert e.g. "`assemble_brief_context` opens ≤2
  connections." Latency alone is noisy and can hide a correctness
  regression (e.g. accidentally sharing a connection across a commit
  boundary, or merging a write into what should stay a read-only path).
- **Correctness parity is non-negotiable**: `tests/test_brief_planner.py`,
  `tests/test_plan_tools.py`, and `tests/evals/`'s shadow-run gate must all
  stay green. Threading a shared connection through read-only lookup
  functions is safe; the same refactor touching a *write* path is not, and
  the design deliberately does not merge any write call into a shared
  connection scope.

## Part B — Retire the web UI

### What's removed

- **All of `web/src/`** (React/Vite SPA, ~3,819 LOC).
- **Every UI-only REST route in `web/server.py`**: `/api/today`,
  `/api/metric/{name}`, `/api/training-load`, `/api/workouts`,
  `/api/workout/{id}`, `/api/brief` (GET), `/api/sync` (POST),
  `/api/notes` (GET/POST/DELETE — already redundant with
  `list_user_notes`/`save_user_note`/`delete_user_note`), `/api/plan`
  (read), `/api/plan/draft`, `/api/plan/{id}/commit`, `DELETE
  /api/plan/{id}`, `/api/activity-heatmap`, `/api/strength-volume`,
  `/api/pace-efficiency`, `/api/status`, `/api/config`,
  `/api/auth/verify`, `/api/sync/status`.
- **SPA static serving**: the `StaticFiles` mount and `spa_fallback`
  catch-all route (`web/server.py:1037-1057+`).
- **Docker's `web-builder` stage** (`Dockerfile:8-32`) and its
  `COPY --from=web-builder` into the runtime stage.
- **CI's frontend job step**: `pnpm install`/`pnpm build`/`pnpm test`
  inside the `validate` job (`.github/workflows/ci.yml:68-91`), and the
  `docker-build` job's web-builder-stage coverage shrinks accordingly
  (still compiles the whole image, just without a Node stage).

### What stays

- `web/server.py` itself — it hosts the authenticated MCP
  streamable-HTTP transport at `/mcp/`, used by any network-connected
  MCP client (not just historically the UI). The bearer-auth middleware,
  `/health`, and the `/mcp/` mount are unaffected.
- Every existing MCP tool. "Keep and improve the API" = the MCP tool
  surface is the API now; Part A is that improvement.

### The gap this surfaces: plan lifecycle

A fully-specced, quality-gated design already exists on disk
(`docs/plans/2026-07-06-agent-plan-lifecycle-design.md`, 9 rounds PASS)
for two MCP tools — `commit_training_plan` and
`discard_training_plan_draft` — that was **never implemented**. Today,
flipping a draft plan to active, or discarding a draft, has **no MCP
tool at all**; it only ever existed via the UI's commit/delete buttons.
Retiring the UI without shipping these would strand plan activation
entirely.

That prior design **reused as-is** (see its own doc for full detail —
not re-litigated here):

| Symbol | Layer | Signature |
|---|---|---|
| `plans.discard_draft` | persistence | `(plan_id: int, db_path: Path \| None = None) -> None` |
| `commit_training_plan` | MCP tool | `{plan_id: int} -> {plan_id, status: "active"}` |
| `discard_training_plan_draft` | MCP tool | `{plan_id: int} -> {plan_id, status: "archived"}` |

That prior design also deliberately left **"abandon the active plan with
nothing queued to replace it"** UI-only, on the explicit reasoning that
the UI existed as a fallback for that bigger-blast-radius action. That
reasoning no longer holds once the UI is gone. **Decided: add a third
tool** rather than leave it stranded.

### New: `abandon_active_plan`

Reuses the *already-existing* `plans.NoActivePlanError`
(`plans.py:436`, already raised by `get_training_plan_progress`'s
underlying lookup) — no new exception type needed.

```python
def abandon_active_plan(db_path: Path | None = None) -> int:
    """Archive the currently active plan, leaving no active plan.
    Returns the archived plan's plan_id. Raises NoActivePlanError if
    none exists."""
    with db.connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE training_plans SET status='archived' "
            "WHERE status='active' RETURNING plan_id"
        )
        row = cur.fetchone()
        if row is None:
            raise NoActivePlanError("no active plan")
        return row["plan_id"]
```

```python
@tool(
    "abandon_active_plan",
    "Archive the currently active training plan with nothing queued to "
    "replace it. Only call when the user explicitly asks to stop "
    "following their plan entirely — never proactively, and never as "
    "part of activating a new plan (commit_training_plan already "
    "archives the prior active plan atomically as part of that swap). "
    "No undo tool exists for this.",
    {"type": "object", "properties": {}},
)
async def abandon_active_plan_tool(args: dict) -> dict:
    ...  # plans.abandon_active_plan(); catch NoActivePlanError -> _err(str(e));
         # else _text({"plan_id": archived_id, "status": "archived"})
```

Deliberately **no `plan_id` argument** — since `idx_one_active_plan`
guarantees at most one active plan exists, requiring the caller to name
it would only add a chance to pass a stale/hallucinated id; operating on
"whichever plan is active" is unambiguous and matches
`get_training_plan_status`'s existing no-arg shape.

All three tools (`commit_training_plan`, `discard_training_plan_draft`,
`abandon_active_plan`) are added to `ALL_TOOLS` and **not** added to
`_READ_ONLY_TOOL_NAMES` (an allowlist, not a denylist — exclusion is
automatic).

## API Surface (full, this design)

| Symbol | Layer | Signature |
|---|---|---|
| `plans.discard_draft` | persistence | `(plan_id: int, db_path: Path \| None = None) -> None` |
| `plans.abandon_active_plan` | persistence | `(db_path: Path \| None = None) -> int` |
| `commit_training_plan` | MCP tool | `{plan_id: int} -> {plan_id, status: "active"}` |
| `discard_training_plan_draft` | MCP tool | `{plan_id: int} -> {plan_id, status: "archived"}` |
| `abandon_active_plan` | MCP tool | `{} -> {plan_id, status: "archived"}` |
| `assemble_brief_context` / `resolve_grading_config` / `_plan_today` | internal | gain an optional `conn: sqlite3.Connection \| None = None` param; behavior unchanged when omitted |
| `status._metric_rows` | internal | same signature, single query instead of 3 |
| `brief_planner._compute_signals` | internal | same signature, bounded 35-day lookback instead of unbounded |

Reused, unchanged: `plans.commit_plan`, `PlanNotFoundError`,
`NotDraftError`, `NoActivePlanError`, `_err`/`_text` tool-response
helpers, `db.connect`/`db.connect_readonly`.

## Invariants

**Checkable by inspection:**
- No write call is ever passed a shared connection opened for a read-only
  scope — the connection-reuse fix in Part A only threads through
  read-only lookup functions.
- `abandon_active_plan` never mutates a row unless `status='active'` — the
  predicate is inline in the `UPDATE`, not a separate SELECT-then-write.
- None of the three new plan-lifecycle tools are in
  `_READ_ONLY_TOOL_NAMES`.
- `web/server.py` retains the `/mcp/` mount and bearer-auth middleware
  unchanged; only UI-only routes and the static mount are removed.
- No `Dockerfile` stage references `web/` after this change; the runtime
  stage's `COPY --from=web-builder` line is deleted, not merely unused.

**Testable:**
- `assemble_brief_context`/`get_training_plan_progress`/
  `get_training_plan_status` open ≤2 connections per call (monkeypatched
  counter assertion).
- `_metric_rows` issues exactly 1 query for the 3 trend metrics (not 3).
- `_compute_signals` never queries `activities` rows older than the
  bounded lookback (assert on the SQL params or on returned row dates in
  a fixture with data older than the bound).
- `commit_training_plan`/`discard_training_plan_draft`/
  `abandon_active_plan`: success path + every error path
  (`PlanNotFoundError`/`NotDraftError`/`NoActivePlanError`), each with no
  mutation on the error paths. `test_tools_registered()` extended with
  all three names. Read-only-allowlist parity tests for all three
  (absent from `read_only_tool_names()`).
- `pytest-benchmark` suite passes with no CI-flagged regression
  (`--benchmark-compare-fail=min:15%`) against the committed baseline.
- `tests/test_web_*` suite shrinks to cover only what remains
  (`/health`, `/mcp/`, auth middleware) — all UI-route tests for removed
  endpoints deleted, not left as dead/skipped tests.

## Testing strategy

- New: `tests/test_perf_benchmarks.py` (or similar) — the
  `pytest-benchmark` suite from Part A, run against a realistic-scale
  fixture DB checked into `tests/fixtures/` (or generated by a
  `scripts/`-level fixture-builder, mirroring how `capture_baseline.py`
  already builds eval fixtures).
- Extend `tests/test_plans_db.py` with `discard_draft` and
  `abandon_active_plan` persistence-layer cases (mirrors the existing
  `test_commit_*`/`test_delete_*` style).
- Extend `tests/test_plan_tools.py` with tool-layer cases for all three
  new tools (success + every error path + wrong-type `plan_id` rejection
  for the two that take one).
- Delete `tests/test_web_plan.py`'s coverage of removed REST routes;
  keep/adapt whatever still exercises `/health` and `/mcp/`.
- `tests/test_security.py`: no new HTTP surface is introduced (the new
  tools are reached only through the already-authenticated MCP session,
  same trust boundary as existing plan-write tools) — but *update* it to
  drop assertions about now-removed `/api/*` routes so it doesn't assert
  against dead code.
- Full `uv run pytest -x` and `docker compose up -d --build local-fitness`
  pass required before calling this done, per repo convention.

## Documentation updates (same PR, per CLAUDE.md)

- **CLAUDE.md**: "What's already wired" section updated to reflect (a)
  the web UI no longer exists, (b) `server.py`'s continued role as the
  MCP transport host, (c) the three new plan-lifecycle tools superseding
  "the agent owns plan writes; the UI is view-only" language (now: the
  agent owns the *entire* plan lifecycle), (d) the new perf-eval
  convention alongside the existing prompt-eval one. "File-layout
  reference" section: remove `web/src/` and `web/server.py`'s
  UI-serving role.
- **README.md**: remove every UI-referencing section (screenshots,
  "27/29 tools" counts bumped to the new total, Traefik/UI deployment
  instructions specific to the frontend). Fix the tool-count claim and
  the stale Write-tools categorization noted in the 2026-07-06 design
  while this section is being touched anyway.
- **`docs/deployment.md`**: remove the frontend-build/serving guidance;
  keep whatever's still relevant to the FastAPI/MCP container.
- **Release process**: version bump + `CHANGELOG.md` entry + `devlog/`
  entry, same PR, per CLAUDE.md's release policy (functional + code
  change).

## Out of scope (explicit, YAGNI)

- No caching layer (Redis, in-memory TTL cache, etc.) for MCP tool
  responses — not justified by the findings; the fixes above address the
  actual measured problems without adding a new moving part.
- No connection-pooling library (e.g. a `sqlite3` pool wrapper) — ruled
  out by research as solving a cost this app doesn't have.
- No rebuild of the retired dashboard-specific visualizations
  (`activity-heatmap`, `strength-volume`, `pace-efficiency`) as MCP
  tools/charts. `generate_chart` already covers ad-hoc single-metric
  trend questions; these three were UI-specific aggregate views with no
  stated ongoing need now that the UI is gone. Revisit only if a real
  need surfaces.
- No changes to `run_sql`, `query_workouts`, or any other tool whose
  payload/indexing was investigated and found already adequate.
