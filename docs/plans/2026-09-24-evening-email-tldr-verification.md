# Evening email TL;DR verification

Reviewed plan commit: `a5de5fd`.

## Result

The evening email now leads with measured activity, five daily numbers, one
bounded takeaway and tomorrow's prescribed workout outline. HTML and plain text
share an 80-word ceiling. Dense days prioritize workout outlines over optional
commentary; critical context retains an explicit review cue. Charts, markdown
details, the weekly table, social byline and extra plan-coach call are removed
from email assembly. The PDF path remains intact. Version: 0.67.0.

The parent reviewed the final diff against the plan, with particular attention
to missing totals, sport/effort classification, all date+seq prescriptions,
source timestamps, escaping, send markers and private data. All tracked fixtures
are fabricated. At initial draft delivery, no SMTP send, live Garmin pull, merge
or deployment was performed; see the later preview-send follow-up below.

## Observed layout

Fabricated before/after sample, rendered through the real email functions:

| Preview | Visible words | Content height at 320 CSS px | Horizontal overflow |
| --- | ---: | ---: | --- |
| Previous full email | 319 | 1,781 px | None |
| Compact daily email | 68 | 596 px | None |
| Missing inputs | 48 | 564 px | None |
| Two dense sessions | 70 | 576 px | None |

The ordinary compact email also fits at 390 px (577 px content height) and
desktop (562 px content height). Screenshots were visually inspected. Browser
zoom was accounted for: the first reported 433 px measurement came from 90%
zoom, not overflow, and the earlier interpretation was explicitly corrected.

The actual `fitness brief-email --date 2026-09-24 --no-pull --no-generate
--no-notify --dry-run ...` produced a local 62-word draft from existing saved
data. Its 320 px preview measured 605 px tall with no horizontal overflow and
preserved separate sync/generation timestamps. Private preview files remain
outside Git. A startup warning about the sandbox-unreachable optional memory
writer did not prevent the dry-run; no account refresh or email send occurred.

HTML, EML and screenshots are retained locally under the temporary
`fitness-email-preview` directory. These are browser layout and MIME checks,
not a claim of verification in Gmail, Outlook or Apple Mail. The 20-second goal
is supported by the content budget and scan layout, not a timed user study.

## Checks

- Focused renderer, real read-only loader, CLI and mailer tests: **86 passed**.
- Final full `uv run pytest -x`: **2,918 passed, 6 skipped**, **95.35% coverage**
  (85% required). One existing Starlette/httpx deprecation warning.
- Full Ruff: passed. Prompt scorer: **11/11**. Prompts/models unchanged, so no
  model A/B generation was needed.
- PRESS lint: zero findings on ordinary, missing-input, dense-session and
  saved-data HTML artifacts.
- Isolated Docker image built successfully, including its native PDF runtime
  check. Final email rendering smoke passed in that image with `--network none`.
- `git diff --check`: passed.

Full tests used the existing offline memory backends via
`LOCAL_FITNESS_PREFERENCES_BACKEND=legacy LOCAL_FITNESS_JOURNAL_BACKEND=legacy`,
with macOS native library and temporary cache paths configured. An initial
unisolated full run failed in an unrelated inline-report test because the local
`.env` selected a vault writer unreachable in the sandbox. The isolated final
run above passed; no production memory setting was changed.

Coverage includes real CLI MIME composition, no image parts, no extra PDF/coach
assembly, successful-send/backstop guards, zero versus unavailable metrics,
incomplete duration/distance totals, cycling/mixed/unknown effort, km units,
prescribed walks, year rollover, double/many sessions, complete source selection,
overlong critical prose, hostile markup, missing DB/tables and date mismatch.


## PR bot follow-up (2026-09-24)

Reviewed all three inline GitHub Advanced Security comments on PR #269:

- CodeQL alerts 109 and 110: accepted. Moved the shared sync timestamp query
  and failure-status vocabulary to `db.py`; callers use that one implementation.
  Email loading no longer imports `agent.tools`, and `email_render` imports its
  digest type only under `TYPE_CHECKING`. The query and connection reuse are
  unchanged. A fresh-process regression test proves loading email inputs imports
  neither the agent tool module nor the Claude SDK.
- CodeQL alert 108: accepted. The MIME test now consistently uses `from email
  import ...`, rather than mixing both import styles for the same module.

Validation: 447 focused tests passed; full suite **2,919 passed, 6 skipped**,
**95.34% coverage**. Ruff, prompt scorer (11/11), diff check, isolated Docker
build, and a network-disabled container import/render smoke passed. The smoke
intentionally exercises a missing database and verifies its unavailable-data
fallback. No presentation or prompt changes were made in this follow-up.

The user explicitly requested one email to inspect in their inbox. Prepared a
62-word preview from saved local data using the normal renderer and mailer,
then sent it to the configured recipient. SMTP accepted the message. Its
subject is prefixed `Preview:`; this direct preview send does not write the
nightly `.emailed-*` marker. No Garmin pull, regeneration, merge or deployment
was performed. The private EML and send receipt remain outside Git.

## Five requested metrics and second preview (2026-09-24)

Reviewed amendment commit: `ca63497`, with focused independent review from
`/root/email_plan_review`. The user requested steps, workout time, workout
score, sleep score and resting HR, and explicitly authorized another email.

The two activity totals now lead a 2+3 metric layout. The three smaller values
show the main on-foot workout's existing capped score out of 5, Garmin sleep
score out of 100 and resting HR in bpm. Saved workout scores are labelled
saved; only today's missing card is calculated with the existing local grader.
No rubric changes, model calls, card writes or Garmin fetches were introduced.
Known-empty workouts show 0m; incomplete totals and ungradeable scores show —.

Final browser checks, all visually inspected with no horizontal overflow:

| Preview | Words | Height at 320 CSS px |
| --- | ---: | ---: |
| Five populated metrics | 75 | 702 px |
| Missing data | 54 | 656 px |
| Dense double day | 77 | 682 px |
| Actual saved-data preview | 68 | 696 px |

The populated sample is 669 px tall at 390 px and 646 px on desktop. The
temporary viewport override was reset. PRESS lint found zero issues in all
four artifacts. MIME/plain-text parity and the complete 80-word budget remain
covered by tests; inbox-client appearance is left for the requested user review.

Validation: full suite **2,929 passed, 6 skipped**, **95.35% coverage**; Ruff,
prompt scorer **11/11** and diff check passed. The expanded tests exercise
real database selection and local capped grading, saved-score precedence,
bike/walk exclusion from main-run selection, date isolation, historical/future
non-regrading, missing/invalid scores and failure isolation. A fresh process
exercises today's uncached scoring path without importing the agent tools or
Claude SDK. Database snapshots confirm no score writes. Initial new fixtures
needed a required description and a running prescription before they exercised
the intended cap; the final real-grading case passes.

The isolated Docker build passed, including native PDF rendering. A separate
network-disabled container used synthetic SQLite data to calculate a workout
score, render all five metrics and verify that no report card was saved and
no agent runtime imported.

SMTP accepted one new message to the configured recipient, subject
`Updated preview: Your fitness TL;DR · 2026-09-24`. It uses the reviewed
saved-data HTML/plain MIME. The preview's direct mailer path does not set the
nightly sent marker. Private artifacts/receipt remain in the temporary preview
directory; no personal health data was committed. The running deployment was
not replaced as part of this draft-PR refinement.
