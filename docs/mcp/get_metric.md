# `get_metric`

> Raw daily values for one metric over the last N days, oldest-first. **Availability:** stdio + HTTP

## What it does

The rawest read in the surface: one column out of `daily_metrics` over a window,
with no interpretation attached. Reach for it when you need the actual series —
to eyeball specific days, count missing readings, or feed the numbers into your
own arithmetic.

When *not* to use it: if the question is "is this going up or down / is today
normal", call [`get_metric_trend`](get_metric_trend.md) instead — it returns the
slope, the baseline distance, and the deterministic `slope_direction` /
`vs_baseline` classifications rather than making you derive them. If the question
is visual, call [`chart`](chart.md) or [`generate_chart`](generate_chart.md).

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

A JSON array of `{date, value}` objects, sorted oldest-first. Nothing else — no
mean, no baseline, no interpretation.

```json
[
  {"date": "2026-07-15", "value": 52},
  {"date": "2026-07-16", "value": null},
  {"date": "2026-07-17", "value": 55},
  "…",
  {"date": "2026-07-21", "value": 54}
]
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
[
  {"date": "2026-07-14", "value": 51},
  {"date": "2026-07-15", "value": 52},
  {"date": "2026-07-16", "value": null},
  {"date": "2026-07-17", "value": 55},
  {"date": "2026-07-18", "value": 54},
  {"date": "2026-07-19", "value": 59},
  {"date": "2026-07-20", "value": 54},
  {"date": "2026-07-21", "value": 54}
]
```

## Gotchas

- **Nulls are returned, not filtered.** A day with a row but no reading comes
  back as `"value": null`. (This differs from
  [`get_metric_trend`](get_metric_trend.md), which drops nulls before computing.)
  Days with no `daily_metrics` row at all are simply absent — don't assume the
  array length equals `days`.
- **The window is `date >= today - days`, inclusive on both ends**, so a 7-day
  ask can return 8 dated rows (the cutoff day through today), as above.
- **CTL / ATL / TSB are not available here.** They live in the `baselines` table,
  not `daily_metrics`. Use [`training_load_status`](training_load_status.md) for
  the current values plus 30-day history, or [`chart`](chart.md) /
  [`generate_chart`](generate_chart.md), whose whitelists do include them.
- **Values are raw SI-ish DB units.** `sleep_seconds` is seconds, not `"7h 33m"`.
  Convert before speaking them — the coach voice never says "25200 seconds".
- **No baseline context.** "54" means nothing without the 60-day mean; pair this
  with [`daily_snapshot`](daily_snapshot.md) or use `get_metric_trend`, which
  attaches the baseline for `rhr` and `sleep_seconds`.

## See also

- [`get_metric_trend`](get_metric_trend.md) — mean, slope, baseline distance, and the classifications.
- [`chart`](chart.md) — a terminal chart of the same series.
- [`daily_snapshot`](daily_snapshot.md) — today's value with its baseline delta.
- [`training_load_status`](training_load_status.md) — for `ctl` / `atl` / `tsb`.
