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

---

## Round 3 — "make sure the grades are meaningful"

Three asks: move the read into the hero, drop the GPA explainer, and make the
grades actually mean something. The third one found three real bugs.

**The HR row contradicted itself.** It printed `136 bpm | expected 146 | -7% |
B+`. Read that as a human: you were seven percent *under* expectation and got
marked down for it. The grade was fine — it came from being 6% above the *easy
ceiling* of 128 — but the card displayed the median instead of the bound it
actually graded against. Every other metric's Expected is the number its
deviation was computed from; HR was the odd one out. It now shows the band
(`≤ 142 bpm`), and a run inside it reads "in range" rather than a percentage
against one edge.

**The easy-HR ceiling was unreachable.** 0.88 × the rolling median asked for
128 bpm. Across 60 days and 13 runs, exactly one came in under that — and its
HR looks like a sensor fault. The bug is conceptual: the reference median is
taken over *all* comparable activities, and for a runner whose training is
mostly easy, that median already sits near easy HR. Demanding 12% below it
means every ordinary easy run gets marked "too hot," which makes the HR grade a
standing penalty rather than a judgment. Recalibrated against the actual
distribution.

**A missed prescription could still earn an overall A.** After the HR fix the
card printed **A (3.70)** for a run whose own coaching read said "you never ran
easy at all." Plan targets and rolling medians were held to identical
tolerance, so a prescribed 10:28 run executed at 9:28 — a full minute per mile
fast, which is the entire failure mode an easy day has — came out a B−. But a
plan is an explicit instruction and a median is a fuzzy reference; they should
not share a scale. `PLAN_TIGHTEN` narrows the bands for plan-referenced
distance and pace. That run is now a C on pace and a B overall, which is what
the prose was saying all along.

The rule that falls out of all three: **the card must never contradict itself**
— not the Expected column against its own grade, not the overall grade against
the coaching read. There's a test for each now.

### The read

It covers all four metrics and never names a letter, since the letters are
printed in the table immediately below it. Budgeted in *words* (85), not
sentences — a sentence cap produced five sentences long enough to push the HR
chart onto a second page.

### And one more network leak

Same lesson as last round, different provider. The PDF path resolves an HR
trace, so a test that merely rendered a PDF was calling Garmin's
activity-details endpoint for a fixture id. It surfaced only as a 404 buried in
the logs of a passing test. The conftest guard now covers Garmin alongside the
SDK, and there's a bounding-box overflow test for the report card too — the
coach read is model-generated variable-length prose in a fixed cell, which is
exactly the shape that caused the 0.24.1 overflow.
