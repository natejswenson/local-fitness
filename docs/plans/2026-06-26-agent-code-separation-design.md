---
ticket: "#TBD"
title: "Agent/Code Separation — deterministic brief-planner + scoped LLM generator + evals"
date: "2026-06-26"
source: "design"
revision: 4  # quality-gate r1 (6 Fatal) + r2 (~6 Sig) + r3 (consistency) resolved
---

# Agent/Code Separation for the Brief Composer

## Goal

Clear separation between the **non-deterministic agent (LLM)** and the
**deterministic code** (per ["Your Prompt Is Too Big"](https://chasepd.github.io/posts/your-prompt-is-too-big/)):
the reasoning pipeline (context selection, threshold evaluation, planning,
validation) is **deterministic code covered by tests**; the generation stage
(prose from scoped instructions) is the **LLM covered by evals**.

## Honest framing (revised)

This **relocates** determinism out of the brief prompt into testable code — it
does **not** net-simplify the system (it adds `brief_planner.py`, `grounding.py`,
new schemas, fixtures, and an eval harness). The win is *testability and
debuggability*, not fewer moving parts. Realistic prompt reduction is **~25–30%**
(the cleanly-deletable parts: tool-orchestration list ~22 lines, chart-metric map
~10 lines, JSON/schema-defense ~50 lines that Pydantic + `_salvage_takeaways`
already enforce). The bulk of the prompt — tone *exemplars*, prose-craft, the
"synthesize don't summarize" voice — is the irreducible LLM half and **stays**.

The system already has **one** in-process LLM loop (the brief composer,
`briefing.py:216`); "chat" is the fitness tools over MCP driven by an external
Claude. **Chat is an ungrounded surface** (it self-polices "never fabricate
numbers" in prose); deferring it is a deliberate **scope choice, not a solved
problem** — `grounding.flag` is the obvious reusable follow-up there.

## Architecture

```
 Garmin → SQLite (data) + data/user_notes.md + briefings/ + injected clock
    │  [DETERMINISTIC, tested]
    ▼
 ① brief_planner.assemble_brief_context(db_path, *, today, notes, recent_briefs)
    - EXTENDS assemble_status: fetches EVERYTHING the prompt used to (today's
      snapshot+deltas, 14-day workout list w/ type/TE/distance, anomalies,
      per-metric 14-day summary stats, training load, plan-today, step goal,
      days-to-race, last-7 brief headlines) so the generator never needs a tool
    - evaluates every trigger predicate → CandidateTakeaway(s)
    - assigns a FIXED-priority rank (no float) + an ADVISORY suggested_tone
    → BriefContext  (typed; the SOLE data source for the generator)
    │
    ▼  [NON-DETERMINISTIC, eval'd]  — generator runs TOOLLESS, max_turns=1
 ② brief_generator  (the only LLM call — prompt shrunk ~25–30%)
    - receives BriefContext, SELECTS 3–5, prioritizes lead, WRITES prose in voice
    - keeps the voice exemplars, continuity rules, and a one-line schema-fix note
    - cannot fetch data (no MCP server attached) → grounding is sound
    → Brief (existing schema)
    │
    ▼  [DETERMINISTIC, tested]  — runs ONCE on the complete brief, AFTER the stream
 ③ grounding.flag(brief, context)  →  briefs.save_brief()
    - runs on the assembled Brief post-stream (streaming UX unchanged); matches
      each prose number against the UNION of BriefContext's GroundedValues, on
      the `display` rendering, per-unit tolerance
    - CONTRADICTION-ONLY: flags only a token close-but-unequal to a known metric
      (a likely corrupted metric value); ignores prescriptions/dates/counts/
      goals/countdowns/continuity (no nearby metric → ignored)
    - PURELY ADVISORY: emits an invention-rate SIGNAL (a report metric), logs it.
      NO reprompt, NO reject, NO dropped takeaway. (A single-turn generator has
      no corrective round-trip; grounding is a measurement, not a gate.)
    → briefings/<date>.json → web UI (view-only)
```

**Why toolless matters (resolves F2):** grounding can only guarantee "numbers
trace to `BriefContext`" if the generator *cannot* obtain a number elsewhere. So
the generator runs with **no MCP server, `max_turns=1`**, and `BriefContext` is
its sole input — which forces `assemble_brief_context` to carry the full data
the prompt used to fetch (enumerated above). `assemble_status` is **extended**,
not merely reused.

## Components

### New: `agent/brief_planner.py` (deterministic, tested)
- `assemble_brief_context(db_path=None, *, today, notes=None, recent_briefs=None) -> BriefContext`.
  **`today` is injected** (no internal `date.today()`); the full input set is
  (DB, `user_notes.md`, `briefings/`, `today`).
- **Trigger predicates** extracted verbatim from the prompt (`prompts.py:381-479`)
  as pure functions, each → `CandidateTakeaway | None`; thresholds live in one
  named `_TRIGGERS` config block with a tuning comment.
- **Tone**: `suggest_tone(...)` returns an **advisory prior** (see Tone below).
- **Priority**: a **fixed rank** (no float) — the prompt's existing order
  `workout > steps > conditioning > recovery > wildcard` (`prompts.py:251-271`).

### New schemas (`agent/schemas.py`) — typed, not a bag (resolves S3)
```python
class GroundedValue(BaseModel):  # a number the prose MAY cite
    name: str          # e.g. "rhr", "tsb", "sleep_seconds" (MetricName family)
    value: float
    unit: Literal["bpm","sec","min","mi","steps","count","sd","pct","none"]
    display: str       # the coach-ready rendering, e.g. "56 bpm", "7h 12m"
class CandidateTakeaway(BaseModel):
    category: str; fired_triggers: list[str]
    metrics: list[GroundedValue]            # typed — grounding matches value+unit
    suggested_tone: Tone; chart_metric: TakeawayMetric | None; evidence: str
class BriefContext(BaseModel):
    date: str; user_name: str
    candidates: list[CandidateTakeaway]     # priority-ordered, over-generated
    # Full data payload so the toolless generator can cite ANY supporting number
    # (even one that fired no trigger, e.g. "body battery topped at 82") and it
    # stays groundable. These retire the 8 tool calls the prompt used to make:
    snapshot: list[GroundedValue]           # today's metrics + baseline deltas (was get_today_status)
    training_load: list[GroundedValue]      # ctl/atl/tsb (was training_load_status)
    trends: list[GroundedValue]             # per-metric 14-day summary stats (was get_metric_trend ×4)
    workouts_14d: list[dict]                # was query_workouts(days=14)
    anomalies: list[dict]                   # was find_anomalies
    continuity: list[str]                   # last-7 headlines (was _recent_briefs_summary)
    plan_today: dict | None                 # incl. last_graded adherence + race name (was get_training_plan_status)
    step_goal: int | None; days_to_race: int | None
```
Every number the generator may legitimately cite is therefore present in
`BriefContext` (in `candidates[].metrics` ∪ `snapshot` ∪ `training_load` ∪
`trends`), which is also the exact set `grounding.flag` matches against.

### New: `agent/grounding.py` (deterministic, tested) — advisory signal (resolves F1)
- `flag(brief, context) -> list[GroundingFlag]` — runs **once on the complete
  Brief** (the output `Takeaway` carries no candidate id, so matching is against
  the **union** of all of `BriefContext`'s `GroundedValue`s, not per-candidate).
  For each numeric token in `summary`/`details`, match against each
  `GroundedValue`'s **`display`** rendering (post unit-conversion — DB is SI,
  prose is miles/pace/`h m`), per-unit tolerance. **Flag only a token close-but-
  unequal** to a known metric (likely a corrupted metric value); tokens with no
  nearby metric are ignored (prescriptions/dates/counts/etc.).
- **It is a measurement, not a gate**: its output is an *invention-rate signal*
  surfaced in the eval report and the brief log. It never rejects a brief, never
  drops a takeaway, never reprompts. Because it's advisory, an occasional
  false-positive is tolerable noise in a metric — the goal is detecting **gross
  metric corruption**, not certifying every number. (Honest scope: a perfect
  numeric verifier over free prose is not buildable; this catches the egregious.)

### Tone (resolves S1 / S7) — advisory prior + a band table, voice stays in prompt
- `suggest_tone` enumerates the **full per-mandate branch sets** (workout 4,
  conditioning 4, recovery 4, steps 3 — `prompts.py:308-478`) as tested
  predicates, composed with the existing `coach.includes_harsh_block` gate. Its
  output is an **advisory prior the generator may override** (recovery-takes-
  precedence and continuity-escalation are holistic and stay LLM judgment).
- The harsh-block **prose stays in the prompt** (it's voice — the validated
  "roast when slipping" exemplars, `prompts.py:162-184`); only the
  `coach.includes_harsh_block` **boolean selection** is what `suggest_tone`
  mirrors. So `score_profiles`' harsh-marker check passes unchanged.
- `coach.PROFILE_TONE_BANDS: dict[str, frozenset[Tone]]` is **informational**,
  not a gate: hardass/adaptive bands span all four tones (their mandates already
  go positive→critical), so band-membership of the *helper* is near-vacuous. The
  **load-bearing test** is that `suggest_tone` reproduces every per-mandate tone
  branch (unit-tested). Band membership of the **final brief tone** is a
  **report-only eval metric** (tone-band hit-rate over k runs), never a gate.
- The prompt **keeps** the voice exemplars and the continuity *rules*
  (`prompts.py:204-218`); only the *selection thresholds* move to code.

### Changed: `agent/prompts.py` + `agent/briefing.py`
- Prompt shrinks ~25–30%: delete tool-orchestration list, chart-metric map, and
  the JSON-defense block **but keep a one-line schema-fix instruction**
  (resolves S11 — the locked "user notes can't break the schema" guardrail).
- `generate_streaming`: `assemble_brief_context` → toolless generate (`max_turns=1`,
  no MCP server, streaming UX unchanged — `_iter_partial_takeaways` still yields
  complete cards) → `grounding.flag(brief, context)` **once on the assembled
  Brief after the stream completes** (advisory log, no reprompt) → save.
- **Thread `today`** through `assemble_status`/`_metric_rows` (they call
  `date.today()` internally today — `status.py:129,200`) so fixtures are
  reproducible (resolves S4).

### Changed: CI scorers AND the pytest assertions (resolves F5 — explicit, full scope)
The prompt shrink deletes strings that **three** places assert — all retargeted
in the cutover phase (the CI `validate` job runs both `score_prompt.py` and
`pytest`, so missing any reds CI):
- `scripts/score_prompt.py:111-117` — `"non-negotiable"`/`"fixed"`/`"exactly one
  key"` → re-point at `schemas.Brief` + `save_brief` (where the guarantee now
  lives). The `<one of:>` metric / `"tone":` checks survive (they read the JSON
  *shape* example, which stays).
- `tests/test_prompts.py:48-53` (schema-lock) + `:67-74` (asserts the tool name
  `"get_training_plan_status"`, which lives only in the deleted orchestration
  list) → retarget to assert the shrunk prompt's kept invariants.
- `tests/test_briefing.py:450-464` (tool-use timing on the now-toolless path) →
  update for `max_turns=1`.
- `score_profiles.py` (run via `tests/test_coach.py`) keeps its harsh-marker
  check (harsh prose stays); only its `_SCHEMA_FIXED` assertion retargets.

### Eval harness (`tests/evals/`) — report-only, not a required gate (resolves F3/F4)
- **Deterministic gate (per-PR, required-eligible):** trigger predicate unit
  tests, `assemble_brief_context` determinism, `grounding.flag` (low false-positive
  rate across fixtures), `suggest_tone` per-mandate branch reproduction, schema
  validity of a *recorded* generator output. All pure pytest — no live model,
  no flake.
- **Live evals (per-PR, REPORT-ONLY / non-blocking):** golden fabricated DB
  fixtures → run the real generator → report metrics (NOT hard asserts):
  invention-rate (raw stage-② output, pre-repair — resolves S "tautology"),
  lead ∈ {mandated, else top-priority-fired} as a **rate over k runs**, tone-band
  hit-rate. Auth = a **dedicated CI `CLAUDE_CODE_OAUTH_TOKEN`** (a separate
  credential from the production brief, but drawing on the same Max-subscription
  rate budget — see the caveat below; NOT `ANTHROPIC_API_KEY`, which the system
  never uses and which has no per-token billing here — resolves F3). The job
  **always runs but no-ops to success**
  when the token is absent (resolves the skipped-required-check footgun S10),
  is **path-filtered** to `agent/prompts.py|brief*.py|tests/evals/**`, and
  **excludes `dependabot[bot]`** (resolves S5). Generation count hard-capped.
  **Honest caveat:** a Max OAuth token is minted from a personal account, so the
  CI token shares Nate's subscription **rate budget** (there is no metered
  per-token billing and no true CI service account) — the path-filter +
  dependabot-exclusion + count cap keep CI eval runs from contending with the
  06:30 production brief in practice. If contention ever bites, move live evals
  to **nightly** (the report-only posture makes that a no-cost change).
- **LLM-judge (reactive / nightly, report-only):** a pinned-model rubric with a
  small **human-labeled calibration set** and a stated pass bar; added only if
  the deterministic property checks prove insufficient (YAGNI — resolves S6/S14).

## Migration plan (reordered — eval net + baseline BEFORE cutover; resolves F6/S2)

Each a `feature → dev` PR with measurable DONE criteria:

| # | Phase | DONE criteria |
|---|---|---|
| 0 | **Root-cause `ab_brief --run` flakiness** (don't assume salvage is the fix — `generate_streaming` already routes through `_salvage_takeaways`); make the eval path `save=False` so it can't clobber the live brief | the real parse error reproduced + root-caused; `--run` clean 10×; never writes `briefings/` |
| 1 | **Eval harness + capture baseline** on the CURRENT prompt — golden fixtures + deterministic property checks; `baseline.json` = **deterministic structural fingerprints** (`ab_brief.extract_features`) + invention-rate distribution. **No judge in the baseline** (judge is deferred/nightly — see below). | harness runs; baseline committed; deterministic checks green |
| 2 | **Extract triggers + tone-branches + priority + thread `today`** → `brief_planner` + `GroundedValue`/`CandidateTakeaway`/`BriefContext` schemas — **code only, no behavior change** (prompt still authoritative) | all trigger predicates 100% unit-tested; `assemble_brief_context` deterministic test passes |
| 3 | **Cutover behind `LOCAL_FITNESS_BRIEF_V2` flag** — toolless generator on `BriefContext`, shrink prompt, retarget the scorers + pytest assertions; **shadow-run** the new path (`save=False`) beside the old for N days, diff via `ab_brief.extract_features`. Flip the flag only when **structural parity holds + invention-rate ≤ baseline** on all fixtures (deterministic gate — **no LLM-judge in the cutover decision**). | prompt char target hit; CI green; shadow structural-parity + invention-rate ≤ baseline; rollback = unset the flag |
| 4 | **`grounding.flag`** (advisory invention-rate signal) + delete the JSON-defense block (keep the one-line schema-fix) + wire `grounding.flag` into the **post-stream, pre-save path** (advisory only) | low grounding false-positive rate on fixtures; a user-note-asks-for-new-field fixture proves salvage+one-liner hold the schema |

## API Surface

```python
# agent/brief_planner.py
def assemble_brief_context(db_path: Path|None=None, *, today: str, notes: str|None=None,
                           recent_briefs: list[dict]|None=None) -> BriefContext: ...
def suggest_tone(category: str, metrics: list[GroundedValue], profile: CoachProfile) -> Tone: ...   # advisory
# agent/grounding.py  — runs once on the whole brief; union-match on `display`; advisory only
class GroundingFlag(BaseModel): takeaway_index: int; token: str; nearest_metric: str; delta: float
def flag(brief: Brief, context: BriefContext) -> list[GroundingFlag]: ...   # never raises/gates/drops/reprompts
def invention_rate(brief: Brief, context: BriefContext) -> float: ...        # the report metric
# agent/coach.py
PROFILE_TONE_BANDS: dict[str, frozenset[Tone]]
```

## Invariants

**Checkable by inspection:**
- The generator runs with **no MCP server attached and `max_turns=1`** (toolless).
- `brief_planner`, `grounding`, `schemas` import no Claude SDK.
- The shrunk prompt retains a schema-fix instruction + the voice/continuity exemplars; deletes only the orchestration/chart/JSON-defense blocks.
- The `evals` job always runs (no-ops to success without the token) and is **report-only** (not a required check); path-filtered; excludes dependabot.

**Requires tests:**
- `assemble_brief_context` is deterministic over its **full input set** (DB + `user_notes.md` + `briefings/` + injected `today`), with `today` defaulting to `date.today()` so existing bare callers don't break.
- Each trigger predicate fires exactly on its documented condition.
- `grounding.flag` flags a deliberately-corrupted metric value (on `display`, union-match) **and** does not flag a correctly-converted miles/pace/duration token — measured as a **low false-positive rate** on the golden fixtures (advisory signal, not a zero-FP gate).
- `suggest_tone` reproduces every per-mandate tone branch + the harsh-block boolean gating. *(Band membership of the helper is informational, not a gated invariant; final-tone band hit-rate is a report-only metric.)*
- After the prompt shrink, the retargeted `score_prompt.py`, `tests/test_prompts.py`, `tests/test_briefing.py`, and `tests/test_coach.py` all pass (CI `validate` stays green).

## Failure modes & edge cases

- **Empty/partial DB / missing notes/briefings** — planner returns whatever fired; toolless generator still writes a valid (shorter) brief.
- **Generator emits a corrupted metric value** — `grounding.flag` logs it (raises the invention-rate signal); **no reprompt**; brief still saves; mandated takeaways never dropped.
- **Live-eval token absent (fork / no CI token)** — eval job no-ops to success (never a pending required check).
- **Shadow-run parity fails** — flag stays off; old monolith remains live; investigate before retry.
- **Cost runaway** — generation-count hard cap; OAuth path has no per-token bill but shares a rate budget, so CI uses a **separate** token from the production brief.

## Risks (residual)

- **Over-determination → robotic briefs.** Mitigation: planner over-generates; the LLM still selects/prioritizes/authors; tone is advisory; grounding flags only corrupted *metric* values, never narrative.
- **Live evals are non-deterministic** → kept **report-only**; only the deterministic subset can ever gate. (This reverses the earlier "required per-PR live gate" choice — see quality-gate F4.)
- **Prompt shrink is the biggest prompt edit in repo history** → gated behind a flag + shadow-run parity vs a committed baseline before cutover.
- **Tone fully de-LLM-ed is impossible** — only selection thresholds move to code; holistic precedence/continuity tone stays LLM, checked by band membership not exact tone.
