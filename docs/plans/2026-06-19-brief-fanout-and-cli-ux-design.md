---
ticket: "TBD"
title: "Brief fan-out (map-reduce) + top-grade CLI output"
date: "2026-06-19"
source: "design"
---

# Brief fan-out (map-reduce) + top-grade CLI output

## Phase 0 outcome (2026-06-20) — fan-out KILLED, shipped the fallback

Phase 0 measurement overrode the fan-out architecture below. Recorded here so
the design reads honestly; the fan-out sections are retained as the rejected
alternative.

- **Concurrency probe:** concurrent `query()` parallelizes only **1.44× at
  3-wide** — under the pre-registered 1.7× kill criterion. Fan-out abandoned.
- **Latency attribution:** a real instrumented brief showed ~208s of a ~230s
  brief is one generation block emitting ~13.6k output tokens of which ~93% is
  extended thinking. Serial tool round-trips are only ~15s. Thinking is the
  cost.
- **Working lever:** the SDK `thinking.budget_tokens` knob is **ignored** on
  the Claude Code CLI / Max-OAuth path (1024 vs 12000 → same output), but
  **`effort`** propagates. `effort="low"` cut the brief to **~82–97s** (~2.5–3×)
  with **better** content (blind LLM-judge A/B: low ≥ high on all four
  dimensions).
- **Dimension 2 (kept, load-bearing):** at low effort the model occasionally
  drops the backslash on a `\n` table row break (`|---|---|n| RHR |`),
  collapsing the table. Shipped a shared `render_table` helper + a
  `fix_table_row_breaks` repair at the save gate so every brief renders clean.

**Shipped:** env-driven `effort` lever (`LOCAL_FITNESS_BRIEF_EFFORT`, default
`low`) + token-usage instrumentation + deterministic table repair. No planner,
no fan-out, no per-card validation. Probe scripts under `scripts/phase0_*`.

## Problem & evidence

The daily brief takes ~6 minutes to generate. `uv run fitness brief` on
2026-06-19 produced `total_ms=358996` (~6 min) for a 4,344-char brief
(~1,500 output tokens). What the existing instrumentation actually tells us —
and, importantly, what it does NOT:

- **DB execution inside tools is cheap: 13ms total** (`tool_duration_sum_ms=13.1`,
  10 calls). But `tool_duration_sum_ms` is measured in `briefing.py` as the
  wall-time between the prior message and each `ToolResultBlock` — it captures
  the time *inside* each tool (the SQLite query), NOT the model round-trip
  *between* tool calls. The `query()` loop re-prompts the model after every
  tool result, and that inter-call model time is invisible to this counter. So
  "13ms" only exonerates the SQLite layer, not the tool loop.
- A **large silent block** (logged ~20:24 → ~20:29) with zero `tool_use` /
  `tool_result` activity. This is model time, but its composition is
  **unknown from the current logs**: it could be serial tool round-trips
  (model re-prompted after each of the 10 results), extended thinking on the
  one big compose pass, or both. We have not measured the split.
- `ab_brief.py` carries `EST_OUTPUT_TOKENS_PER_BRIEF = 40_000`. **This is a
  cost-estimate constant for the dry-run quote (`estimate()` → "est output
  tokens"), not a measurement.** It is never compared to a real usage report.
  The "~40k extended-thinking tokens = the 6 minutes" claim in earlier drafts
  was an inference from this constant and is **unverified**. Treat it as a
  hypothesis, not evidence.
- That same harness assumes `EST_SECONDS_PER_BRIEF = 25` — again a planning
  constant, not a measured baseline. The 6-min observed wall-clock is ~14×
  that assumption, which tells us the *assumption* was optimistic; it does not
  by itself localize the cost.

**Working hypothesis (to be validated in Phase 0):** one sequential `query()`
to `claude-sonnet-4-6` composes all 3–5 takeaways in a single pass, and the
silent block is dominated by uncapped extended thinking and/or serial tool
round-trips. The fan-out below is justified **only if Phase 0 measurement
confirms** that the latency lives in model thinking / serial round-trips that
parallelism + a thinking cap can attack — not in something parallelism can't
help.

Quality is currently excellent and is **sacred** — no change ships that
regresses the scorer or the A/B structural check, or the new per-card
quality judge (below).

## Goals / non-goals

**Goals**
1. Cut brief wall-clock from ~6 min toward **≤ ~90s** — a *target hypothesis*
   to validate against Phase 0 measurement, not a guarantee — without quality
   loss.
2. Make agent output "top-grade CLI tool" quality: pretty-but-simple tables
   when appropriate, consistent by construction, in both the brief `details`
   and conversational chat replies.

**Non-goals**
- No change to the `Brief`/`Takeaway` schema (`schemas.py` is frozen).
- No web-UI chat surface (chat is external-MCP; out of scope).
- No change to which data the brief is grounded in.
- **No progressive / streaming render of cards.** The brief is batch-generated
  to disk and the UI polls the cached `GET /api/brief` (`server.py` ~811;
  `web/src/lib/api.ts` `api.brief()`; `Today.tsx` does a one-shot fetch and
  maps `brief.takeaways`). Nothing consumes the NDJSON `takeaway`/`index`
  events except `_generate`, which discards everything but `done`. So there is
  **no progressive-rendering requirement** to preserve. Live card-by-card
  streaming to the browser is an explicitly OUT-OF-SCOPE future feature: it
  would need a new SSE endpoint plus a frontend `EventSource`, neither of
  which exists today.

## Architecture — map-reduce fan-out

Today: `gather (10 serial tool calls) → compose all cards (1 long pass)`.

New: **assemble (code) → plan (1 small model call) → compose (N parallel
model calls) → reduce (code)**.

```
┌─ 1. ASSEMBLE (code, the BULK of Phase 1) ──────────────┐
│ Deterministically build the full data digest. Be honest │
│ about the split: assemble_status() (status.py:194-215)  │
│ supplies ONLY the snapshot metrics + training_load       │
│ (CTL/ATL/TSB) — and its recent_workouts is CAPPED AT 5   │
│ (status.py:164), which must be extended to a 14-day       │
│ window. assemble_status's own snapshot trend ARROWS are   │
│ a 7-day window (get_today_status recent_days, tools.py    │
│ ~159; _TREND_WINDOW_DAYS, status.py:49,129) — they do NOT │
│ satisfy the digest's 14d-trend need. Everything ELSE in   │
│ the digest is NET-NEW code in briefing_data.py: 14d       │
│ trends (sleep/steps/rhr/bb/stress) from the extracted     │
│ _metric_trend helper at days=14 (independent of the 7-day │
│ snapshot arrows), anomalies, plan status, 14d workouts,   │
│ and recent-brief continuity. The metric-trend / anomaly / │
│ workout queries are TODAY authored INLINE inside the      │
│ @tool bodies (tools.py ~206-251, 272-300, 386-415) —      │
│ there is NO plain query layer to reuse. So Phase 1 FIRST  │
│ EXTRACTS plain helpers (_metric_trend / _query_workouts / │
│ _find_anomalies) out of those @tool bodies; both the      │
│ existing @tool wrapper AND build_digest then call the     │
│ SAME plain helper. The other two ARE already clean reuse  │
│ today: get_training_plan_status delegates to              │
│ plans.build_plan_status (tools.py ~1184) and continuity   │
│ is briefs._recent_briefs_summary (briefs.py:270, a real   │
│ plain function). All run the underlying DB queries        │
│ directly, NOT through the model — ZERO model tool          │
│ round-trips.                                              │
└────────────────────────────────────────────────────────┘
                      │  digest (incl. pre-rendered tables)
                      ▼
┌─ 2. PLAN (1 model call, small output, capped thinking) ─┐
│ Input: digest. Output (structured): ordered list of     │
│ 3–5 themes ∈ {workout, steps, conditioning, hr,         │
│ wildcard}, each with {chart_metric, days, is_lead,      │
│ emit, merge_into}. The planner OWNS the global editorial │
│ rules: ship-3-if-only-3-sharp (set emit=false on weak   │
│ themes), and merge decisions (e.g. emit=false on hr with │
│ merge_into="workout" folds HR into the workout card).    │
│ workout AND steps are always emit=true (required cards). │
└────────────────────────────────────────────────────────┘
                      │  slate: [ThemePlan, ...]
                      ▼
┌─ 3. COMPOSE (N≤5 model calls, PARALLEL) ───────────────┐
│ One agent per planned EMITTED theme. Each receives: the │
│ digest, its theme's mandate (already a modular section   │
│ of the system prompt), the continuity slice for its     │
│ theme, the house-style formatting rules, any inbound     │
│ MERGE DIRECTIVES the planner addressed to this theme     │
│ (see "Merge handoff" below), AND the other emitted       │
│ themes' headline-intents (read-only) so it can avoid     │
│ duplicating them. Returns one Takeaway JSON object.      │
│ Wall-clock = slowest card, not the sum.                  │
└────────────────────────────────────────────────────────┘
                      │  Takeaway × N (out of order)
                      ▼
┌─ 4. REDUCE (code, ~ms) ────────────────────────────────┐
│ asyncio.gather the composer coroutines (batch — wait for │
│ ALL, no progressive emission). Validate EACH returned    │
│ card individually as a bare Takeaway; drop any that fail │
│ (see Schema validation). Order the survivors per the     │
│ slate, assemble the Brief, validate + save ONCE via      │
│ briefs.save_brief (unchanged single write gate).         │
└────────────────────────────────────────────────────────┘
```

**Why this preserves quality — and how each global rule survives the split.**
The monolithic prompt enforces rules a naive per-card composer cannot see;
the fan-out must reproduce each one explicitly:

- *"Ship 3 if only 3 are sharp" (`prompts.py` ~243).* Owned by the **planner**:
  it sets `emit=false` on weak themes so they never reach a composer. One
  place makes the no-dead-weight call, exactly as the monolithic pass did.
- *Merge HR into the workout takeaway / "roll the green light into the workout
  takeaway" (`prompts.py` ~450–453).* Owned by the **planner**, but the actual
  prose is written by the **target composer** — see "Merge handoff" below for
  why a `merge_into` string alone is not enough.
- *"Connect conditioning to today's workout call" (~411).* The conditioning
  composer receives the workout theme's headline-intent as read-only context
  so it can reference today's session.
- *"Don't repeat headlines verbatim across days" (~166).* Each composer gets
  its theme's continuity slice (prior-day headlines for that theme) — same
  cross-day signal the monolithic pass had.
- *Cross-card (intra-day) duplication.* Each composer is handed the other
  emitted themes' headline-intents and instructed not to restate them. If A/B
  + the quality judge still flags repetition, fall back to a light reduce-stage
  model dedup pass (kept as a contingency, not the default).

Net: a composer prompted with *only* its mandate + the digest has less context
dilution than the monolithic pass; the planner concentrates the editorial
judgment. This is a *plausible* quality story, **not a guarantee** — it is
gated on the per-card quality judge and the A/B structural net below.

**Merge handoff (planner → composer).** Composers run in parallel, so when the
planner sets `emit=false` + `merge_into="workout"` on the HR theme, the workout
composer CANNOT read a not-yet-composed HR card — there is no HR card. The
handoff is therefore a **structured merge directive carried in the ThemePlan**,
not a reference to sibling output. Concretely: a merged theme produces no
composer of its own; instead the planner attaches a `merge_directive` to the
TARGET theme's plan — a short, structured payload (NOT prose) holding (a) the
one key data signal drawn from the digest (e.g. `"RHR +6 vs baseline, sleep
score 58"`) and (b) a one-line editorial read (e.g. `"recommend rest/deload"`).
The planner emits the SIGNAL + the read; it does NOT write the merged sentence.

The target composer's `card_prompt` then **inherits the relevant slice of the
HR & recovery mandate** (`prompts.py` ~418–453) so the merged content keeps its
editorial quality: specifically the "ONE clear lead signal (RHR OR sleep OR
body battery OR stress — pick the strongest)" rule (~455–459) and the all-green
roll-in ("recovery is green across the board — push the intervals", ~450–453).
The workout composer, holding that mandate slice + the merge directive, writes
the merged recovery sentence INSIDE the workout takeaway in its own voice. So:
planner decides + supplies data/read; target composer writes the prose under
the inherited mandate. `ThemePlan` gains an optional `merge_directive` field
(see API surface) carried only on themes that absorb a merge.

**Why it's fast (pending Phase 0 confirmation).** (a) Data assembly moves to
code → the serial tool round-trips vanish. (b) IF concurrent `query()` calls
actually run in parallel (see Phase 0 concurrency test — this is unproven and
load-bearing), the one long generation splits into N small generations and
wall-clock collapses toward the slowest card. (c) Each call gets an explicit,
modest thinking budget instead of the SDK default. Note: thinking tokens are
the suspected latency driver, so a smaller budget is a *latency* lever, not a
"free" one — see Thinking budget.

## Thinking budget

Set an explicit `thinking` budget on the planner and composer
`ClaudeAgentOptions` (currently unset → SDK default, the *suspected* latency
source — to be confirmed in Phase 0). Planner: small budget (decision task).
Composer: moderate budget (the writing needs some reasoning, but not 8k tokens
per card). Because thinking tokens are the suspected latency driver, the budget
is a direct wall-clock lever — capping it trades reasoning depth for speed, so
the exact values are tuned against the scorer / A/B / quality judge in the
measurement step, not guessed.

## Dimension 2 — top-grade CLI output

A single **deterministic table renderer** is the spine:

- Extract the table-building logic into a reusable helper (e.g.
  `agent/render.py`) that turns a list of rows + headers into a clean,
  width-aware markdown table following the house rules (≤4 cols, abbreviated
  one-word headers, no prose in cells, ~70-char width). Note the precise
  boundary: `mcp_server.py::_render_status` (~55–122) renders THREE sections —
  the `| Metric | Value | Read |` snapshot table, a one-line "Training load"
  read, and a "Recent workouts" bullet list. Only the snapshot block is a
  table; extract just that `| Metric | Value | Read |` construction into the
  helper and have `_render_status` call it, leaving the training-load line and
  workouts list as-is.
- The brief's `details` tables and the chat house style both call it →
  identical look everywhere, **consistent by construction**, and fewer model
  output tokens (the model embeds the canonical table instead of hand-laying
  ASCII), which also feeds the speed goal.
- Tighten the `prompts.py` "Formatting your chat replies" block with **2–3
  worked examples** of good vs bad output (table, per-item line list, and a
  one-line answer) so the agent has concrete targets, not just rules.

## API surface

New / changed internal interfaces (Python, in `src/local_fitness/agent/`):

```python
# render.py (new) — deterministic, no model, no PHI in logs
def render_table(headers: list[str], rows: list[list[str]], *, max_width: int = 70) -> str: ...
def render_snapshot_table(status: dict) -> str:  # reused by mcp_server._render_status

# briefing_data.py (new) — the ASSEMBLE stage, pure code. This is the BULK of
# the Phase 1 code-only work, NOT a thin wrapper over assemble_status().
def build_digest(user_name: str, daily_step_goal: int) -> BriefDigest: ...
#   BriefDigest: snapshot, training_load, workouts_14d, trends{...},
#                anomalies, plan_status, recent_briefs_summary, rendered_tables
#   Contract — what comes from where:
#   - snapshot + training_load: from assemble_status() (status.py:194-215). Note
#     assemble_status's snapshot trend arrows are a 7-day window
#     (_TREND_WINDOW_DAYS, status.py:49,129); they do NOT supply the 14d trends.
#   - workouts_14d: assemble_status() caps recent_workouts at 5 (status.py:164),
#     so build_digest extends the window to 14 days via the extracted
#     _query_workouts helper (days=14).
#   - trends{sleep,steps,rhr,body_battery_max,avg_stress}: from the extracted
#     _metric_trend helper at days=14 (independent of assemble_status's 7-day
#     snapshot arrows). anomalies (rhr): extracted _find_anomalies helper.
#     plan_status, recent_briefs_summary: as below. NET-NEW assembly, ~2/3 of
#     the digest.
#   - EXTRACT-THEN-REUSE (this is real reuse, but it must be CREATED first):
#     the metric-trend / anomaly / workout queries are authored INLINE in the
#     @tool bodies (tools.py ~206-251, 272-300, 386-415) — there is NO plain
#     query layer to call. Phase 1 extracts plain helpers (_metric_trend(conn,
#     metric, days), _query_workouts(conn, days), _find_anomalies(conn, metric))
#     out of those bodies so BOTH the @tool wrapper AND build_digest call the
#     same helper (this de-risks the tools slightly too). The OTHER two are
#     genuine reuse TODAY: get_training_plan_status already delegates to
#     plans.build_plan_status (tools.py ~1184), and continuity is
#     briefs._recent_briefs_summary (briefs.py:270, a real plain function).
#     All run the underlying DB queries directly, NOT model tool round-trips.

# briefing.py — orchestration (signatures preserved for callers)
async def generate_streaming(model: str = DEFAULT_MODEL, save: bool = True): ...  # now map-reduce internally; batch (gather) under the hood. Still yields a terminal done/error event for back-compat (the only event _generate drains); the intermediate takeaway/index events need NOT be preserved.
def generate_and_save(model: str = DEFAULT_MODEL) -> Path: ...                    # unchanged signature

# internal stages (new, private)
async def _plan_slate(digest, *, model) -> list[ThemePlan]: ...
async def _compose_card(theme: ThemePlan, digest, *, model) -> Takeaway | None: ...  # None when the card fails to validate (dropped in reduce)

# prompts.py (new builders + shared JSON-contract helper)
def brief_json_contract(user_name: str) -> str: ...   # NEW shared helper: the
#   single source of the JSON-shape block — the `<one of: ...>` metric enum, the
#   four tone literals, "exactly ONE key: takeaways", and the FIXED/NON-NEGOTIABLE
#   markers, with {user_name} interpolated inside (today woven into the
#   briefing_prompt f-string at prompts.py ~485, ~491). Lifted OUT of
#   briefing_prompt() into this helper.
#   NOTE: the four scorer anchors are SPREAD OUT, not all inside the literal JSON
#   {...} example — "FIXED and NON-NEGOTIABLE" prose (~467), the metric
#   `<one of: ...>` block (~488), the tone literals (~486), and "exactly ONE key"
#   (~501). brief_json_contract() MUST capture ALL FOUR (not just the JSON
#   example), or the repointed score_prompt silently loses an anchor.
def planner_prompt(digest_text: str, ...) -> str: ...
def card_prompt(theme: str, digest_text: str, continuity: str, ...) -> str: ...
# BOTH briefing_prompt() AND card_prompt() interpolate brief_json_contract(user_name)
# so there is ONE source of the JSON shape and no drift. There is no extractable
# seam in today's f-string (the block is woven in with {user_name} inline at
# ~485/~491), so this is a REAL minor refactor of briefing_prompt() — lift the
# block into the helper, have briefing_prompt() call it — NOT a byte-for-byte
# "retain verbatim" guarantee.
```

**`score_prompt.py` coupling — decision (DECIDED: extract a shared
`brief_json_contract()` helper; repoint the scorer at it).** `score_prompt.py`
imports the real package and asserts on specific strings inside
`briefing_prompt()`'s JSON-output section (`prompts.py` ~465–501): the
"non-negotiable"/"fixed" markers (`score_prompt.py` ~103–104), "exactly one key"
(~107), the metric `<one of: ... >` block (~65, ~111–113), and the four tone
literals (~114–116). The refactor introduces `card_prompt()`/`planner_prompt()`,
so without an explicit decision `score_prompt.py` would either silently validate
now-dead text or break — yet this doc lists it as a passing gate to re-run on every
prompt edit.

**Be honest about why "retain verbatim, no scorer change" does not work.** That
JSON-shape block is woven into ONE ~370-line f-string (`prompts.py` 176–546) with
`{user_name}` interpolated INSIDE the block (~485, ~491). There is **no extractable
seam**: to share it between `briefing_prompt()` and `card_prompt()` you would have
to either (a) extract a shared constant — which EDITS the very text
`score_prompt.py` regex-matches (`score_prompt.py:56,65,107–116`), breaking any
"byte-for-byte verbatim" guarantee — or (b) string-slice `briefing_prompt()`'s
rendered output, which is brittle and self-referential. So the round-4
"retain `briefing_prompt()` VERBATIM + no scorer change" promise was over-promised;
it collides with sharing one source.

**Decision: extract ONE shared JSON-contract source.** Lift the schema/JSON-contract
block into a module-level helper in `prompts.py` — `brief_json_contract(user_name)`
(or a `_BRIEF_JSON_CONTRACT` template the two render) — that owns the `takeaways`
one-key rule, the `<one of: ...>` metric enum, the four tone literals, and the
FIXED/NON-NEGOTIABLE markers. BOTH `briefing_prompt()` AND `card_prompt()`
interpolate it, so there is ONE source of the JSON shape and no drift.
`briefing_prompt()` is **minorly refactored**: the block is lifted out and
`briefing_prompt()` calls the helper (its output text is unchanged in content, but
it is no longer the literal home of the contract — so this is NOT "verbatim
retention"). **Repoint `score_prompt.build_checks()` at the shared helper:**
`build_checks()` calls `prompts.brief_json_contract("TestRunner")` and runs its
non-negotiable/fixed / "exactly one key" / metric-`<one of: ...>` / tone-literal
assertions against THAT (a SMALL, well-defined scorer change at `score_prompt.py:56`
and the assertion bodies — NOT "no change"). The assertions stay meaningful because
they now target the single contract source both prompts inherit. (Rejected
alternative — string-slicing the rendered `briefing_prompt()` output — is brittle
and self-referential; we reject it for the explicit helper.) **Task:** add a unit
assertion that BOTH `card_prompt()` and `briefing_prompt()` contain the shared
contract (same `brief_json_contract` source) AND that `score_prompt` targets that
same source, so the single-source invariant can't silently rot.

`ThemePlan`: `{theme: Literal["workout","steps","conditioning","hr","wildcard"],
chart_metric: MetricName | None, days: int, is_lead: bool, emit: bool,
merge_into: str | None, merge_directive: MergeDirective | None}`.
`MergeDirective`: `{signal: str, read: str}` — the planner attaches this to the
TARGET theme's plan (e.g. on `workout`) when another theme merged into it. It
carries the absorbed theme's key data signal + one-line editorial read (NOT
prose); the target composer writes the merged sentence under its inherited
mandate slice (see "Merge handoff"). Empty/absent on themes that absorb nothing.

**Schema-safe planner output (hazard: `save_brief` validates the whole Brief
atomically — one bad card fails the entire brief, which would violate the
"ship remaining valid cards (≥1)" invariant).** Constrain the planner so it
can't emit a card the `Takeaway`/`TakeawayMetric` schema will reject, and
validate defensively in reduce:

- `chart_metric` is restricted to `MetricName | None` (the `schemas.py`
  Literal) — out-of-enum values are rejected at planner-output parse time.
- `days` is constrained `ge=7` (matching `TakeawayMetric.days = Field(14,
  ge=7, le=730)`). The conditioning mandate's "last 3–5 sessions" framing must
  NOT leak into `days` as `<7`; the planner's `days` is the chart window, not a
  session count. If a value below 7 ever arrives, **clamp to 7** in reduce
  rather than failing the card.
- In **reduce**, validate each composer's output **individually** as a bare
  `Takeaway` (`Takeaway.model_validate(...)`) BEFORE assembling the `Brief`.
  **Drop-on-invalid applies ONLY to OPTIONAL themes** (`conditioning`, `hr`,
  `wildcard`): if one raises `ValidationError`, log and drop it.
- **The two ALWAYS-REQUIRED themes — `workout` AND `steps` — are guaranteed
  survivors and are NOT dropped on validation failure.** Both are mandated in
  every brief (`workout` is the required card; `steps` is REQUIRED per the steps
  mandate, `prompts.py` ~191, ~211, ~335–336). A missing mandated card cannot
  satisfy the schema's intent, so on a validation failure of `workout` or
  `steps`, reduce **RETRIES/REGENERATES that one card once** (re-invoke its
  composer) rather than dropping it. Only if the regeneration ALSO fails does
  the card fall away — a total-failure last resort.
- **Workout-only floor of 1 is a last-resort failure mode, not normal
  degradation.** It is reached only if steps regeneration also fails (and every
  optional card was dropped). In the normal case the floor is **2** (workout +
  steps). `Brief.takeaways` (`min_length=1`) therefore always holds — guaranteed
  by `workout` even in the steps-regeneration-failed total-failure case.

**Per-card parsing (dead code to remove).** With map-reduce, each composer
returns a small standalone JSON object — a single takeaway, not a
`{"takeaways": [...]}` blob. The monolithic streaming-parse machinery becomes
dead and is removed:

- `_iter_partial_takeaways` (`briefing.py` 68–106) — deleted; nothing parses a
  growing `"takeaways": [...]` array anymore.
- `include_partial_messages=True` (`briefing.py` 147) — deleted; no mid-token
  streaming is consumed.
- The `StreamEvent` / `content_block_delta` / `text_delta` branch
  (`briefing.py` 189–215) — deleted; composer output is read from the
  end-of-turn `AssistantMessage` TextBlock(s) per call.

Each per-card object is validated by reusing `briefs._extract_json` for salvage
(fence-strip, control-char repair, bracket scan) and then
`Takeaway.model_validate`. The Brief-level salvage helpers in `briefs.py`
(`_salvage_takeaways`, full `save_brief`) stay for the single final write but
are no longer asked to repair a streamed monolithic blob.

**This removal is also consistent with the single-call FALLBACK path.** All
three deletions above are streaming-only constructs. The fallback composes all
cards in ONE non-streaming `query()` and reads the end-of-turn `AssistantMessage`
TextBlock(s), then runs `briefs._extract_json` — it never iterates a growing
array (`_iter_partial_takeaways`), never sets `include_partial_messages=True`,
and never reads a `StreamEvent`/`text_delta`. So Phase 2's unconditional removal
of the streaming machinery holds whether Phase 0 selects fan-out OR the fallback.

## Invariants

**Checkable by inspection**
- `generate_streaming` / `generate_and_save` keep their current signatures so
  the real callers stay source-compatible. The actual callers are `cli.py`
  (`generate_and_save`) and `ab_brief.py` (via `briefing._generate`, which
  drains `generate_streaming`). `server.py` does **not** call either — it only
  reads the cached brief via `briefs.load_today()` / `load_latest()`.
  `mcp_server.py` does **not** call either — it has its own `_brief_prompt()`
  and only mentions `generate_streaming` in a docstring. (The internal
  `_generate` still drains a `done` event, so map-reduce keeps emitting a
  terminal `done`/`error` for that drainer; the intermediate `takeaway`/`index`
  events are no longer required by anyone and need not be preserved.)
- Brief output still validates against `schemas.Brief` (1–5 takeaways,
  `min_length=1` / `max_length=5`); schema file unchanged. The required
  workout card guarantees `min_length=1`.
- Both always-required themes (`workout` AND `steps`) are regenerated-once on
  validation failure, never dropped; only optional themes (`conditioning`,
  `hr`, `wildcard`) are drop-on-invalid. Normal degradation floor is 2; the
  workout-only floor of 1 is the steps-regeneration-failed last resort.
- Composer agents use `read_only_tool_names()` only — no mutation tools in the
  brief path (existing security invariant).
- Every always-required theme (`workout` AND `steps`) is present and
  `emit=true` in every slate.
- `render_table` never embeds a newline or multi-item list inside a cell.

**Requires tests**
- `card_prompt()` and `briefing_prompt()` both contain the shared
  `brief_json_contract()` source (same single JSON-shape source, no fork), and
  `score_prompt.build_checks()` targets that same `brief_json_contract()` source —
  so the contract, the two prompts, and the scorer can't drift apart.
- Map-reduce brief clears `scripts/score_prompt.py` (no failed checks), where
  `score_prompt` now asserts against `brief_json_contract()` (the repointed gate).
- `scripts/ab_brief.py --run` structural fingerprint (steps takeaway present,
  tone distribution, plan-folded-in) stays within the same tolerance band as the
  monolithic brief, across sonnet + opus — **with the takeaway-count checks
  adjusted per "ab_brief reconciliation" below** so the gate does not false-fail
  correct fan-out behavior. (Structural regression net only — NOT the quality
  verdict; see the quality judge below.)
- Blocking quality gate (automated, per the "never eyeball prompt changes"
  rule): (a) the structural net (`ab_brief.py --run`, relaxed per Significant 3),
  (b) the automated per-card LLM-judge clears its bar across N paired days, AND
  (c) the cross-model A/B (`ab_brief.py --run` across sonnet + opus) — all three
  must pass. The manual paired read of N≥5 days is RETAINED as supplementary
  human corroboration, not the sole blocker.
- Wall-clock target validated on a representative day (instrumented via the
  existing `brief_timing phase=summary total_ms`), measured against the Phase 0
  baseline — treated as a target, not a pre-asserted guarantee.
- Cards gathered out of order still land in planner order in the saved brief.
- Per-card validation drops only invalid OPTIONAL cards: a composer for
  `conditioning`/`hr`/`wildcard` emitting a bad `days`/`metric`/shape is dropped
  in reduce; the remaining valid cards (≥1, always including workout) still ship
  rather than erroring the whole brief.
- A `workout` or `steps` card failing validation is REGENERATED once (not
  dropped); the regenerated card lands in the brief. Only if regeneration also
  fails does the card fall away (workout-only floor = total-failure last resort).

## Quality gate & measurement (per the "measure, don't eyeball" rule)

`ab_brief.py extract_features` only captures `n_takeaways`, the tone
distribution, a `has_steps` substring probe, a `mentions_plan` keyword probe,
and the chart-metric set. It does **not** diff text and **cannot** detect
cross-card repetition or lost editorial judgment. So it is the **structural
regression net, not the quality verdict.**

`scripts/score_prompt.py` is a third, narrower gate: it imports the real
`local_fitness` package and runs **static prompt/schema consistency checks** —
it never calls a model (`build_checks()` is pure assertions; the module docstring
says "no network, no Claude call"). So `score_prompt.py` gives **ZERO
output-quality signal**; it only guarantees the prompts and the `schemas.py`
contract stay consistent. Do not read a green `score_prompt.py` as evidence the
briefs are good.

**`score_prompt.py` is repointed at the shared `brief_json_contract()` helper (see
"score_prompt.py coupling — decision" in API surface).** The JSON shape is extracted
out of `briefing_prompt()`'s f-string into a single module-level
`brief_json_contract(user_name)` source that BOTH `briefing_prompt()` and
`card_prompt()` interpolate. `build_checks()` is **minorly changed** to assert its
"non-negotiable"/"fixed", "exactly one key", metric `<one of: ...>`, and tone-literal
checks against `brief_json_contract()` instead of the rendered `briefing_prompt()`
(`score_prompt.py:56` + the assertion bodies). This is a SMALL, well-defined scorer
repoint — NOT "no scorer change." The assertions stay meaningful because they now
target the single contract source both prompts derive from. The added safeguard is a
unit check that `card_prompt()` and `briefing_prompt()` both contain that shared
source and that `score_prompt` targets it, so the three can't drift.

**The ship decision: which gates actually block (automated, per the
"never eyeball prompt changes" rule).** A prompt/model change of this size cannot
ship on a human read alone — the standing project rule is that prompt/model
changes are validated by an AUTOMATED scorer + cross-model A/B. So the blocking
quality gate is automated and has three parts, ALL of which must pass:

- **(a) Structural net** — `ab_brief.py --run` (relaxed per Significant 3 below)
  shows no structural regression.
- **(b) Automated LLM-judge** — the per-card LLM-judge (fully specified below:
  model, rubric, pass bar) clears its bar across N paired days. This is the
  automated output-quality blocker. It is NOT advisory; it is already specified
  concretely enough to ship as the gate the project rule requires.
- **(c) Cross-model A/B** — `ab_brief.py --run` across sonnet + opus (the
  cross-model arm), so the change holds on both authoring models, not just one.

**The manual paired read of N≥5 days is RETAINED as supplementary human
confirmation** (Nate reads the monolithic and fan-out brief for the same day,
side by side, arm labels stripped) — corroboration, NOT the sole blocker. It
catches subtle drift the judge's loose threshold may miss (see Significant 3),
but the automated judge + cross-model A/B is what gates the ship.

The per-card LLM-judge is specified concretely below — it is the automated
blocker, not a placeholder or a future sub-project:

1. **Baseline** (before any change): `uv run python scripts/ab_brief.py --run
   --runs 1` to capture the current structural fingerprint, plus the Phase 0
   real-latency / token instrumentation (below) for the wall-clock baseline, plus
   the **N≥8 frozen monolithic quality baselines** (Phase 0.4) that the per-card
   judge later compares against — captured from today's monolithic generator before
   Phase 2 removes it.
2. Implement behind the same entry points; re-run `score_prompt.py` (free,
   no model) on every prompt edit.
3. **Structural A/B**: `ab_brief.py --run` (sonnet + opus × 2) comparing
   monolithic vs map-reduce fingerprints. A necessary-but-not-sufficient gate.
4. **Per-card quality judge (AUTOMATED BLOCKER — see "which gates block" above).**
   A new script (NOT `ab_brief.py`) that runs an LLM-judge pass over **paired**
   monolithic-vs-fanout briefs for the SAME days. Fully specified so it ships as
   the automated quality gate:
   - **(a) Judge model:** `claude-opus-4-8`. Rationale: the judge must
     out-discriminate the `claude-sonnet-4-6` model that authors the briefs on
     subtle coach-voice / repetition calls, so use the strongest current model
     for the eval even though it's pricier — it runs ≥8 days once, not daily.
     (`claude-opus-4-8` is the current opus as of this doc; confirm it's still
     current at run time, and bump the stale `ab_brief.py` opus arm to match —
     see the model-id note below.)
   - **(b) Rubric — four dimensions, each scored 1–5** (5 = best):
     *cross-card repetition* (5 = every card earns its space, no restated
     headline/number; 1 = cards echo each other), *specificity* (5 = every claim
     cites a real number/trend from the digest; 1 = vibes), *coach-voice
     fidelity* (5 = matches the mandate voice — direct, numbers-with-implication,
     concrete next move; 1 = generic), *dead-weight* (5 = a sharp coach would
     cut nothing; 1 = filler card present). Scores are assigned **per brief**
     (aggregating its cards), per arm, per day.
   - **(c) Pass bar — a GROSS-regression catch, stated honestly (not a
     statistical test).** Average each dimension over **N≥8 paired days** and
     require **fan-out mean ≥ monolithic mean − ε on EVERY dimension**, with
     **ε = 0.3 on the 1–5 scale**. Be honest about what this is: ε = 0.3 is a
     deliberately LOOSE threshold chosen to catch only GROSS regressions — it is
     NOT a noise-controlled significance test, and we make no claim that it
     "absorbs day-to-day noise." At N≈8–10 with LLM-judge variance, this bar
     reliably flags only large drops; subtle drift is NOT caught here — that's
     what the supplementary manual read (Significant 2) and the cross-model A/B
     are for. **Multiple-comparisons caveat:** the bar is one-sided across 4
     dimensions, so the per-dimension false-pass rates compound — another reason
     to treat the judge as a gross-regression net, not a fine-grained verdict.
     Pairing (same day, both arms) controls for day difficulty. Strip arm labels
     before judging (blind where feasible).
   - **(d) This requires BOTH arms per comparison day — and the control arm is the
     CURRENT monolithic generator, captured BEFORE the Phase-2 refactor.** The
     control ("before") arm is **N≥8 monolithic baseline briefs GENERATED AND FROZEN
     in Phase 0**, while today's monolithic generator is still the live code — i.e.
     the genuine before/after baseline. These frozen briefs are stored to disk and
     re-loaded by the Phase-4 judge; they are NOT regenerated after Phase 2 deletes
     the monolithic path. The fan-out (or fallback) "after" arm is generated in
     Phase 4 from the new path for the SAME N≥8 days. **Do NOT confuse the
     monolithic baseline with the single-call fallback** — they are different
     artifacts measuring different things: the *monolithic baseline* is today's code
     (the control), whereas the *single-call fallback* is a NEW assemble-in-code +
     one-`query()` path that only exists if Phase 0's kill criterion fires. If the
     kill criterion fires, the "after" arm is the fallback's output, but the control
     it is compared against is STILL the Phase-0-frozen monolithic baseline, never
     the fallback. (Alternative, stated for honesty: if the monolithic baselines are
     NOT frozen in Phase 0 and instead the comparison is fallback-vs-fan-out, the gate
     measures a weaker/different signal — two new paths against each other, with no
     real before/after control. We REJECT that and freeze real monolithic baselines
     in Phase 0.) **Cost (quoted + hard-capped up front, per the doc's cost
     discipline):** N≥8 frozen monolithic baselines are generated ONCE in Phase 0
     (8 generations), then in Phase 4 N≥8 fan-out briefs (8 generations) + 8 judge
     passes; at N=10, 10 + 10 generations + 10 judge passes. The two arms are NOT
     both generated in Phase 4 — the control arm is already on disk from Phase 0.
     Quote and hard-cap each script's generation count exactly like `ab_brief` before
     running.

**Note on model ids (confirm before the run):** today's current opus is
**`claude-opus-4-8`**. `ab_brief.py` `DEFAULT_MODELS` still pins the opus id as
`claude-opus-4-7` (line 37) — **likely stale**. Both the **cross-model A/B opus
arm** and the **LLM-judge** MUST use a confirmed-current opus id: bump
`ab_brief.py`'s `claude-opus-4-7` to `claude-opus-4-8` (or the then-current alias)
before relying on the opus arm, and verify the judge's `claude-opus-4-8` is still
current at run time. A stale/invalid id would silently skew or fail the
comparison.

**Cost (quoted up front, hard cap respected):** `ab_brief.py` caps at
`MAX_GENERATIONS = 8`. A full structural A/B (2 models × 2 runs = 4 briefs) per
round, ~2 rounds ≈ 8–16 brief generations. The per-card judge's two arms are
captured at DIFFERENT times: **N≥8 monolithic baselines are generated and frozen
ONCE in Phase 0 (≥8 generations), then N≥8 fan-out briefs are generated in Phase 4
(≥8 generations)**, plus N judge passes — so ≥16 generations total, but split across
phases, not 16 in one Phase-4 run. Quote and cap that script the same way before
running. The daily brief itself goes from 1 call to ~6 calls/day (1 planner + up to
5 cards) — run once daily.

**ab_brief reconciliation (required before the structural net is trusted).**
`compare()` (`ab_brief.py` ~62–86) currently hard-fails two count checks that
**collide with intended fan-out behavior** and would false-fail correct briefs:

- `n_takeaways < 3` → flagged (~72). But the planner LEGITIMATELY ships 3 (the
  "ship 3 if only 3 are sharp" rule, `prompts.py` ~243) and may drop invalid
  OPTIONAL cards in reduce to fewer than 3 — the normal degradation floor is **2**
  (workout + steps, both regenerated-not-dropped per Significant 1), with the
  workout-only floor of 1 reserved for the total-failure case. A count of 2 (or 1
  in total failure) is now correct degradation, not a defect.
- count-variance `max − min > 1` across runs → flagged (~74). But planner-driven
  ship-N produces **expected** day-to-day and run-to-run count variation; this
  is the design, not noise.

**Required change to `ab_brief.py` (implementation, not this design — specified
here so the refactor doesn't reject correct behavior):**

- **Drop count from the parity criterion.** Remove the `c < 3` lower bound and
  the `> 1` count-variance check from `compare()`. Keep only the schema-enforced
  upper bound (`c > 5`) as a flag, since `Brief.max_length=5` makes >5 a genuine
  bug. Count is no longer a parity dimension.
- **Keep, unchanged:** the steps-takeaway-present check (~67–69), the
  tone-distribution fingerprint, and the plan-folded-in check (~77–84). These
  remain valid structural signals. The `has_steps` check stays a **BLOCKING**
  structural net entry — consistent with Significant 1, because `steps` is a
  guaranteed survivor (regenerated, never dropped) so a steps-less brief means a
  genuine total-failure bug, exactly what a blocking check should catch. A
  steps-less brief is never "correct degradation."

Post-refactor, `ab_brief --run` SHOULD flag: a missing mandated steps takeaway,
a brief exceeding 5 cards, plan content leaking when no plan is active (or a plan
not folded in when active). It should NOT flag: a brief shipping 1–3 cards, or
the card count varying across runs/days. The structural net then measures
parity on the dimensions that are actually invariant under fan-out.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| **Concurrent `query()` calls serialize (no real parallelism).** Each `query()` drives a Claude Code CLI subprocess on the Max-subscription OAuth path (`ab_brief.py` runs on `CLAUDE_CODE_OAUTH_TOKEN`). If subprocesses or rate limits serialize them, map-reduce is SLOWER than today (planner + N serial composers > 1 monolithic call). | **Phase 0 concurrency test with an explicit KILL CRITERION** (below). KILL if 3 concurrent `query()` calls show **< 1.7× wall-clock speedup vs the same 3 run serially**; below that, **abandon fan-out** and take the single-call fallback. |
| Latency root cause isn't what we think | Phase 0 measures actual output/thinking token usage from the SDK result message AND per-round-trip latency on a real `uv run fitness brief` BEFORE committing to any lever. If the cost isn't in thinking / serial round-trips, fan-out doesn't apply. |
| Planner adds a serial step before fan-out | Planner output is tiny + thinking-capped. Measure in Phase 0; if it dominates, fold theme-selection into code thresholds. |
| Cross-card repetition / tonal drift | Composers receive other emitted themes' headline-intents (read-only) to avoid duplication; planner owns merge/ship-3; continuity slice per theme. Per-card quality judge (not just tone-distribution) is the catch. Contingency: a reduce-stage dedup model pass if the judge still flags repetition. |
| Per-card thinking still slow | Explicit thinking budget per composer, tuned against the judge + A/B. |
| Higher per-brief cost | ~6 calls/day, once daily; quoted above. |
| Deterministic tables feel rigid / lose coach voice | Tables are for `details` data only; headline/summary/prose stay model-authored. |

**Fallback (if Phase 0 kills fan-out):** "assemble-data-in-code + single
call." Keep the ASSEMBLE-in-code win (kills the serial tool round-trips) and
the deterministic table renderer, but compose all cards in ONE `query()` with
a measured, capped thinking budget — no planner, no concurrent composers. This
recovers whatever latency the round-trips + uncapped thinking were costing
without betting on subprocess parallelism.

## Phasing

- **Phase 0 — REAL measurement, gates everything after it.**
  1. *Latency localization.* Instrument a real `uv run fitness brief` to
     capture actual **output + thinking token usage from the SDK result
     message** (not the `ab_brief.py` estimate constants) AND **per-round-trip
     latency** (time between successive model turns / tool results) so the
     silent block is split into thinking vs serial round-trips.
  2. *Concurrency test.* Run 3 concurrent `query()` calls and measure
     wall-clock vs the same 3 calls run serially. **KILL CRITERION (concrete):**
     if the 3 concurrent calls show **< 1.7× wall-clock speedup** over serial
     (i.e. they serialize on subprocess / rate limits), abandon the fan-out and
     switch to the single-call fallback (see Risks) for the rest of the phases.
     **Note the test is at concurrency 3, but production emits up to 5 composers
     + 1 planner.** Serialization can be load-dependent (subprocess / rate-limit
     pressure rises with concurrency), so if the 3-concurrent test passes, run a
     **5-concurrent confirmation** before relying on full fan-out — a 3-wide pass
     does not guarantee a 5-wide one holds.
  3. *Baseline fingerprint.* `ab_brief.py --run --runs 1` for the structural
     baseline.
  4. *Freeze the monolithic quality baselines.* Generate **N≥8 briefs from the
     CURRENT monolithic generator (today's live code, pre-refactor) and store them
     to disk** as the frozen control arm for the Phase-4 per-card judge. This MUST
     happen before any Phase-2 deletion of the monolithic path, because Phase 2
     replaces that path (whether Phase 0 selects fan-out or the fallback) and the
     real before/after control disappears with it. These frozen briefs — NOT the
     single-call fallback's output — are the judge's "before" arm.
  Re-derive the ≤~90s target from these numbers as a hypothesis to validate,
  not a guarantee. Do not proceed to Phase 2's fan-out until 0.1 confirms the
  latency is attackable by parallelism/thinking-cap AND 0.2 passes.
- **Phase 1** — `render.py` deterministic tables + `briefing_data.build_digest`
  (code-only, no model behavior change yet; unit-tested). This is the **bulk of
  the code-only work**, not a thin wrapper: `assemble_status()` gives only the
  snapshot + training-load (and its 5-workout cap must be widened to 14 days; its
  snapshot trend arrows are a separate 7-day window, status.py:49,129, and do NOT
  supply the 14d trends), so ~2/3 of `build_digest` is net-new assembly — the 14d
  trends, anomalies, plan status, 14d workouts, and continuity. **First extract
  plain query helpers** (`_metric_trend` / `_query_workouts` / `_find_anomalies`)
  OUT of the inline @tool bodies (tools.py ~206-251, 272-300, 386-415) so BOTH the
  @tool wrapper AND `build_digest` call the same helper — this extraction-refactor
  is part of Phase 1's scope, NOT free reuse of something that exists, and it
  de-risks the tools slightly. The plan-status + continuity reuse is genuine today
  (`plans.build_plan_status` via `get_training_plan_status`, tools.py ~1184;
  `briefs._recent_briefs_summary`, briefs.py:270). All compute in code via the
  underlying DB queries (NOT model round-trips). Useful in BOTH the fan-out path
  and the fallback.
- **Phase 2** — EITHER map-reduce orchestration in `briefing.py` (planner +
  parallel composers + per-card-validating reduce) **if Phase 0 passed**, OR
  the single-call fallback (assemble-in-code + one capped-thinking `query()`)
  **if the kill criterion fired** — behind the existing entry points. Remove
  the now-dead streaming machinery (`_iter_partial_takeaways`,
  `include_partial_messages`, the `StreamEvent` branch).
- **Phase 3** — Thinking budgets + house-style worked examples; tune via the
  per-card judge + A/B.
- **Phase 4** — Quality gate (scorer + structural A/B + per-card judge). The
  per-card judge compares Phase-4 fan-out briefs against the **N≥8 monolithic
  baselines frozen in Phase 0.4** (the before/after control), NOT against the
  single-call fallback. Then container rebuild + devlog + version bump per release
  policy.

## Acceptance criteria

- `uv run fitness brief` meets the Phase 0-derived wall-clock target on a
  normal day (≤ ~90s if Phase 0 confirms it's reachable; otherwise the revised,
  measured target).
- `score_prompt.py` passes (prompt/schema consistency only — no output-quality
  signal). The automated blocking gate passes: (a) `ab_brief.py --run` structural
  parity (relaxed per Significant 3), (b) the automated per-card LLM-judge clears
  its bar across N paired days, AND (c) the cross-model A/B (`ab_brief.py --run`
  across sonnet + opus). The supplementary manual paired read of N≥5 days shows
  no quality regression (corroboration, not the sole blocker).
- Brief `details` tables and chat tables render identically via one helper.
- The brief is batch-generated to disk and the UI polls the cached
  `GET /api/brief` (no progressive render); the saved brief is in planner order.
- An OPTIONAL composer failing (or emitting an invalid card) degrades to a
  smaller valid brief (normal floor = 2: workout + steps), never a hard error. A
  failed `workout`/`steps` card is regenerated once rather than dropped; the
  workout-only floor of 1 is reached only if steps regeneration also fails.

## Implementation notes (minor)

Low-risk, implementation-time grounding for whoever picks this up:

- **SDK affordances available as belt-and-suspenders (optional).**
  `claude_agent_sdk`'s `ClaudeAgentOptions` exposes `output_format`
  (structured / json-schema output) and `max_budget_usd` (hard cost cap).
  Neither is required. `output_format` *could* tighten the planner's
  structured-slate parse (`_plan_slate` → `list[ThemePlan]`) instead of
  relying solely on `_extract_json` salvage; `max_budget_usd` is a
  belt-and-suspenders cost cap on the eval / A-B scripts that complements the
  existing `ab_brief.py` `MAX_GENERATIONS = 8` cap. Treat both as optional
  hardening, not scope.
- **Keep input-bounds validation in the @tool WRAPPER, not the extracted
  helper.** When Phase 1 lifts the inline query bodies into plain helpers
  (`_metric_trend` / `_query_workouts` / `_find_anomalies`), the existing
  guards — e.g. `_validate_days(args["days"], lo=2)` in `get_metric_trend`
  (tools.py ~211) — stay in the `@tool` wrapper, which validates raw user
  input. The plain helper takes already-validated args. This preserves the
  tools' current guards and avoids regressing them; `build_digest` calls the
  helper with code-supplied (already-bounded) values.
- **Back-compat test point: the terminal `done` event must carry a brief
  dict with a `date` key.** `generate_and_save` reconstructs the output path
  as `DEFAULT_BRIEFINGS_DIR / f"{last_brief['date']}.json"` (briefing.py
  ~371). The map-reduce reduce step MUST ensure the terminal `done` event's
  brief dict still has a `date` key (server-stamped by `save_brief`) so this
  line keeps working. Add an explicit test asserting the `done` event's brief
  has `date` and that `generate_and_save` returns the matching path.

## Revision log (QG round 1)

- **FATAL 1** (phantom streaming) — Dropped the "keep streaming UX" goal and
  the progressive-render invariants; added a non-goal stating the brief is
  batch-generated to disk and polled via cached `GET /api/brief`, with live
  streaming marked OUT-OF-SCOPE (future SSE + EventSource). Reframed REDUCE as
  `asyncio.gather` + validate/save once. (Goals/non-goals, Architecture,
  Invariants, Acceptance criteria.)
- **FATAL 2** (unmeasured latency) — Rewrote Problem & evidence: labeled the
  40k tokens an unverified `ab_brief.py` cost-estimate constant, corrected the
  `tool_duration_sum_ms`=13ms claim to "DB time only, not inter-call model
  round-trips," and reframed the silent block as unknown (thinking vs serial
  round-trips). Made Phase 0 capture real SDK token usage + per-round-trip
  latency; ≤90s is now a hypothesis. (Problem & evidence, Phasing.)
- **SIGNIFICANT 3** (coarse quality gate) — Demoted `ab_brief` to a structural
  regression net and added an LLM-judge per-card quality gate (repetition /
  specificity / coach-voice / dead-weight) over ≥5 paired blind days.
  (Quality gate & measurement, Invariants.)
- **SIGNIFICANT 4** (hand-waved quality) — Specified the mechanism: planner
  owns ship-3 (`emit`) and merge (`merge_into`); composers get other themes'
  headline-intents for dedup + continuity slice; documented how each named
  global rule (~243/~450/~411/~166) survives the split; dropped the bare
  "quality should improve" assertion. (Architecture.)
- **SIGNIFICANT 5** (schema hazard) — Constrained `ThemePlan.days` to ge=7
  (clamp in reduce), `chart_metric` to `MetricName`, added per-card individual
  `Takeaway` validation in reduce with drop-on-invalid, and guaranteed the
  workout card survives so `Brief.min_length=1` holds. (API surface.)
- **SIGNIFICANT 6** (dead code) — Explicitly scoped removal of
  `_iter_partial_takeaways`, `include_partial_messages`, and the
  `StreamEvent`/`content_block_delta` branch; documented per-card parsing
  (`_extract_json` salvage → `Takeaway.model_validate`). (API surface, Phasing.)
- **SIGNIFICANT 7** (unproven parallelism) — Added a Phase 0 concurrency test
  with an explicit kill criterion and a single-call fallback
  (assemble-in-code + one capped-thinking `query()`) in the risk table and
  phasing. (Risks, Phasing.)
- **Minor — caller list** — Corrected: real callers are `cli.py` and
  `ab_brief.py` (via `_generate`); `server.py` and `mcp_server.py` do NOT call
  `generate_streaming`. (Invariants.)
- **Minor — render_snapshot_table boundary** — Clarified `_render_status`
  renders three sections; only the `| Metric | Value | Read |` block is the
  table to extract. (Dimension 2.)
- **Minor — opus id** — Added a note to confirm `DEFAULT_MODELS`'
  `claude-opus-4-7` is current before relying on the opus A/B arm. (Quality
  gate & measurement.)
- **Minor — "nearly free"** — Removed; reframed the thinking budget as a
  latency lever (thinking tokens are the suspected latency driver), not a free
  add-on. (Architecture "why it's fast", Thinking budget.)

### QG round 2

- **SIGNIFICANT 1** (asserted merge handoff) — Specified the handoff concretely:
  a merged theme produces no composer; the planner attaches a structured
  `merge_directive {signal, read}` (data + one-line read, NOT prose) to the
  TARGET theme's plan; the target composer inherits the HR & recovery mandate
  slice (`prompts.py` ~418–459, the "ONE clear lead signal" rule + all-green
  roll-in) and writes the merged prose itself. Added a "Merge handoff"
  subsection, updated the COMPOSE box, the merge survival-rule bullet, and the
  `ThemePlan`/`MergeDirective` API surface. (Architecture, API surface.)
- **SIGNIFICANT 2** (placeholder judge / hidden sub-project) — Picked
  advisory-judge + manual-blocker for v1 and said so explicitly: the blocking
  gate is the structural net + a defined manual paired read of N≥5 days (honest
  that the manual read is the blocker). Made the judge buildable anyway —
  `claude-opus-4-8` (with rationale), 1–5 rubric per dimension, pass bar
  (fan-out mean ≥ monolithic mean − ε=0.3 over N≥5 paired days), and the
  requirement to generate BOTH arms (paired monolithic via the Phase-0 fallback
  path). Clarified `score_prompt.py` gates prompt/schema consistency only, ZERO
  output-quality signal. (Quality gate & measurement, Invariants, Acceptance.)
- **SIGNIFICANT 3** (ab_brief false-fails) — Added an "ab_brief reconciliation"
  block specifying the required `compare()` change: drop the `c < 3` lower bound
  and the count-variance `> 1` check (both collide with planner-driven ship-N +
  graceful drop-to-fewer); keep only `c > 5` (schema bug) plus the
  steps-present, tone, and plan-folded checks. Stated exactly what the net
  should and should not flag post-refactor. (Quality gate & measurement,
  Invariants.)
- **Minor — fallback also drops streaming machinery** — Noted the three
  deletions are streaming-only and the single-call fallback (non-streaming
  `query()` + `_extract_json`) never used them, so Phase 2's unconditional
  removal holds under both paths. (API surface "Per-card parsing".)
- **Minor — back-compat wording** — Resolved the contradiction once:
  `generate_streaming` keeps yielding only a terminal `done`/`error` (the sole
  event `_generate` drains); intermediate `takeaway`/`index` events need not be
  preserved. (API surface signature comment; Invariants already aligned.)
- **Minor — concrete kill criterion** — Set the Phase 0 concurrency
  kill-criterion to a number: < 1.7× wall-clock speedup for 3 concurrent
  `query()` calls vs serial → abandon fan-out. (Risks, Phasing.)

### QG round 3

- **SIGNIFICANT 1** (steps-mandate vs drop-to-floor contradiction) — Made the
  degradation invariant consistent with the `has_steps` blocking check: BOTH
  always-required themes (`workout` AND `steps`) are now guaranteed survivors
  that are REGENERATED once on validation failure, never dropped; only optional
  themes (`conditioning`/`hr`/`wildcard`) are drop-on-invalid. Normal floor is 2;
  the workout-only floor of 1 is a steps-regeneration-failed last resort.
  `has_steps` stays a BLOCKING check, now consistent (steps is never legitimately
  absent). (API surface, Invariants, ab_brief reconciliation, planner box,
  Acceptance criteria.)
- **SIGNIFICANT 2** (manual read as sole blocker violates "never eyeball") —
  Promoted the LLM-judge to an AUTOMATED CO-BLOCKER. The blocking quality gate is
  now (a) structural net + (b) automated LLM-judge + (c) cross-model A/B; the
  manual paired read is retained as supplementary human corroboration, not the
  sole blocker. Removed the "advisory / too big for v1" framing — the judge is
  fully specified, so it ships as the gate the project rule requires. (Quality
  gate & measurement, Invariants, Acceptance criteria.)
- **SIGNIFICANT 3** (pass bar not statistically meaningful) — Reframed ε=0.3 as a
  deliberately loose GROSS-regression catch (NOT a noise-controlled test);
  dropped the unsupported "absorbs day-to-day noise" claim; raised N to ≥8 paired
  days; acknowledged the multiple-comparisons issue (one-sided across 4
  dimensions) in one line; quoted the generation cost (N=8 → 16 generations + 8
  judge passes). Noted subtle drift is caught by the supplementary manual read +
  cross-model A/B. (Quality gate & measurement pass-bar + cost.)
- **Minor — 5-concurrent confirmation** — Phase 0 tests concurrency at 3 but
  production emits up to 5 composers + 1 planner; added a line that a passing
  3-concurrent test triggers a 5-concurrent confirmation before relying on full
  fan-out (serialization is load-dependent). (Phasing Phase 0.)
- **Minor — opus id** — Stated today's opus is `claude-opus-4-8` and that
  `ab_brief.py` pins the likely-stale `claude-opus-4-7`; both the A/B opus arm and
  the judge must use a confirmed-current opus id. (Quality gate & measurement
  model-id note + step 4(a).)

### QG round 4

- **SIGNIFICANT 1** (`score_prompt.py` hard-coupled to `briefing_prompt()` text) —
  Resolved explicitly with DECISION (b): `briefing_prompt()` is RETAINED VERBATIM as
  the canonical schema-contract surface; `card_prompt()` composes its JSON-shape
  block from it rather than re-authoring, so `score_prompt.build_checks()` keeps
  calling `briefing_prompt()` (`score_prompt.py:56`) and its
  non-negotiable/fixed/exactly-one-key/metric-one-of/tone assertions stay
  meaningful. No scorer rewrite. Added a "score_prompt.py coupling — decision" block
  to API surface and a paragraph to the Quality gate section; added a unit-check task
  that `card_prompt()`'s schema-shape block is sourced from `briefing_prompt()`.
  (API surface, Quality gate & measurement.)
- **SIGNIFICANT 2** (monolithic baseline vs Phase-2 dead-code removal /
  baseline-vs-fallback conflation) — Specified that the N≥8 monolithic baseline
  briefs are GENERATED AND FROZEN in Phase 0 (new step 0.4) from today's live
  monolithic generator, before any Phase-2 deletion, and stored for the Phase-4
  judge. Corrected step 4(d): the control arm is the Phase-0-frozen monolithic
  baseline, NOT the single-call fallback (a separate NEW path); stated the rejected
  fallback-vs-fan-out alternative explicitly. Made phasing (Phase 0, Phase 4), the
  measurement step-1 baseline, step 4(d), and the cost note consistent with
  capturing N≥8 frozen baselines in Phase 0 (generations split across phases, not
  16 in one Phase-4 run). (Quality gate & measurement, Phasing.)

### QG round 5 (look-harder)

- **SIGNIFICANT 1** (ASSEMBLE under-sized) — Made the ASSEMBLE box + `build_digest`
  contract honest: `assemble_status()` supplies only the snapshot + training-load
  and caps recent_workouts at 5 (status.py:164, must widen to 14d); the 14d trends,
  anomalies, plan status, 14d workouts, and continuity are net-new (~2/3 of the
  digest) computed in code by reusing the tools.py query helpers (NOT model
  round-trips). Kept the "ZERO model tool round-trips" claim; stopped presenting
  assemble_status() as the spine. Resized Phase 1 to "bulk of the code-only work."
  (Architecture ASSEMBLE box, API surface build_digest, Phasing Phase 1.)
- **SIGNIFICANT 2** (over-promised "verbatim + no scorer change") — Replaced the
  round-4 "retain `briefing_prompt()` byte-for-byte + no scorer change" decision
  with a real mechanism: extract a shared `brief_json_contract(user_name)` helper
  in prompts.py (one source of the JSON shape — metric `<one of>`, tone literals,
  exactly-one-key, FIXED/NON-NEGOTIABLE) that BOTH `briefing_prompt()` and
  `card_prompt()` interpolate; minorly refactor `briefing_prompt()` to call it; and
  repoint `score_prompt.build_checks()` to assert against that helper (small,
  well-defined scorer change at score_prompt.py:56 + assertion bodies). Was honest
  that there is no extractable seam in today's f-string ({user_name} woven in at
  ~485/~491) so verbatim retention was impossible. Added the unit assertion that
  card_prompt() + briefing_prompt() share the contract source and score_prompt
  targets it. (API surface coupling decision + builders, Quality gate section,
  Invariants.)

### QG round 6

- **SIGNIFICANT 1** ("reuse tools.py query helpers" contradicts the code) — The
  metric-trend / anomaly / workout queries are authored INLINE in @tool-decorated
  `SdkMcpTool` objects (tools.py ~206-251, 272-300, 386-415), not plain functions —
  there is no query layer to reuse. Re-scoped to EXTRACT-THEN-REUSE: Phase 1 first
  extracts plain helpers (`_metric_trend` / `_query_workouts` / `_find_anomalies`)
  out of those @tool bodies so BOTH the @tool wrapper AND `build_digest` call the
  same helper (this extraction is part of Phase 1's scope, not free reuse, and
  de-risks the tools). Noted the two that ARE genuine reuse today —
  `get_training_plan_status` → `plans.build_plan_status` (tools.py ~1184) and
  `briefs._recent_briefs_summary` (briefs.py:270). (Architecture ASSEMBLE box, API
  surface build_digest contract, Phasing Phase 1.)
- **Minor A** (14d-trend source) — Added that the digest's 14d trends come from the
  extracted `_metric_trend` helper at days=14, independent of `assemble_status()`'s
  7-day snapshot trend arrows (get_today_status recent_days tools.py ~159;
  `_TREND_WINDOW_DAYS` status.py:49,129) — so the 7-day snapshot arrows do not
  satisfy the 14d-trend need. (ASSEMBLE box, build_digest contract, Phase 1.)
- **Minor B** (scorer anchors spread out) — Noted `brief_json_contract()` must
  capture ALL FOUR scorer anchors — "FIXED and NON-NEGOTIABLE" prose (prompts.py
  ~467), tone literals (~486), metric `<one of: ...>` block (~488), "exactly ONE
  key" (~501) — not just the literal JSON {...} example, or the repointed
  `score_prompt` silently loses an anchor. (API surface brief_json_contract helper.)

### QG minor pass

- Added an **"Implementation notes (minor)"** section capturing three
  implementation-time notes: (1) the optional `claude_agent_sdk`
  `ClaudeAgentOptions` affordances `output_format` and `max_budget_usd` as
  belt-and-suspenders hardening (the latter complementing `ab_brief`'s
  `MAX_GENERATIONS` cap); (2) keep input-bounds validation
  (`_validate_days(..., lo=2)`, tools.py ~211) in the `@tool` wrapper, not the
  extracted plain helper, when extracting `_metric_trend` / `_query_workouts`
  / `_find_anomalies`; (3) the terminal `done` event must keep carrying a brief
  dict with a `date` key so `generate_and_save`'s path reconstruction
  (briefing.py ~371) keeps working — added as an explicit test point.
