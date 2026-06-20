# 2026-06-20 — The brief got 3× faster by measuring instead of guessing

The daily brief took ~6 minutes. That's the kind of number you *feel* every
morning. The obvious fix — and the one I designed first — was to split the
single big generation into a map-reduce fan-out: assemble the data in code, let
a planner pick the takeaway themes, then compose all the cards in parallel. It's
a clean architecture. It went through `/design` and seven rounds of quality gate
(score 11 → 0). And then Phase 0 measurement killed it.

## What the design got wrong, and how I knew

The design rested on two unverified beliefs, both of which a red-team round
flagged as "you measured a constant, not reality":

1. *The 6 minutes is parallelizable composition.* Maybe — but only if
   concurrent `query()` calls actually run in parallel on the Max-subscription
   CLI path. A 12-call probe said: **1.44× speedup at 3-wide**, under the 1.7×
   kill criterion I'd pre-registered. So fan-out would have bought almost
   nothing while adding a planner, per-card validation, and a continuity
   problem. Abandoned — on the number, not on a vibe.

2. *The cost is "extended thinking."* The earlier draft asserted "~40k thinking
   tokens" from a cost-estimate **constant** in the A/B harness. Embarrassing,
   and the reviewer was right to call it. So I instrumented a real brief: ~208
   of its ~230 seconds is one generation block emitting ~13.6k output tokens, of
   which **~93% is thinking**. Tools: 13ms. The belief happened to be true — but
   now it was measured.

## The lever that worked wasn't the one I reached for

The SDK exposes `thinking.budget_tokens`. I set it to 3072. The brief came back
at ~14k output tokens — **unchanged**. The knob is silently ignored on the
Claude Code CLI / OAuth path. A six-call diagnostic across every thinking knob
showed the truth: `budget_tokens` is inert, but `effort` propagates.
`effort="low"` cut the brief to **~82–97 seconds** — 2.5–3×.

Then the part I actually care about: quality is sacred, and the rule here is
*never eyeball prompt or model changes*. So a blind LLM-judge scored six
anonymized briefs — three high-effort, three low — on specificity, coach-voice,
non-repetition, and dead-weight. Low won every dimension. The high-effort briefs
over-think and retell the same CTL/ATL story across three cards; low-effort is
tighter. Less thinking made the brief *better*, not just faster.

## The one regression, and the table fix

Lower effort has a tell: the model occasionally drops the backslash on a `\n`
table row break, emitting `|---|---|n| RHR |` and collapsing the markdown table
into one unrenderable line. So the "top-grade CLI tables" half of the original
design earned its place after all — just not as a grand renderer. A shared
`render_table` helper (now the single source for the coach snapshot table) plus
a `fix_table_row_breaks` repair at the brief save gate means every brief renders
clean no matter how the model was sampled. Unit-tested against the exact defect
captured from a real low-effort brief.

## What shipped

`effort="low"` by default, tunable via `LOCAL_FITNESS_BRIEF_EFFORT`; token-usage
instrumentation so the next person doesn't have to guess; deterministic table
repair; and three `scripts/phase0_*` probes that turn "I think it's thinking"
into "here are the numbers." No planner, no fan-out, no per-card validation —
the simplest thing that the evidence said would work.

The lesson isn't "fan-out bad." It's that the design's real output was the
*kill criterion* — a number agreed on before the data came in, that let me throw
away a week's worth of clean architecture without flinching when reality
disagreed.
