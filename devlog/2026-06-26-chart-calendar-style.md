# Chart calendar style — fix multi-week truncation/compression

**2026-06-26**

The colored chart styles (`bar`) rendered one horizontal row per day. Fine for
a week, but a 30- or 60-day request became a 30–60 line wall that the terminal
collapsed to a cramped ~14-line slice — so "give me 60 days" looked like ~14,
compressed and out of context. The timeframe was correct in the data; the
**rendering was too tall to display**.

## Fix

New `calendar` style in `agent/charts.py` (`render_calendar`): a week-stacked
emoji heat-grid — one colored square per day, weeks stacked top→bottom,
Mon→Sun left→right, color by the day's magnitude in the window. Any window
stays compact (90 days ≈ 13 rows), so the whole timeframe renders at once.

- Missing in-window days → ⬜; out-of-window pad → `· `.
- Right-hand weekly column: **sum** for additive metrics (steps, intensity
  minutes — `_CHART_CUMULATIVE_METRICS`), **mean** of present days otherwise.
- `calendar` is now the tool **default**; `bar` stays for short ≤2-week
  windows, `combo` (trend line) and `spark` unchanged.

## Tested to standard

- `tests/test_charts.py` — 8 new `render_calendar` cases: empty, single day,
  long-window **compactness** (60 days < 60 lines, asserted), weekly
  **mean-vs-cumulative** aggregate (mean=8 / sum=56 on a clean Mon-Sun week),
  ⬜ for a missing in-window day, `·` pad for a mid-week start, negative (TSB)
  series.
- `tests/test_tools.py` — default-is-calendar (+ compactness), explicit `bar`
  is one-row-per-day, and steps routes with `cumulative=True` (asserts a
  `7*9000 = 63000` weekly sum from the seeded fixture).
- Full suite: **557 passed**, `charts.py` 97%, total 89.6%, ruff clean.

Also: added an explicit "everything gets tested — no coverage theater" rule to
CLAUDE.md's workflow expectations.

Committed to `dev`; not released (no version bump / tag yet).
