# `get_training_plan_status`

> Slim status of the ACTIVE plan: goal, days to race, today's prescription, the last graded day, overall adherence. **Availability:** stdio + HTTP

## What it does

A one-call read of "is there a plan, and what does it say about right now". Deliberately small — it
carries today's prescribed session, the most recent day that actually graded, and a whole-plan
adherence percentage, and nothing else.

Call it first in a brief to decide whether to fold the plan in at all. The moment you need week
rollups, the projected finish, the goal gap, or a day-by-day list, switch to
`get_training_plan_progress` — this tool computes none of those (there is no Riegel projection on
this path).

Read-only. It never writes and never touches drafts: a draft plan is invisible here, and
`{"active": false}` means "no active plan", not "no plan exists".

```
  [ DRAFT ] ──commit──► [ ACTIVE ] ──abandon──► [ ARCHIVED ]
                            │
                            ├── get_training_plan_status    ◄── you are here (slim)
                            └── get_training_plan_progress      (day-by-day + rollups)
```

## Parameters

Takes no parameters.

## Returns

When no plan is active — the whole payload:

```json
{"active": false}
```

When a plan is active:

```json
{
  "active": true,
  "goal_type": "half",
  "race_date": "2026-10-04",
  "target_time_seconds": 6300,
  "target_time_formatted": "1:45:00",
  "days_to_race": 75,
  "adherence_pct": 83,
  "today": {
    "type": "tempo",
    "target_distance_m": null,
    "target_pace_sec_per_km": 320.0,
    "target_duration_sec": 2400,
    "description": "20 min at threshold inside a 40 min run.",
    "verdict": "pending",
    "target_pace_min_per_mi": "8:35",
    "target_duration_formatted": "40:00"
  },
  "last_graded": {
    "type": "long",
    "target_distance_m": 19312.1,
    "target_pace_sec_per_km": 385.9,
    "target_duration_sec": null,
    "description": "Long run 12 mi, easy effort.",
    "verdict": "done",
    "target_distance_mi": 12.0,
    "target_pace_min_per_mi": "10:21"
  }
}
```

| Key | Meaning |
|---|---|
| `active` | `false` ⇒ every other key is absent. Always branch on this first. |
| `goal_type` / `race_date` / `target_time_seconds` | The plan's goal, as stored. `target_time_seconds` is `null` on a "just finish" plan. |
| `target_time_formatted` | `target_time_seconds` rendered `H:MM:SS`; `null` when the goal time is. |
| `days_to_race` | `race_date − date.today()`. Negative once the race is past. `null` if either date fails to parse. |
| `adherence_pct` | Whole-plan, over graded (non-`pending`) workouts only. `done`/`compliant` = 1.0, `partial` = 0.5, `missed` = 0. `null` when nothing has graded yet. |
| `today` | The workout prescribed for `date.today()`, or `null` if the plan has no row for today. |
| `last_graded` | The most recent workout by date whose verdict is not `pending` — i.e. the last day that actually counted. `null` early in a plan. |

Both workout objects are the same slim shape: `type`, `target_distance_m`,
`target_pace_sec_per_km`, `target_duration_sec`, `description`, `verdict`, plus display fields
(`target_distance_mi` in miles mode only, `target_pace_min_per_mi`, `target_duration_formatted`)
when the underlying value exists. There are no `actual_*` fields on this path — use
`get_training_plan_progress` for what was actually run.

Verdicts are `done` | `partial` | `missed` | `compliant` (rest days) | `pending`.

## Example

> "What am I supposed to do today?"

```json
{}
```

Abridged response:

```json
{
  "active": true, "goal_type": "half", "days_to_race": 75, "adherence_pct": 83,
  "today": {"type": "tempo", "target_duration_formatted": "40:00", "verdict": "pending",
            "description": "20 min at threshold inside a 40 min run."},
  "last_graded": {"type": "long", "target_distance_mi": 12.0, "verdict": "done"}
}
```

Answer with the prescription and one line of coach read — don't narrate the lookup.

## Gotchas

- **`today` is keyed to `date.today()`, but verdicts are graded against the DATA FRONTIER.** Those
  are different clocks. A `pending` verdict on today's run does not mean "not done" — it means the
  day's data window is still open (`plans.grade_workout` holds `missed`/`partial` as `pending` at or
  after `db.last_known_daily_date()`). A completed, already-synced run grades `done` immediately,
  even today.
- **`plans.build_plan_detail` / `build_plan_status` have no "as of" date parameter.** Verdicts are
  always graded against the real data frontier, never a hypothetical past perspective. You cannot
  ask this tool "what did my plan look like last Tuesday".
- **Descriptions are truncated to 120 characters** by `plans._slim_workout` (an anti-injection
  measure). If you need the full prose, use `get_training_plan_progress`, which returns the
  untruncated column.
- **`today: null` is normal**, not an error — it just means the plan prescribes nothing for today's
  date (the plan hasn't started, has ended, or a gap day was never given a row).
- **`adherence_pct` is whole-plan, not recent.** A strong last two weeks barely moves it on a
  16-week plan. For a recent read, use `get_training_plan_progress`'s `this_week` or `plan_chart`.
- **No `predicted_finish` / `goal_gap` / `this_week` here** — those live only on
  `get_training_plan_progress`.
- **Drafts are invisible.** `{"active": false}` while a draft is sitting unopened is expected.

## See also

- [`get_training_plan_progress`](./get_training_plan_progress.md) — day-by-day, rollups, projection
- [`update_plan_workout`](./update_plan_workout.md) — change today's prescription
- [`commit_training_plan`](./commit_training_plan.md) — what made this plan active
- [`abandon_active_plan`](./abandon_active_plan.md) — what makes this return `{"active": false}`
