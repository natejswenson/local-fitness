# `log_observation`

> **WRITE TOOL.** Records one timestamped subjective data point — RPE, soreness, weight, mood, energy, or a free-text feeling/injury/note — into the `observations` table. **Availability:** stdio + HTTP

## What it does

Observations are **subjective data points about a specific day**: things Garmin
can't measure and the DB has no column for. They are rows in the `observations`
SQLite table (`observation_id`, `observed_on`, `created_at`, `obs_type`,
`value_num`, `value_text`, `activity_id`) and they are **data the coach reads**,
never instructions it follows.

That is the whole distinction from the user-note family. A note
([`save_user_note`](save_user_note.md)) is a durable *coaching preference* that
lives in a Markdown file and is injected into the system prompt, so it shapes
every future response. An observation is one dated reading that sits in the DB
until something queries it. "Stop roasting my step count" is a note. "Felt
flat, RPE 8 on today's easy run" is an observation.

This tool is for subjective input only. A workout Garmin missed is
[`log_manual_workout`](log_manual_workout.md), which creates an `activities`
row and recomputes training load.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `obs_type` | string | yes | — | Exactly one of: `energy`, `feeling`, `injury`, `mood`, `note`, `rpe`, `soreness`, `weight`. Anything else is rejected with the allowed list. |
| `value` | number | conditional | — | **Required** for the numeric types: `weight`, `rpe`, `soreness`, `energy`, `mood`. Stored in `value_num`. |
| `text` | string | conditional | — | **Required** for the free-text types: `feeling`, `injury`, `note`. Stored in `value_text`. Whitespace-only is rejected. |
| `date` | string | no | today | ISO `YYYY-MM-DD` observed-on date. Validated before any write; a malformed string or a future date is rejected. |
| `activity_id` | integer | no | `null` | Ties the observation to an existing activity. Verified against `activities` — an unknown id errors, it is not silently stored. |

The numeric/text split is enforced by `NUMERIC_OBS_TYPES` in
`agent/tools.py`, which is the single source of truth. There is no unit or
range validation on `value` — an RPE of 47 or a weight in kg both insert fine.

## Returns

The inserted row, read straight back out of the DB:

```json
{
  "logged": true,
  "observation": {
    "observation_id": 118,
    "observed_on": "2026-07-21",
    "created_at": "2026-07-21T09:31:44.812003",
    "obs_type": "rpe",
    "value_num": 8.0,
    "value_text": null,
    "activity_id": 20174451183
  }
}
```

`observation_id` is the handle for [`delete_observation`](delete_observation.md).
Note that `observed_on` is a date while `created_at` is a full timestamp — a
backdated entry has the two disagreeing, by design.

Errors return `is_error: true`: unknown `obs_type` (with `allowed`), missing
`value`/`text` for the type, `invalid date '...'`, `date cannot be in the
future`, or `activity not found` with the offending `activity_id`.

## Example

> "That run felt awful — RPE 8, and my left calf is tight."

Two observations, since they're different types:

```json
{"obs_type": "rpe", "value": 8, "activity_id": 20174451183}
```
```json
{"obs_type": "injury", "text": "Left calf tight after the tempo — noticeable on the cooldown."}
```

```json
{"logged": true, "observation": {"observation_id": 118, "observed_on": "2026-07-21",
 "obs_type": "rpe", "value_num": 8.0, "value_text": null, "activity_id": 20174451183}}
```

## Gotchas

- **Logging an observation does not make the coach act on it.** Observations
  are not injected into any prompt. The brief pipeline never reads them —
  `list_observations` is deliberately excluded from `_READ_ONLY_TOOL_NAMES`, and
  `assemble_status()` (→ [`daily_snapshot`](daily_snapshot.md)) carries
  `user_notes` but no observations. Something has to query them
  ([`list_observations`](list_observations.md) or
  [`run_sql`](run_sql.md)) for them to influence a response. If the user wants
  a standing behavior change, that's a note, not an observation.
- **Nothing dedupes.** Logging RPE twice for the same day inserts two rows; both
  come back from `list_observations`.
- **Backdating is allowed, future-dating is not.** A future `observed_on` would
  be silently excluded from the `days`-windowed `list_observations` lookback, so
  the handler rejects it up front.
- **`weight` has no unit.** `value_num` is a bare REAL. Pick one unit and stay
  consistent; nothing in the schema records which.
- **The `activity_id` link survives workout deletion.**
  `delete_manual_workout` sets `observations.activity_id = NULL` for referencing
  rows rather than deleting them, so the observation persists with a broken
  link.
- Observations are personal data in `data/` — gitignored, never fixtured from
  real values.

## See also

- [`list_observations`](list_observations.md) — read them back, most recent first
- [`delete_observation`](delete_observation.md) — drop one by `observation_id`
- [`log_manual_workout`](log_manual_workout.md) — for an actual workout, not a subjective reading
- [`save_user_note`](save_user_note.md) — the *other* family: durable coaching preferences injected into the prompt
- [`run_sql`](run_sql.md) — ad-hoc analysis across the `observations` table
