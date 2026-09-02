# `list_user_notes`

> Read the saved durable coaching preferences off disk, with the content handles that `update_user_note` / `delete_user_note` target. **Availability:** stdio + HTTP

## What it does

Returns the contents of the user-notes file (`data/user_notes.md`, override
`LOCAL_FITNESS_NOTES_PATH`) — the durable **coaching preferences** that get
injected into every system prompt. Read-only; it never writes.

Two reasons to call it: the user asked what's saved ("what notes do you have",
"show me my settings"), or you are about to save a preference and need to check
whether it overlaps an existing one. It is also the way to get the `handle`
you'll pass to an update or delete — a content address, so it keeps resolving
correctly even if something else changes first, and errors loudly if the note
it named has itself changed since.

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
    {"handle": "4f1a09cd", "timestamp": "2026-04-26T08:30:01",
     "text": "Marathon training starts in May; CTL trajectory matters more than the absolute number."},
    {"handle": "9b3e21aa", "timestamp": "2026-04-28T11:32:14",
     "text": "Roast me when I'm slipping; encouragement softens motivation."}
  ],
  "count": 2
}
```

`handle` is an 8-character lowercase hex content address — derived from the
note's own timestamp and text, not a file position — so it survives an
unrelated note being added, deleted, or rotated to the archive. Two live
notes only ever share a handle if they are byte-for-byte identical in both
fields (see [`update_user_note`](update_user_note.md)'s Gotchas for how that's
handled). A missing or unreadable notes file returns `{"notes": [], "count": 0}`
rather than erroring.

## Example

> "What have you got saved about how I like to be coached?"

```json
{}
```

```json
{"notes": [{"handle": "9b3e21aa", "timestamp": "2026-04-28T11:32:14",
            "text": "Roast me when I'm slipping."},
           {"handle": "c19de402", "timestamp": "2026-07-02T06:11:40",
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
- **A hand-typed duplicate bullet gets the same handle.** The handle is derived
  purely from timestamp + text, so copy-pasting a bullet by hand produces two
  live notes this tool can't tell apart by handle alone — they read back with
  identical `handle` values. `update_user_note` / `delete_user_note` tolerate
  this (they act on the first and report how many matched); this tool just
  lists them as they are.
- Not available to the brief generator — `_READ_ONLY_TOOL_NAMES` excludes it;
  the brief already sees notes through the prompt injection.

## See also

- [`save_user_note`](save_user_note.md) — add a new preference (write)
- [`update_user_note`](update_user_note.md) — replace one by handle (write)
- [`delete_user_note`](delete_user_note.md) — remove one by handle (write)
- [`list_observations`](list_observations.md) — the *other* family: subjective data points, not preferences
- [`daily_snapshot`](daily_snapshot.md) — bundles `user_notes` into the daily payload
