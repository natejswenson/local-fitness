# `save_coach_memory`

> **WRITE TOOL.** Appends ONE dated line to the coach's own journal — the memory that feeds every future brief, report card and conversation. **Availability:** stdio + HTTP

## What it does

Writes a single line into the `coach_journal` table with `source='chat'`. This
is layer 2 of the coach's two-layer memory: layer 1 is the deterministic
relationship ledger (streaks, adherence, report-card aggregates — computed, not
written), and this is the *color* a query can't produce — an excuse, a promise,
an injury flag, a breakthrough.

The newest `JOURNAL_CAP` (60) unarchived entries are rendered into every voice
surface's prompt. Older ones are **archived, never deleted**, and stay
searchable through [`recall_coach_memories`](recall_coach_memories.md).

It is not the same family as [`save_user_note`](save_user_note.md). A user note
is an *instruction the coach follows* ("stop nagging about steps"); a journal
entry is *something the coach observed* ("Jul 18: blamed the heat again —
second time this month"). Notes take precedence over everything; memories are
material to cite.

Two other writers share this table and are not reachable from here:
`agent/reflect.py` auto-writes after each saved daily brief (`source='brief'`)
and each first-render report card (`source='report_card'`).

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `text` | string | yes | — | The memory line, in the coach's own voice. Stripped, then hard-capped at **240 chars** (`journal.ENTRY_MAX_CHARS`) — over that is an error, not a truncation. Empty or whitespace-only errors. |
| `date` | string | no | today | ISO `YYYY-MM-DD` the memory is *about* (not when it was written — `created_at` handles that). Validated by `date.fromisoformat` plus a length-10 check, so `2026-02-30` and `20260726` are both rejected. |

## Returns

The inserted row, with a `saved` flag:

```json
{
  "saved": true,
  "entry_id": 214,
  "created_at": "2026-07-26T08:14:03",
  "entry_date": "2026-07-26",
  "source": "chat",
  "source_key": null,
  "seq": 1,
  "text": "Jul 26: promised the long run moves to Sunday. Third time he's rescheduled it this month."
}
```

`entry_id` is what [`delete_coach_memory`](delete_coach_memory.md) takes.
`source_key` and `seq` are the reflect path's idempotency keys and are always
`null` / `1` for a chat write.

Failure returns `is_error: true` with `{"error": ...}`:

| Condition | Message |
|---|---|
| Empty / whitespace `text` | `journal entry text is required` |
| Over 240 chars | `journal entry too long (N chars, max 240)` |
| Malformed `date` | `date must be a valid YYYY-MM-DD date (got '…')` |
| Non-string `date` | `date must be a YYYY-MM-DD date string` |

## Example

> "I'm going to be honest, I skipped Tuesday because I was hungover."

```json
{"text": "Jul 21: skipped the tempo — hungover, said it outright instead of blaming the schedule. Credit for the honesty, not for the miss."}
```

```json
{"saved": true, "entry_id": 211, "entry_date": "2026-07-21", "source": "chat",
 "source_key": null, "seq": 1,
 "text": "Jul 21: skipped the tempo — hungover, said it outright instead of blaming the schedule. Credit for the honesty, not for the miss."}
```

## Gotchas

- **Every write archives.** `save_entry` calls `archive_overflow` immediately
  after the INSERT, so writing the 61st hot entry flips the oldest to
  `archived = 1`. Nothing is deleted — the entry drops out of the prompt block
  but stays in the table and stays findable via
  [`recall_coach_memories`](recall_coach_memories.md). The cap exists so the
  prompt's token cost is bounded by construction.
- **Read before you write.** Call [`list_coach_memories`](list_coach_memories.md)
  first. A duplicate memory doesn't error — it just spends one of the 60 hot
  slots saying something already there, and pushes a real memory into the
  archive. If the pattern repeated, *escalate the existing line* by deleting and
  rewriting it rather than adding a near-duplicate.
- **Skip routine Q&A.** This is not a transcript log. A durable fact the user
  shared, a promise, an injury, a breakthrough, or a 1–2 line note after a
  substantive conversation — that's the bar. "Asked about last week's mileage"
  is not.
- **240 chars is a hard error, not a clip.** A long paragraph is rejected
  outright; write the line short in the first place rather than retrying against
  the limit.
- **`LOCAL_FITNESS_COACH_MEMORY=0` does not block this tool.** The kill switch
  suppresses memory *injection* into prompts and the auto-reflect step; writes,
  reads and search still work, and the data survives the switch untouched.
- **`date` is the memory's subject date, and it's what ordering keys on.**
  `list_entries` sorts `entry_date DESC, entry_id DESC`, so backdating a memory
  buries it below newer ones and can push it straight past the hot cap.
- **The prompt block only carries the text and the date.** Whatever context is
  needed to make the line make sense in six weeks has to be *in* the 240 chars.

## See also

- [`list_coach_memories`](list_coach_memories.md) — read the journal back (check for duplicates first)
- [`recall_coach_memories`](recall_coach_memories.md) — keyword-search the whole journal, archive included
- [`delete_coach_memory`](delete_coach_memory.md) — the only real deletion path
- [`save_user_note`](save_user_note.md) — the *other* family: a durable instruction, not an observation
- [`get_coach_personality`](get_coach_personality.md) — journal size and whether memory injection is enabled
