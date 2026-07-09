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
    equivalents) but gives real CSS. **This addition lands only in the CI
    `validate` job, not the `Dockerfile`** — `validate` runs `pytest`
    directly on bare `ubuntu-latest`, where importing `weasyprint` would
    otherwise fail outright, but the container's `CMD` (`Dockerfile:101`)
    only ever runs `build_session_manager()`'s path, which is
    structurally unable to reach `LOCAL_ONLY_TOOLS`/`visuals.py` (see
    Architecture) and so never imports WeasyPrint at all. The CI
    `docker-build` job (`.github/workflows/ci.yml:83`, added after a past
    corepack incident, `push: false`) gains nothing from these packages
    either, since it only builds the image and never runs code inside it
    (see Docs to update, which spells out this Dockerfile omission as
    deliberate).
  - **Fonts:** `python:3.12-slim` (and bare `ubuntu-latest`) ship with no
    fonts by default. WeasyPrint renders text through Pango/Cairo, so with
    no font package installed it produces blank/tofu text — a gap that
    exists on the CI runner even though it works fine on a dev machine
    with system fonts already present (it would exist in the container
    too if the container ever called WeasyPrint, but it structurally
    cannot, per above). `fonts-dejavu-core` (or `fonts-liberation`) plus
    `fontconfig` must be installed alongside the Pango/harfbuzz packages
    in the CI `validate` job — not the Dockerfile, for the same reason.
  - **macOS host native dependency:** `run_stdio()` is the only place
    these tools are reachable in v1 (see Delivery), and it runs directly
    on Nate's laptop, not inside the container. WeasyPrint binds to
    system Pango/Cairo/HarfBuzz via cffi/dlopen at import time — a stock
    macOS machine does not ship these, and `pip`/`uv` alone cannot supply
    a native library (only the Python wheel around it). `brew install
    pango` (which pulls in cairo/harfbuzz as dependencies) must be run
    once on the host before the first call to `generate_brief_report` via
    `uv run fitness mcp-stdio` — this is a manual step distinct from the
    CI-only apt packages above (the Dockerfile carries none, see Docs to
    update), and it must be captured in Docs to update.
  - **Verify before fully locking:** render one real brief to PDF and
    screenshot it against the budget report's bar. **A container-based
    check is not meaningful here and must not be attempted:** the
    `Dockerfile` deliberately carries none of WeasyPrint's native packages
    (see Docs to update), because the container's only process can never
    reach `LOCAL_ONLY_TOOLS` (see Architecture) — so the built container
    image cannot render a WeasyPrint PDF at all, by design, not by gap.
    The missing-font (blank/tofu-text) risk is instead caught by the CI
    `validate` job's test suite, which runs `pytest` (and therefore any
    test that imports `weasyprint`) on bare `ubuntu-latest` with the
    Pango/harfbuzz/fonts packages installed there (see Docs to update) —
    a genuinely font-less Linux environment, unlike a host dev machine
    with system fonts already present. **The verification that matters
    for v1** is on the actual host machine: after running `brew install
    pango`, call `generate_brief_report` via `uv run fitness mcp-stdio` on
    Nate's laptop and confirm it renders and screenshots cleanly against
    the budget report's bar — this is the only real v1 delivery path (see
    Delivery), and unlike the CI runner it has no packages pre-installed
    by an automated step, so it must be checked directly rather than
    inferred from CI going green.
- **Templating:** plain Python string-building for the HTML, not Jinja2 —
  only one template exists today, so a templating dependency isn't earned.
- **Markdown rendering:** `Takeaway.details` is raw markdown, rendered
  client-side via a markdown component in
  `web/src/components/TakeawayCard.tsx`. Dropped in raw, `**bold**`/`- list`
  syntax would render as literal text in the PDF, undermining the
  "beautiful" bar the whole feature exists for. Use a pure-Python
  markdown→HTML library (e.g. `markdown` or `mistune` — zero native deps,
  consistent with this design's own dependency-minimalism reasoning) to
  render `details` before embedding it in the report HTML. Unlike Jinja2
  above, this dependency is earned: there is no plain-string-building
  substitute for correct markdown semantics (nested lists, emphasis,
  escaping). **Tables must be explicitly enabled.** `agent/briefs.py`'s
  `save_brief()` already calls `fix_table_row_breaks(tk["details"])`
  specifically to repair collapsed markdown tables — proof this shape
  routinely appears in production `details` content — and the frontend
  renders `details` via `<ReactMarkdown remarkPlugins={[remarkGfm]}>`,
  where `remarkGfm` is what adds GFM table support on that side. Neither
  candidate backend library supports tables by default: `markdown`
  requires `extensions=['tables']`, `mistune` requires
  `plugins=['table']`. Whichever library is chosen, the table
  extension/plugin must be explicitly enabled at construction time, or a
  `details` field containing a table renders as raw, unaligned pipe
  characters in the PDF instead of an actual table. **Defense-in-depth against embedded HTML/network fetches:**
  both candidate markdown libraries pass raw embedded HTML through by
  default (no escaping), and WeasyPrint's default fetcher performs live
  network requests for any external `<img src>`, `<link rel=stylesheet>`,
  or CSS `@import` it encounters in the rendered HTML. Brief composition
  can be influenced by free-text user notes, so "the model will never
  emit an image tag or URL" is an assumption, not a guarantee. Pass a
  `url_fetcher` to `weasyprint.HTML()` that rejects everything except the
  `data:` scheme (chart images are already embedded as `data:` URIs, so
  nothing legitimate needs network access) — this closes both the
  network-fetch and unsanitized-markdown-HTML-passthrough risk in one
  change, since neither vector can reach anything without a working
  fetcher, consistent with this repo's own defense-in-depth house style
  (redundant metric whitelisting, path-containment checks). **This
  covers `details` only — `headline`/`summary`/any other raw-interpolated
  `Takeaway` field is plain Python string-building with no templating
  engine's autoescape underneath it (that protection is exactly what
  rejecting Jinja2, above, gives up), so those fields must be passed
  through `html.escape()` before insertion into the HTML template.**
  Coach-voice text plausibly contains ordinary `&`/`<`/`>` characters
  (e.g. "sleep & HR both dipped", "keep effort < 6/10") that would
  corrupt the HTML structure if inserted raw, silently garbling that
  takeaway's rendering. Only `details` goes through the markdown
  library's own escaping (per the markdown-rendering note above); every
  other raw-interpolated field goes through `html.escape()` explicitly.
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

**Tool registration is local-only, structurally — not just documented.**
`agent/tools.py`'s `ALL_TOOLS` list is consumed by *both* `run_stdio()`
(host-only, single machine) *and* the streamable-HTTP transport mounted at
`/mcp/` in `src/local_fitness/web/server.py` (~lines 96/144) — which, per
`docs/deployment.md`, is reachable at `fitness.home.local`, the same
cross-device/phone path Nate already relies on. If either new tool landed
in `ALL_TOOLS`, a phone-triggered call would get back a container-internal
path with no way to retrieve the file, and the agent would likely report
success anyway. So the two new tools are added to a **new
`LOCAL_ONLY_TOOLS` list** — a plain module-level constant in
`agent/tools.py`, defined immediately after `ALL_TOOLS` (`tools.py:1507`)
as `LOCAL_ONLY_TOOLS = [generate_brief_report, generate_chart]`, exactly
as trivially greppable and cheap to construct as `ALL_TOOLS` itself —
wired only into `run_stdio()` — never merged into `ALL_TOOLS` and never
served by the streamable-HTTP registry. This makes "same-machine chat
only" true by construction: the tools are unavailable over the network
transport, not merely undocumented for it.

**Concrete mechanism.** Today both transports converge on the same
zero-argument path: `web/mcp_server.py`'s `build_server()` (used by both
`run_stdio()` and `build_session_manager()`, the latter backing the
`/mcp/` HTTP mount in `web/server.py`) calls
`agent_tools.make_server()["instance"]`, and `agent/tools.py`'s
`make_server()` is hardcoded to `tools=ALL_TOOLS` — there is no
"register more tools onto an existing server" API in the underlying SDK.
Splitting the tool list into two constants means nothing unless the
*functions that build the server* are also parameterized:
- `agent/tools.py`: `make_server(extra_tools: list | None = None)` builds
  its `tools=` argument as `ALL_TOOLS + (extra_tools or [])` instead of
  the hardcoded `ALL_TOOLS`.
- `web/mcp_server.py`: `build_server(extra_tools: list | None = None)`
  forwards `extra_tools` into `agent_tools.make_server(extra_tools=extra_tools)`.
- `run_stdio()` calls `build_server(extra_tools=LOCAL_ONLY_TOOLS)`.
- `build_session_manager()`'s call site to `build_server()` is left
  **argument-free** — this is the load-bearing line. If a future edit
  ever passes `LOCAL_ONLY_TOOLS` there too (e.g. "for consistency"), the
  HTTP transport silently regains the tools this whole design exists to
  keep off it.

**Tripwires — must not be quietly defeated.** Two existing tests already
exercise this boundary and must keep passing *unmodified in intent*:
`tests/test_mcp_server.py` (has a test asserting the full served tool set
matches `ALL_TOOLS`) and `tests/test_smoke.py`. An implementer must treat
any need to edit either test's assertions as a red flag, not a
formality — if satisfying the new code requires loosening what these
tests check (e.g. changing an equality assertion to permit the extra
tools), that change silently defeats the tripwire the tests exist to be,
and must be flagged for review rather than pushed through quietly.

**New required test.** Add a test that makes an authed `tools/list` call
over the `/mcp/` HTTP transport (valid bearer token) and asserts
`generate_brief_report` and `generate_chart` are **absent** from the
result. This is the only test in the suite that actually proves the
HTTP path can't reach the local-only tools — the grep-based invariant
below is a design-time check, not a regression test.

New module `src/local_fitness/agent/visuals.py`:
- **`matplotlib.use("Agg")` must be called at the top of this module,
  before `pyplot` is imported** — do not rely on matplotlib's
  auto-detected backend. This is the same class of "works on a dev
  machine, breaks headless" gotcha this design already handles carefully
  for WeasyPrint fonts (see Rendering stack), just for matplotlib's
  default backend picking an interactive/GUI toolkit that isn't present
  in the container or CI.
- **Heavy native imports are deferred to function bodies — the tool list
  itself is not.** `web/server.py` imports `mcp_server` at startup, which
  imports `agent/tools.py` — the always-running web server process, which
  by design can never call these local-only tools. `matplotlib` and
  `weasyprint` are heavy native imports; eagerly importing them at module
  scope (either `tools.py`'s or `visuals.py`'s own) would load that cost
  into a process that never uses them, and — more importantly — means a
  future WeasyPrint/matplotlib native-binding break (the same class of
  incident as the past `node:26`/corepack break) would take down the
  always-needed web server, not just the local-only feature it actually
  belongs to. So `import matplotlib` / `matplotlib.use("Agg")` and
  `import weasyprint` are pushed down into the bodies of the two
  `@tool`-decorated functions (`generate_brief_report`, `generate_chart`)
  and into `visuals.py`'s own module body — `visuals.py` is only imported
  from inside those function bodies, never at `tools.py` module scope.
  This deferral does **not** extend to the `LOCAL_ONLY_TOOLS` list itself:
  unlike the heavy libraries, `LOCAL_ONLY_TOOLS = [generate_brief_report,
  generate_chart]` is a list literal referencing two already-defined
  function objects — it costs nothing to construct and is built at
  `tools.py` module scope, symmetric with `ALL_TOOLS` (see above). Only
  the `import matplotlib`/`import weasyprint` statements are deferred;
  the list that names the two functions is not.
- `theme.py`-equivalent constants: the palette above, matplotlib
  `rcParams`, shared CSS snippet.
- `render_chart_png(series_data, chart_type: str, value_fmt: Callable[[float], str]) -> bytes` —
  matplotlib, saved to an in-memory buffer via `savefig`. `value_fmt`
  formats axis/label values into human units exactly as the existing
  ASCII `chart()` tool already does via `_chart_value_fmt(metric)` (e.g.
  `sleep_seconds` divided into hours) — without it, raw unhumanized
  numbers would appear on a "beautiful" chart's axes, failing the
  feature's own acceptance bar. Call sites reuse `_chart_value_fmt(metric)`
  exactly as `chart()` does today, rather than inventing a second
  formatting scheme. `savefig()` is CPU-bound and synchronous; the tool
  call site must offload it (e.g. `await asyncio.to_thread(render_chart_png,
  ...)`) so rendering one or several chart images for a multi-takeaway
  brief doesn't block the event loop the MCP server runs on. Because that
  offload puts the render on a worker thread, and nothing in this design
  confirms the underlying low-level MCP `Server` this stack reuses can
  never have two tool calls in flight within one stdio session,
  `render_chart_png` must build its own `matplotlib.figure.Figure()`
  object (the thread-safe object-oriented API) rather than using
  `pyplot`'s stateful global current-figure functions (`plt.plot`,
  `plt.gcf()`, `plt.savefig()`, etc.) — `pyplot`'s global state is not
  thread-safe, and a race there would silently mix data between two
  charts rather than crash, which is worse for a tool whose entire premise
  is "never recompute, just render exactly the tested numbers."
- `render_brief_pdf(brief: Brief, charts: dict[str, bytes]) -> bytes` —
  builds an HTML string (embedding the **pre-rendered** chart PNGs passed
  in `charts`, as base64 `data:` URIs), calls
  `weasyprint.HTML(string=html).write_pdf()`. It takes rendered bytes as a
  parameter rather than fetching or rendering anything itself, so it stays
  genuinely DB-free and testable with plain string-membership assertions
  (see Testing strategy) — it does not fetch chart data on its own. Like
  `render_chart_png`'s `savefig()`, WeasyPrint's `write_pdf()` performs
  synchronous, CPU-bound HTML/CSS layout, font shaping, and multi-image
  PDF assembly — at least as expensive as one `savefig()` call, often more
  for a multi-chart brief — so the tool call site must offload it the same
  way: `await asyncio.to_thread(render_brief_pdf, brief, charts)`, keeping
  the low-level MCP `Server.run()` loop free to process stdio I/O and
  cancellation while the render is in flight.
  **Keying:** `Takeaway` (in `agent/schemas.py`) has no `id` field, and two
  takeaways can legitimately cite the same `metric` — so `charts` is
  **not** keyed by metric name (two same-metric takeaways would silently
  collide) and there is no takeaway id to key by. `charts` is keyed by
  `str(index)` from `enumerate(brief.takeaways)`, matching the
  `takeaway_index: int` concept `agent/grounding.py` already uses for the
  same "which takeaway, positionally" purpose.

**Serialize renders instead of auditing WeasyPrint's thread-safety.**
`render_chart_png` is mandated to use the thread-safe `Figure()` API
specifically because `pyplot`'s global-current-figure state is not
thread-safe and nothing confirms the underlying MCP `Server` can never
have two tool calls in flight within one stdio session — but
`render_brief_pdf`'s `write_pdf()` is offloaded through the identical
`asyncio.to_thread` mechanism under the exact same uncertainty, with no
confirmed WeasyPrint threading bug to point to either way. Rather than
spend effort verifying WeasyPrint's internal thread-safety (undocumented,
implementation-dependent, and would need to be re-verified on every
WeasyPrint upgrade), close the question by construction: a single
module-level `asyncio.Lock()` in `visuals.py`, acquired by both
`generate_brief_report` and `generate_chart` at their tool-call sites
before entering `asyncio.to_thread` and released once the render
completes. This is trivial to pay for a low-traffic, single-user tool,
and it removes the open question entirely — chart and PDF renders never
run concurrently within one process regardless of the MCP `Server`'s
actual concurrency model, so there is nothing left to verify about
WeasyPrint's (or matplotlib's) internals.

**Prerequisite refactor (required before either tool is built):** `chart()`
in `agent/tools.py` currently builds and executes its whitelisted SQL
inline — there is no separable fetch function today, so "reuses the same
data-fetching functions" below is not yet true of the codebase. Extract
`chart()`'s fetch logic into a shared helper
`_fetch_metric_series(metric: str, days: int) -> tuple[list[str], list[float]]`
(dates, values) that **both** `chart()` and `generate_chart()` call.
`_fetch_metric_series` itself validates `metric` against `_CHART_METRICS`
before building any SQL — the check lives inside the helper, not left to
each caller to remember; both `chart()` and `generate_chart()` inherit
this from the helper. Add a regression test asserting `chart()`'s existing
ASCII output is unchanged after the extraction, before layering
`generate_chart` on top of it.

Two new MCP tools in `agent/tools.py`, added to `LOCAL_ONLY_TOOLS`:

### `generate_brief_report(date: str) -> {"path": str}`

`date` is model-suppliable and is interpolated into both a read path
(`briefings/{date}.json`) and a write path (`reports/brief-{date}.pdf`), so
it is validated against `^\d{4}-\d{2}-\d{2}$` before any file I/O (see
Invariants).

Flow: validate `date` format → read the brief from
`agent.briefs.DEFAULT_BRIEFINGS_DIR / f"{date}.json"` — the same
module-level constant `save_brief()`, `load_today()`, and `load_latest()`
already resolve through (honors the `LOCAL_FITNESS_BRIEFINGS_DIR` env
override and is computed cwd-independently from `_PROJECT_ROOT`), not a
fresh relative-path literal, since a bare `briefings/{date}.json` would
silently break — landing on the "brief missing" error path — if a stdio
MCP client is launched from a different working directory or if the env
override is set → validate against the existing `Brief` schema (reuse
existing validation) → for each
`(index, takeaway)` in `enumerate(brief.takeaways)` that has an
associated metric, fetch its series via `_fetch_metric_series(takeaway.metric.metric,
days=takeaway.metric.days)` — using `takeaway.metric.days` (the
`TakeawayMetric.days: int = Field(14, ge=7, le=730)` field in
`agent/schemas.py`, the same value `TakeawayCard.tsx`'s
`useMetricSeries(takeaway.metric?.metric, takeaway.metric?.days)` already
consumes to render this takeaway's chart in the web UI) as the window, so
the PDF's chart matches exactly what the web UI shows for the identical
brief — and render it with `render_chart_png(series, chart_type="line",
value_fmt=_chart_value_fmt(metric))` (brief-embedded charts default to
`"line"`; `generate_chart`'s own `chart_type` parameter is what varies the
form for the standalone-chart tool below) → call `await
asyncio.to_thread(render_brief_pdf, brief, charts)` with
the resulting `{str(index): png_bytes}` dict, keyed positionally (see
`render_brief_pdf`'s Keying note above) — a takeaway with no chartable
data (no associated metric, or `_fetch_metric_series` returns no rows in
the window) simply has no entry for its index and `render_brief_pdf`
renders that takeaway without a chart image; it does not fail the whole
report. Both `_fetch_metric_series` and `render_chart_png` are called
inside the same per-takeaway try/except — `_fetch_metric_series` raising
(e.g. a transient `sqlite3.OperationalError` from a concurrent write, since
`sync_garmin_data` is a reachable MCP tool that can write to the same DB
mid-conversation) is handled identically to `render_chart_png` raising:
caught, logged, that takeaway's chart is omitted, and the loop continues —
the fetch call is never left outside the try/except. The same
graceful-degradation applies if `render_chart_png` itself
raises for a takeaway whose data is technically present but degenerate
(e.g. a series with exactly one data point, or an all-identical-value
series a given chart type divides by zero on): the exception is caught
and logged, that takeaway's entry is simply omitted from `charts`, and the
loop continues — a render exception on one takeaway, from either the fetch
or the render step, must never fail the
whole `generate_brief_report` call, the same guarantee as the no-data case
→ resolve the target path and confirm it is contained under
`LOCAL_FITNESS_REPORTS_DIR` via `.resolve().relative_to()` (same
containment pattern as `web/server.py`'s SPA-fallback route) → write to a
`.tmp` sibling whose name is suffixed with a per-call unique token
(`uuid4().hex[:8]`, e.g. `brief-{date}.pdf.{token}.tmp`) and
`os.replace()` into `reports/brief-{date}.pdf` — the same atomic-write
shape `agent/briefs.py`'s `save_brief()` already uses, but with a unique
temp filename rather than `save_brief()`'s fixed one (see Invariants), so
two concurrent `uv run fitness mcp-stdio` sessions racing this same call
never share a temp inode before either reaches `os.replace()` → return
the path as text.

Errors (`_err`, distinct messages so the calling agent can explain rather
than surface a stack trace):
- Malformed `date` (fails the format check).
- Brief file missing for that date.
- Schema validation failure.
- WeasyPrint render failure.

### `generate_chart(metric: str, days: int, chart_type: str) -> {"path": str}`

Calls the shared `_fetch_metric_series` helper (extracted above) — no new
SQL/query path. This is the tool's core invariant: the renderer never
recomputes aggregates, only formats what an already-tested function
returned (mirrors the budget project's "every number must be extracted
verbatim, never recomputed" rule).

`chart_type` is a **deliberate subset** of the ASCII tool's `style`
param (`calendar`, `line`, `bar`, `combo`, `spark`), not a full mirror.
**v1 scopes `chart_type` to `line`, `bar`, `combo` only.** Rationale:
this tool's whole premise is producing a "beautiful," shareable
standalone PNG, and `line`/`bar` clearly earn that bar — they're
standard chart forms with axes, gridlines, and labels regardless of
medium — while `combo` earns its place per the paragraph below.
`calendar` (a week-stacked heat-grid) and `spark` (a
single sparkline with no axis/gridlines/labels) are ASCII/terminal-native
concepts built for a monospace, low-fidelity surface — neither obviously
earns standalone-artifact status as a glossy PNG with nothing else on
the page, and both are dropped from v1's scope. They can be reconsidered
later if requested, but this design does not claim they translate.
`agent/charts.py`'s `render_combo_chart` renders bars plus a
least-squares trend-line overlay of the **same single metric** — no
second series, no second axis. `generate_chart`'s `combo` is the
matplotlib-rendered version of that identical shape (bars + smoothed/
regression trend line, one metric, one axis), not a two-metric
dual-axis chart — the tool's signature (`metric: str`, singular) and
`_fetch_metric_series`'s single-series return type make a genuine
second-metric/second-axis chart unimplementable as scoped, so this
design does not claim one. Any true dual-axis (two different metrics,
two scales) chart is out of scope for v1 and would require both a
signature change and a second data-fetch path — not something this
`combo` value provides.

Flow: fetch series via `_fetch_metric_series` → `render_chart_png(series,
chart_type, value_fmt=_chart_value_fmt(metric))` → resolve
`reports/chart-{metric}-{chart_type}-{days}d-{date}.png` — `chart_type` is
part of the filename, not just `(metric, days, date)`, because requesting
a line chart and then a bar chart of the same metric/window on the same
day is an entirely ordinary follow-up ("actually make that a bar chart")
and omitting `chart_type` would silently overwrite the first file at an
identical path with different content. **`{date}` here is
`date.today().isoformat()` at render time, not a tool argument** —
`generate_chart`'s signature (`metric`, `days`, `chart_type`, per API
Surface) has no `date` parameter; it exists in the filename only to
distinguish chart runs across days, and is computed inside the tool, not
supplied by the caller — and
confirm containment under `LOCAL_FITNESS_REPORTS_DIR` (same check as
above) → write to a `.tmp` sibling whose name is suffixed with a per-call
unique token (same pattern as `generate_brief_report` above —
`uuid4().hex[:8]`) and `os.replace()` into the final path
— the same atomic-write shape as `generate_brief_report` above and
`agent/briefs.py`'s `save_brief()`, but with the unique-temp-name
refinement (see Invariants). This filename is fully determined by
`(metric, chart_type, days)` plus the current calendar date, so a
retried/duplicate tool call with identical arguments **on the same day**
writes to the same *final* path and must never leave a partially-written
file behind — the unique temp suffix means two concurrent calls with
identical arguments never race the same temp inode, even though both
converge on the same final path; a retry that crosses midnight produces a
second file, since `{date}` advances independently of the three real
arguments) → return the path as text.

Errors (`_err`, mirroring `generate_brief_report`'s structure):
- Invalid `metric` (not a whitelisted/known metric column).
- Invalid `days` range (e.g. non-positive, or exceeds whatever cap
  `_fetch_metric_series`/`chart()` already enforces).
- Invalid `chart_type` (not one of the v1-supported forms: `line`,
  `bar`, `combo` — see above).
- No data in the requested window (`_fetch_metric_series` returns empty).
- matplotlib render failure — parallel to `generate_brief_report`'s
  "WeasyPrint render failure" case above; this tool has no other takeaway
  to fall back on, so unlike the per-takeaway degradation inside
  `generate_brief_report`, a render exception here is a hard error, not a
  silently-omitted chart.

## File & env conventions

Follows the exact `briefings/` pattern (`agent/briefs.py`):
- `LOCAL_FITNESS_REPORTS_DIR` env var, default `<project_root>/reports`.
- Gitignored, same as `briefings/`. **This requires an explicit new
  `.gitignore` line** — the current `.gitignore` lists `data/`,
  `briefings/`, `logs/` individually with no wildcard that would already
  cover a new `reports/` directory, so this must not be assumed to fall
  out "for free." A generated PDF/PNG embeds real takeaway text and
  sleep/HR/pace values, so an un-gitignored `reports/` is a real path to
  committing personal fitness data to this public repo — CLAUDE.md's
  top hazard. See Docs to update.
- **Decision: `reports/` is ephemeral by design, not bind-mounted, and not
  pre-created in the Dockerfile.** Unlike `briefings/`, `reports/` gets no
  Dockerfile pre-creation/chown step at all: the container's only running
  process (`fitness serve`, see Docs to update) can never reach
  `LOCAL_ONLY_TOOLS` (see Architecture), so it never writes to `reports/`
  and has nothing to pre-create a directory for. The `mkdir(parents=True,
  exist_ok=True)` call each tool makes before its `.tmp` write (see
  Invariants) is what creates `reports/` on demand — and it only ever
  needs to, on the one path that actually calls these tools, the host
  CLI's `run_stdio()`. Also deliberately *not* wired to a host bind-mount
  in compose. Rationale: unlike `briefings/` (the day's only copy of a
  generated brief — a system of record), a PDF or PNG in `reports/` is
  trivially regenerable on demand by re-calling
  `generate_brief_report`/`generate_chart`. Since this repo's own
  workflow rebuilds the container after nearly every change (`docker
  compose up -d --build local-fitness`), an ephemeral `reports/` is the
  correct default — losing generated files on rebuild is expected and
  acceptable, not a hedge to revisit later.

## Delivery (v1 scope)

File path returned as text only — no new web download route. This is safe
because the tools live only in `LOCAL_ONLY_TOOLS` (see Architecture),
wired only into `run_stdio()` — the only client that can reach them at all
is a same-machine `stdio` MCP client (Claude Code/Claude Desktop), where a
local path is directly openable. The streamable-HTTP transport (the
phone/web-UI path at `fitness.home.local`) never has these tools in its
registry, so there is no route by which a phone-triggered call could
receive a container-internal path it has no way to retrieve. Explicitly
**not** solving for phone/web-UI access to generated files in v1 — and
structurally prevented from mis-firing there, not just left undocumented.

## Testing strategy

- **Pure logic, fully tested:** `render_brief_pdf(brief, charts)` takes
  pre-rendered chart PNG bytes as a parameter and does no fetching or
  rendering of its own, so it's testable as pure HTML-assembly logic
  (e.g., "given a `Brief` and a stub `charts` dict, the generated HTML
  contains the exact takeaway text and exact metric values verbatim" —
  string-membership assertions, never recomputation checks, and no DB
  access needed to exercise it). **Caveat for `Takeaway.details`:** per
  Rendering stack, `details` is markdown and is passed through a
  markdown→HTML library before embedding, so its raw source is
  transformed, not passed through unmodified. For `details`, assert the
  markdown-rendered HTML fragment (e.g., `<strong>...</strong>` for a
  bolded sentinel phrase in a test fixture), not the raw markdown source
  string — the raw source is expected NOT to appear verbatim in the
  output. The "exact verbatim" string-membership assertion above applies
  to `headline`/`summary`/metric display values for ordinary text, but
  per the Defense-in-depth note above these fields are passed through
  `html.escape()` before insertion, so a fixture containing HTML-special
  characters is the sharper test: a sentinel `headline` containing
  `<`/`>`/`&` must appear in the rendered HTML as `&lt;`/`&gt;`/`&amp;`,
  never as the raw character — this is the test that would actually fail
  if the `html.escape()` step were dropped, since a "verbatim" assertion
  on plain alphanumeric text passes identically whether or not escaping
  happened. The one genuinely new data-shaping piece
  is the `_fetch_metric_series` extraction (see Architecture) — it needs
  its own regression test proving `chart()`'s existing behavior is
  unchanged, in addition to `generate_chart`'s tests.
- **Markdown table fixture test:** tables are a recurring, real shape
  for `details` — `agent/briefs.py`'s `save_brief()` already calls
  `fix_table_row_breaks(tk["details"])` specifically to repair collapsed
  markdown tables, and the frontend renders `details` with
  `remarkGfm` (GFM table support). Build a fixture `Brief` whose
  `details` field contains a markdown table (header row, separator row,
  at least one data row) and render it via `render_brief_pdf`. Assert
  the rendered HTML contains a real `<table>`/`<td>` structure (e.g.
  via a lightweight HTML parse, or a string-membership check for
  `<table` and `<td`), not the raw `|`-delimited pipe characters — this
  is the test that would fail if the chosen markdown library's table
  extension/plugin (`extensions=['tables']` for `markdown`,
  `plugins=['table']` for `mistune`) were left disabled, since a table
  rendered without it degrades silently to literal pipe-and-dash text
  rather than raising an error.
- **Chart-keying fixture test:** `charts` is keyed positionally by
  `str(index)` from `enumerate(brief.takeaways)` specifically to avoid
  metric-name collisions (see `render_brief_pdf`'s Keying note in
  Architecture) — a non-obvious, easy-to-get-wrong scheme that nothing
  in the existing smoke/url_fetcher tests actually exercises (the
  url_fetcher test proves a malicious image is *excluded*, never that a
  legitimate one is *correctly included* at the right position). Build
  a fixture `Brief` with 2+ takeaways and a `charts` dict giving each
  takeaway index a distinct, identifiable placeholder PNG (e.g. solid
  fills of different colors, or PNGs with distinct pixel dimensions).
  Render via `render_brief_pdf` and assert each takeaway's section in
  the output embeds **its own** chart's `data:` URI/bytes and not
  another takeaway's — an off-by-one or stale-key lookup would silently
  produce zero, duplicate, or swapped charts, and nothing else in this
  suite would catch it. **A dense, sequential fixture alone (charts at
  `"0"`, `"1"`, `"2"`, ... for every takeaway, no gaps) is not
  sufficient:** a naive-but-plausible buggy implementation (e.g.
  `zip(brief.takeaways, charts.values())`, relying on dict insertion
  order rather than true index-keyed lookup) produces byte-identical
  output to a correct implementation whenever `charts` is dense and
  contiguous from 0 — exactly the shape a gapless fixture has. The
  fixture must therefore also include at least one chartless takeaway
  *between* two charted ones — e.g. three takeaways at indices 0, 1, 2
  with `charts = {"0": png_a, "2": png_b}` (index 1 has no entry) — and
  assert index 0 renders `png_a`, index 2 renders `png_b`, and index 1
  renders with no chart image at all. This is the case that actually
  falsifies "positional zip" vs. "true index-keyed" lookup, and it is
  precisely the shape the Flow section already says is normal (a
  takeaway with no chartable data simply has no entry for its index).
- **`url_fetcher` restriction test:** this mitigation defends against a
  materially more dangerous vector than `html.escape()` above — an
  embedded `<img src>`/`<link>`/`@import` that either reaches the
  network (SSRF-adjacent) or passes through unsanitized markdown-derived
  HTML — so it gets the same fixture-based rigor, not just a mention in
  Rendering stack. Build a fixture `Brief` whose `details` field
  contains a bolded sentinel phrase (for the markdown-rendering
  assertion above) **plus** a raw `<img src="http://example.com/x.png">`
  tag, and render it via `render_brief_pdf`. Assert the resulting PDF
  contains no reference to `example.com` and that no outbound HTTP
  request was attempted (e.g. mock/spy the fetcher, or run the test with
  network access disabled and assert the render still succeeds rather
  than raising/hanging on a blocked connection attempt) — proving the
  `url_fetcher` passed to `weasyprint.HTML()` actually rejects
  non-`data:` URIs at render time, not merely that the code exists. This
  is the test that would catch a future WeasyPrint version bump or
  refactor that silently drops or bypasses the `url_fetcher` argument.
- **Rendering smoke tests:** `%PDF-` magic bytes + non-zero page count for
  `render_brief_pdf`; valid PNG magic bytes + expected pixel dimensions for
  `render_chart_png`; both fail cleanly (not a crash) on empty/missing-metric
  input.
- **Required: `LOCAL_ONLY_TOOLS` HTTP-exclusion test.** This is not
  optional polish — it is the only automated proof that the
  `run_stdio()`-vs-`build_session_manager()` split (see Architecture)
  actually holds. Add a test that makes an authed `tools/list` call over
  the `/mcp/` HTTP transport with a valid bearer token and asserts the
  result does **not** contain `generate_brief_report` or `generate_chart`.
  Without this test, nothing in CI would catch a future PR that
  accidentally passes `extra_tools=LOCAL_ONLY_TOOLS` into
  `build_session_manager()`'s `build_server()` call — the existing "grep
  confirms they're absent from `ALL_TOOLS`" check (see Invariants) is a
  point-in-time design check, not a regression test that runs on every
  PR. Before landing, confirm `tests/test_mcp_server.py`'s existing
  "served tool set matches `ALL_TOOLS`" assertion and `tests/test_smoke.py`
  still pass **without modification** — if either needs to change to
  accommodate the new tools, that's a sign the split isn't actually
  structural (see Architecture's Tripwires note).
- **Required companion: `LOCAL_ONLY_TOOLS` stdio-inclusion test.** The
  HTTP-exclusion test above proves the tools are unreachable over
  `/mcp/`; nothing in the existing suite proves the opposite — that they
  *are* reachable via `run_stdio()`'s actual code path. Every existing
  test in `tests/test_mcp_server.py` calls `mcp_server.build_server()`
  with no arguments (defaulting to `ALL_TOOLS` only); none exercise the
  `extra_tools=` parameter `run_stdio()` depends on. Add a test that
  calls `mcp_server.build_server(extra_tools=agent_tools.LOCAL_ONLY_TOOLS)`
  — mirroring `run_stdio()`'s actual call — and asserts
  `generate_brief_report` and `generate_chart` **are** present in the
  served tool names. This is the positive counterpart to the
  HTTP-exclusion test: without it, a future signature rename or an
  accidental regression to an argument-free `build_server()` call inside
  `run_stdio()` would silently break the feature's only sanctioned code
  path with nothing in CI catching it.
- **Explicitly excluded from the coverage gate:** "does it look good" — a
  screenshot/manual-review obligation per CLAUDE.md's UI-verification rule,
  not a unit test.
- The new `tools/list`-over-`/mcp/` test above is the one new
  auth-relevant case this feature needs; add it to `tests/test_mcp_server.py`
  (it exercises the same served-tool-set boundary that file already
  covers) or `tests/test_security.py` if that fits the suite's existing
  organization better — either is acceptable, but it must exist. No other
  new `tests/test_security.py` cases are needed beyond this — v1 adds no
  new `/api/*` route.

## Invariants

**Checkable by inspection:**
- Neither tool performs its own aggregation/rollup — all numeric values are
  pass-through from already-tested data functions.
- Both tools write only under `LOCAL_FITNESS_REPORTS_DIR` — no other write
  path.
- Palette hex values match the `budget` theme exactly, defined once in
  `visuals.py`, not duplicated per chart type.
- The two tools are registered in `LOCAL_ONLY_TOOLS`, never in `ALL_TOOLS`
  — grep confirms they are absent from whatever list backs the
  streamable-HTTP `/mcp/` registry in `web/server.py`, and `build_server()`
  in `web/mcp_server.py` is only ever called with
  `extra_tools=LOCAL_ONLY_TOOLS` from `run_stdio()` — never from
  `build_session_manager()` (see Architecture's Concrete mechanism note).
- `date` (in `generate_brief_report`) is validated against
  `^\d{4}-\d{2}-\d{2}$` **before** it is used to build either the
  `briefings/{date}.json` read path or the `reports/brief-{date}.pdf`
  write path — the same non-negotiable pattern CLAUDE.md requires for any
  model-suppliable path segment.
- `reports/` is gitignored (new `.gitignore` line — see File & env
  conventions and Docs to update).
- Both tools write via a `.tmp` sibling + `os.replace()`, never a direct
  write to the final path — the same atomic-write shape
  `agent/briefs.py`'s `save_brief()` pattern uses, but with one
  deliberate refinement: the temp filename is suffixed with a per-call
  unique token, `uuid4().hex[:8]`, not `save_brief()`'s fixed `.tmp`
  name. `os.replace()` alone only guarantees no reader ever sees a torn
  file from a *single* writer; it does nothing to stop two independent
  OS processes (e.g. two separate `uv run fitness mcp-stdio` sessions —
  Nate routinely runs multiple concurrent terminal/chat sessions) from
  racing to write the *same* fixed temp path before either calls
  `replace()`. A unique-per-call temp name means concurrent writers
  never share an inode, so "a retried/duplicate call must never leave a
  partially-written file behind" actually holds for concurrent-process
  calls, not just concurrent writes to the same file descriptor.
  `os.getpid()` is deliberately **not** used as an alternative token: the
  Architecture section's own reasoning about "two tool calls in flight
  within one stdio session" (same process, same PID, dispatched via
  `asyncio.to_thread`) — the exact justification for mandating the
  thread-safe `Figure()` API over `pyplot`'s global state — applies
  equally here. Two same-process concurrent calls with identical
  arguments would produce identical `os.getpid()`-suffixed temp names,
  recreating the same "two writers share an inode" race at
  intra-process granularity. `uuid4().hex[:8]` alone covers both the
  cross-process and intra-process case, so it is the only token used,
  everywhere this pattern appears.
- Both tools call `LOCAL_FITNESS_REPORTS_DIR.mkdir(parents=True,
  exist_ok=True)` before the `.tmp` write, exactly as `save_brief()` does
  — required for the host CLI path (`run_stdio()` via `uv run fitness
  ...`), which has no Dockerfile pre-creation step; without it, a fresh
  clone's first tool call hits an unhandled `FileNotFoundError` instead of
  a clean `_err`.
- `generate_chart`'s filename (`reports/chart-{metric}-{chart_type}-{days}d-{date}.png`)
  is fully determined by `(metric, chart_type, days)` plus `{date}` —
  where `{date}` is `date.today().isoformat()` at render time, not a tool
  argument (`generate_chart`'s parameters are only `metric`, `days`,
  `chart_type`; see API Surface). Consequently the idempotent-overwrite
  guarantee (identical arguments → identical file) holds only within a
  single calendar day; a retry that crosses midnight produces a second
  file rather than overwriting the first.
- `LOCAL_ONLY_TOOLS = [generate_brief_report, generate_chart]` is a plain
  module-level constant in `agent/tools.py`, symmetric with and as
  trivially greppable as `ALL_TOOLS` — but the `import matplotlib`/
  `import weasyprint` statements are deferred into the two tools'
  function bodies (and `visuals.py`'s own module body), never executed
  at `tools.py` module scope — the always-running web server process
  never pays matplotlib/WeasyPrint's import cost and never inherits a
  native-binding break from either library.
- Both `generate_brief_report` and `generate_chart` acquire the same
  single module-level `asyncio.Lock()` in `visuals.py` before entering
  their `asyncio.to_thread` render call, and release it once the render
  completes — chart and PDF renders never execute concurrently within
  one process (see Architecture's Serialize renders note), closing the
  "two tool calls in flight within one stdio session" question for
  WeasyPrint the same way the `Figure()` API closes it for matplotlib.

**Testable:**
- `generate_brief_report` on a missing date returns an error, never a crash
  or a blank PDF.
- `generate_brief_report` on a malformed `date` (fails the regex, e.g.
  containing `/` or `..`) returns an error before any file I/O is
  attempted — never a path-traversal read/write.
- `generate_chart` called with an unwhitelisted `metric` returns an error
  before any SQL is executed.
- The resolved output path for both tools, checked via
  `.resolve().relative_to(LOCAL_FITNESS_REPORTS_DIR.resolve())`, raises/is
  rejected for any input that would otherwise escape `reports/` (same
  containment pattern as `web/server.py`'s SPA-fallback route).
- Generated PDF text (via extraction, e.g. `pdfplumber`) contains the exact
  `headline`/`summary`/metric display values from the source `Brief`
  verbatim; for `details` (markdown-rendered), it contains the
  markdown-rendered form of a sentinel fixture phrase (e.g. bolded text
  extracted/rendered as such), not the raw markdown source (see Testing
  strategy).
- A sentinel `headline` (or `summary`) containing `<`, `>`, and `&`
  renders in the output HTML as `&lt;`, `&gt;`, and `&amp;` respectively
  — never as the raw character — proving the `html.escape()` step (see
  Rendering stack's Defense-in-depth note) actually runs, not just that
  the source substring is present somewhere in the document.
- A fixture `Brief` whose `details` field embeds a raw
  `<img src="http://example.com/x.png">` (or a `<link rel=stylesheet>`/
  `@import` pulling an external stylesheet) renders to PDF with that
  external resource neither fetched nor present — no network call is
  made and no image/style from the blocked URL appears in the output —
  proving the `url_fetcher` restriction to the `data:` scheme (see
  Rendering stack's Defense-in-depth note) actually runs and is not
  silently bypassed by a future WeasyPrint version bump or refactor.
  This is the sharper test for this mitigation: asserting the PDF still
  renders successfully is not enough, since a fetcher that quietly
  allows the request would also render successfully — the test must
  confirm the external resource is absent/blocked, mirroring the
  fixture-based rigor of the `html.escape()` case above.
- Generated chart PNG is non-empty, valid, and dimensioned as configured.
- An authed `tools/list` call over the `/mcp/` HTTP transport does not
  include `generate_brief_report` or `generate_chart` (see Testing
  strategy's Required test).
- `mcp_server.build_server(extra_tools=agent_tools.LOCAL_ONLY_TOOLS)` —
  the actual call `run_stdio()` makes — serves both `generate_brief_report`
  and `generate_chart` in its tool names (see Testing strategy's Required
  companion test).
- A takeaway with no chartable data (no metric, or an empty
  `_fetch_metric_series` result) still produces a complete PDF — that
  takeaway renders without a chart image rather than failing the whole
  `generate_brief_report` call.
- A takeaway whose `render_chart_png` call raises on degenerate-but-real
  data (e.g. a single-point series, or an all-identical-value series a
  chart type divides by zero on) is treated the same way: the exception
  is caught and logged, that takeaway's chart image is omitted, and
  `generate_brief_report` still returns a complete PDF rather than
  failing outright.
- A `details` field containing a markdown table renders to a real
  `<table>`/`<td>` HTML structure, never raw `|`-delimited pipe
  characters (see Testing strategy's markdown table fixture test).
- In a `Brief` with 2+ takeaways each given a distinct chart PNG in
  `charts` at known indices, each takeaway's rendered PDF section
  contains its own chart image, never another takeaway's — including the
  case of a chartless takeaway positioned between two charted ones (see
  Testing strategy's chart-keying fixture test).

## Docs to update in the same PR

- `pyproject.toml`: add `matplotlib`, `weasyprint`, a pure-Python
  markdown→HTML library (`markdown` or `mistune`) (runtime); a PDF-text-
  extraction lib (`pdfplumber`) as dev-only for tests.
- **`Dockerfile`: deliberately left untouched for WeasyPrint — not an
  oversight.** No Pango/harfbuzz/fonts/fontconfig apt packages and no
  pre-created/chowned `reports/` dir are added here. Verified against
  the actual repo: the container's `CMD` (`Dockerfile:101`) is `["uv",
  "run", "fitness", "serve"]`, which only ever runs
  `build_session_manager()`'s path — structurally unable to reach
  `LOCAL_ONLY_TOOLS` (see Architecture). That process therefore never
  imports `visuals.py` (deferred to the two tools' function bodies, which
  the container never calls), never invokes matplotlib/WeasyPrint, and
  never writes to `reports/`. This is the same reasoning the design
  already applies to defer `import matplotlib`/`import weasyprint` out of
  `tools.py` module scope — don't load a cost into a process that never
  uses it — applied one layer down to the Dockerfile itself. The CI
  `docker-build` job gains nothing from these packages either, since it
  only builds the image (`push: false`) and never runs code inside it.
  Do not "complete the pattern" by adding these to the Dockerfile later
  without first re-confirming `LOCAL_ONLY_TOOLS` is still unreachable
  from the container.
- `.github/workflows/ci.yml`: add the **same** apt-get install step (Pango/
  harfbuzz + fonts + fontconfig) to the `validate` job — that job runs
  `pytest` directly on bare `ubuntu-latest`, not inside the Docker image,
  so any test importing `weasyprint` breaks CI on every future PR touching
  this code unless the runner has these packages too. This must be
  confirmed working in a draft PR (green `validate` run) before treating
  the feature as mergeable.
- `.env.example`: `LOCAL_FITNESS_REPORTS_DIR` with a commented-out example.
- **README/setup docs — macOS host:** `brew install pango` required for
  WeasyPrint before first use of `generate_brief_report` via `uv run
  fitness mcp-stdio`. This is a pre-implementation verification step to
  run once on the actual host machine (see Rendering stack), not just a
  documentation afterthought — the CI-only apt packages above (there are
  no Dockerfile apt packages — see above) do not cover the host
  `run_stdio()` path, which is the only real v1 delivery surface for this
  tool (see Delivery).
- **`.gitignore`: add `reports/`.** The current file lists `data/`,
  `briefings/`, `logs/` individually — no existing wildcard covers a new
  `reports/` directory, so this is a required, explicit line item, not a
  side effect of anything else in this PR. Generated PDFs/PNGs contain
  real takeaway text and sleep/HR/pace values; an un-gitignored
  `reports/` is a direct path to committing personal fitness data to this
  public repo.
- **CLAUDE.md correction (unrelated pre-existing bug, caught during
  investigation):** the claim "CI does NOT run `docker build`" is stale — a
  `docker-build` job exists (`.github/workflows/ci.yml:83`, added after the
  corepack incident). Fix this in the same PR.
- CLAUDE.md "What's already wired": add the two new tools once shipped.
- `devlog/` entry.
- Bump `pyproject.toml` version + add a `CHANGELOG.md` entry (this is a
  functional change, not docs-only, per this repo's release policy).

## Out of scope for v1

- Web download route (`/api/reports/*`) for phone/browser access.
- Auto-generation as part of the daily 6:30am job.
- Custom embedded fonts/branding beyond system fonts.
- Dark-mode PDF theme (matches budget's PDF, which also skips it — a static
  file has no viewer-side theme toggle).
- `calendar` and `spark` `chart_type` forms — ASCII/terminal-native
  concepts (week-stacked heat-grid, axis-less one-liner) that don't
  obviously earn standalone-PNG-artifact status; can be reconsidered
  later if requested (see `generate_chart`'s Flow section).

## API Surface

```
generate_brief_report(date: str) -> {"path": str} | error
generate_chart(metric: str, days: int, chart_type: "line" | "bar" | "combo") -> {"path": str} | error
```

`chart_type`'s v1 enum is `line` | `bar` | `combo` only — `calendar` and
`spark` (present in the ASCII `chart()` tool's `style` param) are
explicitly out of scope for v1 (see `generate_chart`'s Flow section
above for rationale).

## Acceptance Criteria

- Calling `generate_brief_report` with a date that has a saved brief
  produces a PDF at `reports/brief-{date}.pdf` containing the brief's exact
  takeaway text and metric values.
- Calling it with a date that has no saved brief returns a clear error, not
  a crash.
- Calling `generate_chart` with a valid metric/days/chart_type produces a
  correctly dimensioned, non-empty PNG at the expected path.
- Both tools' outputs are visually reviewed via screenshot against the
  budget report's bar before being called "beautiful" and done — this
  review is done **on the actual host machine** (`brew install pango`,
  then `uv run fitness mcp-stdio`), the only real v1 delivery path. A
  container-based review is not attempted: the `Dockerfile` deliberately
  carries none of WeasyPrint's native packages (see Docs to update), so
  the built container image cannot render a WeasyPrint PDF at all —
  missing-font (blank/tofu-text) regressions are instead caught by the CI
  `validate` job's test suite running on bare `ubuntu-latest` with the
  Pango/harfbuzz/fonts packages installed there.
- `docker compose up -d --build local-fitness` succeeds with the new
  Python dependencies installed (`matplotlib`, `weasyprint`, the markdown
  library) — this exercises `pyproject.toml`'s `uv sync`, not WeasyPrint's
  native packages, which the Dockerfile deliberately omits (see Docs to
  update).
- A draft PR shows a green CI `validate` job with the new WeasyPrint apt
  packages installed in that job — confirming `pytest` importing
  `weasyprint` doesn't break CI on bare `ubuntu-latest`. These packages
  are deliberately CI-only; the `Dockerfile` does not carry them (see
  Docs to update).
