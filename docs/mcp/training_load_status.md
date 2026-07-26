# `training_load_status`

> Current CTL/ATL/TSB — fitness, fatigue, freshness — plus 30 days of history, a TSB zone label, and the 14-day CTL change. **Availability:** stdio + HTTP

## What it does

The training-load read: how much fitness has been built, how much fatigue is
sitting on top of it, and whether the runner is fresh enough to work today. It
returns the current values, the trailing 30 days, and two deterministic
classifications from `agent/interpret.py` (`tsb_zone`, `ctl_direction`) so the
zone never has to be eyeballed off a raw float.

Reach for this over [`daily_snapshot`](daily_snapshot.md) when the question is
specifically about load — `daily_snapshot` carries today's CTL/ATL/TSB and the
same zone string, but no history and no 14-day CTL delta. Reach for
[`chart`](chart.md) with `metric="ctl"` (or `atl` / `tsb`) when the ask is
visual; [`get_metric`](get_metric.md) cannot serve these series at all.

### The three numbers, in plain English

The repo translates these on first use, every time — it's a contract, not a
style preference:

- **CTL → "fitness"** — chronic training load, a 42-day exponentially weighted
  average of activity training load. Rises slowly with consistent work, decays
  slowly with time off.
- **ATL → "fatigue"** — acute training load, the same average over 7 days.
  Spikes after a hard week, drops within days of rest.
- **TSB → "freshness"** — training stress balance, `CTL - ATL`. Positive means
  rested (fitness exceeds recent fatigue); negative means worn down.

Zone bands (`interpret.tsb_zone`, single-sourced so `daily_snapshot`, the brief
planner and this tool agree by construction):

| TSB | `tsb_zone` |
|---|---|
| > +5 | `fresh` |
| -10 … +5 | `neutral` |
| < -10 | `fatigued` |
| < -20 | `very fatigued` |

The tool's own MCP description recites those same numbers — *"TSB > 5 fresh,
-10..5 neutral, < -10 fatigued, < -20 very fatigued"* — and as of 0.37.0 that
sentence is **built from `interpret.TSB_FRESH` / `TSB_FATIGUED` /
`TSB_VERY_FATIGUED`** with an f-string rather than typed out. The numbers a
caller reads in the tool list can no longer drift from the classifier that
produced `tsb_zone` in the payload. (`correlate`'s legend string had the same
duplication removed for the same reason.) Retuning a band is a one-line edit to
`interpret.py` — but this page's table is hand-written, so update it here too.

## Parameters

Takes no parameters.

## Returns

| Key | Meaning |
|---|---|
| `current` | The newest `baselines` row with a non-null CTL: `{date, ctl, atl, tsb}`, each rounded to 2. This is `history_30d[0]`, the same object. |
| `history_30d` | Rows from the last 30 days with a non-null CTL, **newest first**. |
| `tsb_zone` | `fresh` / `neutral` / `fatigued` / `very fatigued`, from `current.tsb`. |
| `ctl_pct_change_14d` | Percent change in CTL from 14 days ago to now, rounded to 1. `null` when the 14-day-ago value is missing or zero. |
| `ctl_direction` | `interpret.delta_direction` on that percentage — `rising` / `falling` / `flat` (within ±2%) / `no data`. |
| `interpretation` | A static three-line legend defining `ctl` / `atl` / `tsb`. |

```json
{
  "current": {"date": "2026-07-21", "ctl": 41.7, "atl": 48.2, "tsb": -6.5},
  "history_30d": [
    {"date": "2026-07-21", "ctl": 41.7, "atl": 48.2, "tsb": -6.5},
    {"date": "2026-07-20", "ctl": 41.2, "atl": 47.1, "tsb": -5.9},
    "…",
    {"date": "2026-06-22", "ctl": 38.4, "atl": 35.0, "tsb": 3.4}
  ],
  "tsb_zone": "neutral",
  "ctl_pct_change_14d": 6.8,
  "ctl_direction": "rising",
  "interpretation": {
    "ctl": "chronic training load (fitness) — 42-day EWMA of activity training_load",
    "atl": "acute training load (fatigue) — 7-day EWMA",
    "tsb": "training stress balance (form) = CTL - ATL"
  }
}
```

With no baselines rows at all it errors rather than returning nulls:

```json
{"error": "no training-load data yet — call sync_garmin_data to pull activities (baselines recompute automatically once data lands)"}
```

## Example

> "How's my training load?"

```
training_load_status()
```

```json
{
  "current": {"date": "2026-07-21", "ctl": 41.7, "atl": 48.2, "tsb": -6.5},
  "tsb_zone": "neutral",
  "ctl_pct_change_14d": 6.8,
  "ctl_direction": "rising",
  "history_30d": ["…"]
}
```

Fitness up ~7% over the fortnight with fatigue running ahead of it — freshness
neutral, not dug in. Phrase the zone; don't re-derive it from the -6.5.

## Gotchas

- **`history_30d` is newest-first.** [`get_metric`](get_metric.md) is
  oldest-first. Don't chart one assuming the other's order.
- **`current` is `history_30d[0]` — the same dict object, and it is the newest
  row that has a CTL, which may not be today.** If the Garmin pull is behind,
  `current.date` lags; check it before saying "today".
- **The 14-day CTL comparison is not taken from `history_30d`.** It uses
  `brief_planner.ctl_at_or_before()` — a separate point-query with no lookback
  floor — so this tool and the brief agree by construction even when baselines
  are gappy. On a gap, the "then" value can be older than 14 days.
- **`ctl_direction` is a scalar-delta classifier, not a slope.** Its flat band is
  ±2 percentage points (`interpret.DELTA_DIRECTION_FLAT_PCT`), which is a
  different rule from [`get_metric_trend`](get_metric_trend.md)'s half-a-sample-SD
  flat band. They are deliberately different classifiers for differently shaped
  inputs.
- **Rows are filtered on `ctl IS NOT NULL`, not `tsb`.** A row with CTL but no
  TSB is included, and `tsb_zone` then reports `"no training-load data yet"` —
  a sentence, not a zone label. Handle that string.
- **Only 30 days of history.** For a longer view use [`chart`](chart.md) or
  [`generate_chart`](generate_chart.md) with `metric="ctl"`.
- **Load comes from Garmin's per-activity `training_load`.** Activities without
  it (most manual entries) contribute nothing, so a hand-logged week can look
  like a rest week.

## See also

- [`daily_snapshot`](daily_snapshot.md) — today's CTL/ATL/TSB plus every other metric.
- [`get_brief_context`](get_brief_context.md) — the same numbers as grounded values, with the fired triggers.
- [`chart`](chart.md) — plot `ctl` / `atl` / `tsb` over any window.
- [`get_metric_trend`](get_metric_trend.md) — the equivalent read for `daily_metrics` columns.
