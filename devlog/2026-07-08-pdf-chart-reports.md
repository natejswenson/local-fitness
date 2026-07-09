# Beautiful PDF brief reports + on-demand trend charts

**2026-07-08 · v0.17.0**

Nate wanted a PDF report for daily briefs comparable to the sibling `budget`
project's monthly reports, plus a way to render a beautiful standalone chart
for any ad-hoc trend question — both on demand, not baked into the always-on
web server.

## Design decisions (from `/design` + a 12-round quality gate)

- **Two separate MCP tools**, not one tool with a mode switch —
  `generate_brief_report(date)` and `generate_chart(metric, days,
  chart_type)`. Narrower, self-documenting schemas match the existing
  one-tool-one-purpose convention (see `chart`).
- **Local file path only for v1** — no web download route. The tools return
  `{"path": ...}`; there's no way to fetch the file over HTTP, which is what
  makes the next decision safe.
- **Structurally, not just documentally, stdio-only.** `agent/tools.py`'s new
  `LOCAL_ONLY_TOOLS = [generate_brief_report, generate_chart]` is never
  merged into `ALL_TOOLS`. `web/mcp_server.py`'s `build_server()` takes an
  `extra_tools` param; `build_session_manager()` (the authenticated
  streamable-HTTP `/mcp/` transport) calls it argument-free, while
  `run_stdio()` alone passes `extra_tools=agent_tools.LOCAL_ONLY_TOOLS`. A
  phone-triggered call over the network transport would get back a
  container-internal path with no way to retrieve the file — the boundary
  closes that off at the registry level, not by convention.
- **Same color theme as the budget project's monthly PDFs** — reused
  verbatim (`PRIMARY #2a78d6`, `GOOD #0ca30c`, `WARNING #fab219`, `CRITICAL
  #d03b3b`, `NEUTRAL #e1e0d9`), not the ASCII chart tool's heat-ramp. Brand
  consistency across the two "glossy" reporting surfaces took priority.

## Rendering stack

- **matplotlib** for `generate_chart` and the brief PDF's inline charts —
  the thread-safe `Figure()`/`FigureCanvasAgg` object-oriented API, never
  `pyplot`'s global state (nothing guarantees the stdio MCP server can't have
  two tool calls in flight).
- **WeasyPrint** for `generate_brief_report`'s HTML→PDF — real CSS (flexbox,
  color, borders) beats ReportLab's core-font-only look and Playwright's
  400-700MB image cost.
- Both renders serialize behind one `asyncio.Lock()` in `agent/visuals.py`
  rather than auditing either library's internal thread-safety.

## Security (this is the part the quality gate spent the most rounds on)

- `html.escape()` on every raw-interpolated brief field (headline, summary,
  tone); `details` goes through `markdown.markdown(..., extensions=
  ["tables"])` instead, so a markdown table renders as a real `<table>`, not
  literal `| a | b |`.
- WeasyPrint's `url_fetcher` is restricted to the `data:` scheme
  (`weasyprint.URLFetcher(allowed_protocols=["data"])` — the non-deprecated
  class-based API, not the module-level `default_url_fetcher` function,
  which WeasyPrint flags for removal). Brief `details` is free-text and can
  be influenced by user notes, so a hostile `<img src="http://...">` must be
  rejected rather than trusted — one mechanism closes both the
  SSRF-adjacent network-fetch risk and unsanitized-markdown-HTML-passthrough
  risk, since neither vector reaches anything without a working fetcher.
- `date`/`metric`/`chart_type` are model-suppliable values that reach file
  paths and SQL construction. `date` is checked against
  `^\d{4}-\d{2}-\d{2}$` before touching any path; `metric` is validated
  against the same `_CHART_METRICS` whitelist `chart()` already used
  (extracted into a shared `_fetch_metric_series` helper so both tools
  inherit the same check, not two copies that could drift).
- Every write goes through `_write_atomic()`: `.resolve().relative_to(
  reports_dir.resolve())` containment check (mirrors `web/server.py`'s SPA
  fallback route), then a uuid4-suffixed `.tmp` sibling + `os.replace()` —
  refined from `briefs.py save_brief()`'s fixed `.tmp` name so two
  concurrent calls never share a temp inode.

## Graceful degradation

A per-takeaway chart fetch/render failure (a transient DB lock, a
degenerate single-point series) is caught and logged — that takeaway
renders without a chart image, but the report as a whole never fails.
`generate_chart` has no such fallback (there's no takeaway to degrade to):
a render failure there is a hard `_err`.

## New macOS-only finding (the design review didn't catch this)

WeasyPrint needs native Pango/Cairo/HarfBuzz via cffi/dlopen. `brew install
pango` alone isn't enough on Apple Silicon — Homebrew's `/opt/homebrew/lib`
isn't on the default dylib search path, so the import still fails with
`OSError: cannot load library 'libgobject-2.0-0'` until
`DYLD_LIBRARY_PATH=$(brew --prefix)/lib` is set. Documented in
`.env.example` and CLAUDE.md. Linux (CI, Docker) needs no equivalent — the
`docker-build`/container path never reaches these tools anyway (stdio-only),
so CI's `validate` job installs the same libs via `apt-get` purely so
`pytest` can import `weasyprint`.

## Shape of the change

- `agent/visuals.py` (new) — palette constants, `render_chart_png`,
  `render_brief_pdf`, the `data:`-only `URLFetcher`, the shared
  `RENDER_LOCK`.
- `agent/tools.py` — `_fetch_metric_series` (extracted from `chart()`,
  regression-tested), `_write_atomic`, `REPORTS_DIR`
  (`LOCAL_FITNESS_REPORTS_DIR`, project-relative default), the two new
  tools, `LOCAL_ONLY_TOOLS`, `make_server(extra_tools=...)`.
- `web/mcp_server.py` — `build_server`/`build_session_manager` threading
  through `extra_tools`; `build_session_manager()`'s call site left
  argument-free (the load-bearing line).
- `tests/test_visuals.py` (new, 13 tests) — rendering internals: PNG/PDF
  magic bytes, HTML escaping, markdown tables, the hostile-`<img>` block,
  and the chart-keying test that specifically catches a `zip()`-style
  positional bug a dense fixture couldn't discriminate.
- `tests/test_tools.py` — `_fetch_metric_series`/`_write_atomic` unit tests,
  full error/happy-path coverage for both tools (missing/malformed date,
  unwhitelisted metric before any SQL, per-takeaway degradation, path
  containment).
- `tests/test_mcp_server.py` — an authed `tools/list` over the real HTTP
  `/mcp/` transport proving the two tools are absent, and
  `build_server(extra_tools=LOCAL_ONLY_TOOLS)` proving they're present on
  the stdio path.
- `.gitignore` (`reports/`), `.env.example`, `.github/workflows/ci.yml`
  (WeasyPrint apt-get step), CLAUDE.md.

pyproject bumped 0.16.0 → 0.17.0.
