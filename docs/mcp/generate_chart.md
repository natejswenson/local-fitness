# `generate_chart`

> Standalone matplotlib PNG (line/bar/combo) of one metric over the last N days, returned inline as an image. **Availability:** stdio + HTTP

## What it does

The same metric whitelist and the same window as [`chart`](chart.md), rendered
as a real, brand-themed PNG instead of emoji text. Reach for it when the answer
wants to be *looked at* — a shareable image, a smooth axis, a fitted trend line
drawn properly. Reach for `chart` instead when the answer wants to be *read* in
the transcript: ASCII/emoji reproduces in a code block, survives copy-paste, and
costs nothing to render. For scheduled-vs-actual questions neither applies —
that is [`plan_chart`](plan_chart.md).

### Why this one is NOT local-only

The rule that decides membership in `LOCAL_ONLY_TOOLS` is: **a tool that hands
back a filesystem path is local-only**, because a remote `/mcp/` caller gets a
container-internal path it has no way to retrieve. `generate_chart` used to sit
there for exactly that reason. Since the 2026-07-13 MCP-speed fold-in it returns
the PNG as an **inline MCP image content block** alongside the path, so a
networked client no longer needs the file — and the tool moved into `ALL_TOOLS`,
reachable over both `fitness mcp-stdio` and the authenticated
streamable-HTTP `/mcp/` transport. The two PDF tools
([`generate_brief_report`](generate_brief_report.md),
[`workout_report_card`](workout_report_card.md)) stay local-only because a PDF
is not representable as an MCP content block.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `metric` | string | yes | — | Same `_CHART_METRICS` whitelist as `chart`: any `daily_metrics` numeric column, `ctl`/`atl`/`tsb` (from `baselines`), or the derived `intensity_minutes_weighted`. Unknown names error with the allowed list. |
| `days` | integer | yes | — | Trailing window ending today, cutoff inclusive (a 28-day ask plots 29 points on full data). Bounds-checked to `1..3650`. |
| `chart_type` | string | yes | — | `line` \| `bar` \| `combo`. No `calendar` or `spark` here — those are text-only styles, deliberately out of scope. |

| `chart_type` | What it draws |
|---|---|
| `line` | Ink line at 2px with a 6%-alpha fill down to the padded axis floor (not to zero). |
| `bar` | Vertical ink bars at 100% alpha. |
| `combo` | The same bars at 55% alpha plus a least-squares trend line of the **same single metric** in the brand accent. One metric, one axis — this is not a two-metric dual-axis chart. |

## Returns

**Two content blocks**, not one — this is the shape that makes the tool
transport-agnostic:

1. a `text` block holding `{"path": "<absolute path to the written PNG>"}`
2. an `image` block: base64 PNG data with `mimeType: "image/png"`

```json
{
  "content": [
    {"type": "text", "text": "{\"path\": \"/var/folders/.../local-fitness-reports-48213-x9f2a1/chart-rhr-line-28d-2026-07-21.png\"}"},
    {"type": "image", "data": "iVBORw0KGgoAAAANSUhEUg...", "mimeType": "image/png"}
  ]
}
```

Errors come back as a single text block, `{"error": ...}` with `is_error` set —
unknown `chart_type`, out-of-range `days`, unknown `metric`, `no data in
window`, or `chart render failed: ...`.

The file is written atomically to the reports directory and, on macOS,
auto-opened in the default viewer (best-effort — a failed `open` logs a warning
and never fails the call). Filename:
`chart-{metric}-{chart_type}-{days}d-{today}.png`.

## Example

**Ask:** "give me a picture of my fitness trend for the last 90 days"

```
generate_chart(metric="ctl", days=90, chart_type="combo")
```

The image comes back inline, so present it and add the read — the trend line is
the accent and it is the only orange on the page by design.

## Gotchas

- **The value axis is scaled to the data band, never zero-based.** Resting HR
  living between 48 and 57 on a 0-based axis is a flat sliver with 85% of the
  canvas empty; `value_axis_bounds` pads 8% either side of the real range (a flat
  series pads by `max(1, 5% of magnitude)`). Bar rectangles are still drawn from
  zero but clip at the padded floor — the standard truncated-bar look. This was
  a deliberate call (Nate, 2026-07-19: "autoscale everything; zero-basing is
  pointless"), not an oversight.
- **Styling comes from the brand theme** (`agent/branding.py`), PRESS by default:
  warm paper `#F5F0E6`, ink `#181510`, dim gridlines, and exactly ONE accent
  `#E8501F` — used only for the `combo` trend line. Override colors or fonts by
  pointing `LOCAL_FITNESS_BRAND_FILE` at a JSON file, which deep-merges over the
  default; a missing or broken brand file logs a warning and falls back rather
  than failing the render.
- **Output location.** By default the PNG lands in a per-process ephemeral
  `tempfile.mkdtemp()` directory (`local-fitness-reports-<pid>-*`) that is swept
  at process exit — a fresh `fitness mcp-stdio` subprocess per session is the
  natural cleanup boundary. Set `LOCAL_FITNESS_REPORTS_DIR` to write somewhere
  persistent instead (still auto-opened, never auto-cleaned).
- **No WeasyPrint here.** This tool is matplotlib-only, so it does NOT need the
  native Pango/HarfBuzz setup (`DYLD_LIBRARY_PATH=$(brew --prefix)/lib` on
  macOS) that the two PDF tools require.
- **Idempotent only within a calendar day.** The date in the filename comes from
  `date.today()`, so an identical metric/type/days call overwrites the same file
  today and writes a new one tomorrow.
- **Long windows thin their x labels** (every `len//10`th tick past 14 points),
  the same idea as `chart`'s weekly bucketing — but unlike `chart`, the values
  themselves are never bucketed here.
- **Still say something about it.** An image is not a read. Pair it with the
  interpretation the same way a `chart` block gets one.

## See also

- [`chart`](chart.md) — the text-reproducible version of the same series.
- [`plan_chart`](plan_chart.md) — scheduled vs actual.
- [`generate_brief_report`](generate_brief_report.md) — the PDF that embeds these
  same PNGs, one per takeaway.
- [`get_metric_trend`](get_metric_trend.md) — the numbers behind the picture.
