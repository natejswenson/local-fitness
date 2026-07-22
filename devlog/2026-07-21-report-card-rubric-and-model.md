# 2026-07-21 — the report card was grading runs against walks

## Why

I regenerated two report cards and read the letters against the coach text
sitting directly above them. They disagreed — not subtly.

Today's interval session:

| Metric | Actual | Expected | Delta | Grade |
| --- | --- | --- | --- | --- |
| Distance | 5.95 mi | 5.00 mi | +19% | D- |
| Pace | 10:42/mi | 6:58/mi | 224s/mi slower | F |
| Avg HR | 136 bpm | ≥ 116 bpm | in range | A+ |
| Training Load | 81 | 22 | +263% | A+ |

Overall: C. The coaching read called it *"more miles, same broken session"* and
*"your legs are stacking debt before the week's hardest ask"*. Three of those
four letters were artifacts, and the fourth was arithmetic.

## What was actually wrong

**The reference pool contained walks.** `activity_type` is Garmin's label, not
a measurement, and my walking-desk sessions log as `treadmill_running`. Both
the exact-type filter and `plans._is_running` — a substring match on `"running"`
— passed every one of them through. Over the 60-day window that type held 46
activities that were cleanly bimodal:

- 16 real runs — 8:40–11:46/mi, HR 114–172, load 19–434
- 30 walking-pad sessions — 14:08–84:20/mi, HR 76–120, load 3–31

So the "median comparable activity" was a **15:50/mi walk at 116 bpm and 22
training load**. An interval session's HR floor was 116 bpm and its load
expectation was 22. HR and load are 40% of the composite and had quietly become
constants: A+ for clearing a walking bar.

**Interval pace could only ever be an F.** A plan's quality-day pace describes
the *reps*. `avg_pace_sec_per_km` averages in the warmup, the recovery jogs and
the cooldown. Comparing them isn't a strict rubric — it's an arithmetic
guarantee, because every correctly-executed interval session averages far
slower than its rep pace. Mile 4 of that run was 9:25 at 164 bpm. The rubric
structurally could not see it.

**The load grade contradicted its own note.** `load_deviation` was
one-sided-low and uncapped, so overshoot was always an A. The card printed
`A+` and, one line below, `Training Load: **spike** — more than double your
median day`. An A+ that means "this is a red flag" is not a grade.

**Flat weights outvoted the point of the session.** A prescribed 10:28 easy run
executed at 9:28 — a full minute per mile too hot, which is the *only* way an
easy day fails — scored an overall **B (3.40)**, because HR and load together
carried 40% and both landed A.

## What landed

Comparability is now gated on **measured locomotion, not the label**
(`RUN_PACE_CEILING_SEC_PER_MI`, a 13:00 mile — the live gap runs 11:46 → 14:08,
so there's ~2 minutes of margin either side). A run compares only against
running-effort activities, a walk only against walking ones, and a paceless row
has an unknown mode so it joins neither. The filter runs **before** the type
filters, because widening is exactly what would otherwise drag the whole
walking corpus into a thin running pool.

Quality-day pace is graded on the **fastest full split** — the one documented
exception to "no grade reads `activity_splits`". Where there are no splits
(~88% of backfilled history) it returns n/a *with a stated reason* and its
weight redistributes; it never falls back to the comparison it exists to avoid.

Load is two-sided past `LOAD_SPIKE_FACTOR` (at or under the threshold is still
a clean A, so a big day is still not a failure). `overall_grade` is
intent-weighted — pace carries an easy or quality day, distance carries a long
run — and an F on any metric caps the overall at C.

The same card now reads:

| Metric | Actual | Expected | Delta | Grade |
| --- | --- | --- | --- | --- |
| Distance | 5.95 mi | 5.00 mi | +19% | D- |
| Pace | 9:25/mi best mile | 6:58/mi | 147s/mi slower | F |
| Avg HR | 136 bpm | ≥ 145 bpm | -6% | B+ |
| Training Load | 81 | 105 | -23% | D+ |

Overall: **D**. Note the load flip — against a real interval-day expectation of
105, banking 81 isn't a spike at all, it's a shortfall. The card and the coach
now say the same thing.

`HR_BANDS` was re-verified against the cleaned distribution and deliberately
**left unchanged**: excluding the walks moves the `treadmill_running` median
from 116 to 145, which agrees with the never-contaminated outdoor median of
144.5, so the existing constants describe both pools. Checking was the work;
changing nothing was the result.

## The model was one generation behind and unconfigured

`workout_coach` inherited `briefing.DEFAULT_MODEL` and set no `effort` and no
`thinking`. That coupling was the real problem: `briefing.DEFAULT_MODEL` also
drives the daily brief, where a model change is a prompt change that has to
clear the scorer and a cross-model A/B first — so this call could not be tuned
at all. The two share a vendor and nothing else. The brief reasons over a whole
day of data; this one phrases four 45-word paragraphs from grades
`report_card.py` already computed and the prompt explicitly forbids it from
re-deriving.

Measured, 12 generations across 2 real cards:

| config | median | max | parsed | over-45 words | leaks |
| --- | --- | --- | --- | --- | --- |
| sonnet-4-6, no effort, adaptive thinking | **142.9s** | 165.6s | 4/4 | 2/16 | 0 |
| sonnet-5, `effort=low`, thinking off | **10.0s** | 10.8s | 6/6 | 4/24 | 0 |
| sonnet-5, `effort=medium`, thinking off | 9.7s | 10.7s | 6/6 | 10/24 | yes |
| haiku-4-5, `effort=low`, thinking off | — | — | 0/4 | — | — |

Two things I'd have got wrong by eyeballing. First, the old config was running
**142.9s median against a 180s timeout** — 15s of margin, where a timeout
silently swaps the coach's voice for the deterministic template. The docstring
claimed 66.9s. Second, `effort=medium` bought back *no* latency and more than
doubled word-budget overruns, and it leaked exactly what `_GRADE_TONE` forbids
outright: *"F is F."* and *"B+ on paper"*. `low` it is.

The `effort`/`thinking` settings are load-bearing, not polish — current Sonnet
runs adaptive thinking whenever `thinking` is unset, so moving the model ID
forward *without* them would have made an already-slow call slower. Timeout
dropped 180s → 90s, still ~8x the measured max.

## Things worth knowing next time

**A split-heavy card is 2 PDF pages, and always was.** I spent a while assuming
I'd caused it before measuring the pre-change layout, which does the same. It
sits right on the boundary and the read's word count is the swing factor —
which is what makes the 45-word budget real rather than cosmetic. Any content
added to the card should be measured with
`len(HTML(...).render().pages)` on `activity_id` 23685126977 (6 splits) first.
The quality-pace note was dropped from the success case for exactly this
reason: `9:25/mi best mile` beside a `6:58/mi` target already says what was
compared, so the bullet bought nothing and cost a row.

**Still open, deliberately out of scope:** the same mislabelling reaches plan
adherence. `plans._is_running` is a substring match, so a 29:15/mi walking-desk
session currently counts toward prescribed running mileage in
`get_training_plan_progress`, the plan charts, and the brief PDF's plan
section. Wider blast radius, its own change — and `plans.py` already has
`count_walks_easy` / `count_walks_mileage` knobs a fix should probably route
through.
