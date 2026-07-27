# `delete_coach_memory`

> **WRITE TOOL — destructive, no undo.** Removes one journal entry by `entry_id`. The only path in the whole system that actually deletes a memory. **Availability:** stdio + HTTP

## What it does

Issues `DELETE FROM coach_journal WHERE entry_id = ?`. Use it when the user asks
you to forget something, or when an entry turned out to be wrong.

Everything else about the journal is append-and-archive. Passing the 60-entry
hot cap flips old entries to `archived = 1` — they leave the prompt block but
stay in the table and stay findable through
[`recall_coach_memories`](recall_coach_memories.md). There is no prune job, no
GC, no TTL. This tool is the single exception, and it is genuinely final.

If the memory is merely *stale* rather than wrong — a promise that's since been
kept, a niggle that resolved — consider writing the update with
[`save_coach_memory`](save_coach_memory.md) instead. The arc is often the point.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `entry_id` | integer | yes | — | From [`list_coach_memories`](list_coach_memories.md) or [`recall_coach_memories`](recall_coach_memories.md). Must be an `int` — a numeric string is rejected. |

## Returns

```json
{"deleted": true, "entry_id": 214}
```

The deleted text is **not** echoed back. Read it first if you want to confirm to
the user exactly what you removed.

Failure returns `is_error: true` with `{"error": ...}`:

| Condition | Message |
|---|---|
| Missing or non-int `entry_id` | `entry_id is required` |
| No such row | `no journal entry with entry_id 214` |

## Example

> "Delete that note about me being hungover, I don't want that on the record."

```json
{"entry_id": 211}
```

```json
{"deleted": true, "entry_id": 211}
```

## Gotchas

- **No undo, no archive, no tombstone.** Unlike overflow archiving, this removes
  the row. `recall_coach_memories` will never find it again and there is no
  restore path short of a DB backup.
- **Confirm when the target is ambiguous.** "Forget that" over a conversation
  that touched four memories is not an id. List or search first, name the exact
  line back to the user, then delete.
- **`entry_id` is stable; positions are not.** Unlike
  [`delete_user_note`](delete_user_note.md), which takes a *line index* that
  shifts under you, this takes a primary key. Deleting several entries in any
  order is safe — no re-read needed between calls.
- **Deleting a hot entry does not promote an archived one.** `archive_overflow`
  only ever runs on write, and it only ever archives; it never un-archives. So
  the prompt block simply gets one line shorter until the next
  [`save_coach_memory`](save_coach_memory.md).
- **A reflect-written entry can be deleted, and it will not come back.** Entries
  from `source='brief'` / `source='report_card'` carry a `(source, source_key,
  seq)` unique index and `journal.has_event` is the pre-check that stops a
  re-render from reflecting twice. Deleting one clears that guard, so a
  *re-render of that same event* could write a fresh entry. In practice the
  brief and card fast paths mean this rarely fires — but it is the one way a
  deleted memory reappears in a recognisable form.
- Not reachable from the scheduled brief loop — `_READ_ONLY_TOOL_NAMES` excludes
  every journal write.

## See also

- [`list_coach_memories`](list_coach_memories.md) — get `entry_id`s
- [`recall_coach_memories`](recall_coach_memories.md) — find the entry by keyword, archive included
- [`save_coach_memory`](save_coach_memory.md) — often the better answer: write the correction, don't erase the history
- [`delete_user_note`](delete_user_note.md) — the *other* family: drop a coaching preference
