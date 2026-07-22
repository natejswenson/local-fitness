# `commit_training_plan`

> Activate a DRAFT plan, archiving any prior active plan in the same transaction. **Availability:** stdio + HTTP

## What it does

The one draft→active transition. It flips `training_plans.status` from `draft` to `active`, stamps
`committed_at`, and archives whatever plan was active before — atomically, in one connection.

Call it once the athlete has actually agreed to the schedule you proposed. It is also the correct
way to *replace* a plan: committing a new draft archives the old active plan as part of the swap,
so you never need `abandon_active_plan` first (calling that first just leaves a window with no
active plan and no way back).

```
  propose_training_plan ──► [ DRAFT ] ──commit_training_plan──► [ ACTIVE ]
                             ▲    │       ◄── you are here       ▲     │
                             └────┘                              └─────┘
                     revise_training_plan                 update_plan_workout
                          (loop)                           (loop, ONE day)
                                  │                                    │
             discard_training_plan_draft            abandon_active_plan (NO UNDO)
                                  │                                    │
                                  ▼                                    ▼
                              [ ARCHIVED ] ◄───────────────────────────┘
```

**Only one plan can be active at a time.** That is not a convention — it is a partial unique index
in the schema:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_plan
    ON training_plans(status) WHERE status = 'active';
```

A concurrent second commit fails loudly with an `IntegrityError` rather than quietly producing two
active plans.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `plan_id` | integer | yes | — | The draft's id, as returned by `propose_training_plan` / `revise_training_plan`. Must be a real `int`. |

## Returns

```json
{"plan_id": 12, "status": "active"}
```

`status` is the literal `"active"` — the handler reports the transition it performed, it does not
re-read the row.

Errors:

```json
{"error": "no plan 12"}
```

```json
{"error": "plan 12 is 'archived', not draft"}
```

## Example

> "Yeah, that plan looks right. Let's do it."

```json
{"plan_id": 12}
```

Back:

```json
{"plan_id": 12, "status": "active"}
```

The previously active plan (if any) is now `archived`, and `get_training_plan_status` /
`get_training_plan_progress` immediately read plan 12.

## Gotchas

- **Draft-only, and the check is strict.** An already-active plan_id gives
  `plan N is 'active', not draft` — commit is not idempotent and cannot "re-commit" a live plan.
  An archived draft (one you superseded with a newer `propose_training_plan` call) is equally
  refused.
- **Committing archives the prior active plan with no confirmation step.** The old plan's rows
  survive as `archived` history, but nothing in the tool surface can bring it back — `commit`
  refuses non-drafts and there is no un-archive tool.
- **No revalidation at commit time.** Structure was validated when the draft was written. A draft
  written weeks ago whose entire schedule is in the past commits happily and immediately grades as a
  wall of `missed`.
- **`committed_at` is the local wall clock** (`datetime.now().isoformat(timespec="seconds")`), not
  the data frontier and not UTC.
- **You need the `plan_id`, and only propose/revise hand it to you.** No structured tool lists
  drafts. If you have lost it mid-session, `run_sql` can recover it:
  `SELECT plan_id, title, race_date, created_at FROM training_plans WHERE status='draft'`.

## See also

- [`propose_training_plan`](./propose_training_plan.md) — create the draft
- [`revise_training_plan`](./revise_training_plan.md) — edit it before committing
- [`discard_training_plan_draft`](./discard_training_plan_draft.md) — the other draft exit
- [`abandon_active_plan`](./abandon_active_plan.md) — stop following a plan with nothing to replace it
- [`get_training_plan_status`](./get_training_plan_status.md) — confirm the new plan is live
