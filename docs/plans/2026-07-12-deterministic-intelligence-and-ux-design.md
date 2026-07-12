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
  remains the spec of record for those three items; this doc adds the rest —
  with **one explicit supersession**: the 07-10 doc's checkable invariant
  about `_build_plan_section` is superseded by WS2a in BOTH of its clauses —
  the "`_build_plan_section` is unchanged" claim (it now calls the new
  module-level `weekly_rollup` in `agent/tools.py`) AND its inline
  "…and verdict-suppression logic" clause (the suppression, along with the
  windowing and per-day `to_miles` conversion, moves *into* `weekly_rollup`
  — see 2a). What still holds: `_build_plan_section` does NOT call
  `_augment_plan_workout`; it keeps only the `today`-payload conversion
  (tools.py:1813-1818) and pace-string formatting — per-day
  window/convert/suppress lives solely in `weekly_rollup`.
- **Deliberately reverses** one 07-10 verdict: that doc cleared the analysis
  tools (`compare_periods`, `correlate`, `find_anomalies`, …) as "fine as
  raw numeric payloads." The interpretation-parity investigation behind WS1
  found the opposite: the same judgments the brief path makes in tested
  Python (zones, deltas, effect reads) are left to the LLM on the ad-hoc
  path, and that asymmetry is exactly the class of derivation error this
  release exists to remove.
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
`training_load_status`'s TSB bands living in its tool description).

**Change.** New pure module `src/local_fitness/agent/interpret.py` — no I/O,
no SDK, stdlib-only — housing the shared classifiers. Existing private
classifiers delegate to it so brief and tools agree by construction:

- `tsb_zone(tsb: float | None) -> str` — extracted from
  `status._tsb_interpretation` (status.py:84); `status.py` delegates.
- `pct_change(now: float | None, then: float | None) -> float | None` — the
  `ctl_pct_change_14d` arithmetic from `brief_planner._compute_signals`
  (brief_planner.py:500-508); brief_planner delegates. `then == 0` or
  either input `None` → `None` (matching the source's truthiness guard at
  brief_planner.py:507 — a zero baseline has no defined % change). The
  helper returns the **unrounded** float; rounding stays at the
  boundaries — `brief_planner` keeps its `round(..., 1)` on the signal
  (brief_planner.py:508), and `training_load_status` rounds at the payload
  boundary (the 1 dp pct budget below).
- `trend_direction(slope_per_day: float | None, *, flat_threshold: float) ->
  "rising" | "falling" | "flat" | "no data"` — returns `"no data"` **iff**
  `slope_per_day is None`; `"flat"` when `abs(slope_per_day) <=
  flat_threshold` (inclusive `<=` — so a zero threshold from a
  constant-series SD still classifies slope 0 as flat, no side-door);
  sign otherwise. The None mapping is the *caller's* job — see the
  `flat_threshold` note below for how `get_metric_trend` supplies both
  arguments.
- `delta_direction(pct_change: float | None, *, flat_pct: float = 2.0) ->
  "rising" | "falling" | "flat" | "no data"` — the classifier for scalar
  %-delta fields that have no series or slope behind them (routing those
  through `trend_direction` would be shape-incoherent): `None` →
  `"no data"`; `abs(pct_change) <= flat_pct` → `"flat"` (inclusive `<=`);
  sign otherwise. `2.0` is a named constant in `interpret.py`, boundary
  pinned on both sides.
- `baseline_position(sd_distance: float | None) -> "elevated" | "normal" |
  "suppressed" | "no data"` (bands: > +1 SD elevated, < −1 SD suppressed;
  `None` → `"no data"`). The boundary side is explicit: exactly ±1.0 →
  `"normal"` — the bands are **deliberately strict** (`> +1` / `< −1`),
  unlike `correlation_read`/`effect_size`'s inclusive lower bounds; pinned
  on both sides by test.
- `correlation_read(r: float | None) -> {strength: "weak"|"modest"|
  "moderate"|"strong", direction: "positive"|"negative"} | None` — the bands
  already written in `correlate`'s legend string (tools.py:679: 0.2/0.4/0.6
  thresholds), **lower-bound inclusive** at each named threshold:
  `|r| >= 0.2` modest, `>= 0.4` moderate, `>= 0.6` strong, below 0.2 weak.
  Direction at `r == 0.0` is `"positive"` via `>=` (consistent with
  `sd_position`'s rule). `r is None` → `None` (no strength/direction). All
  band boundaries and the `r == 0.0` direction pinned by test.
- `effect_size(mean_a, mean_b, sd_a, sd_b, n_a, n_b) -> {delta_pct,
  cohens_d, magnitude: "negligible"|"small"|"moderate"|"large"} | None`
  (Cohen's conventional bands, **lower-bound inclusive**: `|d| >= 0.2`
  small, `>= 0.5` moderate, `>= 0.8` large, below 0.2 negligible; pooled
  SD). Degradation is **per-field, not whole-function** (whole-`None` only
  when ALL inputs are None): `delta_pct` needs only the two means — it is
  `None` only when `mean_b` is 0/None (division guard) or `mean_a` is
  None, and is otherwise computed even when the SDs/ns can't support a d;
  `cohens_d`/`magnitude` are `None` when either SD is 0/None or either
  `n < 2`. The realistic case the per-field rule protects: comparing two
  1-day periods (`compare_periods`'s per-period sample SD uses a
  `max(len - 1, 1)` denominator, tools.py:509, so a single-sample period
  yields sd 0) must still yield `delta_pct` — the very field this
  workstream adds — with `cohens_d`/`magnitude` None.
- `sd_position(value, mean, sd) -> {sd_distance: float, direction:
  "above"|"below"} | None` (None when sd is 0/None). Direction at a delta
  of exactly 0 is `"above"` via `>=` (`value >= mean` → `"above"`) —
  effectively unreachable in sane calls (`find_anomalies`'s SQL requires
  `ABS(value − mean) > sd * threshold`, tools.py:555; `_rhr_anomalies`
  requires `> 2*sd` — though a negative `sd_threshold`, whose sign is
  unvalidated at tools.py:543, would let a zero delta through
  `find_anomalies`' filter), but defined and pinned by test regardless so
  `test_interpret.py`'s 100%-coverage promise holds.

Payload attachments (all additive; raw fields stay):

| Tool | New fields |
|---|---|
| `training_load_status` | `tsb_zone`, `ctl_pct_change_14d`, `ctl_direction` (from `delta_direction` over `ctl_pct_change_14d` — a scalar % delta, not a slope, so it does **not** route through `trend_direction`) |
| `correlate` | `strength`, `direction` (replaces the static legend string — the legend is deleted, the computed read supersedes it) |
| `find_anomalies` | per-row `sd_distance`, `direction` |
| `compare_periods` | `delta_pct`, `cohens_d`, `magnitude` |
| `get_metric_trend` | `slope_direction`, `vs_baseline` (from `baseline_position` over the existing `current_vs_baseline_sd`). `vs_baseline` is **always attached**, never conditionally omitted: only `rhr`/`sleep_seconds` carry baseline SDs (`current_vs_baseline_sd` is set only for `BASELINE_METRICS` with a non-zero SD, tools.py:240-246, :264-268), so for every other metric — and whenever the SD is absent/None — the key carries `"no data"` (the classifier's `None` mapping), deterministically |

`brief_planner._rhr_anomalies` also attaches `sd_distance`/`direction` to
`BriefContext.anomalies` entries via the same helper (additive keys on an
existing dict — `Brief`/`Takeaway` schemas unchanged; `BriefContext.anomalies`
is a free-form list). Its above-only bias (`> 2*sd`) is *kept* — widening to
low-side anomalies would change brief triggering behavior, out of scope.

**`ctl_pct_change_14d`'s "then" value is shared, not re-derived.** The
arithmetic (`pct_change`) is only half the agreement story — the *data
source* must agree too. The brief signal's "then" CTL comes from a
dedicated no-lookback-floor query (`SELECT ctl FROM baselines WHERE date
<= ? AND ctl IS NOT NULL ORDER BY date DESC LIMIT 1`, anchored
`today − 14d`, brief_planner.py:501-504), while `training_load_status` has
only a 30-day history window in hand (tools.py:597-601) — picking a
"then" point out of that window would diverge from the brief on gappy
baselines (the oldest in-window row is not necessarily the row at-or-before
the anchor). Change: extract the "then" lookup into a **public
module-level function in `brief_planner`** —
`ctl_at_or_before(conn, anchor_date: str) -> float | None` (the same SQL,
no lookback floor) — used by BOTH `_compute_signals` and
`training_load_status`. The tool runs it on its **existing** connection:
one extra indexed point-query, and `training_load_status` is NOT in
tests/test_perf_benchmarks.py's benchmarked set (verified — the benchmarks
cover exactly `assemble_brief_context`, `get_training_plan_progress`,
`get_training_plan_status`, `_build_plan_section`, `daily_snapshot`), so
the extra query is allowed. The import direction already exists:
`tools.py` imports `brief_planner` lazily inside `get_brief_context`
(tools.py:1591-1594 — in-function because `brief_planner → status →
tools` would cycle at module scope); `training_load_status` uses the same
lazy-import pattern. Checkable invariant: the 14-day "then" lookup exists
in exactly ONE place (`brief_planner`), consumed by both paths —
agreement by construction, mirroring the `tsb_zone` treatment.

**Float rounding at the payload boundary.** The analysis tools emit
full-precision floats (`0.4285714285714286`). Round at the `_text({...})`
boundary: correlation/slope/cohens_d → 3 dp; means/SDs/deltas/recovery-days
→ 2 dp; pct fields → 1 dp (matching `status.py:127`). Applies to
`get_metric_trend`, `compare_periods`, `correlate`, `recovery_pattern`, and
`training_load_status` (whose payload entry in the API surface already says
"floats rounded"). No test currently pins raw precision — safe. One rule,
stated once for all analysis tools: **rounding at the `_text` boundary
skips `None` values** — a `None` passes through as `null`, unrounded.
The concrete case: `correlate`'s `pearson_r` is `None` on a
zero-variance denominator (tools.py:675), and `round(None, 3)` raises.

**`flat_threshold` note.** `trend_direction`'s flat band is one of WS1's
new named thresholds — alongside `delta_direction`'s 2.0% flat band and
`baseline_position`'s ±1 SD bands, nothing classifies those today either;
all are named constants in `interpret.py` with pinned boundaries. The flat
band is the one that needs derivation, so it gets the full treatment here. **Correction — an earlier draft keyed the flat band
to the metric's 60-day baseline SD; that's undefined for most metrics.**
Only `rhr` and `sleep_seconds` carry a baseline SD (`BASELINE_METRICS`,
tools.py:41), and `get_metric_trend` joins baselines only for those two
(tools.py:240-246) — every other metric would have had no flat band at all.
Define it instead from the **sample SD of the fetched window series itself**
(the tool already has the series in hand). **Second correction — an earlier
revision expressed the band as fitted change over `window_days`; the units
were wrong.** `get_metric_trend` regresses against the non-null **sample
index**, not calendar days (`xs = list(range(n))`, tools.py:249-255), so the
slope it computes is per-observation and the fitted total change across the
window is `slope * (n - 1)` — NOT `slope * window_days`. Dividing the band
by `window_days` would make it ~10x too strict for sparse metrics (e.g.
`vo2_max` with `n = 4` over `days = 30`), so near-flat noisy series would
classify rising/falling. The band, in the correct units: flat when
`abs(slope * (n - 1)) <= 0.5 * sample_sd` — the fitted total change across
the observed samples stays within half a sample SD. **Inclusive `<=`** is
load-bearing: a constant series has `sample_sd == 0` and slope 0, so the
comparison `abs(0) <= 0` classifies it `"flat"` with no special case (a
strict `<` would make "flat" unreachable there). Naming honesty, stated so
the implementer doesn't "fix" it: the existing payload key `slope_per_day`
is, given the index x-axis, actually per-*observation* — a pre-existing
naming inaccuracy this design documents but does not rename (a rename is a
payload break, out of scope; the new `slope_direction` classification is
what readers should consume). The division of labor is explicit:

- `trend_direction` itself returns `"no data"` **iff** `slope_per_day is
  None` and otherwise applies the inclusive band above — it never sees `n`.
- **`get_metric_trend` owns the `n < 2` → None mapping.** Today the tool
  computes a slope of 0.0 even for a single sample (the least-squares
  denominator is guarded with `or 1e-9`, tools.py:254), so without this
  mapping "no data" would be unreachable — the tool must pass
  `slope_per_day=None` when the window has fewer than 2 values, and only
  pass the computed slope otherwise. The **payload's** `slope_per_day` key
  becomes `null` on that path too (today it carries the guarded 0.0),
  alongside `slope_direction` `"no data"` — a deliberate, enumerated
  payload change, not just an internal argument. This mapping is part of
  the tool change, not an implementation nicety. For `n < 2` the tool also **skips
  the `flat_threshold` computation entirely** — a sample SD with an `n − 1`
  denominator is undefined at `n = 1`, so there is no threshold to compute
  on that path.
- The tool computes the per-call `flat_threshold` from the window's sample
  SD, passing `flat_threshold = (0.5 * sample_sd) / max(n - 1, 1)` so the
  in-function comparison `abs(slope_per_day) <= flat_threshold` is exactly
  the band above (the `max(…, 1)` is belt-and-suspenders — the `n < 2`
  branch above never reaches this expression).

Well-defined for every metric, deterministic, and requires no new data. The
`0.5` is a named constant in `interpret.py` with a test pinning the boundary
on both sides — the `<=` inclusivity is pinned explicitly (a value exactly
at the threshold classifies `"flat"`).

---

## WS2 — Plan-tool payload quality

**2a. Extract the weekly rollup into a shared pure function in
`agent/tools.py` (share with the PDF).**
`_build_plan_section` (tools.py:1746-1842) computes a per-day `last_7_days`
list and, from it, `week_planned_mi`, `week_actual_mi`, `slips` — only for
the PDF. Extract a module-level pure function that returns **both the
per-day list and the totals**:

```python
# agent/tools.py, module level
weekly_rollup(workouts: list[dict], target_date: str) -> dict
# target_date is an ISO date string — the repo convention
# -> {week_planned_mi, week_actual_mi, slips, days: list[dict]}
# each day: {date, verdict, type, planned_mi, actual_mi}
```

`target_date: str` (ISO date), not `date` — the repo convention:
`_build_plan_section` already windows via ISO-*string* comparison
(`window_start <= w["date"] <= target_date`, tools.py:1776-1778), and
string comparison of ISO dates is order-correct, so the shared function
takes the string directly with no parse/round-trip.

taking already-graded workout dicts (no I/O, no connections — direct-import
testable). The `days` list is the canonical per-day shape: the function owns
the trailing-7-day windowing, the per-day `units.to_miles` conversion
(2 dp), and the verdict-conditional `actual_mi` suppression (below), and the
three totals are computed **from that same `days` list** (sum, then 1 dp) —
per-day rows and totals agree by construction, and there is exactly ONE copy
of the window/convert/suppress logic. **`days` is ordered
reverse-chronological (most recent first)** — not an incidental detail:
tests/test_tools.py:1697-1698 pins `last_7_days` reverse-chronological,
`plan_coach.build_prompt` labels the list "most recent first"
(plan_coach.py:77), and `fallback_coaching_line` picks the *first*
non-pending entry as the latest graded day — a forward order would
silently change its verdict phrase. On an empty window `weekly_rollup`
returns `days: []` with zero totals; the **empty → `None` short-circuit
stays in `_build_plan_section`** (consumer behavior — the
tests/test_tools.py:1713-1717 pin is unchanged). **Correction — an earlier draft
returned only the three totals; that quietly forced `_build_plan_section` to
keep its own inline copy of the windowing, conversion, and suppression just
to build its per-day display list (tests/test_tools.py:1701 pins per-day
`actual_mi is None` on a pending day), contradicting the doc's own
"suppression lives inside the shared function / no duplicated aggregation"
claims.** One convention fix rides along: the shared function tests planned
distance with `target_distance_m is not None` (the `units.to_miles`
convention — `to_miles(0) == 0.0`, only `None` propagates) rather than
`_build_plan_section`'s current truthy `if target_m` (tools.py:1786) —
picking up the 07-10 doc's deferred truthy-vs-`None` flag. On real data
nothing changes (a 0-distance prescribed target doesn't occur; rest days
carry `None`); the difference is pinned with a test case (a synthetic
0-meter target yields `planned_mi == 0.0`, not `None`).
**Correction — an earlier draft placed this in `plans.py`; that
forces an unsanctioned import.** `weekly_rollup` needs `units.to_miles`,
but `plans.py` imports only `config`/`db` (plans.py:18) and its internal
convention is km — a core→`agent.units` import would breach the layer
boundary for no benefit, since both of the function's only consumers
(`_build_plan_section` and `get_training_plan_progress`) live in
`tools.py`, which already imports `units`. `plans.py` gains only
`goal_gap` (pure seconds arithmetic, no units needed — see 2b); tests
import `weekly_rollup` directly from `tools`.

**Rounding order is specified, not incidental**: per-day
`units.to_miles` (which rounds to 2 dp), *then* sum, *then* round the
total to 1 dp — reproducing the PDF's existing order (per-day conversion
and summing at tools.py:1786-1804; the totals' 1 dp rounding at
tools.py:1837-1838). Summing raw meters first and converting once can
differ by 0.1 from the per-day-rounded sum, so the tests/test_tools.py:1687
pin (`week_actual_mi == 6.9`) holds **by construction** only under this
order. **Correction — an earlier
draft called the verdict-conditional `actual_mi` suppression "PDF display
logic, not aggregation" and left it in `_build_plan_section`. That's wrong:
the aggregation CONSUMES the suppression.** `_build_plan_section` nulls
`actual_mi` for `pending`/`compliant` verdicts (tools.py:1787-1788) *before*
`week_actual_mi` sums over the entries (tools.py:1804), and
tests/test_tools.py:1687 pins the suppressed total
(`week_actual_mi == 6.9`). So the suppression is part of `weekly_rollup`'s
documented **aggregation semantics**: the function itself nulls `actual_mi`
on each `days` entry for `verdict in ("pending", "compliant")`, and the
totals sum the suppressed list. This is the existing PDF rule promoted to
the shared definition of "week actual mileage" (runs that haven't been
graded don't count into actuals) — PDF and MCP agree by construction, the
:1687 total pin survives unchanged, and the :1701 per-day pin (pending day's
`actual_mi is None`) survives via the same shared suppression on the `days`
entries. The earlier draft also
returned a `week_adherence_pct` field; that was an invention (nothing
computes it today) — dropped. Whole-plan `adherence_pct` already exists on
both plan tools.

Consumers split cleanly. `_build_plan_section` consumes `rollup["days"]` as
the PDF's `last_7_days` table **as-is** — the current per-day entries carry
exactly `{date, type, planned_mi, actual_mi, verdict}` (tools.py:1792-1798),
no pace strings, so `days` IS the table with no per-day enrichment; the
only conversions that stay local to the PDF are the `today` payload's
`to_miles`/pace-string formatting (tools.py:1813-1818). `get_training_plan_progress` attaches
**only the three totals** as a `this_week` object
(`{week_planned_mi, week_actual_mi, slips}`, NOT `days`) — the windowed
`workouts` list (2c) already covers per-day data on that tool, so echoing
the week's days inside `this_week` would be redundant payload. And it is
**progress only, not `get_training_plan_status`**. Status is built from
`plans.build_plan_status`'s slim dicts, which carry no `actual_distance_m`
(`_slim_workout`, plans.py:854-867). To be honest about *why* status stays
slim: it is **not** that the data is unreachable — the status tool already
loads `activities_by_date` for the full plan window (tools.py:1485), so a
week rollup *could* be derived there. The real reasons are the tool's
pinned one-connection budget and latency benchmark
(tests/test_perf_benchmarks.py) plus its slim-by-design contract ("just
today"): re-deriving the rollup on status would duplicate progress's answer
for no routing benefit. Status stays slim; its description
points at `get_training_plan_progress` for week rollups. This is the
deterministic answer to "how's my week going" that today forces the agent
to re-aggregate a 100-row list.

Prior art: `plans.weekly_mileage` (plans.py:344-371) already rolls up
planned-vs-actual per `week_index`. `weekly_rollup`'s
trailing-7-days-ending-on-`target_date` window is deliberately different —
it matches the PDF section's existing definition, not plan-week boundaries.

**2b. Goal-gap fields.** `plans.goal_gap(predicted_finish_s: float | None,
target_time_s: int | None) -> {gap_seconds, gap_pct, on_pace: bool} | None`
(None when `predicted_finish_s` is None **or when `target_time_s` is None
or `<= 0`**; `predicted_finish_s` is a float because `riegel_predict`
returns `float`, plans.py:341). The `<= 0` guard is a real
zero-denominator rule, not paranoia: `validate_plan_input` **admits** a
zero `target_time_seconds` (plans.py:205-207 — `n < 0` rejects only
negatives), and the stored value reaches `goal_gap` via
`detail.get("target_time_seconds")` (tools.py:1550), so a storable zero
goal time would otherwise divide `gap_pct` by zero. A zero goal time is
storable but meaningless — there is no defined gap % against it. Pinned
in the degenerate-input test list alongside the None cases. Attached to `get_training_plan_progress` (top level, next to the
existing raw seconds) — **progress only. Correction: an earlier draft also
attached it to `build_plan_status`'s payload so `get_training_plan_status`
would carry it; that has no data source.** `build_plan_status`
(plans.py:869-903) computes no Riegel projection — only
`get_training_plan_progress` has `best_recent_effort` /
`predicted_finish_seconds` in hand (tools.py:1514-1516). Plumbing it in
would either change `build_plan_status`'s signature (whose other caller,
`brief_planner._plan_today`, sits inside the perf-benchmarked
`assemble_brief_context`) or add queries to `get_training_plan_status` —
itself benchmarked with a pinned one-connection budget
(tests/test_perf_benchmarks.py:90-92, :119-122). Status stays slim; its
description points at progress for goal-gap questions. "Am I trending
toward my goal" is currently a raw two-number diff left to the model.

**2c. Window `get_training_plan_progress`.** Today it returns every
prescribed day (~112 entries for a 16-week plan) and its description steers
the LLM to it for "how is my plan going." Change: the `workouts` list
defaults to a window of **`[anchor_back − 14 days, anchor_fwd + 7 days]`**,
where `anchor_back = frontier if frontier is not None else today` and
`anchor_fwd = max(frontier or today, today)` (`frontier` =
`db.last_known_daily_date()`, the grading frontier the tool already
fetches). Anchoring the back edge to the frontier keeps the graded history
in view; taking `max(…, today)` on the forward edge guarantees **today is
always in-window even when the frontier is stale** (>7 days behind after a
sync gap — and this is the very tool CLAUDE.md steers to for "show my plan
through today"); the `else today` fallbacks make the window well-defined on
a fresh DB where the frontier is None. A new optional boolean arg `full`
(default false) returns the complete list. The rollups (`adherence_pct`,
`days_to_race`, `predicted_finish_seconds`, `goal_gap`, `this_week`) are
computed from the **full graded workout list, not the 2c-windowed
projection**: `adherence_pct`/`days_to_race`/`goal_gap` remain whole-plan,
and `this_week` is trailing-7-days *by definition* (so "regardless of
window" phrasing would be sloppy — its window is its own, never 2c's).
Description rewritten to say so and to name `get_training_plan_status` for
"just today." Behavior change by design: an agent that wants the whole plan
(e.g. "show my plan through today" for a plan older than 14 days) must pass
`full=true` — the description must state this explicitly since CLAUDE.md
steers that question here. Test pin `tests/test_plan_tools.py:199`
(1-workout fixture) survives — its single workout sits at today+1, inside
the window's *upcoming* side (an earlier draft said "because today is
always in-window," which is true but not why that fixture survives); add a
long-plan truncation test.

**2d. Formatted time fields.** Use the existing `units.format_duration` —
verified: it already emits `H:MM:SS` at/over an hour and `M:SS` under
(units.py:55-68; `3750 → "1:02:30"`), so **no `format_hms` sibling is
added** (an earlier draft proposed one behind an "implementer verifies"
hedge; the verification is done — the function exists and covers the
shape). The sub-hour form is deliberate and correct for these fields: a
45-minute target renders as `"45:00"`, pinned by test.
Attach: `target_duration_formatted` per workout (when `target_duration_sec`
present) on `get_training_plan_progress`, and the same on
`get_training_plan_status`'s `today`/`last_graded` dicts (`_slim_workout`
already carries `target_duration_sec` — pure formatting of data in hand).
Top-level `predicted_finish_formatted` / `target_time_formatted` attach to
`get_training_plan_progress`; `get_training_plan_status` gets
`target_time_formatted` only (`target_time_seconds` IS in
`build_plan_status`'s payload — pure formatting, no new data) and **not**
`predicted_finish_formatted` (no projection on that path — see 2b). Raw
seconds stay. A wrong hand-built h:mm:ss for a race-time answer is a
plausible, embarrassing agent error.

**2e. Fix C from the 07-10 doc** (`_augment_plan_workout` mile/pace fields
with the exact `display_units()` gating split) ships as specified there. The
07-10 doc's claim that plan tools were "the only" convention gap was wrong;
2f closes the rest.

**2f. Close the remaining miles-convention gaps.** Apply `_augment_workout`
per split in `get_workout_detail` (`tools.py:470-473` — currently `SELECT *`
raw, so the whole-run view is in miles while its splits are meters/sec-per-km)
and per matched workout in `recovery_pattern` (`tools.py:759-763`). One
prerequisite on the latter: `recovery_pattern`'s SELECT (tools.py:721-724)
fetches only `activity_id, date, activity_type, distance_meters,
training_load, aerobic_te` — no `avg_pace_sec_per_km` or
`duration_seconds` — so as written `_augment_workout` could only add
`distance_mi` there. Widen the SELECT to include `avg_pace_sec_per_km` and
`duration_seconds` (trivial — same table, same rows) so the helper produces
its full field set (`pace_min_per_mi`, `duration_formatted` included).

**2g. Aggregate over activity distance in `compare_periods`** *(designated
cut if the quality gate flags scope)*. "How much did I run this week vs
last" has no structured tool and forces `run_sql`. Minimal move mirroring
the existing `training_load` special case (`tools.py:492-497`): accept
`distance_meters` as a metric, sourced from `activities` with **SUM per
period**. The SUM-branch payload shape is fixed here: each period carries
`{n, total}` (no `mean`/`sd` — a period total has no per-observation
stats) plus a per-period `total_mi` convenience via `units.to_miles` under
the same `display_units()` gate as `_augment_workout`; the top level
carries `delta` and `delta_pct` and **no** `cohens_d`/`magnitude` (no
per-observation SD to pool — `effect_size` fields are omitted for SUM
metrics). Pace aggregation is deliberately excluded (duration-weighted
mean is a real design problem — not this pass). Constraint tension,
acknowledged: this is the closest thing to a new capability in a
zero-new-features release — which is exactly why it is the designated cut
rather than a core work item.

---

## WS3 — Agent UX surface

**3a. plan_coach prompt parity.** `plan_coach.build_prompt`
(plan_coach.py:58-64) omits user notes and the metric-translation block, so a
saved preference ("stop roasting my steps") is honored in chat and brief but
violated in the PDF's coaching line. Change: `build_prompt` gains a
`notes_text: str | None` parameter (pure — caller does the I/O via
`notes.render_for_prompt()`, same pattern as `prompts.py:26-33`) appended as
a notes section, plus the one-paragraph metric-translation reminder from
`system_prompt`. `generate_coaching_line` gains the same
`notes_text: str | None = None` parameter, plumbed through to
`build_prompt` — this signature change is part of the API surface, not an
implementation detail — and its caller in `tools.py` threads the notes in.
Enumerated test casualty: `tests/test_tools.py:1729-1747` monkeypatches
`generate_coaching_line` with a fake pinned to the current fixed positional
signature; threading a new argument through would make the fake raise
`TypeError`, which `generate_brief_report` swallows into the deterministic
fallback — the test then fails on its assertion that the fake's line
appears in the PDF (a silent-fallback failure mode, not a loud one). Update
that fake's signature in the same change.

**3b. Chart-rendering guidance reaches the client.** `system_prompt`
(prompts.py:82-100, the exact text delivered as MCP `instructions` and the
`coach` prompt) gains a "Charts" bullet: *when you call `chart`, reproduce
its full output in a fenced code block in the reply, then add the coach
read — never leave it in the collapsed tool call.* The `chart` tool's
description gets a one-line echo of the same rule. This is CLAUDE.md's most
emphatic convention and it currently never reaches the connecting LLM.
One adjacent pin to note: tests/test_prompts.py:34-37
(`test_v2_system_prompt_is_shorter_than_v1`) survives because the Charts
bullet lengthens only V1's `system_prompt` side of the inequality —
mirroring the bullet into `brief_v2_system_prompt` later would trip it.

**3c. Sleep seconds → formatted.** `_render_status` (mcp_server.py:73)
renders `sleep_seconds` baseline rows as raw integers (`27180`) in the very
table the coach reads first. **Not via `format_duration`** — that renders
`27180` as `"7:33:00"`, a run-duration shape, and the repo's sleep
convention is deliberately hours-and-minutes: `brief_planner._hm` renders
`"7h 33m"` (`_SNAPSHOT_UNITS`, brief_planner.py:596, chosen per
brief_planner.py:437-439 so grounding's pool isn't polluted by stray
seconds), and prompts.py:64-65 mandates hours-and-minutes for sleep.
Change: add `units.format_hm(seconds: float | int | None) -> str | None`,
reproducing `_hm`'s current output **exactly** (brief_planner.py:437-443):
`f"{h}h {m:02d}m"` at/over an hour — minutes zero-padded, so `"7h 05m"`,
not `"7h 5m"` — and `f"{m}m"` sub-hour (`"45m"`); `None` in → `None` out
(the float in the union is real — callers feed float seconds, and `_hm`
already `int(round())`s internally, brief_planner.py:442). Have
`brief_planner._hm` delegate to it (single source — `_hm` keeps its own
""-on-None contract at its own boundary while delegating), and
use `format_hm` for `_render_status`'s sleep row and for
`value_formatted` (and `baseline_formatted`) on the `sleep_seconds` metric
row in `status._metric_rows` — symmetric with how `recent_workouts`
already get `duration_formatted`. `units.format_duration` stays the
formatter for workout/target durations (2d unchanged). `get_today_status`
inherits via Fix B's convergence. Cross-path invariant gained: the brief
pool and the snapshot rows render sleep identically **by construction**
(one function).

**3d. MCP-appropriate error strings.** Three errors point MCP-only users at
CLI commands they cannot run: `training_load_status`'s "pull activities and
run recompute-baselines" (tools.py:605), `log_manual_workout`'s "run
`fitness baselines`" (tools.py:1161), and `delete_manual_workout`'s
identical "run `fitness baselines`" warning (tools.py:1212-1213). The
rewordings differ because the honest remedy differs:

- **`training_load_status`'s empty-DB error → point at `sync_garmin_data`.**
  This is the one case where the pointer is honest: an empty DB is fixed by
  a pull. The same `days_pulled > 0` recompute gate applies here too, but
  benignly — a first pull on an empty DB pulls > 0 days and so recomputes
  baselines; the exotic activities-exist-but-baselines-missing state falls
  back to the nightly job, same as the manual-workout cases below.
- **The two manual-workout recompute-failure warnings must NOT point at
  `sync_garmin_data` as an immediate fix**: that tool recomputes baselines
  only when `days_pulled > 0` (tools.py:579-580), so calling it seconds
  after logging a manual workout is a no-op. Keep the literal phrase
  **"recompute failed"** — tests/test_tools.py:731 pins
  `"recompute failed" in payload["warning"]`, and :754's
  `recompute_failed is True` flag is untouched — and reword only the
  remedy tail to be honest for MCP users: baselines may lag until the next
  *successful* sync (the nightly job, or `sync_garmin_data` once new
  Garmin data exists). No CLI wording in any of the three.

`run_sql`'s opaque "query failed: invalid
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
(`adherence_pct`, `days_to_race`, today's `distance_mi`/pace,
`week_planned_mi`/`week_actual_mi`). `generate_brief_report` logs the flags advisorily, exactly as V2
does (`log_grounding` pattern) — never gates, never alters the PDF.
`grounding`'s parser internals (`_parse`, `_nearest`) get public
re-exports (or a small public wrapper) rather than plan_coach importing
underscore-names cross-module. `GroundingFlag` requires an integer
`takeaway_index` (grounding.py:60); coaching-line flags use **index 0** by
convention — the PDF has exactly one coaching line, so there is no takeaway
list to index into. Two advisory-signal caveats, stated so nobody
over-reads the flags: the plan-section pool includes string-shaped pace
values (`"9:23"`) that the tokenizer splits into numeric tokens (9, 23)
the same way `_display_numbers` does; and a `days_to_race` cited in prose
is typically skipped by grounding's time-window rule. The signal has the
same partial-coverage character as V2's — advisory, never a gate.

**4b. Restore the invention-rate signal on the V1 rollback path**
*(second designated cut)*. V1 (`LOCAL_FITNESS_BRIEF_V2=0`) currently loses
grounding entirely because no `BriefContext` is assembled. Change: the V1
branch calls `brief_planner.assemble_brief_context()` **solely to build the
grounding pool** (never for the prompt), then `log_grounding` runs on both
paths. Decouples measurement from generation strategy. **Error isolation
is mandatory**: the V1 `assemble_brief_context()` call is wrapped in
`try/except Exception` — on ANY failure the pool is `None`, grounding is
skipped, and generation proceeds exactly as today. V1 is the *rollback*
path; an unguarded planner call there would make rollback crash on the
very planner bug that might have motivated flipping
`LOCAL_FITNESS_BRIEF_V2=0` in the first place. If this proves
awkward in implementation (V1 is rollback-only), dropping 4b is acceptable —
it is a measurement nicety, not a correctness fix.

**Invariant scoping (correction).** An earlier draft pinned "V1 prompt
construction byte-identical to today" via a frozen snapshot fixture. That's
false *in this same release*: WS3b changes `system_prompt`, and V1 builds
its options from `prompts.system_prompt` (briefing.py:621), so V1's system
prompt legitimately changes here. The real invariant is that **4b
introduces no prompt change of its own**: assert the V1 *user* prompt
(`briefing_prompt`) is unchanged, and that the V1 options' system prompt
equals the live `prompts.system_prompt(...)` output (whatever that is
post-WS3b) — equality against the live prompt builder, not a frozen byte
snapshot. Enumerated test casualty: `tests/test_briefing.py:606-613`
`test_v1_path_does_not_run_grounding` pins exactly the behavior 4b inverts
("V1 has no BriefContext → no grounding log") — REWRITE it to assert
grounding DOES log on V1 with a real pool assembled, prompts unchanged per
the above. If 4b is cut, that test stays as-is.

**Explicitly out of scope for WS4** (documented, not forgotten): grounding
for external-client chat (structurally impossible — prose composed outside
the process, per the 06-27 design), the live-eval CI job (deferred by
design), script `--run` I/O glue tests (wraps already-tested composer).

---

## Deferred / out of scope (whole design)

- **Wall-clock vs data-frontier window anchoring** (`date.today()` in read
  tools vs frontier in plan grading). Real inconsistency, but re-anchoring
  is a semantic change with stale-data edge cases deserving its own design.
  The frontier is derivable server-side (`db.last_known_daily_date`), and
  2c's window formula already guards the one place staleness would have
  bitten in this release; a general re-anchoring of the read tools gets its
  own design — no new fields here. (Correction: an earlier draft claimed
  the frontier "is already visible via
  `get_brief_context.data_through_date`" — no such field exists;
  `BriefContext`, schemas.py:129-146, carries no frontier field and
  nothing defines `data_through_date`. CLAUDE.md's brief-failure-signature
  paragraph references the same phantom field — inaccurate, and should be
  corrected in this PR's CLAUDE.md pass.)
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
- `trend_direction(slope_per_day: float | None, *, flat_threshold: float) -> str` — `"no data"` iff `slope_per_day is None`; flat via inclusive `<=` (see WS1)
- `delta_direction(pct_change: float | None, *, flat_pct: float = 2.0) -> str` — scalar %-delta classifier (see WS1)
- `baseline_position(sd_distance: float | None) -> str` — strict bands (`> +1` / `< −1`; exactly ±1.0 → `"normal"` — deliberately strict, unlike `correlation_read`/`effect_size`'s inclusive lower bounds; see WS1)
- `correlation_read(r: float | None) -> dict | None`
- `effect_size(mean_a, mean_b, sd_a, sd_b, n_a, n_b) -> dict | None`
- `sd_position(value, mean, sd) -> dict | None`

New public function in `brief_planner` (takes a connection — not pure, but single-sourced):
- `brief_planner.ctl_at_or_before(conn, anchor_date: str) -> float | None` — public module-level extraction of the 14-day CTL "then" lookup (same no-lookback-floor SQL, brief_planner.py:501-504); consumed by BOTH `_compute_signals` and `training_load_status` (which runs it on its existing connection via the same lazy in-function `brief_planner` import `get_brief_context` already uses, tools.py:1591-1594) — agreement by construction; see WS1's shared-"then" paragraph.

New pure functions in existing modules:
- `tools.weekly_rollup(workouts: list[dict], target_date: str) -> dict` — `target_date` is an ISO date string (repo convention — callers window via ISO-string comparison, tools.py:1776-1778, which is order-correct); module-level in `agent/tools.py` (NOT `plans.py` — see 2a's relocation rationale); returns `{week_planned_mi, week_actual_mi, slips, days: list[dict]}` where each `days` entry is `{date, verdict, type, planned_mi, actual_mi}` — the canonical per-day shape, with the verdict-conditional `actual_mi` suppression (`verdict in ("pending", "compliant")` → `None`) applied inside the function and the totals summed from that same list; `days` is **reverse-chronological** (most recent first — the tests/test_tools.py:1697-1698 pin; see 2a for the downstream consumers that depend on it); rounding order: per-day `to_miles` 2 dp → sum → total 1 dp (see 2a). Empty window → `days: []` with zero totals; the empty→`None` short-circuit stays in `_build_plan_section` (see 2a). `_build_plan_section` consumes `days`; `get_training_plan_progress`'s `this_week` carries the three totals only.
- `plans.goal_gap(predicted_finish_s: float | None, target_time_s: int | None) -> dict | None` (`predicted_finish_s` is float — `riegel_predict` returns `float`, plans.py:341). Returns `None` when `predicted_finish_s` is None or when `target_time_s` is None **or `<= 0`** — a zero goal time is storable (`validate_plan_input` rejects only negatives, plans.py:205-207) but has no defined gap % (see 2b).
- `units.format_hm(seconds: float | int | None) -> str | None` — reproduces `_hm`'s output exactly: `f"{h}h {m:02d}m"` at/over an hour (zero-padded minutes — `27180 → "7h 33m"`, `25500 → "7h 05m"`), `f"{m}m"` sub-hour (`"45m"`), `None` → `None`; the sleep-rendering convention (callers feed floats — `sleep_seconds` values and 60-day means; `_hm` already does `int(round(...))`, brief_planner.py:442); `brief_planner._hm` delegates to it, keeping its own ""-on-None contract at its boundary (see 3c).
- No `units.format_hms` — the existing `units.format_duration` already emits `H:MM:SS` at/over an hour (units.py:55-68); reused as-is for workout/target durations (see 2d). `format_hm` is not an `H:MM:SS` sibling — it is the hours-and-minutes sleep shape (3c), a different convention.
- `plan_coach.ground_coaching_line(text: str, plan_section: dict) -> list[GroundingFlag]` — flags carry `takeaway_index=0` (see 4a).
- `plan_coach.build_prompt(..., notes_text: str | None = None)` — signature extension, still pure.
- `plan_coach.generate_coaching_line(..., notes_text: str | None = None)` — signature extension, plumbed through to `build_prompt` (see 3a's enumerated test casualty).

Tool payload changes (all additive unless noted):
- `training_load_status`: + `tsb_zone`, `ctl_pct_change_14d` (its "then" value via `brief_planner.ctl_at_or_before` on the tool's existing connection — the shared no-floor lookup, NOT a point picked from the tool's 30-day window; see WS1), `ctl_direction` (via `delta_direction`, not `trend_direction`); floats rounded.
- `correlate`: + `strength`, `direction`; **legend string removed**; `pearson_r` rounded to 3 dp (skipped when `None` — the zero-denominator case, tools.py:675; the boundary-rounding rule skips `None` generally, see WS1's rounding paragraph).
- `find_anomalies`: per-row + `sd_distance`, `direction`.
- `compare_periods`: + `delta_pct`, `cohens_d`, `magnitude`; floats rounded; (2g) `distance_meters` accepted with SUM semantics — per-period `{n, total}` + `total_mi` (miles-gated), top-level `delta`/`delta_pct`, no `cohens_d`/`magnitude`.
- `get_metric_trend`: + `slope_direction`, `vs_baseline` (**always attached** — `"no data"` when `current_vs_baseline_sd` is absent/None, i.e. every metric outside `rhr`/`sleep_seconds`, tools.py:240-246; deterministic, never conditionally omitted); floats rounded; for `n < 2` the payload's `slope_per_day` becomes `null` (not today's guarded 0.0) alongside `slope_direction` `"no data"` — a deliberate, enumerated payload change (see WS1's flat-threshold note).
- `recovery_pattern`: matched workouts pass through `_augment_workout`; SELECT widened to add `avg_pace_sec_per_km` + `duration_seconds` (see 2f); floats rounded.
- `get_workout_detail`: splits pass through `_augment_workout`.
- `get_training_plan_progress`: + `this_week`, `goal_gap`, `predicted_finish_formatted`, `target_time_formatted`, per-workout `target_duration_formatted` (+ Fix C mile/pace fields); `workouts` **windowed by default** (`[anchor_back − 14d, anchor_fwd + 7d]` per 2c; `full=true` restores complete list). This doc's own workstreams carry **three non-additive changes**, enumerated by name: (1) 2c's windowing here; (2) `correlate`'s legend-string removal in WS1 (unpinned by tests — tests/test_tools.py:371 asserts only `pearson_r` presence); (3) `get_metric_trend`'s `slope_per_day` 0.0 → `null` for `n < 2` (WS1's flat-threshold note). Folded-in Fix B is a **fourth at the release level** — it replaces `get_today_status`'s whole payload shape and is enumerated in the 07-10 doc.
- `get_training_plan_status`: + `target_time_formatted`; `target_duration_formatted` + Fix C mile/pace fields on `today`/`last_graded`; description rewritten to point at `get_training_plan_progress` for week rollups / goal gap. **No** `this_week`, `goal_gap`, or `predicted_finish_formatted` — goal-gap/projection genuinely has no data source on that path (no Riegel query, see 2b); `this_week` is excluded by the slim-by-design contract + perf pins, not data absence (see 2a).
- `BriefContext.anomalies` entries: + `sd_distance`, `direction` (schema class unchanged — free-form list).
- `daily_snapshot` / `get_today_status` (via `status._metric_rows`): `sleep_seconds` row + `value_formatted`/`baseline_formatted` via `units.format_hm` (`"7h 33m"` shape — see 3c).
- Plus the 07-10 doc's API surface (Fix A/B/C) verbatim.

Prompt/instruction changes:
- `system_prompt`: + Charts bullet (delivered via MCP `instructions` + `coach` prompt).
- `plan_coach.build_prompt`: + notes section + metric-translation block.
- Tool description rewrites: `chart`, `daily_snapshot`, `get_today_status`, `get_brief_context`, `get_training_plan_progress`, `get_training_plan_status` (points at progress for week rollups / goal gap), `generate_chart` (per 07-10 doc), error strings per 3d.

## Invariants

**Checkable by inspection:**
- `interpret.py` imports nothing outside stdlib (no db, no SDK, no schemas).
- `status.py` delegates TSB-zone classification to `interpret.py`;
  `brief_planner.py` delegates pct-change (it never computes a TSB zone —
  an earlier draft claimed it delegated "TSB zone" too). The tsb-zone
  classification bands live only in `interpret.py`; `brief_planner._TRIGGERS`'
  tone thresholds (`tsb_fresh`/`tsb_very_fatigued`, brief_planner.py:47-48)
  reference interpret's constants where feasible; prose tool descriptions
  may restate the bands.
- The 14-day CTL "then" lookup exists in exactly one place —
  `brief_planner.ctl_at_or_before` — consumed by both `_compute_signals`
  and `training_load_status`; neither path re-derives a "then" point from
  a history window (agreement by construction, mirroring the `tsb_zone`
  treatment — see WS1's shared-"then" paragraph).
- `tools.weekly_rollup` (module-level in `agent/tools.py`) and
  `plans.goal_gap` are I/O-free — `weekly_rollup` opens no connections and
  is directly importable/testable; `plans.py` gains no `agent.units` import
  (see 2a). `_build_plan_section`
  calls `weekly_rollup` (no duplicated aggregation; the verdict-conditional
  actual suppression lives inside the shared function — see 2a).
- `plan_coach.build_prompt` remains I/O-free (notes text passed in).
- `plan_coach.ground_coaching_line` never raises on arbitrary text; grounding
  stays advisory (no gate on the PDF path).
- All raw fields kept wherever formatted/interpreted siblings are added.
- V1 branch (if 4b ships) uses `assemble_brief_context` for grounding only —
  4b introduces no prompt change of its own: the V1 user prompt
  (`briefing_prompt`) is unchanged, and the V1 system prompt equals the
  live `prompts.system_prompt(...)` output post-WS3b (not a frozen byte
  snapshot — see 4b's invariant-scoping correction).
- 4b introduces **no new failure mode on the rollback path**: the
  grounding-context assembly is wrapped in `try/except Exception`; any
  failure means grounding is skipped and V1 generation proceeds exactly as
  today (see 4b's error-isolation requirement).
- All invariants from the 07-10 doc (Fix A/B/C) hold, with one explicit
  supersession: its "`_build_plan_section` is unchanged" line is superseded
  by WS2a's `weekly_rollup` call. The rest of that invariant still holds —
  `_build_plan_section` does NOT call `_augment_plan_workout`; it keeps
  only the `today`-payload conversion (tools.py:1813-1818) and pace-string
  formatting — per-day window/convert/suppress lives solely in
  `weekly_rollup`.

**Testable:**
- Every `interpret.py` classifier: band boundaries pinned on both sides,
  None/zero/missing handled (no exceptions on degenerate input). The `<=`
  inclusivity is pinned explicitly: `trend_direction` with
  `abs(slope_per_day)` exactly at `flat_threshold` → `"flat"` (including
  the `flat_threshold == 0`, slope-0 constant-series case), and
  `delta_direction` with `abs(pct_change)` exactly at `flat_pct` →
  `"flat"`; `trend_direction(None, ...)` and `delta_direction(None)` →
  `"no data"`. `baseline_position` at exactly ±1.0 → `"normal"` (the
  strict-band side, pinned on both sides). `correlation_read`'s
  0.2/0.4/0.6 thresholds and
  `effect_size`'s 0.2/0.5/0.8 magnitude thresholds pinned on both sides
  (lower-bound inclusive — a value exactly at a threshold takes the named
  band); direction at `r == 0.0` → `"positive"`; `pearson_r` None →
  `correlation_read` returns None. `get_metric_trend` passes
  `slope_per_day=None` for windows with `n < 2` (the tool-side mapping in
  WS1's flat-threshold note) and its payload carries `slope_per_day: null`
  (not 0.0) on that path.
- `training_load_status` payload contains `tsb_zone` equal to
  `interpret.tsb_zone` of its own `tsb` value (agreement by construction).
- `training_load_status`'s `ctl_pct_change_14d` equals the brief signal
  for the same DB state — including a **gappy-baselines fixture** (no row
  exactly at `today − 14d`, so the at-or-before lookup and a
  window-derived point would differ): both paths must return the same
  value because both call `brief_planner.ctl_at_or_before`.
- `correlate` payload has computed `strength`/`direction` and no legend
  string; `find_anomalies` rows carry `sd_distance` matching
  `(value-mean)/sd` to 2 dp; `compare_periods` carries `cohens_d`/`magnitude`
  consistent with `effect_size`, including the per-field degradation case:
  two 1-day periods → `delta_pct` present (computed from the means),
  `cohens_d`/`magnitude` None.
- Rounded floats: no analysis-tool payload float exceeds its dp budget;
  `None` values are skipped by the boundary rounding (a `None` `pearson_r`
  passes through as `null`, no exception).
- `get_metric_trend`'s `vs_baseline` key is present on every metric —
  `"no data"` for a non-baselined metric (e.g. `steps`), a real band for
  `rhr`/`sleep_seconds` with a baseline SD in the fixture.
- `weekly_rollup` over a fixture week equals the values the PDF section
  displays for the same fixture — this now holds **by construction** (the
  shared function carries the suppression semantics AND the rounding order:
  per-day 2 dp → sum → 1 dp; see 2a), and
  tests/test_tools.py:1687's `week_actual_mi == 6.9` pin survives unchanged;
  `days` is reverse-chronological (the :1697-1698 pin survives) and the
  empty-window → `None` behavior of `_build_plan_section` (the :1713-1717
  pin) is unchanged; `weekly_rollup` is tested via direct import from
  `tools` with no DB.
- `goal_gap` sign convention pinned (positive gap = slower than goal);
  degenerate inputs pinned: `target_time_s` of `None` **and of `0`** (and
  negative) → `None`, `predicted_finish_s` `None` → `None` (see 2b's
  zero-denominator rule).
- `get_training_plan_progress` default call on a long-plan fixture returns
  only in-window workouts; `full=true` returns all; rollups identical in
  both modes; today is in-window under a stale frontier (frontier > 7 days
  behind today) and the window is defined when the frontier is None.
- `format_duration(6420) == "1:47:00"` (existing behavior, pinned for these
  fields), the sub-hour case (`format_duration(2700) == "45:00"` for a
  45-minute target), and the None case.
- `format_hm(27180) == "7h 33m"`, the zero-padded-minutes case
  (`format_hm(25500) == "7h 05m"`, not `"7h 5m"`), the sub-hour case
  (`"45m"`), and the None case pinned;
  cross-path sleep rendering identical by construction —
  `brief_planner._hm` (grounding pool) and `status._metric_rows`'
  `value_formatted` produce the same string for the same seconds (see 3c).
- `build_prompt` output contains the notes text when provided and the
  metric-translation block always; `test_plan_coach` asserts a note string
  appears in the assembled system prompt.
- `system_prompt` contains the Charts bullet (scorer stays green —
  `scripts/score_prompt.py` must pass on the modified prompt).
- Error strings: `training_load_status` empty-DB error mentions
  `sync_garmin_data` and not "recompute-baselines" CLI wording; the
  manual-workout warnings still contain the literal "recompute failed"
  (the tests/test_tools.py:731 pin), contain no `fitness baselines` CLI
  wording, and their remedy tail mentions the next successful sync (see
  3d); test_tools.py:754's `recompute_failed is True` stays green as-is.
- `ground_coaching_line` flags an invented adherence number and does not
  flag faithful citations — the faithful set must include a pace string
  (e.g. "9:30/mi") so tokenizer false-positives are observed (mirror
  `test_grounding.py` patterns); flags carry `takeaway_index == 0`.
- V1 path (if 4b ships): `log_grounding` called with a real pool; the V1
  user prompt (`briefing_prompt`) unchanged and the V1 system prompt equal
  to the live `prompts.system_prompt(...)` output — not a frozen snapshot
  fixture (see 4b). `test_v1_path_does_not_run_grounding`
  (tests/test_briefing.py:606-613) is rewritten to assert grounding DOES
  log on V1; if 4b is cut, it stays as-is.
- V1 failure isolation (if 4b ships): with `assemble_brief_context`
  monkeypatched to raise, the V1 brief still generates and saves — no
  grounding log, no exception (4b's "no new failure mode on the rollback
  path" invariant, made testable).
- `generate_coaching_line`'s monkeypatched fake in
  tests/test_tools.py:1729-1747 updated to the new `notes_text` signature
  (see 3a).
- `get_workout_detail` splits and `recovery_pattern` workouts carry
  `distance_mi`/`pace_min_per_mi` in miles mode, absent distance keys in km
  mode (same gate as `_augment_workout`).
- Plus all testable invariants from the 07-10 doc.

## Testing strategy

- `tests/test_interpret.py` (new): exhaustive band/edge coverage for every
  classifier — this module should be trivially 100%.
- `tests/test_plans.py`: `goal_gap` (None propagation, the `target_time_s
  <= 0` → None rule — zero and negative pinned, per 2b — sign convention,
  boundary values).
- `tests/test_tools.py`: `weekly_rollup` via **direct import from `tools`**
  (pure, no DB, no connection — empty, single, flat, missing-data,
  boundary weeks, and the rounding order per 2a).
- `tests/test_tools.py` / `test_plan_tools.py` / `test_mcp_server.py`:
  payload-attachment assertions per tool; windowing tests; the 07-10 doc's
  enumerated rewrites; description-string assertions (chart bullet, "when
  NOT to use" lines, error rewording). Two existing pins are **constraints
  on 3d's rewording, not casualties**: tests/test_tools.py:731
  (`"recompute failed" in payload["warning"]`) and :754
  (`recompute_failed is True`) must stay green unmodified.
- `tests/test_plan_coach.py`: notes-in-prompt, translation block,
  `ground_coaching_line` faithful/invented cases (faithful set includes a
  pace string like "9:30/mi" — see the testable invariant).
- `tests/test_status.py`: `value_formatted` on sleep rows (the `"7h 33m"`
  shape via `units.format_hm`, not `format_duration`'s `"7:33:00"`);
  delegation to `interpret` (same zone string).
- `tests/test_briefing.py`: 4b — V1 grounding-only context assembly (mocked
  SDK; assert `log_grounding` fires, the V1 user prompt is unchanged, and
  the V1 system prompt equals the live `prompts.system_prompt(...)` output).
  This is the rewrite of `test_v1_path_does_not_run_grounding` (:606-613);
  if 4b is cut, that test stays. Plus the failure-isolation case:
  `assemble_brief_context` monkeypatched to raise → V1 brief still
  generates and saves, no grounding log, no exception.
- `tests/test_tools.py`: update the `generate_coaching_line` fake
  (:1729-1747) to the new `notes_text` signature (see 3a).
- Prompt-change verification per repo policy: `scripts/score_prompt.py`
  green on the modified `system_prompt`; the ab_brief harness is known-flaky
  — do NOT rely on it; use the scorer + the unit assertions above.
- Perf gate — explicit analysis for **all five** benchmarked paths
  (tests/test_perf_benchmarks.py pins both latency, 15% floor, and
  `db.connect()` open counts of exactly 1):
  `get_training_plan_status`'s benchmarked path gains only
  `_augment_plan_workout` + `format_duration` formatting on ≤2 dicts
  (`today`/`last_graded`) — pure arithmetic, zero new queries or
  connections, well under the 15% latency floor.
  `get_training_plan_progress` (also benchmarked, latency + connection
  count) gains `weekly_rollup` + `goal_gap` + per-entry augmentation — all
  pure functions over already-fetched rows, zero new connections.
  `_build_plan_section` calls `weekly_rollup` on data it already fetched
  (no new opens).
  `daily_snapshot` (benchmarked via `assemble_status`) gains 3c's
  `value_formatted`/`baseline_formatted` on the sleep row — pure
  formatting, no new opens.
  `assemble_brief_context` gains the anomaly `sd_distance`/`direction`
  attachment in `_rhr_anomalies` — pure compute over rows already in hand,
  no new opens.
  `training_load_status` is NOT in the benchmarked set (verified against
  tests/test_perf_benchmarks.py), so WS1's one extra indexed point-query
  (`ctl_at_or_before`, run on the tool's existing connection) is allowed —
  no benchmarked path gains a query or a connection.
  One deliberate improvement: 2c's windowing *shrinks*
  `get_training_plan_progress`'s serialization work vs the committed
  baseline — CI-safe, since `--benchmark-compare-fail=min:15%` fails only
  on regression; a deliberate rebaseline (via `capture-perf-baseline.yml`)
  is optional per CLAUDE.md, not required.
  The connection-count tests
  (test_perf_benchmarks.py:119-122 etc.) must stay green **as-is** — they
  are pins, not casualties. `assemble_brief_context` reuse in 4b is off the
  benchmarked path since briefs aren't benchmarked with V1.
- Full `uv run pytest -x` + `ruff` locally before the PR; coverage ≥ 85%.

## Failure modes / edge cases

- Empty/fresh DB: every classifier returns its "no data" value; payload
  attachments are None/absent, never exceptions (same guarantee
  `assemble_status` documents).
- Degenerate stats: `sd == 0` → `sd_position` returns None (no
  ZeroDivisionError); `effect_size` degrades per-field — `sd` 0/None or
  `n < 2` nulls only `cohens_d`/`magnitude`, while `delta_pct` is still
  computed from the means (None only when `mean_b` is 0/None or `mean_a`
  is None); two 1-day periods → `delta_pct` present, `cohens_d` None;
  `r` exactly on a band boundary → pinned by test.
- Plan with no goal time, a **zero goal time** (storable —
  `validate_plan_input` rejects only negatives, plans.py:205-207 — but
  meaningless, and a bare zero would divide by zero), or no best-effort
  projection → `goal_gap` None, formatted fields absent.
- Windowing when the plan is entirely in the past (race done) or entirely
  future (starts next week): the 2c formula governs — there is **no
  clamping to plan bounds**. A fully-past or fully-future plan may legally
  yield an empty `workouts` list, with the rollups (`adherence_pct`,
  `days_to_race`, `goal_gap`, `this_week`, … — computed from the full
  graded workout list, not the 2c-windowed projection) still present;
  `full=true` remains the escape hatch for the complete list.
- Windowing when the frontier is None (fresh DB, no daily rows): both
  anchors fall back to `today` — the window is `[today − 14d, today + 7d]`,
  never an exception.
- Windowing when the frontier is stale (>7 days behind today after a sync
  gap): `anchor_fwd = max(frontier, today)` keeps today — and today's
  prescribed workout — in-window; a frontier-only forward anchor would have
  silently dropped it on exactly the "show my plan through today" question.
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
- CLAUDE.md updates in the same PR: add a tool-count note where relevant
  (CLAUDE.md carries no count today — the pinned 33 lives only in
  tests/test_smoke.py:44, bumping to 34),
  `get_training_plan_progress` windowing note in the Q&A section (the
  "show my plan through today" guidance must mention `full=true`), and the
  07-10 doc's Fix A/B notes — including a **rewrite of the
  "`generate_brief_report`/`generate_chart` are stdio-only, never
  `ALL_TOOLS`" paragraph**, which Fix A falsifies outright: it is a
  structural-boundary claim (stdio-only registration, unreachable over the
  HTTP transport), not just a count, and `generate_chart`'s `ALL_TOOLS`
  promotion breaks it.
- Devlog entry for the release.
- No promotion to `main` (dev-only until Nate says release).
