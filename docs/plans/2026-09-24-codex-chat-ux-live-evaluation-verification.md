# Live chat evaluation results

Plan commit: `58360f0`. Starting implementation: `9b0d940` on
`feature/codex-chat-ux`, draft PR #268 into `dev`.

## What was exercised
Started a fresh 0.66.0 MCP stdio server from this checkout with the actual local
configuration and existing operational data. Used the production handlers through
`ClientSession.call_tool`, not ad-hoc SQL or hand-built fitness summaries.
Initialization advertised inline/json on all three changed tools. The already
connected Codex tool returned old JSON, so it was not treated as the changed server.

The final matrix made 29 requests: 27 successful results and two expected errors
(unknown metric, no stored report for the requested activity). Cases:

| Journey | Observed result |
| --- | --- |
| Daily check-in | Readable date-grouped tables; partial/provisional distinctions; current-form date; computed effort beside activity name |
| Today's plan | Saved title, neutral custom-plan end date, full instructions, session identity and computed verdict |
| Recent and full progress | Explicit window; unchanged rollups/verdicts; today's actuals unfinished; no future actual totals |
| Compatibility | All four inline responses exactly matched paired JSON payloads in structuredContent |
| Metric charts | Resting HR, sleep, freshness/combo, and VO2 max returned inline PNGs |
| Plan chart | Actual inline chart returned with planned/actual legend and run/walk units |
| Saved report | Listed an existing card, retrieved its stored text and image without rerating or generating coaching |
| Missing report / invalid metric | Useful error and recovery tool / allowed metrics; no implicit writes |
| Repeated requests | Three repeated calls each for snapshot, status, progress, and HR chart remained consistent |

All pre-existing structured facts compared exactly before/after the corrections.
Only plan titles and complete status descriptions were added. Existing capped
descriptions remain unchanged. No grading code changed.

## Defects found and corrected
- A custom maintenance plan was labeled as a race. The view now uses its saved
  title and a neutral end date; supplied finish projections still appear.
- Status cut instructions off at character 120, mid-word. Complete descriptions
  are now copied from already-loaded rows, matching both date and session number.
  The original capped field remains compatible; prose is rendered literally.
- Original activity names hid the computed walking classification. The view now
  includes that existing classification rather than inferring effort from a name.
- Future sessions displayed zero actual mileage. Future actual totals are omitted
  from the view; today's total is explicitly unfinished; payload facts are retained.
- At 360px, four-column snapshot rows repeatedly split dates and values across
  lines. Three-column tables group readings under dated headings and retain arrows.

## Visual evidence and tradeoffs
Used the browser skill with Chrome and a local-only Markdown preview (Python
Markdown tables, system font 16px/1.5, 8px cell padding, no external assets).
The preview was a fixed 360px or 760px reading column, served on loopback.
No browser styles were added to the product: MCP clients control their rendering.

| Snapshot proxy measurement | Before | After |
| --- | ---: | ---: |
| 360px combined table height | 1733px | 1342px (-23%) |
| 360px whole response height | 2353px | 2139px (-9%) |
| 760px whole response height | 1465px | 1636px (+12%) |

Both widths had zero horizontal overflow before and after. The fix addresses
wrapping and scan order, not an invented overflow bug. The wider view is longer
because of explicit date sections and effort labels. Visually inspected the
narrow snapshot and complete plan status. Status and progress also had zero
horizontal overflow at both widths. Inspected actual HR, sleep, freshness,
plan-comparison, and saved-report images at their native sizes.

Visible text sizes: snapshot 2132 → 1949 characters; recent progress 6492 → 5847;
full progress 42043 → 35938. Status grew 665 → 1125 characters to restore the
missing instructions and title. Structured data is additional MCP content, so
these are not total-token savings or measured end-to-end latency improvements.

## Timing observations
Final fresh-process startup/initialization: 285.5ms. Single-run request timings,
including local MCP transport (not statistical benchmarks):

| Tool | First call | Three repeat calls |
| --- | ---: | ---: |
| Snapshot | 8.2ms | 9.3–10.1ms |
| Status | 1.5ms | 1.3–1.5ms |
| Progress | 2.1ms | 2.0–2.2ms |
| HR PNG | 133.3ms | 24.0–25.2ms |

Other first calls: full progress 3.2ms; sleep/freshness 26.1/29.1ms; plan chart
52.3ms; existing stored report with image 47.3ms. These numbers demonstrate
working local paths, not a claim that Codex answers faster.

## Local checks
- Focused renderer/MCP suite: **78 passed**; includes real stdio subprocess and
  HTTP tests, JSON parity, full instructions, session matching and shared actuals.
- Full suite: **2922 passed, 6 skipped, 1 existing warning**, 49.98 seconds;
  coverage **95.31%** against the required 85% gate. Benchmark timing cases are
  skipped by repository default; structural performance tests passed.
- Ruff: passed. Prompt scorer: **11/11**. Docs drift after documentation edits:
  **103 passed**. `git diff --check`: passed.
- Isolated Docker image `local-fitness:codex-chat-ux`: build passed, including the
  real one-page PDF runtime check. `docker run --rm --network none` with no host
  mounts exercised the production snapshot handler and readable empty state.

Full-suite environment: legacy preference/journal backends for synthetic isolation,
`DYLD_LIBRARY_PATH=/opt/homebrew/lib`, temporary matplotlib/cache directories;
command `.venv/bin/pytest -x`. The live matrix used the real configured vault
backend, with only its existing read methods.

## Boundaries and remaining uncertainty
Direct computer control of Codex was explicitly denied by the computer-use tool;
no workaround was attempted. Browser layout checks are proxies. Native Codex
rendering, automatic model tool selection, and end-to-end conversational latency
remain unverified. The existing connected fitness server still needs a reload
to load changed local code; no client settings or running deployment were changed.

The first exploratory process used normal DB connections and hit the sandbox's
loopback restriction for memory, so it served a degraded preference state. Those
results were not used for final comparisons. Final runs loaded configuration,
called `run_stdio()` directly without schema initialization, and replaced the
test process's connection factory with existing `db.connect_readonly`. They used
only read tools. No Garmin, plan, journal, calendar/email, model, or report-cache
writes were requested. Raw responses, persona data, preview pages and images
remain private temporary artifacts outside Git; committed regressions are fabricated.
