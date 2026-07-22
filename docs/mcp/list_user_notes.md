# `list_user_notes`

> Read the saved durable coaching preferences off disk, with the line indices that `update_user_note` / `delete_user_note` target. **Availability:** stdio + HTTP

## What it does

Returns the contents of the user-notes file (`data/user_notes.md`, override
`LOCAL_FITNESS_NOTES_PATH`) — the durable **coaching preferences** that get
injected into every system prompt. Read-only; it never writes.

Two reasons to call it: the user asked what's saved ("what notes do you have",
"show me my settings"), or you are about to save a preference and need to check
whether it overlaps an existing one. It is also the only way to get a
trustworthy `line` index before an update or delete — indices shift, so never
reuse one from an earlier turn.

This lists *preferences*, not data. For timestamped subjective readings about a
specific day — RPE, soreness, weight, mood, how a run felt — use
[`list_observations`](list_observations.md).

## Parameters

None. The tool takes an empty object.

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| — | — | — | — | No parameters. |

## Returns

One entry per parsed bullet, plus a count:

```json
{
  "notes": [
    {"line": 0, "timestamp": "2026-04-26T08:30:01",
     "text": "Marathon training starts in May; CTL trajectory matters more than the absolute number."},
    {"line": 1, "timestamp": "2026-04-28T11:32:14",
     "text": "Roast me when I'm slipping; encouragement softens motivation."}
  ],
  "count": 2
}
```

`line` is the 0-indexed **raw file line number**, not a position in the `notes`
array — a hand-added prose line in the file consumes an index without producing
a note entry. A missing or unreadable notes file returns `{"notes": [], "count": 0}`
rather than erroring.

## Example

> "What have you got saved about how I like to be coached?"

```json
{}
```

```json
{"notes": [{"line": 0, "timestamp": "2026-04-28T11:32:14",
            "text": "Roast me when I'm slipping."},
           {"line": 1, "timestamp": "2026-07-02T06:11:40",
            "text": "Don't comment on weekend sleep."}],
 "count": 2}
```

## Gotchas

- **Ordering is oldest-first here, newest-first in the prompt.** `read_notes()`
  returns bullets in on-disk order and the file is append-only, so the *last*
  entry is the newest. `render_for_prompt()` reverses that for the system-prompt
  injection. If you present the list to the user, say which order you're using
  — the two surfaces disagree. (The `read_notes` docstring claims "newest-first
  ordering matching the on-disk order"; the docstring is wrong, the behavior is
  oldest-first.)
- **Archived notes are invisible.** When the live file exceeds the 4 KB cap,
  the oldest bullets rotate out to `user_notes.archive.md`. This tool reads only
  the live file, so rotated-out preferences will not appear — and are no longer
  in the prompt either.
- **The recency contract is advisory.** Nothing here dedupes. When two notes
  conflict, the system prompt tells the model to prefer the newer one; that is
  the only reconciliation that exists.
- **The file is hand-editable.** Lines not starting with `- ` are skipped by the
  parser (so free prose in the file is tolerated), and a bullet with no ` — `
  separator comes back with an empty `timestamp`.
- Not available to the brief generator — `_READ_ONLY_TOOL_NAMES` excludes it;
  the brief already sees notes through the prompt injection.

## See also

- [`save_user_note`](save_user_note.md) — add a new preference (write)
- [`update_user_note`](update_user_note.md) — replace one by line index (write)
- [`delete_user_note`](delete_user_note.md) — remove one by line index (write)
- [`list_observations`](list_observations.md) — the *other* family: subjective data points, not preferences
- [`daily_snapshot`](daily_snapshot.md) — bundles `user_notes` into the daily payload
