# `query_workouts`

> List activity rows with optional filters, most recent first. **Availability:** stdio + HTTP

## What it does

Answers "what did I run last week", "show me my long runs", "how many workouts
since the 1st" — a filtered listing of the `activities` table with the raw
columns plus mile/formatted convenience fields. Reach for this when you want
*several* workouts; use [`get_workout_detail`](get_workout_detail.md) when you
already have one `activity_id` and want its splits and HR zones. If the question
is "how did that run go" rather than "what did I do", use `workout_report_card`
— it grades one session instead of listing many.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `activity_type` | string | no | — | Substring match — compiled to SQL `activity_type LIKE '%value%'`. `'run'` matches both `running` and `treadmill_running`. |
| `days` | integer | no | — (no date filter) | Trailing window: `date >= today - days`. Bounds-checked to 1–3650; a non-int or out-of-range value is a hard error. |
| `min_distance_km` | number | no | — | `distance_meters >= value * 1000`. Kilometres, even though display units are miles. |
| `min_duration_min` | integer | no | — | `duration_seconds >= value * 60`. |
| `limit` | integer | no | `50` | Max rows. No upper cap is enforced. |

All filters are AND-ed. With no arguments at all the tool returns the 50 most
recent activities of any type.

## Returns

A JSON **array** (not an object) of workout rows, sorted `date DESC,
start_time DESC`. Each row carries these columns straight from `activities`:

`activity_id`, `date`, `activity_type`, `activity_name`, `duration_seconds`,
`distance_meters`, `avg_hr`, `max_hr`, `avg_pace_sec_per_km`,
`elevation_gain_meters`, `aerobic_te`, `anaerobic_te`, `training_load`

plus the convenience fields added by `_augment_workout` — each one present only
when it has a real value:

| Field | Meaning |
|---|---|
| `distance_mi` | `distance_meters` in miles, 2 dp. Suppressed entirely when `LOCAL_FITNESS_DISPLAY_UNITS` isn't `miles`. |
| `pace_min_per_mi` | `avg_pace_sec_per_km` rendered `"M:SS"` per mile. Omitted when pace is null or 0. |
| `duration_formatted` | `"M:SS"` under an hour, `"H:MM:SS"` at or over. |

```json
[
  {
    "activity_id": 21044837291,
    "date": "2026-07-19",
    "activity_type": "running",
    "activity_name": "Morning Run",
    "duration_seconds": 3120,
    "distance_meters": 9012.3,
    "avg_hr": 141,
    "max_hr": 163,
    "avg_pace_sec_per_km": 346.2,
    "elevation_gain_meters": 58.0,
    "aerobic_te": 3.1,
    "anaerobic_te": 0.2,
    "training_load": 88.0,
    "distance_mi": 5.6,
    "pace_min_per_mi": "9:17",
    "duration_formatted": "52:00"
  },
  …
]
```

The raw columns are never dropped in favour of the formatted ones —
[`correlate`](correlate.md) and `run_sql` depend on them.

## Example

> "What have I run in the last 10 days?"

```json
{"activity_type": "running", "days": 10}
```

```json
[
  {"activity_id": 21044837291, "date": "2026-07-19", "activity_type": "running",
   "distance_mi": 5.6, "pace_min_per_mi": "9:17", "avg_hr": 141, "training_load": 88.0},
  {"activity_id": 21038112004, "date": "2026-07-17", "activity_type": "treadmill_running",
   "distance_mi": 1.4, "pace_min_per_mi": "22:41", "avg_hr": 92, "training_load": 9.0},
  …
]
```

Note the second row: a 22:41/mi "run" at 92 bpm is a walking-desk session, not a
run. See below.

## Gotchas

- **`activity_type` is Garmin's label, not a measurement.** Nate's walking-desk
  sessions are logged by Garmin as `activity_type='treadmill_running'`, so any
  filter on the running type mixes real runs with walks. Measured on live data
  (2026-07-21): a 60-day `treadmill_running` pool held 46 activities that were
  cleanly bimodal — **16 real runs** at 8:40–11:46/mi (HR 114–172) and **30
  walking-pad sessions** at 14:08–84:20/mi (HR 76–120). Anything that averages
  or medians over the raw type filter is averaging two different regimes.
  **Gate on pace, not on type:** `report_card.RUN_PACE_CEILING_SEC_PER_MI` is
  `13 * 60` seconds per mile, and `report_card.is_running_effort()` is the
  shared predicate (`pace_sec_per_km * 1.609344 <= 780`; `None` pace ⇒ unknown,
  exclude from both pools). The live gap runs 11:46 → 14:08, so 13:00 has
  roughly two minutes of margin on either side.
- **Windows are anchored to `date.today()`, not the data frontier.** If the last
  Garmin sync was three days ago, `days=7` really covers four days of data. Call
  `sync_garmin_data` first when freshness matters.
- **Falsy arguments are silently ignored.** The handler tests `args.get(...)`
  truthiness, so `days=0`, `min_distance_km=0`, `min_duration_min=0` and
  `limit=0` all behave as "not supplied" (`limit=0` falls back to 50).
- **Manually-logged workouts are included and not labelled.** The `source`
  column isn't in the SELECT list, so the only tell that a row came from
  [`log_manual_workout`](log_manual_workout.md) is its **negative**
  `activity_id`.
- `min_distance_km` / `min_duration_min` are coerced with bare `float()` /
  `int()` — a non-numeric value raises rather than returning a clean tool error.

## See also

- [`get_workout_detail`](get_workout_detail.md)
- [`recovery_pattern`](recovery_pattern.md)
- [`compare_periods`](compare_periods.md)
