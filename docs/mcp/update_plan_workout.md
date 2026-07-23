# `update_plan_workout`

> Re-prescribe ONE day on the ACTIVE training plan — prescription columns only. **Availability:** stdio + HTTP

## What it does

The single write path into a live plan. Given a date (and optionally a `seq` for a double day), it
overwrites that day's prescription: type, target distance, target pace, target duration,
description. Nothing else. It is how you move a long run, swap a session, or dial a day back after
a bad night's sleep — without touching the rest of the plan.

`revise_training_plan` is the draft-side counterpart and refuses active plans; this tool is the
active-side counterpart and does not know about drafts. If the athlete wants a structurally
different plan, propose a new draft and commit it — do not try to walk an active plan into a new
shape one `update_plan_workout` call at a time.

```
  propose_training_plan ──► [ DRAFT ] ──commit_training_plan──► [ ACTIVE ]
                             ▲    │                              ▲     │
                             └────┘                              └─────┘
                     revise_training_plan                 update_plan_workout ◄── you are here
                          (loop)                           (loop, ONE day)
                                  │                                    │
             discard_training_plan_draft            abandon_active_plan (NO UNDO)
                                  │                                    │
                                  ▼                                    ▼
                              [ ARCHIVED ] ◄───────────────────────────┘
```

## The write boundary

`plans.update_active_workout` whitelists exactly five columns:

```python
_EDITABLE_WORKOUT_COLS = frozenset(
    {"type", "target_distance_m", "target_pace_sec_per_km", "target_duration_sec", "description"}
)
```

`date`, `seq`, `week_index`, `plan_id`, and `workout_id` are identity/structure and are **not**
editable through this path. The `UPDATE` is scoped to
`WHERE plan_id=<the active plan> AND date=:date AND seq=:seq`, so a call can re-prescribe a day but
can never re-key a workout, move it to another plan, change a plan's status, or restructure the
schedule. A field outside the whitelist raises `non-editable workout field(s): [...]` before any
SQL runs.

That boundary is why hand-written `UPDATE` SQL against `plan_workouts` is forbidden in this repo:
the agent owns the plan lifecycle, there is no UI, and this whitelist is the only thing standing
between "adjust today's run" and "silently rewrite the plan's structure".

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `date` | string | yes | — | ISO `YYYY-MM-DD` of a day that **already exists** in the active plan. |
| `type` | string | no | unchanged | `easy` \| `long` \| `tempo` \| `interval` \| `rest` \| `race` \| `cross`. |
| `distance_mi` | number | no | unchanged | Miles. Converted to metres (`× 1609.344`). |
| `pace_min_per_mi` | number | no | unchanged | Decimal min/mi, e.g. `9.65` for ~9:39/mi. Converted to sec/km (`× 60 ÷ 1.609344`). |
| `duration_min` | number | no | unchanged | Minutes. Converted to seconds and rounded. The **graded** field for `tempo`/`interval`. |
| `description` | string | no | unchanged | Prose prescription. |
| `seq` | integer | no | `1` | Intra-day session: 1 = first/AM, 2 = second/PM. Must be a positive int. |

At least one of `type` / `distance_mi` / `pace_min_per_mi` / `duration_min` / `description` is
required — a call with only `date` errors with `nothing to update`.

**`type: "rest"` is special:** it clears `target_distance_m`, `target_pace_sec_per_km`, and
`target_duration_sec`, and defaults `description` to `"Rest day"` unless you pass one. Without that,
a day flipped to rest would keep its old hard-run prose, which surfaces in every plan payload and
in the brief PDF.

## Returns

The updated row, re-read from the DB after the write, projected into the repo's standard workout
field names and augmented with display units.

```json
{
  "date": "2026-07-25",
  "type": "long",
  "seq": 1,
  "distance_meters": 19312.128,
  "avg_pace_sec_per_km": 385.87,
  "duration_seconds": null,
  "description": "Long run 12 mi, easy effort.",
  "distance_mi": 12.0,
  "pace_min_per_mi": "10:21"
}
```

- `distance_meters` / `avg_pace_sec_per_km` / `duration_seconds` — the raw stored values (note the
  names: they follow the `activities` convention, not `target_*`). `duration_seconds` is `null` on a
  distance-typed day.
- `seq` — which intra-day session was edited (1 = AM, 2 = PM), so a double-day edit is unambiguous
  in the reply.
- `distance_mi` — present only when display units are miles; omitted in km mode.
- `pace_min_per_mi` — formatted string, always present when a pace is stored.
- `duration_formatted` — present only when a duration is stored (e.g. `"40:00"` for a tempo day),
  so a `duration_min` edit can be confirmed straight from the tool result.

Errors:

```json
{"error": "no active training plan"}
```

```json
{"error": "no workout on 2026-07-25 (seq 1) in the active plan"}
```

```json
{"error": "unknown type 'recovery'", "allowed": ["cross", "easy", "interval", "long", "race", "rest", "tempo"]}
```

## Example

> "Move Saturday's long run to Sunday."

That is **two** calls — the tool cannot change a workout's date:

```json
{"date": "2026-07-25", "type": "rest"}
```
```json
{"date": "2026-07-26", "type": "long", "distance_mi": 12,
 "pace_min_per_mi": 10.35, "description": "Long run 12 mi, moved from Saturday."}
```

First call returns:

```json
{"date": "2026-07-25", "type": "rest", "seq": 1, "distance_meters": null,
 "avg_pace_sec_per_km": null, "duration_seconds": null, "description": "Rest day"}
```

Second returns the re-prescribed Sunday, as above.

## Gotchas

- **"Move a long run" means re-prescribe two days.** You cannot change `date` — it is part of the
  row's identity and outside the whitelist. Rest the old day, prescribe the new one.
- **It cannot ADD a day.** The `UPDATE` matches on `(plan_id, date, seq)`; a date with no
  prescription hits `rowcount == 0` and errors. Only `propose_training_plan` /
  `revise_training_plan` create workout rows, and only on a draft. A plan with no row for a given
  day cannot gain one while active.
- **Imperial in, metric stored — the inverse of `propose_training_plan`.** This tool takes miles and
  min/mi; `propose`/`revise` take metres and sec/km. Mixing them up is the most common mistake here.
- **The response echoes the whole prescription.** Setting `duration_min` writes
  `target_duration_sec` and the payload now carries it back as `duration_seconds` plus a formatted
  `duration_formatted` (and `seq` for which session was edited), so a duration change confirms from
  the tool result — no follow-up `get_training_plan_progress` needed.
- **Duration is what grades `tempo`/`interval`; distance is what grades `easy`/`long`/`race`.**
  Setting `distance_mi` on a tempo day is display-only — adherence still measures running duration.
- **No dry run, no undo.** The write is immediate and the previous prescription is gone.
- **`seq` defaults to 1.** On a double day, updating without `seq` silently edits the AM session.
- **A stale `pace_min_per_mi` survives a `type` change** unless you also clear it — only
  `type: "rest"` clears the numeric targets.

## See also

- [`revise_training_plan`](./revise_training_plan.md) — the DRAFT-side editor (wholesale, not per-day)
- [`get_training_plan_progress`](./get_training_plan_progress.md) — read the day back, graded
- [`get_training_plan_status`](./get_training_plan_status.md) — just today's prescription
- [`propose_training_plan`](./propose_training_plan.md) — when the change is structural, start a new draft
