---
ticket: "N/A"
title: "MCP tool speed/efficiency + retire the web UI"
date: "2026-07-09"
source: "design"
status: "quality-gated PASS-with-follow-up (13 rounds; weighted score 10→9→8→6→3→3→3→2→2→3→3→1→1; 0 Fatal / 0 Significant blocking as of round 13's final accepted state)"
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

1. **Two separate hot paths each open a stack of independent DB
   connections per call — not one shared chain.**
   - **1a. `agent/brief_planner.py`'s `assemble_brief_context()`** —
     called once by the unattended 06:30 `fitness brief` job
     (`agent/briefing.py:600`, the V2 production branch) and once per
     ad-hoc `get_brief_context` MCP call (`agent/tools.py:1517-1533`).
     There's a second call site at `agent/briefing.py:547`, but it sits
     inside a mutually-exclusive alt-model branch gated by an early
     return at :592 — that branch is shadow-run-only (its own adjacent
     comment says the real Claude model this codebase runs in production
     never takes it), so the unattended job only ever reaches the :600
     call, once, not twice. Doesn't change the fix's priority or the
     9-connection count below, which is independently correct — only the
     "how many times production calls this" framing. Chains: `db.get_setting`
     (conn #1) → `db.get_setting` (#2) → `resolve_coach_profile` →
     `config.coach_profile` → `db.get_setting` (#3, `config.py:64`, inside
     `config._resolve` — `agent/coach.py:161` only calls
     `config.coach_profile`, which routes through `_resolve`; the actual
     DB read happens in `_resolve` itself — a hop the original count of
     this problem missed) →
     `db.all_settings` (#4, `agent/coach.py:163`) → `_plan_today` →
     `plans.get_active_plan` (#5) → `db.last_known_daily_date` (#6) →
     `plans.load_activities_by_date` (#7) → `resolve_grading_config` →
     `db.all_settings` (#8, `plans.py:47`, inside `resolve_grading_config`
     — `brief_planner.py:568-577` is `_plan_today()`'s own body, which
     only calls `plans.resolve_grading_config(db_path)` at :576; the
     actual DB read happens inside `resolve_grading_config` itself) →
     one final batched `with db.connect()` block (#9, `brief_planner.py:662`).
     That's **9** connections, not 8 — **when a plan is active; `_plan_today`
     calls `plans.get_active_plan` first, and on the early `{"active":
     False}` return it never reaches `last_known_daily_date`/
     `load_activities_by_date`/`resolve_grading_config`, so a call with
     no active plan opens only 6.** (1b/1c already state this
     active-plan caveat explicitly for their own counts; 1a's count is
     conditional the same way even though the number itself is
     independently correct.) Every `connect()` (not
     `connect_readonly()`) re-executes two write PRAGMAs
     (`db.py:206-219`) even for pure reads — the PRAGMA execution itself
     is cheap once WAL is active, so it isn't the cost driver; the real
     savings from fixing this is the per-call connection-open/
     object-creation overhead compounding across 9 opens, not repeated
     PRAGMA cost.
   - **1b. `agent/tools.py`'s `get_training_plan_status` (:1417-1428)
     and `get_training_plan_progress` (:1442-1494 — the
     connection-opening prologue runs through :1457, matching :1417-1428's
     full-function citation convention for the shorter sibling)** — these
     do **not** call `assemble_brief_context` or `_plan_today`; they
     independently call the same-*named* plan/date lookups directly, plus
     their own extras. `get_training_plan_status`: `plans.get_active_plan`
     (#1) → `db.last_known_daily_date` (#2) → `plans.load_activities_by_date`
     (#3) → `resolve_grading_config` → `db.all_settings` (#4) — 4
     connections when a plan is active, 1 on the early `{"active":
     False}` return. `get_training_plan_progress` also calls all four of
     those lookups, but **not in the same order or position** —
     `resolve_grading_config` is called *last*, not fourth. Verified
     actual call order (`agent/tools.py:1442-1457`): `get_active_plan`
     (#1) → `last_known_daily_date` (#2) → `load_activities_by_date` (#3)
     → `config.riegel_lookback_days()` → `db.get_setting` (#4) →
     `plans.best_recent_effort()` (#5, `plans.py:661-681`, its own
     `with db.connect()` block, not mentioned anywhere else in this
     design) → `resolve_grading_config` → `db.all_settings` (#6) —
     **6** connections when a plan is active (4 + 2, not "3 +
     2" as an earlier draft of this arithmetic implied). 1a and 1b are
     independent call chains; each needs
     its own fix (below), and neither is a subset of the other.
   - **1c. `agent/tools.py`'s `_build_plan_section()` (:1684-1711 — the
     connection-opening prologue; the full function continues through
     :1779, same "prologue vs. full function" distinction 1b's citation
     makes above)**, used only by `generate_brief_report`'s PDF pipeline — its own
     docstring says it "Mirrors `get_training_plan_progress`'s
     plan-loading pattern." It independently opens the same four
     connections as 1b's first four: `plans.get_active_plan` (#1) →
     `db.last_known_daily_date` (#2) → `plans.load_activities_by_date`
     (#3) → `plans.resolve_grading_config` (#4) — 4 connections when a
     plan is active, 1 on the early no-active-plan `None` return. A
     third independent call chain, not a subset of 1a or 1b — named here
     for completeness even though its fix priority differs (see fix #1
     below).
2. **Real N+1 in `agent/status.py`'s `_metric_rows` (line 128-138).** For
   each of 3 `_TREND_METRICS` (steps, sleep_score, max_stress), it runs a
   separate `SELECT {metric} FROM daily_metrics WHERE date >= ? ...` over
   the *identical* 7-day window — 3 near-duplicate scans that are one query.
   Hit by every `daily_snapshot` call and inside the brief's shared block.
3. **Unbounded query in `brief_planner._compute_signals` (line 503-506).**
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

1. **Thread one shared connection through all three call chains** — 1a
   (`assemble_brief_context`), 1b (`get_training_plan_status`/
   `get_training_plan_progress`), and 1c (`_build_plan_section`) —
   instead of opening ~9, ~6, and ~4 respectively. Collapses
   connection-open overhead on the path that runs unattended every
   morning, on every ad-hoc plan query, and on the on-demand PDF
   pipeline. Every function in all three call chains needs an optional
   `conn: sqlite3.Connection | None = None` parameter that, when passed,
   is used directly instead of opening a fresh one — additive, not a
   breaking signature change (existing callers that pass nothing keep
   today's behavior). Full list, not a subset of the functions named in
   the original scope:
   - **1a's chain**: `resolve_coach_profile`, `config.coach_profile`,
     `config._resolve` (`config.py:62-64`), `resolve_grading_config`,
     `_plan_today`, `plans.get_active_plan`, `plans._get_by_status`,
     `db.last_known_daily_date`, `plans.load_activities_by_date`,
     `db.get_setting`, `db.all_settings`.
     `config.coach_profile` doesn't call `db.get_setting` directly — it
     routes through the private helper `_resolve(key, env, default,
     cast, db_path=None)`, which must also gain and pass through `conn`,
     or threading `coach_profile` alone does nothing (the unlisted
     `_resolve` would still open its own connection underneath it). Same
     bug class applies one level down: `plans.get_active_plan(db_path=None)`
     (`plans.py:641-642`) doesn't open a connection itself — it just calls
     `_get_by_status("active", db_path)` (`plans.py:628-638`), which is
     where the actual `with db.connect(db_path) as conn:` lives. `conn`
     must be added to and threaded through `_get_by_status` too, or
     threading `get_active_plan` alone does nothing (the unlisted
     `_get_by_status` would still open its own connection underneath it).
   - **1b's chain** (separate from 1a — `get_training_plan_status`/
     `get_training_plan_progress` call these directly, not through
     `_plan_today`): `plans.get_active_plan`, `plans._get_by_status`
     (same underlying helper `get_active_plan` delegates to — see 1a's
     note above; must gain `conn` too or threading `get_active_plan`
     alone is a no-op here as well), `db.last_known_daily_date`,
     `plans.load_activities_by_date`, `plans.resolve_grading_config`,
     `db.all_settings`, `config.riegel_lookback_days`, `config._resolve`
     (`config.py:62-64` — same private helper as 1a; `riegel_lookback_days`
     also routes through it rather than calling `db.get_setting`
     directly), `db.get_setting`, and `plans.best_recent_effort`
     (`plans.py:661-681`). Its only other
     caller is `/api/plan`/`/api/plan/draft` (via `_assemble_plan_detail`,
     `web/server.py:381-399` — not `/api/training-load`, which lives at
     `web/server.py:363-372` and only queries the baselines table
     directly, never touching `best_recent_effort`), both of which
     Part B removes — so after this change `best_recent_effort` has
     exactly one live caller and no back-compat constraint on adding the
     `conn` param.
   - **1c's chain: `_build_plan_section()` (`agent/tools.py:1684-1711`),
     used only by `generate_brief_report`'s PDF pipeline.** Same
     underlying functions as 1b's first five (`plans.get_active_plan`,
     `plans._get_by_status`, `db.last_known_daily_date`,
     `plans.load_activities_by_date`, `plans.resolve_grading_config`)
     already gain the `conn` param under
     1b — `_build_plan_section` just needs to open its own single
     connection at entry and pass it through those same five calls, no
     new plumbing beyond what 1b already adds to the shared functions.
     Included in the fix scope (not silently dropped) but **lower
     priority than 1a/1b**: `generate_brief_report` is a stdio-only,
     on-demand PDF-render tool (`agent/tools.py`'s `LOCAL_ONLY_TOOLS`),
     not the unattended 06:30 hot path or a frequently-called ad-hoc
     query — so this fix is cheap (reuses 1b's threaded functions
     as-is) and gets done, but isn't where the benchmark/CI-gate
     pressure concentrates.
   - **Honest target, not an inflated fix-list with no payoff**: fully
     threading 1a lets `assemble_brief_context` open **1** connection at
     its own entry point and pass it through every sub-call including
     the final batched block. Fully threading 1b lets
     `get_training_plan_status` and `get_training_plan_progress` each
     open **1** connection at their own entry point. Fully threading 1c
     lets `_build_plan_section` open **1** connection at its own entry
     point. This "1 connection" target is genuinely achievable only
     because `config._resolve` and `plans._get_by_status` are both
     included in 1a's and 1b's function lists above — without threading
     `conn` through `_resolve` too, `coach_profile`/`riegel_lookback_days`
     would still open a second, untracked connection underneath the
     threaded public functions, and without threading `conn` through
     `_get_by_status` too, `get_active_plan` would do the same — and the
     real floor would stay 2, not 1. The "≤2 connections" testable
     invariant is revised to account for these as three separate
     functions with their own counts, not one shared "≤2" — see
     Invariants.
   - **No read-consistency semantics change results from this refactor**:
     threading a single shared connection through
     `assemble_brief_context`'s entire read chain does **not** change
     read consistency, correcting an earlier pass through this design
     that claimed otherwise. Python's `sqlite3` module, at the default
     `isolation_level`, never implicitly opens a transaction before a
     `SELECT` — each read on the shared connection still sees whatever
     is latest-committed at its own call instant, exactly as it would
     on separate connections. There is no "one snapshot for the whole
     assembly" behavior and nothing here for a concurrent `fitness pull`
     to interact with differently than before; this is a factual
     correction, not a tradeoff to weigh or an open question.
   - **`db.connect()`, not `db.connect_readonly()` — a deliberate choice,
     not an oversight**: all three chains (1a/1b/1c) are pure reads, so
     `connect_readonly()` is the naming-obvious fit, but this design's
     own reasoning already establishes (above, "Every `connect()`...")
     that the two write PRAGMAs `connect()` re-executes are cheap once
     WAL is active — PRAGMA cost isn't the thing this fix is chasing.
     Threading `conn` through means the caller who opens the shared
     connection is the one who decides which opener to use; staying on
     `db.connect()` avoids introducing a second code path (and a second
     set of connection semantics to keep in sync) for a cost that's
     already been shown immaterial. Stated explicitly here so it reads
     as a considered non-issue rather than a gap.
2. **Collapse `_metric_rows`'s 3-query loop into 1 query** selecting all
   three `_TREND_METRICS` columns over the shared window in a single
   `SELECT`. **Implementation detail that must survive the collapse**:
   today each metric's own query filters `WHERE {metric} IS NOT NULL`
   (`status.py:135`), so each metric's trend series only ever includes
   dates where *that* metric is non-null — a joined single-`SELECT`
   returns one row per date with each column independently nullable, so
   the per-metric null-exclusion has to be reproduced by filtering per
   column in Python after the combined fetch (build each metric's series
   by dropping that column's `None`s from the shared row set), not by
   dropping a whole date row just because one of the three columns is
   null that day.
3. **Bound `_compute_signals`'s activities query** to a fixed lookback
   (35 days — covers the 28-day windows plus slack) instead of unbounded.
   **Accepted tradeoff**: this changes `days_since_last_run`
   (`brief_planner.py:507-510`) for stale accounts — if the last run
   predates the 35-day window, the bounded `runs` list is empty and
   `days_since_last_run` becomes `None` instead of the true (larger) day
   count. This is a real, silent semantic loss for that one field, not a
   performance-neutral bound. Accepted because: (a) `long_run_absence`
   already treats `None` as "≥5 days," so the signal that actually drives
   coaching behavior (`_workout_tone`'s conditioning) still fires
   correctly; (b) past 35 days without a run, the exact day-count stops
   being the useful thing to tell Nate — "it's been over a month" carries
   the same coaching weight as "it's been 47 days" would, so losing
   precision on an already-extreme value is a reasonable trade for
   bounding an unbounded scan. See Testing Strategy for the regression
   test this requires. **Same tradeoff class, second-order**: `recent_te`
   (`brief_planner.py:515`, a tuple of the last 5 `aerobic_te` readings)
   is built from this same bounded query — for an infrequent runner
   whose last 5 runs span more than 35 days, the bounded query can
   return fewer than 5 TE readings where the unbounded query previously
   found exactly 5, changing the input set `te_collapsing()`/
   `conditioning_fires()` grade against. Given Nate's actual running
   cadence this is unlikely to matter in practice, and it's the same
   low-risk/acceptable shape as the `days_since_last_run` tradeoff
   above — noted here rather than treated as a separate open question.
4. ~~Verify/add `PRAGMA busy_timeout`~~ — **considered and ruled out, not
   a fix.** Empirically verified: `PRAGMA busy_timeout;` already returns
   `5000` on both `db.connect()` and `db.connect_readonly()` today, with
   zero code changes — Python's `sqlite3.connect()` defaults its
   `timeout` parameter to `5.0`, which `db.py` never overrides, and
   which the stdlib already applies via `sqlite3_busy_timeout()`
   internally. There is no absent-busy_timeout problem in this codebase
   to fix, so this is not carried forward as an actionable fix or a
   testable invariant.

### Eval-proof methodology (the hard requirement)

The existing prompt-quality scorer (`tests/evals/` + `capture_baseline.py`)
measures generation grounding, not latency — this needs a **new, separate**
harness, same convention, different axis:

- **`pytest-benchmark` is not currently a dependency** (verified against
  `pyproject.toml`/`uv.lock`) — add it to the `dev` dependency group as
  part of this change, same place `pytest`/`pytest-cov`/`ruff` already
  live. Adding the dependency alone is not sufficient: `pyproject.toml`'s
  `addopts` also needs `--benchmark-skip` added in the same change, or
  `tests/test_perf_benchmarks.py` runs its full timed suite on every
  ordinary `pytest` invocation the moment the file exists — see the
  concrete mechanism and rejected alternatives below.
- **`pytest-benchmark`**, wrapping `assemble_brief_context`,
  `get_training_plan_progress`, `get_training_plan_status`,
  `_build_plan_section`, and `daily_snapshot`/`assemble_status()` as
  benchmark targets.
- Run against a **realistic-scale fixture DB** (multi-year history), not
  the small existing eval fixtures — fix #3's bug is invisible at small
  scale by construction, so the eval must include a fixture large enough
  to make the before/after delta real. **This fixture must be
  synthetically generated** — a small script fabricating N years of
  plausible `activities`/`daily_metrics` rows (randomized but
  realistic-shaped: run cadence, sleep/HR ranges, etc.) — **never**
  dumped or trimmed from `data/fitness.db`. Per CLAUDE.md: "Never commit
  fixtures derived from real data; if you need a fixture, fabricate it."
  Nate's real multi-year Garmin history is exactly the personal data
  that rule exists to keep out of the public repo. **The synthetic
  fixture must include an active training plan.** 1a's 9-connection
  count (and 1b/1c's 6/4 counts) only hold when a plan is active — an
  early `{"active": False}`/`None` return short-circuits before most of
  the chain even runs (6 connections for 1a, 1 for 1b, 1 for 1c). If
  the fixture has no active plan, the committed pre-fix baseline number
  would silently reflect the wrong (early-return) path instead of the
  one this design's connection counts are based on.
- **Baseline must be captured against pre-fix code, as its own step,
  before fixes 1-3 land** — otherwise the CI gate only proves "no
  regression from here forward," not that the claimed ~9/~6-connection
  and unbounded-query problems were real or that the fixes helped.
  Sequence: (1) land the benchmark harness + connection-count
  instrumentation against **today's unfixed code**, (2) run it **in CI,
  on the same `ubuntu-latest`/Python-3.12 runner `validate` uses — not
  locally on Nate's Mac** (mechanism and rationale below) — and commit
  the resulting baseline, capturing the pre-fix
  numbers — e.g. ~9 connections for `assemble_brief_context`, ~6 for
  `get_training_plan_progress`, 4 for `_build_plan_section`, an
  unbounded row count for `_compute_signals` on the synthetic fixture —
  (3) *then* apply fixes 1-3, (4) re-run the same benchmark and show it
  beats the committed baseline by the gated margin. This ordering is
  what makes "proven, not eyeballed" actually true, rather than
  assumed.
- **The squash-only merge policy destroys step-level commit separation
  — the proof mechanism has to survive that, not rely on it.**
  CLAUDE.md's *Branching & release strategy* mandates squash-only PRs
  into `dev`; whatever granular commit sequence exists locally while
  doing steps (1)-(4) above collapses into a single commit the moment
  the PR merges, leaving one commit with baseline-capture and fixes
  intermixed — "commit the baseline as its own small commit before the
  fix lands" cannot be the durable evidence. Two things carry the
  before/after proof past the squash instead: (a) **the PR description
  itself pastes the actual pre-fix and post-fix benchmark numbers**
  (connection counts and `min` timings for each target) side by side,
  so the proof lives in PR history (preserved by GitHub regardless of
  squash) rather than in commit granularity; (b) **a comment block in
  `tests/test_perf_benchmarks.py`** documents the pre-fix numbers
  observed on the synthetic fixture (e.g. `# pre-fix baseline (measured
  YYYY-MM-DD before Part A fix #1): assemble_brief_context ~9
  connections, get_training_plan_progress ~6, _build_plan_section ~4`)
  so the claim is checked into the file under test, not dependent on
  git archaeology through a squashed history. The committed
  `.benchmarks/Linux-CPython-3.12-64bit/0001_*.json` (mechanism below)
  is the machine-checked half of this; the PR description and code
  comment are the human-readable half — together they make the pre/post
  claim verifiable without needing separable commits.
- **Comparison mechanism (concrete, not left as an open choice between
  a bespoke JSON and pytest-benchmark's built-in comparison)**: use
  pytest-benchmark's own storage/compare machinery, not a hand-rolled
  `perf_baseline.json` alongside `capture_baseline.py`'s convention —
  the two don't compose (`--benchmark-compare-fail` reads from
  pytest-benchmark's own `.benchmarks/` storage tree via
  `--benchmark-compare`, not an arbitrary hand-named file).
  - **Machine-id constraint (verified against the installed
    `pytest_benchmark` package, not assumed)**: `pytest-benchmark`'s
    `FileStorage` always nests every save under a machine-id directory —
    `get_machine_id()` returns
    `platform.system()-platform.python_implementation()-pyver-arch`,
    e.g. `Darwin-CPython-3.12-64bit` on Nate's Mac vs.
    `Linux-CPython-3.12-64bit` on GitHub's `ubuntu-latest` (CI's Python
    version is pinned to 3.12 in `ci.yml`). `FileStorage.query()`'s
    single-segment-glob branch — which is exactly what
    `--benchmark-compare=0001` hits, since `0001` is one path segment —
    resolves to searching **only the current run's own machine-id
    subdirectory**; there is no wildcard or cross-machine-id fallback.
    `check_regressions()` then raises `pytest.UsageError` (a hard CI
    failure, not a warning) when `--benchmark-compare-fail` is set and
    no matching save is found. So capturing the baseline on the wrong
    machine doesn't just produce a *less accurate* comparison — it makes
    every subsequent `validate` run fail outright with "no benchmark
    found to compare", because `ubuntu-latest` will never find a
    `Linux-CPython-3.12-64bit/0001_*` file if the only save committed is
    under `Darwin-CPython-3.12-64bit/`.
  - **Every `pytest tests/test_perf_benchmarks.py` invocation below
    appends `--no-cov`, and that's load-bearing, not cosmetic.**
    `pyproject.toml`'s `[tool.pytest.ini_options]` sets `addopts =
    "--cov=local_fitness --cov=scripts --cov-report=term-missing
    --cov-fail-under=85"`, which applies to every pytest invocation
    regardless of which test path is passed on the command line — a
    narrow benchmark-only slice can't cover 85% of the whole codebase
    and would fail the build on the coverage gate alone, independent of
    whether the benchmarks themselves pass. (Reproduced empirically:
    even running an existing, fully-passing file in isolation, e.g. `uv
    run pytest tests/test_brief_planner.py -q`, fails with "Required
    test coverage of 85% not reached.") `--no-cov` is pytest-cov's flag
    to disable coverage collection for that invocation, overriding the
    `addopts`-level coverage flags; these benchmark-only runs aren't
    meant to exercise or prove coverage of the whole codebase, so the
    gate doesn't apply to them.
  - **`tests/test_perf_benchmarks.py` must not run its full timed suite
    on an ordinary, flag-less `pytest` invocation — this needs an
    explicit default-collection fix, not just careful wording on the
    explicit benchmark commands.** `pyproject.toml`'s `testpaths =
    ["tests"]` sweeps in every `test_*.py` under `tests/` on any bare
    invocation, and the repo registers zero pytest markers today (no
    `[tool.pytest.ini_options]` `markers` list, no `conftest.py`
    collection hook) — so with nothing added, both CLAUDE.md's mandated
    `uv run pytest -x` and this design's own closing "Testing strategy"
    bullet, *and* `ci.yml`'s existing `validate` step (`uv run pytest
    --cov-report=xml`, unmodified by this design, no `--benchmark-only`),
    would execute the full multi-round timed suite against the
    realistic-scale synthetic fixture on every single run, forever —
    twice per CI trigger once the separate benchmark-compare step is
    also added.
    - **Mechanism (verified against the installed `pytest_benchmark`
      package, not assumed): add `--benchmark-skip` to `pyproject.toml`'s
      `addopts`.** `pytest-benchmark` auto-detects, per test item, whether
      it uses the `benchmark` fixture (`'benchmark' in
      item.fixturenames`, in its `pytest_collection_modifyitems` hook) —
      no marker or decorator is needed on `test_perf_benchmarks.py`'s
      functions for this to work. When `--benchmark-skip` is active, any
      test using that fixture gets `pytest.mark.skip` added at
      collection time and never executes at all (not even once) —
      confirmed empirically: a bare `pytest --benchmark-skip` run against
      a fixture-using test reports `1 skipped` in `0.00s`, versus the
      same test running 34 full timed rounds with no flags at all.
      Critically, **this composes with the explicit `--benchmark-only`
      commands below with zero changes needed to those commands**:
      `pytest-benchmark`'s own session setup
      (`pytest_benchmark/session.py`) contains `if self.skip and
      self.only: self.skip = False` — passing `--benchmark-only` on the
      command line unconditionally cancels the `addopts`-level
      `--benchmark-skip`, and instead skips every test that does *not*
      use the `benchmark` fixture, which is exactly `--benchmark-only`'s
      documented behavior. Confirmed empirically: running
      `--benchmark-skip --benchmark-only` together (simulating
      `addopts`'s default plus one of this design's explicit commands)
      executes the benchmark test with its full 34 rounds and skips the
      non-benchmark test instead — identical to running `--benchmark-only`
      alone. **This is why none of the three explicit
      `--benchmark-only ... --no-cov` commands elsewhere in this section
      (baseline capture, self-check compare, and the recurring CI
      compare) need any additional flag** — only `pyproject.toml`'s
      `addopts` changes.
    - **Rejected alternative: `--benchmark-disable` in `addopts`.**
      Verified against `pytest_benchmark/session.py`: `self.disabled =
      config.getoption('benchmark_disable') and not
      config.getoption('benchmark_enable')`, and separately `if
      self.disabled and self.only: raise pytest.UsageError("Can't have
      both --benchmark-only and --benchmark-disable options.")`. Because
      `--benchmark-only` alone does *not* clear `--benchmark-disable`
      (only the distinct `--benchmark-enable` flag does), defaulting to
      `--benchmark-disable` would make every explicit `--benchmark-only`
      command in this section hard-fail with that `UsageError` unless
      `--benchmark-enable` were also added to each one — more moving
      parts, and a wrong/missing flag fails loudly and blocks CI, whereas
      `--benchmark-skip` composes for free. Not used.
    - **Rejected alternative: a custom `benchmark` pytest marker +
      `-m "not benchmark"` in `addopts`.** `pytest-benchmark` already
      registers a `benchmark` marker itself (`config.addinivalue_line
      ('markers', 'benchmark: mark a test with custom benchmark
      settings.')`), but that marker is opt-in metadata for per-test
      settings (e.g. `@pytest.mark.benchmark(group=...)`), not something
      auto-applied to every test that merely uses the `benchmark`
      fixture — a marker-based exclusion would require decorating every
      function in `test_perf_benchmarks.py` by hand and would silently
      stop working the moment someone added a new benchmark test and
      forgot the decorator. `--benchmark-skip`'s fixture-based detection
      has no such gap: it inspects `item.fixturenames` directly, so any
      test using `benchmark` is covered automatically. Not used.
    - **`pyproject.toml` diff, concrete**: `addopts` becomes
      `"--cov=local_fitness --cov=scripts --cov-report=term-missing
      --cov-fail-under=85 --benchmark-skip"`. No `markers` entry is
      needed (per the rejected alternative above), and no
      `conftest.py` change is needed.
    - **Every place in this document that describes running the default
      test suite stays accurate with no further change**, precisely
      because the fix lives in `addopts` rather than in wording at each
      call site: CLAUDE.md's `uv run pytest -x`, this design's own
      closing "Testing strategy" bullet (`Full uv run pytest -x ...`
      below), and `ci.yml`'s existing `validate` step all pick up
      `--benchmark-skip` automatically and skip the benchmark suite with
      no per-command edits; the three explicit `--benchmark-only`
      commands in this section stay exactly as written above and
      continue to run the full timed suite as designed.
  - **Resolution: the pre-fix baseline is captured inside CI, on the
    same `ubuntu-latest`/Python-3.12 runner `validate` uses — never
    locally on Nate's Mac.** This makes the machine-id match by
    construction instead of by convention:
    1. Add a one-off `workflow_dispatch` job (e.g.
       `.github/workflows/capture-perf-baseline.yml`), `runs-on:
       ubuntu-latest`, with the same `astral-sh/setup-uv` + `uv python
       install 3.12` + `uv sync --dev` steps `validate` already uses in
       `ci.yml`. Its one step runs:
       `uv run pytest tests/test_perf_benchmarks.py --benchmark-only
       --benchmark-storage=file://./.benchmarks --benchmark-autosave
       --no-cov`, then uploads the resulting directory via `actions/upload-artifact`
       with **`path: .benchmarks/`** (the directory, not a glob on the
       json file inside it) — this is what preserves the machine-id
       subdirectory nesting (`.benchmarks/Linux-CPython-3.12-64bit/...`)
       in the downloaded zip; uploading a narrower/globbed path would
       flatten that nesting and reproduce the exact "wrong directory
       committed" failure this whole mechanism exists to avoid.
    2. Trigger that workflow once, by hand from the Actions tab, on the
       harness-only commit/branch from step (1) above — i.e. before
       fixes 1-3 land, so the captured numbers are genuinely pre-fix.
    3. Download the artifact. Once unzipped it contains
       `.benchmarks/Linux-CPython-3.12-64bit/0001_<commit-hash>.json`.
       **Commit that file to the repo at that exact path** (not
       `.gitignore`d, and not moved/flattened), in the same PR that adds
       the harness — this is the pre-fix baseline artifact, produced by
       pytest-benchmark itself under the exact machine-id directory
       `validate`'s `ubuntu-latest` runner will look for on every future
       run. **Self-check before considering this step done**: either run
       `uv run pytest tests/test_perf_benchmarks.py --benchmark-only
       --benchmark-storage=file://./.benchmarks --benchmark-compare=0001
       --no-cov`
       locally against the just-placed file to confirm pytest-benchmark
       finds and loads it as save `0001` (a `pytest.UsageError: Could not
       find benchmark file` means the file landed in the wrong
       subdirectory), or simply trust that this same PR's own CI
       `validate` run exercises the identical compare command on
       `ubuntu-latest` and will fail loudly, before merge, if the file is
       misplaced — either way, don't consider the manual step complete
       until one of those two checks has actually run.
    4. The one-off workflow is a bootstrap tool, not a recurring job —
       delete it once the baseline is committed, or leave it in place as
       a documented "recapture the baseline" utility for if the
       benchmark suite is ever intentionally rebaselined; either is
       fine, neither is load-bearing for correctness. **Rebaselining
       note for whoever eventually uses it**: `FileStorage`'s `_next_num`
       allocates the next free number, so simply re-running this
       workflow a second time saves `0002_*.json` alongside `0001_*.json`
       rather than overwriting it — a real rebaseline needs the old
       `0001_*.json` deleted by hand, the new save renamed/placed as
       `0001_*.json` (or `ci.yml`'s hardcoded `--benchmark-compare=0001`
       bumped to match whatever number actually lands), and the stale
       `0001` file's removal committed in the same PR.
  - **Acknowledged, not mitigated**: this resolution assumes
    `ubuntu-latest`'s architecture (currently `64bit`/x86_64) stays
    constant. A future GitHub-side runner architecture migration (e.g. an
    arm64 default) would silently reintroduce the same machine-id
    mismatch class this section exists to solve — no monitoring is
    proposed for that here; it's a low-probability, distant risk noted
    for awareness, not designed around.
  - **Compare (every CI run thereafter, step (4) and all future PRs)**:
    runs as part of the regular `validate` job (already `ubuntu-latest`
    / Python 3.12 — the same machine-id the baseline was captured
    under):
    `uv run pytest tests/test_perf_benchmarks.py --benchmark-only
    --benchmark-storage=file://./.benchmarks
    --benchmark-compare=0001 --benchmark-compare-fail=min:15% --no-cov`. With
    capture and compare both pinned to `ubuntu-latest`, the machine-id
    directories always line up, so pinning the explicit baseline id
    (`0001`, the committed pre-fix save) — rather than relying on "most
    recent save" — deterministically selects the pre-fix save *within*
    that shared machine-id directory, instead of resolving to whatever
    was captured most recently. Gating on `min` (not `mean`) is what
    absorbs the run-to-run scheduling/hardware noise between two
    different `ubuntu-latest` VM instances (still real, even with
    identical machine-id) — `min` is the least-perturbed-by-noise
    statistic per pytest-benchmark's own guidance, which is why it's the
    gated metric rather than `mean`.
  - If GitHub Actions' VM-to-VM variance turns out too noisy in practice
    for a 15% `min` gate even with matched machine-ids (a real risk that
    can only be confirmed once this lands and runs a few times), the
    fallback is `--benchmark-json=out.json` piped through a small
    comparison script mirroring `capture_baseline.py`'s existing
    percentage-delta-against-committed-numbers pattern — this sidesteps
    pytest-benchmark's machine-id storage model entirely, so it would
    also be the fallback if a future contributor ever needs to compare
    across genuinely different runner types. Noted here as the
    documented fallback, not left open as a live decision blocking
    implementation; the primary mechanism above is what ships first.
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

- **The entire `web/` directory**, not just `web/src/`: the React/Vite
  SPA source (`web/src/`, ~3,819 LOC) plus its build/tooling files —
  `package.json`, `vite.config.ts`, `tsconfig.json` /
  `tsconfig.app.json` / `tsconfig.node.json`, `eslint.config.js`,
  `index.html`, `pnpm-lock.yaml` — and its generated/build artifacts —
  `web/dist/`, `web/node_modules/`.
- **Every UI-only REST route in `web/server.py`**: `/api/today`,
  `/api/metric/{name}`, `/api/training-load`, `/api/workouts`,
  `/api/workout/{id}`, `/api/brief` (GET), `/api/sync` (POST),
  `/api/notes` (GET/POST/DELETE — already redundant with
  `list_user_notes`/`save_user_note`/`delete_user_note`), `/api/plan`
  (read), `/api/plan/draft`, `/api/plan/{id}/commit`, `DELETE
  /api/plan/{id}`, `/api/activity-heatmap`, `/api/strength-volume`,
  `/api/pace-efficiency`, `/api/status`, `/api/config`,
  `/api/auth/verify`, `/api/sync/status`.
- **The background-sync-orchestration subsystem that exists solely to
  back `/api/sync`/`/api/sync/status`** (`web/server.py`): the six
  private functions `_last_sync_run()`, `_sync_state_dict()`,
  `_is_transient()`, `_run_sync()`, `_schedule_retry()`,
  `_trigger_sync()` (:828-976 — lines 977-990 are the two route
  handlers themselves, already covered by the separate "every UI-only
  REST route" bullet above), the six module-level state vars
  `_sync_running`/`_sync_started_at`/`_sync_lock`/`_sync_task`/
  `_retry_task`/`_retry_count` (:79-87), and the three constants
  `SYNC_MAX_DAYS`/`SYNC_THROTTLE_SECONDS`/`SYNC_RETRY_BACKOFFS`
  (:68-76). None of these has any caller besides the two routes above
  and the ~15 `test_web_api.py` tests already being deleted — removing
  only the two route decorators and leaving this code in place would
  strand ~100+ lines of unreachable code and quietly flip it from
  covered to permanently uncovered (its only tests are the ones being
  deleted alongside it), undercutting the coverage-safety claim made
  elsewhere in this design. Also delete `lifespan()`'s
  `_retry_task`/`_sync_task` shutdown-cleanup block (:114-125), which
  would otherwise forever check module globals that can now only ever
  be `None`. `agent/tools.py:43`'s comment ("Mirrors web/server.py's
  SYNC_MAX_DAYS") becomes a dangling cross-reference to a constant that
  no longer exists — needs updating too (see Documentation updates);
  `agent/tools.py`'s own `SYNC_MAX_DAYS` (:46, used by
  `sync_garmin_data`) is unaffected and stays.
- **`_assemble_plan_detail()`** (`web/server.py:381-399`) — the helper
  that assembles the response body for `/api/plan` and `/api/plan/draft`
  (frontier date via `db.last_known_daily_date`, activities-by-date,
  `best_recent_effort`, grading config, the `build_plan_detail` call,
  plus a CTL-series lookup). Its only two callers are `api_plan()` and
  `api_plan_draft()` (`web/server.py:402-419`), both routes this design
  deletes in the bullet above, and its only test coverage goes with
  them — the same "dead code once its only callers are removed" pattern
  as the six sync-orchestration helpers just above; called out
  explicitly here rather than left as an implicit consequence of the
  route removal.
- **SPA static serving**: the `StaticFiles` mount and `spa_fallback`
  catch-all route — superseded by the more precise citation on the
  "`if WEB_DIST.exists(): ... else: ...` wrapper" bullet below
  (`web/server.py:1040-1075`), which fully covers this removal; not
  re-cited with its own open-ended range here.
- **Vite dev-server CORS middleware** (`web/server.py:130-136`) — the
  `CORSMiddleware` allowing `http://localhost:5173`/`127.0.0.1:5173`
  exists only to let the Vite dev server call the API cross-origin
  during frontend development; with no frontend and no dev server,
  remove it entirely.
- **`root_no_build()`** (`web/server.py:1068-1075`) — the fallback
  `GET /` handler that currently tells developers to `cd web && pnpm
  install && pnpm build`. Once `web/` no longer exists, that instruction
  is actively wrong, not just unused; remove the handler (there's no
  "frontend not built yet" state left to report once there's no
  frontend to build).
- **The `if WEB_DIST.exists(): ... else: ...` wrapper itself**
  (`web/server.py:1040-1075`) — removing both branches (the SPA
  `StaticFiles`/`spa_fallback`/`root` block on the `if` side and
  `root_no_build()` on the `else` side) leaves the surrounding
  conditional with nothing left to wrap; delete the `if`/`else`
  scaffolding too, not just its two bodies. That also makes the
  `WEB_DIST` existence check (`web/server.py:66`,
  `WEB_DIST = _PROJECT_ROOT / "web" / "dist"`) itself pointless — with
  no branch left that reads it, delete the `WEB_DIST` constant along
  with the block, not leave it as an unused module-level path.
  `_PROJECT_ROOT` (`web/server.py:65`,
  `_PROJECT_ROOT = Path(__file__).resolve().parents[3]`) has no other
  use besides feeding `WEB_DIST` at the next line — it becomes orphaned
  one hop further up and should be deleted alongside `WEB_DIST`, not
  left as a dangling module-level constant.
- **Note (decision needed, not auto-resolved by this design)**:
  `plans.delete_plan` loses its only caller once `DELETE
  /api/plan/{id}` is removed, becoming dead code. Keep it (for possible
  future delete-draft parity with `discard_draft`) or delete it outright
  — flagged here as an open call for whoever implements this, not
  decided by this design.
- **Docker's `web-builder` stage** (`Dockerfile:8-31` — the stage's last
  content line is `RUN pnpm build` at :31; :32-33 are two blank
  separator lines before the Stage 2 comment at :34) and its
  `COPY --from=web-builder` into the runtime stage.
- **CI's frontend job step**: `pnpm install`/`pnpm build`/`pnpm test`
  inside the `validate` job (`.github/workflows/ci.yml:60-91`) — the
  range starts at :60, not :68, to also remove the "Set up Node" step
  (:63-67, `actions/setup-node@v4` pinned to `node-version: 26`) and its
  preceding comment (:60-62, which references the Dockerfile
  web-builder stage); once every other frontend step is gone, nothing
  else in the `validate` job uses Node/pnpm, so leaving that step behind
  would orphan it. The `docker-build` job's web-builder-stage coverage
  shrinks accordingly (still compiles the whole image, just without the
  web-builder Node stage — the runtime stage keeps its own independent
  Node.js/npm install for the Claude Agent SDK's `claude` CLI shell-out,
  unaffected by this removal).
- **`.github/dependabot.yml`'s npm/web entry** (:16-26) — the
  `package-ecosystem: "npm"`, `directory: "/web"` block exists only to
  scan `web/package.json`; once `web/` is deleted there's nothing left
  for it to scan. Delete the entire block (comment line included), not
  just the `directory` value — leaving it behind would silently degrade
  CLAUDE.md's "CI dep scanning" posture to a dependabot entry that
  errors or no-ops against a missing manifest on every weekly run. The
  `pip`, `docker`, and `github-actions` entries are unaffected.
- **`.github/workflows/codeql.yml`'s `javascript-typescript` matrix
  leg** (`strategy.matrix.language`, currently `[python,
  javascript-typescript]` at :27) — once `web/` is deleted this leg
  scans zero source files on every push/PR/weekly cron run, either
  burning CI minutes on a permanent no-op or starting to fail at
  Autobuild/Analyze for a language with nothing to build. Remove
  `javascript-typescript` from the matrix, keeping only `python`. Also
  update the file's header comment (:3, "Static security analysis over
  the Python backend + the React/TS frontend") to drop the "+ the
  React/TS frontend" half — it becomes a stale claim the moment the
  matrix leg is gone.

### What stays

- `web/server.py` itself — it hosts the authenticated MCP
  streamable-HTTP transport at `/mcp/`, used by any network-connected
  MCP client (not just historically the UI). The bearer-auth middleware,
  `/health`, and the `/mcp/` mount are unaffected.
- Every existing MCP tool. "Keep and improve the API" = the MCP tool
  surface is the API now; Part A is that improvement.

### Decision point: tighten `_is_public_path` to deny-by-default

`_is_public_path` (`web/server.py:151-170`) currently defaults to
**public** for anything not under `/api/` or `/mcp/` — justified today
by needing to serve the SPA shell and its static assets without a
Bearer token (an unauthenticated browser tab has to load *something*
before it can even prompt for the token). Once the SPA is gone, that
justification disappears, and CLAUDE.md's own security convention argues
for flipping the default to **deny** — its *Security defaults that are
non-negotiable* section says a new public endpoint should "whitelist it
explicitly in `_is_public_path()`, not by sneaking it outside the
prefix" (quoted verbatim; the general principle is explicit whitelist,
not blanket-public). Only
`/health` stays explicitly whitelisted (the `/mcp/` mount already has
its own gate); everything else is auth-gated by default. **Decided:
flip it** as part of this PR — an explicit sub-task to change
`_is_public_path`'s fallthrough from `return True` to `return False`,
plus a new `tests/test_security.py` case asserting an arbitrary unknown
path (e.g. `/foo`) now returns 401 without a token, where today it
would return 200. **Now-redundant dead code once the fallthrough
flips**: the explicit `if lowered == "/mcp" or lowered.startswith("/mcp/"):
return False` block (`web/server.py:163-167`) exists today only to
override the old `return True` fallthrough for non-`/api/` paths —
once that fallthrough itself returns `False` by default, every path
(including `/mcp*`) already denies without it. Harmless to leave (it's
correct, just now a no-op), but worth flagging alongside this PR's
other dead-code cleanup so it doesn't linger unexplained.

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
(`plans.py:436-437`) — no new exception type needed, but this is a new
**read**-adjacent write-path use of an exception that today is raised
only by a different write path: `update_active_workout`
(`plans.py:539`) is the sole existing raise site, and
`get_training_plan_status`/`get_training_plan_progress` never raise
it — they detect "no active plan" by `plans.get_active_plan()`
returning `None` and returning `{"active": False}`. There is no prior
precedent of this exception being used by a read/status lookup;
`abandon_active_plan` introduces the second raise site because it is
itself a write (an `UPDATE ... RETURNING`), and raising on "nothing to
update" matches `update_active_workout`'s existing write-path
convention rather than the read-path `None`-check convention.

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

**Stylistic departure, noted not fixed**: this is the first use of
SQLite's `UPDATE ... RETURNING` clause anywhere in this codebase.
`plans.py`'s other status-flip functions split across two different
existing patterns, neither of which is `RETURNING`: `delete_plan`
(`plans.py:577-584`) and the planned `discard_draft` use the
`cur.rowcount == 0` check (an unconditional-but-guarded `UPDATE ...
WHERE plan_id=? [AND status=...]`, then a rowcount check to detect "no
matching row"). `commit_plan` (`plans.py:556-574`) uses neither
pattern — it does a `SELECT status` first, raises if the plan isn't a
draft, then issues two unconditional `UPDATE`s with no rowcount check
at all (see the interleaving-race analysis below, which already relies
on exactly this mechanism: "`commit_plan`'s initial `SELECT status`
... is a read and does not hold SQLite's write lock"). SQLite added
`RETURNING` support in 3.35.0 (2021); the codebase's target runtimes
are well past that, so this is verified fine on the SQLite versions in
play, not a compatibility risk — just a one-off stylistic choice worth
a reader's awareness rather than a blocking concern.

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

#### Failure modes / edge cases (`abandon_active_plan`)

The prior 2026-07-06 design has a dedicated "Failure modes / edge cases"
section for `commit_training_plan`/`discard_training_plan_draft` covering
concurrent-commit races, lock-contention timeouts, and uncaught exception
types. `abandon_active_plan` gets the same treatment here — this tool is
explicitly the bigger-blast-radius one ("No undo tool exists for this"),
so it earns no less scrutiny than the two the prior design already
analyzed:

- **No active plan** → `NoActivePlanError` → `_err`, no mutation (the
  `RETURNING plan_id` clause returning no row is itself the atomic
  check — no separate SELECT-then-write, so there's no window for this
  case to race with anything).
- **Interleaving with `commit_training_plan`'s own unguarded
  SELECT-then-write** (`plans.py:556-574` — `commit_plan`'s final
  activating `UPDATE ... SET status='active' WHERE plan_id=?` carries no
  `WHERE status='draft'` guard, only a `plan_id` match). `commit_plan`'s
  initial `SELECT status FROM training_plans WHERE plan_id=?` is a read
  and does not hold SQLite's write lock, so there is a real window
  between that `SELECT` and `commit_plan`'s first `UPDATE` in which a
  concurrent `abandon_active_plan` can run to completion. Concretely:
  if `abandon_active_plan` archives the currently-active plan in that
  window, `commit_plan`'s own `UPDATE ... WHERE status='active'` (which
  archives "whatever is active" unconditionally, not by `plan_id`)
  simply matches zero rows and no-ops, and `commit_plan` still activates
  its target draft as intended — this ordering is benign, no
  double-archive, no corrupted state. The only user-visible surprise is
  UX-level, not data-level: a caller who invoked `abandon_active_plan`
  expecting "no plan active" can be immediately followed by a
  concurrent `commit_training_plan` activating a different plan anyway
  — a race in *intent*, not in the data. Once `commit_plan` executes its
  first `UPDATE`, it holds SQLite's write lock through both of its
  `UPDATE`s until it commits at the end of its `with db.connect()`
  block (per `db.connect`'s commit-on-exit semantics, `db.py:206-219`),
  so `abandon_active_plan` cannot interleave *between* `commit_plan`'s
  two `UPDATE`s — only before the first one, the benign ordering above.
  Not addressed further — negligible risk given the same
  cross-surface-race framing the prior design used (a browser tab plus
  an agent session, or two tool calls landing in the same LLM turn, not
  literally two simultaneous human operators).
- **Lock-contention timeout** → if `abandon_active_plan`'s `UPDATE`
  attempts to acquire SQLite's write lock while `commit_plan` (or any
  other writer) already holds it, it blocks up to the connection's
  `busy_timeout` (already 5000ms by default on both `connect()` and
  `connect_readonly()` — see Part A's ruled-out fix #4 note above). If
  contention outlasts that window,
  `abandon_active_plan` raises a raw `sqlite3.OperationalError: database
  is locked` — **uncaught**: `abandon_active_plan_tool`'s `except
  NoActivePlanError` clause does not catch `sqlite3.OperationalError`,
  so it propagates out of the tool call. Same accepted-risk treatment as
  the prior design's identical finding for `commit_training_plan`/
  `discard_training_plan_draft` — not solved here, documented as a
  residual risk given the same low-realistic-likelihood trigger (this is
  a single-user personal app; true concurrent writers require two
  overlapping tool calls or a stray REST caller, not routine usage).

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
| `assemble_brief_context`, `resolve_coach_profile`, `config.coach_profile`, `config._resolve`, `resolve_grading_config`, `_plan_today`, `plans.get_active_plan`, `plans._get_by_status`, `db.last_known_daily_date`, `plans.load_activities_by_date`, `db.get_setting`, `db.all_settings` | internal | all gain an optional `conn: sqlite3.Connection \| None = None` param; behavior unchanged when omitted. This is chain 1a (`assemble_brief_context`'s) — see Part A fix #1. `plans._get_by_status(status: str, db_path: Path \| None = None, conn: sqlite3.Connection \| None = None) -> dict \| None` is the actual connection-opening helper `get_active_plan` delegates to |
| `get_training_plan_status`, `get_training_plan_progress`, `config.riegel_lookback_days`, `config._resolve`, `plans.best_recent_effort` | MCP tool / internal | gain (or thread through) an optional `conn` param; a **separate** call chain from 1a (chain 1b) — these two tools never call `assemble_brief_context`/`_plan_today` — see Part A fix #1 |
| `_build_plan_section` | internal | gains (or threads through) an optional `conn` param, reusing the same `conn`-aware `plans.get_active_plan`/`db.last_known_daily_date`/`plans.load_activities_by_date`/`plans.resolve_grading_config` that 1b already threads; a **third, separate** call chain (chain 1c), used only by `generate_brief_report`'s PDF pipeline — see Part A fix #1 |
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
- `assemble_brief_context` opens ≤2 connections per call (target: 1;
  ≤2 leaves slack in case the final batched block stays a distinct
  `connect()` in practice). `get_training_plan_status` and
  `get_training_plan_progress` are accounted **separately** — they are
  an independent call chain (1b), not a subset of 1a — each opens ≤2
  connections per call, down from today's 4 and 6 respectively.
  `_build_plan_section` is accounted separately again (chain 1c, not a
  subset of 1a or 1b) — opens ≤2 connections per call, down from
  today's 4. Four separate monkeypatched counter assertions, one per
  function.
- `_metric_rows` issues exactly 1 query for the 3 trend metrics (not 3),
  **and** per-metric trend-arrow correctness survives the collapse on
  sparse data — a fixture where each of the 3 trend metrics is null on a
  different subset of dates within the window must still produce the
  same per-metric series (and the same `arrow`) as today's per-metric
  `IS NOT NULL` queries, not a series shortened by another column's null.
- `_compute_signals` never queries `activities` rows older than the
  bounded lookback (assert on the SQL params or on returned row dates in
  a fixture with data older than the bound).
- `_compute_signals`/`assemble_brief_context` return `days_since_last_run
  is None` (not a stale count, not a crash) for a fixture whose only run
  predates the 35-day bound — the accepted-tradeoff case from Part A
  fix #3.
- `_is_public_path("/foo")` (or any arbitrary unknown path) returns
  `False` post-fix, where it returns `True` today.
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
  `pytest-benchmark` suite from Part A, run against a realistic-scale,
  **synthetically generated** fixture DB checked into `tests/fixtures/`
  (or generated by a `scripts/`-level fixture-builder, mirroring how
  `capture_baseline.py` already builds eval fixtures) — never derived
  from `data/fitness.db`. Includes committing the pre-fix baseline as
  its own step, per the Eval-proof methodology sequencing above.
- New test case: `_compute_signals`/`assemble_brief_context` against a
  fixture containing a run older than the 35-day bound and no runs
  inside it — asserts `days_since_last_run is None` (not a crash, not a
  stale/wrong count), covering the accepted tradeoff from Part A fix #3.
- Extend `tests/test_plans_db.py` with `discard_draft` and
  `abandon_active_plan` persistence-layer cases (mirrors the existing
  `test_commit_*`/`test_delete_*` style).
- Extend `tests/test_plan_tools.py` with tool-layer cases for all three
  new tools (success + every error path + wrong-type `plan_id` rejection
  for the two that take one).
- Delete `tests/test_web_plan.py` (`/api/plan`, `/api/plan/draft`,
  `/api/plan/{id}/commit`, `DELETE /api/plan/{id}`), `tests/test_web_api.py`
  (~47 tests covering `/api/today`, `/api/metric/{name}`,
  `/api/training-load`, `/api/workouts`, `/api/workout/{id}`,
  `/api/activity-heatmap`, `/api/strength-volume`, `/api/pace-efficiency`,
  `/api/status`, `/api/config`, `/api/auth/verify`, `/api/sync`,
  `/api/sync/status`, `/api/notes` — also including
  `test_plan_empty_and_404s` (`tests/test_web_api.py:675-679`), which
  hits `/api/plan`, `/api/plan/draft`, `/api/plan/9999/commit`; deleting
  the whole file already removes it, this is a completeness note, not a
  behavior change), and `tests/test_web_brief.py`
  (`GET /api/brief`)'s coverage of removed REST routes; keep/adapt
  whatever in any of the three still exercises `/health`, `/mcp/`, and
  the auth middleware itself. Run implemented literally, `uv run pytest
  -x` would fail on dozens of tests hitting deleted endpoints without
  this step.
  - **Explicit call on `test_web_api.py`'s two auth-verify tests, not
    left as implementer judgment**: `test_auth_verify_open_when_no_token`
    (asserts `{"ok": True, "auth_required": False}` from `/api/auth/verify`
    with no token set) and `test_auth_accepts_valid_token` (asserts
    `{"ok": True, "auth_required": True}` from the same route with a
    valid bearer token) both target the specific response body of a
    route Part B deletes. **Rewrite, don't delete**: point both at
    `/mcp/` and assert on the *auth-middleware* behavior each test is
    really pinning — "no token configured → request proceeds
    unauthenticated," "valid bearer token → request is accepted" —
    rather than the removed route's specific JSON shape. `/health` is
    **not** a valid target for either rewrite: the Invariants above
    guarantee `/health` stays explicitly whitelisted (public regardless
    of token state), so it can never exercise "valid token accepted" or
    meaningfully distinguish "no token configured" from "route is just
    always public." `/mcp/` is the only generic authed endpoint that
    survives. Testing against `/mcp/` requires the ASGI lifespan
    running (`async with _MCP_MANAGER.run()`) — `test_security.py`'s
    existing `app_with_token`/`app_no_token` fixtures use a bare
    `httpx.ASGITransport` with no lifespan setup, which is sufficient
    for the existing 401-only checks (the middleware short-circuits
    before the mount ever runs) but, on an actual `/mcp/` call that
    reaches the mount, doesn't produce an HTTP 500 response — with
    `ASGITransport`'s default `raise_app_exceptions=True`, the
    "Task group is not initialized" `RuntimeError` propagates unhandled
    out of the test coroutine instead, so a naive
    `assert r.status_code == 500` would itself error, not fail an
    assertion. Follow `tests/test_mcp_server.py:277-345`'s working
    pattern instead — `TestClient` used as a context manager (which
    drives the lifespan) — not the bare `ASGITransport` fixtures. That
    middleware behavior is still live and still worth a regression test;
    only the route these two tests happen to exercise today, and the
    fixture pattern used to reach it, need to change.
  - **The same salvage question applies to five more tests in
    `test_web_api.py` that the two above don't cover** — their own
    docstrings/comments mark them as tests of the auth middleware itself,
    not of a specific route's business logic, so leaving them unnamed
    would be the same "stop and ask" gap the two auth-verify tests above
    were called out to avoid:
    - **`test_auth_rejects_without_bearer`** and
      **`test_auth_rejects_wrong_token`** both hit `/api/today` (deleted).
      Because the bearer middleware gates by path prefix ahead of
      routing, both would keep returning 401 even against the now-404'd
      route — the same "still passes, tests nothing real" rot as
      `test_api_requires_bearer_when_token_set` above, not a hard
      failure. Rewrite both against `/mcp/`, the same target and
      `TestClient`-context-manager pattern as `test_auth_accepts_valid_token`,
      since they're pinning the same rejection-side behavior
      ("no bearer → 401," "wrong bearer → 401") just against the route
      that survives.
    - **`test_root_public`** (`srv` fixture — no `LOCAL_FITNESS_API_TOKEN`
      configured) and **`test_root_public_even_with_token`** (`srv_auth`
      fixture — token configured, request sent with no `Authorization`
      header) both assert `GET /` returns 200 today, and both need a
      **different** corrected status, not the same one — the middleware's
      `if API_TOKEN is None or _is_public_path(path): ...` short-circuit
      (`web/server.py:183`) means "no token configured" bypasses the
      auth gate entirely regardless of `_is_public_path`'s default:
      - `test_root_public` (no token configured): the auth gate is a
        no-op either way (before and after the deny-by-default flip) —
        only the route disappearing changes anything. Update the
        expected status from 200 to **404**.
      - `test_root_public_even_with_token` (token configured, no header
        sent): today `_is_public_path("/")` returns `True` (public
        fallthrough), so the request passes through unauthenticated and
        hits the (still-existing) route → 200. Once the fallthrough
        denies by default, `_is_public_path("/")` returns `False`, so the
        bearer check now runs and rejects the missing header *before*
        routing ever gets a chance to 404 on the deleted route. Update
        the expected status from 200 to **401**, not 404 — the auth gate
        fires first.
    - **`test_mcp_path_gated_with_token`** already targets `/mcp/` with
      the `srv_auth` fixture and no `Authorization` header, asserting
      401 — this one needs no route change. It also doesn't need the
      `TestClient`-context-manager treatment the two auth-verify tests
      above require: the 401 it asserts fires in the bearer middleware
      before the request ever reaches the `/mcp/` mount, so the ASGI
      lifespan is irrelevant to it (same reason the existing
      `test_security.py` 401-only checks tolerate the bare
      `ASGITransport` fixture). Keep as-is; verify during implementation
      that it still targets `/mcp/` and still asserts 401 pre-mount.
- `tests/test_security.py`: no new HTTP surface is introduced by the
  three plan-lifecycle tools (they're reached only through the
  already-authenticated MCP session, same trust boundary as existing
  plan-write tools) — but *update* it to (a) drop assertions about
  now-removed `/api/*` routes so it doesn't assert against dead code,
  and (b) add the `_is_public_path` deny-by-default case from the Part B
  decision point above. **Six existing tests specifically break** under
  Part B and must be fixed in this same pass, not discovered later by a
  failing suite — one breaks because of the deny-by-default flip, five
  more break because the routes/files they target are deleted. A
  **seventh** test doesn't break (keeps passing) but rots silently and
  needs the same pass:
  - **`test_is_public_path_uppercase_api_not_public`** (:391-403)
    asserts `srv._is_public_path("/") is True` and
    `srv._is_public_path("/assets/index.js") is True`. Both flip to
    `False` once the fallthrough denies by default — update these two
    assertions in place to expect `False`, matching the new
    deny-by-default contract (the test's actual point, that an
    uppercase-cased `/API/...` path isn't treated as the public-exempt
    `/api/` prefix, is unaffected and stays as-is).
  - **`test_spa_fallback_blocks_path_traversal`** (:65) exercises the
    `spa_fallback` route directly (asserts `status == 200` plus an HTML
    body) — that route is deleted outright by Part B, so the route under
    test no longer exists and this test must be deleted, not adapted.
    Its path-traversal-containment property does not need a replacement
    test: `spa_fallback` was the *only* route in the app that joined a
    URL path segment onto a filesystem base (the SPA catch-all resolving
    arbitrary sub-paths to `web/dist/`), and Part B removes that entire
    code path along with the route — once there's no file-serving route
    left, there's nothing left to traverse, so the concern is moot, not
    merely untested.
  - **`test_dashboards_require_auth`** (:210-228) asserts 401 unauthed
    then 200 + `"values" in body` for `/api/activity-heatmap`,
    `/api/strength-volume`, `/api/pace-efficiency` — all three deleted by
    Part B. Once deleted, the authed GET 404s instead of 200. Delete this
    test outright; the route it targets no longer exists.
  - **`test_auth_verify_path_is_public`** (:193-206) asserts 401 unauthed
    then 200 + a specific JSON body against `/api/auth/verify`, which
    Part B removes. Once removed, the authed-request assertion fails
    (404, not 200). Delete this test outright — same treatment, not an
    "update assertions" case, since the route under test is gone.
  - **`test_plan_endpoints_require_auth`** (:257-273) asserts
    `GET /api/plan` returns 200 authed and `POST /api/plan/abc/commit`
    returns 422 (non-int path param rejected). Once `/api/plan*` is
    deleted, `GET /api/plan` 404s (not 200) and `POST
    /api/plan/abc/commit` also 404s before FastAPI's path-param type
    validation ever runs (not 422). Delete this test outright — the
    route it targets no longer exists.
  - **`test_plan_components_have_no_raw_html_sink`** (:276-284) asserts
    `web/src/components/TrainingPlan.tsx` exists on disk. Part B deletes
    the entire `web/` directory, so this fails outright once that lands.
    Delete this test outright — the file it targets no longer exists.
  - **`test_api_requires_bearer_when_token_set`** (:126-138) — the
    seventh case, coverage theater rather than a break. It asserts 401
    against `/api/today` (deleted) unauthed and with a wrong token, and
    `!= 401` against `/api/status` (also deleted) with a valid token.
    The bearer middleware gates by path prefix regardless of whether the
    route exists, so the 401 assertions keep passing against a 404'd
    route, and a 404 also satisfies `!= 401` — the test keeps **passing**
    post-Part-B while its own inline comments ("No token → 401 on
    `/api/*`", "Correct token → not 401") become false claims about
    routes that no longer exist. This is coverage theater in the very
    file CLAUDE.md designates as the security-regression net. Rewrite it
    against a surviving route the same way
    `test_auth_accepts_valid_token` is rewritten below (target `/mcp/`,
    same `TestClient`-context-manager pattern) so it once again exercises
    real behavior — or delete it and note in the PR why: its assertions
    stopped testing anything meaningful once `/api/today`/`/api/status`
    were gone.
  Unlike the two deny-by-default cases above (assertions updated in
  place because the underlying behavior they test still exists, just
  with a different expected value), these four are pure deletions: the
  route or file each test exercises is gone, so there is nothing left to
  assert against and no in-place update makes sense.
- **Coverage-gate check, explicit step**: CLAUDE.md's `--cov-fail-under=85`
  gate is CI-enforced, and Part B bulk-deletes roughly 60 tests alongside
  the code they cover — after the removal, run `uv run pytest --cov` and
  confirm the reported total is still ≥85%. Empirically verified going
  into this design: full-suite coverage is currently 93.36% (8+ points of
  slack above the gate), and `web/server.py` itself is 92% covered by
  exactly the tests this design deletes — code and tests come out roughly
  in lockstep, so this is expected to pass, not a live risk — but the
  design had no explicit verification step for a CLAUDE.md-mandated gate,
  so this line item closes that gap rather than leaving it implicit.
- Full `uv run pytest -x` and `docker compose up -d --build local-fitness`
  pass required before calling this done, per repo convention. This stays
  fast and does not run `tests/test_perf_benchmarks.py`'s timed suite —
  see the `--benchmark-skip` `addopts` mechanism in the Eval-proof
  methodology section above; the benchmark suite only executes under the
  explicit `--benchmark-only` commands documented there.

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
  while this section is being touched anyway. **The new total, stated
  explicitly**: `ALL_TOOLS` (`agent/tools.py:1964-1995`) currently has
  **30** entries as of this design (verified by direct count against
  the file, not the ~29 an earlier pass assumed); this design adds a
  **third** tool (`abandon_active_plan`) beyond the two
  (`commit_training_plan`, `discard_training_plan_draft`) the
  2026-07-06 design already planned, so the correct post-merge count is
  **33**, not 31 or 32. (`LOCAL_ONLY_TOOLS`'s `generate_brief_report`/
  `generate_chart` are separate and stdio-only — not part of this
  count, consistent with Part A's own intro citing "~32 `@tool`
  functions" as the combined `ALL_TOOLS` + `LOCAL_ONLY_TOOLS` total
  today.)
- **Tool descriptions inherited from the reused 2026-07-06 design, now
  stale**: `propose_training_plan`'s `@tool` description
  (`agent/tools.py:1275` — corrected from the prior design's `:1215`
  citation, which has drifted) still reads "Does NOT activate the plan —
  the user commits it," and `revise_training_plan`'s (`agent/tools.py:1311`
  — not `:1251` as the 2026-07-06 design cited; that line number has
  drifted since) still reads "Cannot change a plan's status — the user
  commits via the UI." Both become actively false once Part B retires
  the UI — there's no UI left to "commit via." Update both descriptions
  in this PR to point at the real post-retirement path
  (`commit_training_plan`/`discard_training_plan_draft`/
  `abandon_active_plan`), not "the UI." This carries forward the prior
  design's own documentation-updates obligation, which reuse-as-is
  otherwise leaves ambiguous. **Two more spots carry the same
  staleness, missed by the 2026-07-06 design entirely**:
  `update_plan_workout`'s own `@tool` description
  (`agent/tools.py:1358-1359`) still reads "the agent is the plan write
  path; the web UI is view-only," and the section-header comment above
  the plan tools (`agent/tools.py:1225-1233`, specifically line 1228's
  "activation/deletion is a human action via REST" and line 1230's
  "(the web UI is view-only)") describes the same now-false split
  between agent-owned prescriptions and UI-owned structure/lifecycle.
  Update both in this PR to describe the actual post-retirement tool
  surface — the agent owns the entire plan lifecycle via
  `propose_training_plan`/`revise_training_plan`/`update_plan_workout`/
  `commit_training_plan`/`discard_training_plan_draft`/
  `abandon_active_plan` — not "the web UI" or "REST."
- **Stale in-code cross-references to removed `web/server.py` symbols/
  routes, not caught by any other bullet here**: three separate
  comments name something Part B deletes and need their wording updated
  in the same PR, not left pointing at dead code. `agent/tools.py:43`'s
  comment ("Mirrors web/server.py's `SYNC_MAX_DAYS`") references a
  constant deleted along with the sync-orchestration subsystem (What's
  removed, above) — reword it to describe the throttle in its own
  terms rather than cross-referencing a symbol that no longer exists.
  `agent/tools.py:1673`'s `_write_atomic()` docstring ("mirroring
  web/server.py's SPA-fallback containment check") and
  `tests/test_tools.py:1056`'s comment ("INV-T4: containment check
  mirrors web/server.py's SPA-fallback route") both describe
  `_write_atomic`'s path-containment check by analogy to `spa_fallback`,
  which this design deletes outright (What's removed, above) — reword
  both to describe the containment check on its own terms (e.g. "the
  same `.resolve().relative_to()` containment pattern CLAUDE.md's
  security section mandates for path joins with user input") rather than
  pointing at a route that no longer exists.
- **`docs/deployment.md`**: remove the frontend-build/serving guidance;
  keep whatever's still relevant to the FastAPI/MCP container. Two
  sections go stale the same way and both need fixing, not just the
  first: line 83 ("The web UI checks `/api/auth/verify` on first
  paint...", under "Per-device login") specifically references a route
  this design removes and must be rewritten or cut; and lines 90-97
  (the "Rotating the token" section) describes "the `AuthGate`
  component" re-prompting mid-session on token rotation — frontend
  component behavior that no longer exists once the SPA is retired.
  Rewrite both to describe the post-retirement reality: MCP clients
  (Claude Code/opencode) authenticate via the `Authorization: Bearer`
  header on the `/mcp/` transport directly, so a rotated token means
  updating whatever client-side config holds the token (e.g. the MCP
  server config's env/header), not a browser re-prompt.
- **`web/server.py`'s own module docstring and mount-ordering comments**
  (the comments describing where the SPA static mount / `spa_fallback`
  sits relative to the API routes) go stale once those routes are gone —
  update them in the same PR so they describe the post-retirement route
  table, not the removed one. While rewriting this docstring, also fold
  in removing its pre-existing, unrelated staleness: line 10 documents
  "GET /api/baseline — current baseline row," a route that doesn't
  exist anywhere in the file (not introduced by this design, but since
  the docstring is already being rewritten here, no reason to leave it).
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

## Known follow-up (accepted at quality-gate, not blocking)

13 rounds of red-team/fix converged this design from a weighted score of
10 to 1 (Fatal=3, Significant=1), with 0 Fatal findings surviving past
round 10. One narrow, real gap was found at round 13 and explicitly
accepted rather than looped on further:

- **The Eval-proof methodology's "self-check before considering this
  step done" gives false confidence for its first documented option.**
  Running `uv run pytest tests/test_perf_benchmarks.py --benchmark-only
  --benchmark-storage=file://./.benchmarks --benchmark-compare=0001
  --no-cov` locally, with no matching `0001_*.json` baseline present,
  does **not** raise `pytest.UsageError` or fail the test — it prints a
  `PytestBenchmarkWarning` to stderr and exits **0** ("1 passed"),
  because `--benchmark-compare-fail` (the flag that actually triggers
  the hard failure) is absent from that specific self-check command. An
  implementer relying on this local self-check alone could conclude the
  baseline loaded correctly when it silently didn't. The doc's *second*
  self-check option — trusting CI's `validate` run, which uses the
  identical compare command **with** `--benchmark-compare-fail` — is
  reliable and remains the actual safety net.
  **Action for whoever implements this:** drop the local self-check as a
  stated option (or add `--benchmark-compare-fail=min:15%` to it so it
  actually raises), and rely on CI's `validate` run as the sole
  verification that the baseline placement is correct before merging.

Two additional Minor observations from round 13 (not requiring action,
noted for completeness): the synthetic-fixture-generation script has no
explicit instruction to get its own unit test (unlikely to trip the 85%
coverage gate given the current 93.36%/8+-point slack, per the
Testing strategy's coverage-gate check); and the "Compare" CI step's
insertion point into `.github/workflows/ci.yml`'s `validate` job isn't
given as a precise diff the way every other CI/Dockerfile edit in this
document is.
