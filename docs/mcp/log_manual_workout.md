# `log_manual_workout`

> **WRITE TOOL.** Inserts an activity Garmin never captured and recomputes the training-load model. **Availability:** stdio + HTTP

## What it does

Records a workout that isn't in Garmin Connect — a strength session, a class, a
run on a dead watch. The row lands in `activities` with a synthetic **negative**
`activity_id` and `source='manual'`, then `baselines.recompute()` runs so the
workout is reflected in CTL/ATL/TSB. May be backdated. This is the only write
path for activity data; the Garmin metrics themselves are read-only. To undo,
use [`delete_manual_workout`](delete_manual_workout.md) — there is no edit tool.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `activity_type` | string | yes | — | Free text, e.g. `'strength'`, `'cycling'`, `'yoga'`. Not validated against an enum. |
| `duration_min` | number | yes | — | Minutes. Must be **> 0** — rejected before any write. Stored as `duration_seconds = round(duration_min * 60)`. |
| `date` | string | no | today | ISO `YYYY-MM-DD`. May be backdated. **Must not be in the future** — rejected before any write. |
| `distance_mi` | number | no | — | Miles; converted to `distance_meters` via 1609.344. |
| `avg_hr` | integer | no | — | Stored as-is. |
| `training_load` | number | no | — | TSS-style load. **This is the field that feeds CTL/ATL/TSB** — see gotchas. |
| `name` | string | no | `"Manual <activity_type>"` | Stored as `activity_name`. |

Validation order is deliberate: date parse → duration positivity → future-date
check, all *before* the insert, so a malformed argument can never commit a row
and then raise.

## Returns

On success:

```json
{
  "logged": true,
  "activity": {
    "activity_id": -3,
    "date": "2026-07-20",
    "activity_type": "strength",
    "activity_name": "Lower body",
    "duration_seconds": 2700,
    "distance_meters": null,
    "avg_hr": 118,
    "training_load": 45.0,
    "source": "manual",
    "duration_formatted": "45:00"
  },
  "note": "training load recomputed (lookback_days=90)"
}
```

`activity` is the freshly-inserted row read back (`SELECT *`, `raw_json`
stripped) and passed through the same augmentation as
[`query_workouts`](query_workouts.md) — so `distance_mi`, `pace_min_per_mi` and
`duration_formatted` appear when they have values.

If the row committed but the baseline recompute raised, you get a
**partial success** instead — note `logged` is still `true`:

```json
{
  "logged": true,
  "activity": { … },
  "recompute_failed": true,
  "warning": "workout saved but training-load recompute failed; baselines may lag until the next successful sync (the nightly job, or sync_garmin_data once new Garmin data exists)",
  "error_detail": "database is locked"
}
```

Error payloads (nothing written): `invalid date '<x>', expected YYYY-MM-DD`,
`duration_min must be positive`, `date cannot be in the future`.

## Example

> "Log the 45-minute lift I did yesterday, roughly 45 training load."

```json
{
  "activity_type": "strength",
  "duration_min": 45,
  "date": "2026-07-20",
  "training_load": 45,
  "avg_hr": 118,
  "name": "Lower body"
}
```

```json
{"logged": true,
 "activity": {"activity_id": -3, "date": "2026-07-20", "training_load": 45.0, "source": "manual", …},
 "note": "training load recomputed (lookback_days=90)"}
```

## Gotchas

- **Omitting `training_load` means the workout contributes nothing to
  CTL/ATL/TSB.** `baselines.recompute()` sums `training_load` per date; a NULL
  contributes zero. The row will show up in `query_workouts` and in the workout
  list, but freshness/fitness will read as if you took a rest day. If you want
  the session to count, pass a number.
- **The recompute lookback is widened for backdated entries.**
  `lookback_days = max(90, (today - workout_date).days + 1)`, so a workout
  backdated 200 days rewrites its own date's baseline row and every day
  forward — not just the default 90-day window. This is why `note` echoes the
  actual `lookback_days` used.
- **Never blind-retry a `recompute_failed` response.** The activity row is
  already committed at that point. There is no dedupe on manual inserts, so a
  retry inserts a **second** workout and double-counts its load. The
  partial-success shape exists precisely so the caller can tell the row landed
  and skip the retry. Fix the underlying problem and let the next successful
  sync (or a subsequent write) recompute.
- **Ids are allocated as `MIN(MIN(activity_id), 0) - 1`** under a
  `BEGIN IMMEDIATE` transaction — first manual workout on an all-Garmin table
  gets `-1`, then `-2`, `-3`. The immediate lock is what stops two concurrent
  logs from reading the same `MIN()` and colliding on the primary key. Don't
  assume ids are contiguous or reusable.
- **Future dates are refused** because `recompute()` only walks dates `<= today`
  — a future-dated row would be stored but never feed the model.
- There is **no update tool.** Correcting a manual workout means
  [`delete_manual_workout`](delete_manual_workout.md) then logging it again,
  which yields a new (lower) negative id.

## See also

- [`delete_manual_workout`](delete_manual_workout.md)
- [`query_workouts`](query_workouts.md)
- [`get_workout_detail`](get_workout_detail.md)
