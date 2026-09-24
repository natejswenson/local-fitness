# Fitness answers designed for chat

## Request
Improve local-fitness's UI/UX inside Codex using the local-dev workflow.

## Acceptance criteria
- Daily snapshots, active-plan status and plan progress arrive as readable Markdown
  plus the complete existing payload in MCP `structuredContent`, on both stdio
  and HTTP. Internal SDK callers keep their existing JSON text contract.
- Snapshot values have readable labels/units and explicit dates: yesterday's
  comparisons, provisional readings, partial totals and current-form dates cannot
  masquerade as settled data from today. Empty data gives a useful next step.
- Plan summaries distinguish active plans from pending drafts, prescriptions from
  actuals, pending from missed, and whole-plan adherence from the displayed window.
  Workout descriptions and HR caps remain visible; multiple sessions retain identity.
- Charts name the metric/window in the image, show isolated readings, and place
  samples by calendar date so missing days do not look like adjacent days.
- Instructions and examples consistently keep reports/images in the reply and
  reserve PDF exports for explicit requests. No host-specific paths or client hacks.
- Existing grading, tools, resource URIs, write permissions and export behavior
  remain compatible. Tests use fabricated data only.

## Decisions and scope
The repository retired its web UI; the MCP conversation is the product surface.
The user confirmed both polished in-chat results and smoother/faster everyday
coaching interactions. No application chrome changes,
new frontend, new tool aliases, model changes or live fitness data modifications.
The human-facing summaries are deterministic projections, not new coaching or
grading logic. Preserve all source fields as structured data. Avoid exposing saved
preferences in the visible snapshot; they remain in the existing structured payload.
Use the current PRESS theme without hand-writing brand values.
Deliver a draft PR into `dev`; do not merge or rebuild the live deployment from a
feature branch. Validate the container with an isolated image build/smoke check.

## Repository evidence
- Base: clean local `dev` at `614f7dd`; remote freshness unverified (local-dev
  keeps investigation offline). Branch: `feature/codex-chat-ux`.
- `web/mcp_server.py:build_server` already adapts external results independently
  of the SDK. Inline report/chart results preserve images and structured fields.
- `agent/tools.py` returns JSON text for snapshots and plan summaries. Its hot
  paths have DB-open/performance gates; presentation must not add database reads.
- `_render_status` labels the load with `as_of` (pipeline date), ignoring
  `current_form_date`, and ignores partial/provisional metric flags.
- `agent/visuals.py:render_chart_png` uses sample indices rather than dates,
  omits a marker for a single-point line, and receives no metric title from `chart`.
- `agent/prompts.py` describes a monospace pane and a fixed character width;
  docs still describe the default workout report as opening a PDF.

## Implementation
1. Add a pure chat-presentation module for daily snapshots and plan status/progress.
   Integrate only the three named successful external tool results; leave errors,
   images, other tools and memory-only operation untouched. Reuse the snapshot
   renderer in the coach prompt with the same rounding and settling guard.
   Offer `format="json"` for existing text-JSON consumers. Add date/frontier and
   effective-window metadata from values already in hand. Show same-date actuals
   once as day totals, never as per-session actuals. Status is explicitly a single
   session summary; complete-day requests route to progress.
2. Improve standalone chart labeling and date geometry using existing theme and
   formatters, retaining the shared PDF chart API and no new dependencies.
3. Update conversational formatting guidance and relevant docs/examples. Bump the
   package minor version and lockfile metadata; add a concise changelog entry.
4. Add pure-renderer edge tests and actual MCP request/response journeys proving
   machine-payload parity and the stdio/HTTP contract. Inspect synthetic previews.

## Validation
- Focused renderer, chart, prompt and MCP tests, including no data, zero values,
  stale/provisional data, multiple sessions, drafts, descriptions with Markdown,
  short/full plan windows, SI fallback, missing days and a single chart reading.
- `uv run ruff check .`, `uv run pytest -x` with the existing 85% coverage gate,
  `uv run python scripts/score_prompt.py` and local perf structural checks.
- Build an isolated Docker image and smoke-test its CLI/MCP health surface where
  Docker is available. Do not touch the live `dev` deployment or call models/Garmin.
- Inspect a fabricated snapshot/plan response and PNG at chat width. Record exact
  checks and any environmental limitations in the adjacent verification note.

## Review
Independent reviewer: `/root/plan_review` (read-only, no remote calls).
All findings accepted:
- Actuals repeat each date's totals per prescription (`plans._workout_actuals`):
  group by date and display the total once; retain session identities/verdicts.
- Status selects one session: label its scope, route complete-day asks to progress.
- `this_week` is trailing seven days: label accordingly. Explicit metadata avoids
  guessing the data frontier and progress window.
- Preserve external JSON compatibility via an explicit format and test parity.
- A missing settled value with a provisional reading is not an empty database.
- Speed claims require evidence: assert no new DB opens/network/model work and
  measure envelope sizes. Do not claim lower tokens or measured Codex latency.
- Inspect chart coordinates, singleton markers and titles, not just PNG bytes.
No unresolved blockers remain.
