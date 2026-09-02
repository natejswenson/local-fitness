# `delete_user_note`

> **WRITE TOOL — destructive, no undo.** Removes the coaching preference with a given content handle from the notes file. **Availability:** stdio + HTTP

## What it does

Deletes one bullet from the user-notes file (`data/user_notes.md`, override
`LOCAL_FITNESS_NOTES_PATH`) so it stops being injected into the system prompt.
Use it when the user asks you to forget or drop a durable coaching preference.

If the user is *changing* a preference rather than dropping it, use
[`update_user_note`](update_user_note.md) — an edit keeps one bullet where a
delete-then-save produces two entries and a fresh handle. If what they want
removed is a logged subjective reading (an RPE, a weight, a soreness note),
that is a different family entirely:
[`delete_observation`](delete_observation.md).

Preference management is **conversational by design** — there is no settings UI
and none is planned.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `handle` | string | yes | — | 8-character content address, from [`list_user_notes`](list_user_notes.md) or the `[handle]` prefixes in the system prompt's notes section. Normalised on the way in (whitespace stripped, one layer of surrounding `[ ]` dropped, lowercased); matched exactly — no prefix matching. |

## Returns

```json
{"deleted": true, "handle": "7c2f9a10", "duplicates": 1}
```

The deleted text is not echoed back — read it with
[`list_user_notes`](list_user_notes.md) first if you want to confirm to the user
what was removed. `duplicates` is normally `1`; see Gotchas for when it isn't.

Failure returns `is_error: true` with `{"error": "no note with handle '7c2f9a10' — it may have already been deleted, updated, or rotated to the archive; call list_user_notes to re-read"}`, or `{"error": "handle is required"}` for a missing argument.

## Example

> "Forget the note about weekend sleep, I want you calling that out again."

```json
{"handle": "c19de402"}
```

```json
{"deleted": true, "handle": "c19de402", "duplicates": 1}
```

## Gotchas

- **No undo and no archive.** `delete_note()` rewrites the file without the
  line. Unlike the 4 KB rotation, nothing is copied to
  `user_notes.archive.md` — the text is gone.
- **A handle addresses content, not position — it does not shift.** Deleting
  several notes in one conversation no longer needs a re-read between calls
  the way a raw line index did: each handle you captured up front still
  resolves correctly even after an earlier delete in the same batch removed a
  different note.
- **A duplicated handle deletes only the first match.** Two bullets only ever
  share a handle if they are byte-for-byte identical in timestamp and text —
  most likely a hand-edited copy. The call removes the first one in file
  order and reports `duplicates: 2`; the remaining copy is unique and
  addressable on its own right after.
- **Confirm when the intent is ambiguous.** "I don't need that anymore" over a
  list of five notes is not a target; ask which one.
- **Fails closed on non-bullet content.** A handle only ever matches a parsed
  bullet, so there is no way to point this at hand-written prose in the file.
- Not reachable from the brief loop — `_READ_ONLY_TOOL_NAMES` excludes every
  note-write tool.

## See also

- [`list_user_notes`](list_user_notes.md) — get fresh handles before deleting
- [`update_user_note`](update_user_note.md) — usually what "change that note" actually means
- [`save_user_note`](save_user_note.md) — add a preference
- [`delete_observation`](delete_observation.md) — the *other* family: drop a logged data point
