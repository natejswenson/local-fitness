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

---

## Round 2 — five fixes from first real use

Looking at an actual rendered card surfaced five things, four cosmetic and one
that turned into real infrastructure.

**The card now opens with the coach talking.** A report card that leads with a
table is a spreadsheet. `workout_coach.py` phrases what the grades mean, in
voice, before the grades appear — same shape as `plan_coach` (single-shot,
cached, deterministic fallback), separate module because prepping a run you
haven't done and judging one you have share a voice but nothing else.

**Per-tenth-mile HR was the expensive one.** The samples weren't in the DB at
all — `activity_splits` is per-lap and `raw_json` is the activity summary.
Garmin's details endpoint has them (~1700 per run), so: fetch on demand for the
one activity being graded, cache in a new table, never backfill. 747 activities
of detail calls is precisely the shape that produced the 429 the token cache
was added to fix. The chart now carries a title, both axis labels, and the
run's own average as a reference line.

**GPA needed explaining, so it now explains itself.** The card printed 3.45
with no way to reconstruct it. The explainer lists the weights actually used —
and had to learn to renormalize when a metric is n/a, and to absorb the
rounding remainder so the shares sum to 100 rather than 99.

Two one-liners: the Distance column came out of the per-mile table (it printed
"1.00 mi" next to a column headed "Mile"), and the Grade column moved to
left-aligned with everything else.

### What the tests caught that review didn't

- **The read cache ignored `activity_id`.** Two sessions on one day with the
  same name and grades — a double day, which this tool already reports —
  hashed identically, so the second card served the first's read.
- **`.get("grade", "")` doesn't default when the key exists holding `None`.**
  That's exactly the insufficient-history card, and it raised
  `AttributeError` in the fallback path — the code that only runs when
  something else already failed.

### The one that mattered most

Adding the read meant *every* report-card render called Claude, so the test
suite quietly started making live network calls. It went from 10 seconds to 7
minutes and cost real money — and stayed green the whole time, which is why it
would have shipped unnoticed. `conftest.py` now blocks `claude_agent_sdk.query`
outright for the whole suite, at the choke point rather than per-caller, so a
future generator module inherits it without anyone remembering.

That also exposed the timeout: with the SDK actually reachable, a real card
took 22.2s against a 30s ceiling. Raised to 90s. Every prior "successful" local
render had been silently serving the template fallback.
