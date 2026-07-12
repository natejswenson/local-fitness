---
ticket: "N/A"
title: "Deterministic intelligence, MCP payload quality, agent UX, and layer-separation hardening"
date: "2026-07-12"
source: "design"
---

# Deterministic intelligence, MCP payload quality, agent UX, and layer-separation hardening

## Goal

One efficiency/quality release, **zero new user-facing features**, across four
dimensions of the MCP-only surface:

1. **MCP determinism** — kill redundancy, bound payloads, fix convention gaps
   so common questions resolve in one deterministic call.
2. **Agent UX** — make the guidance the connecting LLM receives match the
   repo's own conventions, and make payloads presentation-ready.
3. **Intelligence layered on deterministic data** — every judgment the code
   already knows how to make (zones, deltas, strengths, gaps) is computed in
   tested Python and attached to the payload; the LLM phrases, never derives.
4. **Layer separation** — every LLM output that enters the system is grounded
   or explicitly documented as ungroundable; every LLM boundary stays typed,
   mocked, and pytest-covered.

## Relationship to prior art

- **Folds in** `2026-07-10-mcp-tool-ux-efficiency-design.md` wholesale. Its
  three fixes (A: inline chart image + `ALL_TOOLS` promotion for
  `generate_chart`; B: `get_today_status` → `assemble_status()` convergence;
  C: `_augment_plan_workout` mile/pace fields) ship in this release exactly
  as specified there, including its enumerated test casualties. That doc
  remains the spec of record for those three items; this doc adds the rest.
- **Does not re-litigate** `2026-07-09-mcp-speed-and-ui-retirement-design.md`
  Part A (DB/connection efficiency) — separate, already-designed axis.
- **Extends** the `2026-06-26-agent-code-separation-design.md` philosophy
  (deterministic planner → typed contract → toolless LLM → advisory
  grounding) from the brief path to the plan-coach path.

## Investigation verdicts (what stays untouched)

Four parallel investigations (MCP surface, agent UX, intelligence layer,
boundary map) confirmed these are healthy — no changes:

- Error envelope (`_err`), payload ordering, snake_case params, ISO dates,
  `run_sql` safety bounds, `_validate_days`.
- `brief_planner.py`, `grounding.py`, `schemas.py`, the V2 toolless boundary
  and its failure-path tests, `fallback_coaching_line`, prompt scorers.
- Coach-voice consistency across chat/brief/MCP-prompt (single divergence:
  plan_coach, fixed in WS3).
- Tool-routing descriptions for plan lifecycle, `get_metric` vs
  `get_metric_trend`, `chart` vs `generate_chart`.
- Coverage config: no LLM module is excluded from the 85% gate; the live SDK
  round-trip is the only intentionally-unmocked seam.

---

## WS1 — Interpretation parity on the analysis tools

**Problem.** The interpretation layer is rich on the brief path
(`status._tsb_interpretation`, arrows, `brief_planner` signals) and absent on
the ad-hoc analysis tools, which return bare floats plus *static legend
strings the LLM applies by hand* (`correlate`'s "|r| < 0.2 weak…" legend;
`training_load_status`'s TSB bands living in its docstring).

**Change.** New pure module `src/local_fitness/agent/interpret.py` — no I/O,
no SDK, stdlib-only — housing the shared classifiers. Existing private
classifiers delegate to it so brief and tools agree by construction:

- `tsb_zone(tsb: float | None) -> str` — extracted from
  `status._tsb_interpretation` (status.py:84); `status.py` delegates.
- `pct_change(now: float | None, then: float | None) -> float | None` — the
  `ctl_pct_change_14d` arithmetic from `brief_planner._compute_signals`
  (brief_planner.py:500-508); brief_planner delegates.
- `trend_direction(slope_per_day: float | None, *, flat_threshold: float) ->
  "rising" | "falling" | "flat" | "no data"`.
- `baseline_position(sd_distance: float | None) -> "elevated" | "normal" |
  "suppressed" | "no data"` (bands: > +1 SD elevated, < −1 SD suppressed).
- `correlation_read(r: float | None) -> {strength: "weak"|"modest"|
  "moderate"|"strong", direction: "positive"|"negative"} | None` — the bands
  already written in `correlate`'s legend string.
- `effect_size(mean_a, mean_b, sd_a, sd_b, n_a, n_b) -> {delta_pct,
  cohens_d, magnitude: "negligible"|"small"|"moderate"|"large"} | None`
  (Cohen's conventional 0.2/0.5/0.8 bands; pooled SD; None when either SD
  is 0/None or either n < 2).
- `sd_position(value, mean, sd) -> {sd_distance: float, direction:
  "above"|"below"} | None` (None when sd is 0/None).

Payload attachments (all additive; raw fields stay):

| Tool | New fields |
|---|---|
| `training_load_status` | `tsb_zone`, `ctl_pct_change_14d`, `ctl_direction` (from `trend_direction` over the 14-day change) |
| `correlate` | `strength`, `direction` (replaces the static legend string — the legend is deleted, the computed read supersedes it) |
| `find_anomalies` | per-row `sd_distance`, `direction` |
| `compare_periods` | `delta_pct`, `cohens_d`, `magnitude` |
| `get_metric_trend` | `slope_direction`, `vs_baseline` (from `baseline_position` over the existing `current_vs_baseline_sd`) |

`brief_planner._rhr_anomalies` also attaches `sd_distance`/`direction` to
`BriefContext.anomalies` entries via the same helper (additive keys on an
existing dict — `Brief`/`Takeaway` schemas unchanged; `BriefContext.anomalies`
is a free-form list). Its above-only bias (`> 2*sd`) is *kept* — widening to
low-side anomalies would change brief triggering behavior, out of scope.

**Float rounding at the payload boundary.** The analysis tools emit
full-precision floats (`0.4285714285714286`). Round at the `_text({...})`
boundary: correlation/slope/cohens_d → 3 dp; means/SDs/deltas/recovery-days
→ 2 dp; pct fields → 1 dp (matching `status.py:127`). Applies to
`get_metric_trend`, `compare_periods`, `correlate`, `recovery_pattern`.
No test currently pins raw precision — safe.

**`flat_threshold` note.** `trend_direction`'s flat band is the one genuinely
new tuning knob in WS1. Default: `abs(slope_per_day) * window_days <
0.5 * baseline_sd` reads as flat when baseline SD is available; fall back to
a per-call `flat_threshold` argument the tool computes from the metric's
baseline SD, and treat slope as rising/falling only beyond it. Implementer
picks the exact constant; the invariant is that it's a named constant in
`interpret.py` with a test pinning the boundary on both sides.

---

## WS2 — Plan-tool payload quality

**2a. Extract the weekly rollup into `plans.py` (share with the PDF).**
`_build_plan_section` (tools.py:1746-1842) computes `week_planned_mi`,
`week_actual_mi`, `slips` — only for the PDF. Extract a pure function:

```python
plans.weekly_rollup(workouts: list[dict], target_date: date) -> dict
# -> {week_planned_mi, week_actual_mi, slips, week_adherence_pct}
```

taking already-graded workout dicts (no I/O). `_build_plan_section` calls it
(its per-day verdict-conditional `actual_mi` display suppression stays where
it is — that's PDF display logic, not aggregation).
`get_training_plan_progress` and `get_training_plan_status` both attach the
rollup as a `this_week` object. This is the deterministic answer to "how's my
week going" that today forces the agent to re-aggregate a 100-row list.

**2b. Goal-gap fields.** `plans.goal_gap(predicted_finish_s, target_time_s)
-> {gap_seconds, gap_pct, on_pace: bool} | None` (None when either input is
None). Attached to `get_training_plan_progress` (top level, next to the
existing raw seconds) and to `build_plan_status`'s payload so
`get_training_plan_status` carries it too. "Am I trending toward my goal" is
currently a raw two-number diff left to the model.

**2c. Window `get_training_plan_progress`.** Today it returns every
prescribed day (~112 entries for a 16-week plan) and its description steers
the LLM to it for "how is my plan going." Change: the `workouts` list
defaults to a window of **trailing 14 days + upcoming 7** (relative to the
grading frontier used today); a new optional boolean arg `full` (default
false) returns the complete list. Full-plan rollups (`adherence_pct`,
`days_to_race`, `predicted_finish_seconds`, `goal_gap`, `this_week`) are
computed over the whole plan regardless of the window. Description rewritten
to say so and to name `get_training_plan_status` for "just today."
Behavior change by design: an agent that wants the whole plan (e.g. "show my
plan through today" for a plan older than 14 days) must pass `full=true` —
the description must state this explicitly since CLAUDE.md steers that
question here. Test pin `tests/test_plan_tools.py:199` (1-workout fixture)
survives because today is always in-window; add a long-plan truncation test.

**2d. Formatted time fields.** Add `units.format_hms(seconds) -> "H:MM:SS"`
(or reuse/extend `format_duration` if it already emits that shape — the
implementer verifies before adding a sibling).
Attach: `target_duration_formatted` per workout (when `target_duration_sec`
present), and top-level `predicted_finish_formatted` / `target_time_formatted`
on `get_training_plan_progress`, plus the same two on
`get_training_plan_status` when present. Raw seconds stay. A wrong hand-built
h:mm:ss for a race-time answer is a plausible, embarrassing agent error.

**2e. Fix C from the 07-10 doc** (`_augment_plan_workout` mile/pace fields
with the exact `display_units()` gating split) ships as specified there. The
07-10 doc's claim that plan tools were "the only" convention gap was wrong;
2f closes the rest.

**2f. Close the remaining miles-convention gaps.** Apply `_augment_workout`
per split in `get_workout_detail` (`tools.py:470-473` — currently `SELECT *`
raw, so the whole-run view is in miles while its splits are meters/sec-per-km)
and per matched workout in `recovery_pattern` (`tools.py:759-763`).

**2g. Aggregate over activity distance in `compare_periods`** *(designated
cut if the quality gate flags scope)*. "How much did I run this week vs
last" has no structured tool and forces `run_sql`. Minimal move mirroring
the existing `training_load` special case (`tools.py:492-497`): accept
`distance_meters` as a metric, sourced from `activities` with **SUM per
period** (plus `sum_mi` convenience via `units.to_miles` under the same
`display_units()` gate as `_augment_workout`). Pace aggregation is
deliberately excluded (duration-weighted mean is a real design problem —
not this pass). `effect_size` fields are omitted for SUM metrics (a
period total has no per-observation SD; the payload carries
`delta_pct` only).

---

## WS3 — Agent UX surface

**3a. plan_coach prompt parity.** `plan_coach.build_prompt`
(plan_coach.py:58-64) omits user notes and the metric-translation block, so a
saved preference ("stop roasting my steps") is honored in chat and brief but
violated in the PDF's coaching line. Change: `build_prompt` gains a
`notes_text: str | None` parameter (pure — caller does the I/O via
`notes.render_for_prompt()`, same pattern as `prompts.py:26-33`) appended as
a notes section, plus the one-paragraph metric-translation reminder from
`system_prompt`. `generate_coaching_line`'s caller in `tools.py` threads the
notes in.

**3b. Chart-rendering guidance reaches the client.** `system_prompt`
(prompts.py:82-100, the exact text delivered as MCP `instructions` and the
`coach` prompt) gains a "Charts" bullet: *when you call `chart`, reproduce
its full output in a fenced code block in the reply, then add the coach
read — never leave it in the collapsed tool call.* The `chart` tool's
description gets a one-line echo of the same rule. This is CLAUDE.md's most
emphatic convention and it currently never reaches the connecting LLM.

**3c. Sleep seconds → formatted.** `_render_status` (mcp_server.py:73)
renders `sleep_seconds` baseline rows as raw integers (`27180`) in the very
table the coach reads first. Format via the existing `units.format_duration`
in `_render_status`, and add `value_formatted` (and `baseline_formatted`)
on the `sleep_seconds` metric row in `status._metric_rows` — symmetric with
how `recent_workouts` already get `duration_formatted`. `get_today_status`
inherits via Fix B's convergence.

**3d. MCP-appropriate error strings.** Two errors point MCP-only users at
CLI commands they cannot run: `training_load_status`'s "pull activities and
run recompute-baselines" (tools.py:605) and `log_manual_workout`'s "run
`fitness baselines`" (tools.py:1161). Reword both to point at the
`sync_garmin_data` MCP tool. `run_sql`'s opaque "query failed: invalid
query" (tools.py:833) stays generic but gains direction: "check table/column
names against the `fitness://schema` resource."

**3e. "Today's read" description disambiguation.** After Fix B, three tools
answer "how am I doing today" (`get_today_status` ≡ `daily_snapshot` ⊂
`get_brief_context`) and two descriptions actively compete. No merges —
`daily_snapshot` is genuinely cheaper. Rewrite descriptions with explicit
"when NOT to use" lines:
- `daily_snapshot` / `get_today_status`: "…no plan/anomalies/candidates —
  use `get_brief_context` for the full read or anything plan-/trend-related."
- `get_brief_context`: "…overkill for a single-metric question — use
  `get_metric`/`get_metric_trend`."

**3f. Fixes A and B from the 07-10 doc** ship as specified there (inline
image block + `ALL_TOOLS` promotion + description rewrite for
`generate_chart`; `assemble_status()` convergence + description rewrite for
`get_today_status`), including that doc's enumerated test rewrites
(`test_smoke.py:44` count 33→34, INV-4, INV-T9, INV-T10 comment,
`test_get_today_status`, `test_tool_call_returns_unwrapped_content`).

---

## WS4 — Layer-separation hardening

**4a. Ground the PDF coaching line.** The coaching line is the one LLM
output entering a user-facing artifact with zero numeric validation. Add
`plan_coach.ground_coaching_line(text: str, plan_section: dict) ->
list[GroundingFlag]` — pure, reusing `grounding`'s numeric-token parser and
nearest-match bands over a pool built from the plan section
(`adherence_pct`, `days_to_race`, today's `distance_mi`/pace, `this_week`
mileage). `generate_brief_report` logs the flags advisorily, exactly as V2
does (`log_grounding` pattern) — never gates, never alters the PDF.
`grounding`'s parser internals (`_parse`, `_nearest`) get public
re-exports (or a small public wrapper) rather than plan_coach importing
underscore-names cross-module.

**4b. Restore the invention-rate signal on the V1 rollback path**
*(second designated cut)*. V1 (`LOCAL_FITNESS_BRIEF_V2=0`) currently loses
grounding entirely because no `BriefContext` is assembled. Change: the V1
branch calls `brief_planner.assemble_brief_context()` **solely to build the
grounding pool** (never for the prompt), then `log_grounding` runs on both
paths. Decouples measurement from generation strategy. If this proves
awkward in implementation (V1 is rollback-only), dropping 4b is acceptable —
it is a measurement nicety, not a correctness fix.

**Explicitly out of scope for WS4** (documented, not forgotten): grounding
for external-client chat (structurally impossible — prose composed outside
the process, per the 06-27 design), the live-eval CI job (deferred by
design), script `--run` I/O glue tests (wraps already-tested composer).

---

## Deferred / out of scope (whole design)

- **Wall-clock vs data-frontier window anchoring** (`date.today()` in read
  tools vs frontier in plan grading). Real inconsistency, but re-anchoring
  is a semantic change with stale-data edge cases deserving its own design.
  The frontier is already visible via `get_brief_context.data_through_date`.
- **Response-envelope standardization** (`{rows,count}` vs bare lists) —
  churn exceeds benefit; divergence noted.
- **Streak/record detection** — borders on a new feature.
- **Pace aggregation in `compare_periods`** — duration-weighted mean needs
  its own thought.
- **Low-side anomaly widening in `_rhr_anomalies`** — would change brief
  triggering.

## API Surface

New module `src/local_fitness/agent/interpret.py` (all pure):
- `tsb_zone(tsb: float | None) -> str`
- `pct_change(now: float | None, then: float | None) -> float | None`
- `trend_direction(slope_per_day, *, flat_threshold: float) -> str`
- `baseline_position(sd_distance: float | None) -> str`
- `correlation_read(r: float | None) -> dict | None`
- `effect_size(mean_a, mean_b, sd_a, sd_b, n_a, n_b) -> dict | None`
- `sd_position(value, mean, sd) -> dict | None`

New pure functions in existing modules:
- `plans.weekly_rollup(workouts: list[dict], target_date: date) -> dict`
- `plans.goal_gap(predicted_finish_s: int | None, target_time_s: int | None) -> dict | None`
- `units.format_hms(seconds: int | None) -> str | None` (if `format_duration` doesn't already cover H:MM:SS)
- `plan_coach.ground_coaching_line(text: str, plan_section: dict) -> list[GroundingFlag]`
- `plan_coach.build_prompt(..., notes_text: str | None = None)` — signature extension, still pure.

Tool payload changes (all additive unless noted):
- `training_load_status`: + `tsb_zone`, `ctl_pct_change_14d`, `ctl_direction`; floats rounded.
- `correlate`: + `strength`, `direction`; **legend string removed**; `pearson_r` rounded to 3 dp.
- `find_anomalies`: per-row + `sd_distance`, `direction`.
- `compare_periods`: + `delta_pct`, `cohens_d`, `magnitude`; floats rounded; (2g) `distance_meters` accepted with SUM semantics + `sum_mi`.
- `get_metric_trend`: + `slope_direction`, `vs_baseline`; floats rounded.
- `recovery_pattern`: matched workouts pass through `_augment_workout`; floats rounded.
- `get_workout_detail`: splits pass through `_augment_workout`.
- `get_training_plan_progress`: + `this_week`, `goal_gap`, `predicted_finish_formatted`, `target_time_formatted`, per-workout `target_duration_formatted` (+ Fix C mile/pace fields); `workouts` **windowed by default** (trailing 14 + upcoming 7; `full=true` restores complete list) — the one non-additive change.
- `get_training_plan_status`: + `this_week`, `goal_gap`, formatted time fields (+ Fix C fields on `today`/`last_graded`).
- `BriefContext.anomalies` entries: + `sd_distance`, `direction` (schema class unchanged — free-form list).
- Plus the 07-10 doc's API surface (Fix A/B/C) verbatim.

Prompt/instruction changes:
- `system_prompt`: + Charts bullet (delivered via MCP `instructions` + `coach` prompt).
- `plan_coach.build_prompt`: + notes section + metric-translation block.
- Tool description rewrites: `chart`, `daily_snapshot`, `get_today_status`, `get_brief_context`, `get_training_plan_progress`, `generate_chart` (per 07-10 doc), error strings per 3d.

## Invariants

**Checkable by inspection:**
- `interpret.py` imports nothing outside stdlib (no db, no SDK, no schemas).
- `status.py` and `brief_planner.py` delegate to `interpret.py` for
  TSB zone and pct-change — no duplicated band constants anywhere.
- `plans.weekly_rollup` / `plans.goal_gap` are I/O-free; `_build_plan_section`
  calls `weekly_rollup` (no duplicated aggregation).
- `plan_coach.build_prompt` remains I/O-free (notes text passed in).
- `plan_coach.ground_coaching_line` never raises on arbitrary text; grounding
  stays advisory (no gate on the PDF path).
- All raw fields kept wherever formatted/interpreted siblings are added.
- V1 branch (if 4b ships) uses `assemble_brief_context` for grounding only —
  the V1 prompt construction is byte-identical to today.
- All invariants from the 07-10 doc (Fix A/B/C) hold.

**Testable:**
- Every `interpret.py` classifier: band boundaries pinned on both sides,
  None/zero/missing handled (no exceptions on degenerate input).
- `training_load_status` payload contains `tsb_zone` equal to
  `interpret.tsb_zone` of its own `tsb` value (agreement by construction).
- `correlate` payload has computed `strength`/`direction` and no legend
  string; `find_anomalies` rows carry `sd_distance` matching
  `(value-mean)/sd` to 2 dp; `compare_periods` carries `cohens_d`/`magnitude`
  consistent with `effect_size`.
- Rounded floats: no analysis-tool payload float exceeds its dp budget.
- `weekly_rollup` over a fixture week equals the values the PDF section
  displays for the same fixture (shared-function agreement).
- `goal_gap` sign convention pinned (positive gap = slower than goal).
- `get_training_plan_progress` default call on a long-plan fixture returns
  only in-window workouts; `full=true` returns all; rollups identical in
  both modes.
- `format_hms(6420) == "1:47:00"` (and the None case).
- `build_prompt` output contains the notes text when provided and the
  metric-translation block always; `test_plan_coach` asserts a note string
  appears in the assembled system prompt.
- `system_prompt` contains the Charts bullet (scorer stays green —
  `scripts/score_prompt.py` must pass on the modified prompt).
- Error strings: `training_load_status` empty-DB error mentions
  `sync_garmin_data` and not "recompute-baselines" CLI wording.
- `ground_coaching_line` flags an invented adherence number and does not
  flag faithful citations (mirror `test_grounding.py` patterns).
- V1 path (if 4b ships): `log_grounding` called with a real pool; V1 prompt
  unchanged (assert prompt equality against a pre-change snapshot fixture).
- `get_workout_detail` splits and `recovery_pattern` workouts carry
  `distance_mi`/`pace_min_per_mi` in miles mode, absent distance keys in km
  mode (same gate as `_augment_workout`).
- Plus all testable invariants from the 07-10 doc.

## Testing strategy

- `tests/test_interpret.py` (new): exhaustive band/edge coverage for every
  classifier — this module should be trivially 100%.
- `tests/test_plans.py`: `weekly_rollup`, `goal_gap` (empty, single, flat,
  missing-data, boundary weeks).
- `tests/test_tools.py` / `test_plan_tools.py` / `test_mcp_server.py`:
  payload-attachment assertions per tool; windowing tests; the 07-10 doc's
  enumerated rewrites; description-string assertions (chart bullet, "when
  NOT to use" lines, error rewording).
- `tests/test_plan_coach.py`: notes-in-prompt, translation block,
  `ground_coaching_line` faithful/invented cases.
- `tests/test_status.py`: `value_formatted` on sleep rows; delegation to
  `interpret` (same zone string).
- `tests/test_briefing.py`: 4b — V1 grounding-only context assembly (mocked
  SDK, assert `log_grounding` fires and prompt unchanged).
- Prompt-change verification per repo policy: `scripts/score_prompt.py`
  green on the modified `system_prompt`; the ab_brief harness is known-flaky
  — do NOT rely on it; use the scorer + the unit assertions above.
- Perf gate: none of these paths add `db.connect()` opens in the
  benchmarked hot paths (`weekly_rollup` extraction must not add an open in
  `_build_plan_section`; `assemble_brief_context` reuse in 4b is off the
  benchmarked path since briefs aren't benchmarked with V1).
- Full `uv run pytest -x` + `ruff` locally before the PR; coverage ≥ 85%.

## Failure modes / edge cases

- Empty/fresh DB: every classifier returns its "no data" value; payload
  attachments are None/absent, never exceptions (same guarantee
  `assemble_status` documents).
- Degenerate stats: `sd == 0` → `sd_position`/`effect_size` return None
  (no ZeroDivisionError); `n < 2` → `effect_size` None; `r` exactly on a
  band boundary → pinned by test.
- Plan with no goal time or no best-effort projection → `goal_gap` None,
  formatted fields absent.
- Windowing when the plan is entirely in the past (race done) or entirely
  future (starts next week): window clamps to plan bounds; empty `workouts`
  list is legal with rollups still present.
- `ground_coaching_line` on prose with no numbers → empty flag list.
- Notes file missing → `notes_text=None` → prompt identical to today plus
  the translation block.
- `chart`/`generate_chart` behavior on render failure unchanged (`_err`
  before the new code paths).

## Release mechanics

- Branch `feature/deterministic-intelligence-ux` → PR → `dev` (squash,
  auto-merge on green `validate`).
- Version bump 0.21.0 → 0.22.0 + CHANGELOG entry (code + prompt changes ⇒
  bump per release policy). `uv.lock` picks up the version sync.
- CLAUDE.md updates in the same PR: tool-count note (33→34 in `ALL_TOOLS`),
  `get_training_plan_progress` windowing note in the Q&A section (the
  "show my plan through today" guidance must mention `full=true`), and the
  07-10 doc's Fix A/B notes.
- Devlog entry for the release.
- No promotion to `main` (dev-only until Nate says release).
