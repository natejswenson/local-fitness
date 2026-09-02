# `delete_observation`

> **WRITE TOOL — destructive, no undo.** Deletes one logged subjective data point from the `observations` table by its `observation_id`. **Availability:** stdio + HTTP

## What it does

Removes a single row from the `observations` DB table — a mistyped weight, a
duplicate RPE, a soreness note the user wants gone. Use it when the user asks to
drop a **logged reading**.

If instead they want to drop a durable **coaching preference** ("forget that I
said to roast me"), that is the other family entirely:
[`delete_user_note`](delete_user_note.md). Observations are dated data the coach
reads; notes are prompt-injected instructions the coach follows. Deleting the
wrong one is the classic confusion here.

To correct rather than remove a reading, delete and re-log — there is no
`update_observation`.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `observation_id` | integer | yes | — | The `observation_id` from [`log_observation`](log_observation.md)'s result or [`list_observations`](list_observations.md). Missing or non-integer returns a clean error. |

## Returns

```json
{"deleted": true, "observation_id": 118}
```

The deleted row's contents are not echoed back, so read it with
[`list_observations`](list_observations.md) first if you want to confirm to the
user exactly what's being removed.

A non-existent id returns `is_error: true` with
`{"error": "no observation at id 118"}` — the handler checks for the row before
issuing the `DELETE`, so a bad id is a clean error rather than a silent no-op.

## Example

> "Scratch that weight entry, I read the scale wrong."

```json
{"observation_id": 121}
```

```json
{"deleted": true, "observation_id": 121}
```

## Gotchas

- **No undo.** The row is gone; nothing is archived.
- **IDs are stable, and so is `delete_user_note`'s handle now.**
  `observation_id` is an autoincrement primary key, so an id you read earlier
  in the conversation still points at the same row after other deletes.
  `delete_user_note` used to be the opposite (a raw line index that shifted on
  every write); it now takes a content handle that survives the same way an
  id does — the two tools no longer need different habits.
- **Deletes one row, not a day.** "Delete today's observations" is several
  calls; list first and confirm the set.
- **Missing or non-numeric `observation_id` returns a clean error**, matching
  every neighbouring tool (`delete_coach_memory` is the same shape) — the
  handler validates before it queries. Over the real MCP transport this is
  already rejected by input-schema validation before the handler runs; the
  handler-level check matters for in-process and test callers.
- Deleting an observation never touches the linked activity. The reverse
  direction is also non-destructive: `delete_manual_workout` sets
  `observations.activity_id = NULL` instead of removing referencing rows.

## See also

- [`list_observations`](list_observations.md) — find the right `observation_id` first
- [`log_observation`](log_observation.md) — re-log a corrected value
- [`delete_user_note`](delete_user_note.md) — the *other* family: drop a coaching preference
