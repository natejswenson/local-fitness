# 2026-07-22 — Don't show the model the thing you told it not to say

## Why

Carried over from the 0.28.0 work: the report card's verbal read named a letter
grade despite `_GRADE_TONE` forbidding it in capitals. I'd reported the rate as
"~10%".

## The rate was wrong, and that mattered

My first detector counted the article "A" as a grade. "A blown interval session
that's also light on load" scored as a leak. So did the earlier A/B's 2/20 and
3/20 columns.

Rebuilt it and validated it against 12 hand-written cases first — 5 real leaks,
7 lookalikes — before trusting a single number. True rate: **3 of 96 paragraphs
(3.1%)** across 3 real cards.

The lesson isn't "measure twice". It's that a measurement instrument needs its
own test suite before its output is allowed to drive a decision.

## The retry that taught me the actual bug

The agreed fix was detect-and-regenerate-once. I built it, then ran it against
14 live generations. One still leaked — and the log said:

```
workout_coach read named a grade ('D-') — regenerating once
workout_coach read named a grade twice ('D-', 'D-') — keeping the first
```

The same letter, twice. That is not what independent sampling looks like, and
it killed the "~99.9%" estimate I'd put in front of Nate when he chose the
option. So I looked at the prompt:

```
Overall grade D (1.05 GPA).

Metric grades (already computed — phrase, don't re-derive):
  Distance: D- — actual 5.95 mi vs target 5.00 mi.
  Pace: F — actual 9:25/mi best mile vs target 6:58/mi.
```

We were printing every letter directly beside the metric the model was being
asked to write about, in a prompt whose system half said NEVER state a letter
grade. `_GRADE_TONE` then spelled out `"A"`, `"B-"`, `"C+"` as examples of what
not to write, planting four more.

The read wasn't disobeying. It was completing.

## The fix

Carry **severity** instead of the letter: `on target → slightly off target →
off target → well off target → missed badly`.

Severity, not silence. Deleting the judgment would have been worse than the
leak: a +19% distance overshoot is a D- only because intent scaling says an
interval day is no place for bonus miles, and no amount of staring at
"5.95 vs 5.00" recovers that. The read would have praised an overshoot the
table grades D-, which is the one thing CLAUDE.md says a card must never do.

`_GRADE_TONE` now states the ban without demonstrating it.

## Result

Identical protocol, 3 cards × 8 generations:

| | leaking paragraphs | median latency |
|---|---|---|
| before | 3 / 96 (3.1%) | 9.3s |
| after | **0 / 96 (0.0%)** | 9.1s |

The retry guard stayed in as a backstop, but it is no longer the mechanism. It
is deliberately narrow — a bare "A" is almost always the article, and a false
positive throws away a clean read and pays for another generation. Its
specification is two lists in the test file (8 real leaks, 9 lookalikes) that
both have to be extended before the pattern is touched.

## Noted, not fixed

27% of paragraphs run over the 45-word budget. That is not a regression (the
pre-change measurement was 35% on one card, 20% on another) — the budget has
always been loosely honored. Separate problem, separate change.

## Verification

1478 passed, `ruff` clean, and the before/after leak measurement above.
