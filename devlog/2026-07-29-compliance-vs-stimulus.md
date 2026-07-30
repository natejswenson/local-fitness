# 2026-07-29 — the rubric was rewarding disobedience

## Why

I told Nate to run 5 easy miles with heart rate under 140. He did it — 5.01 mi,
average HR 126, splits inside 13 seconds of each other, 97% of the run in Z1–Z2.
Then the card graded it a **C** and the coach text told him he'd been coasting:

> "You banked distance and pace, then let the work evaporate. Long run's
> Thursday. Show up loaded, not coasting. Back to work."

He pushed back, correctly: *"If I would have ran faster my HR would have
increased and my pace would have increased, therefore reducing my grade. The way
it's set up there was no way for me to succeed."*

He was right, and it was worse than he thought.

## The measurement

Two easy days that week, same prescription both times — *"Easy 5mi. Keep HR under
140."* Graded against the live DB (`median_hr` 143, `median_load` 99.7 over 23
comparable treadmill runs):

| Session | dist | pace | HR | load | GPA | Overall |
|---|---|---|---|---|---|---|
| **Wed** — 5.01 mi, HR 126, even splits, obeyed | A+ | A+ | A+ | **F** | 3.60 | **C** |
| **Mon** — 5.00 mi, HR 144, splits 128/139/150/144/159 | A+ | A+ | A− | A+ | 4.00 | **A** |

Not blunt. **Inverted.** The rubric gave the disobedient run an A and the
obedient one a C, and Monday earned its **A+ on load** — on the exact axis that
gave Wednesday an F.

## Three independent causes

**1. Training load *is* heart rate wearing a disguise.** Garmin's load is
essentially `duration × f(HR)`. From the 34 runs in the window:

| Avg HR | Load per minute |
|---|---|
| 114–126 | 0.49 – 0.51 |
| 129–138 | 1.16 – 1.77 |
| 142–149 | 1.58 – 3.00 |
| 169–172 | 6.02 – 7.61 |

The card graded HR against a cap *and* graded load — the same variable twice,
with the sign reversed. Obey the cap, fail the load.

**2. The load grade was unreachable, not merely strict.**
`LOAD_FACTORS["easy"]` was 0.75, giving a ~75 expectation. A compliant sub-cap
50-minute run tops out near 70 (median 1.42 load/min across the window's
sub-140 sessions); a *properly* easy one lands near 25. The F threshold sat
**above the physical maximum of a compliant easy run**.

**3. The prescription's HR cap was unreadable.** `plan_workouts` had no HR
column, so "Keep HR under 140" was prose in `description` that no grade could
see. HR was measured against `0.97 × rolling median` — which was 139 that day
**by coincidence of Nate's training mix**. That's why blowing a stated 140 cap
by 5 bpm, with miles 3–5 at 150/144/159, cost exactly one `+/−` modifier.

## The fix: two surfaces, not one letter

- **Compliance** (distance, pace, HR — and continuity, added in Phase 3 below) —
  graded. "Did you execute the prescription?"
- **Stimulus** (load, aerobic/anaerobic TE, HR-zone share, drift) — reported with
  a `LOW|MODERATE|HIGH|VERY HIGH` descriptor and **no letter**.

The important part is *how* load was removed: it is absent from every
`INTENT_METRIC_WEIGHTS` table, not given a small weight. `overall_grade` iterates
those tables, so "load cannot lower your grade" became a property of the data
structure. Load was already only 10% of an easy day — a small weight was never
the protection, because the **F-cap** bypassed it and turned a 3.60 GPA into a C.

Three things I deliberately did **not** do:

- **Didn't delete the F-cap.** The original reasoning is right: a card printing
  "Overall: A" above an F row is averaging away a finding. I narrowed its scope
  to weighted metrics. The cap was never the wrong rule — load was the wrong
  thing to apply it to.
- **Didn't fix this by recalibrating.** I did move `LOAD_FACTORS["easy"]` to
  0.61 (measured: the 9 sub-ceiling sessions median 60.5 against the pool's
  99.7), but only for descriptor honesty. At 0.61 a correct 25-load easy run
  still deviates 0.58 — still an F. Constants weren't the bug.
- **Didn't let the model re-derive the scolding.** Removing the letter isn't
  enough; the read would still moralize at a low number. The prompt now carries
  the stimulus block marked `REPORTED, not graded`, plus an explicit rule that a
  low easy-day stimulus is the prescription working. The deterministic fallback
  template carries the same sentence — otherwise a failed Claude call silently
  reverts to the old reading.

## Then: teaching the grader to read the prescription

Splitting load out fixed Wednesday (C → A) but left Monday at an A too. The
inversion became a **tie**, which is not a fix. So the cap became a column:
`plan_workouts.target_hr_max` (guarded `ALTER`, the `activities.source` pattern)
plus `hr_max` on `update_plan_workout`.

And it grades **time above the cap**, not just the average — the average is what
let Monday read A−. Monday spent 59% of its split time above 140. That's an F on
"keep HR under 140", and it should be.

One wrinkle worth recording: the time check reads `activity_splits`, and this
module has a standing rule that no grade reads splits (only ~97 of 757 rows have
them). It earns a second exception by **degrading rather than abstaining** —
with no splits the cap is still graded on the average, which beats not reading
the cap at all. Different shape from the quality-day-pace exception, which
abstains, and the difference is now documented in CLAUDE.md.

Graded on the base bands, not `PLAN_TIGHTEN`: tightening a *time fraction* would
double-count strictness and turn "8% of the run drifted over" into a D.

## Result

The same three real sessions, before and after:

| Session | Before | After |
|---|---|---|
| Wed — obeyed the cap exactly | **C** | **A** |
| Mon — 59% of the run above the cap | **A** | **C** (F on HR, capped) |
| Tue — tempo that never reached tempo pace | **C** | **B** |

Before: three sessions, three C-or-better letters with the ordering backwards.
After: the ordering matches what actually happened.

The zone data turned out to be the sharpest discriminator, and it was sitting in
the DB unused the whole time. Same prescription, same distance, averages 18 bpm
apart — but **97% aerobic versus 30%**. That's the number that makes "this really
was an easy run" checkable instead of asserted, and it's on the card now.

## What I'd watch

`HR_CAP_GRACE_FRACTION` is 5% and per-split-average granularity is coarse — a
single mile a hair over the cap counts fully. If that starts firing on runs that
were genuinely fine, the grace fraction is the knob, not the bands.

---

## Phase 3 — the metric for "was this one session?"

Splitting stimulus out and reading the cap fixed the *inversion*, but Tuesday
still bothered me. Its tempo session contained a **12:31 mile among ~9:20 miles**
and the card graded it A+ on distance, A+ on HR, and said nothing. Every existing
metric averages a break away.

Before building it I checked whether a 4th axis would actually add information or
just re-grade something already graded — the exact mistake that caused all of
this. Across the 40 split-bearing sessions in the live 90-day window, **three
pass distance, pace AND HR while carrying a slowest split 30-41% off their own
median.** Independent. Build it.

Two things I expected to use and rejected on the data:

**Standard deviation.** It punishes a conservative opening mile. 2026-07-27
measures SD 22.2 s/mi across the run and **4.9 s/mi once the warm-up mile is
dropped** — the first split is routinely the outlier. Failing a run for starting
easy would have recreated precisely the unfairness I had just spent the day
removing.

**Absolute walk-pace detection.** 12:31/mi is *under* the measured 13:00 run/walk
boundary, so `RUN_PACE_CEILING_SEC_PER_MI` would have missed the exact session
the metric exists for. Embarrassing thing to discover after writing the function.

What works is **slowest full split / median full split**. Self-scaling, so it
means the same thing on a 9:00 tempo and a 12:00 shakeout and needs no intent
factor. Threshold 1.15 separates cleanly: 33 of 40 sessions at or under it, and
all 7 above are genuine run/walk days.

One scaling detail that mattered. `(ratio - tol) / tol` compresses every real
break into the A/B bands — a 12:31 mile among 9:20s scored a **B**. The ratio
already lives near 1.0, so dividing by the tolerance is the wrong shape. The raw
excess (`ratio - tol`) reads as "how much slower than an already-generous
allowance the worst split was, in units of a median split", which puts 20% slower
at an A, 35% at a C and 55% at an F.

Result on the three sessions:

| Session | continuity | note |
|---|---|---|
| Wed 7/29 easy | **A+** (1.006) | — |
| Mon 7/27 easy | **A+** (1.082) | — (the 10:41 opener does not fire) |
| Tue 7/28 tempo | **C+** (1.274) | "mile 4 ran 12:31/mi — 27% slower than your median mile for this run" |

Also fixed en route: `reference_line` was blaming the thin reference pool for
continuity's n/a — "continuity ungraded, only 2 comparable activities in the last
60 days" — when continuity never touches the pool. It now lists only metrics whose
reference actually IS the pool, which also stops a by-feel pace from being blamed
on missing history. Small lie, but this whole day was about the card not telling
them.

## What I'd watch (updated)

`CONTINUITY_TOLERANCE` at 1.15 was fitted on 40 sessions, most of them treadmill.
Outdoor runs with real hills will carry more legitimate mile-to-mile spread. If
this starts firing on honest hilly runs, the tolerance is the knob — or it becomes
intent-aware the way `HR_BANDS` already is.
