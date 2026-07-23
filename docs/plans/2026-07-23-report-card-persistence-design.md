---
ticket: "—"
title: "Report-card persistence — cards as durable coach memory"
date: "2026-07-23"
source: "design"
---

# Report-card persistence — cards as durable coach memory

## Problem

Report cards are the richest judgment the coach produces — four graded
metrics, an intent-aware overall, and a four-paragraph verbal read — and
today **all of it is thrown away after render**. The only durable trace is
0–2 free-text journal lines from `reflect.py`. Asking "how did my last five
interval days grade" today means re-rendering each card: even
`format='table'` pays a ~10s coach-read SDK call per cache miss, and the
read cache is single-entry, so alternating between two cards regenerates
both. A remote `/mcp/` client (phone) can't render cards at all
(`workout_report_card` is stdio-only), so it has *no* access to this
history.

Goal: persist each card as it's rendered so the coach's memory includes
previous report cards and can answer questions about them quickly, on both
the local and networked surfaces.

## The one decision that shaped everything: a stored card is a dated snapshot

`build_card` grades the plan half against the **currently active** plan
(`report_card.py:1198`), not the plan of record for the activity's date.
Grades therefore drift when the plan changes — they are not recomputable
history. So:

- **A stored card is the card Nate was actually shown, as graded on
  `graded_at`.** A historical record, not a live view.
- `workout_report_card` keeps recomputing live on every render and
  **re-saves** (upsert), so the stored row always matches the last card
  actually seen. The two can never silently disagree about "the card".
- The query tools label every result with `graded_at` and their tool
  descriptions state the snapshot semantics explicitly.
- **No backfill** (decided 2026-07-23). Backfilling 747 historical
  activities would grade them against today's plan — actively wrong
  intents and grades. History accumulates organically: every render
  (including re-renders of old activities) persists.

This is *more* truthful than recompute-always: recomputing an old
marathon-block run today would grade it against today's plan.

## Architecture

New module `src/local_fitness/agent/card_store.py`, mirroring
`journal.py`'s pure/persistence split:

- **Pure half**: `card_row(card, *, read_cache_key)` — reduces a card dict
  to the row payload: extracted columns + `card_json` (the card minus
  `hr_trace`, `recent_activities`, `upcoming_workouts` — presentation- and
  prompt-only, reproducible, and the bulk of the bytes; the stripped keys
  are re-defaulted to empty on load so `render_markdown` still works).
- **Persistence half**: `save_card(...)` (INSERT OR REPLACE, best-effort —
  a save failure must never fail a render), `load_card(activity_id)`,
  `list_cards(start_date, end_date, intent_class, limit)`,
  `load_read(activity_id)` → `(read_cache_key, coach_read)`.

### Schema (appended to `db.SCHEMA`; new table, no migration needed)

```sql
CREATE TABLE IF NOT EXISTS report_cards (
    activity_id     INTEGER PRIMARY KEY,
    activity_date   TEXT NOT NULL,
    graded_at       TEXT NOT NULL,
    intent          TEXT,
    intent_class    TEXT,
    intent_source   TEXT,
    overall_grade   TEXT,
    gpa             REAL,
    capped_by       TEXT,
    distance_grade  TEXT,
    pace_grade      TEXT,
    hr_grade        TEXT,
    load_grade      TEXT,
    read_cache_key  TEXT,
    card_json       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_report_cards_date ON report_cards(activity_date);
```

**Grain:** one row = the most recently rendered report card for one
activity. Extracted columns exist so `list_report_cards` filters and
summarizes without JSON parsing; `card_json` is the full snapshot.
No row cap: a few KB per row, personal DB, and unlike the journal these
rows are pulled on demand, never injected into prompts.

### Write path

At the end of `tools.workout_report_card`'s card assembly (after
`coach_read` attach, before the format branch, so both `table` and PDF
formats persist): `card_store.save_card(...)` in a `try/except` that logs
and continues — same fail-silent contract as the reflect task and the
read cache. `read_cache_key` is stamped **only when the read came from a
real generation or a verified cache/store hit**; a `fallback_read`
template is still saved as what-was-shown, but with `read_cache_key`
NULL so it can never be reused as if it were the coach's voice.

### Read-reuse fast path (the speed win)

`workout_coach` gains a pure exported helper `read_cache_key(profile,
card, *, model, notes_text, user_name, memory_text)` — the existing
sha256 key computation (`workout_coach.py:543-548`) factored out and
shared, so there is one key definition, not two. `generate_read_cached`
calls it internally; `tools.workout_report_card` calls it once before
generating:

```
key = workout_coach.read_cache_key(...)          # pure, ~µs
stored = card_store.load_read(activity_id)
if stored and stored.key == key:
    card["coach_read"] = parse_read(stored.text)  # ValueError → treat as miss
else:
    card["coach_read"] = await generate_read_cached(...)   # unchanged
```

Effect: the single-entry `workout_coach_cache.json` is effectively
upgraded to a per-activity persistent cache — alternating between two
past cards stops costing ~10s each. Grades are still recomputed fresh
every render (cheap, deterministic), so a stale stored grade can never
shadow current data; only the read is reused, and only when the prompt
inputs are byte-identical. The stored key is an **opaque blob**: it
cannot be re-derived from `card_json` (the prompt includes
recent/upcoming context that storage strips) — never attempt to
recompute it from a stored row.

`generate_read_cached`'s internal single-entry file cache and the
grade-leak single-retry logic are untouched.

### New MCP tools — both in `ALL_TOOLS` (shared: stdio + `/mcp/`)

Membership follows the locked rule: these return pure JSON, no
filesystem path, so they belong on the shared surface. The phone gains
full card-history Q&A even though it still can't render new cards.
`workout_report_card` itself stays in `LOCAL_ONLY_TOOLS`, unchanged.

- **`list_report_cards`** `(start_date?, end_date?, intent_class?,
  limit?=20)` → `{cards: [{activity_id, date, graded_at, intent,
  intent_class, overall, gpa, capped_by, grades: {distance, pace, hr,
  load}}], count}`, newest first. One call answers "how have my quality
  days trended".
- **`get_report_card`** `(activity_id)` → the stored card:
  `{activity_id, date, graded_at, card, markdown, coach_read}` where
  `markdown` is `render_markdown` over the stored snapshot (rendered to
  the user verbatim, per the existing card convention). Missing row →
  `_err` pointing at `workout_report_card` (local session) to render one.

Both descriptions state: *snapshot as graded on `graded_at`; grades
reflect the plan active at render time, not a retroactive regrade.*

## Deliberate v1 exclusions

- **No injection into `render_memory_for_prompt`.** Folding cards into
  `memory_text` would bust the `plan_coach`/`workout_coach` prompt-hash
  caches on every card write, inflate the V2 compact budget, and —
  the landmine — re-introduce the self-render cascade
  (`exclude_source_key` is journal-scoped and would NOT cover a
  `report_cards` source; a card's own stored grades would feed its own
  next prompt and regenerate forever). **Any future v2 that injects
  cards must build a parallel exclusion first.** The reflect journal
  already provides ambient card memory; structured Q&A goes through the
  tools.
- **No backfill** (see snapshot decision above).
- **No kill-switch env var.** The write is fail-silent, the tools are
  pull-only, and the fast path reuses a read only on exact key equality —
  there is no runtime behavior to disable that isn't already
  best-effort. (Contrast with coach memory, which injects into prompts
  and so needed `LOCAL_FITNESS_COACH_MEMORY`.)
- **No row versioning/history-of-regrades.** Latest-render-wins matches
  "the card you saw"; INSERT OR REPLACE losing the first `graded_at` is
  immaterial under snapshot semantics.

## Facet summary

- **UX** — "How did my intervals trend?" = one `list_report_cards` call,
  instant, no SDK. "What did the coach say about Tuesday's run?" =
  `get_report_card`, returns the verbatim read and card markdown from
  that day. Works from the phone. Snapshot labeling prevents the
  two-grades-for-one-run trust failure.
- **Speed/Efficiency** — list/get are pure DB reads. The fast path
  removes the ~10s SDK call from re-renders of any previously rendered
  card (not just the most recent one). No perf-benchmarked path is
  touched; memory resolution is unchanged.
- **Quality/Reliability** — fail-silent save; fallback reads never
  stamped as reusable; grades always recomputed live on render;
  `parse_read` failure on a stored read degrades to a normal miss;
  reflect idempotency and `_render_tag` content-addressing untouched.
- **Maintainability** — journal-pattern module with pure/persistence
  divider; one shared key definition; schema is durable (simple typed
  columns + JSON snapshot, grain answerable in one sentence); the v2
  memory-injection landmine is documented here and in the module
  docstring.

## API surface

- `agent/card_store.py`: `card_row(card, *, read_cache_key: str | None) -> dict`;
  `save_card(card, *, read_cache_key, db_path=None) -> None` (never raises);
  `load_card(activity_id, *, db_path=None) -> dict | None`;
  `list_cards(*, start_date=None, end_date=None, intent_class=None, limit=20, db_path=None) -> list[dict]`;
  `load_read(activity_id, *, db_path=None) -> tuple[str | None, dict | None] | None`.
- `agent/workout_coach.py`: `read_cache_key(profile, card, *, model=None,
  notes_text=None, user_name=..., memory_text=None) -> str` (pure; the
  single key definition, used internally by `generate_read_cached`).
- MCP: `list_report_cards`, `get_report_card` (schemas above), appended
  to `ALL_TOOLS`.

## Invariants

Checkable by inspection:
- `report_cards` write sites: exactly one (`workout_report_card`), always
  post-`coach_read`, always fail-silent.
- Neither new tool appears in `LOCAL_ONLY_TOOLS`; neither returns a path.
- `memory.py`, `prompts.py`, `reflect.py`, perf-benchmarked functions:
  zero diff.
- One key definition: `generate_read_cached` computes its key via
  `read_cache_key`.

Require tests:
- Upsert: re-saving an activity replaces the row and updates `graded_at`.
- `card_json` round-trip: stored → loaded → `render_markdown` succeeds;
  extracted columns equal the card's actual grades (pinned values).
- Fast path: key match → no generation call; key mismatch → regenerates;
  unparseable stored read → regenerates; fallback read row has NULL key
  and is never reused.
- Save failure (patched to raise) → render still returns the full card.
- Tool payloads: date/intent filters pin actual returned rows; missing
  activity errors.

## Testing strategy

Unit tests in new `tests/test_card_store.py` (pure half with plain
dicts; persistence half against a tmp DB) plus tool-level cases in the
existing tools test module, fabricated fixtures only. The offline
guards in `conftest.py` already cover the SDK/Garmin choke points; the
fast-path tests patch `workout_coach.generate_read` with a counter to
assert call/no-call. Coverage gate 85% applies; nothing here is pure
I/O glue except the `open`-style auto behaviors, which don't change.

## Versioning

0.32.0 + CHANGELOG entry (Added), landing as one PR on a
`feature/report-card-persistence` branch → `dev`, per the branch model.
