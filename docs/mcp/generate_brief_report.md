# `generate_brief_report`

> Renders an already-saved daily brief into a PRESS-themed PDF report. **Availability:** stdio only — local

## What it does

Takes a brief that has already been composed and saved to
`briefings/<date>.json` and typesets it: a masthead, a rail of signal cards
(each with an embedded line chart for the metric it cites), and a Training Plan
rail computed live at render time. It **renders**, it does not compose — if
there is no saved brief for that date this tool errors, and generating one is
`fitness brief` / the `brief` prompt + [`save_brief`](save_brief.md). For a
single graded workout rather than a whole day, use
[`workout_report_card`](workout_report_card.md); for one ad-hoc metric picture,
[`chart`](chart.md)'s png format.

### Why this one is local-only

The rule: **a tool that hands back a filesystem path is local-only**, because a
remote `/mcp/` caller receives a container-internal path it cannot retrieve. A
PDF is not representable as an MCP content block, so there is no inline-image
escape hatch of the kind that moved `generate_chart` into `ALL_TOOLS`. This tool
lives in `agent/tools.py`'s `LOCAL_ONLY_TOOLS` and is registered **only** by
`run_stdio()`'s `build_server(extra_tools=LOCAL_ONLY_TOOLS)` call —
`build_session_manager()` calls `build_server()` argument-free, so the
authenticated streamable-HTTP `/mcp/` transport structurally cannot see it. Not
a convention; a wiring fact.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `date` | string | yes | — | `YYYY-MM-DD`. Must match `^\d{4}-\d{2}-\d{2}$` and must have a saved brief at `briefings/<date>.json`. |

## Returns

A single text content block: the absolute path of the written PDF, plus
`coaching_line_source` whenever the report carries a Training Plan rail.

```json
{"path": "/var/folders/.../local-fitness-reports-48213-x9f2a1/brief-2026-07-21.pdf",
 "coaching_line_source": "generated"}
```

| Field | Values | Notes |
|---|---|---|
| `path` | absolute path | Always present. |
| `coaching_line_source` | `generated` \| `fallback` | Present only when the PDF has a plan rail with a today prescription. `fallback` means the Claude call failed and the line on the page is the deterministic template — the PDF is still correct, but the coach's voice is not in it. Absent when there is no plan section. |

A `fallback` is not an error and never fails the call. It exists in the payload
because the stdio path previously had no way to tell the two lines apart: the
substitution was logged at WARNING to the server log and ran unnoticed for a
month of evening briefs (#241).

Errors (single text block, `is_error` set):

| Error | Cause |
|---|---|
| `malformed date '...', expected YYYY-MM-DD` | Regex reject, before any filesystem access. |
| `no saved brief for <date>` | Nothing at `briefings/<date>.json`. |
| `brief failed schema validation: ...` | The saved JSON no longer matches the `Brief` schema. |
| `PDF render failed: ...` | WeasyPrint raised — most often the missing-native-library case below. |
| `could not prepare reports directory: ...` | The reports directory could not be created. |

The file is written atomically (`.tmp` sibling + `os.replace`, with a
`resolve().relative_to()` containment check) as `brief-<date>.pdf`, then
auto-opened on macOS (best-effort; a failed `open` logs and never fails the
call).

### What ends up on the page

| Region | Source |
|---|---|
| Masthead | Brand theme identity — stamp, `{brand_line} · MORNING BRIEF · {date}`, byline — plus `{user_name}'s Brief`. |
| Signal cards | One per `brief.takeaways`, headline + summary + markdown details, toned by `takeaway.tone`. A takeaway carrying a `metric` also gets a `line` chart PNG rendered from that metric/window. |
| Training Plan rail | Computed fresh from `plans.py` at render time by `_build_plan_section(date)` — adherence %, days-to-race, this week's planned/actual mileage, a slip count, today's prescription with a coaching line, and a last-7-days table graded done/partial/missed/rest/scheduled. |

The `Brief` / `Takeaway` schema carries **zero** plan fields and never will —
the plan section is joined in at render time, keyed to the brief's own date
rather than `date.today()`, so regenerating an old brief's PDF shows that day's
plan state.

## Example

**Ask:** "make me a PDF of today's brief"

```
generate_brief_report(date="2026-07-21")
```

→ `{"path": "/var/folders/.../brief-2026-07-21.pdf"}`, and the PDF opens on the
Mac automatically. Report the path; don't paste the JSON.

## Gotchas

- **WeasyPrint needs native Pango/HarfBuzz.** On Linux/CI that is an `apt-get`
  away, but on macOS Homebrew's install is not on the default dylib search path:
  `brew install pango` and then `export
  DYLD_LIBRARY_PATH="$(brew --prefix)/lib"` (or set it in `.env`, which the CLI
  loads at startup) so `uv run fitness mcp-stdio` and pytest both see it.
  Without it this tool fails at import/render time while
  [`chart`](chart.md)'s png format, which is matplotlib-only, keeps working.
- **Output location.** Default is a per-process ephemeral `tempfile.mkdtemp()`
  directory cleaned up at process exit — a fresh `mcp-stdio` subprocess per
  session is the natural boundary, and a best-effort sweep reaps a prior
  session's leftovers (dead PID + older than 24h). Set
  `LOCAL_FITNESS_REPORTS_DIR` to opt into a persistent directory instead (still
  auto-opened, never auto-cleaned). An old populated `./reports/` from before
  2026-07-09 is vestigial and safe to delete.
- **Theming is local-overridable.** `LOCAL_FITNESS_BRAND_FILE` points at a JSON
  file whose keys deep-merge over the PRESS default (paper `#F5F0E6`, ink
  `#181510`, dim `#6E675C`, one accent `#E8501F`, ink rules, no rounded corners /
  shadows / gradients). Tones and verdicts are typographic — done/positive is
  ink, partial/caution dim italic, MISSED/critical takes the accent — so a good
  day legitimately renders with zero orange on it. A broken brand file logs a
  warning and falls back; it never breaks a render.
- **Partial failures degrade, they don't abort.** A takeaway whose chart can't be
  fetched or rendered simply prints without an image; a plan section that raises
  is omitted entirely. One section's problem never sinks the report.
- **The plan section is omitted, not blank**, when there is no active plan or no
  plan workouts in the trailing-7-day window. With no plan rail, the signals take
  the full page width rather than leaving a dangling divider.
- **`build_plan_detail()` has no "as of" date.** Its verdicts are always graded
  against the real data frontier, so `date` only selects which graded day is
  "today" and which trailing window to slice. (Its 4th positional arg is
  `best_effort`, a Riegel-projection dict — not a date. That mistake has been
  made here before.)
- **Today's coaching line is a Claude call** (`agent/plan_coach.py`: toolless,
  single-shot, behind a disk cache keyed on the prompt hash). Identical inputs
  reuse the cached line; any input change regenerates; failures are never cached
  and fall back to a deterministic template. The PDF generates either way — read
  `coaching_line_source` in the payload to know which line you got. A cache hit
  counts as `generated`; the distinction is template versus coach.
- **Only `data:` URIs are fetchable from the HTML.** Charts are embedded as
  data URIs and the WeasyPrint `URLFetcher` is restricted to that scheme —
  brief text can be influenced by free-text user notes, so an injected
  `<img src>` / `@import` has nothing to reach.

## See also

- [`save_brief`](save_brief.md) — writes the JSON this tool renders.
- [`get_brief_context`](get_brief_context.md) — the pre-assembled context a brief
  is composed from.
- [`workout_report_card`](workout_report_card.md) — the other PDF tool, and the
  other member of `LOCAL_ONLY_TOOLS`.
- [`chart`](chart.md) — `format="png"` is the chart renderer this shares
  (`visuals.render_chart_png`).
