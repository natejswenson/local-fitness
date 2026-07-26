# 2026-07-26 — Maintainability pass: gates that actually gate (0.38.0)

## Why

Batch 4 of 4 from the three-axis audit. Its findings shared a theme:
guardrails that looked real and weren't. Ruff ran defaults-only while the
code carried `noqa: BLE001` suppressions for a rule that was never on —
reviewers reading those comments reasonably believed a blind-except gate
existed. The docs claimed "every tool, one page each" while sitting 8 pages
and 8 tools behind the registry (README said 37; the truth was 45) — and
among the missing was `recall_coach_memories`, the tool the coach's own
system prompt says to call before claiming it doesn't remember. And
`READ_SECTIONS` living in `workout_coach` inverted a dependency, forcing
seven lazy imports and four wrapper functions whose entire body was an
import.

## What

- `READ_SECTIONS` → `report_card` (the card's contract), re-exported for
  compatibility; `workout_coach` now imports `report_card` at module scope
  and the wrappers are gone. A subprocess test pins the import direction.
- `[tool.ruff.lint] select = ["E4","E7","E9","F","I","UP","B","A"]` with
  the exclusions documented IN the config: `ARG` never (MCP `_args`
  contract), `BLE`+`RUF100` deferred as a pair (enabling RUF100 alone
  would flag the existing BLE noqas as unused; 30 broad-except sites need
  real triage). ~75 fixes, mostly autofix; the hand-fixes were judgment
  calls — `zip(strict=True)` where series are same-length by construction,
  a frozen-dataclass default singleton for `GradingConfig`.
- 8 new docs/mcp pages + corrected counts + `tests/test_docs_drift.py`:
  page-per-tool, no orphans, counts derived from `len(ALL_TOOLS)`.
- Six `assert err` plan-validation tests now pin exact messages — the
  after-race and before-created boundary checks were structurally
  identical to a test that couldn't tell them apart.
- Dead `plans._foot_duration` deleted; public `units.METERS_PER_MILE` /
  `KM_PER_MILE` with the redeclarations retired (interpret keeps its
  private copy for its stdlib-only contract, pinned equal by test).

## Gotchas

- B007 flagged `fit_one_page`'s loop `index` — a false positive (it's read
  after the loop as the returned rung); scoped noqa with the reason, not a
  rename that would obscure the return contract.
- The `zip(strict=True)` conversions are behavior changes in the strict
  sense: silent truncation becomes a loud error. Every converted site
  pairs series built from the same rows, where truncation could only ever
  have hidden a bug.
