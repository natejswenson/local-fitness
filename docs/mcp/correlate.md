# `correlate`

> Pearson *r* between two daily metrics, optionally lagged, with a deterministic strength/direction read. **Availability:** stdio + HTTP

## What it does

Answers "does sleep predict my resting heart rate the next morning", "do high-
stress days follow low body-battery days". It pairs two `daily_metrics` columns
day-by-day (optionally offsetting one by `lag_days`), computes Pearson *r*, and
attaches a classified `strength` and `direction` from `agent/interpret.py`.
Reach for this when you want a *relationship across a window*; use
[`compare_periods`](compare_periods.md) when you want a *difference between two
windows*, and [`find_anomalies`](find_anomalies.md) when you want specific
outlier days.

**Correlation is not causation.** *r* says two series moved together over this
window, nothing more. A third factor (a hard training block, illness, a
travel week) can drive both. Never report a correlation as a mechanism, and
never turn one into a training instruction on its own.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `metric_a` | string | yes | — | Must be in `DAILY_NUMERIC_METRICS`; anything else is a hard error listing the allowed set. |
| `metric_b` | string | yes | — | Same whitelist. |
| `days` | integer | yes | — | Nominal window. Bounds-checked 1–3650. |
| `lag_days` | integer | no | `0` | Offset applied to **b**. Positive = b lags a (a on day *N* paired with b on day *N+lag*). Bounds-checked −365…365; negative flips which metric leads. |

Allowed metrics: `sleep_seconds`, `sleep_score`, `sleep_deep_seconds`,
`sleep_rem_seconds`, `sleep_light_seconds`, `sleep_awake_seconds`, `rhr`,
`avg_stress`, `max_stress`, `body_battery_min`, `body_battery_max`,
`body_battery_charged`, `body_battery_drained`, `steps`, `active_calories`,
`vo2_max`, `intensity_minutes_moderate`, `intensity_minutes_vigorous`.

## Returns

| Key | Meaning |
|---|---|
| `metric_a`, `metric_b`, `days`, `lag_days` | Echo of the inputs. |
| `n_pairs` | How many day-pairs actually had **both** values present. This is the sample size — read it before reading `pearson_r`. |
| `pearson_r` | Coefficient in −1…1, 3 dp. `null` only when a variance is zero (a flat series has no defined correlation). |
| `strength` | **interpret.py** — band for `|r|`. `null` when `pearson_r` is `null`. |
| `direction` | **interpret.py** — `"positive"` or `"negative"`. `null` when `pearson_r` is `null`. |

**How to read `strength`** (`interpret.correlation_read`, lower bounds
**inclusive**): `|r| >= 0.6` → `"strong"`, `>= 0.4` → `"moderate"`,
`>= 0.2` → `"modest"`, below 0.2 → `"weak"`. These are the same bands the tool
used to ship as a static legend string for the model to apply by hand; they now
come back pre-applied so the phrasing can't drift from the number.

**How to read `direction`**: `r >= 0` → `"positive"` (the two move together),
`r < 0` → `"negative"` (one rises as the other falls). Exactly `r == 0.0`
classifies as `"positive"` via the `>=`, consistent with `sd_position`'s rule —
though at `r = 0` the `strength` is `"weak"` anyway, which is the field that
matters.

A `"weak"` strength means *report no relationship*, not "a small relationship".

```json
{
  "metric_a": "sleep_seconds",
  "metric_b": "rhr",
  "days": 90,
  "lag_days": 1,
  "n_pairs": 87,
  "pearson_r": -0.412,
  "strength": "moderate",
  "direction": "negative"
}
```

Below the sample-size floor the tool returns an MCP error instead of a payload:
`{"error": "insufficient paired data", "n": 3}`.

## Example

> "Does a bad night's sleep show up in my resting heart rate the next day?"

```json
{"metric_a": "sleep_seconds", "metric_b": "rhr", "days": 90, "lag_days": 1}
```

```json
{"metric_a": "sleep_seconds", "metric_b": "rhr", "days": 90, "lag_days": 1,
 "n_pairs": 87, "pearson_r": -0.412, "strength": "moderate", "direction": "negative"}
```

Read: over 87 paired days, shorter sleep went with a higher next-morning RHR,
moderately. That is an association in this window — not proof sleep *causes*
the RHR move.

## Gotchas

- **Hard sample-size floor of 5 pairs.** Fewer than 5 usable pairs returns
  `{"error": "insufficient paired data", "n": <n>}` rather than a coefficient.
  Even above the floor, treat single-digit `n_pairs` as unreportable — always
  quote `n_pairs` alongside `r`.
- **`n_pairs` can exceed `days`.** The SQL cutoff is widened to
  `today - (days + |lag| + 1)` so lagged partners exist at the window edge, but
  pairing is **not** re-restricted to the original `days` window afterwards. A
  90-day request with `lag_days=7` can pair up to ~98 days of data. Read
  `n_pairs`, not `days`, as the true sample.
- **Windows are anchored to `date.today()`, not the data frontier.** Stale sync
  ⇒ a shorter effective window.
- **`pearson_r: null` means zero variance, not "no data".** If either series is
  constant across the window the denominator is 0, and `strength`/`direction`
  come back `null` too. That's a flat metric, not a failed query.
- **Missing days are dropped, not interpolated.** A row is paired only when
  `a_val` is non-NULL *and* the partner date exists with a non-NULL `b_val`.
- **Both series come from `daily_metrics` only.** You can't correlate a
  training-load or activity column here — that's a `run_sql` job.
- **Lag direction is easy to get backwards.** `lag_days=1` means "a on day N vs
  b on day N+1" — a predicts b. Flip the sign to ask the reverse.

## See also

- [`compare_periods`](compare_periods.md)
- [`find_anomalies`](find_anomalies.md)
- [`recovery_pattern`](recovery_pattern.md)
