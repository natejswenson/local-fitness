# Facet review round 2 — plan-vs-actual charts, plan-tool gaps, prompt drift

**2026-07-19**

Continuation of the morning's four-facet review (see
`2026-07-19-facet-review-brief-resilience.md` / 0.23.0). Round 2 ran three
*focused* reviewers instead of generic dimensions — charts deep-dive,
plan-lifecycle tool audit, prompts/instructions drift audit — over the same
evidence pack, and shipped the confirmed findings (released together with round 1 as 0.23.0).

## Findings → what shipped

- **Charts (both round-1 gaps confirmed).** No renderer accepted two
  aligned series, and bar/combo emitted one row/column per day with no cap.
  Shipped: `render_plan_vs_actual` (pure, fill-style bars — █ actual, ░
  shortfall, verdict emoji per row, uniform double-width glyphs so bars
  align; ⬜ for pending instead of the narrow ▫ for exactly that reason) +
  `plan_chart` tool (daily ≤21d, Monday-anchored weekly buckets above,
  `weekly` to force; actual-suppression matches `weekly_rollup`), and
  weekly bucketing for long-window `bar`/`combo` in the `chart` tool.
  Declined: D2/D3 cosmetic nits (flat-series bar color, short-window line
  footer padding) — defensible behavior, not worth the churn.
- **Plan tools (no HIGHs — state machine and write boundary verified
  sound).** Shipped MED-1 (`duration_min` on `update_plan_workout` — the
  graded field for tempo/interval had NO tool path), MED-2 (rest-flip
  defaults description to "Rest day" instead of leaving "Long run 12mi…"
  prose on a rest day), MED-4 (`revise_training_plan(goal_type=...)`
  re-derives `goal_distance_m`). Deferred MED-3 (seq≥2 double-day support
  — Nate doesn't run doubles; plumbing half-exists) and LOW-2 (partial
  unique index on (plan_id,date,seq) — validation already prevents it).
- **Prompts drift.** Shipped the code-only finding: V1 rollback's brief
  loop never granted `get_training_plan_status` despite the prompt saying
  "call it FIRST" — plan-aware V1 briefs silently dead since the V2
  cutover (allow-list fixed, test pins it). Also fixed stale docstrings/
  comments (retired `/api/*`, "frontend renders...") and added the
  brief-staleness line to the `/coach` snapshot. **Deferred to the A/B
  queue** (prompt-text changes, gated per policy): the "read-only access"
  self-contradiction in `system_prompt` (line 37 vs the note-write
  instructions), and the in-prompt "so the UI can render" phrasing.
- **From round 1's backlog, also shipped here:** launchd 09:30 backstop
  (`--if-missing` + second `StartCalendarInterval`; plutil-linted).

## Gotchas

- `tests/test_smoke.py` pins `len(ALL_TOOLS)` — any new tool bumps it (34→35).
- WeasyPrint tests need `DYLD_LIBRARY_PATH=/opt/homebrew/lib` on macOS; it
  lives in `.env` but pytest doesn't auto-load `.env`, so a bare shell run
  can fail 13 `generate_brief_report` tests that are green in CI. Not a
  code issue.
- `validate_plan_input` rejects workouts dated before `created_floor` —
  test plans that need in-window graded days must date them today, not in
  the past.

## Still open (A/B queue + backlog)

- A/B-gated prompt edits above (run scorer + differential stash test —
  `ab_brief.py --run` is flaky; see feedback_ab_brief_harness_flaky).
- MED-3 seq support, LOW-2 unique index, D2/D3 chart cosmetics.
- Watch the brief hit-rate for a week (retry + backstop should take it
  from ~50% to ~100%; failure notification now fires if not).
