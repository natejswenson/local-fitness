# Reports and charts in ChatGPT

## User outcome

"Show my latest workout report" returns computed ratings, the formatted card,
and an inline HR chart in one MCP call, on stdio and HTTP. Repeating the call
reuses matching stored coaching text without waiting for a model. A requested
PDF is a separate output choice; remote downloads never expose filesystem paths.

## Implementation

- `agent/tools.py`: move `workout_report_card` into `ALL_TOOLS`; default to
  `inline`, retain table/PDF/both formats, and add an explicit local-only
  coaching-generation option. Inline results use cached coaching or a labeled
  deterministic read. No Garmin details fetch or journal generation runs on
  this fast path. Add coverage/freshness messages without changing ratings.
- `web/mcp_server.py`: preserve native MCP content and structured results at
  the external transport boundary. Set export capabilities per call, with
  context isolation between concurrent callers. Keep the SDK backend intact.
- `agent/visuals.py`: reuse the existing HR chart and theme; render the
  existing daily/weekly plan-chart rows as a labeled PNG. Charts default to
  inline PNG; explicit ASCII and ASCII-only styles remain available.
- `web/artifacts.py` and `web/server.py`: bounded in-memory PDF downloads,
  authorized by an unguessable, short-lived capability scoped to one artifact.
  No arbitrary file reads, no public directory, no API token in URLs. A
  configured `LOCAL_FITNESS_PUBLIC_URL` supplies the trusted link origin.
- Prompts, MCP pages, README, `.env.example`, deployment guidance and
  `CLAUDE.md`: describe inline images separately from fenced ASCII, portable
  report creation, PDF delivery, and the explicit coaching-generation path.

## Trade-offs

- Default reports use cached or deterministic prose so latency does not depend
  on a second model. Local callers can explicitly request generated prose;
  HTTP cannot start Claude generation. Grades always use the existing rubric.
- Downloads expire after ten minutes or server restart. Bounded storage may
  evict the oldest download; requesting the PDF again creates a new link.
- A sync timestamp describes recorded ingestion, not proof every Garmin
  endpoint succeeded. Report messages must not claim stronger freshness than
  the data supports. The broader Garmin reliability changes are separate work.

## Verification

- Synthetic HTTP journeys: latest report, repeated report, weekly chart,
  missing splits, stale data, and PDF download. Assert actual ratings/text,
  decodable images, unchanged cached reads, and no hidden network/model calls.
- Security: missing/wrong/expired capabilities, path traversal, scope isolation,
  and unauthenticated report creation. Download links never authorize MCP calls.
- Preserve existing PDF page-count and grading tests, cache/warm behavior,
  tool-count/documentation gates, and the two-connection report-card budget.
- Run the full Python suite, Ruff, prompt scorer and applicable performance
  checks. Visually inspect generated synthetic charts and a PDF. Land through
  a feature PR into `dev`, rebuild the container from `dev`, and smoke-test it.
