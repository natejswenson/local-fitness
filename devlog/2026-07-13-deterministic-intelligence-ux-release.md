# Deterministic intelligence, MCP payload quality, agent UX, and layer-separation hardening

**2026-07-13**

One efficiency/quality release across the entire MCP-only surface, zero new
user-facing features. Round-tripped through `/design`
(`docs/plans/2026-07-12-deterministic-intelligence-and-ux-design.md`),
quality-gated 6 rounds (1–3 batched, then 4, 5, and a final minor-polish PASS)
before any code landed, then implemented as four workstreams on
`feature/deterministic-intelligence-ux`.

## What shipped (WS1–WS4, one branch)

- **WS1 — interpretation parity on the analysis tools.** New pure module
  `agent/interpret.py` (stdlib-only, no I/O, no SDK) houses every classifier
  the brief path already computed in tested Python but the ad-hoc MCP tools
  left to the connecting LLM: `tsb_zone`, `pct_change`, `trend_direction`,
  `delta_direction`, `baseline_position`, `correlation_read`, `effect_size`,
  `sd_position`. `status.py` and `brief_planner.py` now delegate to it instead
  of keeping parallel copies, and `training_load_status`/`correlate`/
  `find_anomalies`/`compare_periods`/`get_metric_trend` attach the computed
  reads to their payloads (`tsb_zone`, `strength`/`direction`, `sd_distance`,
  `magnitude`, `slope_direction`, …), replacing static legend strings the
  model applied by hand. The 14-day CTL "then" lookup that both the brief
  signal and `training_load_status` need is now one shared function
  (`brief_planner.ctl_at_or_before`) instead of two independent derivations
  that could silently disagree on gappy baselines. Floats are rounded at the
  payload boundary (3/2/1 dp by field class), skipping `None` rather than
  raising.
- **WS2 — plan-tool payload quality.** `tools.weekly_rollup` (pure, no I/O)
  is the one place the trailing-7-day window/convert/suppress logic for
  `this_week` mileage lives — shared verbatim between the PDF's Training Plan
  section and `get_training_plan_progress`'s new `this_week` field, so the
  two agree by construction. `plans.goal_gap` adds gap-seconds/gap-pct/
  on-pace against the Riegel projection. `get_training_plan_progress`'s
  `workouts` list is now windowed by default (trailing 14 + upcoming 7 days,
  anchored to the data frontier so today never falls out of window even on a
  stale sync) — `full=true` restores the complete list. Both plan tools gain
  formatted time fields via the existing `units.format_duration` (no new
  formatter needed — it already covers the shape). `get_workout_detail`
  splits and `recovery_pattern` matched workouts now carry mile/pace fields
  under the same `display_units()` gate as the rest of the API, rounded out
  with `compare_periods` accepting `distance_meters` as a SUM metric for
  "how much did I run this week vs. last."
- **WS3 — agent UX surface.** `plan_coach.build_prompt`/
  `generate_coaching_line` now take the same user-notes + metric-translation
  context chat and the brief already get, closing the one place a saved
  preference was silently ignored (the PDF coaching line). `system_prompt`
  gains an explicit Charts bullet — the repo's most emphatic, most-violated
  convention, and it had never actually reached the connecting LLM. Sleep
  renders as `"7h 33m"` everywhere via a new `units.format_hm` (not
  `format_duration`'s `H:MM:SS` shape, which is wrong for sleep) — the status
  snapshot's sleep row now matches the brief's grounding pool by construction.
  Three MCP error strings that pointed at CLI commands MCP-only users can't
  run are reworded honestly. Competing tool descriptions
  (`daily_snapshot`/`get_today_status`/`get_brief_context`) get explicit
  "when NOT to use" lines. Folded in the 07-10 doc's Fix A
  (`generate_chart` → inline MCP image content block + `ALL_TOOLS`
  promotion, reachable over `/mcp/` for the first time) and Fix B
  (`get_today_status` converges on `assemble_status()`, same path as
  `daily_snapshot`).
- **WS4 — layer-separation hardening.** `plan_coach.ground_coaching_line`
  checks the PDF's coaching line — the one LLM output entering a
  user-facing artifact with zero numeric validation — against a pool built
  from the plan section, logged advisorily next to the existing V2 grounding
  signal, never gating the PDF. The V1 brief-generation rollback path
  (`LOCAL_FITNESS_BRIEF_V2=0`) now also assembles a grounding pool solely to
  restore the invention-rate measurement it silently lost when no
  `BriefContext` was built — wrapped in `try/except Exception` so the
  rollback path itself can't be broken by the very planner bug that might
  have motivated flipping the flag in the first place.

## Folded-in from the 07-10 doc

`2026-07-10-mcp-tool-ux-efficiency-design.md` shipped wholesale as part of
this release rather than separately: Fix A (`generate_chart` inline image +
`ALL_TOOLS`), Fix B (`get_today_status` convergence), and Fix C
(`_augment_plan_workout` mile/pace fields on plan payloads). One explicit
supersession: that doc's "`_build_plan_section` is unchanged" invariant is
superseded by WS2a — it now calls `weekly_rollup`, though it still doesn't
call `_augment_plan_workout` directly.

## Key decisions along the way

- **Reversed a prior verdict.** The 07-10 doc cleared the analysis tools as
  "fine as raw numeric payloads." The interpretation-parity investigation
  behind WS1 found the opposite — the same class of derivation error the
  06-27 agent/code-separation work removed from the brief path was still
  present on the ad-hoc path.
- **Two designated cuts, both shipped anyway** after the quality gate
  confirmed they fit: `compare_periods`' distance-SUM aggregation (2g — the
  closest thing to a new capability in a zero-new-features release) and the
  V1 rollback grounding restoration (4b — a measurement nicety, not a
  correctness fix).
- **Corrected a phantom field.** An earlier design draft (and CLAUDE.md
  itself) referenced `get_brief_context`'s `data_through_date` as the
  brief-staleness signal; no such field exists anywhere in `BriefContext`.
  Fixed in this PR's CLAUDE.md pass — the actual signal is comparing the
  brief's own `date` against `db.last_known_daily_date()`.
- **CLAUDE.md updated in the same commit**: rewrote the stdio-only-tools
  paragraph (Fix A structurally falsified it), added the
  `get_training_plan_progress` windowing/`full=true` note, corrected the
  phantom-field paragraph, and added a note on `agent/interpret.py`'s
  deterministic-interpretation contract.

## Verified

`DYLD_LIBRARY_PATH=/opt/homebrew/lib uv run pytest -x` green, `uv run ruff
check .` clean. No promotion to `main` — this lands on `dev` per the usual
flow; `main` stays a deliberate, Nate-triggered snapshot.
