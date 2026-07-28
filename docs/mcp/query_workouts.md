# `query_workouts`

> List activity rows with optional filters, most recent first, as `{workouts, count, truncated}`. **Availability:** stdio + HTTP

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
| `min_distance_mi` | number | no | — | **Miles** — the app's display unit. `distance_meters >= value * 1609.344`. |
| `min_distance_km` | number | no | — | **Deprecated** alias in kilometres (`value * 1000`). Still accepted; `min_distance_mi` wins if both are given. |
| `min_duration_min` | integer | no | — | `duration_seconds >= value * 60`. A non-numeric value returns `min_duration_min must be an integer` rather than raising. |
| `limit` | integer | no | `50` | Max rows, **validated 1–500**. A non-int (including `true`, `50.0`) or out-of-range value is a hard error. |

All filters are AND-ed. With no arguments at all the tool returns the 50 most
recent activities of any type.

**Distance is in miles now.** `min_distance_mi` was added in 0.37.0 because this
is a miles-display app end to end: "runs over 5 miles" sent as
`min_distance_km: 5` filtered at 5 km — 3.1 mi — and quietly returned a pile of
short walks alongside the runs. `min_distance_km` still works so nothing breaks,
but the native param is miles. Passing both is not an error; miles wins.

## Returns

An **object** — `{"workouts": [...], "count": n, "truncated": bool}`.

> **Breaking change (0.37.0).** This used to be a bare JSON array. Anything that
> indexed the result directly (`result[0]`) now needs `result["workouts"][0]`.

| Key | Meaning |
|---|---|
| `workouts` | The rows, sorted `date DESC, start_time DESC`. At most `limit` of them. |
| `count` | `len(workouts)` — what you got back, **not** how many matched. |
| `truncated` | `true` when more rows matched than `limit` returned. |

`truncated` comes from a `limit + 1` fetch: the tool asks SQLite for one row
more than it intends to return, drops the extra, and reports its existence. So
`truncated: false` is a real guarantee that you are looking at the complete
match set, and `truncated: true` means re-ask with a bigger `limit` or a
narrower filter before answering "that's all of them".

Each row in `workouts` carries these columns straight from `activities`:

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
| `effort` | `"run"` / `"walk"` / `null` — MEASURED from pace (`interpret.is_running_effort`), never from `activity_type`. Garmin's own label can misreport a walk as a run (e.g. a walking-desk session logging as `treadmill_running`) — always present, `null` only when pace is unusable. |

```json
{
  "workouts": [
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
  ],
  "count": 12,
  "truncated": false
}
```

The raw columns are never dropped in favour of the formatted ones —
[`correlate`](correlate.md) and `run_sql` depend on them.

## Example

> "What have I run in the last 10 days?"

```json
{"activity_type": "running", "days": 10}
```

```json
{
  "workouts": [
    {"activity_id": 21044837291, "date": "2026-07-19", "activity_type": "running",
     "distance_mi": 5.6, "pace_min_per_mi": "9:17", "avg_hr": 141, "training_load": 88.0},
    {"activity_id": 21038112004, "date": "2026-07-17", "activity_type": "treadmill_running",
     "distance_mi": 1.4, "pace_min_per_mi": "22:41", "avg_hr": 92, "training_load": 9.0},
    …
  ],
  "count": 6,
  "truncated": false
}
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
- **Check `truncated` before saying "that's everything".** The default `limit`
  is 50, so "show me all my runs this year" used to come back as a silently
  clipped 50 rows that read exactly like a complete answer. The flag exists to
  make that visible — treat `truncated: true` as "you have not seen the whole
  set yet".
- **`limit` is capped at 500, and `limit: -1` is now an error.** It used to
  reach SQLite as `LIMIT -1`, which SQLite reads as *no limit* — an unbounded
  dump of the whole `activities` table into model context. `limit: 0` errors
  too (it used to fall back to 50). Both come back as
  `limit must be between 1 and 500`; a non-integer gets
  `limit must be an integer between 1 and 500`.
- **Falsy `days` and `min_duration_min` are still silently ignored.** The
  handler tests `args.get(...)` truthiness for those two, so `days=0` and
  `min_duration_min=0` behave as "not supplied". `min_distance_mi`/
  `min_distance_km` no longer do — an explicit `0` is honoured as a
  `distance_meters >= 0` filter, which is a no-op but not a silent one.
- **Manually-logged workouts are included and not labelled.** The `source`
  column isn't in the SELECT list, so the only tell that a row came from
  [`log_manual_workout`](log_manual_workout.md) is its **negative**
  `activity_id`.
- **Bad filter values return errors, not tracebacks.** A non-numeric
  `min_distance_mi` / `min_distance_km` returns
  `<param> must be a number`, a negative one `<param> must be non-negative`,
  and a non-numeric `min_duration_min` returns
  `min_duration_min must be an integer`. These used to be bare `float()` /
  `int()` coercions that raised at the caller.

## See also

- [`get_workout_detail`](get_workout_detail.md)
- [`recovery_pattern`](recovery_pattern.md)
- [`compare_periods`](compare_periods.md)
