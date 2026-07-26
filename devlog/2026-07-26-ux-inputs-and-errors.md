# 2026-07-26 — UX pass: the tool surface stops lying politely (0.37.0)

## Why

Batch 3 of the three-axis audit. The theme: an MCP tool surface is a UI
whose only user is a language model, and this one had UI bugs — inputs that
silently meant something else (`min_distance_km` in a miles app;
`pace_min_per_mi: 9.39` prescribing 9:23), failures indistinguishable from
empty results (reversed `compare_periods` ranges returning `n: 0`;
impossible dates matching nothing in SQL), truncation without a signal
(`query_workouts` clipping "all my runs this year" to 50 rows silently),
and errors stripped of the one detail the model could act on (`run_sql`
hiding "no such column: sleep_hours").

## What

- One `_validate_date` (fromisoformat) replaces the shape regex + the
  try/except idiom across every dated tool; `_validate_limit` and
  `_min_distance_meters` join `_validate_days` as shared guards.
- Pace on `update_plan_workout` accepts `"M:SS"` (round-tripping the app's
  own display format) with a 3:00–30:00/mi plausibility band; units.py
  gained the public constants and the inverse conversions.
- Truncation envelopes (`limit+1` fetch → `truncated`) on query_workouts
  (now an object payload — the one breaking shape change), list_report_cards,
  list_coach_memories.
- Error detail rebalanced: run_sql now INCLUDES the sqlite message (a
  deliberate reversal, safe because the read-only URI gate bounds what can
  appear there); pydantic errors compact to `loc: msg`; PDF failures log
  the traceback and return one stable line.
- `get_metric` mirrors `get_metric_trend`'s baseline read; the `/coach`
  snapshot renders training-load freshness (`as of …` + stale warning)
  beside the brief banner that already existed.

## Gotchas

- `sd_threshold: 0` was being swallowed by `or 2.0` — an explicit zero
  (every day an anomaly) silently became the default. `None`-check, not
  falsy-check, for numeric params.
- The old `_DATE_RE` looseness was load-bearing-safe only by accident
  (briefs.save_brief stamps dates server-side); the new validator makes the
  safety local instead of two modules away.
- `interpret.TSB_*` constants now render into `training_load_status`'s
  description at decorator time — the same drift that was fixed once in
  `correlate`'s legend had quietly survived here.
