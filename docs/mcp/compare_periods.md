# `compare_periods`

> Two date ranges, one metric — means, SDs, delta, and a deterministic Cohen's *d* effect-size read. **Availability:** stdio + HTTP

## What it does

Answers "last 30 days vs the 30 before", "how much did I run this week vs last".
Two branches live behind one name: **mean/SD semantics** for daily metrics and
`training_load`, and **SUM semantics** for `distance_meters` (a period total has
no per-observation mean). Reach for this when you have two explicit windows;
use `get_metric_trend` when the question is "is this drifting" within a single
window, and `find_anomalies` when it's "which specific days were weird".

The magnitude of the difference is classified in Python
(`agent/interpret.py:effect_size`), not left to the model to eyeball.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `metric` | string | yes | — | One of `DAILY_NUMERIC_METRICS` (read from `daily_metrics`), `"training_load"` (read from `activities`), or `"distance_meters"` (read from `activities`, SUM branch). Anything else is a hard error listing the allowed set. |
| `period_a_start` | string | yes | — | ISO `YYYY-MM-DD`. **Inclusive.** Validated — see below. |
| `period_a_end` | string | yes | — | ISO `YYYY-MM-DD`. **Inclusive.** Must be on or after `period_a_start`. |
| `period_b_start` | string | yes | — | ISO `YYYY-MM-DD`. **Inclusive.** Validated — see below. |
| `period_b_end` | string | yes | — | ISO `YYYY-MM-DD`. **Inclusive.** Must be on or after `period_b_start`. |

All four dates are checked **before any query runs** (0.37.0). Each must be a
real calendar date in exactly the `YYYY-MM-DD` shape — parsed with
`date.fromisoformat`, so `2026-02-30` and `2026-13-01` are rejected, as are
`"2026-07-26T10:00"` and `"20260726"`, which `fromisoformat` itself would
otherwise accept. Then each period's start must be on or before its end.

```json
{"error": "period_a_start must be a valid YYYY-MM-DD date (got '2026-02-30')"}
```

```json
{"error": "period_b_start must be on or before period_b_end (got 2026-06-20 > 2026-05-22)"}
```

Allowed daily metrics: `sleep_seconds`, `sleep_score`, `sleep_deep_seconds`,
`sleep_rem_seconds`, `sleep_light_seconds`, `sleep_awake_seconds`, `rhr`,
`avg_stress`, `max_stress`, `body_battery_min`, `body_battery_max`,
`body_battery_charged`, `body_battery_drained`, `steps`, `active_calories`,
`vo2_max`, `intensity_minutes_moderate`, `intensity_minutes_vigorous`.

Direction convention throughout: **A minus B**. Put the recent window in A.

## Returns

### Mean/SD branch (everything except `distance_meters`)

| Key | Meaning |
|---|---|
| `metric` | Echo of the input. |
| `period_a`, `period_b` | `{n, mean, sd}` — `n` is the count of non-NULL rows in range; `mean`/`sd` are `null` when `n = 0`. Sample SD (`n-1` denominator). Rounded to 2 dp. |
| `delta_mean_a_minus_b` | `mean_a - mean_b`, 2 dp. `null` if either mean is missing. |
| `delta_pct` | **interpret.py** — `(mean_a - mean_b) / mean_b * 100`, 1 dp. `null` when `mean_b` is 0 or either mean is missing. |
| `cohens_d` | **interpret.py** — standardised difference using the pooled SD, 3 dp. `null` when either SD is 0/missing or either `n < 2`. |
| `magnitude` | **interpret.py** — plain-English band for `|cohens_d|`. |

**How to read `magnitude`** (`interpret.effect_size`, lower bounds
**inclusive**): `|d| >= 0.8` → `"large"`, `>= 0.5` → `"moderate"`,
`>= 0.2` → `"small"`, below 0.2 → `"negligible"`. This is Cohen's
conventional scale, and it is the field that separates "the number moved" from
"the number moved *meaningfully*" — a 4% RHR shift on a noisy series can be
`negligible` while a 4% shift on a tight one is `moderate`. Report the
magnitude word; don't re-derive a verdict from the raw delta.

Note the fields degrade **per-field, not whole-payload**: `delta_pct` is
computed from the two means alone and survives even when the SDs or counts
can't support a `cohens_d`.

```json
{
  "metric": "rhr",
  "period_a": {"n": 30, "mean": 48.9, "sd": 2.41},
  "period_b": {"n": 29, "mean": 51.2, "sd": 2.88},
  "delta_mean_a_minus_b": -2.3,
  "delta_pct": -4.5,
  "cohens_d": -0.867,
  "magnitude": "large"
}
```

### SUM branch (`distance_meters` only)

No `mean`/`sd`/`cohens_d`/`magnitude` — a period total has no per-observation
spread to pool.

| Key | Meaning |
|---|---|
| `metric` | `"distance_meters"`. |
| `period_a`, `period_b` | `{n, total, total_mi}` — `n` = activities with a non-NULL distance, `total` = summed metres (2 dp), `total_mi` = the same in miles (only when display units are miles). |
| `delta` | `total_a - total_b` in **metres**, 2 dp. |
| `delta_pct` | `(total_a - total_b) / total_b * 100`, 1 dp. `null` when `total_b` is 0. |

```json
{
  "metric": "distance_meters",
  "period_a": {"n": 5, "total": 48280.0, "total_mi": 30.0},
  "period_b": {"n": 4, "total": 38624.0, "total_mi": 24.0},
  "delta": 9656.0,
  "delta_pct": 25.0
}
```

## Example

> "Is my resting heart rate better this month than last?"

```json
{
  "metric": "rhr",
  "period_a_start": "2026-06-21", "period_a_end": "2026-07-20",
  "period_b_start": "2026-05-22", "period_b_end": "2026-06-20"
}
```

```json
{"metric": "rhr",
 "period_a": {"n": 30, "mean": 48.9, "sd": 2.41},
 "period_b": {"n": 29, "mean": 51.2, "sd": 2.88},
 "delta_mean_a_minus_b": -2.3, "delta_pct": -4.5,
 "cohens_d": -0.867, "magnitude": "large"}
```

Read: down 2.3 bpm (−4.5%), and the effect is **large** — not noise.

## Gotchas

- **`n: 0` now genuinely means "empty window".** Before 0.37.0 the dates went
  straight into a SQLite string comparison, so a malformed or reversed range
  quietly matched zero rows and came back as
  `{"n": 0, "mean": null, "sd": null}` — identical to a real window with no
  data, and the model read "no data" where the truth was "bad input". All four
  dates and both orderings are now validated up front, so a `null` mean is a
  fact about the data, not a typo. It is still worth checking `n` before
  reporting a mean, but you no longer have to suspect your own arguments.
- **Reversed ranges error rather than returning nothing.** `period_a_start`
  after `period_a_end` is rejected by name, so you find out which period you
  flipped.
- **Both ends are inclusive** (`date >= start AND date <= end`), so a 30-day
  window is `start` .. `start + 29`.
- **`distance_meters` sums *all* activities, with no type filter.** Walking-desk
  sessions (logged by Garmin as `activity_type='treadmill_running'`) are counted
  into "how much did I run this week". If you want run mileage specifically,
  this tool can't filter — use [`query_workouts`](query_workouts.md) and gate on
  pace (`report_card.RUN_PACE_CEILING_SEC_PER_MI`, 13:00/mi).
- **`cohens_d` is `null` on constant series.** A zero SD in either period kills
  it (no `ZeroDivisionError`, just `null`), as does `n < 2`. `delta_pct` still
  comes back.
- **`training_load` is read from `activities`, not `daily_metrics`** — it's a
  per-activity mean over the window, so rest days don't drag it down (they
  simply aren't rows). That is a different question from "average daily load".
- **The mean/SD branch has no unit conversion.** `sleep_seconds` comes back in
  seconds, `distance_meters` in metres (the SUM branch adds `total_mi`, the
  mean/SD branch adds nothing). Translate before showing a number to the user.

## See also

- [`correlate`](correlate.md)
- [`find_anomalies`](find_anomalies.md)
- [`query_workouts`](query_workouts.md)
