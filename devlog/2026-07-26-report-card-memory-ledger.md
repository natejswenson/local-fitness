# 2026-07-26 — Past report cards join the coach's memory (0.34.0)

## Why

Every workout gets graded into a stored report card, but none of that
history was part of what the coach *remembers* — only discoverable if the
user thought to ask and the model happened to call `list_report_cards`.
The ask: make the past 3 weeks of grading genuinely part of standing
memory. The catch: `card_store.py`'s own docstring (and `CLAUDE.md`)
explicitly warns against injecting stored cards into
`render_memory_for_prompt` — that function's output is baked into the
`plan_coach`/`workout_coach` prompt-hash cache keys, so anything injected
there has to be cache-stable or every card save invalidates every future
render's cache.

## What

Designed by a higher-tier model (Fable 5) via the Plan agent, verified
against the real code before implementing with Sonnet 5:

- **`ledger.report_card_facts`** — a new deterministic fact (count, mean
  GPA, grade distribution, trend) computed ONLY over cards dated strictly
  before today, mirroring `step_streak_facts`'s existing "as of yesterday"
  rule for the identical reason. Flows through the existing ledger block
  to all four voice surfaces automatically — no `memory.py` change needed.
- **A standing chat directive** in `system_prompt()` telling the coach to
  call `list_report_cards`/`get_report_card` for grade/trend questions.
  Zero cache risk: the chat system prompt is resolved live per MCP
  connect, never disk-cached.

## The residual, stated plainly

The as-of-yesterday cutoff isn't fully watertight: grading a *prior-day*
workout mid-day (common — grading last night's run this morning) flips
`memory_text` once that day. This is bounded and self-converging, not a
cascade — the aggregate depends only on `activity_date`/`gpa`/
`overall_grade`, none of which change on a re-save, so a second render
regenerates once and a third converges. Same order of magnitude as an
existing intra-day mutation the system already tolerates (an auto-reflect
journal write after any first-render card). Documented in `CLAUDE.md`
rather than silently accepted; the watertight fix (`first_graded_at`,
set-on-INSERT-only) is deferred unless it proves annoying in practice.

## What didn't change

- `reflect.py`'s "no letter grades in journal text" rule — deliberate,
  out of scope.
- No new tools, no `card_store.py` API change — `list_report_cards`
  already supported the date-range query; the gap was purely the missing
  standing directive to use it.
