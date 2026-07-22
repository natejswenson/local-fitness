# `revise_training_plan`

> Edit the DRAFT plan mid-conversation: change goal fields and/or replace the workout set wholesale. **Availability:** stdio + HTTP

## What it does

The draft-side editor. It updates whitelisted goal fields on a `status='draft'` plan and, if you
pass `workouts`, atomically deletes and reinserts the entire schedule. It refuses `active` and
`archived` plans outright.

Use it while riffing on a proposal the athlete has not agreed to yet. Once the plan is **active**,
this tool is the wrong one — `update_plan_workout` is the only write path there, and it edits a
single day rather than restructuring. There is no "revise the active plan" tool by design: a
structural change to a live plan means proposing a new draft and committing it.

```
  propose_training_plan ──► [ DRAFT ] ──commit_training_plan──► [ ACTIVE ]
                             ▲    │                              ▲     │
                             └────┘                              └─────┘
                     revise_training_plan  ◄── you are here    update_plan_workout
                          (loop)                                (loop, ONE day)
                                  │                                    │
             discard_training_plan_draft            abandon_active_plan (NO UNDO)
                                  │                                    │
                                  ▼                                    ▼
                              [ ARCHIVED ] ◄───────────────────────────┘
```

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `plan_id` | integer | yes | — | The draft's id, from `propose_training_plan`'s return. Must be an `int`, not a numeric string. |
| `goal_type` | string | no | unchanged | `5k` \| `10k` \| `half` \| `full` \| `custom`. Changing it re-derives `goal_distance_m` unless you also pass one. |
| `race_date` | string | no | unchanged | ISO `YYYY-MM-DD`. |
| `target_time_seconds` | integer | no | unchanged | |
| `goal_distance_m` | number | no | unchanged / re-derived | |
| `title` | string | no | unchanged | |
| `workouts` | array | no | **unchanged** | Same item shape as `propose_training_plan`. Omit to leave the schedule alone; pass it to replace the schedule *entirely*. |

Anything outside `{goal_type, race_date, target_time_seconds, goal_distance_m, title}` is ignored by
the tool handler and, at the `plans.py` layer, rejected with
`non-editable plan field(s): [...]`. `status`, `created_at`, `committed_at`, and `plan_id` are
structurally unreachable from here.

## Returns

Same two-key shape as `propose_training_plan` — the plan is not echoed back.

```json
{"plan_id": 12, "status": "draft"}
```

`status` is a literal, not a read: this tool cannot change status, so it always reports `"draft"`.

Errors surface as MCP error results:

```json
{"error": "plan 12 is 'active', not draft"}
```

```json
{"error": "workout 3: duplicate (date, seq) ('2026-08-02', 1)"}
```

## Example

> "That's too much mileage in week 3 — cut it back and push the race a week."

```json
{
  "plan_id": 12,
  "race_date": "2026-10-11",
  "workouts": [
    {"date": "2026-07-21", "week_index": 1, "type": "easy",
     "target_distance_m": 8046.7, "description": "Easy 5 mi."},
    {"date": "2026-07-22", "week_index": 1, "type": "rest",
     "description": "Rest day."}
  ]
}
```

Back:

```json
{"plan_id": 12, "status": "draft"}
```

## Gotchas

- **`workouts` is a wholesale replacement, never a patch.** `plans.revise_draft` runs
  `DELETE FROM plan_workouts WHERE plan_id=?` then reinserts what you sent, in one transaction. If
  you send three workouts to fix week 3 of a 60-workout plan, you now have a three-workout plan.
  Resend the complete schedule every time.
- **Omitting `workouts` entirely is the safe way to touch only goal fields.** `None` means "skip";
  an empty array `[]` fails validation (`at least one workout is required`).
- **Changing `goal_type` without `goal_distance_m` re-derives the distance for you.** The handler
  fills in `plans.GOAL_DISTANCE_M[goal_type]`. This was a real bug: `revise(goal_type="half")` on a
  10k draft used to leave `goal_distance_m=10000`, so the Riegel projection predicted a 10k finish
  labelled as a half. `custom` derives nothing.
- **Revalidation only happens when you pass `workouts`.** Goal-field-only edits are not
  re-checked against the schedule, so moving `race_date` *earlier* can leave workouts dated after
  the race. Send the schedule along with a `race_date` change.
- **Validation runs before the draft check when `workouts` is present.** Calling this on an active
  plan's `plan_id` with a workout list will report a validation error first if the schedule is bad,
  and `plan N is 'active', not draft` only once the schedule is clean. Both are errors; the message
  order can be confusing.
- **Workout dates are floored at the data frontier**, same as `propose_training_plan` —
  `db.last_known_daily_date()` or today on an empty DB.
- **This tool cannot activate anything.** `commit_training_plan` is the only draft→active
  transition.

## See also

- [`propose_training_plan`](./propose_training_plan.md) — create the draft this edits
- [`commit_training_plan`](./commit_training_plan.md) — activate the draft
- [`discard_training_plan_draft`](./discard_training_plan_draft.md) — drop the draft instead
- [`update_plan_workout`](./update_plan_workout.md) — the ACTIVE-plan counterpart, one day at a time
