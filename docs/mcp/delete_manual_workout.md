# `delete_manual_workout`

> **WRITE TOOL — destructive, no undo.** Removes a manually-logged workout and recomputes the training-load model. **Availability:** stdio + HTTP

## What it does

Deletes one row that [`log_manual_workout`](log_manual_workout.md) created,
identified by its negative `activity_id`. It refuses any non-negative id, so
Garmin-sourced data can never be deleted through this surface. Referencing
`observations` are detached (not deleted) first, then the row goes and
`baselines.recompute()` runs so CTL/ATL/TSB no longer include that workout's
load. There is no edit tool and no undo — correcting a manual workout means
delete-then-relog.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `activity_id` | integer | yes | — | Must be **negative**. Coerced with `int()`; `>= 0` is rejected outright. |

## Returns

On success:

```json
{
  "deleted": true,
  "activity_id": -3,
  "note": "training load recomputed (lookback_days=90)"
}
```

If the delete committed but the recompute raised, a **partial success** — note
`deleted` is still `true`:

```json
{
  "deleted": true,
  "activity_id": -3,
  "recompute_failed": true,
  "warning": "workout deleted but training-load recompute failed; baselines may lag until the next successful sync (the nightly job, or sync_garmin_data once new Garmin data exists)",
  "error_detail": "database is locked"
}
```

Error payloads (nothing written):

- `{"error": "refusing to delete non-manual activity (id >= 0)", "activity_id": 21044837291}`
- `{"error": "no manual workout at id -9"}`

## Example

> "Scratch that lift I logged — I already had it in there."

```json
{"activity_id": -3}
```

```json
{"deleted": true, "activity_id": -3, "note": "training load recomputed (lookback_days=90)"}
```

## Gotchas

- **Deleting changes the training-load model for that date and every date
  after it.** The workout's `training_load` is removed from the daily TSS sum,
  so CTL (fitness) and ATL (fatigue) are re-run from that point forward — TSB
  (freshness) will read *higher* afterwards. If you delete a real session
  because of a typo, re-log it before reading anything into the new numbers.
- **The recompute lookback is widened to reach the deleted workout's date:**
  `lookback_days = max(90, (today - workout_date).days + 1)`. The date is read
  from the row *before* the delete, precisely so the widened window can still be
  computed.
- **Observations survive but lose their link, permanently.** Any row in
  `observations` pointing at this activity gets `activity_id = NULL` — the
  observation itself (an RPE, a soreness note) is kept. Re-logging the workout
  produces a *new*, lower negative id and does **not** re-attach anything.
- **Never blind-retry a `recompute_failed` response.** The delete is already
  committed; a retry will just return `no manual workout at id N`. The
  partial-success shape exists so the caller can tell the delete landed.
- **A deleted id can be handed out again.** `log_manual_workout` allocates
  `MIN(MIN(activity_id), 0) - 1` — one below the table's *current* minimum. If
  you delete the most-negative manual workout, the next log reclaims that same
  id. Don't cache a negative `activity_id` across a delete and assume it still
  refers to the same session.

## See also

- [`log_manual_workout`](log_manual_workout.md)
- [`query_workouts`](query_workouts.md)
