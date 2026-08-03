# `update_plan_workouts`

> Re-prescribe MANY days on the ACTIVE training plan in one atomic call. **Availability:** stdio + HTTP

## What it does

The batch form of [`update_plan_workout`](update_plan_workout.md). One call, many days, one
transaction. Each entry takes exactly the same fields as the single-day tool, and the write boundary
is identical — this is a throughput and atomicity change, not a capability change.

Reach for it whenever you are reshaping more than one day: a week's mileage, a deload, a training
block after a race moves, or re-pacing every easy day at once.

```
  propose_training_plan ──► [ DRAFT ] ──commit_training_plan──► [ ACTIVE ]
                                                                 ▲     │
                                                                 └─────┘
                                            update_plan_workout  (ONE day)
                                            update_plan_workouts (MANY days) ◄── you are here
```

## Why it exists

Restructuring used to be a sequence of single-day calls. Measured across recorded sessions: 39
`update_plan_workout` calls, about twenty of them one restructure, and 52 fitness tool calls in a
single day.

Two costs, and the second is the real one:

- **Twenty model turns**, each carrying the full tool schemas and persona.
- **Twenty independent transactions.** The documented "move Saturday's long run to Sunday" idiom is
  *two* calls — rest the old day, prescribe the new one. If the second failed, the long run was
  simply gone. Nothing rolled back, because nothing was a transaction.

One batch is one transaction. It lands whole or not at all.

> **This is not a latency fix.** Twenty sequential calls cost ~75 ms of *our* time end to end
> (~3.8 ms each); the twenty LLM turns dominate by three orders of magnitude. The win is turns,
> tokens and atomicity — do not justify this tool on database performance.

## All-or-nothing, and what that actually guarantees

Validation happens in three passes, all before any row changes:

1. **Per-entry shape** — every entry's fields go through the same `_prescription_fields` the
   single-day tool uses (identical unit conversions, identical 3:00–30:00/mi and 90–210 bpm bounds).
2. **Whitelist + types + duplicates** — `plans.update_active_workouts` re-checks every field against
   `_EDITABLE_WORKOUT_COLS`, validates `type` against `WORKOUT_TYPES`, and rejects a batch naming the
   same `(date, seq)` twice (last-wins would be silent and order-dependent).
3. **Existence pre-flight** — every `(date, seq)` is confirmed present on the active plan with a
   `SELECT` before the first `UPDATE`. A typo'd date fails the batch instead of half-applying it.

Only then do the writes run, inside one `db.connect()` — which commits on clean exit and rolls back
on any exception.

Every error message names the offending entry by index and date:

```
update 13 (2026-09-04): no workout on 2026-09-04 (seq 1) in the active plan —
this tool re-prescribes existing days, it cannot add one
```

## The write boundary

Identical to the single-day path — same whitelist, same keyed `UPDATE`:

```sql
UPDATE plan_workouts SET <whitelisted cols>
 WHERE plan_id=:plan_id AND date=:date AND seq=:seq
```

`date` is the **key**, not an editable column. So this tool — batch or not — can never add a day,
move one, delete one, re-key a workout, change a plan's status, or restructure a schedule. A batch
is many re-prescriptions, never a restructure.

For an actual structural change, use [`propose_training_plan`](propose_training_plan.md) and
[`commit_training_plan`](commit_training_plan.md). Do not walk an active plan into a new shape,
whether one call at a time or sixty at once.

## Parameters

| Name | Type | Required | Notes |
|---|---|---|---|
| `updates` | array | yes | 1–60 entries. Each is the same object [`update_plan_workout`](update_plan_workout.md) takes. |

Each entry:

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `date` | string | yes | — | ISO `YYYY-MM-DD` of a day that **already exists** on the active plan. |
| `type` | string | no | unchanged | `easy` \| `long` \| `tempo` \| `interval` \| `rest` \| `race` \| `cross`. |
| `distance_mi` | number | no | unchanged | Miles → metres. |
| `pace_min_per_mi` | string \| number | no | unchanged | **`"M:SS"` preferred** (`"9:39"`). A bare number is *decimal minutes* — `9.65` is 9:39/mi, `9.39` is 9:23/mi. |
| `duration_min` | number | no | unchanged | Minutes. The **graded** field for `tempo`/`interval`. |
| `hr_max` | number | no | unchanged | Prescribed HR **ceiling**, bpm, bounded 90–210. Pass it whenever the day has a cap — prose in `description` is invisible to the grader. |
| `description` | string | no | unchanged | Prose prescription. |
| `seq` | integer | no | `1` | 1 = first/AM, 2 = second/PM. |

Each entry needs at least one field beyond `date`, and `type: "rest"` clears distance, pace,
duration **and** `hr_max` exactly as it does on the single-day tool — that behaviour now lives in
`plans.apply_rest_semantics` at the write boundary, so both tools share one definition.

The 60-entry cap is a blast-radius bound, not a performance one: the live active plan is 75
workouts, and one malformed call should not be able to rewrite a whole plan.

## Returns

```json
{
  "updated": 3,
  "workouts": [
    {"date": "2026-08-08", "type": "easy", "seq": 1, "distance_mi": 4.0,
     "pace_min_per_mi": "10:28", "target_hr_max": 140.0,
     "description": "Recovery 4mi."},
    {"date": "2026-08-09", "type": "rest", "seq": 1, "description": "Rest day"},
    {"date": "2026-08-10", "type": "long", "seq": 1, "distance_mi": 9.0,
     "pace_min_per_mi": "9:23", "description": "Long run 9mi @ easy-steady."}
  ]
}
```

Every written row is echoed, so a twenty-day restructure can be confirmed from this one result
without a follow-up read.

## Example

Move Saturday's long run to Sunday — two entries, one transaction, and either both land or neither
does:

```json
{"updates": [
  {"date": "2026-08-15", "type": "rest"},
  {"date": "2026-08-16", "type": "long", "distance_mi": 9,
   "pace_min_per_mi": "9:23", "description": "Long run 9mi @ easy-steady."}
]}
```

Both days must already be on the plan. If 2026-08-16 isn't, the batch fails and Saturday's long run
is still there — which is the whole point.

## Gotchas

- **It cannot add or move a day.** A "move" is still rest-the-old + prescribe-the-new, and the new
  day must already exist. Batching does not change that; it only makes the pair atomic.
- **A duplicate `(date, seq)` in one batch is an error**, not last-wins. Two entries for the same day
  means the caller lost track of its own intent.
- **`hr_max` is not inferred from prose.** Writing "keep HR under 140" in `description` for twenty
  days still leaves twenty ungraded caps. Pass `hr_max` on each entry.
- **A rolled-back batch writes nothing at all** — including the entries that were individually fine.
  That is the contract, but it means a 60-entry batch with one bad date reports one error and no
  progress. Fix the entry and resend the whole batch.
- **Not a restructure tool.** If you find yourself batching a change to most of the plan, you want
  `propose_training_plan`.

## See also

- [`update_plan_workout`](update_plan_workout.md) — the single-day form; same fields, same boundary
- [`get_training_plan_progress`](get_training_plan_progress.md) — read the plan before reshaping it
- [`propose_training_plan`](propose_training_plan.md) — a structurally different plan (a draft)
- [`get_training_plan_status`](get_training_plan_status.md) — goal, adherence, and any `pending_draft`
