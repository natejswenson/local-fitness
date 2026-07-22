# `propose_training_plan`

> Create a DRAFT training plan from a goal plus a full workout schedule you generated. **Availability:** stdio + HTTP

## What it does

Validates a whole schedule and writes it to `training_plans` with `status='draft'`, returning the
`plan_id`. It is the *only* entry point into the plan lifecycle — everything else in the lifecycle
either edits that draft, activates it, or tears something down.

It does **not** activate anything. The active plan is untouched by a propose call; only a prior
*draft* is archived (one draft at a time). Reach for `revise_training_plan` instead when a draft
already exists and you are iterating on it, and for `update_plan_workout` when the plan is already
active and you only want to change one day.

```
  propose_training_plan ──► [ DRAFT ] ──commit_training_plan──► [ ACTIVE ]
                             ▲    │                              ▲     │
                             └────┘                              └─────┘
                     revise_training_plan                 update_plan_workout
                          (loop)                           (loop, ONE day)
                                  │                                    │
             discard_training_plan_draft            abandon_active_plan (NO UNDO)
                                  │                                    │
                                  ▼                                    ▼
                              [ ARCHIVED ] ◄───────────────────────────┘
```

Ground the plan first: `training_load_status`, `get_today_status`, and `query_workouts` before you
invent a schedule. The tool validates *structure*, not *sensibility*.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `goal_type` | string | yes | — | One of `5k`, `10k`, `half`, `full`, `custom`. Anything else is rejected. |
| `race_date` | string | yes | — | ISO `YYYY-MM-DD`. Upper bound for every workout date. |
| `workouts` | array | yes | — | Full schedule. Each item: `{date, week_index, type, target_distance_m?, target_pace_sec_per_km?, target_duration_sec?, description, seq?}`. Max 200. |
| `target_time_seconds` | integer | no | `null` | Goal finish time. Nullable for a "just finish" plan. Must be finite and non-negative. |
| `goal_distance_m` | number | no | derived from `goal_type` | `5k`→5000, `10k`→10000, `half`→21097.5, `full`→42195. `custom` derives **nothing** — pass it explicitly or the Riegel projection stays `null`. |
| `title` | string | no | `null` | Free text. |
| `ability_snapshot` | object | no | `null` | Your current-ability estimate, JSON-serialized into the plan row for the record. Never read back by grading. |

### Per-workout fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `date` | string | yes | ISO. Must fall inside `[created_floor, race_date]` — see Gotchas. |
| `week_index` | integer | yes (schema `NOT NULL`) | 1-based week. Drives `weekly_mileage` rollups. |
| `type` | string | yes | `easy` \| `long` \| `tempo` \| `interval` \| `rest` \| `race` \| `cross`. |
| `description` | string | yes | Non-blank after `.strip()`. This is what gets rendered to the athlete. |
| `target_distance_m` | number | no | Metres. The graded field for `easy` / `long` / `race`. |
| `target_pace_sec_per_km` | number | no | Seconds per km. Never graded — display/coaching only. |
| `target_duration_sec` | number | no | Seconds. The graded field for `tempo` / `interval`. |
| `seq` | integer | no | Intra-day session on a double day; defaults to 1. `(date, seq)` must be unique across the schedule. |

## Returns

A two-key JSON object. That is the whole payload — the plan is not echoed back.

```json
{"plan_id": 12, "status": "draft"}
```

- `plan_id` — the handle you need for `revise_training_plan`, `commit_training_plan`, and
  `discard_training_plan_draft`. **Nothing else hands it to you**; hold onto it.
- `status` — always the literal `"draft"`. `status` is hardcoded in `plans.insert_draft`, never
  taken from input, so a propose call can never mint an active plan.

On a validation failure you get an MCP error result instead:

```json
{"error": "workout 14: date 2026-09-30 outside [created, race_date]"}
```

## Example

> "Build me a half plan for the Twin Cities race on 2026-10-04, sub-1:45."

```json
{
  "goal_type": "half",
  "race_date": "2026-10-04",
  "target_time_seconds": 6300,
  "title": "TC Half — sub 1:45",
  "ability_snapshot": {"ctl": 42.1, "recent_10k_pace_sec_per_km": 282},
  "workouts": [
    {"date": "2026-07-21", "week_index": 1, "type": "easy",
     "target_distance_m": 8046.7, "target_pace_sec_per_km": 390,
     "description": "Easy 5 mi, conversational."},
    {"date": "2026-07-22", "week_index": 1, "type": "rest",
     "description": "Rest day."},
    {"date": "2026-07-23", "week_index": 1, "type": "tempo",
     "target_duration_sec": 2400, "target_pace_sec_per_km": 320,
     "description": "20 min at threshold inside a 40 min run."}
  ]
}
```

Back:

```json
{"plan_id": 12, "status": "draft"}
```

Then present the schedule to the athlete, revise if they push back, and only call
`commit_training_plan(plan_id=12)` once they have agreed.

## Gotchas

- **Distances here are METRIC; `update_plan_workout` is IMPERIAL.** This tool takes
  `target_distance_m` / `target_pace_sec_per_km` / `target_duration_sec` (raw DB columns).
  `update_plan_workout` takes `distance_mi` / `pace_min_per_mi` / `duration_min` and converts.
  Passing miles here silently writes a plan of ~13 metre runs — validation only checks
  finite-and-non-negative.
- **The earliest legal workout date is the DATA FRONTIER, not today.** `created_floor` is
  `db.last_known_daily_date()` (the most recent day Garmin data exists for), falling back to
  `date.today()` on an empty DB. If your sync is two days stale, the floor is two days *behind*
  today and back-dated workouts in that gap are accepted; you can never date a workout before the
  frontier.
- **A prior draft is silently archived.** `insert_draft` runs
  `UPDATE training_plans SET status='archived' WHERE status='draft'` before inserting. Proposing a
  second plan while a draft is open discards the first one with no warning and no undo — the old
  draft's `plan_id` becomes uncommittable (`commit_training_plan` refuses non-drafts).
- **There is no plan-quality gate at propose time.** `plans.score_plan` (the ≤15%/week ramp +
  taper check) exists and is unit-tested, but nothing calls it from this handler. A 100%
  week-over-week mileage spike validates and stores fine. Judge the ramp yourself before proposing.
- **`custom` goals get no `goal_distance_m`.** Without it, `predicted_finish_seconds` and
  `goal_gap` in `get_training_plan_progress` are permanently `null`.
- **`target_pace_sec_per_km` is never graded.** Adherence grades distance (`easy`/`long`/`race`) or
  duration (`tempo`/`interval`). A prescribed pace is coaching prose in numeric form.
- **A `null`/`0` `target_distance_m` on a distance-type workout means "by feel"** — any qualifying
  activity that day grades `done`, none grades `missed`.

## See also

- [`revise_training_plan`](./revise_training_plan.md) — iterate on the draft before committing
- [`commit_training_plan`](./commit_training_plan.md) — activate the draft
- [`discard_training_plan_draft`](./discard_training_plan_draft.md) — drop the draft
- [`update_plan_workout`](./update_plan_workout.md) — edit ONE day once the plan is active
- [`get_training_plan_status`](./get_training_plan_status.md) · [`get_training_plan_progress`](./get_training_plan_progress.md)
