# `recovery_pattern`

> How many days it typically takes body battery and RHR to return to baseline after a matching workout. **Availability:** stdio + HTTP

## What it does

Answers "how long does a long run knock me down for", "do I bounce back faster
from short runs than from 10-milers". It selects activities matching the
filters, then for each one walks the following 1–7 days looking for the first
day body-battery max recovers to **≥ 95%** of its 60-day baseline and the first
day RHR falls back to **≤ 103%** of its 60-day baseline. Reach for this over
[`find_anomalies`](find_anomalies.md) when the question is causal-shaped
("after X, how long…") rather than "which days were odd"; reach for
[`query_workouts`](query_workouts.md) when you just want the workout list.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `activity_type` | string | no | — | Substring match — SQL `activity_type LIKE '%value%'`. |
| `min_distance_km` | number | no | — | `distance_meters >= value * 1000`. Kilometres. |
| `min_duration_min` | integer | no | — | `duration_seconds >= value * 60`. |
| `lookback_days` | integer | no | `365` | `date >= today - lookback_days`. Bounds-checked 1–3650. |

All filters AND together. With no arguments the tool looks at every activity in
the last year.

## Returns

| Key | Meaning |
|---|---|
| `n_workouts_matched` | Count of workouts that matched the filters **and** had a usable baseline row on their own date (see gotchas — this is not the raw filter count). |
| `n_skipped_no_baseline` | (0.36.0) Workouts that cleared the filters but were dropped for want of a usable baseline row — so "3 matched" is readable: 3 of 3 and 3 of 40 no longer print identically. |
| `avg_recovery_days_body_battery` | Mean days to body-battery recovery across matched workouts that recovered inside 7 days, 2 dp. `null` when none did. |
| `avg_recovery_days_rhr` | Same for RHR, 2 dp. `null` when none did. |
| `recent_workouts` | The **10 most recent** matched workouts, ordered **oldest → newest** (the tail of a date-ascending list). |

Each entry in `recent_workouts` carries `activity_id`, `date`, `activity_type`,
`distance_meters`, `training_load`, `aerobic_te`, `avg_pace_sec_per_km`,
`duration_seconds`, the usual augmentation fields (`distance_mi`,
`pace_min_per_mi`, `duration_formatted`), plus:

| Field | Meaning |
|---|---|
| `recovery_days_to_bb_baseline` | Days after the workout until `body_battery_max >= baseline * 0.95`. `null` = never got there within 7 days (or no data on those days). |
| `recovery_days_to_rhr_baseline` | Days after the workout until `rhr <= baseline * 1.03`. `null` = same. |

```json
{
  "n_workouts_matched": 34,
  "avg_recovery_days_body_battery": 1.72,
  "avg_recovery_days_rhr": 1.24,
  "recent_workouts": [
    {"activity_id": 20981220117, "date": "2026-07-05", "activity_type": "running",
     "distance_meters": 19312.1, "training_load": 168.0, "aerobic_te": 3.9,
     "distance_mi": 12.0, "pace_min_per_mi": "9:44", "duration_formatted": "1:56:48",
     "recovery_days_to_bb_baseline": 2, "recovery_days_to_rhr_baseline": 1},
    …
  ]
}
```

**Note what this tool does *not* return.** Unlike
[`compare_periods`](compare_periods.md), [`correlate`](correlate.md) and
[`find_anomalies`](find_anomalies.md), `recovery_pattern` attaches **no**
`agent/interpret.py` classifier — no `magnitude`, no `strength`, no
`sd_distance`. The recovery-day counts are themselves the deterministic
judgment (thresholds fixed in code at 0.95× and 1.03× baseline), so there is no
band label to apply on top. Report the day counts; don't invent a severity word
for them.

## Example

> "How long does it take me to recover from a run over 10 miles?"

```json
{"activity_type": "running", "min_distance_km": 16, "lookback_days": 365}
```

```json
{"n_workouts_matched": 11,
 "avg_recovery_days_body_battery": 2.09,
 "avg_recovery_days_rhr": 1.5,
 "recent_workouts": [
   {"date": "2026-07-05", "distance_mi": 12.0, "training_load": 168.0,
    "recovery_days_to_bb_baseline": 2, "recovery_days_to_rhr_baseline": 1},
   …
 ]}
```

## Gotchas

- **`n_workouts_matched` is not the number of workouts your filter matched.**
  Any workout whose own date has no `baselines` row — or whose
  `body_battery_max_60day_mean` is NULL — is skipped entirely before it's
  counted. Early history and baseline gaps therefore vanish silently. If the
  number looks low against [`query_workouts`](query_workouts.md) with the same
  filters, that's why.
- **`null` recovery is ambiguous and the averages exclude it.** A `null` means
  "did not reach the threshold within 7 days" *or* "no `daily_metrics` row on
  those days". Both are dropped from `avg_recovery_days_*` (the averages filter
  on truthiness), which biases the means **optimistically** — the workouts you
  never recovered from don't drag the average up. Always report the averages
  alongside `n_workouts_matched` and the count of `null`s you can see.
- **The 7-day search window is hard-coded** (`range(1, 8)`), as are the 0.95×
  body-battery and 1.03× RHR thresholds. Nothing here is tunable by parameter.
- **`activity_type` is Garmin's label, not a measurement.** A `'running'`
  substring filter also matches `treadmill_running`, which is what Nate's
  walking-desk sessions log as — in one 60-day window that pool was 16 real runs
  against 30 walking-pad sessions. Recovery after a 20-minute walk is not
  recovery after a run, so pair the type filter with `min_distance_km` /
  `min_duration_min`, or filter by pace in [`query_workouts`](query_workouts.md)
  first and inspect ids individually.
- **Falsy arguments are ignored.** `min_distance_km=0`, `min_duration_min=0`,
  `lookback_days=0` all read as "not supplied" (`lookback_days` falls back to
  365).
- **This is the most expensive read tool in the set.** There is no `limit`: it
  runs one baseline query plus up to seven `daily_metrics` point-queries *per
  matched workout*. A 365-day unfiltered call over a multi-year DB is hundreds
  of round trips. Narrow the filters.
- **Windows are anchored to `date.today()`**, not the data frontier.

## See also

- [`query_workouts`](query_workouts.md)
- [`find_anomalies`](find_anomalies.md)
- [`correlate`](correlate.md)
