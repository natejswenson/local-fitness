# `daily_snapshot`

> Today's metrics with baseline deltas and trend arrows, current CTL/ATL/TSB, the last 5 workouts, and the saved user notes. **Availability:** stdio + HTTP

## What it does

One call that returns the whole "how is today" read: every daily metric with its
60-day baseline delta or short-window trend arrow, the training-load triple, the
five most recent activities in miles/pace, the coach's saved user notes, and how
stale the newest saved brief is. Pure read — never mutates, never raises on an
empty DB.

Which one do I call?

- **`daily_snapshot`** — you want today's numbers, the recent workouts, and the
  user notes. This is the everyday snapshot.
- **[`get_brief_context`](get_brief_context.md)** — you want the *brief's* read:
  fired triggers, priority-ordered candidate takeaways, the 14-day workout list,
  RHR anomalies, and active-plan status. Bigger, and it carries no user notes.
- **[`get_metric_trend`](get_metric_trend.md)** — you want a real slope over an
  arbitrary window. This tool's `arrow` is a 7-day slope *sign* only, with no
  magnitude attached.

## Parameters

Takes no parameters.

## Returns

`assemble_status()` (`src/local_fitness/agent/status.py`) builds the payload.
Seven top-level keys:

| Key | Meaning |
|---|---|
| `date` | ISO date the snapshot is anchored to (wall-clock today for the tool). |
| `metrics` | One row per metric in `DAILY_NUMERIC_METRICS` (18 rows), sorted alphabetically. Always all 18 — `value` is `null` when the day has no reading. |
| `training_load` | `ctl` / `atl` / `tsb` from the latest `baselines` row on or before `date`, plus a plain-English `interpretation`. |
| `recent_workouts` | The last 5 activities (`date DESC, start_time DESC`), raw columns plus mile/formatted convenience fields. |
| `user_notes` | The saved coaching notes as a list of strings (`data/user_notes.md`). |
| `latest_brief_date` | ISO date of the newest file in the briefings dir, or `null` when none exist. |
| `brief_stale_days` | Days between `date` and `latest_brief_date`, floored at 0. `0` = today's brief exists; `> 0` = nightly generation has been failing; `null` = no briefs on disk. |

Each `metrics` row carries a `treatment` naming how to read it:

- `baseline_delta` — `rhr`, `sleep_seconds`, `avg_stress`, `body_battery_max`,
  `body_battery_min`. Adds `baseline` (the 60-day mean), `delta_pct`, and a
  direction `arrow` (`↑` / `↓` / `→`, pure direction, no good/bad). `delta_pct`
  and `arrow` are `null` when the value or the baseline is missing.
  `sleep_seconds` additionally carries `value_formatted` / `baseline_formatted`
  in the repo's `"7h 33m"` sleep shape.
- `trend_arrow` — `steps`, `sleep_score`, `max_stress`. Adds only `arrow`: the
  sign of a least-squares slope over the trailing 7 days. `null` with fewer than
  2 present readings. No `baseline` (`max_stress` has no baseline column at all).
- `raw` — everything else. `metric` and `value` only.

`training_load.interpretation` is `interpret.tsb_zone` — the same TSB bands
[`training_load_status`](training_load_status.md) reports, so the two agree by
construction: `very fatigued` (< -20), `fatigued` (< -10), `fresh` (> +5),
`neutral` otherwise, and `"no training-load data yet"` when there is no
baselines row.

```json
{
  "date": "2026-07-21",
  "metrics": [
    {"metric": "active_calories", "value": 812, "treatment": "raw"},
    {"metric": "avg_stress", "value": 31, "treatment": "baseline_delta",
     "baseline": 28.4, "delta_pct": 9.2, "arrow": "↑"},
    "…",
    {"metric": "rhr", "value": 54, "treatment": "baseline_delta",
     "baseline": 51.2, "delta_pct": 5.5, "arrow": "↑"},
    {"metric": "sleep_score", "value": 72, "treatment": "trend_arrow", "arrow": "↓"},
    {"metric": "sleep_seconds", "value": 25200, "treatment": "baseline_delta",
     "baseline": 27180.0, "delta_pct": -7.3, "arrow": "↓",
     "value_formatted": "7h 00m", "baseline_formatted": "7h 33m"},
    {"metric": "steps", "value": 9124, "treatment": "trend_arrow", "arrow": "→"},
    {"metric": "vo2_max", "value": 48.0, "treatment": "raw"}
  ],
  "training_load": {"ctl": 41.7, "atl": 48.2, "tsb": -6.5, "interpretation": "neutral"},
  "recent_workouts": [
    {"activity_id": 19283746501, "date": "2026-07-20", "activity_type": "running",
     "activity_name": "Morning Run", "duration_seconds": 3180,
     "distance_meters": 8046.7, "avg_hr": 141, "max_hr": 158,
     "avg_pace_sec_per_km": 395.2, "elevation_gain_meters": 62,
     "aerobic_te": 2.8, "anaerobic_te": 0.4, "training_load": 71.0,
     "distance_mi": 5.0, "pace_min_per_mi": "10:36", "duration_formatted": "53:00"},
    "…"
  ],
  "user_notes": ["Long run goes on Saturday", "…"],
  "latest_brief_date": "2026-07-21",
  "brief_stale_days": 0
}
```

## Example

> "How am I doing today?"

```
daily_snapshot()
```

```json
{
  "date": "2026-07-21",
  "metrics": [
    {"metric": "rhr", "value": 54, "treatment": "baseline_delta",
     "baseline": 51.2, "delta_pct": 5.5, "arrow": "↑"},
    {"metric": "sleep_seconds", "value": 25200, "treatment": "baseline_delta",
     "baseline": 27180.0, "delta_pct": -7.3, "arrow": "↓",
     "value_formatted": "7h 00m", "baseline_formatted": "7h 33m"},
    "…"
  ],
  "training_load": {"ctl": 41.7, "atl": 48.2, "tsb": -6.5, "interpretation": "neutral"},
  "brief_stale_days": 0
}
```

RHR 3 above baseline on 7 hours of sleep, freshness neutral — one call, no
follow-ups needed.

## Gotchas

- **`metrics` rows always exist even when the value is `null`.** All 18 metrics
  are emitted every time. A missing reading is `"value": null`, not an absent
  row — don't read row count as data coverage.
- **`value` is today's reading only.** Garmin's sync lags: if the pull hasn't
  run yet, today's row may be absent and every `value` reads `null` while the
  `baseline`s still populate. `brief_stale_days` and the trend arrows are your
  clue that the data frontier is behind.
- **`brief_stale_days > 0` means orphaned sync**: the pull advanced but the
  nightly brief generation failed. Read `logs/brief.launchd.err.log` before
  touching the Claude credential — see CLAUDE.md's failure-signature table.
- **`arrow` is direction, not judgment.** `↑` on RHR is bad, `↑` on sleep is
  good; the glyph says nothing about which.
- **The trend window is 7 days and is anchored to `date`, not wall clock.**
  Fewer than 2 present readings in that window gives `"arrow": null`.
- **`distance_mi` is suppressed entirely** when `LOCAL_FITNESS_DISPLAY_UNITS`
  isn't `miles`; `pace_min_per_mi` / `duration_formatted` are omitted per-workout
  when the underlying value is null or zero.
- **This tool IS the V1 brief loop's daily snapshot** (0.48.0). The allow-list
  used to name `get_today_status` — a byte-identical alias removed as a
  duplicate — so the grant moved here with it. The V1 prompt names this tool as
  step 1, and the two must always agree: a prompt instructing a tool the loop
  isn't granted fails silently, which is what happened for three weeks in 2026.

## See also

- [`get_brief_context`](get_brief_context.md) — the full brief read.
- [`training_load_status`](training_load_status.md) — CTL/ATL/TSB with history.
- [`get_metric_trend`](get_metric_trend.md) — a real slope over any window.
