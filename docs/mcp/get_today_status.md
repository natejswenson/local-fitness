# `get_today_status`

> The daily snapshot — today's metrics with baseline deltas / trend arrows, current CTL/ATL/TSB, recent workouts, and saved user notes. **Availability:** stdio + HTTP

## What it does

Returns exactly the same payload as [`daily_snapshot`](daily_snapshot.md): both
handlers are two lines calling `status.assemble_status()`, and they share one
description constant (`_DAILY_SNAPSHOT_DESCRIPTION` in `agent/tools.py`) so the
two can't drift. Pure read.

Which one do I call?

- **`get_today_status`** — call it when you're inside the V1 brief loop, whose
  read-only allow-list (`_READ_ONLY_TOOL_NAMES`) names this tool and *not*
  `daily_snapshot`. Everywhere else the name is just historical.
- **[`daily_snapshot`](daily_snapshot.md)** — the same thing under a name that
  says what it is. Prefer it for ad-hoc questions. Full field documentation lives
  on that page.
- **[`get_brief_context`](get_brief_context.md)** — when you want the brief's
  read: fired triggers, ranked candidate takeaways, the 14-day workout list,
  RHR anomalies, plan status. Bigger payload, and no user notes.
- **[`get_metric`](get_metric.md)** / **[`get_metric_trend`](get_metric_trend.md)**
  — when the question is about one metric over a window, not about today.

## Parameters

Takes no parameters.

## Returns

Identical to [`daily_snapshot`](daily_snapshot.md) — see that page for the
per-field reference. Top-level keys: `date`, `metrics`, `training_load`,
`recent_workouts`, `user_notes`, `latest_brief_date`, `brief_stale_days`.

```json
{
  "date": "2026-07-21",
  "metrics": [
    {"metric": "rhr", "value": 54, "treatment": "baseline_delta",
     "baseline": 51.2, "delta_pct": 5.5, "arrow": "↑"},
    {"metric": "sleep_score", "value": 72, "treatment": "trend_arrow", "arrow": "↓"},
    {"metric": "vo2_max", "value": 48.0, "treatment": "raw"},
    "…"
  ],
  "training_load": {"ctl": 41.7, "atl": 48.2, "tsb": -6.5, "interpretation": "neutral"},
  "recent_workouts": [
    {"activity_id": 19283746501, "date": "2026-07-20", "activity_type": "running",
     "duration_seconds": 3180, "distance_meters": 8046.7, "avg_hr": 141,
     "distance_mi": 5.0, "pace_min_per_mi": "10:36", "duration_formatted": "53:00", "…": "…"}
  ],
  "user_notes": ["Long run goes on Saturday"],
  "latest_brief_date": "2026-07-21",
  "brief_stale_days": 0
}
```

Each `metrics` row is one of three `treatment`s — `baseline_delta` (the five
metrics with a 60-day baseline column), `trend_arrow` (`steps`, `sleep_score`,
`max_stress`), or `raw`.

## Example

> "What's my resting heart rate doing relative to normal?"

```
get_today_status()
```

```json
{
  "date": "2026-07-21",
  "metrics": [
    {"metric": "rhr", "value": 54, "treatment": "baseline_delta",
     "baseline": 51.2, "delta_pct": 5.5, "arrow": "↑"},
    "…"
  ],
  "training_load": {"ctl": 41.7, "atl": 48.2, "tsb": -6.5, "interpretation": "neutral"}
}
```

## Gotchas

- **This and `daily_snapshot` are the same tool.** Calling both in one turn is a
  wasted round-trip — the payloads are identical, field for field.
- **The tool description says to use `get_brief_context` for "anything
  plan-/trend-related".** Half true: `get_brief_context` does carry plan status,
  but its `trends` field is a *value* subset of `snapshot`, not a slope. The
  short trend arrows are here; real trend statistics are in
  [`get_metric_trend`](get_metric_trend.md).
- Every gotcha on [`daily_snapshot`](daily_snapshot.md) applies verbatim — null
  values in `metrics` rows, direction-only arrows, `brief_stale_days` as the
  orphaned-sync signal, and the `LOCAL_FITNESS_DISPLAY_UNITS` gate on
  `distance_mi`.

## See also

- [`daily_snapshot`](daily_snapshot.md) — the same payload, better name, full reference.
- [`get_brief_context`](get_brief_context.md) — the full brief read.
- [`training_load_status`](training_load_status.md) — CTL/ATL/TSB with 30-day history.
