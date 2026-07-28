# `discard_training_plan_draft`

> Archive the DRAFT plan without activating it — the draft-side exit from the lifecycle. **Availability:** stdio + HTTP

## What it does

Sets a `status='draft'` plan to `archived`. It is the counterpart to `commit_training_plan`: both
take a draft out of the draft state, one by activating it and one by dropping it.

It refuses active and archived plans. If the athlete wants to *replace* a live plan, that is
`commit_training_plan` on a new draft (which archives the old active plan atomically); if they want
to stop training against a plan entirely, that is `abandon_active_plan`. This tool never touches the
active plan.

```
  propose_training_plan ──► [ DRAFT ] ──commit_training_plan──► [ ACTIVE ]
                             ▲    │                              ▲     │
                             └────┘                              └─────┘
                     revise_training_plan                 update_plan_workout
                          (loop)                           (loop, ONE day)
                                  │                                    │
             discard_training_plan_draft            abandon_active_plan (NO UNDO)
              ◄── you are here    │                                    │
                                  ▼                                    ▼
                              [ ARCHIVED ] ◄───────────────────────────┘
```

Only call it when the user explicitly asks to drop or reject a draft. Do not call it "to clean up"
before proposing a replacement — `propose_training_plan` already archives any open draft.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `plan_id` | integer | yes | — | The draft's id. Must be a real `int`. |

## Returns

```json
{"plan_id": 12, "status": "archived"}
```

`status` is the literal `"archived"` — the transition performed, not a re-read.

Errors:

```json
{"error": "no plan 12"}
```

```json
{"error": "plan 12 is 'active', not draft"}
```

## Example

> "Forget that plan, I'm not racing this fall."

```json
{"plan_id": 12}
```

Back:

```json
{"plan_id": 12, "status": "archived"}
```

The previously active plan (if there was one) is untouched and still active.

## Gotchas

- **Refusing the active plan is the point, not a limitation.** The status guard is folded into the
  `UPDATE`'s `WHERE` clause (`WHERE plan_id=? AND status='draft'`), so there is no
  check-then-write window in which a concurrent `commit_training_plan` could let this call archive a
  freshly-activated plan.
- **A `rowcount == 0` is disambiguated by a follow-up read on the same connection**, which is why
  you get either `no plan N` or `plan N is '<status>', not draft` rather than one vague error.
- **This is a soft delete.** The plan and its `plan_workouts` rows survive with
  `status='archived'`; nothing is `DELETE`d. But no tool re-activates an archived plan
  (`commit_training_plan` is draft-only), so treat it as final from the agent's point of view.
- **You rarely need it.** `propose_training_plan` archives any open draft on its own. The genuine
  use case is "the athlete rejected the proposal and does not want a replacement right now".

## See also

- [`commit_training_plan`](./commit_training_plan.md) — the other draft exit (activate instead)
- [`propose_training_plan`](./propose_training_plan.md) — creates drafts (and archives prior ones)
- [`revise_training_plan`](./revise_training_plan.md) — fix the draft instead of dropping it
- [`get_training_plan_draft`](./get_training_plan_draft.md) — read the draft back if you lost its `plan_id`
- [`abandon_active_plan`](./abandon_active_plan.md) — the ACTIVE-plan equivalent, with no undo
