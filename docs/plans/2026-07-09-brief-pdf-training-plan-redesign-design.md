---
ticket: "#TBD"
title: "Brief PDF redesign: 2-column signals + new Training Plan section"
date: "2026-07-09"
source: "design"
---

# Brief PDF redesign: 2-column signals + new Training Plan section

## Context

Nate reviewed the current `generate_brief_report` PDF (shipped in #88/#91) and
flagged it isn't there yet visually — the takeaway cards stack in one column,
and the report has no dedicated training-plan view. He wants two changes,
confirmed via an approved HTML/CSS mockup
(`/private/tmp/.../brief-pdf-mockup.html`, screenshot round-tripped through
`/design`):

1. **Reflow the existing signal-card section into a 2-column grid.** Same
   cards, same tone-colored accent bars, same content — just denser, less
   scroll.
2. **Add a new "Training Plan" section** below it: adherence, today's
   prescribed run (with a coaching line prepping him for it), and the last
   week's completed runs graded against what was prescribed.

The whole point of the report, per Nate: "extremely clean and crisp —
something I enjoy visually looking at that has good data." This is a look
and information-density upgrade, not a new capability — the underlying data
(`plans.py`'s day-by-day grading) already exists and is unchanged by this
work.

## Design decisions

- **Signals grid is CSS flexbox (`flex-wrap: wrap`, ~50% basis per card),
  not CSS Grid.** Robust to the real, variable takeaway count (`brief_planner`
  triggers 1-N cards depending on the day, not always exactly 4) — a
  fixed 2x2 grid would break the day it sees 3 or 5. The last card in an odd
  count spans full width (`flex-basis: 100%` via a `.span-full` class
  computed in Python: `is_odd_last = index == len(takeaways) - 1 and
  len(takeaways) % 2 == 1`) rather than leaving a dangling gap. Flexbox over
  Grid because it's the longer-established, more reliably-supported layout
  model in WeasyPrint; Grid support is newer and this isn't a place to find
  out it's flaky.
- **Training Plan data is computed live at PDF-render time, not stored on
  the `Brief` schema.** `Brief`/`Takeaway` (schemas.py) carry zero
  plan-specific fields today, and stay that way — `generate_brief_report`
  calls `plans.get_active_plan()` + `plans.build_plan_detail(plan, frontier,
  activities_by_date, today=target_date, cfg)` fresh, the same functions
  `get_training_plan_progress` already uses, just anchored to the **brief's
  date** (`target_date`) as `today` instead of `date.today()` — so
  regenerating an old brief's PDF shows that day's plan state, not today's.
  This keeps the change fully additive to the PDF-only code path: zero risk
  to the V2 brief generation pipeline, zero eval-fixture/schema-version
  impact.
- **The coaching line is Claude-generated, using the same model as the real
  daily brief, called fresh on every render — with a deterministic
  fallback on failure.** Confirmed with Nate: not a canned template as the
  primary path (he wants a "coach me" line with real judgment, not fill-in-
  the-blank prose), and not cached (regenerates every render, even for
  repeat opens of the same day — accepted cost/latency tradeoff for a
  personal single-user tool). "Same model as the real daily brief" resolves
  concretely to `briefing.DEFAULT_MODEL` via the Claude Agent SDK's toolless
  single-shot pattern (`ClaudeAgentOptions(model=briefing.DEFAULT_MODEL,
  max_turns=1)`, no MCP tools) — **not** the `opencode:`/`ollama:`
  alt-model transport, which stays exactly what its own docstring says it
  is ("shadow-run diagnostic only... never the live production path unless
  a future, separate decision promotes it"). This isn't that promotion: it
  reads `briefing.DEFAULT_MODEL`, the same constant the real
  `generate_and_save()` always passes, so if that constant ever changes,
  this call follows automatically without a separate setting to keep in
  sync. No new env var. If the call fails for any reason (missing/expired
  `.env` credential, network, timeout, malformed response), a deterministic
  fallback line is used instead and the PDF still generates — matches this
  file's existing "one render failure must never fail the whole report"
  precedent (see `generate_brief_report`'s per-takeaway chart try/except).
- **New module `agent/plan_coach.py`** houses the coaching-line logic:
  `build_prompt(...)`, `generate_coaching_line(...)` (the Claude call —
  Agent SDK imported inside the function body, not at module top level),
  and `fallback_coaching_line(...)` (the deterministic template). Kept
  separate from `briefing.py` (whole-brief generation lifecycle, eval'd
  against `baseline.json`/fixtures — a different concern) and separate from
  `visuals.py` (pure rendering, no LLM calls, no DB access — stays that
  way; `render_brief_pdf` still does zero I/O so it stays trivially
  testable with plain string-membership assertions). The Agent SDK import
  must be deferred exactly like `visuals.py`'s matplotlib/weasyprint
  imports — `tools.py` is imported by the always-running containerized web
  server, so a module-level heavy import in anything it imports would
  contaminate that process for a feature it never uses.
- **Coach voice**: `generate_coaching_line` resolves the same
  `coach.resolve_coach_profile()` used by the real brief, so the line's
  tone (harshness/warmth/push, hardass-by-default) matches the rest of the
  report instead of inventing a separate voice.
- **"Last week" = trailing 7 calendar days ending on the brief's date**, not
  the plan's week-index boundaries — avoids a partial-week-at-the-seam
  confusion when regenerating a brief mid-plan-week, and reuses the exact
  same `workouts` slice for both the stat strip's mileage tile and the
  table, so the two can't disagree.
- **Stat strip is 4 tiles**: adherence %, days-to-race, this-week mileage
  (planned/actual, same trailing-7-day window), and a "slips" count (workouts
  graded `partial` or `missed` in that window) — this last tile is new,
  proposed during the mockup pass, not explicitly requested; flag if it
  doesn't earn its place once it's live.
- **Whole Training Plan section is omitted, not shown empty, when there's
  nothing to show** — no active plan, or an active plan with no workout
  data at all for the trailing-7-day window (e.g. brief date is before the
  plan started). Matches "extremely clean" — an empty-state section is
  clutter, not data.
- **Units**: distances/paces in the new section use `units.to_miles()` /
  `units.format_pace_min_per_mi()`, same as everywhere else in this app
  (miles, min/mile — never km).

## Files to change

- **`src/local_fitness/agent/plan_coach.py`** (new): prompt assembly +
  Claude call + deterministic fallback. Public surface below.
- **`src/local_fitness/agent/visuals.py`**:
  - `render_brief_pdf(brief, charts, plan_section=None)` — new optional
    third parameter. `None` (default) renders exactly as today (existing
    tests/callers unaffected).
  - New `_CSS` rules: `.signals` flex container + `.signal-card`
    (`flex-basis: calc(50% - gap)`, `.span-full` override), `.stat-strip`
    grid, `.stat-tile`, `.today-callout`, `table.week-table`, `.verdict.*`
    badge classes — ported from the approved mockup's CSS, adapted to this
    file's existing token variables (`PRIMARY`/`GOOD`/`WARNING`/`CRITICAL`/
    `NEUTRAL` — unchanged, no new colors introduced).
  - New `_render_plan_section_html(plan_section: dict | None) -> str`
    (returns `""` when `None`).
- **`src/local_fitness/agent/tools.py`**: `generate_brief_report` gains, after
  the existing chart-rendering loop and before the PDF render call:
  1. Resolve the active plan + build `plan_section` (or `None`) anchored to
     `target_date`, mirroring `get_training_plan_progress`'s existing
     plan-loading pattern.
  2. If `plan_section is not None`, call `plan_coach.generate_coaching_line`
     (try/except broad `Exception` → `plan_coach.fallback_coaching_line`)
     and attach the result as `plan_section["today"]["coaching_line"]`.
  3. Pass `plan_section` into `visuals.render_brief_pdf`.
- **`tests/test_visuals.py`**, **`tests/test_tools.py`**, new
  **`tests/test_plan_coach.py`**: see Testing below.
- Docs: `CLAUDE.md` "What's already wired" bullet update, `CHANGELOG.md` +
  `pyproject.toml` version bump (functional change), `devlog/` entry.

## API Surface

```python
# agent/plan_coach.py
def build_prompt(
    profile: CoachProfile,
    today_workout: dict,          # {type, distance_mi, pace_min_per_mi, description}
    last_7_days: list[dict],      # same shape as plan_section["last_7_days"]
    adherence_pct: int,
    days_to_race: int | None,
    goal_type: str,
) -> tuple[str, str]:              # (system_prompt, user_prompt)
    ...

async def generate_coaching_line(
    profile: CoachProfile,
    today_workout: dict,
    last_7_days: list[dict],
    adherence_pct: int,
    days_to_race: int | None,
    goal_type: str,
    *,
    model: str = briefing.DEFAULT_MODEL,
    timeout: float = 30.0,
) -> str:
    """Raises RuntimeError/TimeoutError/anything the Agent SDK raises on
    failure — caller (tools.py) is responsible for the fallback."""
    ...

def fallback_coaching_line(
    today_workout: dict,
    last_7_days: list[dict],
    days_to_race: int | None,
    goal_type: str,
) -> str:
    """Pure, deterministic, never raises."""
    ...


# agent/visuals.py
def render_brief_pdf(
    brief: "Brief",
    charts: dict[str, bytes],
    plan_section: dict | None = None,
) -> bytes: ...

# plan_section shape (all distances in miles, pace as "M:SS" strings):
# {
#   "adherence_pct": int,                 # 0-100
#   "goal_type": str,
#   "days_to_race": int | None,
#   "week_planned_mi": float,
#   "week_actual_mi": float,
#   "slips": int,
#   "today": {
#     "type": str, "distance_mi": float | None,
#     "pace_min_per_mi": str | None, "description": str,
#     "coaching_line": str,
#   } | None,
#   "last_7_days": [
#     {"date": str, "type": str, "planned_mi": float | None,
#      "actual_mi": float | None, "verdict": str}, ...
#   ],
# }
```

## Invariants

**Checkable by inspection:**
- No top-level `import claude_agent_sdk` (or `weasyprint`/`matplotlib`) in
  `plan_coach.py` or anywhere reachable from `tools.py`'s module scope —
  deferred inside function bodies only.
- `render_brief_pdf` does no DB access and no network access — pure
  formatting of its three parameters.
- `generate_coaching_line`'s `model` default reads `briefing.DEFAULT_MODEL`
  (an attribute reference), never a duplicated literal string.

**Testable:**
- 2-column layout: N=1,2,3,4,5 takeaways each produce the correct
  `.span-full` placement (only ever the *last* card, only when N is odd).
- N=0 takeaways renders the signals section as empty (no crash) — real
  edge case, however rare.
- `plan_section=None` → rendered HTML contains no "Training Plan" heading
  string at all.
- Adherence/mileage/slips numbers match hand-computed values against a
  seeded plan+activities fixture (pin exact values, not just "a number").
- Verdict → badge class/label mapping is exhaustive over
  `{done, partial, missed, compliant, pending}`.
- `generate_coaching_line` raising (mocked) → `generate_brief_report` still
  returns a success payload, and the written PDF's text contains the
  fallback line's distinguishing substring, not a raised exception.
- `fallback_coaching_line` is pure/deterministic: same inputs → byte-
  identical output across two calls.
- No active plan (`get_active_plan()` → `None`) → `plan_section is None`
  path is exercised end-to-end through `generate_brief_report`.

## Testing strategy

- `tests/test_visuals.py`: pure `render_brief_pdf`/`_render_plan_section_html`
  tests, no network/DB — string-membership + pdfplumber structural
  assertions (page count, image count) exactly like the existing tests in
  this file.
- `tests/test_plan_coach.py`: `fallback_coaching_line` fully tested (pure
  function, real assertions on exact output for a few representative
  verdict/adherence combinations). `generate_coaching_line`'s Agent SDK call
  is mocked at the SDK boundary (this is the "pure I/O glue" this repo's
  own testing bar explicitly excuses from deeper coverage) — but the
  **dispatch** (correct model, correct prompt content, correct timeout) is
  asserted against the mock's call args, which is real assertable behavior,
  not a mock echoing its own canned value.
- `tests/test_tools.py`: extend `generate_brief_report`'s existing tests
  with the plan-data-present / plan-data-absent / coaching-line-failure-
  falls-back paths, following this file's existing seeded-DB fixture
  pattern.

## Failure modes

- Missing/expired Claude credential, network error, timeout → deterministic
  fallback line, PDF still generates (non-fatal, unlike the daily brief
  job's harder failure mode — this is a bonus section, not the main brief).
- No active plan → section omitted, rest of the report unaffected.
- Active plan but zero workout data in the trailing-7-day window (e.g.
  regenerating a brief from before the plan existed) → section omitted.
- Malformed/partial plan data from `plans.py` (should not happen given
  `build_plan_detail`'s existing contract, but this file's whole pattern is
  "never let one section's problem fail the report") → wrapped in the same
  broad try/except as the chart-rendering loop; logs a warning, omits the
  section rather than failing the tool call.

## Explicitly out of scope

- Any change to the live daily brief (`fitness brief` / `generate_and_save`)
  or the V2 planner/generator pipeline — this is the PDF-only render path.
- Caching/memoizing the coaching line across renders (Nate confirmed
  regenerate-every-time is fine for this personal, single-user tool).
- A new model-selection env var for this call — it follows
  `briefing.DEFAULT_MODEL` unconditionally.
- Changing the existing color palette/tokens — this is a layout and
  information-density pass, not a rebrand.
- Anything in the web UI's brief view (React) — PDF only, per Nate's
  original ask and the attached screenshot.

## Verification

- `uv run pytest -x` green, coverage gate maintained.
- `uv run ruff check .` clean.
- Manual smoke test: regenerate a real PDF against the production DB (both
  with and without an active plan) and open it — confirm the 2-column
  layout, the Training Plan section's data matches `get_training_plan_progress`
  for the same date, and the coaching line reads sensibly (or the fallback
  fires cleanly if credentials are unavailable at test time).
