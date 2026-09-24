# Evening email TL;DR verification

Reviewed plan commit: `a5de5fd`.

## Result

The evening email now leads with measured activity, three daily numbers, one
bounded takeaway and tomorrow's prescribed workout outline. HTML and plain text
share an 80-word ceiling. Dense days prioritize workout outlines over optional
commentary; critical context retains an explicit review cue. Charts, markdown
details, the weekly table, social byline and extra plan-coach call are removed
from email assembly. The PDF path remains intact. Version: 0.67.0.

The parent reviewed the final diff against the plan, with particular attention
to missing totals, sport/effort classification, all date+seq prescriptions,
source timestamps, escaping, send markers and private data. All tracked fixtures
are fabricated. No SMTP send, live Garmin pull, merge or deployment was performed.

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
