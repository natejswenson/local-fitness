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

`plans.update_active_workout` whitelists exactly six columns:

```python
_EDITABLE_WORKOUT_COLS = frozenset(
    {"type", "target_distance_m", "target_pace_sec_per_km", "target_duration_sec",
     "target_hr_max", "description"}
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
| `date` | string | yes | — | ISO `YYYY-MM-DD` of a day that **already exists** in the active plan. Parsed with `date.fromisoformat`, so an impossible calendar date (`2026-02-30`, `2026-13-01`) is a hard error, not a lookup that matches nothing. |
| `type` | string | no | unchanged | `easy` \| `long` \| `tempo` \| `interval` \| `rest` \| `race` \| `cross`. |
| `distance_mi` | number | no | unchanged | Miles. Converted to metres (`units.from_miles`, `× 1609.344`). On `tempo`/`interval` this is the graded VOLUME whenever `duration_min` isn't set (see Gotchas) — it is display-only only on `easy`/`long`/`race`. |
| `pace_min_per_mi` | string \| number | no | unchanged | **`"M:SS"` preferred** — `"9:39"`. A bare number is *decimal minutes*: `9.65` is 9:39/mi. Bounded to 3:00–30:00/mi. See the trap below. On `tempo`/`interval` this IS graded (0.63.0) — the verdict is capped by the pace of the day's fastest rep-sized split. On `easy`/`long`/`race` it stays display/coaching only. |
| `duration_min` | number | no | unchanged | Minutes. Converted to seconds and rounded. The graded VOLUME field for `tempo`/`interval` when you set it — `distance_mi` is the fallback volume when you don't. Either way the pace cap above still applies on top. |
| `hr_max` | number | no | unchanged | Prescribed heart-rate **ceiling** in bpm. Bounded to 90–210. Pass it whenever the day has a cap: [`workout_report_card`](workout_report_card.md) grades the run's average HR **and** how far above the ceiling it ran (in bpm, `hr_exceedance_bpm` — 0.40.2 replaced the earlier time-above-cap fraction, which was a different unit fed into bands calibrated for relative magnitudes) against this column. A cap written only into `description` is invisible to the grader (0.40.0). |
| `description` | string | no | unchanged | Prose prescription. |
| `seq` | integer | no | `1` | Intra-day session: 1 = first/AM, 2 = second/PM. Must be a positive int. |

At least one of `type` / `distance_mi` / `pace_min_per_mi` / `duration_min` / `hr_max` / `description` is
required — a call with only `date` errors with `nothing to update`.

**`type: "rest"` is special:** it clears `target_distance_m`, `target_pace_sec_per_km`,
`target_duration_sec` **and `target_hr_max`**, and defaults `description` to `"Rest day"` unless you
pass one. Without that, a day flipped to rest would keep its old hard-run prose (which surfaces in
every plan payload and in the brief PDF) and a stale HR ceiling prescribing a session that no longer
exists.

Since 0.46.0 that clearing lives in `plans.apply_rest_semantics`, at the **write boundary** rather
than in this tool — so [`update_plan_workouts`](update_plan_workouts.md) and any future write path
inherit it. It was tool-side while this was the only caller, which was correct right up until it
wasn't.

### Pace: send `"9:39"`, not `9.39`

`pace_min_per_mi` takes two shapes, and one of them is a trap:

| You send | Stored as | Reads back as |
|---|---|---|
| `"9:39"` | 579 sec/mi | `9:39` ✅ |
| `9.65` | 579 sec/mi | `9:39` ✅ |
| `9.39` | 563.4 sec/mi | `9:23` ❌ |

A bare number is **decimal minutes**, so `9.39` means 9 minutes plus 0.39 of a minute — 9:23/mi,
16 s/mi faster than the 9:39 you meant. This was a real failure mode: the tool's own reply echoes
`pace_min_per_mi` as a formatted `"M:SS"` string, so a model that copied a display string into the
float field prescribed a harder run and saw nothing wrong in the confirmation. `"M:SS"` round-trips
the app's own display format — prefer it and the ambiguity disappears.

The string form is strict (`units.parse_pace_min_per_mi`): one or two digits of minutes, a colon,
exactly two digits of seconds under 60. `"9:5"`, `"9:75"` and `"0:00"` are all rejected.

Both shapes are then **bounds-checked to 3:00–30:00/mi** before anything is written. That catches
transposed arguments and unit-confused numbers — a sec/km value sent as min/mi, or `distance_mi`
and `pace_min_per_mi` swapped — while they are still input errors rather than a bad prescription
sitting on the active plan.

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

```json
{"error": "date must be a valid YYYY-MM-DD date (got '2026-02-30')"}
```

```json
{"error": "pace_min_per_mi must be \"M:SS\" (e.g. \"9:39\") or decimal minutes (9.65 = 9:39/mi)"}
```

```json
{"error": "pace_min_per_mi of 2:30/mi is outside the plausible 3:00–30:00/mi range"}
```

The out-of-range message formats the pace it *parsed*, so a mistyped value shows you what the tool
thought you asked for.

## Example

> "Move Saturday's long run to Sunday."

That is **two** calls — the tool cannot change a workout's date:

```json
{"date": "2026-07-25", "type": "rest"}
```
```json
{"date": "2026-07-26", "type": "long", "distance_mi": 12,
 "pace_min_per_mi": "10:21", "description": "Long run 12 mi, moved from Saturday."}
```

First call returns:

```json
{"date": "2026-07-25", "type": "rest", "seq": 1, "distance_meters": null,
 "avg_pace_sec_per_km": null, "duration_seconds": null, "description": "Rest day"}
```

Second returns the re-prescribed Sunday, as above.

## Gotchas

- **To prescribe a WALK, set a walking pace** — there is no `walk` type. Keep `type: "easy"`
  (that is what makes the day gradeable) and give `pace_min_per_mi` a value slower than 13:00,
  the repo's run/walk boundary. That one signal drives everything downstream: the report card
  grades it as a walk instead of refusing the prescription, and the calendar titles it "Walk"
  rather than "Easy run". A walk prescribed with a *running* pace, or none at all, is still read
  as a run.

- **This writes Google Calendar too, when it's configured** (0.53.0). The response carries a
  `calendar` object with `created`/`updated`/`deleted`/`unchanged` and the dates that moved —
  quote the dates, not the counts, when confirming an edit. The key is **absent** when no sync
  was attempted (no credentials, or the kill switch is off); `{"status": "error"}` means the
  plan write still succeeded and only the calendar failed, so never retry the plan edit because
  of it. Configure with [`get_plan_calendar_settings`](get_plan_calendar_settings.md).
  A day re-prescribed as `rest` has its calendar event **deleted**, not blanked.

- **"Move a long run" means re-prescribe two days.** You cannot change `date` — it is part of the
  row's identity and outside the whitelist. Rest the old day, prescribe the new one.
- **It cannot ADD a day.** The `UPDATE` matches on `(plan_id, date, seq)`; a date with no
  prescription hits `rowcount == 0` and errors. Only `propose_training_plan` /
  `revise_training_plan` create workout rows, and only on a draft. A plan with no row for a given
  day cannot gain one while active.
- **Imperial in, metric stored — the inverse of `propose_training_plan`.** This tool takes miles and
  min/mi; `propose`/`revise` take metres and sec/km. Mixing them up is the most common mistake here.
- **`9.39` is not `9:39`.** A bare `pace_min_per_mi` is decimal minutes, so copying a displayed
  `"9:39"` in as a float silently prescribes 9:23/mi — and the reply's formatted echo looks fine.
  Send the `"M:SS"` string. See [Pace](#pace-send-939-not-939) above.
- **The date has to be a real day on the calendar.** `2026-02-30` and `2026-13-01` error up front
  instead of running an `UPDATE` that matches nothing and reporting "no workout on that date" — the
  two failures used to be indistinguishable.
- **The response echoes the whole prescription.** Setting `duration_min` writes
  `target_duration_sec` and the payload now carries it back as `duration_seconds` plus a formatted
  `duration_formatted` (and `seq` for which session was edited), so a duration change confirms from
  the tool result — no follow-up `get_training_plan_progress` needed.
- **A quality day grades on VOLUME, then gets CAPPED by rep pace** (0.63.0, #242). `duration_min`
  is the graded volume on `tempo`/`interval` when you set it; `distance_mi` is the volume when you
  don't (it is no longer display-only there — it was the shape of #242: every proposed quality day
  had a distance and a pace and no duration, so an untouched distance field was silently what
  volume graded on already). Either way, `pace_min_per_mi` then CAPS that verdict against the day's
  fastest rep-sized split (≥300 m) — never raises it. On `easy`/`long`/`race`, `distance_mi` is
  still the sole graded field and `pace_min_per_mi` is still coaching prose only.
- **A `tempo`/`interval` day must end up with a duration OR a distance.** An edit that would leave
  one with neither — most commonly flipping a `rest` day (targets NULL) straight to `tempo`/
  `interval` without setting `distance_mi` or `duration_min` — is rejected with a `ValueError`
  naming the day; the write does not land. The check runs on the RESOLVED row, so a `description`-only
  edit to an existing, already-targeted quality day is unaffected.
- **No dry run, no undo.** The write is immediate and the previous prescription is gone.
- **`seq` defaults to 1.** On a double day, updating without `seq` silently edits the AM session.
- **A stale `pace_min_per_mi` survives a `type` change** unless you also clear it — only
  `type: "rest"` clears the numeric targets. This used to be harmless prose on every type; since
  0.63.0 it is LOAD-BEARING the moment the type is (or becomes) `tempo`/`interval` — a pace left
  over from a previous prescription now caps that day's verdict. Pass a fresh `pace_min_per_mi` (or
  clear it) whenever you change a day's type onto a quality type.

## See also

- [`revise_training_plan`](./revise_training_plan.md) — the DRAFT-side editor (wholesale, not per-day)
- [`get_training_plan_progress`](./get_training_plan_progress.md) — read the day back, graded
- [`get_training_plan_status`](./get_training_plan_status.md) — just today's prescription
- [`propose_training_plan`](./propose_training_plan.md) — when the change is structural, start a new draft
