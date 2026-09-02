# `list_observations`

> Read logged subjective data points (RPE, soreness, weight, mood, feeling/injury/note) back out of the `observations` table, most recent first. **Availability:** stdio + HTTP

## What it does

Returns rows from the `observations` DB table — the timestamped **subjective
data points** about specific days that [`log_observation`](log_observation.md)
writes. Read-only. Optional `days` lookback and `obs_type` filters.

This is the only structured way to surface observations: they are not injected
into any prompt and the brief pipeline never sees them. If a run's RPE or a
soreness note should influence what you say, you have to call this.

It lists *data*, not preferences. Durable coaching preferences ("stop roasting
my step count") live in a Markdown file and are read with
[`list_user_notes`](list_user_notes.md) — those are instructions the coach
follows, these are readings it interprets.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `days` | integer | no | no limit | Only observations with `observed_on` in the last N days. Bounds-checked to `[1, 3650]`; non-int (including `bool`) or out-of-range errors. `0` is falsy and silently means "no filter". |
| `obs_type` | string | no | all types | Filter to one of `energy`, `feeling`, `injury`, `mood`, `note`, `rpe`, `soreness`, `weight`. Validated against the same enum `log_observation` writes with — an unknown value (including a wrong case) errors and names the allowed list instead of returning zero rows. |
| `limit` | integer | no | `100` | Max rows returned, most recent first. When more rows match, the result is clipped and the payload carries `"truncated": true`. Raise it (or narrow with `days`/`obs_type`) to see more; [`run_sql`](run_sql.md) is the unbounded escape hatch. |

## Returns

Whole rows, ordered `observed_on DESC, observation_id DESC`:

```json
{
  "observations": [
    {"observation_id": 118, "observed_on": "2026-07-21",
     "created_at": "2026-07-21T09:31:44.812003", "obs_type": "rpe",
     "value_num": 8.0, "value_text": null, "activity_id": 20174451183},
    {"observation_id": 117, "observed_on": "2026-07-20",
     "created_at": "2026-07-20T07:02:10.441920", "obs_type": "injury",
     "value_num": null, "value_text": "Left calf tight after the tempo.",
     "activity_id": null}
  ],
  "count": 2
}
```

Numeric types populate `value_num` with `value_text` null; free-text types the
reverse. No rows matched returns `{"observations": [], "count": 0}`. When more
than `limit` rows match, the newest `limit` are returned and the payload adds
`"truncated": true`.

## Example

> "How have I been rating my runs the last couple weeks?"

```json
{"days": 14, "obs_type": "rpe"}
```

```json
{"observations": [{"observation_id": 118, "observed_on": "2026-07-21", "obs_type": "rpe",
                   "value_num": 8.0, "value_text": null, "activity_id": 20174451183},
                  {"observation_id": 114, "observed_on": "2026-07-17", "obs_type": "rpe",
                   "value_num": 5.0, "value_text": null, "activity_id": 20161009882}],
 "count": 2}
```

## Gotchas

- **Defaults to the newest 100.** Called with no arguments it returns at most
  `limit` (default 100) rows and flags `"truncated": true` when it clipped a
  larger set — it no longer dumps the entire table. Pass `days`/`obs_type` to
  narrow, raise `limit`, or use [`run_sql`](run_sql.md) for the full history.
- **`obs_type` is validated.** A typo (`"RPE"`, `"soreness_level"`) returns an
  error carrying the allowed list — it no longer falls through to the
  case-sensitive SQL `=` and comes back as an empty list indistinguishable
  from "nothing logged".
- **The `days` window is anchored to `date.today()`**, not to the data frontier
  — a stale DB and a fresh one behave differently for the same argument.
- **The brief never calls this.** It's deliberately excluded from
  `_READ_ONLY_TOOL_NAMES` in `agent/tools.py` (alongside `daily_snapshot`) to
  keep the brief's tool set unchanged, so observations do not reach the daily
  brief unless you surface them yourself.
- `activity_id` may be `NULL` on an observation that once pointed at a workout:
  `delete_manual_workout` detaches referencing observations rather than deleting
  them.
- For anything the two filters can't express — a date range, an aggregate, a
  join against `activities` — use [`run_sql`](run_sql.md); `observations` is in
  the queryable schema.

## See also

- [`log_observation`](log_observation.md) — write one (the allowed `obs_type` enum lives there)
- [`delete_observation`](delete_observation.md) — drop one by `observation_id`
- [`list_user_notes`](list_user_notes.md) — the *other* family: durable coaching preferences, not data
- [`run_sql`](run_sql.md) — ad-hoc queries over the `observations` table
