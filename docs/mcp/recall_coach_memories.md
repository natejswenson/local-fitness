# `recall_coach_memories`

> Keyword-search the ENTIRE coach journal — hot entries and the archive alike — best matches first. **Availability:** stdio + HTTP

## What it does

Runs a BM25 full-text search over `coach_journal` via the `coach_journal_fts`
FTS5 external-content index, falling back to AND-joined `LIKE` when the SQLite
build has no FTS5. Unlike [`list_coach_memories`](list_coach_memories.md), which
pages the newest 60, this reaches **everything ever written** — the archive
included.

This is the retrieval half of the coach's memory contract, and it is not
optional. The system prompt says: **search here before claiming not to remember
something, and never cite a memory the search didn't return.** Any question
shaped like "didn't we talk about…", "what did I say about my knee", "you told
me in June that…" starts with this call.

Only the newest 60 entries are injected into the prompt. Everything older is
archived, not deleted — so "it isn't in my memory block" and "it never happened"
are completely different claims, and this tool is what distinguishes them.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `query` | string | yes | — | Keywords, e.g. `"knee pain"` or `"marathon goal"`. Stripped; empty errors. Max **200 chars**. Must contain at least one alphanumeric token. |
| `limit` | integer | no | `8` | Max matches. Must be a positive integer; **silently clamped to 25**, not an error. |

**Query syntax is inert.** Every whitespace token is re-quoted as an FTS5 phrase
and the phrases are implicitly AND-ed, so `NEAR(`, `col:`, `*` and stray quotes
are searched as literal data rather than interpreted as operators. You cannot
express OR, prefix matching, or proximity here — pass plain words.

Tokens are AND-ed, so more words means fewer hits. Search `knee` before
`left knee pain tempo`.

## Returns

```json
{
  "query": "knee",
  "matches": [
    {"entry_id": 87, "created_at": "2026-05-14T06:40:12", "entry_date": "2026-05-14",
     "source": "chat", "source_key": null, "seq": 1,
     "text": "May 14: left knee grumbling on downhills. Told him to back off the Thursday hills, he agreed.",
     "archived": true},
    {"entry_id": 142, "created_at": "2026-06-21T06:32:08", "entry_date": "2026-06-21",
     "source": "brief", "source_key": "2026-06-21", "seq": 2,
     "text": "Jun 21: knee held up through a 12-miler. First long run since May without a complaint.",
     "archived": true}
  ],
  "count": 2,
  "search": "fts"
}
```

| Key | Meaning |
|---|---|
| `query` | The query as searched, echoed back. |
| `matches` | Entries, **BM25 relevance order** under `fts`, newest-first under `like`. |
| `count` | `len(matches)`. |
| `search` | `"fts"` or `"like"` — which engine answered. |

Match fields are the same as [`list_coach_memories`](list_coach_memories.md)'s,
with one difference: `archived` is coerced to a real boolean here (`true` /
`false`), not left as `0` / `1`. Archived entries are flagged rather than
hidden — an old memory is still a memory, but its age is worth knowing before
you cite it.

Failure returns `is_error: true` with `{"error": ...}`:

| Condition | Message |
|---|---|
| Missing / empty `query` | `query is required` |
| Over 200 chars | `query too long (max 200 chars)` |
| Query is all punctuation | `query has no searchable words` |
| Bad `limit` | `limit must be a positive integer` |

## Example

> "Didn't I tell you something about my calf a while back?"

```json
{"query": "calf", "limit": 5}
```

```json
{"query": "calf",
 "matches": [
   {"entry_id": 96, "entry_date": "2026-05-28", "source": "chat",
    "text": "May 28: left calf tight after every tempo for two weeks. Says it loosens after a mile — watching it.",
    "archived": true}
 ],
 "count": 1,
 "search": "fts"}
```

Answer from that line and nothing else. One match means one fact.

## Gotchas

- **This is a retrieval *contract*, not a convenience.** Two rules, both in the
  system prompt: search before saying you don't remember, and cite only what
  came back. A memory you "recall" that the search didn't return is a
  fabrication with a citation attached, which is worse than admitting the gap.
- **`count: 0` means the words didn't match, not that nothing happened.** Tokens
  are AND-ed and there is no stemming or fuzzy matching worth relying on — try
  fewer words, or a different word ("knee" vs "patella"), before concluding the
  journal is empty on a subject.
- **`search: "like"` means you lost relevance ranking.** The LIKE fallback fires
  when the SQLite build has no FTS5, or if a MATCH still errors. It is a plain
  substring AND, ordered newest-first, so the *best* match may be below the
  `limit` cutoff rather than at the top. Raise `limit` when you see this mode.
- **No `truncated` flag.** Unlike the list tools, there is no signal that more
  matched than came back. `count == limit` is the hint to re-ask with a higher
  `limit` (up to 25).
- **`limit` over 25 is silently clamped.** No error; you just get 25.
- **The FTS index is external-content and self-healing.** `db.FTS_SCHEMA` is
  applied separately from `SCHEMA` on purpose — `executescript` aborts the whole
  script on error, so FTS5 DDL inside the main schema would brick every table on
  an FTS5-less build. The index rebuilds on a count mismatch against the
  `_docsize` shadow table. You should never need to think about this, but a
  first-search-after-upgrade may be doing a rebuild.
- **Search ignores the memory kill switch.** `LOCAL_FITNESS_COACH_MEMORY=0`
  suppresses injection and auto-reflect only; recall keeps working and the data
  is untouched.

## See also

- [`list_coach_memories`](list_coach_memories.md) — page the hot 60 (what's actually in the prompt right now)
- [`save_coach_memory`](save_coach_memory.md) — write one line
- [`delete_coach_memory`](delete_coach_memory.md) — the only path that really removes an entry
- [`get_report_card`](get_report_card.md) — the *other* durable record: graded workout snapshots
