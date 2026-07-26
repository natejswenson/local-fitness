# `get_metric`

> Raw daily values for one metric over the last N days, oldest-first, with the 60-day baseline attached. **Availability:** stdio + HTTP

## What it does

The rawest read in the surface: one column out of `daily_metrics` over a window.
Reach for it when you need the actual series — to eyeball specific days or feed
the numbers into your own arithmetic. Days with no reading are dropped and
reported as a `days_with_data` vs `days_window` count, so a sparse metric
doesn't pad the payload with null rows.

Since 0.37.0 the raw series no longer travels alone: for `rhr` and
`sleep_seconds` the payload carries the 60-day baseline and the latest value's
signed distance from it, and every payload carries a `vs_baseline` read. That is
the same interpretation [`get_metric_trend`](get_metric_trend.md) attaches,
mirrored exactly — the house rule is that deterministic interpretation rides
along with the numbers, and without it "is 52 high?" cost a second tool call
against the sibling.

When *not* to use it: if the question is "is this going up or down", call
`get_metric_trend` instead — it adds the least-squares slope and the
`slope_direction` classification, which this tool does not compute. If the
question is visual, call [`chart`](chart.md) or
[`generate_chart`](generate_chart.md).

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `metric` | string | yes | — | Must be one of the 18 `daily_metrics` numeric columns below. |
| `days` | integer | yes | — | Lookback window. Integer in `[1, 3650]`; booleans and non-ints are rejected. |

Allowed `metric` values (`tools.DAILY_NUMERIC_METRICS`):

`sleep_seconds`, `sleep_score`, `sleep_deep_seconds`, `sleep_rem_seconds`,
`sleep_light_seconds`, `sleep_awake_seconds`, `rhr`, `avg_stress`, `max_stress`,
`body_battery_min`, `body_battery_max`, `body_battery_charged`,
`body_battery_drained`, `steps`, `active_calories`, `vo2_max`,
`intensity_minutes_moderate`, `intensity_minutes_vigorous`.

## Returns

An object: the metric, the requested window, a `days_with_data` count, a
`values` array of `{date, value}` objects sorted oldest-first, and the baseline
block. Days with no reading (NULL) are dropped, so `days_with_data` vs
`days_window` tells you how much of the window actually had data. Still no mean
and no slope — for those, use `get_metric_trend`.

| Key | Meaning |
|---|---|
| `metric`, `days_window` | Echo of the inputs. |
| `days_with_data` | `len(values)` — rows that actually had a reading. |
| `values` | `{date, value}` oldest-first, plus `value_formatted` on duration metrics. |
| `baseline_60day_mean` | 60-day mean from the newest `baselines` row that has one. **`rhr` / `sleep_seconds` only**, and only when the window returned at least one value. |
| `baseline_60day_sd` | Its standard deviation. Same two metrics, same condition. |
| `current_vs_baseline_sd` | `(last value - baseline_mean) / baseline_sd`, rounded to 2. Omitted when the SD is 0 or absent. |
| `vs_baseline` | `interpret.baseline_position` — `elevated` (> +1 SD), `suppressed` (< -1 SD), `normal`, or `no data`. **Always present.** |

The bands are strict: exactly ±1.0 SD reads as `normal`. `current_vs_baseline_sd`
is measured against the **last** entry in `values` — the most recent reading in
the window, which is not necessarily today if the metric was missing yesterday.

```json
{
  "metric": "rhr",
  "days_window": 7,
  "days_with_data": 6,
  "values": [
    {"date": "2026-07-15", "value": 52},
    {"date": "2026-07-17", "value": 55},
    "…",
    {"date": "2026-07-21", "value": 54}
  ],
  "baseline_60day_mean": 51.2,
  "baseline_60day_sd": 2.9,
  "current_vs_baseline_sd": 0.97,
  "vs_baseline": "normal"
}
```

For a metric with no baseline columns, the three numeric fields are absent and
`vs_baseline` still comes back — as `"no data"`:

```json
{
  "metric": "steps",
  "days_window": 7,
  "days_with_data": 7,
  "values": ["…"],
  "vs_baseline": "no data"
}
```

For `*_seconds` metrics each row also carries a `value_formatted` companion (the
hours-and-minutes shape the coach voice speaks), so you never hand-convert raw
seconds:

```json
{
  "metric": "sleep_seconds",
  "days_window": 3,
  "days_with_data": 3,
  "values": [
    {"date": "2026-07-19", "value": 27180, "value_formatted": "7h 33m"},
    {"date": "2026-07-20", "value": 25200, "value_formatted": "7h 00m"},
    {"date": "2026-07-21", "value": 21600, "value_formatted": "6h 00m"}
  ]
}
```

On a bad metric name the tool returns an error payload listing the whole
whitelist; on a bad `days` it returns the bounds message:

```json
{"error": "unknown metric 'hrv'", "allowed": ["active_calories", "avg_stress", "…"]}
```

## Example

> "Show me my resting heart rate for the last week."

```
get_metric(metric="rhr", days=7)
```

```json
{
  "metric": "rhr",
  "days_window": 7,
  "days_with_data": 7,
  "values": [
    {"date": "2026-07-14", "value": 51},
    {"date": "2026-07-15", "value": 52},
    {"date": "2026-07-17", "value": 55},
    {"date": "2026-07-18", "value": 54},
    {"date": "2026-07-19", "value": 59},
    {"date": "2026-07-20", "value": 54},
    {"date": "2026-07-21", "value": 54}
  ],
  "baseline_60day_mean": 51.2,
  "baseline_60day_sd": 2.9,
  "current_vs_baseline_sd": 0.97,
  "vs_baseline": "normal"
}
```

Seven readings, the latest one about 1 SD above the 60-day mean — inside the
normal band. Phrase `vs_baseline`; don't re-derive it from the 54.

## Gotchas

- **Nulls are dropped, not returned.** A day with a row but no reading is
  omitted from `values` (matching [`get_metric_trend`](get_metric_trend.md),
  which also drops nulls). `days_with_data` counts the rows you got back; compare
  it against `days_window` to see how sparse the metric was over the window
  (`vo2_max`, for instance, only updates on run days). Don't assume
  `len(values)` equals `days`.
- **The window is `date >= today - days`, inclusive on both ends**, so a 7-day
  ask can span the cutoff day through today.
- **CTL / ATL / TSB are not available here.** They live in the `baselines` table,
  not `daily_metrics`. Use [`training_load_status`](training_load_status.md) for
  the current values plus 30-day history, or [`chart`](chart.md) /
  [`generate_chart`](generate_chart.md), whose whitelists do include them.
- **Values are raw SI-ish DB units, but `*_seconds` metrics carry a formatted
  companion.** `sleep_seconds` rows include `value_formatted` (`"7h 33m"`) —
  speak that, never the raw `25200`. Non-duration metrics have no
  `value_formatted`; their raw value is already speakable.
- **`vs_baseline: "no data"` is a missing-baseline signal, not "unremarkable".**
  It is what you get for the 16 metrics with no `*_60day_mean` column in
  `baselines`, and also for an empty window or a zero SD. Only `rhr` and
  `sleep_seconds` (`tools.BASELINE_METRICS`) can ever return a real band.
- **The baseline is the newest one on file, not the one from the window's end.**
  It is a single `ORDER BY date DESC LIMIT 1` read, so a long window's values are
  compared against today's baseline rather than each day's own. That is the
  opposite of [`find_anomalies`](find_anomalies.md), which joins each day to its
  own rolling baseline.
- **Still no mean and no slope.** The baseline block answers "is the latest
  reading normal", not "is this drifting". `get_metric_trend` is the one that
  fits a line.

## See also

- [`get_metric_trend`](get_metric_trend.md) — the same baseline block plus the mean, the least-squares slope, and `slope_direction`.
- [`chart`](chart.md) — a terminal chart of the same series.
- [`daily_snapshot`](daily_snapshot.md) — today's value with its baseline delta.
- [`training_load_status`](training_load_status.md) — for `ctl` / `atl` / `tsb`.
