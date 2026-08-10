# `find_anomalies`

> Days where RHR or sleep sat more than N SDs off its 60-day baseline, each tagged with a signed SD distance and direction. **Availability:** stdio + HTTP

## What it does

Answers "when did my resting heart rate spike", "which nights were unusually
short". It joins `daily_metrics` to the pre-computed `baselines` table and
returns only the days that breached the threshold, with the deviation already
quantified by `agent/interpret.py`. Reach for this when the question is *which
specific days*; use [`compare_periods`](compare_periods.md) for window-vs-window
and `get_metric_trend` for drift within one window.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `metric` | string | yes | — | Enum: `"rhr"` or `"sleep_seconds"` only. These are the two metrics with 60-day mean/SD columns in `baselines`; anything else is a hard error. |
| `lookback_days` | integer | no | `90` | Trailing window, `date >= today - lookback_days`. Bounds-checked 1–3650. |
| `sd_threshold` | number | no | `2.0` | Flags rows where `ABS(value - baseline_mean) > baseline_sd * threshold`. **Strictly greater than** — exactly 2.0 SD is not an anomaly. Bounded **0.5–10**, both ends inclusive. |

`sd_threshold` is bounds-checked as of 0.37.0. Only an *omitted* value takes the
2.0 default; an explicit `0`, a negative number, or anything above 10 is a hard
error, and a non-numeric value returns a message instead of raising:

```json
{"error": "sd_threshold must be between 0.5 and 10"}
```

```json
{"error": "sd_threshold must be a number between 0.5 and 10"}
```

The bounds exist because both ends fail as *data* rather than as input: at or
below 0 every day in the window breaches, so the tool returns the entire history
as "anomalies"; above 10 nothing can breach, so it returns an empty list that
reads like a clean bill of health.

## Returns

| Key | Meaning |
|---|---|
| `metric`, `lookback_days`, `sd_threshold` | Echo of the effective inputs (after defaults). |
| `anomalies` | List of breaching days, **newest first**. Empty list when nothing breached — that is a valid, meaningful answer. |

Each anomaly row:

| Field | Meaning |
|---|---|
| `date` | ISO date. |
| `value` | The raw metric value that day (seconds for `sleep_seconds`). |
| `value_formatted` | **`sleep_seconds` only** — the `"7h 33m"` shape to speak instead of raw seconds. Absent for `rhr` (its raw value is already speakable). |
| `baseline_mean` | That day's 60-day rolling mean from `baselines`, rounded to 2 dp. |
| `baseline_formatted` | **`sleep_seconds` only** — the baseline mean as `"7h 26m"`. |
| `baseline_sd` | That day's 60-day rolling SD, rounded to 2 dp. |
| `sd_distance` | **interpret.py** — signed distance in SDs: `(value - mean) / sd`, 2 dp. |
| `direction` | **interpret.py** — `"above"` or `"below"` the baseline. |

**How to read `sd_distance`** (`interpret.sd_position`): it is *signed*, so the
sign already carries the direction and the magnitude tells you how far out the
day was. `-2.4` on `sleep_seconds` is "2.4 standard deviations short of your
usual night"; `+3.1` on `rhr` is "3.1 SDs above your normal resting heart rate".
Translate it into plain language for the user — "almost an hour shorter than
usual", not "1.76 SD below baseline". `direction` is the same judgment as a
word, so a caller never has to infer it from the sign. Both fields are omitted
(not set to `null`) on the degenerate case where `sd_position` returns `None` —
unreachable from this tool, since the SQL already requires `baseline_sd > 0`.

Note this is a *different* classifier from `interpret.baseline_position` (which
buckets an SD distance into `elevated`/`normal`/`suppressed` and is what
`get_metric_trend` attaches). `find_anomalies` returns the raw signed distance
because every row it returns is by definition already outside the normal band.

```json
{
  "metric": "rhr",
  "lookback_days": 90,
  "sd_threshold": 2.0,
  "anomalies": [
    {"date": "2026-07-11", "value": 58, "baseline_mean": 49.4,
     "baseline_sd": 2.6, "sd_distance": 3.31, "direction": "above"},
    {"date": "2026-06-02", "value": 43, "baseline_mean": 49.9,
     "baseline_sd": 2.7, "sd_distance": -2.56, "direction": "below"},
    …
  ]
}
```

For `sleep_seconds`, each row also carries the formatted companions:

```json
{
  "metric": "sleep_seconds",
  "lookback_days": 90,
  "sd_threshold": 2.0,
  "anomalies": [
    {"date": "2026-07-04", "value": 18000, "value_formatted": "5h 00m",
     "baseline_mean": 26784.33, "baseline_formatted": "7h 26m",
     "baseline_sd": 3600.67, "sd_distance": -2.44, "direction": "below"},
    …
  ]
}
```

## Example

> "Any resting-heart-rate spikes in the last three months?"

```json
{"metric": "rhr", "lookback_days": 90}
```

```json
{"metric": "rhr", "lookback_days": 90, "sd_threshold": 2.0,
 "anomalies": [
   {"date": "2026-07-11", "value": 58, "baseline_mean": 49.4, "baseline_sd": 2.6,
    "sd_distance": 3.31, "direction": "above"}
 ]}
```

One day, 3.3 SDs high — about 9 bpm above normal.

## Gotchas

- **Only `rhr` and `sleep_seconds` are supported**, because `baselines` only
  carries `*_60day_mean` / `*_60day_sd` for those two. The error is
  `only baseline-tracked metrics supported` with the allowed list.
- **Days with no baseline row are invisible, not "normal".** The query requires
  a joined `baselines` row with a non-NULL mean and `sd > 0`. Early history
  (before 60 days of data accumulated) and any gap in the baseline table simply
  can't produce an anomaly, so an empty result may mean "baselines not computed"
  rather than "nothing unusual". `sync_garmin_data` recomputes baselines when
  new data lands.
- **`sd_threshold: 0` errors; it is no longer silently replaced by 2.0.** The
  handler used to write `args.get("sd_threshold") or 2.0`, which defaulted an
  *explicit* 0 the same way it defaulted an omitted value — so asking for
  "everything off baseline at all" quietly returned the 2-SD answer. Only
  `None` (omitted) takes the default now.
- **`lookback_days: 0` still falls back to 90.** That falsy-fallback was left
  alone; only `sd_threshold` changed.
- **The baseline is *that day's* rolling window, not today's.** Each row is
  compared against the mean/SD as of its own date, so an anomaly is judged
  against what was normal at the time — the right behaviour, but it means
  `baseline_mean` varies down the list.
- **`sleep_seconds` values are seconds.** 19 800 is 5h 30m; never print the raw
  number.
- **Windows are anchored to `date.today()`**, not the data frontier.
- **Today never scans** (0.59.0). Both supported metrics settle through the
  morning — Garmin revises them as the night is processed — so a provisional
  same-day value is not a confirmed anomaly (a mid-sleep pull's rhr 54,
  revised to 50 post-wake, would have scanned as a +2 SD spike). For today's
  read use [`get_metric_trend`](get_metric_trend.md), which labels
  provisional values.

## See also

- [`correlate`](correlate.md)
- [`compare_periods`](compare_periods.md)
- [`recovery_pattern`](recovery_pattern.md)
