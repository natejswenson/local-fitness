---
ticket: "#TBD"
title: "Beautiful PDF brief reports + on-demand trend charts"
date: "2026-07-07"
source: "design"
---

# Beautiful PDF brief reports + on-demand trend charts

## Goal

Two new MCP tools:

1. Render a saved daily brief (`briefings/{date}.json`) into a polished PDF
   report, visually comparable to the sibling `budget` project's monthly PDF
   reports.
2. Render any ad-hoc trend/metric question into a "beautiful" standalone
   chart image (PNG), on demand, in the same visual language as (1).

Both are **chat-triggered, on-demand tools** — not part of the automated
6:30am headless brief job (that job's V2 composer is toolless/single-turn
and structurally cannot call MCP tools).

## Why this shape

The existing `mcp__fitness__chart` tool renders pure ASCII/emoji text for
terminal display — it has no image/PDF output path, and this repo has zero
PDF/charting dependencies today. The sibling `budget` project achieves its
"beautiful" PDF look by having a live Claude Code session author fresh
HTML+CSS per report and shell out to a hardcoded local Chrome binary path
for print-to-pdf. That approach is explicitly incompatible with this repo's
"must work for any stranger's clone, no host-specific binary assumptions"
rule (CLAUDE.md), and this feature must work as a deterministic, code-driven
MCP tool rather than a live-session skill.

## Rendering stack

- **Charts:** `matplotlib`, styled via a shared stylesheet/`rcParams` module
  (no default-matplotlib look). Chosen over alternatives because it is a
  pure-Python wheel with zero native system dependencies — no Docker image
  risk.
- **PDF layout:** `WeasyPrint` (HTML+CSS → PDF), not ReportLab or Playwright.
  - ReportLab was the safer-looking pick on paper (zero native deps) but
    carries real risk of reading as a "generic invoice PDF" — no
    gradients/flexbox, core fonts limited to Helvetica/Times/Courier unless
    a TTF is embedded (a redistribution/license question for a public repo).
  - Playwright+Chromium would best match the budget report's visual freedom
    but costs 400-700MB of image size and adds non-root sandbox complexity
    (this container already runs as uid 1001) — disproportionate.
  - WeasyPrint needs a modest apt-get addition (`libpango-1.0-0
    libharfbuzz0b libpangoft2-1.0-0 libharfbuzz-subset0` or current
    equivalents) but gives real CSS. This repo's CI already runs a
    `docker-build` job (`.github/workflows/ci.yml:83`, added after a past
    corepack incident), so a native-dependency regression here would be
    caught, not silent. Given that safety net exists, WeasyPrint's CSS
    fidelity is worth the modest cost.
  - **Verify before fully locking:** render one real brief to PDF and
    screenshot it against the budget report's bar before calling this
    decision final.
- **Templating:** plain Python string-building for the HTML, not Jinja2 —
  only one template exists today, so a templating dependency isn't earned.
- **Color theme:** reuse the `budget` project's exact validated palette —
  primary series blue `#2a78d6`, status tiers good `#0ca30c` / warning
  `#fab219` / critical `#d03b3b`, neutral gridlines/chrome `#e1e0d9` — as a
  single source of truth shared by both the WeasyPrint report CSS and the
  matplotlib chart stylesheet. Not the existing ASCII chart tool's heat-ramp
  palette — brand consistency across the two "glossy" surfaces (budget PDF,
  fitness PDF/charts) takes priority over consistency with the plain-text
  terminal chart experience. Validate contrast/accessibility of the reused
  palette against new chart forms it wasn't originally designed for (line
  charts over time, calendar-style views) using the `dataviz` skill's method
  at implementation time.

## Architecture

New module `src/local_fitness/agent/visuals.py`:
- `theme.py`-equivalent constants: the palette above, matplotlib
  `rcParams`, shared CSS snippet.
- `render_chart_png(series_data, chart_type: str) -> bytes` — matplotlib,
  saved to an in-memory buffer via `savefig`.
- `render_brief_pdf(brief: Brief) -> bytes` — builds an HTML string
  (embedding chart PNGs as base64 `data:` URIs), calls
  `weasyprint.HTML(string=html).write_pdf()`.

Two new MCP tools in `agent/tools.py`, added to `ALL_TOOLS`:

### `generate_brief_report(date: str) -> {"path": str}`

Flow: read `briefings/{date}.json` → validate against the existing `Brief`
schema (reuse existing validation) → `render_brief_pdf(brief)` → write
`reports/brief-{date}.pdf` → return the path as text.

Errors (`_err`, distinct messages so the calling agent can explain rather
than surface a stack trace):
- Brief file missing for that date.
- Schema validation failure.
- WeasyPrint render failure.

### `generate_chart(metric: str, days: int, chart_type: str) -> {"path": str}`

Reuses the **same data-fetching functions** the existing ASCII `chart` tool
already calls — no new SQL/query path. This is the tool's core invariant:
the renderer never recomputes aggregates, only formats what an already-
tested function returned (mirrors the budget project's "every number must
be extracted verbatim, never recomputed" rule).

`chart_type` mirrors the ASCII tool's `style` param where it makes sense
(`line`, `bar`, `calendar`) plus forms matplotlib suits better than ASCII
(e.g. `combo` as a dual-axis chart).

Flow: fetch series via existing data functions → `render_chart_png(series,
chart_type)` → write `reports/chart-{metric}-{days}d-{date}.png` → return
the path as text.

## File & env conventions

Follows the exact `briefings/` pattern (`agent/briefs.py`):
- `LOCAL_FITNESS_REPORTS_DIR` env var, default `<project_root>/reports`.
- Gitignored, same as `briefings/`.
- Container: pre-created/chowned in the Dockerfile alongside `briefings/`,
  wired via `environment:` in compose if needed.

## Delivery (v1 scope)

File path returned as text only — no new web download route. This works
because the primary consumption path is chatting via Claude Code/Claude
Desktop on the same machine the MCP server runs on, where a local path is
directly openable. Explicitly **not** solving for phone/web-UI access to
generated files in v1.

## Testing strategy

- **Pure logic, fully tested:** any new HTML-assembly function for the PDF
  (e.g., "given a `Brief`, the generated HTML contains the exact takeaway
  text and exact metric values verbatim" — string-membership assertions,
  never recomputation checks). Data-shaping helpers already covered via
  reuse of the existing chart tool's functions.
- **Rendering smoke tests:** `%PDF-` magic bytes + non-zero page count for
  `render_brief_pdf`; valid PNG magic bytes + expected pixel dimensions for
  `render_chart_png`; both fail cleanly (not a crash) on empty/missing-metric
  input.
- **Explicitly excluded from the coverage gate:** "does it look good" — a
  screenshot/manual-review obligation per CLAUDE.md's UI-verification rule,
  not a unit test.
- No new `tests/test_security.py` cases needed — v1 adds no new `/api/*`
  route.

## Invariants

**Checkable by inspection:**
- Neither tool performs its own aggregation/rollup — all numeric values are
  pass-through from already-tested data functions.
- Both tools write only under `LOCAL_FITNESS_REPORTS_DIR` — no other write
  path.
- Palette hex values match the `budget` theme exactly, defined once in
  `visuals.py`, not duplicated per chart type.

**Testable:**
- `generate_brief_report` on a missing date returns an error, never a crash
  or a blank PDF.
- Generated PDF text (via extraction, e.g. `pdfplumber`) contains the exact
  takeaway strings from the source `Brief`.
- Generated chart PNG is non-empty, valid, and dimensioned as configured.

## Docs to update in the same PR

- `pyproject.toml`: add `matplotlib`, `weasyprint` (runtime); a PDF-text-
  extraction lib (`pdfplumber`) as dev-only for tests.
- `Dockerfile`: add WeasyPrint's apt packages to the existing apt-get block;
  create/chown `reports/` alongside `briefings/`.
- `.env.example`: `LOCAL_FITNESS_REPORTS_DIR` with a commented-out example.
- **CLAUDE.md correction (unrelated pre-existing bug, caught during
  investigation):** the claim "CI does NOT run `docker build`" is stale — a
  `docker-build` job exists (`.github/workflows/ci.yml:83`, added after the
  corepack incident). Fix this in the same PR.
- CLAUDE.md "What's already wired": add the two new tools once shipped.
- `devlog/` entry.

## Out of scope for v1

- Web download route (`/api/reports/*`) for phone/browser access.
- Auto-generation as part of the daily 6:30am job.
- Custom embedded fonts/branding beyond system fonts.
- Dark-mode PDF theme (matches budget's PDF, which also skips it — a static
  file has no viewer-side theme toggle).

## API Surface

```
generate_brief_report(date: str) -> {"path": str} | error
generate_chart(metric: str, days: int, chart_type: str) -> {"path": str} | error
```

## Acceptance Criteria

- Calling `generate_brief_report` with a date that has a saved brief
  produces a PDF at `reports/brief-{date}.pdf` containing the brief's exact
  takeaway text and metric values.
- Calling it with a date that has no saved brief returns a clear error, not
  a crash.
- Calling `generate_chart` with a valid metric/days/chart_type produces a
  correctly dimensioned, non-empty PNG at the expected path.
- Both tools' outputs are visually reviewed via screenshot against the
  budget report's bar before being called "beautiful" and done.
- `docker compose up -d --build local-fitness` succeeds with the new
  dependencies installed.
