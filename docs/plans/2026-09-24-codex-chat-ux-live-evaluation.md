# Live evaluation of the chat experience

## Request and scope
Use local-dev to exercise the changed code through actual local usage and verify
that the UI/UX improves. Resume draft PR #268 on `feature/codex-chat-ux` at
`9b0d940`; cached base remains `origin/dev`. Remote state is unverified this turn.

## Acceptance criteria
- Start a fresh MCP stdio process from this checkout using the existing local
  fitness data. Verify it advertises the changed schemas and readable results;
  the connected session currently returns the older JSON presentation.
- Exercise daily check-in, today's plan, recent/full progress, metric charts,
  plan chart, and an existing saved report through production tool handlers.
- Compare readable text with its structured source for dates, units, session
  identity, prescription/actual distinctions, and missing data. Record response
  lengths and request timings without claiming end-to-end Codex latency.
- Inspect images and Markdown at wide and narrow reading widths. Native Codex
  computer control is explicitly unavailable; a browser preview is a layout
  proxy, not proof of native Codex or model behavior.
- Correct observed presentation defects only; preserve grading, structured facts,
  transport compatibility, and no extra data/model calls. Recheck affected live
  workflows and add fabricated regression cases for confirmed defects.

## Method and privacy
Use the MCP client/protocol, not ad-hoc SQL queries. Read-only requests only:
no Garmin sync, plan edits, report generation/cache writes, calendar/email or
model calls. Keep any personal output and browser previews in a private temporary
directory outside Git. Commit only generic findings and fabricated test data.
Use production read handlers against the existing DB; do not initialize its schema.
The preview must use local assets and listen only on loopback. Do not attempt to
bypass the explicit denial of access to the Codex app.

## Implementation and validation
1. Run the live matrix and inspect results; prioritize concrete usability defects
   over speculative redesign. Compare the current branch with corrected renderers
   on identical captured production payloads where layout changes are needed.
2. Make focused renderer/schema/prompt corrections only if evidence warrants them.
   Retain the existing unreleased 0.66.0 feature version and update its changelog.
3. Rerun affected tests, full pytest with coverage, Ruff, prompt scorer and isolated
   container build if code changes. Do not rerun unrelated timing benchmarks.
4. Record source-backed observations, results, and limitations in a verification
   note; review the diff, commit, verify the existing draft PR once, and update it.

## Review
Independent reviewer: `/root/plan_review`; all corrections accepted.
- Launch `run_stdio()` directly, never the schema-initializing CLI. The first
  exploratory run used normal production connections; subsequent runs enforce
  `db.connect_readonly` in the test process, including all plan/snapshot handlers.
- Use `get_report_card` only; keep persona initialization private; redirect caches.
- Compare identical captured payloads, report startup/first/repeated timing
  separately, and measure overflow at fixed widths. No native Codex claim.

## Confirmed defects to correct
The real tool outputs exposed issues absent from the short synthetic fixtures:
- Custom plans display their end date as a race despite lacking a goal distance.
  Use a neutral end-date label for custom plans and show the existing plan title.
- Status descriptions truncate at 120 characters, cutting instructions mid-word.
  Preserve the legacy description field and add the full source description for
  readable status; obtain it from already-loaded workouts without extra queries.
- Recent activity names can say running while the computed effort says walking.
  Show the existing effort classification beside the original activity name.
- Progress displays future zero totals as actual activity. Suppress future actuals
  and label today's totals as unfinished; retain all original structured fields.
- At 360px the snapshot did not overflow, but values/units and dates wrapped across
  multiple lines and only four distinct metrics fit in the first screen. Group
  readings under dated headings to remove the repeated date column and separate
  settled comparisons from provisional readings.
These are presentation corrections, not changes to the grading or plan semantics.
