# `chart`

> Chart one daily metric over the last N days — terminal ASCII/emoji (default) or a rendered PNG returned inline (`format="png"`). **Availability:** stdio + HTTP

## What it does

Answers "show me my resting HR for the last month". It plots ONE metric from
`daily_metrics`, one of the three training-load series from `baselines`
(`ctl`/`atl`/`tsb`), or the derived `intensity_minutes_weighted` — as terminal
text you paste straight into a reply (the default), or as a polished matplotlib
PNG returned inline (`format="png"`; the former `generate_chart` tool, folded
in at 0.57.0 — same whitelist, same window semantics). For "scheduled vs
actual" / "am I hitting my plan", neither format is right — that is
[`plan_chart`](plan_chart.md), which is a two-series view these single-series
renderers cannot express.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `metric` | string | yes | — | Whitelisted against `_CHART_METRICS`: every column in `DAILY_NUMERIC_METRICS`, plus `ctl` / `atl` / `tsb` (read from `baselines`, not `daily_metrics`), plus `intensity_minutes_weighted` (derived: `moderate + 2 × vigorous`). An unknown name errors and echoes the allowed list. |
| `days` | integer | yes | — | Trailing window ending today. The cutoff is `today - days` **inclusive**, so a full-data 28-day ask reports `n=29`. Bounds-checked to `1..3650`; a non-int (including `bool`) or an out-of-range value errors rather than being clamped. |
| `style` | string | no | `calendar` (ascii) / `line` (png) | ascii: `calendar` / `line` / `bar` / `combo` / `spark`. png: `line` / `bar` / `combo` only — an ascii-only style with `format="png"` errors with the allowed list. |
| `format` | string | no | `ascii` | `ascii` = terminal chart in a text block. `png` = matplotlib image returned as an inline image content block, plus the saved file path as text (content-addressed filename `chart-{metric}-{style}-{N}d-<sha8>.png`, auto-opened locally). |

### Which style

| Style | Shape | Color | Good for |
|---|---|---|---|
| `calendar` | Week-stacked heat grid, Mon→Sun, one cell per day, weekly aggregate in the right column | emoji heat ramp | The default. Stays compact at any window — 90 days is ~13 rows. |
| `line` | Smoothed, down-sampled curve in box-drawing glyphs (`─ ╭ ╮ ╰ ╯ │`) with a y-axis | mono | Trend shape over a long window. |
| `bar` | One horizontal bar per point | emoji heat ramp | Windows of ~2 weeks or less. |
| `combo` | 2D vertical bars plus a least-squares trend line (`•`) on a labelled y-axis | mono | Series that go negative — TSB / freshness — and any "is this rising or falling" ask. |
| `spark` | One-line block sparkline plus min..max | mono | A dense series inline in a sentence. |

## Returns

A single text content block holding the rendered chart **verbatim** — plain
text, not JSON. (Errors are the exception: those come back as
`{"error": ..., ...}` with `is_error` set.)

Every chart opens with a title line of the form
`{metric} · last {days}d · n={samples}`. `calendar` follows it with a legend
line naming the low/high ends of the ramp; `plan_chart`-style verdict glyphs do
not appear here — the ramp is neutral magnitude, not good/bad.

```
rhr · last 28d · n=29
🟦 47 (low) → 🟥 55 (high)   ⬜ no data · ⬛ outside · rows = weeks (Mon→Sun) · right = wk avg
Jun 22  ⬛🟨🟨🟧🟩🟨🟩   51
Jun 29  🟨🟩🟩🟩🟨🟩🟨   50
Jul 06  🟨🟨🟧🟨🟨🟥🟩   52
Jul 13  🟨🟧🟦🟩🟩🟧🟩   51
Jul 20  🟩🟦⬛⬛⬛⬛⬛   48
```

`combo` returns an axis instead, and closes with a fitted-endpoint trend
footer formatted in the same units as the axis:

```
tsb · last 21d · n=22 · weekly avg
-14.9 ┤ █
      ┤ ██•
      ┤ █•
      ┤ ███
-27.1 ┤ •██
      ┤•███
      ┤ ███
      ┤ ███
-39.3 ┤████
      └────
       trend -30.7 → -17.1 · rising
```

(Note the ` · weekly avg` in that title and the four columns for a 21-day ask —
`combo` bucketed to weeks, and the negative axis is why `combo` is the style for
TSB.)

## Example

**Ask:** "how's my resting heart rate looked this month?"

```
chart(metric="rhr", days=28)
```

Then paste the full block into the reply (see the first gotcha) and add the
coach read on top of it: *"You spent the first three weeks bouncing 50–52 and
the last four days at 47–48. That's the taper showing up."*

### The png format

`format="png"` renders the same series with matplotlib: `line` (axis chart with
gridlines, default), `bar` (vertical bars), or `combo` (bars + a least-squares
trend line of the same metric — one metric, one axis, never a dual-axis chart).
The response carries TWO content blocks — the saved file path as text, then the
PNG as an inline image — so a networked `/mcp/` client sees the chart without
needing the path. Filenames are content-addressed (changed data lands on a new
file so macOS `open` shows fresh bytes instead of refocusing a stale window;
identical data reuses one file).

## Gotchas

- **Reproduce an ascii chart in your reply, in a fenced code block.** A chart left
  only in the tool call renders collapsed in the Claude Code UI and forces a
  Ctrl-O to see it — Nate flagged this as "very unfriendly". This is a standing
  requirement in CLAUDE.md, not a preference: paste the output, *then* add the
  read.
- **Color rides on emoji, deliberately.** The 2026-06-25 prototype established
  that ANSI escapes are stripped between tool text and the display, so the only
  color that survives is emoji/Unicode glyphs. That is why `calendar` and `bar`
  use the `🟦🟩🟨🟧🟥` ramp and why `line`/`combo` are monochrome instead: the
  double-width emoji cannot be overlaid with a trend line or aligned against
  ASCII, and box-drawing glyphs can. Don't "fix" the mono styles by adding
  color.
- **The ramp is magnitude, not judgment.** 🟥 means "high in this window" for
  both sleep (good) and RHR (bad). Say which in the coach read.
- **`bar` and `combo` bucket to Monday-anchored weeks past 21 points.** The
  title gains ` · weekly avg` (or ` · weekly sum` for cumulative metrics like
  `steps`, `intensity_minutes_*`, `body_battery_charged`). A 90-day `bar` ask is
  honored as 13 weekly rows rather than 90 lines.
- **The calendar's right-hand column is a weekly SUM for cumulative metrics and
  a mean of present days otherwise.** Days inside the window with no value
  render `⬜`; days outside it render `⬛` (an emoji-width pad, so columns stay
  aligned).
- **`ctl`/`atl`/`tsb` come from `baselines`**, which only has rows for days the
  baseline recompute has covered — a window ending before the last recompute can
  return fewer points than `days` suggests.
- **An empty window is an error, not an empty chart** (`no data in window`).

## See also

- [`plan_chart`](plan_chart.md) — scheduled vs actual; the only two-series view.
- [`get_metric_trend`](get_metric_trend.md) — the numbers behind the shape.
