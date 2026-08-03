# 2026-08-03 — twenty calls was the obvious problem; twenty transactions was the real one

0.46.0. The third fix from auditing usage rather than code, and the one where
the obvious framing was wrong.

The number that started it: **39 `update_plan_workout` calls**, the most-used
tool by a wide margin, about twenty of them a single plan restructure done a day
at a time. Fifty-two fitness tool calls in one day.

The instinct is "that's slow, batch it." So I measured it, because this repo's
rule is that you don't get to eyeball a performance claim:

```
20 sequential update_plan_workout over real stdio:
  75.2 ms TOTAL — 3.76 ms per call (min 1.29, max 16.36)
```

Seventy-five milliseconds. Our code contributes about 4 ms per call; the twenty
LLM round-trips dominate by three orders of magnitude. **There is no latency
problem here at all.** A batch tool justified on speed would be justified on a
number that doesn't exist.

## What the twenty calls actually cost

Two things, and I had the order of importance backwards.

**Turns and tokens.** Twenty model inferences, each carrying ~8,900 tokens of
tool schemas and a persona. Real, and worth fixing.

**Twenty independent transactions.** This is the one that matters.

`docs/mcp/update_plan_workout.md` documents the swap idiom, because the tool
cannot move a day:

> a swap is **two calls** — rest the old day, prescribe the new one

Two calls. Two transactions. Rest Saturday: committed. Prescribe Sunday: fails —
wrong date, a typo, a day that isn't on the plan. `update_active_workout` raises
`ValueError` on `rowcount == 0`.

Saturday's long run is now a rest day. Sunday has nothing. Nothing rolls back,
because nothing was ever a transaction. The plan silently lost a long run and
the only signal is an error message about the *second* call.

That is a data-loss bug hiding inside a documented idiom, and it has been there
the whole time. It didn't bite me only because the live plan happens to have a
row for all 75 calendar days, so every date I named existed.

## The fix

`update_plan_workouts` takes a list, validates in three passes before touching
anything, and writes inside one `db.connect()` — which commits on clean exit and
rolls back on any exception.

The pre-flight matters more than it looks. Without it, the transaction still
rolls back, but the error arrives after fourteen `UPDATE`s have run and tells
you nothing about which entry was wrong. With it, every target is confirmed to
exist first, and the message reads:

```
update 13 (2026-09-04): no workout on 2026-09-04 (seq 1) in the active plan —
this tool re-prescribes existing days, it cannot add one
```

The write boundary is untouched: the same `_EDITABLE_WORKOUT_COLS` whitelist,
the same `WHERE plan_id=… AND date=… AND seq=…`. `date` is the key, not a
column. A batch of sixty is still sixty re-prescriptions and cannot become a
restructure. The 60 cap is blast radius, not throughput — the live plan is 75
workouts and one malformed call shouldn't be able to rewrite all of it.

## The bug I nearly shipped writing the fix

The rest-day clear lived in `tools.py`:

```python
if fields.get("type") == "rest":
    fields["target_distance_m"] = None
    fields["target_pace_sec_per_km"] = None
    fields["target_duration_sec"] = None
    fields["target_hr_max"] = None
```

Correct — and correct *only* because `update_plan_workout` was the single caller
of `plans.update_active_workout`. The write boundary itself knows nothing about
rest days.

So the naive batch tool, calling the same boundary, would have written rest days
that kept their distance, their pace, and their HR cap. A stale
`target_hr_max` on a rest day is a prescribed ceiling for a session that no
longer exists — and `workout_report_card` grades against that column directly.

It moved to `plans.apply_rest_semantics`, at the boundary, where every caller
inherits it. Single-day behaviour is byte-identical, with a regression test
pinning that.

The general shape is worth keeping: **logic that lives in the only caller is
indistinguishable from logic that lives at the boundary, right up until there
are two callers.** You cannot tell the difference by reading it. You find out by
adding the second one.

## Proving the test tests something

The rollback assertion is the entire contract, so I checked it fails against the
implementation I might plausibly have written — a loop of single-day calls:

```
E  AssertionError: a failed batch wrote something
E  assert len(opens) == 1, "4-entry batch opened 4 connections"
```

I also reordered that test. It originally asserted the error message first and
the rollback second, which meant a naive implementation failed on *message
wording* rather than on the thing that matters. Now `_rows(seeded) == before`
comes first, so the headline failure is "you wrote something", not "your string
is different."

A test can assert the right thing in the wrong order and tell you the wrong
story when it breaks.

## Also cleaned up

The `hr_max` parameter told the model the report card grades "average HR and
time-above-cap" against it. **0.40.2 deleted that axis** — it fed a time
fraction into bands calibrated for relative magnitudes, a category error that
emitted only A+ or F across 90 days. The fraction survives as reporting only.
The wrong description was live in the tool schema and the docs page.

And the docs listed `_EDITABLE_WORKOUT_COLS` as five columns, missing
`target_hr_max`, which 0.40.0 added.

`test_docs_drift.py` checks that every tool has a page and that the pages agree
about stdio-vs-HTTP availability. It has never checked whether a page's *body*
is true. Both of these sat there through several releases. That is the next gap
worth closing.
