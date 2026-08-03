# 2026-08-02 — a prescribed HR cap is graded on how far over, not how long

**Version:** 0.40.2 · **Branch:** `feature/hr-cap-grading-fix`

## The card that was wrong

Activity 23825963527. Treadmill easy run, 2026-08-02. The active plan
prescribed *"Easy 5mi. Keep HR under 140."* with `target_hr_max = 140`.

What actually happened: 5.01 mi, **average HR 139**, peak 148, five mile splits
at 134 / 141 / 135 / 143 / 142. Garmin logged **0 seconds in zones 4-5**.

What the card said:

| Metric | Actual | Expected | Delta | Grade |
|---|---|---|---|---|
| Distance | 5.01 mi | 5.00 mi | on target | A+ |
| Pace | 9:44/mi | 9:39/mi | 5s/mi slower | A+ |
| Avg HR | 58% above cap (avg 139 bpm) | ≤ 5% above cap | 53% over | **F** |
| Continuity | 1.04 | ≤ 1.15 | even | A+ |

GPA 3.60, F-capped to an overall **C**.

The run obeyed its prescription. Three A+ grades and a note saying so, and the
card called it a C.

## Root cause: a category error, not an off-by-one

Two things stacked.

**1. A split counted as entirely above the cap if its average exceeded it by any
amount.** `time_above_cap_fraction` did `if r["avg_hr"] > cap`. Miles 2, 4 and 5
averaged 141, 143 and 142 — one to three bpm over, 0.7% to 2.1% — and each
contributed 100% of its duration. 1692 s of 2921 s = 58%.

**2. The resulting time fraction was graded on `GRADE_BANDS`.** That table
(0.05 → A, 0.10 → B, 0.20 → C, 0.35 → D) describes *relative magnitude* — a 53%
pace miss is catastrophic. A fraction of a run is a different quantity in a
different unit. `hr_cap_deviation` then took `max()` over that fraction and the
average-over-cap relative deviation, which are not commensurable, so the
comparison had no meaning and the fraction won essentially every time.

The consequence is measurable. Over the **19 completed capped days** in the
active plan:

- The axis produced **only A+ or F. Never a letter in between.**
- It ranked 2026-07-16 — 4.55 bpm over on average, **1%** of it in zones 4-5 —
  as the *worst* session of the nine it failed (75.4% of time over), worse than
  2026-07-22, which hit 185 bpm and spent 48% in zones 4-5.
- It failed 2026-08-02 (0% in zones 4-5) and 2026-07-19 (0% in zones 4-5).

Nine of nineteen days graded F. Six of those nine were wrong.

## The fix

`hr_exceedance_bpm` — the time-weighted mean bpm **above** the ceiling:

```
sum(duration * max(0, split_hr - cap)) / total_duration
```

The integral of the breach over the run, divided by the run. It reads as "you
sat, on average across the session, N bpm above the ceiling you were given",
and it is linear in magnitude, which is the property the time fraction lacked
entirely: 1.15 bpm for 2026-08-02 against 19.51 bpm for 2026-07-22.

Both cap axes — the average and the exceedance — now go through one
`hr_cap_severity`, so `max()` compares like with like:

```
max(0, bpm_over - HR_CAP_NOISE_BPM) / HR_CAP_BPM_SCALE
```

Raw excess past a floor, scaled — the same shape `continuity_deviation` uses,
and for the same reason. Dividing by the cap compresses every real breach into
the passing bands, because HR has a huge non-zero offset: the worst session in
the window comes out at `19.5 / 140` = 0.139, a **C**.

That compression was also corrupting the *average* axis, which nobody had
flagged. A run averaging 168 against a prescribed 140 — 28 bpm over, for the
whole run — graded `28/140` = 0.20, a **C**. It is now an F.

## Calibrating the two constants

Not intuition. Both against the live distribution, validated by a signal the
grade does not read: **Garmin's own zone-4+5 time fraction**, computed on the
device from the per-sample trace and therefore independent of both `avg_hr` and
the splits.

**`HR_CAP_NOISE_BPM = 1.5`** — the floor below which a split-average breach is
not distinguishable from rounding. The two runs whose *average* obeyed the cap
(139 and 136 against 140) carry exceedances of 1.15 and 1.37 bpm and both spent
0% in zones 4-5. The smallest exceedance belonging to a run that broke the cap
on average is 4.55. 1.5 separates the two populations with margin on both sides.

**`HR_CAP_BPM_SCALE = 28.0`** — puts the F boundary at
`1.5 + 0.35 × 28 = 11.3` bpm sustained over the ceiling. Exactly three sessions
in the window sit at or above it (11.91, 13.58, 19.51) and they are exactly the
three whose zone-4+5 share reached 42% or more — the runs that stopped being
aerobic runs. The highest zone-4+5 share below the boundary is 37%.

Rank correlation against that independent signal:

| axis | Spearman ρ vs zone-4+5 |
|---|---|
| binary time fraction (0.40.0) | +0.851 |
| time fraction with a 3 bpm magnitude floor | +0.889 |
| **exceedance integral (0.40.2)** | **+0.910** |

The ρ understates it — the binary axis's correlation is carried almost entirely
by the zero/non-zero split. Among the nine non-zero cases it has no
discriminating power at all, which is why it emitted one letter.

## What changed on real data

| date | avg | z4+5 | exc bpm | old | new |
|---|---|---|---|---|---|
| 2026-07-07 | 152 | 43% | 13.58 | F | **F** |
| 2026-07-12 | 149 | 14% | 9.32 | F | D |
| 2026-07-15 | 142 | 37% | 5.48 | F | C |
| 2026-07-16 | 143 | 1% | 4.55 | F | C+ |
| 2026-07-19 | 136 | 0% | 1.37 | F | A+ |
| 2026-07-22 | 157 | 48% | 19.51 | F | **F** |
| 2026-07-26 | 149 | 42% | 11.91 | F | **F** |
| 2026-07-27 | 144 | 16% | 6.49 | F | C- |
| 2026-08-02 | 139 | 0% | 1.15 | F | A+ |

Six of nineteen grades change. The ten clean days are untouched. Six distinct
letters where there were two.

Note 2026-07-27 in particular — the session the time axis was *introduced* for
in 0.40.0. It is still penalised (C-, three letters below the obedient run's
A+), just not identically to a run that hit 185. That is the whole point.

## What I deliberately did not do

**Read `activity_hr_samples`.** The per-sample trace would make the exceedance
exact rather than split-averaged, and the table is local once fetched, so the
"no grade may depend on a network input" rule looks like it might not bite. It
does. The table holds **11 of 760 activities**. Grading off it "when present"
would make the metric mean one thing on 1.4% of history and another on the
rest — the exact availability trap the splits rule exists to prevent, and worse
than the splits case (100 of 760) it would supposedly improve on. Split
averages smooth real within-mile excursions; that is an accepted cost of a
metric that means the same thing on every row.

**Grade the time fraction on its own band table.** It would fix the unit error
without fixing the measurement: 58% of a run 1 bpm over would still outrank
30% of a run 20 bpm over. Wrong quantity, correctly graded.

**Delete the time axis.** 2026-07-22 grades `(157-140)/140` on the average
alone — a C. The split-derived axis is what makes it an F.

The fraction survives as a *reported* number: it still appears as
`time_above_cap_pct`, in the note, and in the row's parenthetical. It is the
right answer to "for how much of the run?" It was never an answer to "how bad?"

## Display

The 0.40.1 contract carries onto the new axis — the row states the quantity the
letter was measured against — and the three cells now reconcile by arithmetic:

```
| Avg HR | +19.5 bpm over cap (75% of run) | ≤ +1.5 bpm over cap | +18.0 bpm | F |
```

`19.5 − 1.5 = 18.0`. (Rounding the actual to whole bpm printed "+20" beside a
"+18" delta against a 1.5 expectation, which does not add up on the page.)

A compliant run keeps its bpm display and reads "in range":

```
| Avg HR | 139 bpm | ≤ 140 bpm | in range | A+ |
```

...with a note that reconciles the A+ against the 58% that is *still true*,
rather than stating the fraction by itself beside a passing grade:

> 58% of the run sat above the prescribed 140 bpm cap, by 1.2 bpm on average —
> inside sensor noise

A grade must never contradict the prose beside it, in either direction. 0.40.1
fixed one direction; this is the other.

## Tests

`tests/test_report_card.py`, HR-cap block rewritten. The anchors:

- `test_the_real_2026_08_02_run_is_not_a_failure` — the regression, on the
  unmodified live splits and the real active-plan prescription. Fails on `dev`
  with `assert 'F' == 'A+'`.
- `test_the_real_2026_07_22_run_still_fails_hard` — the genuine breach, also on
  unmodified live numbers. Pins F, pins the overall cap, and pins the C the
  average-only grade would have given.
- `test_the_time_fraction_alone_cannot_tell_a_1_bpm_drift_from_a_20_bpm_blowup`
  — the discrimination test. Any future severity measure has to keep those two
  sessions an order of magnitude apart.
- `test_the_f_boundary_sits_where_the_run_stopped_being_aerobic` — the
  calibration guard. Moving either constant forces the zone-4+5 comparison to be
  re-run.
- Plus: no splits (degrades to the average), no cap (falls through to the
  rolling band), zero breach, sub-noise breach, marginal, severe, and a walked
  day refusing the cap along with the rest of the plan.

`uv run pytest -x` — 2094 passed, 5 skipped. `report_card.py` at 98%, total
95.23%. `ruff check` clean.
