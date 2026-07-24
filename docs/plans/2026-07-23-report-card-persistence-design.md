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

`build_card` grades the plan half against the **currently active** plan —
resolved by `load_report_card_inputs` (`report_card.py:1198`,
`plans.get_active_plan`) and passed into `build_card` — not the plan of
record for the activity's date. Grades therefore drift when the plan
changes — they are not recomputable history. So:

- **A stored card is the card Nate was actually shown, as graded on
  `graded_at`.** A historical record, not a live view.
- `workout_report_card` keeps recomputing live on every render and
  **re-saves** (a guarded upsert keyed on the render's prompt key) only
  when a render produces a read under a *new* prompt key — a
  distinct-key logical render from the one stored — so the stored row is
  the most recent render **whose read differed** (i.e. hashed to a new
  prompt key), or the only render if every render so far fell back to the
  template. A render that merely reuses the stored read (a fast-path or
  file-cache hit, same key) is a keyed no-op, and a render whose read is a
  fallback is a no-op when a real-read row already exists — see the write
  path — so the stored card's words and grades always originate from the
  *same* logical render and can never silently disagree about "the card".
- **`graded_at` dates the stored render, and can lag more recent
  renders.** Because an equal-key render is a no-op, `graded_at` advances
  only on a *distinct-key* render, not on every render. Two consequences
  that the labeling makes honest rather than hides: (a) a render whose
  inputs hash identically leaves `graded_at` untouched, so the timestamp
  is the age of the most recent *distinct-key* render, not of the last
  time the card was looked at; and (b) grades are recomputed live on every
  render but only *stored* on a distinct-key render, so a small grade drift
  that stays within one severity bucket (e.g. a rolling-median shift moving
  B+ → B- without changing the rounded numbers or bucket words the prompt
  key hashes) does **not** advance the snapshot — a fresh laptop render can
  legitimately show B- while `get_report_card` still returns the stored B+
  under the earlier `graded_at`. This divergence is inherent to
  dated-snapshot semantics and is **labeled, not silent**: the query tools
  carry `graded_at` and say what it dates. The rejected alternative —
  advancing grades on an equal-key render — would splice this render's
  grades onto the stored render's words (the mixed-render row the invariant
  forbids), so the snapshot deliberately trails rather than splices.
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
  prompt-only, reproducible, and the bulk of the bytes). `render_markdown`
  reads none of those three keys (it renders from `activity`, `overall`,
  `metrics`, `splits`, and `coach_read`), so it works regardless; the keys
  are still re-defaulted to empty on load to keep the loaded card the same
  shape as a freshly built one.
  `card_row` is **pure — it never reads the clock**; `graded_at` is not its
  concern (see below).
- **`graded_at` is stamped by `save_card` at write time**, never by the pure
  `card_row`. It uses the repo's existing timestamp convention —
  `datetime.now().isoformat(timespec="seconds")` (local time, seconds
  precision, no timezone suffix), matching `journal.py`'s `created_at`
  (`journal.py:59`) — bound as the `:graded_at` parameter of the UPSERT. This
  keeps the row-shaping logic clock-free and unit-testable with plain dicts,
  and puts the one impure act (reading the clock) in the persistence half
  alongside the DB write.
- **Persistence half**: `save_card(...)` (one atomic guarded UPSERT keyed
  on `read_cache_key`, `busy_timeout=5000` ms, best-effort — a save failure
  must never fail a render), `load_card(activity_id)`,
  `list_cards(start_date, end_date, intent_class, limit)`,
  `load_read(activity_id)` → `(read_cache_key, coach_read)` where
  `coach_read` is the stored parsed sections dict, not raw text. A row
  whose `card_json` fails to decode is treated as **no stored card** (a
  miss / absent), same fail-silent ethos as the write path — the loaders
  never raise on a corrupt row, they return `None`.

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
formats persist): `card_store.save_card(...)`. `save_card` **itself never
raises** — it catches every exception internally and logs, the same
fail-silent contract as the reflect task and the read cache — so the call
site needs no `try/except` of its own; a belt-and-suspenders wrapper is
optional and, if kept, is purely defensive. **The call site awaits
`save_card` via `asyncio.to_thread`**
(`await asyncio.to_thread(card_store.save_card, card,
read_cache_key=read_key)`), the same pattern `tools.py:3257,3269` already
uses to keep the blocking PDF renders off the event loop. `save_card` is a
synchronous SQLite call whose connection sets `PRAGMA busy_timeout = 5000`
(below) — running it inline in the async `workout_report_card` would block
the event loop (the whole stdio transport) for up to 5 s under exactly the
write contention this design exists for. Awaited-via-`to_thread` (rather
than fire-and-forget) is chosen deliberately: the write is a single-row
UPSERT, so the await is cheap, and awaiting keeps `save_card`'s
fail-silent log adjacent to the render instead of stranded on an
un-awaited task. `save_card` receives **the computed prompt key for this
render** as `read_cache_key`: non-NULL when the read is the coach's real
voice (a fresh generation, a file-cache hit, or a stored-read fast-path
reuse — see the fast path below), NULL when the read is a `fallback_read`
template render. That single key is both the reuse identity and the save
discriminator, so a fallback can never be reused or stored as if it were
the coach's voice.

**Key identity is the save discriminator.** A stored row already
represents a *logical render*, identified by its `read_cache_key`. The
decision `save_card` makes is entirely a function of the incoming key vs.
the stored row's key — never a function of which numbers were recomputed
this render:

- **Incoming key equals the stored row's key** (both non-NULL and equal) →
  **NO-OP**. The stored row already *is* this logical render. This is the
  one rule that closes both re-save holes: a fast-path stored-read hit and
  a `generate_read_cached` file-cache hit both arrive here carrying the
  render-N key, and neither is allowed to overwrite render-N's stored words
  with this render's freshly-recomputed grades. (Grades can drift between
  renders that hash identically — `build_prompt` hashes rounded numbers and
  `grade_severity` *bucket* words, not exact letters, so a D- → D+ slip can
  produce the same key. Persisting render-M grades under render-N words is
  exactly the mixed-render row the invariant forbids.)
- **Incoming key is non-NULL and differs from the stored key, or there is
  no row** → **full fresh overwrite** (INSERT, or UPDATE every column from
  `excluded`). This render is a genuinely new real generation; its words
  and grades are consistent *by construction* because they come from this
  one render, and they win together.
- **Incoming key is NULL (fallback) and the stored row has a real
  (non-NULL) key** → **NO-OP**. The transient-fallback render leaves the
  real-read snapshot untouched; neither its words nor its grades move.
- **Incoming key is NULL (fallback) and there is no row, or the stored row
  is itself all-fallback (NULL key)** → **full fallback save** (INSERT, or
  UPDATE with a NULL key). The first-ever render always persists so history
  isn't empty; a later all-fallback render simply refreshes an
  all-fallback row's grades.

`save_card` never splices a read from one render onto grades from another:
it writes the whole row (all extracted columns + `card_json`) or writes
nothing, and the write is **one atomic guarded UPSERT** — no
SELECT-then-write window (see *The atomic guarded UPSERT* below). The upshot: a transient
SDK failure on render N+1 — the documented stream-death mode — is a no-op,
so the stored snapshot stays exactly the render N card that was actually
shown (the coach's real words *and* the grades from that same render). A
fast-path or file-cache hit is likewise a no-op, so the stored row stays
byte-identical rather than acquiring render-M grades under render-N words.
`get_report_card` (the headline "what did the coach say" UX) never
regresses to the template after once having a real read, and it never
prints render-N words above render-M grades: the stored row's words and
grades always originate from one logical render, identified by its
`read_cache_key`.

#### The atomic guarded UPSERT

The branch rules are encoded entirely in SQL — the guard is a `WHERE`
clause on `DO UPDATE`, so there is no read-modify-write window in which a
concurrent render could interleave. `save_card` binds this render's values
(with `read_cache_key` NULL for a fallback) and executes exactly one
statement:

```sql
INSERT INTO report_cards
    (activity_id, activity_date, graded_at, intent, intent_class,
     intent_source, overall_grade, gpa, capped_by, distance_grade,
     pace_grade, hr_grade, load_grade, read_cache_key, card_json)
VALUES (:activity_id, :activity_date, :graded_at, :intent, :intent_class,
     :intent_source, :overall_grade, :gpa, :capped_by, :distance_grade,
     :pace_grade, :hr_grade, :load_grade, :read_cache_key, :card_json)
ON CONFLICT(activity_id) DO UPDATE SET
    activity_date  = excluded.activity_date,
    graded_at      = excluded.graded_at,
    intent         = excluded.intent,
    intent_class   = excluded.intent_class,
    intent_source  = excluded.intent_source,
    overall_grade  = excluded.overall_grade,
    gpa            = excluded.gpa,
    capped_by      = excluded.capped_by,
    distance_grade = excluded.distance_grade,
    pace_grade     = excluded.pace_grade,
    hr_grade       = excluded.hr_grade,
    load_grade     = excluded.load_grade,
    read_cache_key = excluded.read_cache_key,
    card_json      = excluded.card_json
WHERE
    -- real generation of a genuinely new logical render (key differs, or
    -- overwriting an all-fallback row): its words+grades win together
    (excluded.read_cache_key IS NOT NULL
     AND (report_cards.read_cache_key IS NULL
          OR excluded.read_cache_key <> report_cards.read_cache_key))
    -- an all-fallback row refreshing its own grades
    OR (excluded.read_cache_key IS NULL
        AND report_cards.read_cache_key IS NULL);
```

Walking the guard against the four branches: an equal-key hit satisfies
neither disjunct (`excluded.read_cache_key <> report_cards.read_cache_key`
is false and both-NULL is false) → the `DO UPDATE` is skipped, a true
no-op. A differing non-NULL key, or a non-NULL key over an all-fallback
row, satisfies the first disjunct → full overwrite. A NULL key over a
real-read row satisfies neither → no-op (a fallback can never null a real
key). A NULL key over an all-fallback row satisfies the second disjunct →
grades refresh. When no row exists the `INSERT` simply proceeds, for both
real and first-ever-fallback renders. One statement covers every case; no
`SELECT` precedes it.

Because `db.connect()` enables WAL but sets no `busy_timeout`, two
concurrent local renders can otherwise collide — a second writer raising
`database is locked` straight into the fail-silent `except`, silently
dropping the save. `save_card` opens **its own connection** (it is not on
any perf-benchmarked path, so the extra open costs nothing that matters)
and sets `PRAGMA busy_timeout = 5000` (5000 ms) on it, so a briefly-locked
DB **waits** for the other writer to commit rather than dropping the save;
combined with the atomic guarded UPSERT, the loser of the race then
re-evaluates the guard against the just-committed row and correctly no-ops
instead of clobbering it. A timeout *expiry* (a writer held longer than
5 s, not expected for a single-row UPSERT on a personal DB) still falls
into the fail-silent contract — the save is dropped and logged, never
surfaced — which is the same best-effort guarantee the whole write path
already makes. (Out of scope here: the existing reflect/journal writer
keeps `db.connect()`'s no-`busy_timeout` behavior; hardening it is a
separate change.) The save also depends on `init_schema` having created
`report_cards` at process start — both `workout_report_card` entry points
(`fitness mcp-stdio` and the `/mcp/` transport) run `init_schema` on
startup, so the table always exists by the first render.

### Read-reuse fast path (the speed win)

`workout_coach` gains a pure exported helper `read_cache_key(profile,
card, *, model, notes_text, user_name, memory_text)` — the existing
sha256 key computation (`workout_coach.py:543-548`) factored out and
shared, so there is one key definition, not two. `generate_read_cached`
calls it internally, and `read_cache_key` is added to
`workout_coach.__all__`. The helper must reproduce `generate_read_cached`'s
exact byte layout — `sha256("\x00".join([system_prompt, user_prompt, model
or "default", str(activity_id)]))`, including the literal string `"default"`
(NOT `DEFAULT_MODEL`) when `model` is `None`, and `str(activity_id)` from
`card["activity"]["activity_id"]` — or a factored key would silently never
match the file cache. `tools.workout_report_card` calls it once before
generating, threading the same resolved `notes_text`/`memory_text` locals
into both the key and the generator so their args are byte-identical:

```
notes_text = notes.render_for_prompt()           # resolved ONCE into a local
memory_text = memory.render_memory_for_prompt(   # resolved ONCE into a local
    exclude_source_key=("report_card", activity_key), user_name=user_name)
key = workout_coach.read_cache_key(              # pure, ~µs
    profile, card, notes_text=notes_text, user_name=user_name,
    memory_text=memory_text)
stored = card_store.load_read(activity_id)        # -> (stored_key, stored_read) | None
if stored and stored[0] == key and _read_is_complete(stored[1]):
    card["coach_read"] = stored[1]               # the parsed sections dict, reused as-is
    read_key = key                               # a real read (render-N's), non-NULL
else:
    try:                                          # NEW fast-path branch this design adds
        card["coach_read"] = await generate_read_cached(  # RAISES on failure
            profile, card, notes_text=notes_text, user_name=user_name,
            memory_text=memory_text)             # byte-identical args → same key
        read_key = key                           # generation succeeded → real read
    except Exception:
        card["coach_read"] = workout_coach.fallback_read(card) # deterministic template
        read_key = None                          # fallback → NULL discriminator
...
await asyncio.to_thread(
    card_store.save_card, card, read_cache_key=read_key)   # keyed no-op on a hit
```

The `try`/`except` around `generate_read_cached` → `fallback_read` is the
pre-existing control flow in `tools.py` (`tools.py:3211-3215`); the
`read_cache_key` + `load_read` fast-path branch *above* it is **new in this
design**. **Fallback-ness is known only
from the try/except outcome — it is not detectable from the returned dict
and must never be inferred from shape.** `generate_read_cached` never
returns a fallback; it *raises* on failure, and `fallback_read` is assigned
in the caller's `except` branch. The two functions return structurally
identical four-key sections dicts, so no shape-based predicate can
distinguish them — `read_key` is set from *which branch ran*, non-NULL on
the success path and NULL in the `except`, never by re-inspecting
`card["coach_read"]`.

On a stored-read fast-path hit, `read_key` is render-N's key — the same
value already on the stored row — so `save_card`'s guarded UPSERT skips the
`DO UPDATE` and the row is left byte-identical. The same holds for a
`generate_read_cached` file-cache hit: it returns render-N's text, so the
recomputed key equals the stored key and the write no-ops. Only a genuinely
new real generation (a differing key) overwrites, and only with words and
grades from that one render.

`coach_read` is persisted as the already-parsed sections dict (the shape
`generate_read_cached`/`fallback_read` both return), never as raw text —
so there is nothing to re-parse on load. The reuse guard is an explicit
shape check, `_read_is_complete(read)`: it is `True` only when `read` is a
dict carrying all four `READ_SECTIONS` keys, each mapping to a non-empty
string — so `_read_is_complete(None)` is `False` (an older row that predates
`coach_read`, or one whose read failed to decode, is a miss). A stored read
that fails the check falls through to the `else` branch and regenerates the
**display** — it does *not* rewrite the store: because the freshly generated
read hashes to the same `key`, `save_card`'s equal-key guard no-ops, so the
partial row is left in place. `_read_is_complete` is therefore display-side
corruption defense, not a store self-heal (and is currently unreachable in
practice, since every `save_card` path writes a shape-complete read).

Effect: the single-entry `workout_coach_cache.json` is effectively
upgraded to a per-activity persistent cache — alternating between two
past cards stops costing ~10s each. On a store-hit the single-entry file
cache is simply bypassed (harmless — the store is the better cache here).
On a store *miss* `build_prompt` runs more than once (once inside
`read_cache_key`, once inside `generate_read_cached`); it is pure and
cheap, so the duplication costs microseconds, not a round-trip. Grades are
still recomputed fresh every render (cheap, deterministic), so a stale
stored grade can never shadow current data; only the read is reused, and
only when the prompt inputs are byte-identical. The stored key is an **opaque blob**: it
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
  load}}], count}`, ordered `activity_date DESC` (newest run first). That
  ordering — not `graded_at` — matches the `idx_report_cards_date` index
  and the "trend my runs" use-case; `graded_at DESC` would surface a
  re-rendered old run above a more recent run. `intent_class` filters on one
  of the four classes `easy | long | quality | steady`. One call answers
  "how have my quality days trended".
- **`get_report_card`** `(activity_id)` → the stored card:
  `{activity_id, date, graded_at, card, markdown, coach_read}` where
  `markdown` is `render_markdown` over the stored snapshot (rendered to
  the user verbatim, per the existing card convention). The snapshot is
  the most recent *distinct-key* render with the coach's real voice (words
  and grades from that same render); a later render whose own read fell
  back to the template, or whose read hashed to the same key, is a no-op
  that leaves it in place (see write path), so this card never regresses to
  the deterministic template once a real read exists — and its `coach_read`
  never disagrees with the grade table beside it. Because equal-key renders
  don't advance `graded_at`, a fresh live re-render may legitimately show a
  slightly different grade than this stored snapshot (small drift within
  one severity bucket); the returned card is honestly dated by `graded_at`,
  not claimed to be the newest possible grade. Missing row → `_err`
  saying no card is stored for this activity yet, and that one is created by
  rendering the card from a local session (`workout_report_card` is
  stdio-only — never imply it can be invoked over `/mcp/`).

Both descriptions state: *this is the stored snapshot as graded on
`graded_at` — the most recent render whose read differed (a distinct
prompt-key render), so `graded_at` can lag more recent renders whose inputs
hashed identically; grades reflect the plan active at that render, not a
retroactive regrade, and a live re-render may show a slightly different
grade.*

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
  "the card you saw"; the guarded UPSERT overwriting the first `graded_at`
  on a real-key overwrite is immaterial under snapshot semantics.

## Facet summary

- **UX** — "How did my intervals trend?" = one `list_report_cards` call,
  instant, no SDK. "What did the coach say about Tuesday's run?" =
  `get_report_card`, returns the verbatim read and card markdown from
  that day. Works from the phone. What the snapshot actually guarantees is
  narrower than "never two grades for one run": each stored card is
  **internally consistent** (its read and its grade table come from one
  logical render) and **honestly dated** by `graded_at`. It does not
  guarantee cross-surface agreement — a live laptop re-render can
  legitimately differ from the stored snapshot (a small grade drift within
  one severity bucket does not advance the row), and that divergence is
  *labeled by `graded_at`, not silent*. See *Quality/Reliability* for the
  named residual.
- **Speed/Efficiency** — list/get are pure DB reads. The fast path
  removes the ~10s SDK call from re-renders of any previously rendered
  card (not just the most recent one). No perf-benchmarked path is
  touched; memory resolution is unchanged.
- **Quality/Reliability** — fail-silent save via one atomic guarded UPSERT
  (`busy_timeout=5000` ms, no SELECT-then-write window, so a concurrent
  local render can't clobber a just-committed real read); fallback reads
  never stamped as reusable; grades always recomputed live on render but
  **key identity, not the recomputed grades, decides the save** — an
  equal-key render (fast-path or file-cache hit) is a byte-identical no-op,
  so this render's grades never land under the stored render's words; a
  shape-incomplete stored read degrades to a normal miss; a render whose
  own read is a fallback is a whole-row no-op when a real-read row exists
  (see write path) — so `get_report_card` keeps returning the render-N
  card that was actually shown across a transient generate failure, and
  the stored card is always internally consistent (its `coach_read` and
  its grade table originate from the same logical render, upholding the
  subsystem's "a grade must never contradict the prose beside it" invariant
  on the phone surface); reflect idempotency and `_render_tag`
  content-addressing untouched. **Named residual (accepted):** the
  guarantee is intra-row, not cross-surface. Because an equal-key render is
  a no-op, a small grade drift that stays inside one severity bucket (e.g. a
  rolling-median shift moving B+ → B- with the rounded numbers and bucket
  words the prompt key hashes unchanged) does not advance the snapshot, so
  the letter a query tool shows can differ from a fresh live render's
  letter, under a `graded_at` that did not move. This is inherent to
  dated-snapshot semantics — the only way to close it would be to advance
  grades on equal-key renders, which was rejected because it splices this
  render's grades onto the stored render's words (the mixed-render row this
  facet's own invariant forbids). The residual is bounded (within-bucket
  drift only; a bucket-crossing change produces a new key and does advance
  the row) and, critically, **labeled** — `graded_at` dates the snapshot
  honestly rather than the divergence being silent.
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
  `read_cache_key`, and the helper reproduces the exact byte layout —
  `sha256("\x00".join([system_prompt, user_prompt, model or "default",
  str(activity_id)]))`, with the literal `"default"` (not `DEFAULT_MODEL`)
  and `str(activity_id)` — so a card rendered through the factored helper
  hashes identically to one rendered through `generate_read_cached` (pin the
  full hash, not just "same length").
- Key identity is the save discriminator, not the recomputed grades: when
  the incoming `read_cache_key` equals the stored row's `read_cache_key`
  (both non-NULL, equal), `save_card` writes nothing. So a fast-path
  stored-read hit and a `generate_read_cached` file-cache hit — which both
  carry the stored render's key — leave the row byte-identical and can
  never persist this render's recomputed grades under the stored render's
  words.
- A fallback render is a whole-row no-op once a real-read row exists: when
  the incoming `read_cache_key` is NULL (fallback) **and** the stored row
  already carries a real read (non-NULL key), `save_card` writes nothing.
  So a stored `read_cache_key`, once non-NULL, never reverts to NULL and
  never has its grades moved by a fallback render.
- Single-statement atomicity: `save_card` performs **no `SELECT` before its
  write** — the branch rules are the `WHERE` guard of one
  `INSERT ... ON CONFLICT DO UPDATE`, so **within `save_card`** no
  read-then-write window exists for a concurrent render to interleave
  through (this closed the SELECT-then-write hole a read-modify-write save
  would have had). The *fast path as a whole* still reads `load_read`
  outside that guard, so it retains a benign read-then-write window against
  a concurrent **writer** — two same-activity cross-process renders
  straddling e.g. a notes edit — which is vanishingly rare, non-corrupting
  (each `save_card` still writes a whole internally-consistent row from one
  render), and best-effort by design; it is not claimed closed. Its
  connection sets `PRAGMA busy_timeout = 5000` so a briefly-locked DB waits
  instead of dropping the save; only a >5 s timeout expiry falls to the
  fail-silent drop.
- The event loop never blocks on the card save: the async
  `workout_report_card` invokes the synchronous `save_card` via
  `await asyncio.to_thread(...)` (mirroring `tools.py:3257,3269`), so the
  `busy_timeout=5000` wait runs on a worker thread and the stdio transport
  stays responsive under write contention.
- `graded_at` is stamped exactly once, by `save_card`, as
  `datetime.now().isoformat(timespec="seconds")` (matching `journal.py`);
  `card_row` is pure and never reads the clock.
- A stored row is internally consistent: `card_json`'s `coach_read` and
  every extracted grade column originate from the *same* logical render,
  identified by its `read_cache_key` — there is no code path that writes a
  read from one render onto grades from another (no field-level splicing
  exists; the guarded UPSERT writes the full row from `excluded` or
  nothing).
- **No delete/prune path exists, and its absence is load-bearing.** The
  only `report_cards` mutation is `save_card`'s single guarded UPSERT;
  nothing deletes or prunes rows (no row cap — see *Grain*). The fast path
  depends on this: `load_read(activity_id)` reads outside the UPSERT's
  atomic guard. The fast path already tolerates a benign read-then-write
  window against a concurrent *writer* (the guard still writes one whole
  consistent row), but a concurrent pruning *delete* would turn that benign
  window corrupting: it could remove a row between `load_read` and
  `save_card`, so the fast path would reason about a row that no longer
  exists (a stored-read hit deciding a keyed no-op against a row a delete
  has since removed). A future pruning/GC tool must therefore not be added
  without first revisiting the fast path (e.g. folding the existence check
  into the guarded statement). This assumption
  is recorded in the module docstring and the CLAUDE.md bullet.

Require tests:
- Upsert: re-saving an activity with a DIFFERING (non-NULL) key replaces the
  row and updates `graded_at`; an equal-key re-save is a byte-identical
  no-op (`graded_at` unchanged), consistent with the keyed-no-op test below.
- `card_json` round-trip: stored → loaded → `render_markdown` succeeds;
  extracted columns equal the card's actual grades (pinned values).
- Fast path: key match on a shape-complete stored read → no generation
  call; key mismatch → regenerates; a shape-incomplete stored read
  (missing/empty `READ_SECTIONS` key) → regenerates; fallback read row has
  NULL key and is never reused.
- Keyed no-op (S1): a fast-path stored-read hit re-invokes `save_card` with
  the stored render's key but *different recomputed grades* (simulate a
  D- → D+ drift that hashes identically) → the stored row is byte-for-byte
  unchanged, i.e. it keeps render-N's words **and** render-N's grades, never
  the fresh grades. Same assertion for a `generate_read_cached` file-cache
  hit path: identical returned text ⇒ equal key ⇒ no rewrite.
- Save failure (patched to raise) → render still returns the full card.
- Fallback no-op / whole-row preservation: a first render with a real read
  stores it (non-NULL key); a later render whose read is a fallback is a
  no-op — the stored row is byte-for-byte unchanged (real read + key **and**
  the render-N grades both stay, proving no field-level splice); a later
  render with a new real read overwrites the whole row (read, key, and
  grades all move together); a first-ever render with a fallback stores the
  fallback (NULL key); a second all-fallback render refreshes that
  NULL-key row's grades. Assert internal consistency on the stored row:
  `card_json`'s `coach_read` and the extracted grade columns always match a
  single render's output (pin both, confirm they never straddle two
  renders).
- Atomic guard (S2): drive `save_card` twice as a guard-clause unit test —
  seed a real-read row (non-NULL key), then call `save_card` with a NULL
  (fallback) key for the same `activity_id` and assert the row's
  `read_cache_key` and grades are unchanged (a fallback UPSERT cannot null a
  real key). Pin the guard directly by executing the UPSERT statement
  against a tmp DB for each of the four key-pair cases (equal non-NULL →
  no-op; differing non-NULL → overwrite; NULL over non-NULL → no-op; NULL
  over NULL → refresh) and asserting `changes()`/row contents. Confirm the
  connection sets `busy_timeout` (query `PRAGMA busy_timeout` → 5000) and
  that `save_card` issues no `SELECT` before its write (single-statement).
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

## Same-PR deliverables

Per CLAUDE.md, a change that adds a subsystem, a table, and two `ALL_TOOLS`
tools ships its release bump, its docs, and its tests **in the same PR** —
none as a follow-up. The complete deliverables list for the one
`feature/report-card-persistence` branch → `dev` PR:

- **Version + changelog** — bump `pyproject.toml` to **0.32.0** and add a
  matching `CHANGELOG` entry (Added), per the release policy (code/feature
  change ⇒ version bump). The `dev → main` promotion later auto-cuts
  `v0.32.0`.
- **CLAUDE.md update (same PR, not a follow-up)** — add a new bullet to
  *What's already wired* covering the `report_cards` store, so future-you
  reads the contract from the source of truth. It must state: the store is
  a **dated snapshot per activity** (grades against the plan active *at that
  render*, no backfill); the save is **fail-silent** via **one atomic
  guarded UPSERT keyed on the read's prompt key** with `busy_timeout=5000`,
  so an **equal-key render is a byte-identical no-op** (the letter a query
  tool shows can therefore lag a fresh live render by within-bucket drift —
  the named residual above); the two `ALL_TOOLS` query tools
  (`list_report_cards`, `get_report_card`) reach both stdio and `/mcp/`
  because they return JSON, not a path; and the **no-delete/no-prune
  assumption is load-bearing** (see the invariant below) — a future pruning
  tool would reopen a splice window between `load_read` and `save_card`, so
  it must not be added without revisiting the fast path. Cross-reference the
  v2-injection landmine already noted in *Deliberate v1 exclusions*.
- **Devlog entry** — a `devlog/` note for this PR (meaningful change ⇒
  devlog), summarizing the snapshot decision, the keyed-no-op mechanic, and
  the phone-surface Q&A win.
- **Tests** — everything under *Require tests* and *Testing strategy*
  (new `tests/test_card_store.py` pure + persistence cases, tool-level cases,
  fabricated fixtures only), holding the 85% coverage gate.
