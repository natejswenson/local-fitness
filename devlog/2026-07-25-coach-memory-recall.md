# 2026-07-25 — Coach memory v2: archive-not-delete + FTS5 recall (0.33.0)

## Why

"I need my agent to be smart and remember our conversations." Three
structural reasons it didn't:

1. **The journal deleted its own past.** `journal.prune` hard-DELETEd
   everything beyond 60 entries — the coach was *required* to forget.
2. **No retrieval.** Only the newest 10 entries were injected into prompts;
   anything older was unreachable even while it still existed.
3. **Weak capture.** Memories were only written when the chat model happened
   to call `save_coach_memory`; nothing told it when or that it should.

Research verdict (2026 landscape): memory frameworks (Mem0/Zep/Letta/Cognee)
are cloud-first or heavy for a single-user local SQLite app; the local-first
consensus is FTS5 (+ optional sqlite-vec later) in the one database file.
Lean v1: zero new dependencies.

## What

- **Archive, don't delete.** `coach_journal.archived` flag (guarded ALTER);
  `archive_overflow` replaces `prune`. Hot injected set still 60 → prompt
  budgets and prompt-hash caches untouched. Only `delete_coach_memory`
  removes rows for real.
- **`recall_coach_memories`** — BM25 keyword search over the whole journal
  via `coach_journal_fts` (FTS5 external-content, porter stemming, sync
  triggers). Query tokens are quoted as phrases so MATCH operators are inert
  data. LIKE fallback for FTS5-less builds. Pure JSON → stdio AND `/mcp/`.
- **Capture directives** in the standing instructions: save durable facts
  when shared, session note at the end of substantive conversations, search
  before claiming not to remember, never cite a memory the search didn't
  return.

## Gotchas (the ones worth remembering)

- **`executescript` aborts whole-script on the first error**, so the FTS
  virtual table CANNOT live in `SCHEMA` — an FTS5-less SQLite build would
  brick every table on a fresh clone. `FTS_SCHEMA` is a separate script in
  its own try/except. Triggers live with the vtable so neither exists
  without the other.
- **`COUNT(*)` on an external-content FTS5 vtable reads through to the
  content table** — a count-vs-count sync check against the vtable always
  "matches" and the backfill rebuild never fires (caught by the backfill
  test on first run). The real indexed-document count is
  `coach_journal_fts_docsize`.
- The update trigger is `AFTER UPDATE OF text` — archiving (a flag flip)
  never churns the index.
- Injection is untouched by design: retrieval is tool-driven only, so
  `plan_coach`/`workout_coach` prompt-hash caches stay valid.

## Deferred (deliberately)

- sqlite-vec + local embeddings: BM25 over 240-char lines is enough at this
  corpus size; the schema needs no change to add an embedding column later
  if keyword recall demonstrably misses paraphrases.
- Deterministic capture (client-side Stop hook prompting a session summary):
  v2 hardening if instruction-driven capture proves too flaky in practice.
