# `get_workout_detail`

> Every stored column for ONE activity, plus its HR-zone breakdown and lap splits. **Availability:** stdio + HTTP

## What it does

Answers "what were the splits on Saturday's long run", "how much time did I
spend in zone 4". Use it when you already have an `activity_id` — usually from
[`query_workouts`](query_workouts.md), which is where you go when you don't.
This tool reports; it does not judge. For "was that run any good", use
`workout_report_card`, which grades the same activity against the plan or a
60-day rolling median instead of handing back raw columns.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `activity_id` | integer | yes | — | Coerced with `int()`. Negative ids are valid — those are manually-logged workouts. |

## Returns

An object with three top-level keys.

| Key | Meaning |
|---|---|
| `activity` | The full `activities` row (`SELECT *`) with `raw_json` stripped, plus the same `distance_mi` / `pace_min_per_mi` / `duration_formatted` / `effort` convenience fields `query_workouts` adds. |
| `hr_zones` | Rows from `activity_hr_zones` — `{zone, seconds_in_zone}` — ordered by `zone`. Empty list when none were ingested. |
| `splits` | Rows from `activity_splits` ordered by `split_index`, each augmented the same way as `activity` (so a split gets its own `distance_mi` / `pace_min_per_mi` / `duration_formatted` / `effort`). Empty list when none were ingested. |

`effort` (`"run"` / `"walk"` / `null`) is MEASURED from pace
(`interpret.is_running_effort`), never from `activity_type` — Garmin's own
label can misreport a walk as a run (walking-desk sessions log as
`treadmill_running`).

`activity` carries the full column set: `activity_id`, `date`, `start_time`,
`activity_type`, `activity_name`, `duration_seconds`, `moving_seconds`,
`distance_meters`, `avg_hr`, `max_hr`, `avg_pace_sec_per_km`,
`elevation_gain_meters`, `elevation_loss_meters`, `calories`, `aerobic_te`,
`anaerobic_te`, `training_load`, `avg_cadence`, `vo2_max_estimate`,
`weather_temp_c`, `weather_conditions`, `source`.

```json
{
  "activity": {
    "activity_id": 21044837291,
    "date": "2026-07-19",
    "start_time": "2026-07-19T06:12:04",
    "activity_type": "running",
    "activity_name": "Morning Run",
    "duration_seconds": 3120,
    "moving_seconds": 3098,
    "distance_meters": 9012.3,
    "avg_hr": 141, "max_hr": 163,
    "avg_pace_sec_per_km": 346.2,
    "elevation_gain_meters": 58.0, "elevation_loss_meters": 55.0,
    "calories": 640, "aerobic_te": 3.1, "anaerobic_te": 0.2,
    "training_load": 88.0, "avg_cadence": 168, "vo2_max_estimate": 47.0,
    "weather_temp_c": 19.0, "weather_conditions": "Clear",
    "source": "garmin",
    "distance_mi": 5.6, "pace_min_per_mi": "9:17", "duration_formatted": "52:00"
  },
  "hr_zones": [
    {"zone": 1, "seconds_in_zone": 240},
    {"zone": 2, "seconds_in_zone": 1980},
    {"zone": 3, "seconds_in_zone": 780},
    …
  ],
  "splits": [
    {"activity_id": 21044837291, "split_index": 1, "distance_meters": 1609.3,
     "duration_seconds": 566, "avg_hr": 133, "avg_pace_sec_per_km": 351.6,
     "elevation_gain_meters": 9.0,
     "distance_mi": 1.0, "pace_min_per_mi": "9:26", "duration_formatted": "9:26"},
    …
  ]
}
```

On an unknown id the tool returns an MCP error payload:
`{"error": "activity not found", "activity_id": 999}`.

## Example

> "Break down Sunday's run mile by mile."

```json
{"activity_id": 21044837291}
```

```json
{
  "activity": {"date": "2026-07-19", "distance_mi": 5.6, "pace_min_per_mi": "9:17", "avg_hr": 141, …},
  "hr_zones": [{"zone": 2, "seconds_in_zone": 1980}, {"zone": 3, "seconds_in_zone": 780}, …],
  "splits": [
    {"split_index": 1, "distance_mi": 1.0, "pace_min_per_mi": "9:26", "avg_hr": 133},
    {"split_index": 2, "distance_mi": 1.0, "pace_min_per_mi": "9:19", "avg_hr": 139},
    …
  ]
}
```

## Gotchas

- **`splits` is empty on most of the history.** Only ~**87 of 747** activities
  have split rows: the daily-sync ingest writes them, the ZIP-backfill path
  never does. Anything older than the point daily sync took over will come back
  with `"splits": []`. This is exactly why the report card treats splits as
  presentation-only and no grade reads `activity_splits` — a splits-dependent
  judgment would be unavailable on ~88% of history and would mean different
  things on different rows. Same caveat applies here: never conclude "no splits
  ⇒ no laps", it usually means "not ingested".
- **`activity_type` is Garmin's label, not a measurement.** A walking-desk
  session logs as `treadmill_running`. If you're deciding whether this activity
  was a run, check the pace: `report_card.RUN_PACE_CEILING_SEC_PER_MI` is
  `13 * 60` sec/mi, and anything slower is a walk regardless of what the type
  column says. In one 60-day window the `treadmill_running` pool was 16 real
  runs (8:40–11:46/mi) against 30 walking-pad sessions (14:08–84:20/mi).
- **No per-sample HR trace here.** `activity_hr_samples` (fetched on demand by
  `ingest/details.py`) is not part of this payload — only the coarse
  `activity_hr_zones` totals and per-lap `avg_hr`. Per-sample traces are
  resolved only by the report-card PDF path.
- `raw_json` is deliberately popped from the response; if you need it, that's a
  `run_sql` job.

## See also

- [`query_workouts`](query_workouts.md)
- [`log_manual_workout`](log_manual_workout.md)
- [`recovery_pattern`](recovery_pattern.md)
