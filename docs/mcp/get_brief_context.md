# `get_brief_context`

> The deterministic brief planner's typed output in one call — priority-ordered candidate takeaways with the triggers that fired, every citable number pre-rendered, the 14-day workout list, RHR anomalies, and active-plan status. **Availability:** stdio + HTTP

## What it does

This is the whole "what's today's read" payload, assembled in tested Python
rather than by an LLM orchestrating eight tool calls. `agent/brief_planner.py`
evaluates a fixed block of named thresholds (`_TRIGGERS`), over-generates up to
five candidate takeaways, ranks them by a fixed priority, attaches an *advisory*
tone to each, and hands back a typed `BriefContext`. Every number in the payload
is paired with its coach-ready display string — the generator quotes, it never
derives.

Which one do I call?

- **`get_brief_context`** — "how am I doing / what should I do today / what's
  the read". You want judgment already applied: which signals fired, what
  matters first, what the plan says. This is also the sole input to the V2 brief
  generator, so what you see here is exactly what the daily brief saw.
- **[`daily_snapshot`](daily_snapshot.md)** / **[`get_today_status`](get_today_status.md)**
  — you just want today's numbers, the last five workouts, and the user notes.
  Smaller, and it's the *only* one of the three that carries `user_notes`.
- **[`get_metric`](get_metric.md)** / **[`get_metric_trend`](get_metric_trend.md)**
  — a single-metric question. This tool is overkill for "how did I sleep".
- **[`get_training_plan_progress`](get_training_plan_progress.md)** — you want the
  plan day-by-day. `plan_today` here is a rollup, not a schedule.

## Parameters

Takes no parameters. (`brief_planner.assemble_brief_context()` accepts `db_path`,
`today`, `notes` and `recent_briefs` for in-process callers and fixtures; the MCP
handler passes none of them.)

## Returns

A `model_dump()` of `schemas.BriefContext`. Fifteen top-level keys:

| Key | Type | Meaning |
|---|---|---|
| `date` | str | ISO date the context is anchored to (wall-clock today via the tool). |
| `user_name` | str | From the `user_name` setting; `"Nate"` when unset. |
| `candidates` | list | Over-generated, priority-ordered takeaway candidates. See below. |
| `snapshot` | list[GroundedValue] | Today's present metric values plus the 60-day baseline reference values. |
| `training_load` | list[GroundedValue] | `ctl`, `atl`, `tsb` as grounded values. |
| `trends` | list[GroundedValue] | The `rhr` / `sleep_score` / `steps` / `body_battery_max` entries from `snapshot`, re-emitted. |
| `workouts_14d` | list[dict] | The actual activities of the last 14 days, newest first. |
| `anomalies` | list[dict] | RHR readings in the last 14 days more than 2 SD above the 60-day mean. |
| `continuity` | list[str] | Last-7 brief lead headlines, newest first. |
| `plan_today` | dict \| null | Active-plan status rollup, or `null` when no plan is active. |
| `step_goal` | int | The `daily_step_goal` setting; `10000` when unset or unparseable. |
| `days_to_race` | int \| null | Days from `date` to the active plan's race date. `null` with no active plan. |
| `data_frontier` | str \| null | Newest date with any `daily_metrics` row — how current the data actually is. |
| `baseline_stale_days` | int \| null | Days from the baselines row backing `training_load` to `date`. |
| `brief_stale_days` | int \| null | Days from the newest saved brief to `date`. |
| `tsb_zone` | str \| null | `interpret.tsb_zone(tsb)` — `very fatigued` / `fatigued` / `neutral` / `fresh`. |

### Freshness

The three staleness fields exist to make the *orphaned sync* visible: the pull
advances the data frontier but brief generation fails, or the pull itself
stalls and the baselines row freezes. TSB decays daily even with zero
workouts, so a stale baselines row served as current reports the wrong
freshness — `baseline_stale_days` is what tells you the CTL/ATL/TSB numbers
describe a day that has already passed. Any of them may be `null` (no data,
no briefs); `null` means "unknown", not "fresh".

`tsb_zone` is the deterministic read of `training_load`'s `tsb`, single-sourced
from `interpret.tsb_zone` so this tool, `training_load_status` and the brief
can't disagree about what "fatigued" means. It is `null` — not the string
`"no training-load data yet"` — when there is no TSB.

### GroundedValue

`snapshot`, `training_load`, `trends`, and each candidate's `metrics` are all
lists of the same shape. The union of those four lists is the exact set of
numbers the brief generator is permitted to cite, and the exact set
`grounding.flag` matches its prose against.

```json
{"name": "rhr", "value": 54.0, "unit": "bpm", "display": "54 bpm"}
```

`unit` is one of `bpm`, `sec`, `min`, `mi`, `steps`, `count`, `sd`, `pct`,
`none`. `display` is the post-conversion coach rendering — sleep as `"7h 33m"`,
TSB signed as `"-6.5"`, steps comma-grouped as `"9,124"`. Quote `display`, not
`value`.

`snapshot` carries at most eleven metric values (`rhr`, `sleep_seconds`,
`sleep_score`, `avg_stress`, `max_stress`, `body_battery_max`,
`body_battery_min`, `steps`, `vo2_max`, `intensity_minutes_moderate`,
`intensity_minutes_vigorous` — null values are dropped, not emitted) plus up to
four baseline references named `rhr_baseline`, `sleep_baseline`,
`body_battery_max_baseline`, `stress_baseline`.

### Candidates

Each entry is a `CandidateTakeaway`:

```json
{
  "category": "recovery",
  "fired_triggers": ["rhr_elevated", "sleep_poor"],
  "metrics": [
    {"name": "rhr_delta_bpm", "value": 4.0, "unit": "bpm", "display": "+4"},
    {"name": "rhr_days_elevated", "value": 3.0, "unit": "count", "display": "3"},
    {"name": "sleep_score", "value": 61.0, "unit": "none", "display": "61"}
  ],
  "suggested_tone": "critical",
  "chart_metric": {"metric": "sleep_seconds", "days": 14},
  "evidence": "rhr_elevated; sleep_poor"
}
```

Categories, in the fixed priority order the list is sorted by:

| Rank | Category | Fires when | `fired_triggers` vocabulary | Chart |
|---|---|---|---|---|
| 0 | `workout` | Always | `workout_mandate`, `active_plan` | `tsb` / 30d |
| 1 | `steps` | Always | `steps_mandate`, `under_goal`, `avg_slipping` | `steps` / 14d |
| 2 | `conditioning` | Any conditioning predicate | `ctl_shifted`, `run_count_shifted`, `te_collapsing`, `long_run_absence` | `ctl` / 60d |
| 3 | `recovery` | A recovery predicate fires AND it isn't all-green | `rhr_elevated`, `sleep_poor`, `bb_or_stress_low`, `recovery_anomaly` | `sleep_seconds`, `body_battery_max`, or `rhr` / 14d |
| 4 | `wildcard` | `0 <= days_to_race <= 10` | `race_week` | none |

`suggested_tone` is one of `positive` / `caution` / `critical` / `neutral`. It is
an **advisory prior**, not a command — recovery precedence and continuity
escalation stay LLM judgment, and the generator may override it.

The thresholds behind the triggers are one named block (`brief_planner._TRIGGERS`):

| Threshold | Value |
|---|---|
| `ctl_change_pct` | CTL moved > 5% over 14d |
| `run_count_delta` | 14d run count differs from the prior 14d by ≥ 3 |
| `run_gap_days` | ≥ 5 days since the last run (or never ran) |
| `te_collapse` | last 3+ runs all aerobic TE < 1.0 |
| `rhr_elevated_bpm` / `rhr_elevated_days` | RHR ≥ 3 bpm over baseline for ≥ 3 consecutive days |
| `sleep_score_low` | sleep score < 65 |
| `sleep_short_seconds` | ≥ 1h below the 60-day sleep mean |
| `bb_low_max` / `bb_low_nights` | body battery topping < 50 on ≥ 3 of the last 3 nights |
| `stress_7d_high` | 7-day stress average > 40 |
| `tsb_fresh` / `tsb_very_fatigued` | `interpret.TSB_FRESH` (+5) / `interpret.TSB_VERY_FATIGUED` (-20) |

### Anomalies

RHR-only, last 14 days, strictly more than 2 SD above the 60-day mean. Each entry
carries the deterministic interpretation from `interpret.sd_position` —
`sd_distance` (signed, rounded to 2) and `direction` (`above` / `below`):

```json
{"date": "2026-07-19", "metric": "rhr", "value": 59, "baseline": 51.2,
 "sd_distance": 2.69, "direction": "above"}
```

### plan_today

`plans.build_plan_status()` — a rollup, not a schedule:

```json
{
  "active": true,
  "goal_type": "half_marathon",
  "race_date": "2026-10-04",
  "target_time_seconds": 6900,
  "days_to_race": 75,
  "adherence_pct": 82,
  "today": {"type": "easy", "target_distance_m": 8046.7,
            "target_pace_sec_per_km": 400.0, "target_duration_sec": null,
            "description": "easy 5 mi", "verdict": "pending"},
  "last_graded": {"type": "long", "target_distance_m": 16093.4, "…": "…",
                  "verdict": "done"}
}
```

### Full shape

```json
{
  "date": "2026-07-21",
  "user_name": "Nate",
  "candidates": [
    {"category": "workout", "fired_triggers": ["workout_mandate", "active_plan"],
     "metrics": [{"name": "tsb", "value": -6.5, "unit": "none", "display": "-6.5"}, "…"],
     "suggested_tone": "neutral",
     "chart_metric": {"metric": "tsb", "days": 30},
     "evidence": "TSB -6.5, 1d since last run, plan: easy"},
    {"category": "steps", "fired_triggers": ["steps_mandate", "under_goal"], "…": "…"},
    {"category": "recovery", "fired_triggers": ["rhr_elevated"], "…": "…"}
  ],
  "snapshot": [
    {"name": "rhr", "value": 54.0, "unit": "bpm", "display": "54 bpm"},
    {"name": "sleep_seconds", "value": 25200.0, "unit": "sec", "display": "7h 00m"},
    {"name": "rhr_baseline", "value": 51.2, "unit": "bpm", "display": "51 bpm"},
    "…"
  ],
  "training_load": [
    {"name": "ctl", "value": 41.7, "unit": "none", "display": "41.7"},
    {"name": "atl", "value": 48.2, "unit": "none", "display": "48.2"},
    {"name": "tsb", "value": -6.5, "unit": "none", "display": "-6.5"}
  ],
  "trends": [{"name": "rhr", "value": 54.0, "unit": "bpm", "display": "54 bpm"}, "…"],
  "workouts_14d": [
    {"date": "2026-07-20", "type": "running", "distance_mi": 5.0,
     "pace_min_per_mi": "10:36", "duration": "53:00", "aerobic_te": 2.8,
     "training_load": 71.0, "avg_hr": 141},
    "…"
  ],
  "anomalies": [],
  "continuity": ["Easy 5 on tap — RHR is back at baseline", "…"],
  "plan_today": {"active": true, "…": "…"},
  "step_goal": 10000,
  "days_to_race": 75,
  "data_frontier": "2026-07-21",
  "baseline_stale_days": 0,
  "brief_stale_days": 1,
  "tsb_zone": "neutral"
}
```

## Example

> "What should I do today?"

```
get_brief_context()
```

```json
{
  "date": "2026-07-21",
  "candidates": [
    {"category": "workout", "fired_triggers": ["workout_mandate", "active_plan"],
     "suggested_tone": "caution",
     "metrics": [{"name": "tsb", "value": -6.5, "unit": "none", "display": "-6.5"},
                 {"name": "recovery_red", "value": 1.0, "unit": "none", "display": "1"}],
     "evidence": "TSB -6.5, 1d since last run, plan: easy"},
    {"category": "recovery", "fired_triggers": ["rhr_elevated"],
     "suggested_tone": "caution",
     "metrics": [{"name": "rhr_delta_bpm", "value": 3.0, "unit": "bpm", "display": "+3"},
                 {"name": "rhr_days_elevated", "value": 3.0, "unit": "count", "display": "3"}],
     "chart_metric": {"metric": "rhr", "days": 14}}
  ],
  "plan_today": {"active": true, "days_to_race": 75,
                 "today": {"type": "easy", "description": "easy 5 mi", "verdict": "pending"}},
  "…": "…"
}
```

The recovery card fired and the workout card's `recovery_red` flag is 1, so the
prescribed easy 5 is the ceiling, not the floor. Answer from `display` strings —
`+3` bpm over baseline for three straight days — never from raw `value`.

## Gotchas

- **`continuity` is the LEAD headline of each recent brief, not all of them.**
  The handler now passes `briefs.load_recent_briefs()` in (it used to pass
  nothing, so `continuity` was permanently `[]` over MCP), but
  `_continuity` keeps only the first takeaway of each of the last seven saved
  briefs, newest first. It is `[]` when no briefs are on disk. For the full
  text of a past brief, read `briefings/<date>.json`.
- **No `user_notes`.** `BriefContext` has no notes field at all. Use
  [`daily_snapshot`](daily_snapshot.md) or `list_user_notes` for the coaching
  preferences.
- **`trends` carries no trend.** It is the `rhr` / `sleep_score` / `steps` /
  `body_battery_max` entries of `snapshot`, re-emitted verbatim — no slope, no
  direction, no arrow. For a real trend read call
  [`get_metric_trend`](get_metric_trend.md); for the 7-day arrows call
  [`daily_snapshot`](daily_snapshot.md).
- **`candidates` is over-generated, not a brief.** `workout` and `steps` always
  appear whether or not anything is wrong. The generator's job is to SELECT 3–5
  and lead with one; a candidate's presence is not itself a finding.
  `fired_triggers` is what tells you a threshold was actually crossed —
  `workout_mandate` and `steps_mandate` are unconditional, the rest are not.
- **`suggested_tone` is a prior, not a verdict.** Overriding it is expected
  behavior, not a bug.
- **Run history is bounded to 35 days** (`_ACTIVITY_LOOKBACK_DAYS`). If the last
  run predates that window, `days_since_last_run` reads `null` (which
  `long_run_absence` treats as "fires") and `recent_te` is short — an accepted
  tradeoff for a bounded scan.
- **"Run" means measured pace, not `activity_type`.** Garmin files walking-desk
  sessions as `treadmill_running`, so `days_since_last_run`, `runs_14d`,
  `runs_prior_14d` and `recent_te` gate on `interpret.is_running_effort` (a
  13:00 mile ceiling) and fall back to the label only for a row with no pace.
  A labelled run at walking pace is excluded; a bike ride is excluded whatever
  its pace, because the on-foot label is checked first. These counts will
  therefore be LOWER than a naive `activity_type` query over the same window —
  that is the fix, not a discrepancy.
- **`plan_today` is graded against the real data frontier**, not against `date` —
  `build_plan_status` has no "as of" perspective. `days_to_race` and which
  workout is "today" are the only date-relative parts.
- **`anomalies` is RHR-only.** It is not the general
  [`find_anomalies`](find_anomalies.md) tool; other metrics are never scanned here.
- **It is not in the V1 brief loop's tool grant.** V2's planner *is* this code
  path, called in-process; the V1 rollback (`LOCAL_FITNESS_BRIEF_V2=0`) uses the
  tool-driven prompt instead.

## See also

- [`daily_snapshot`](daily_snapshot.md) — today's numbers + user notes, smaller payload.
- [`get_today_status`](get_today_status.md) — the same as `daily_snapshot`.
- [`training_load_status`](training_load_status.md) — CTL/ATL/TSB with 30-day history.
- [`get_metric_trend`](get_metric_trend.md) — an actual slope over any window.
- [`save_brief`](save_brief.md) — persist the brief you compose from this context.
