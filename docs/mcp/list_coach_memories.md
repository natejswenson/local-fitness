# `list_coach_memories`

> Read the coach's journal back, newest first — optionally including the archive. **Availability:** stdio + HTTP

## What it does

Lists rows from `coach_journal` — the lines
[`save_coach_memory`](save_coach_memory.md) and the auto-reflect step wrote.
Read-only. By default it returns only the **hot** set (`archived = 0`): the
newest 60 entries, which are exactly what gets injected into every voice
surface's prompt.

Three jobs it does well: answering "what do you remember about me", checking for
a near-duplicate *before* writing a new memory, and fetching `entry_id`s for
[`delete_coach_memory`](delete_coach_memory.md).

It **pages**; it does not search. For "did we ever talk about my knee" use
[`recall_coach_memories`](recall_coach_memories.md), which runs BM25 over the
whole journal including the archive. Paging through `include_archived` to find a
keyword is the wrong tool and gets slower as history grows.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `days` | integer | no | — (no date filter) | Only entries whose `entry_date` is within the trailing N days. Must be a **positive** integer; `0`, a negative, or a non-int errors. |
| `limit` | integer | no | `50` | Max entries returned. Must be a positive integer — but see the clamp below: it is silently capped at 60 (hot) or 200 (with the archive). |
| `include_archived` | boolean | no | `false` | Also return archived entries. Coerced with `bool()`, so any truthy value works and nothing errors. |

## Returns

```json
{
  "memories": [
    {"entry_id": 214, "created_at": "2026-07-26T08:14:03", "entry_date": "2026-07-26",
     "source": "chat", "source_key": null, "seq": 1,
     "text": "Jul 26: promised the long run moves to Sunday. Third reschedule this month.",
     "archived": 0},
    {"entry_id": 213, "created_at": "2026-07-25T06:31:55", "entry_date": "2026-07-25",
     "source": "report_card", "source_key": "23685126977", "seq": 2,
     "text": "Jul 25: interval day, hit the reps but bolted the warmup again.",
     "archived": 0}
  ],
  "count": 2,
  "truncated": false
}
```

| Key | Meaning |
|---|---|
| `memories` | Entries sorted `entry_date DESC, entry_id DESC`. |
| `count` | `len(memories)` — what you got, not how many exist. |
| `truncated` | `true` when more entries matched than were returned. Always present. |

Per-entry fields: `entry_id` (the delete key), `created_at` (when it was
written), `entry_date` (what day it is *about*), `source` (`chat` / `brief` /
`report_card`), `source_key` + `seq` (the reflect path's idempotency keys —
`null` / `1` for chat writes), `text`, and `archived`.

`truncated` comes from a `limit + 1` fetch, the same pattern as
[`query_workouts`](query_workouts.md) — so `truncated: false` is a real
guarantee you have the complete set for those filters.

## Example

> "What do you actually remember about me?"

```json
{"limit": 10}
```

```json
{"memories": [
   {"entry_id": 214, "entry_date": "2026-07-26", "source": "chat",
    "text": "Jul 26: promised the long run moves to Sunday. Third reschedule this month.", "archived": 0},
   {"entry_id": 209, "entry_date": "2026-07-19", "source": "brief", "source_key": "2026-07-19", "seq": 1,
    "text": "Jul 19: eight straight days over the step goal. He hasn't mentioned it once.", "archived": 0}
 ],
 "count": 10,
 "truncated": true}
```

## Gotchas

- **`limit` is silently clamped, and `truncated` is measured against the clamp.**
  The effective cap is `min(limit, 60)` without `include_archived` and
  `min(limit, 200)` with it. `limit: 500` does **not** error — it quietly
  becomes 60, and `truncated: true` then means "more than 60", not "more than
  500". There is no way to page past 200 here; use
  [`recall_coach_memories`](recall_coach_memories.md) or
  [`run_sql`](run_sql.md) against `coach_journal` for the full archive.
- **The default view is deliberately the prompt's view.** Without
  `include_archived`, what comes back is precisely the 60 hot entries the coach
  is actually speaking from. That makes this the right call for "what am I
  grounded on right now" and the *wrong* call for "have we ever discussed X".
- **`archived` comes back as `0`/`1`, not `true`/`false`.** Unlike
  [`recall_coach_memories`](recall_coach_memories.md), which coerces it, this
  tool passes the SQLite integer straight through. Test it for truthiness, don't
  compare to `true`.
- **`days` filters on `entry_date`, using SQLite's `date('now', '-N days')`** —
  the database clock (UTC), not the Python `date.today()` other tools anchor to,
  and not the data frontier. Near midnight the two can disagree by a day.
- **A memory's absence here is not proof it never existed.** It may be archived
  (past the hot 60) or deleted. Only [`delete_coach_memory`](delete_coach_memory.md)
  removes anything for real, so search before telling the user you don't
  remember something — that retrieval contract is in the system prompt and gated
  by `tests/test_prompts.py`.
- **Not affected by the memory kill switch.** `LOCAL_FITNESS_COACH_MEMORY=0`
  stops prompt injection and auto-reflect; listing still returns everything.

## See also

- [`recall_coach_memories`](recall_coach_memories.md) — search, including the archive; use this to answer questions about the past
- [`save_coach_memory`](save_coach_memory.md) — write one line
- [`delete_coach_memory`](delete_coach_memory.md) — remove one by `entry_id`
- [`get_coach_personality`](get_coach_personality.md) — hot and archived counts in one call
- [`list_user_notes`](list_user_notes.md) — the *other* family: durable instructions, not observations
