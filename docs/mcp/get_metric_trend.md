# `get_metric_trend`

> Mean, least-squares slope, and current-vs-baseline stats for one metric over N days — with the direction classifications computed, and the raw daily series on request (`include_values=true`). **Availability:** stdio + HTTP

## What it does

Answers "is this metric moving, and is today normal?" in one call. It fits a
least-squares line over the present readings in the window and attaches two
deterministic interpretations from `agent/interpret.py`: `slope_direction`
(`rising` / `falling` / `flat` / `no data`) and `vs_baseline` (`elevated` /
`suppressed` / `normal` / `no data`). Per the repo rule, those judgments are
computed in tested Python — phrase them, don't re-derive them.

Pass `include_values=true` when you also need the actual day-by-day values
(the former `get_metric` tool, folded in at 0.57.0): a `values` array of
`{date, value}` rows, oldest-first, capped at the most-recent 120 rows
(`values_truncated: true` marks a cut) — `*_seconds` rows carry a
`value_formatted` companion like `"7h 33m"`; speak that, never raw seconds.
Use [`daily_snapshot`](daily_snapshot.md) when you want *today* against
baseline across all metrics at once rather than one metric over a window.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `metric` | string | yes | — | One of the 18 `daily_metrics` numeric columns (an unknown name errors and echoes the whole whitelist). |
| `days` | integer | yes | — | Integer in `[2, 3650]` — a slope is meaningless on a single sample, so the lower bound is 2, not 1. |
| `include_values` | boolean | no | `false` | Attach the raw `{date, value}` series (most-recent 120 rows max; `values_truncated: true` past the cap). |

## Returns

A flat object. `metric`, `days_window`, `n_samples`, `mean`, `current`,
`slope_per_day`, `slope_direction` and `vs_baseline` are always present; the
three baseline fields appear only for the metrics that have a baseline column.

| Key | Meaning |
|---|---|
| `metric` | Echo of the requested metric. |
| `days_window` | Echo of `days`. |
| `n_samples` | Count of **non-null** readings found in the window. |
| `mean` | Arithmetic mean of those readings, rounded to 2. |
| `current` | The most recent non-null reading (unrounded). |
| `slope_per_day` | Least-squares slope, rounded to 3. `null` when `n_samples < 2`. |
| `slope_direction` | `interpret.trend_direction` — `rising` / `falling` / `flat` / `no data`. |
| `baseline_60day_mean` | 60-day mean from the newest `baselines` row that has one. `rhr` / `sleep_seconds` only. |
| `baseline_60day_sd` | Its standard deviation. Same two metrics only. |
| `current_vs_baseline_sd` | `(current - baseline_mean) / baseline_sd`, rounded to 2. Omitted when the SD is 0 or absent. |
| `vs_baseline` | `interpret.baseline_position` — `elevated` (> +1 SD), `suppressed` (< -1 SD), `normal`, or `no data`. Always present. |
| `values` | Only with `include_values=true`: `{date, value}` oldest-first (within the 120-row cap); `*_seconds` rows add `value_formatted`. Nulls are dropped, same as the stats. |
| `values_truncated` | `true` only when the window held more than 120 rows — the cap keeps the most recent. |
| `partial_today_excluded` | `true` only for running-tally metrics (`steps`, `avg_stress`, `max_stress`, `active_calories`, intensity minutes, `body_battery_charged`/`drained`), whose window anchors on **yesterday** — today's tally is partial all day, and a slope fit against it would manufacture a false dip. Absent for point-in-time metrics. |

`flat` means the fitted *total* change across the window stays inside half a
sample SD (`interpret.TREND_FLAT_SD_MULTIPLIER`), so a noisy series doesn't get
called a trend. The `vs_baseline` bands are strict — exactly ±1.0 SD is `normal`.

```json
{
  "metric": "rhr",
  "days_window": 30,
  "n_samples": 28,
  "mean": 52.6,
  "current": 54,
  "slope_per_day": 0.081,
  "slope_direction": "rising",
  "baseline_60day_mean": 51.2,
  "baseline_60day_sd": 2.9,
  "current_vs_baseline_sd": 0.97,
  "vs_baseline": "normal"
}
```

With no readings in the window the tool errors instead of returning zeros:

```json
{"error": "no data in window", "metric": "vo2_max", "days": 5}
```

## Example

> "Is my sleep trending down?"

```
get_metric_trend(metric="sleep_seconds", days=30)
```

```json
{
  "metric": "sleep_seconds",
  "days_window": 30,
  "n_samples": 29,
  "mean": 25980.0,
  "current": 25200,
  "slope_per_day": -42.7,
  "slope_direction": "falling",
  "baseline_60day_mean": 27180.0,
  "baseline_60day_sd": 2640.0,
  "current_vs_baseline_sd": -0.75,
  "vs_baseline": "normal"
}
```

Falling over the month, but last night is still inside the normal band — say
that, don't recompute it.

## Gotchas

- **`slope_per_day` is a misnomer: it is per *observation*, not per calendar
  day.** The regression's x-axis is the sample index over the null-filtered
  series, so on a gappy metric one step is more than a day. Use `slope_direction`
  for the read; treat the raw slope as a magnitude hint only.
- **Nulls are dropped before anything is computed.** `n_samples` is the honest
  denominator — a 90-day window with 12 readings still reports a slope. Check it.
- **`baseline_*` exist for `rhr` and `sleep_seconds` only.** Those are the only
  two metrics in `tools.BASELINE_METRICS`. For every other metric
  `current_vs_baseline_sd` is absent and `vs_baseline` is `"no data"` — which is
  a missing-baseline signal, not a "value is unremarkable" signal.
- **`vs_baseline` is always in the payload**, even when it says `"no data"`.
  Don't treat its presence as evidence a baseline exists.
- **The baseline row is the newest one that has a value, ignoring the window.**
  It is not restricted to `days` and is not anchored to `current`'s date.
- **`days` must be ≥ 2.** A slope is meaningless on a single sample, so
  `days=1` is rejected with a bounds error — even with `include_values=true`.
- **CTL / ATL / TSB are not supported** — they aren't `daily_metrics` columns.
  [`training_load_status`](training_load_status.md) carries the equivalent
  `ctl_direction` classification.

## See also

- [`daily_snapshot`](daily_snapshot.md) — today vs baseline for every metric at once.
- [`training_load_status`](training_load_status.md) — the same treatment for CTL/ATL/TSB.
- [`compare_periods`](compare_periods.md) — two windows against each other, with effect size.
