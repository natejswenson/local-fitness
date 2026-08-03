# 2026-08-03 — a 59-workout plan was sitting in the database for twelve days

0.44.0. This one came out of reading my own usage rather than my own code.

I audited the MCP surface by mining every recorded session — 247 real tool
invocations — and ranking the tools by how often I actually call them. The top
of the list:

```
 39  update_plan_workout
 27  workout_report_card
 25  run_sql
 25  update_coach_personality
```

`update_plan_workout` edits exactly one day. Thirty-nine calls. One day in
July: fifty-two fitness tool calls, about twenty of them walking the active
plan into a new shape a day at a time — which is precisely what
`docs/mcp/update_plan_workout.md` tells you not to do.

The obvious read is "add a batch tool." That's on the list. But it's the wrong
first question, and the right one was hiding in the bottom half of the table:

```
  2  propose_training_plan
  0  commit_training_plan
  0  discard_training_plan_draft
  0  get_training_plan_draft
```

I proposed plans. I never committed or discarded one. So I went and looked:

```
plan_id status   n   created            title
4       active   75  2026-07-06T08:23   10K — Sub-48:30 (Sept 18)
5       draft    59  2026-07-22T08:05   10K sub-48:30 — walk-supported rebuild
```

A fifty-nine-workout plan, drafted on 22 July, still sitting there on 3 August.
I have no memory of it. I patched plan 4 by hand for twelve days while a whole
rebuild of it waited in a status nothing reads.

## Why it was invisible

`get_training_plan_status` and `get_training_plan_progress` — the two tools any
agent reaches for — both call `plans.get_active_plan`. Neither has a draft
branch. The only tool that can see a draft is `get_training_plan_draft`, and
you have to already suspect a draft exists to call it.

So the coach never mentioned it, because the coach genuinely could not know.

The docs were honest about this, in four separate places, including a gotcha
that read:

> **Drafts are invisible here.** `{"active": false}` while a draft is sitting
> unopened is expected — use `get_training_plan_draft` to see it.

Documented, deliberate, and still wrong. "Expected" was doing a lot of work in
that sentence.

## The part that makes it a bug rather than a gap

`plans.insert_draft` archives any existing draft. One draft slot, last write
wins.

So an unsurfaced draft isn't merely forgotten — it's *destroyed by the next
proposal*, silently, with no error and nothing in any payload to say it
happened. If I'd asked for a new plan last week, plan 5 would have gone to
`archived` and I would never have learned it existed.

An invisible thing that also silently deletes is worse than an invisible thing.

## The fix

`get_training_plan_status` gains `pending_draft`:

```json
{"active": true, ...,
 "pending_draft": {"plan_id": 5, "title": "10K sub-48:30 — walk-supported rebuild",
                   "created_at": "2026-07-22T08:05", "workout_count": 59,
                   "first_date": "2026-07-23", "last_date": "2026-09-19"}}
```

Three decisions worth writing down.

**It's a summary, not the plan.** This tool's description has said "slim by
design" since it was written, and the full draft has its own tool. Counts and a
date span are enough to say "you have a loose end"; anything more is a second
tool's job.

**It resolves before the no-active-plan early return.** The first draft of the
patch put it after, which meant `{"active": false}` — no active plan, nothing
governing your training — was still the one payload that couldn't tell you a
plan was sitting there ready to commit. That's the state where it matters most.

**It costs no extra connection.** `get_training_plan_status` is on the
perf-benchmark gate, which fails at 15% over the committed floor. `get_draft_plan`
didn't accept a `conn` the way `get_active_plan` does, so the naive version
opened a second one. It now takes the handler's existing connection, and I A/B'd
it over 300 iterations against the synthetic fixture rather than assuming:

```
with the draft read:     median 0.650 ms
without:                 median 0.642 ms   (+1.2%)
```

The open-count has its own test, and I checked the test actually bites by
reverting to a second connection:

```
E  AssertionError: expected 1 db.connect(), got 2
```

## The lesson

Four of the audit's findings were things I could have found by reading the code.
This one was only visible from the outside — from a table of what I *do*, next
to a table of what the tools *offer*. Twenty tools out of forty-six have never
been called once. Some of those are fine and situational. But a cluster of
consecutive zeroes across `commit` / `discard` / `get_draft` wasn't the tools
being unnecessary. It was a workflow with no exit, and a database row quietly
holding twelve days of work.

Usage data is a bug report you have to go and read.
