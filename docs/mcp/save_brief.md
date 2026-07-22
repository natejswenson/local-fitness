# `save_brief`

> **WRITE TOOL.** Validates a composed brief against the `Brief` schema and atomically overwrites `briefings/<today>.json`. **Availability:** stdio + HTTP

## What it does

The only tool in the fitness surface that writes a brief. It hands the payload to
`briefs.save_brief()` — the single integrity gate shared by the scheduled
composer, this tool, and `ab_brief --run` — which salvages malformed shapes,
stamps server-side fields, validates, and atomically replaces today's brief file.

Call it after composing a brief (from the `brief` prompt, or from
[`get_brief_context`](get_brief_context.md)). Do not call it to "check" a brief:
there is no dry-run, and a successful call destroys whatever brief already exists
for today.

## What it overwrites

- **`briefings/<today>.json`, unconditionally.** The filename comes from
  `date.today()`, never from the payload. An existing brief for today is replaced
  with no backup, no merge, and no confirmation.
- The write is `tmp file + os.replace`, so a reader never sees a half-written
  file — but it is still a full replacement, not an append.
- The directory is `LOCAL_FITNESS_BRIEFINGS_DIR` when set, otherwise
  `<repo>/briefings/`. Briefs are gitignored personal data.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `brief` | object | yes | — | The brief JSON. Must contain a `takeaways` list of 1–5 items. `date`, `user_name` and `generated_at` are stamped server-side. |

Each takeaway (`schemas.Takeaway`):

| Field | Type | Required | Notes |
|---|---|---|---|
| `headline` | str | yes | One-line action or insight, ~6–12 words. |
| `summary` | str | yes | One-line "why" — the data backing it, ~10–25 words. |
| `tone` | enum | no (`neutral`) | `positive` \| `caution` \| `critical` \| `neutral`. |
| `metric` | object \| null | no (`null`) | `{"metric": <name>, "days": <7..730>}` for the inline chart. `days` defaults to 14. |
| `details` | str | yes | Full markdown deep-dive, shown when the card expands. |

`metric.metric` is a closed whitelist (`schemas.MetricName`): `rhr`,
`sleep_seconds`, `sleep_score`, `avg_stress`, `body_battery_max`,
`body_battery_min`, `vo2_max`, `steps`, `intensity_minutes_moderate`,
`intensity_minutes_vigorous`, `ctl`, `atl`, `tsb`. Anything else fails validation.

## Returns

On success, three scalars — the validated `Brief` object is deliberately dropped
before serialization:

```json
{"saved": true, "date": "2026-07-21", "path": "/…/briefings/2026-07-21.json"}
```

On a schema failure the tool returns an MCP error payload (`is_error: true`) with
the pydantic message and nothing is written:

```json
{"error": "brief failed schema validation: 2 validation errors for Brief …"}
```

## Example

> "Save today's brief."

```jsonc
save_brief({
  "brief": {
    "takeaways": [
      {
        "headline": "Keep today easy — RHR is still three over baseline",
        "summary": "Third straight morning at 54 vs a 51 baseline, on 7h of sleep.",
        "tone": "caution",
        "metric": {"metric": "rhr", "days": 14},
        "details": "Three consecutive days ≥3 bpm above the 60-day mean …"
      },
      {
        "headline": "Steps slipped under goal again",
        "summary": "9,124 yesterday against a 10,000 goal; the 7-day average is under too.",
        "tone": "critical",
        "metric": {"metric": "steps", "days": 14},
        "details": "…"
      }
    ]
  }
})
```

```json
{"saved": true, "date": "2026-07-21", "path": "/…/briefings/2026-07-21.json"}
```

## Gotchas

- **You cannot backdate a brief.** `date` is *forced* to today and
  `generated_at` is *forced* to now, both before validation — a payload value for
  either is discarded. This keeps the filename and the in-document date
  consistent, and stops the agent from faking freshness. `user_name` is the one
  stamped field the payload can win (`setdefault`).
- **1 to 5 takeaways, hard.** Zero or six fails validation and writes nothing.
- **A nested `takeaways` list gets salvaged, not rejected.** If the payload wraps
  the brief in some other object, `_salvage_takeaways` walks it for the first
  list-of-dicts whose items all have a `headline` and rebuilds
  `{"takeaways": [...]}`, logging a warning. Compatible top-level `date` /
  `user_name` / `generated_at` strings are preserved. Don't rely on this — it is
  a safety net for models that invent top-level fields, and the schema is still
  non-negotiable.
- **Markdown tables in `details` are repaired at the gate.** Every takeaway's
  `details` passes through `render.fix_table_row_breaks` before validation, which
  fixes the literal-`n` row-break artifact models emit. No-op on details with no
  table.
- **Whitespace inside enum values is collapsed**, so a streamed `"crit\nical"`
  still validates as `critical`; free-form prose fields are only edge-trimmed so
  intentional newlines in `details` survive.
- **Only `ValidationError` is caught.** A payload that reaches the gate as a
  string goes through the full JSON-extraction path, and an unparseable one
  raises `ValueError` out of the tool rather than returning a clean error object.
  Pass an object.
- **Saving a brief does not sync Garmin.** The daily job couples pull → recompute
  → generate → save; calling this tool alone writes a brief over whatever data
  frontier already exists. `brief_stale_days` in
  [`daily_snapshot`](daily_snapshot.md) is what surfaces the mismatch.

## See also

- [`get_brief_context`](get_brief_context.md) — the pre-assembled context to compose from.
- [`daily_snapshot`](daily_snapshot.md) — `latest_brief_date` / `brief_stale_days` after a save.
- [`generate_brief_report`](generate_brief_report.md) — render a saved brief to PDF (stdio only).
