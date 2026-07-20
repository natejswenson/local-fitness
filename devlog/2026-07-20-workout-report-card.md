# 2026-07-20 — `workout_report_card`: grading a single workout

## Why

The coach could already *describe* a workout — `get_workout_detail` returns
distance, pace, HR, splits — but nothing *judged* one. Every assessment was
phrased ad hoc by the model, which meant the same 3-mile easy run could come
back "solid" one morning and "flat" the next, with no stable rubric behind
either word. That's exactly the failure mode `interpret.py` was written to
kill: the LLM should phrase a judgment, never derive one that tested Python
can compute.

## What landed

`agent/report_card.py` — a pure-then-persistence module (the `plans.py`
layout) that grades one activity across four metrics: distance, pace, HR, and
training load. Each metric reduces to a single non-negative relative deviation
`d`, and every `d` goes through ONE shared band table. Four small deviation
functions, one grader — that's the whole trick, and it's what keeps the rubric
testable instead of a pile of per-metric special cases.

Surfaced as the `workout_report_card` MCP tool: a preformatted `markdown` card
plus a PRESS-themed PDF with a per-mile split table and an HR bar chart.

## The decisions that actually mattered

**Direction gating.** The first honest version of this graded
`|actual − expected|` and handed every recovery run an F, because an easy run
is *supposed* to be slow. Easy/long days are now penalized only for running
too fast, quality days only for too slow, and each expectation is scaled by
the workout's intent.

**Don't pool treadmill with road.** Measured on live data: pooling `running`
with `treadmill_running` put median HR at 119 when outdoor runs average 140,
which gave a perfectly normal outdoor easy run a D on heart rate. That's an
artifact of mixing two HR regimes, not a judgment. Comparability is now exact
`activity_type` first, widening to the on-foot class only when the exact pool
is too thin — and the card says when it widened.

**Intent-scale the load expectation too.** Same bug class, caught later: a
prescribed 3-mile recovery run graded against the unscaled load median took a
D. An easy day is supposed to bank less load. `LOAD_FACTORS` is deliberately
the same numbers as `DISTANCE_FACTORS` — a second independently-tuned table
would be false precision — but named separately so it can diverge if evidence
ever says it should.

**Refuse to grade against noise.** Under 5 comparable activities in the
window, the median isn't a yardstick. The card returns n/a and explains why,
which is strictly better than a confident letter derived from three data
points.

**Splits are presentation-only.** Only 87 of 747 activities have
`activity_splits` — the daily-sync ingest path writes them, backfill never
did. A splits-dependent grade would be silently unavailable on ~88% of the
history and would quietly mean different things on different rows. So no grade
reads them; they render, and that's all.

## Housekeeping folded in

- The 0.24.1 release metadata (version bump + CHANGELOG entry) had been lost:
  the overflow-fix *code* reached `dev` through the 0.24.0 promotion, but the
  post-promotion `dev` reset took the bump with it. Recovered here.
- Two invariant tests hardcoded `LOCAL_ONLY_TOOLS == {generate_brief_report}`.
  They now assert the membership *rule* — a tool that returns a filesystem
  path is local-only, because a remote `/mcp/` caller can't retrieve the file
  — rather than a frozen one-element set that any second PDF tool would break.

## Verified

1234 tests pass, 94% coverage (`report_card.py` at 98%), ruff clean, and the
tool was smoke-tested end-to-end against the real DB — rendered PDF inspected
for the 0.24.1 overflow class of bug before shipping.
