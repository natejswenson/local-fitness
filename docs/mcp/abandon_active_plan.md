# `abandon_active_plan`

> Archive the currently active plan, leaving the athlete with no plan at all. **Availability:** stdio + HTTP

## ⚠️ There is no undo

No tool re-activates an archived plan. `commit_training_plan` is draft-only, so once a plan is
archived it cannot be brought back through the MCP surface at all — recovering it would mean
hand-editing SQLite. The rows survive (this is a soft archive, nothing is `DELETE`d), but from the
agent's side the action is final.

**Only call it when the user explicitly asks to stop following their plan entirely.** Never
proactively, and never as a step in switching plans — `commit_training_plan` already archives the
prior active plan atomically as part of that swap, so calling this first only creates a window
where nothing is active.

## What it does

Runs a single atomic statement:

```sql
UPDATE training_plans SET status='archived' WHERE status='active' RETURNING plan_id
```

The `RETURNING` clause *is* the existence check — there is no separate `SELECT`, so no window for a
concurrent writer to race it. Afterwards `get_training_plan_status` and
`get_training_plan_progress` both return `{"active": false}`, `update_plan_workout` errors with
`no active training plan`, and the brief and PDF drop their plan sections entirely.

```
  propose_training_plan ──► [ DRAFT ] ──commit_training_plan──► [ ACTIVE ]
                             ▲    │                              ▲     │
                             └────┘                              └─────┘
                     revise_training_plan                 update_plan_workout
                          (loop)                           (loop, ONE day)
                                  │                                    │
             discard_training_plan_draft            abandon_active_plan (NO UNDO)
                                  │                    ◄── you are here │
                                  ▼                                    ▼
                              [ ARCHIVED ] ◄───────────────────────────┘
```

## Parameters

Takes no parameters. It operates on whichever plan is currently active — and there can only ever be
one, enforced by the `idx_one_active_plan` partial unique index in the schema.

## Returns

```json
{"plan_id": 12, "status": "archived"}
```

- `plan_id` — the id of the plan that was archived, read straight out of the `RETURNING` clause.
  This is the only record you get of what was dropped; nothing else surfaces it afterwards.
- `status` — the literal `"archived"`.

Error when nothing is active:

```json
{"error": "no active plan"}
```

## Example

> "I'm done with the half plan — my knee's shot, I'm not racing."

```json
{}
```

Back:

```json
{"plan_id": 12, "status": "archived"}
```

A follow-up `get_training_plan_status` now returns `{"active": false}`.

## Gotchas

- **This writes Google Calendar too, when it's configured** (0.53.0). The response carries a
  `calendar` object with `created`/`updated`/`deleted`/`unchanged` and the dates that moved —
  quote the dates, not the counts, when confirming an edit. The key is **absent** when no sync
  was attempted (no credentials, or the kill switch is off); `{"status": "error"}` means the
  plan write still succeeded and only the calendar failed, so never retry the plan edit because
  of it. Configure with [`get_plan_calendar_settings`](get_plan_calendar_settings.md).
  This is the one path that only deletes: every remaining event for the plan goes, from
  today forward. **Past events stay** — they record what was prescribed at the time. The
  calendar is not restorable by re-committing; a new plan writes new events.

- **No undo — see the banner above.** Confirm with the user before calling; do not infer intent
  from "I might skip this week" or "this plan isn't working".
- **Do not use it to swap plans.** `commit_training_plan(new_draft_id)` archives the old active plan
  in the same transaction. Abandoning first is strictly worse: it loses the plan with nothing queued
  and the commit still would have handled it.
- **Adherence history goes with it.** Rollups (`adherence_pct`, `days_to_race`, `goal_gap`,
  `this_week`) only ever describe the *active* plan, so after abandoning there is no graded history
  anywhere in the tool surface — the archived rows are only reachable via `run_sql`.
- **It says nothing about drafts.** An open draft stays a draft. If the athlete is quitting
  entirely, `discard_training_plan_draft` may also be needed.
- **The archived `plan_id` is worth reporting back to the user**, since it is the only handle for a
  manual DB recovery if they change their mind.

## See also

- [`discard_training_plan_draft`](./discard_training_plan_draft.md) — the DRAFT-side exit
- [`commit_training_plan`](./commit_training_plan.md) — the correct way to replace a plan
- [`propose_training_plan`](./propose_training_plan.md) — start over with a new plan
- [`get_training_plan_status`](./get_training_plan_status.md) — confirm nothing is active
