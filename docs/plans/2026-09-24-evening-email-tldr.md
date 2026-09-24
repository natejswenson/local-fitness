# A twenty-second evening fitness email

## Request
Redesign the daily evening email as a beautiful, concise fitness summary. The
reader should understand the day and next step in under twenty seconds.

## Acceptance criteria
- Lead with actual activity for the brief's date, followed by three useful
  numbers, at most one brief takeaway, and the next day's prescribed workout.
- All visible copy is at most 80 words; stressful inputs never grow into a
  deep dive. Long generated prose is omitted with an honest full-brief cue,
  never cut mid-sentence or rewritten into unsupported coaching.
- PRESS typography, warm paper, whitespace and rules; readable at 320/390 px
  and desktop without horizontal scrolling. Inline CSS, table layout, meaningful
  headings, matching plain text. No charts, weekly tables, social byline or
  markdown details in the evening email.
- Dates, activity labels and unavailable data remain honest: measured walking
  is not called running; missing values are not zero; tomorrow means target+1;
  an absent prescription is not a rest day. Include compact sync provenance.
- Existing SMTP settings, TLS, kill switch, recipient override, successful-send
  marker, backstop and dry-run safety remain intact. PDF reports stay full.

## Decisions and scope
Confirmed priority: "Activity + tomorrow's workout" (user response). This is
presentation and deterministic input assembly only:
no prompt/model change, new model call, live Garmin pull or actual email send
during validation. Reuse the saved brief, unit/effort helpers and active plan.
No website link is invented: the product's detail surface is fitness chat.
Keep the existing dated subject for inbox threading compatibility.
Delivery is a draft PR into dev. Validate an isolated Docker build/run; do not
merge or replace the live deployment as part of this new local-dev task.

## Repository evidence
- Clean local dev at f13fc80 (0.66.0); cached base, remote unverified until delivery.
- `agent/email_render.py` currently emits every takeaway's markdown details,
  inline charts and the full plan rail, with no content budget.
- `cli.brief_email` calls `assemble_brief_render_inputs`, rendering all charts
  and resolving an additional plan-coach line even for dry-run. The compact
  email needs neither; the PDF retains this shared legacy helper.
- `db.connect_readonly`, `plans.get_active_plan`, `agent/workout_rows.py`,
  `agent/units.py` and `tools.data_as_of_today` provide existing facts/helpers.
- `tests/test_cli_brief_email.py` covers send/dedupe/settings guards;
  `tests/test_email_render.py` asserts the old deliberately verbose contract.

## Implementation
1. Add a small read-only, target-date email input assembler and a pure bounded
   digest representation shared by HTML and plain text. No new data schema.
2. Replace the verbose email layout with a compact editorial summary; escape
   every external string and remove arbitrary markdown rendering.
3. Wire the CLI to the digest input path, passing no image attachments. Remove
   obsolete plan-coach diagnostics from the email path only.
4. Add behavioral coverage for selection, budget, missing data, measured effort,
   historical dates, tomorrow boundaries, safe content and real dry-run MIME.
5. Update version, changelog and canonical email architecture guidance.

## Validation
- Reproduce the old long email with fabricated representative content; keep
  before/after HTML, EML and desktop/mobile screenshots in local temporary files.
- Inspect rendered previews at desktop, 390 px and 320 px, including missing
  data and long content; measure words, vertical size and horizontal overflow.
- Exercise the actual CLI dry-run with an isolated fabricated SQLite DB and
  saved brief, without Garmin, model or SMTP access. Parse its MIME and inspect
  both alternatives. Test delivery guards with existing network-blocked tests.
- Focused email/CLI/mailer tests, full `uv run pytest -x` (85% gate), Ruff,
  prompt scorer, PRESS artifact lint and isolated Docker build/smoke test.
- Review final diff for scope, secrets, public-safe fixtures and compatibility.

## Review
Independent reviewer: `/root/email_plan_review`, read-only local review.

- Accepted: preserve every target+1 `date+seq` prescription. Render compact
  outlines for up to two sessions; larger/over-budget days show an explicit
  session count and full-instructions cue. Never silently select the first.
- Accepted: distance/pace is not a complete interval prescription. Include all
  structured duration/distance/pace/HR constraints in an outline and always
  indicate where full instructions live when descriptions are omitted. Reuse
  the calendar's measured-pace prescription label (walking versus running).
- Accepted: guard on-foot activity types before measured effort classification;
  test cycling, mixed sports and unknown pace. Never relabel a bike as a run.
- Accepted: a missing database/table must degrade to unavailable, not fabricated
  zeros; retain explicit no-plan/rest/no-prescription distinctions.
- Accepted: bound the entire message, including multi-session plans and long
  source fields. Never substitute a positive takeaway for an overlong critical
  one. Use an explicit review cue instead. Do not clip advice mid-sentence.
- Accepted: use sync provenance only for contemporaneous data; an older date is
  an archived snapshot, not a claim that today's ingest refreshed that date.
- Baseline reproduction: fabricated old email has 319 visible words, 1,405 px
  document height at the initial browser zoom. Corrected, zoom-adjusted
  measurement at 320 CSS px: 1,781 px content height and no horizontal overflow.
  The earlier 433 px width was browser zoom, not a layout overflow.

No unresolved blockers. These corrections are part of the committed scope.
