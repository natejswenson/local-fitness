# `plan_chart`

> Scheduled-vs-actual training-plan chart (ASCII/emoji): one bar per day or per week, with a verdict glyph per row. **Availability:** stdio + HTTP

## What it does

THE tool for "planned vs actual", "am I hitting my plan", "how's the week
going". It reads the ACTIVE plan, grades every prescribed day against real
activities, and draws two series in one bar — `█` for **on-foot** miles (run +
walk, since easy days count prescribed walking by design), `░` padding out to
the prescribed distance, so the `░` tail *is* the shortfall.
Never hand-roll matplotlib or an ASCII grid for this view. Use
[`chart`](chart.md) instead when the question is about a single metric over
time (RHR, sleep, TSB) rather than about adherence, and
[`get_training_plan_progress`](get_training_plan_progress.md) when you want the
day-by-day numbers rather than a picture.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `days` | integer | no | `14` | Trailing window ending at the **data frontier** (`db.last_known_daily_date()`), falling back to today when the DB is empty — not at the wall clock. Bounds-checked to `1..3650`. |
| `weekly` | boolean | no | auto | Auto means daily rows for `days ≤ 21` and weekly buckets above. Pass `true`/`false` to force it either way. |

## Returns

A single text content block holding the rendered chart **verbatim** — plain
text, not JSON. Errors are the exception: `{"error": "no active training
plan"}` when nothing is active, and `{"error": "no plan workouts in window",
...}` when the active plan prescribes nothing in the trailing window.

Line 1 is the title (`plan vs actual · last {days}d · {n} runs · adherence
{pct}%` — the adherence figure is whole-plan, not window-scoped). Line 2 is the
legend. Then one row per day: `MM-DD type`, verdict glyph, bar, then
`actual /planned` in miles.

```
plan vs actual · last 10d · 10 runs · adherence 94%
█ on-foot mi vs ░ short of plan · 🟩done 🟨partial 🟥missed 🟦rest ⬜pending
07-12 easy     🟩 ████████             3.6 /  3.0
07-13 easy     🟨 ███████░░            3.0 /  4.0
07-14 tempo    🟩 ███████░░░░          3.0 /  5.0
07-15 easy     🟩 █████████            4.0 /  4.0
07-16 easy     🟩 █████████            4.0 /  4.0
07-17 long     🟩 ███████████████      7.0 /  7.0
07-18 easy     🟩 ████░                2.0 /  2.5
07-19 easy     🟩 ███████              3.1 /  3.0
07-20 easy     🟩 █████████            4.0 /  4.0
07-21 interval 🟩 ████████████████████ 9.2 /  5.0
```

Weekly mode swaps the labels to `wk MM-DD`, sums planned and actual mileage per
Monday-anchored week, and re-colors the glyph from the completion ratio rather
than the per-day verdict:

```
plan vs actual · last 42d · weekly · 3 wks · adherence 94%
█ actual vs ░ planned mi · 🟩≥90% 🟨70–89% 🟥<70% of plan · 🟦rest wk
wk 07-06 🟩 ███████████████████  28.6 / 28.5
wk 07-13 🟨 ██████████████████░░ 26.2 / 29.5
wk 07-20 🟩 █████████            13.2 /  9.0
```

Two row shapes are not bars:

- A **rest** day (`type == "rest"`, or a verdict of `compliant`) renders
  `🟦 —` with no bar.
- A **duration-prescribed** day (tempo/interval given in minutes, no target
  distance) renders its actual mileage tagged `(by time)` — there is no
  planned distance to scale a bar against.

## Example

**Ask:** "am I actually hitting my plan?"

```
plan_chart(days=10)
```

Paste the block above into the reply, then the read: *"94% adherence and the
only yellow in ten days is a 3-of-4 easy day. The 07-14 tempo came in 2 miles
light, and yesterday's interval bar reads 9.2 on-foot against a prescribed 5 —
but that's run plus walking-pad miles, so check `week_run_mi` before calling it
over-cooked; the run itself may have been on target."*

## Gotchas

- **Reproduce the chart in your reply, in a fenced code block.** A chart left
  only in the tool call renders collapsed in the UI and needs a Ctrl-O to
  expand. CLAUDE.md makes this a standing requirement for every chart, every
  time: paste the output, *then* add the coach read.
- **The window ends at the data frontier, not today.** If the last Garmin sync
  was two days ago, a `days=14` window is the 14 days ending then. That is
  deliberate — grading days with no data yet would print false misses — but it
  means a stale sync silently shortens the picture. `sync_garmin_data` first if
  freshness matters.
- **Actual mileage is suppressed for `pending` and `compliant` rows**, the same
  verdict-conditional rule `weekly_rollup` (and therefore the brief PDF's plan
  table) uses. Chart and PDF agree by construction; don't "fix" a blank actual
  on a pending day.
- **Overachievement has no separate glyph.** Running past the prescription just
  produces a longer all-`█` bar (see `07-21` above: 9.2 against 5.0). The
  verdict stays 🟩 — this chart shows adherence, not restraint.
- **Weekly verdicts are ratio-derived, not the per-day grades**: ≥90% of planned
  mileage is 🟩, 70–89% 🟨, below that 🟥, and a week with no planned or actual
  mileage is a 🟦 rest week.
- **Bars are relative, not absolute.** Cell width is `max(planned, actual)
  across the window / 20`, so bar lengths are only comparable *within* one
  chart.
- **The `█` bar is ON-FOOT miles (run + walk), not run-only.** It plots
  `actual_distance_m`, which counts prescribed walking on easy/recovery days by
  design (CLAUDE.md, 0.27.0) — so the legend says "on-foot mi", not "run". The
  brief PDF's plan strip headlines run-only miles instead; when you need the
  run/walk split, read [`get_training_plan_progress`](get_training_plan_progress.md)'s
  `this_week.week_run_mi` / `week_walk_mi`.

## See also

- [`get_training_plan_progress`](get_training_plan_progress.md) — the graded
  day-by-day data this chart is drawn from.
- [`get_training_plan_status`](get_training_plan_status.md) — the slim one-day
  summary.
- [`chart`](chart.md) — single-metric ASCII/emoji charts.
- [`workout_report_card`](workout_report_card.md) — grade ONE session instead of
  the plan.
