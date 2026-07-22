# `delete_user_note`

> **WRITE TOOL — destructive, no undo.** Removes the coaching preference at a given line index from the notes file. **Availability:** stdio + HTTP

## What it does

Deletes one bullet from the user-notes file (`data/user_notes.md`, override
`LOCAL_FITNESS_NOTES_PATH`) so it stops being injected into the system prompt.
Use it when the user asks you to forget or drop a durable coaching preference.

If the user is *changing* a preference rather than dropping it, use
[`update_user_note`](update_user_note.md) — an edit keeps one bullet where a
delete-then-save produces two entries and a churned index. If what they want
removed is a logged subjective reading (an RPE, a weight, a soreness note),
that is a different family entirely:
[`delete_observation`](delete_observation.md).

Preference management is **conversational by design** — there is no settings UI
and none is planned.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `line` | integer | yes | — | 0-indexed raw file line number, from [`list_user_notes`](list_user_notes.md) or the `[n]` prefixes in the system prompt's notes section. Must be an `int`. |

## Returns

```json
{"deleted": true, "line": 2}
```

The deleted text is not echoed back — read it with
[`list_user_notes`](list_user_notes.md) first if you want to confirm to the user
what was removed.

Failure returns `is_error: true` with `{"error": "no note at line 2"}` when the
index is out of range, the notes file doesn't exist, or the line isn't a bullet.

## Example

> "Forget the note about weekend sleep, I want you calling that out again."

```json
{"line": 3}
```

```json
{"deleted": true, "line": 3}
```

## Gotchas

- **No undo and no archive.** `delete_note()` rewrites the file without the
  line. Unlike the 4 KB rotation, nothing is copied to
  `user_notes.archive.md` — the text is gone.
- **Every later index shifts down by one.** If you are removing several notes,
  re-read with [`list_user_notes`](list_user_notes.md) between calls, or delete
  from the highest index downward. Deleting stale indices in ascending order
  will remove the wrong preferences without erroring.
- **Confirm when the intent is ambiguous.** "I don't need that anymore" over a
  list of five notes is not a target; ask which one.
- **Non-bullet lines are protected.** If `line` points at a hand-written prose
  line the delete is refused (returns `no note at line N`) rather than removing
  an arbitrary line of the file.
- Not reachable from the brief loop — `_READ_ONLY_TOOL_NAMES` excludes every
  note-write tool.

## See also

- [`list_user_notes`](list_user_notes.md) — get fresh line indices before deleting
- [`update_user_note`](update_user_note.md) — usually what "change that note" actually means
- [`save_user_note`](save_user_note.md) — add a preference
- [`delete_observation`](delete_observation.md) — the *other* family: drop a logged data point
