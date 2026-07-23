# 2026-07-23 — Coach memory: the relationship ledger + the coach's journal (0.30.0)

First half of the memory-based-personality build (the tunable personality
spec and the new hard-ass default ship separately as 0.31.0). Until today
every brief and report card judged the day in isolation — observations were
captured but never reached the voice, and the coach had no record of its own
past reads. Now every voice surface carries a two-layer memory, so the coach
can say "third missed quality day this month — Jul 12, Jul 19, today" and
mean it.

## The two layers

- **Ledger (computed)** — `agent/ledger.py`, pure section + persistence
  divider like `plans.py`. Adherence miss/done streaks from
  `build_plan_detail`'s verdicts (one grader, one truth), step-goal streaks
  as-of-yesterday, observation repeat-patterns (thresholds at the scale
  ends; injury counts from the first log), notable results. The
  `interpret.py` rule extended to relationships: Python derives every count
  the coach may quote, the LLM only phrases.
- **Journal (written)** — `coach_journal` table via `agent/journal.py`.
  Short dated lines the coach writes itself ("Blamed the heat again —
  second time this month"). 60-entry cap pruned on every write, 240-char
  lines, partial unique index on `(source, source_key, seq)` so a reflect
  race dies loudly instead of double-writing.

## Writes

`agent/reflect.py` — a toolless Sonnet-low single-shot (workout_coach's
measured ~10s config) that fires after each **saved** brief and each
**first-render** report card, emitting `MEMORY:` lines (0–2) or `NONE`.
Fail-silent and post-persistence by construction. Chat gets
`save_coach_memory`/`list_coach_memories`/`delete_coach_memory`.

## Gotchas worth remembering

- **Memory is passed into the prompt builders, never resolved inside.**
  Two reasons discovered in the code, not the design: `prompts.py` builds
  `SYSTEM_PROMPT` at import (internal resolution = DB open on import), and
  the PDF coaches key disk caches on the assembled-prompt hash.
- **The reflect self-cascade is real and broken twice.** Reflecting on a
  card changes the memory, which would change that card's next prompt,
  bust its cache, and regenerate (and re-reflect) forever. `has_event`
  short-circuits re-renders; `exclude_source_key` drops an artifact's own
  journal entries from its own prompt.
- **Step streaks are as-of-yesterday on purpose.** Today's step count is
  partial all day; a block that flips intra-day invalidates the prompt-hash
  caches on every render instead of once per day.
- **`limit: 0` and `or`-defaulting don't mix.** `args.get("limit") or 50`
  silently turned an explicit invalid 0 into the default; caught by the
  validation test, fixed with an explicit `is None` check.

Kill switch: `LOCAL_FITNESS_COACH_MEMORY=0` (injection + reflect, data
untouched). 1581 tests green, 94.8% coverage, scorer 11/11, perf gate green.
