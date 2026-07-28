# `get_training_plan_draft`

> The DRAFT plan awaiting a decision, if one exists — the only way to see it before committing or discarding. **Availability:** stdio + HTTP

## What it does

`propose_training_plan` / `revise_training_plan` write a `status='draft'` row, but neither
`get_training_plan_status` nor `get_training_plan_progress` will ever show it — both report the
ACTIVE plan only, by design. Before this tool existed, a draft was reachable only through
`run_sql`, which meant an agent that lost track of a `plan_id` mid-session (a new chat, a
compacted context) had no structured way back to it — and `commit_training_plan` /
`discard_training_plan_draft` both require that `plan_id`. This tool is the read half of the
lifecycle the other two assume exists.

```
propose_training_plan ──► [ DRAFT ] ──commit_training_plan──► [ ACTIVE ]
                            ▲    │
                            │    └── get_training_plan_draft   ◄── you are here
                            │
                    revise_training_plan (loop)
                            │
              discard_training_plan_draft
```

Read-only. It never writes and never touches the active plan.

## Parameters

Takes no parameters.

## Returns

When no draft exists — the whole payload:

```json
{"draft": false}
```

When a draft exists:

```json
{
  "draft": true,
  "plan_id": 5,
  "title": "10K sub-48:30 — walk-supported rebuild",
  "goal_type": "10k",
  "race_date": "2026-09-18",
  "target_time_seconds": 2910,
  "target_time_formatted": "48:30",
  "days_to_race": 53,
  "created_at": "2026-07-22T09:14:03",
  "workout_count": 59,
  "workouts": [
    {
      "date": "2026-07-23",
      "seq": 1,
      "type": "easy",
      "target_distance_m": 4828.0,
      "target_pace_sec_per_km": null,
      "target_duration_sec": null,
      "description": "Easy 3 mi, walk-run as needed.",
      "verdict": null,
      "target_distance_mi": 3.0
    }
  ]
}
```

| Key | Meaning |
|---|---|
| `draft` | `false` ⇒ every other key is absent. Always branch on this first. |
| `plan_id` | Hand this straight to `commit_training_plan` or `discard_training_plan_draft`. |
| `title` / `goal_type` / `race_date` / `target_time_seconds` | The plan's goal, as stored. `target_time_seconds` is `null` on a "just finish" plan. |
| `target_time_formatted` | `target_time_seconds` rendered `H:MM:SS` (or `MM:SS` under an hour); `null` when the goal time is. |
| `days_to_race` | `race_date − date.today()`. Negative once the race date is past. `null` if either date fails to parse. |
| `created_at` | When `propose_training_plan`/`revise_training_plan` last wrote this row. |
| `workout_count` | `len(workouts)` — the fast way to answer "how big is this draft" without counting the list yourself. |
| `workouts` | Every prescribed day, oldest first. |

Each workout uses the same slim shape `get_training_plan_status` uses for `today`/`last_graded`
(`plans._slim_workout` — structured fields plus a description capped at 120 characters, an
anti-injection measure), with the same mile/pace display fields
(`get_training_plan_progress`/`get_training_plan_status`'s `_augment_plan_workout`) layered on top.
`verdict` is always `null` here — a draft has never been graded against real data, unlike the
active-plan tools where it is `done`/`partial`/`missed`/`compliant`/`pending`. There are no
`actual_*` fields for the same reason.

## Example

> "Did I ever finish deciding on that new plan?"

```json
{}
```

Abridged response:

```json
{"draft": true, "plan_id": 5, "title": "10K sub-48:30 — walk-supported rebuild",
 "days_to_race": 53, "workout_count": 59}
```

Follow with "commit it" → `commit_training_plan({"plan_id": 5})`, or "scrap it" →
`discard_training_plan_draft({"plan_id": 5})`.

## Gotchas

- **There is at most one draft at a time.** `propose_training_plan` archives any prior open draft
  before writing a new one, so `plan_id` here is unambiguous — you never have to pick among several.
- **This tool is silent about the active plan.** A `{"draft": true, ...}` response says nothing
  about whether a plan is currently active too (both can exist at once — a draft under review while
  an older plan keeps running). Call `get_training_plan_status` separately for that.
- **Descriptions are truncated to 120 characters**, same as `get_training_plan_status` (not
  `get_training_plan_progress`, which returns them untruncated) — an anti-injection measure on
  prescription text you're about to render back to the user.
- **`workouts` is never windowed.** Unlike `get_training_plan_progress`'s default 14-day/7-day
  window, every row in the draft comes back every time — a draft is reviewed as a whole before it's
  ever activated.

## See also

- [`propose_training_plan`](./propose_training_plan.md) — creates the draft this reads
- [`revise_training_plan`](./revise_training_plan.md) — edit the draft before committing
- [`commit_training_plan`](./commit_training_plan.md) — activate it
- [`discard_training_plan_draft`](./discard_training_plan_draft.md) — drop it instead
- [`get_training_plan_status`](./get_training_plan_status.md) · [`get_training_plan_progress`](./get_training_plan_progress.md) — the ACTIVE-plan equivalents
