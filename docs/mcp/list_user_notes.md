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

One entry per parsed bullet, newest-first, plus a count:

```json
{
  "notes": [
    {"handle": "9b3e21aa", "timestamp": "2026-04-28T11:32:14",
     "text": "Roast me when I'm slipping; encouragement softens motivation."},
    {"handle": "4f1a09cd", "timestamp": "2026-04-26T08:30:01",
     "text": "Marathon training starts in May; CTL trajectory matters more than the absolute number."}
  ],
  "count": 2
}
```

`handle` is an 8-character lowercase hex content address — derived from the
note's own timestamp and text, not a file position — so it survives an
unrelated note being added, deleted, or rotated to the archive. Two live
notes only ever share a handle if they are byte-for-byte identical in both
fields, which only a hand-edit produces — the write tools re-stamp a second
forward rather than mint a live handle twice (see
[`update_user_note`](update_user_note.md)'s Gotchas for how a hand-edited pair
is handled). A missing or unreadable notes file returns
`{"notes": [], "count": 0}` rather than erroring.

## Example

> "What have you got saved about how I like to be coached?"

```json
{}
```

```json
{"notes": [{"handle": "c19de402", "timestamp": "2026-07-02T06:11:40",
            "text": "Don't comment on weekend sleep."},
           {"handle": "9b3e21aa", "timestamp": "2026-04-28T11:32:14",
            "text": "Roast me when I'm slipping."}],
 "count": 2}
```

## Gotchas

- **Ordering is newest-first by timestamp, and every surface agrees.** This
  tool, the system prompt's notes section, and `daily_snapshot`'s `user_notes`
  all rank through the same `recent_first()`: timestamp descending, tie-broken
  by on-disk position. That's not the same thing as file order — `update_user_note`
  refreshes a note's timestamp in place *without moving its line*, so on-disk
  order stops being a recency order the moment any note is refined. (An earlier
  version of this tool returned raw on-disk order — oldest-first — which
  disagreed with the prompt's own newest-first claim the first time a note was
  updated; both surfaces now derive from the same ranking, so that can't happen
  again. `read_notes()` itself is still plain on-disk/arrival order, oldest
  first — it's the ranking on top of it that changed, not the reader.)
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
- **A hand-typed duplicate bullet gets the same handle, and it is the only way
  to get one.** The handle is derived purely from timestamp + text, so
  copy-pasting a bullet by hand produces two live notes this tool can't tell
  apart by handle alone — they read back with identical `handle` values.
  `save_user_note` and `update_user_note` cannot do this to you: each stamps
  against the handles already live and steps a second forward rather than
  repeating one. `update_user_note` / `delete_user_note` still tolerate a
  hand-edited pair (they act on the first and report how many matched); this
  tool just lists them as they are. Two bullets with the same *text* and
  different timestamps are ordinary distinct notes, not this case.
- Not available to the brief generator — `_READ_ONLY_TOOL_NAMES` excludes it;
  the brief already sees notes through the prompt injection.

## See also

- [`save_user_note`](save_user_note.md) — add a new preference (write)
- [`update_user_note`](update_user_note.md) — replace one by handle (write)
- [`delete_user_note`](delete_user_note.md) — remove one by handle (write)
- [`list_observations`](list_observations.md) — the *other* family: subjective data points, not preferences
- [`daily_snapshot`](daily_snapshot.md) — bundles `user_notes` into the daily payload
