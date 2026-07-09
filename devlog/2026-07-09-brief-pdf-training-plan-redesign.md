# Brief PDF redesign: 2-column signals + Training Plan section

**2026-07-09 · v0.20.0**

Nate reviewed the brief PDF shipped in #88/#91 and wasn't happy with it
visually — the takeaway cards stacked in one column, and there was no
dedicated view of the training plan. Round-tripped through `/design` with
an approved HTML/CSS mockup before any code landed; the whole ask was to
make the report "extremely clean and crisp — something I enjoy visually
looking at that has good data."

## What changed

- **Signal cards → 2-column flexbox grid.** Robust to the real, variable
  takeaway count (`brief_planner` triggers 1-N cards per day, never a
  fixed 4) — an odd-count last card spans the full width instead of
  leaving a gap. Flexbox over CSS Grid: the longer-established, more
  reliably-supported layout model in WeasyPrint.
- **New Training Plan section**: a stat strip (adherence %, days-to-race,
  this week's planned/actual mileage, a slip count), a "Today" callout
  with the prescribed workout and a coaching line, and a last-7-days table
  graded against prescription (done/partial/missed/rest/scheduled).
  Computed live from `plans.py` at render time, keyed to the brief's own
  date — the `Brief`/`Takeaway` schema gets zero new fields.
- **New `agent/plan_coach.py` module**: the coaching line is a real Claude
  Agent SDK call (toolless, single-shot, same `briefing.DEFAULT_MODEL` the
  production brief uses), called fresh on every render — confirmed with
  Nate twice, no caching, despite the repeat-cost/latency tradeoff — with
  a deterministic fallback template if the call fails for any reason.

## Bugs caught during implementation (not by the design doc — by actually
running it)

- **A real circular import.** `briefing.py` imports `tools.py` at module
  scope (as `agent_tools`); `tools.py` now imports `plan_coach.py`. The
  first draft had `plan_coach.py` import `briefing` at module scope too
  (for `DEFAULT_MODEL`) — `tools -> plan_coach -> briefing -> tools`. Fixed
  by resolving `briefing.DEFAULT_MODEL` inside the function body instead
  of as a signature default.
- **A decorator-placement bug from my own edit.** Inserting
  `_build_plan_section` between `@tool(...)` and
  `async def generate_brief_report` silently decorated the wrong function
  — caught immediately by the existing test suite (`AttributeError:
  'function' object has no attribute 'handler'`), not by review.
- **A wrong function signature, caught by nothing until a real DB-backed
  test.** `plans.build_plan_detail`'s 4th positional argument is
  `best_effort` (a Riegel-projection dict), not a `today` date — an
  assumption carried over from `build_plan_status`'s (different) shape.
  Passing a date string there would raise `AttributeError` the moment a
  plan was active; every existing test fixture had no active plan, so it
  slipped past the whole suite until a new fixture actually seeded one.
  It turns out `build_plan_detail` has no "as of" date concept at all —
  `grade_workout`'s pending-holdout compares each workout's own date
  against the real data frontier, never a hypothetical past perspective.
- **`days_to_race` isn't a `build_plan_detail` field.** It's computed
  on-the-fly in `build_plan_status` from `race_date` and an explicit
  `today` argument — `build_plan_detail` never touches it. Fixed by
  computing it the same way, anchored to the brief's own date.
- **Two CSS-comment false positives in my own tests.** Explanatory CSS
  comments (`/* Section 2: Training Plan (new)... */`, `.span-full,
  computed in Python`) are always present in every render, so a loose
  substring search for "Training Plan" or "span-full" matched them even
  when the actual rendered section/class was correctly absent. Fixed by
  asserting exact rendered markup instead of bare phrases.
- **N=0 takeaways isn't a real edge case.** `Brief.takeaways` carries a
  pydantic min-length-1 constraint — a zero-takeaway `Brief` can't be
  constructed at all. Dropped the planned test for it rather than force
  one against an unreachable state.

## Verified

888 → 932 tests passing, 93.4% coverage, ruff clean. A real smoke test
against the production DB and today's actual saved brief produced a
genuine Claude-generated coaching line in the established hardass voice,
using live numbers (83% adherence, 71 days to race, 9:39 pace) consistent
with the existing takeaway card's own figures — not the fallback path,
though the fallback is unit-tested separately.

## Explicitly out of scope

- The web UI's brief view (React) — PDF only, per the original ask.
- A model-selection env var for the coaching line — it follows
  `briefing.DEFAULT_MODEL` unconditionally; no separate knob.
- Any change to the color palette — layout/density pass, not a rebrand.
