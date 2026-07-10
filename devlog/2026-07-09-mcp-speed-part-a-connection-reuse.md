# MCP tool speed, Part A: connection reuse + bounded queries

**2026-07-09**

Nate's asking for the daily brief and plan-progress reads over the MCP
surface (Claude, opencode) to be faster — and wanted the claim eval-tested,
not eyeballed. Round-tripped through `/design`, quality-gated 13 rounds
(`docs/plans/2026-07-09-mcp-speed-and-ui-retirement-design.md`), then
implemented Part A (speed/efficiency; Part B, UI retirement, is separate).

## What changed

Three independent connection-count/query problems, all real, all measured:

- **`assemble_brief_context`**: 9 → 1 `db.connect()` opens when a plan is
  active. Every function in the chain (`db.get_setting`/`all_settings`,
  `config.coach_profile`/`riegel_lookback_days`, `plans.get_active_plan`/
  `load_activities_by_date`/`resolve_grading_config`/`best_recent_effort`,
  `agent/coach.resolve_coach_profile`) gained an optional
  `conn: sqlite3.Connection | None = None` param — additive, existing
  no-conn callers unchanged. `assemble_brief_context` now opens one
  connection at its own entry point and threads it all the way down.
- **`get_training_plan_progress`**: 6 → 1. **`get_training_plan_status`**
  and **`_build_plan_section`** (the PDF's Training Plan section): 4 → 1
  each. Same pattern, applied in `agent/tools.py`.
- **`agent/status.py`'s `_metric_rows`**: up to 4 queries → 1. The old code
  ran a separate `WHERE <metric> IS NOT NULL` query per trend metric
  (steps/sleep_score/max_stress); now one trailing-window `SELECT *` feeds
  all three, with the null-filtering moved into Python.
- **`agent/brief_planner.py`'s `_compute_signals`**: the run-history query
  against `activities` was unbounded — `WHERE date <= ?`, no floor, so it
  scanned the whole table regardless of account age. Bounded to a 35-day
  lookback (`_ACTIVITY_LOOKBACK_DAYS`). Accepted tradeoff:
  `days_since_last_run`/`recent_te` read `None`/fewer-than-5 instead of the
  true value once the runner's last run predates 35 days — a real behavior
  change, covered by a regression test asserting exactly that.

## Proving it, not eyeballing it

New `pytest-benchmark` harness (`tests/test_perf_benchmarks.py`,
`scripts/perf_fixture.py`) — separate axis from the existing prompt-quality
eval:

- **Synthetic 3-year fixture with an active plan**, fabricated (never
  derived from `data/fitness.db` — CLAUDE.md's rule on personal data). Small
  fixtures hide these bugs by construction; the unbounded-query problem is
  invisible on a 30-day eval fixture.
- **Connection-open COUNT is an explicit assertion**, via a monkeypatched
  counter around `db.connect`, not just latency (latency alone can hide a
  correctness regression, like accidentally sharing a connection across a
  write boundary).
- **Pre-fix numbers verified empirically**, not assumed: stashed the fix
  commits, reran the connection-count assertions against unmodified code,
  confirmed the exact 9/6/4/4 the design doc predicted, then restored the
  fix. Documented in a comment block at the top of the test file.
- **Skipped by default** (`--benchmark-skip` in `pyproject.toml`'s
  `addopts`) — composes with an explicit `--benchmark-only` (pytest-benchmark's
  own `if self.skip and self.only: self.skip = False`), so ordinary
  `uv run pytest` runs are unaffected.
- **CI-gated**: a new `capture-perf-baseline.yml` `workflow_dispatch` job
  captures the baseline on `ubuntu-latest` (machine-id matched —
  pytest-benchmark nests saves under `platform-implementation-pyver-arch`,
  so a Mac-captured baseline would never match in CI). `ci.yml`'s `validate`
  job gained a "Perf-benchmark regression gate" step comparing every PR
  against the committed baseline (`--benchmark-compare-fail=min:15%`).

## Not done here

Part B of the design (retiring the web UI, adding `abandon_active_plan`)
is separate, larger, more destructive (deletes `web/` outright) — tracked
in the same design doc, implemented as a follow-up.

## Verified

939 tests passing (932 → 939; +5 benchmark tests, +2 correctness/regression
tests), 93.45% coverage, ruff clean. Perf-benchmark suite runs green under
`--benchmark-only --no-cov` locally; the `--benchmark-compare` gate needs the
CI-captured baseline to be live (next step).
