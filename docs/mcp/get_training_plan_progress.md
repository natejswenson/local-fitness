# `get_training_plan_progress`

> Day-by-day graded progress on the ACTIVE plan, plus adherence, projected finish, goal gap, and this week's mileage. **Availability:** stdio + HTTP

## What it does

The full plan read. Every prescribed workout comes back with its graded verdict and what was
actually run that day, alongside whole-plan rollups: adherence, days to race, a Riegel-projected
finish, the gap to the goal time, and a trailing-7-day planned-vs-actual mileage summary.

Reach for `get_training_plan_status` instead when you only need today's prescription — this payload
is much larger. Reach for `plan_chart` when the ask is visual ("am I hitting my plan"). Never query
`plan_workouts` by hand for this.

Read-only, active-plan-only. Drafts are invisible; `{"active": false}` means no plan is active.

```
  [ DRAFT ] ──commit──► [ ACTIVE ] ──abandon──► [ ARCHIVED ]
                            │
                            ├── get_training_plan_status       (slim, today only)
                            └── get_training_plan_progress  ◄── you are here
```

## The `workouts` window — read this before anything else

**`workouts` is windowed by default. The rollups are not.**

By default the list covers roughly the trailing two weeks plus the coming week:

```
window_start = (data frontier)          − 14 days
window_end   = max(data frontier, today) +  7 days
```

`anchor_back` is the **data frontier** (`db.last_known_daily_date()`), so graded history stays in
view; `anchor_fwd` takes `max(frontier, today)` so today and today's prescribed workout stay in
window even when the sync is stale. On a fresh DB with no daily data at all, both fall back to
today. The window is **not** clamped to the plan's own start/end dates.

Pass `full: true` for the complete list across the whole plan. "Show my plan through today" on a
plan older than two weeks needs `full: true` — otherwise the first weeks are simply absent, which
reads as data loss rather than a window.

Every rollup — `adherence_pct`, `days_to_race`, `predicted_finish_seconds`, `goal_gap`,
`this_week` — is computed from the **full** graded workout list, never the window. They are byte-for-byte
identical whether `full` is true or false. `this_week` has its own independent trailing-7-day
window ending today, unrelated to the 14/7 one.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `full` | boolean | no | `false` | `true` ⇒ return every workout in the plan instead of the rolling window. Only affects `workouts`. |

## Returns

`{"active": false}` when nothing is active. Otherwise:

```json
{
  "active": true,
  "goal_type": "half",
  "race_date": "2026-10-04",
  "target_time_seconds": 6300,
  "target_time_formatted": "1:45:00",
  "days_to_race": 75,
  "adherence_pct": 83,
  "predicted_finish_seconds": 6612.4,
  "predicted_finish_formatted": "1:50:12",
  "goal_gap": {"gap_seconds": 312.4, "gap_pct": 4.96, "on_pace": false},
  "this_week": {"week_planned_mi": 28.5, "week_actual_mi": 24.1, "slips": 1},
  "workouts": [
    {
      "date": "2026-07-19", "seq": 1, "week_index": 1, "type": "long",
      "target_distance_m": 19312.1, "target_pace_sec_per_km": 385.9,
      "target_duration_sec": null,
      "description": "Long run 12 mi, easy effort — stay off the gas.",
      "verdict": "done",
      "actual_distance_m": 19480.0, "actual_pace_sec_per_km": 381.2,
      "actual_activity_types": ["running"],
      "target_distance_mi": 12.0, "actual_distance_mi": 12.1,
      "target_pace_min_per_mi": "10:21", "actual_pace_min_per_mi": "10:13"
    }
  ]
}
```

| Key | Meaning |
|---|---|
| `active` | `false` ⇒ every other key is absent. |
| `goal_type` / `race_date` / `target_time_seconds` / `target_time_formatted` | The plan's goal. Goal time is `null` on a "just finish" plan. |
| `days_to_race` | `race_date − date.today()`. Negative after the race. |
| `adherence_pct` | Whole plan, graded (non-`pending`) workouts only. `done`/`compliant` = 1.0, `partial` = 0.5, `missed` = 0. `null` when nothing has graded. |
| `predicted_finish_seconds` / `_formatted` | Riegel projection (`t2 = t1 · (d2/d1)^1.06`) from the fastest qualifying run in the last `riegel_lookback_days` (default 120, ≥2 km, running only) onto `goal_distance_m`. `null` if either is missing. |
| `goal_gap` | `{gap_seconds, gap_pct, on_pace}` — projected minus goal, so **positive means slower than goal**. `null` when `target_time_seconds` is missing or `<= 0`, or when there is no projection. |
| `this_week` | Trailing 7 days ending **today**: `week_planned_mi`, `week_actual_mi`, `slips` (count of `partial` + `missed`). `actual_mi` is suppressed on `pending` and `compliant` days, so an ungraded run does not inflate the actual. |
| `workouts` | Windowed unless `full: true`. See below. |

Each `workouts` entry: `date`, `seq`, `week_index`, `type`, `target_distance_m`,
`target_pace_sec_per_km`, `target_duration_sec`, `description` (untruncated), `verdict`,
`actual_distance_m`, `actual_pace_sec_per_km`, `actual_activity_types` — plus display fields when
the raw value exists: `target_distance_mi` / `actual_distance_mi` (miles mode only, omitted entirely
in km mode), `target_pace_min_per_mi` / `actual_pace_min_per_mi`, `target_duration_formatted`.
Identifiers (`plan_id`, `workout_id`, `status`, `ability_snapshot`) are deliberately dropped.

**Verdicts:** `done` | `partial` | `missed` | `compliant` (rest days) | `pending` (a negative verdict
on a day whose data window is still open — at or after the data frontier).

## Example

> "Show me my plan through today."

```json
{"full": true}
```

Abridged:

```json
{
  "active": true, "goal_type": "half", "days_to_race": 75,
  "adherence_pct": 83,
  "predicted_finish_formatted": "1:50:12",
  "goal_gap": {"gap_seconds": 312.4, "gap_pct": 4.96, "on_pace": false},
  "this_week": {"week_planned_mi": 28.5, "week_actual_mi": 24.1, "slips": 1},
  "workouts": [
    {"date": "2026-06-29", "type": "easy", "target_distance_mi": 5.0,
     "actual_distance_mi": 5.1, "verdict": "done"},
    {"date": "2026-06-30", "type": "rest", "verdict": "compliant"},
    {"date": "2026-07-01", "type": "tempo", "target_duration_formatted": "40:00",
     "verdict": "missed", "actual_distance_m": 0.0, "actual_activity_types": []}
  ]
}
```

Lead with the one-line answer (83% adherence, 5 minutes off goal pace), then a compact table — not
a dump of the workout array.

## Gotchas

- **The default window hides old plan history.** This is the single most common surprise. A
  three-month-old plan returns ~21 days of `workouts` by default. If the user asked for the whole
  plan, pass `full: true`; if you report "your plan started 2026-06-29" from a windowed list, you
  are reporting the window edge, not the plan.
- **The window anchors backwards on the DATA FRONTIER, not today.** After a five-day sync gap the
  trailing 14 days are measured from five days ago, so you get ~9 days of past plan. The forward
  edge uses `max(frontier, today)`, which is why today never falls out of the window.
- **Rollups ignore `full` entirely** — `adherence_pct`, `days_to_race`, `goal_gap`, `this_week`
  are always whole-plan (or, for `this_week`, its own trailing-7-day window ending today). Do not
  recompute adherence by hand from a windowed `workouts` list; you will get a different, wrong
  number.
- **`plans.build_plan_detail` has no "as of" date parameter.** Every verdict is graded against the
  real data frontier, never a hypothetical past perspective. There is no way to ask what the plan
  looked like a week ago.
- **`pending` ≠ missed.** `grade_workout` holds `missed`/`partial` as `pending` at or after the data
  frontier so a mid-day half-finished run doesn't count 0.5 into adherence and then self-heal. A
  synced completed run grades `done` immediately, even today.
- **`positive gap_seconds` means slower than goal.** `on_pace` is `gap_seconds <= 0`.
- **`actual_*` are foot-based (running + walking), regardless of workout type.** On a walk-only day
  `actual_pace_sec_per_km` is *walking* pace. Whether the walk *counted* is the verdict's job
  (`count_walks_easy` / `count_walks_mileage` grading config), not the actuals'.
  `actual_activity_types` (`running` / `walking` / `other`) tells you which it was.
- **`predicted_finish_seconds` is `null` for a `custom` goal without `goal_distance_m`**, and for
  any athlete with no qualifying run (≥2 km, with a pace, running-type) in the lookback window.
- **The window is not clamped to the plan.** A plan that ended last month returns an empty
  `workouts` list by default while every rollup still reports real numbers.

## See also

- [`get_training_plan_status`](./get_training_plan_status.md) — the slim, today-only read
- [`update_plan_workout`](./update_plan_workout.md) — change a day you see here
- [`propose_training_plan`](./propose_training_plan.md) · [`commit_training_plan`](./commit_training_plan.md) — how this plan got here
- [`abandon_active_plan`](./abandon_active_plan.md) — what makes this return `{"active": false}`
