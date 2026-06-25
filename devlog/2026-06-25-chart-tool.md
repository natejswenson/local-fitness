# Terminal charts from your fitness data (`chart` tool)

**2026-06-25 · v0.13.0**

Nate wanted to see graphs in the Claude Code terminal, straight from the app's
data — and specifically asked whether "active minutes" was available and whether
the charts could be vibrant/multicolor with bar charts and trend lines.

Both answers were yes, with one caveat worth recording.

## Active minutes was already there

Garmin calls it *Intensity Minutes* — `intensity_minutes_moderate` and
`intensity_minutes_vigorous`, both already in `daily_metrics` and fully
populated. The number that matches the watch's weekly-goal badge is
`moderate + 2×vigorous` (Garmin double-weights vigorous). That became a derived
chartable metric, `intensity_minutes_weighted`.

## The color finding (why we prototyped first)

Before building, I prototyped four rendering styles against the *real* terminal
and printed them as raw stdout. The result was decisive: **ANSI color escapes do
not survive the path from tool output to the rendered display** — they leak as
literal `[38;2;…m` garbage. The only color that survives is emoji/Unicode
*glyphs*. Good thing we tested instead of claiming.

That constraint forced the design:

- **`bar`** (default) — horizontal bars built from heat-ramp square emoji
  (🟦🟩🟨🟧🟥). Color survives. Emoji are double-width and can't be overlaid, so
  no trend line here.
- **`combo`** — a 2D canvas of vertical `█` bars with a least-squares trend line
  (`•`) overlaid, a labeled y-axis, and correct handling of **negative series**
  (TSB / freshness lives below zero). Monochrome by necessity — thin box-drawing
  glyphs align where emoji wouldn't.
- **`spark`** — a one-line block-glyph sparkline for dense/long windows.

## Shape of the change

- `agent/charts.py` — three pure renderers (no DB), unit-tested in isolation,
  mirroring how `render.py` keeps table rendering pure.
- `agent/tools.py` — the `chart(metric, days, style)` tool. Metric is validated
  against a frozen whitelist (every daily numeric column ∪ `ctl/atl/tsb` ∪ the
  derived weighted-intensity series) before any column name reaches an f-string;
  values are parameterized — same SQL-safety contract as `get_metric`.
- The tool joins `ALL_TOOLS` (chat/coach can call it) but is **kept out of the
  brief's read-only tool set** — the brief renders its own UI cards, so terminal
  ASCII has no place there. Same call we made for `daily_snapshot`.
- Tests: `tests/test_charts.py` (renderer edge cases — empty, flat, single
  point, negative TSB) and new `chart` cases in `tests/test_tools.py`.

Full suite green (341 passed, 65% coverage), ruff clean.
