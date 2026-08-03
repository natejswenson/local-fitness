# 2026-08-02 — the coach was remembering the wrong week

## Why

An efficiency audit of `workout_report_card` went looking for slow queries and
found none. Two connections, 26 queries, 0.4 ms in SQLite, no N+1 anywhere; a
warm render finishes in 1.6 ms. The whole cost of the tool is one thing: the
coach's verbal read, a ~10 s SDK call that is supposed to be cached.

So the question became whether the cache works. It does not.

```
stored cards: 15
2026-08-02  23825963527  HIT   stored=d4188462d7 now=d4188462d7
2026-07-29  23778992014  MISS  stored=7d326bc860 now=0b1cf6f5de
2026-07-28  23767829677  MISS  stored=6a9ecb9389 now=39c0426d57
...
fast-path HIT 1 / MISS 14
```

Only the card rendered *that day* hit. Every older one paid the full call
again.

## The measurement

The read is cached under a hash of its prompt, and the prompt carries the
coach's memory. The memory was resolved against the clock. Rendering the same
block for two consecutive days, same database, nothing else changed:

```
-- Steps: goal hit 7 days running (through yesterday); best in 60 days is 13.
+- Steps: goal hit 8 days running (through yesterday); best in 60 days is 13.
```

One counter that increments every day, inside the hash. That is enough. Every
stored card's key rotated overnight, forever, and there was no state in which
the cache could work for longer than a day.

## The part that actually matters

The cache is not the bug. It is the symptom.

`build_card` already refuses to grade a July run against today's plan — a
stored card is a dated snapshot, and its verdicts belong to the day it was
earned. But the *words* beside those verdicts were being written with today's
relationship in hand: today's streaks, today's plan misses, today's trailing
grade average. A read about a run on the 28th was citing facts from the 2nd.

`plan_coach` never had this problem. It has passed `today=target_date` since
it was written. The report card just never did:

```python
_memory_text = memory.render_memory_for_prompt(
    conn=conn,
    today=inputs["activity"]["date"],      # <- the run's date, not the clock's
    exclude_source_key=("report_card", _activity_key),
    user_name=_user_name,
)
```

Half of `today=` was already honoured, though. The ledger took it; the journal
sat beside it as an unbounded latest-N list, so the block described two
different moments at once and any new entry anywhere rewrote the memory of
every past artifact. `journal.list_entries` grew an `on_or_before` bound, off
by default — chat and the MCP persona pass no `today` and should keep seeing
now.

## The second thing holding the cache shut

Even with a matching key, 12 of the 15 stored rows could not be reused:

```
2026-07-28  read_keys ['distance', 'hr', 'load', 'pace']       complete False
2026-08-02  read_keys ['distance', 'hr', 'pace', 'stimulus']   complete True
```

0.40.0 renamed the read's fourth section `load` → `stimulus` when compliance
and stimulus were split. `read_is_complete` demands the current four names, so
every row written before that release fails it and regenerates a read the
database is already holding.

`card_store.migrate_read_section_names` renames the key in place, from
`db.init_schema`, once. It is idempotent and it touches `card_json` and nothing
else — `graded_at`, `read_cache_key` and every grade column stay as the render
that produced them wrote them. A stored card is a historical record; this
repairs a field name the schema moved underneath it, and regrades nothing.

## Does it work

Against a snapshot of the live database, with the generator stubbed to count
calls. Render all 15 cards, move the clock, render them all again:

| pass | before | after |
|---|---|---|
| 1 — today | 15 generations | 15 generations |
| 2 — clock +3 days | 15 | 13 |
| 3 — clock +17 days | **15** | **0** |

Pass 1 costs the same either way: any prompt change re-keys every stored card
once, and this is one. Pass 2's remaining 13 are the trailing-3-week
report-card aggregate re-converging after pass 1 rewrote all 15 rows' grades —
the flip CLAUDE.md already documents as costing at most one extra generation.
Pass 3 is the answer: a fourteen-day jump, zero calls. Before, every clock move
cost fifteen.

The migration is measurable separately — stored rows passing `read_is_complete`
went 3/15 → 15/15.

## The test that would have caught it

```python
def test_the_read_cache_key_does_not_move_with_the_calendar(...):
    _freeze_ledger_clock(monkeypatch, date.today())
    call(tools.workout_report_card, {"format": "table"})
    assert len(generated) == 1

    _freeze_ledger_clock(monkeypatch, date.today() + timedelta(days=3))
    call(tools.workout_report_card, {"format": "table"})

    assert len(generated) == 1, (
        "the read was regenerated for a card nothing had changed about")
```

With a sibling asserting the precondition — that the memory block really does
differ across those two days — so the regression test cannot quietly become
vacuous if the fixture stops being day-sensitive.

## While in there: the path is now in the perf gate

It was not benchmarked at all. The audit's numbers say the DB side is already
clean, so the assertion worth having is the one that keeps it clean:
`workout_report_card` opens exactly two connections — one shared by every read,
plus `save_card`'s own, which cannot share because it runs on a worker thread
and sqlite3 connections are same-thread-checked. That is the shape a
re-introduced per-split lookup would break, and it is invisible to a latency
gate on a small fixture.

The fixture needed splits, HR zones and paces to exercise the locomotion filter
and the three documented split exceptions rather than their abstain branches —
but adding paces to the *shared* fixture moved `get_training_plan_status`
+7.2% and `get_training_plan_progress` +4.4% on min, because
`plans.best_recent_effort` excludes paceless rows and would suddenly have been
handed 500 more. Against a `min:15%` gate that can only be rebaselined on
ubuntu CI, spending half the budget on fixture noise is how you get a false
failure three PRs later. So the report card got its own database.

The PDF path stays out of the gate. Its cost is real — the density ladder runs
all three rungs on every card measured — but WeasyPrint latency is font- and
machine-dependent and belongs nowhere near a 15% floor.
