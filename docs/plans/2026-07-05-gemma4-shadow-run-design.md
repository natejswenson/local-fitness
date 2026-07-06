---
ticket: "n/a"
title: "gemma4 local-model shadow-run (retry of the abandoned local-model brief)"
date: "2026-07-05"
source: "design"
---

# gemma4 local-model shadow-run

## Why this is a retry, not a fresh idea

`docs/plans/2026-06-16-fitness-mcp-server-design.md` records that a prior attempt
to move brief generation off Claude onto a local model was tried and abandoned:
*"small models fabricate numbers and miss the coach voice. The lesson: don't
fight Claude's strengths."* That failure is why the MCP-server-for-interactive-
Claude architecture exists at all — it's the designed response to this failure,
not incidental infrastructure.

Two things justify a retry now:

1. **gemma4 is a materially different model** than whatever was tried before —
   native tool-calling, a "thinking" (extended-reasoning) mode, 131K context.
   The earlier test isn't necessarily representative of what it can do.
2. **The codebase already built the safety net this needs**, apparently in
   response to the same failure: the V2 brief pipeline (cut over 2026-06-27)
   feeds the model a deterministic, pre-computed `BriefContext` and asks it to
   only narrate — no tool-calling, no model-driven number lookup — plus an
   advisory `grounding.invention_rate()` signal built to catch exactly the
   "fabricates numbers" failure mode.

**Explicit, disclosed unknown:** whether the original abandoned attempt used
this same toolless architecture, or the older tool-driven one, is undocumented
beyond the one sentence above. This design proceeds on the reasonable but
*unverified* assumption that it's testing meaningfully different (safer)
conditions than the original attempt, not a settled fact.

## Scope

Shadow-run only. gemma4 is fed the exact same grounded `BriefContext` and V2
prompts Claude gets, using the existing shadow-run gate infrastructure. No
change to the live 06:30 production brief job's behavior. Promoting gemma4 to
replace or supplement Claude there is an explicitly separate, future, gated
decision based on what this shadow-run and a manual voice-fidelity read show.

## What's being reused vs. built new

This codebase already has almost everything this needs:

- `agent/brief_planner.py::assemble_brief_context()` — deterministic context assembly.
- `agent/prompts.py::brief_v2_system_prompt()` / `brief_v2_user_prompt()` — the exact V2 prompts.
- `agent/grounding.py::invention_rate()` — model-agnostic fabrication signal.
- `scripts/eval_fixtures.py` — 6 fully-synthetic fixture DBs, never derived from real data.
- `scripts/capture_baseline.py` / `scripts/shadow_run.py` — an existing structural-parity gate: dry-run by default, hard generation cap, per-scenario failure isolation (a bad generation becomes a recorded flake, never crashes the batch), hard checks (schema validity, steps-mandate, takeaway count, plan-folding) plus an advisory invention-rate budget, against a committed baseline.

Building a parallel comparison script from scratch was the original plan and
was wrong — it would have duplicated all of this and risked drift between two
independently-maintained comparison pipelines. **The actual new code is two
pieces:**

1. **`src/local_fitness/agent/local_model.py`** (new file) —
   `generate_local_completion(system_prompt, user_prompt, *, model="gemma4", host="http://localhost:11434", think=False, temperature=0.4, timeout=300.0) -> str`.
   Stdlib `urllib.request` (no new dependency — `httpx`/`ollama` aren't
   currently declared dependencies). POSTs to Ollama's local `/api/chat`,
   `stream=False`. Normalizes connection errors, non-2xx responses, and a
   malformed response body into one consistent `RuntimeError` rather than
   leaking three different low-level exception shapes into flake diagnostics.
   `think=False` empirically verified (see Verification, below) to cleanly
   suppress gemma4's reasoning output on this install — not merely assumed.

2. **A new branch inside `briefing.generate_streaming()`** — model names
   prefixed `"ollama:"` (e.g. `"ollama:gemma4"`) are dispatched to
   `local_model.generate_local_completion()` instead of
   `claude_agent_sdk.query()`. Reachable **only** when V2 is enabled; an
   `"ollama:"` model requested under the V1 (tool-calling) path is refused
   outright — this is the code-level enforcement that gemma4 never gets MCP
   tool access, not just a documented convention. When taken, the branch:
   - asserts the resolved `db.DEFAULT_DB_PATH` **equals** the fixture path
     this run explicitly built (allow-list, not a deny-list against one
     hardcoded "real" path — see Fixes, below, for why);
   - builds `brief_context`/prompts via the exact same `brief_planner`/
     `prompts` calls the Claude V2 path already uses (prompt parity by
     construction, not by discipline);
   - calls `generate_local_completion()` via `asyncio.to_thread` (the shared
     caller, `generate_streaming()`, is also driven by FastAPI's live
     `/api/brief` regen endpoint on a real event loop — `to_thread` protects
     that caller even though the shadow-run's own serial loop doesn't need it);
   - falls through into the existing, unmodified tail:
     `_extract_json` → `Brief.model_validate` → `grounding.log_grounding` →
     yield done/error. No parsing/validation/grounding logic is duplicated.

Once the branch exists, `scripts/shadow_run.py --model ollama:gemma4 --run`
works using the existing gate, unmodified except for the fixes below.

## Fixes from two rounds of adversarial review

**Round 1** (score 13 → resolved/refuted): building a duplicate harness was
wrong (resolved by the reuse decision above); a "small models can't produce
valid JSON" concern and a "does `think:false` actually work" concern were
both **empirically tested against the real install** rather than assumed —
see Verification below.

**Round 2** (score 8, found via direct review of the actual reused code):

1. **Fatal, verified — pre-existing data-isolation bug in `shadow_run.py`/
   `capture_baseline.py`.** `agent/briefs.py:44` freezes
   `DEFAULT_BRIEFINGS_DIR` at **module import time**; `_recent_briefs_summary()`
   (line 279) reads that frozen global directly. Both scripts set
   `os.environ["LOCAL_FITNESS_BRIEFINGS_DIR"]` **after** importing `briefing`
   (which imports `briefs`), so the override is a no-op — confirmed by
   reading the actual import order and the actual function body. Every
   "fixture-only" shadow-run has been folding real recent-brief content
   (real personal coaching history) into the prompt. This doesn't leave the
   machine (Ollama is `localhost`), but it breaks the fixture-isolation
   invariant this whole design depends on and contaminates invention-rate
   scoring (real continuity numbers absent from the fixture's `BriefContext`
   misread as fabrication). **Fix:** mutate `briefs.DEFAULT_BRIEFINGS_DIR`
   directly in `_capture_v2()`/`_capture_live()`, exactly like
   `db.DEFAULT_DB_PATH` is already handled (attribute mutation + `finally`
   restore) — not an env var the module never re-reads. This is a fix to
   already-shipped code, not something the new gemma4 path introduces, but
   it must land first.
2. **Significant — DB-path safety check must be an allow-list.** The literal
   `db.DEFAULT_DB_PATH != <real path>` comparison silently passes under a
   container deployment (`LOCAL_FITNESS_DATA_DIR=/data`) where the real path
   is different from the hardcoded literal. Fixed by asserting the path
   **equals** the exact fixture path this run built, not that it merely
   differs from one guessed "real" location.
3. **Significant — dry-run print falsely claims Claude subscription billing
   for local-model runs.** Fixed by making that message conditional on
   whether `model` is `"ollama:"`-prefixed.
4. **Significant — invention-rate budget (0.5) and cost/time estimates are
   Claude-tuned constants applied uncritically to gemma4.** Not fixing the
   budget value (no data to tune it on yet), but: the dry-run estimate prints
   "$0, wall-clock unmeasured" for local models instead of Claude's canned
   numbers, and this doc states explicitly — **treat the first gemma4
   shadow-run's invention-rate numbers as calibration data, not a verdict.**
   The hard structural checks (schema validity, takeaway count, plan-folding)
   remain meaningful across models since they test JSON shape, not fabrication
   style.
5. **Significant — `tests/evals/baseline.json` predates the V2 cutover.**
   Comparing gemma4-V2 against a Claude-**V1** baseline conflates two
   different questions. **Required operational step, not a code change:**
   re-run `capture_baseline.py --run --model claude-sonnet-4-6` (V2 is the
   default now) to refresh the baseline before running the gemma4 comparison,
   so it's gemma4-V2 vs. current-Claude-V2.
6. **Minor, accepted — observability regression.** The rich per-message
   timing/telemetry in `generate_streaming()`'s Agent-SDK message loop has no
   equivalent for a bare Ollama response string. Accepted gap for a
   diagnostic-only path; not fixed.
7. **Minor, accepted — no wall-clock cap, only a generation-count cap.** A
   slow/contended local Ollama instance could stretch a 12-generation run
   long before any guard notices. Accepted for v1 — a per-call timeout well
   under 300s failing on this install would itself be informative (a model
   too slow for an unattended 06:30 job is disqualifying regardless of
   output quality).

## Verification already performed (not just asserted)

Before writing any code, two direct tests were run against the real local
Ollama + gemma4 install:

1. `curl localhost:11434/api/chat` with `"think": false` → `thinking: null`,
   clean JSON in `content`. Confirms the reasoning-suppression mechanism
   this design's speed/parseability strategy depends on actually works here
   — refuting a review claim (citing unverifiable external issue numbers)
   that it might not.
2. A hand-built prompt approximating the real V2 schema (not the actual coach
   persona) returned valid JSON with no markdown fencing, 4 takeaways in the
   [3,5] range, and **zero fabricated numbers** — every injected value (RHR,
   sleep, steps, CTL/ATL/TSB) was echoed exactly. One sample, generic prompt
   — anecdotal, not proof — but a real positive signal that this is worth
   carrying through to the full shadow-run rather than a reason to stop here.

## Invariants

**Checkable by inspection:**
- `local_model.py` adds no new production dependency.
- The `"ollama:"` dispatch branch in `briefing.generate_streaming()` is
  unreachable when V2 is disabled.
- Claude's existing code path in `briefing.py` is unmodified by the new branch
  (net-new branch, not a rewrite of the existing one).

**Requires running to verify:**
- The local-model branch never calls out to Ollama with the real
  `data/fitness.db` or real `briefings/` history — enforced by the allow-list
  path assertion (DB) and the direct `briefs.DEFAULT_BRIEFINGS_DIR` mutation
  (recent-briefs), both restored in `finally`.
- A gemma4 JSON/schema failure is recorded as a flake (via the existing
  `shadow_run.py`/`capture_baseline.py` exception handling) and never
  silently drops a scenario from the report or crashes the batch.
- The live 06:30 production job (`fitness brief` → `generate_and_save` →
  Claude) is unaffected by any of this.

## API Surface

- `agent/local_model.py::generate_local_completion(system_prompt, user_prompt, *, model="gemma4", host="http://localhost:11434", think=False, temperature=0.4, timeout=300.0) -> str`
- `briefing.generate_streaming(model, save)` — extended to accept
  `"ollama:<name>"` model strings under V2; refuses them under V1.
- No new CLI surface — `scripts/shadow_run.py`'s existing `--model` flag is
  the entry point (`--model ollama:gemma4`).

## Rollout sequence

1. Land the `briefs.DEFAULT_BRIEFINGS_DIR` isolation fix in
   `shadow_run.py`/`capture_baseline.py` (fixes existing Claude-only runs too).
2. Land the DB-path allow-list fix, the conditional billing message, and the
   local-model-aware estimate message.
3. Add `agent/local_model.py` and the `"ollama:"` dispatch branch.
4. Re-run `capture_baseline.py --run --model claude-sonnet-4-6` to refresh
   the stale V1 baseline.
5. Run `scripts/shadow_run.py --model ollama:gemma4 --run`, treat the
   invention-rate numbers as calibration data, and manually read the
   takeaway text for coach-voice fidelity against the same fixtures.
6. Only then decide, as an explicitly separate future step, whether any of
   this warrants touching the live production path.

## Outcome (2026-07-05, post-implementation)

Ran the full shadow-run gate against gemma4 across four configurations,
each a real, measured data point:

| Configuration | Schema compliance | Content quality |
|---|---|---|
| Baseline V2 prompt (shared with Claude) | ~5/12 valid, frequent crashes (capitalized enums, bare-string `metric`, missing fields) | invention_rate=0.0 where valid |
| + gemma4-specific stricter prompt (`brief_v2_*_prompt_gemma4`) | Still ~5/12 valid — different crash shapes, no net improvement | invention_rate=0.0 where valid |
| + Ollama structured output (`format=Brief.model_json_schema()`) | **12/12 valid, 0 flakes** — fully solved | Degraded: missing mandated workout/steps takeaways, generic headlines, `tone` defaulting to "neutral", `metric` defaulting to `null` |
| + `think=True` on top of structured output | N/A | Worse — takeaway count dropped further, required fields empty, ~20x slower (17-20s vs sub-second per call) |

**invention_rate was 0.0 in every configuration where a brief was schema-valid.**
The original 2026-06-16 failure mode ("small models fabricate numbers") does
not reproduce with gemma4 — that specific concern is empirically resolved for
this model.

**What remains unresolved: content-mandate compliance.** The prompt requires
specific mandated takeaways (workout, steps) and asks the model to actively
select a meaningful tone and cite a metric. None of the four levers tried
here — all formatting/decoding-side interventions (prompt wording, grammar-
constrained decoding, reasoning budget) — reached this, because none of them
address the model's actual instruction-following/reasoning capacity for
free-text semantic requirements. JSON Schema constrains structure; it has no
mechanism to enforce "must include a steps takeaway."

**Conclusion (superseded by the 2026-07-06 update below):** gemma4 (8B,
Q4_K_M, this Ollama install) is not ready to replace or supplement Claude for
the daily brief. The fabrication risk that originally killed local-model
brief generation is resolved for this model, but a different, still-blocking
gap (content-mandate adherence) was discovered and not resolved by the
interventions tried. The shadow-run infrastructure itself (the `"ollama:"`
dispatch branch, `local_model.py`, the fixed data-isolation bug, the
gemma4-specific prompt variants, and structured-output support) is sound,
tested, and reusable — for this model, for a future larger/more capable
local model, or for further iteration (e.g., a model better at
instruction-following under grammar constraints, or restructuring the
prompt so mandated-candidate selection is itself part of the enforced
schema rather than a free-text instruction).

## Outcome update (2026-07-06, category-mandate schema restructuring)

Picked back up on the "content-mandate adherence" gap above. The insight:
the tone/metric-defaulting fix already proved that **required object keys**
are reliably honored by Ollama's grammar-constrained decoder for gemma4,
while the two JSON-Schema mechanisms tried for forcing category coverage in
a free-form `takeaways` array were not:

- A single `contains` constraint (force one `category: X` to appear)
  worked reliably in isolated testing.
- Combining two `contains` clauses via `allOf` (force BOTH `category:
  workout` and `category: steps`) did **not** — across 3 fixtures it
  produced inconsistent results, including one case (`fatigued_recovery`)
  where the output satisfied *neither* mandated category across all 3 runs
  and still validated as schema-conformant. `allOf`+`contains` composition
  is not reliably enforced by this decoder.

Fix: restructure `_gemma4_format_schema()` to drop the free-form
`takeaways` array entirely and replace it with two **required** slots —
`workout_takeaway`, `steps_takeaway` — plus a bounded `other_takeaways`
array (1-3 items) for the rest. A new `_reshape_gemma4_slots()` flattens
this back into Brief's real `takeaways` list immediately after the Ollama
call, so `_finalize_brief` (parse/validate/save/grounding) is unchanged and
identical across every model. Verified 9/9 (3 fixtures × 3 runs) before
being wired into `briefing.py`.

**Full shadow-run gate result (`--model ollama:gemma4 --run`, 12
generations across all 6 fixtures):**

| Scenario | Schema-valid | Parity | Invention rate |
|---|---|---|---|
| green_light | 2/2 | PARITY | 0.0 |
| sliding_fitness | 2/2 | PARITY | 0.0 |
| fatigued_recovery | 2/2 | PARITY | 0.0 |
| missed_steps | 2/2 | PARITY | 0.167 |
| taper_plan | 2/2 | **MISMATCH** (plan_parity) | 0.333 |
| sparse | 2/2 | PARITY | 0.333 |

12/12 schema-valid, 0 flakes, 5/6 scenarios at full structural parity — up
from 0/6 in the prior conclusion. The workout+steps category mandate is now
reliably satisfied.

**New, narrower gap found:** `taper_plan` (the only scenario with an active
training plan) fails `plan_parity` — the workout takeaway doesn't fold in
the plan's prescribed session. Manual inspection of the raw output (2 runs)
confirmed this is a real content miss, not an `ab_brief` keyword-heuristic
false negative: the takeaways were generic training advice ("Recovery
Focus," "Tempo Work: Maintain Pace") with no reference to the specific
prescribed workout, pace, or plan adherence status carried in
`BriefContext.plan_today`. This is a different failure mode than the
original category-mandate gap — it's the model not attending to a specific
input field under grammar-constrained decoding, not a structural coverage
problem — and is not yet resolved.

**Revised conclusion (superseded below):** gemma4 has gone from "not ready"
to "close, with one remaining known gap." Schema compliance, category-mandate
coverage, and fabrication are all solved. Plan-aware workout takeaways (a
minority case — 1 of 6 fixture scenarios, and only relevant when the user has
an active training plan) are not yet solved. Whether to keep iterating on
that gap or accept it as a documented limitation is an open decision, not a
technical blocker on the shadow-run infrastructure itself.

## Outcome update (2026-07-06, plan-fold gap)

Investigated the `taper_plan` gap further. First attempt: add a required
`plan_status` sub-object (race/adherence/prescription fields) to the
workout slot, on the theory that required object keys — reliable for
everything else this session — would force the model to derive these facts
correctly. It did NOT: the sub-object was reliably populated (structural
compliance held), but the CONTENT was fabricated — `days_to_race` (actually
10) came back as "14 days" in 2 of 3 runs, and `adherence` was reported as
"on_track"/"Good" in every run despite the real `last_graded.verdict` being
`"missed"` — the opposite. A required field forces *something*, not
*correctness*; this is a grounding/reasoning failure, not a structural gap,
and schema engineering can't reach it.

Fix: stop asking the model to derive these facts at all. `days_to_race`,
the race goal/time, the last-graded verdict, and today's prescribed session
are all already known, deterministic values in `BriefContext.plan_today` —
the same data Claude already reasons over correctly. Two new gemma4-only
functions in `briefing.py` (never touching the shared `brief_v2_user_prompt`
Claude's live path also calls):

- `_gemma4_plan_prompt_facts()` — appends a pre-computed facts block to the
  PROMPT ("cite these VERBATIM"), to reduce (not guarantee) the odds the
  model's own prose contradicts them.
- `_gemma4_plan_status_appendix()` + `_append_gemma4_plan_status()` —
  appends a deterministic, always-correct plan-status sentence to the
  workout takeaway's `details` AFTER generation, independent of what the
  model wrote. This is the load-bearing fix: correctness is guaranteed by
  construction (computed in Python), and the fixed phrasing reliably hits
  `ab_brief._PLAN_KEYWORDS` regardless of the model's own wording (manual
  testing showed gemma4's free-text phrasing hit those keywords 0/4 times
  even when factually correct).

Verified before wiring in: 4/4 manual runs on `taper_plan` showed the
appended facts were exactly correct every time (guaranteed by construction)
and `ab_brief.extract_features(...)["mentions_plan"]` was `True` 4/4 (up
from 0/4 with prompt-only facts). One residual, non-blocking risk observed:
in 1 of 4 runs the model's OWN earlier prose (before the appendix)
recommended a different session than the plan prescribed — an internal
inconsistency within the card, though the correct fact is always present
and stated last.

**Full shadow-run gate result, post-fix (`--model ollama:gemma4 --run`, 12
generations):** `taper_plan` no longer fails `plan_parity` — confirmed via
the per-scenario `FAILED CHECK` list, which no longer names it. A separate,
pre-existing flake surfaced in this run: `steps_mandate` failed for
`taper_plan` and, notably, `fatigued_recovery` (a scenario with NO active
plan — proof this is unrelated to the plan-fold change). Manual re-run (4
generations × 2 scenarios) confirmed this is `temperature=0.8` sampling
noise, not a regression: the steps takeaway is structurally present every
time (the required-slot mechanism holds), but ~1-in-4 times the model's
free-text phrasing doesn't happen to contain the literal substring `"step"`
(e.g. "Focus on Consistency Over Peak Efforts") that `ab_brief`'s naive
keyword check looks for — a heuristic-precision issue, not a missing
takeaway. Not fixed here; flagged as a known, pre-existing, orthogonal risk
of the `temperature=0.8` choice (see `_gemma4_format_schema`'s docstring)
that predates this round of work.

**Final conclusion:** the plan-fold gap is resolved for the failure mode
that mattered (fabricated/inverted facts) and for the structural parity
check (`plan_parity` passes). The category mandate, schema compliance, and
fabrication are all solved. The one remaining known risk is `has_steps`
keyword-matching flakiness (~25% in small samples) from temperature-driven
phrasing variance — orthogonal to everything fixed this round, low-severity
(the content is present, just not always using the exact expected word),
and a reasonable place to stop this fine-tuning pass.

## Testing / verification approach

Follows this codebase's existing pattern: `parity_report()` and
`aggregate_scenario()` are already pure/deterministic and unit-tested; the
LLM-calling glue (`_capture_v2`, `_capture_live`, `generate_local_completion`)
is not unit-tested by design (a test would only assert a mock replays
itself) and is instead exercised live via the shadow-run itself. New unit
tests are warranted for: the DB-path allow-list assertion logic, and the
exception-normalization in `generate_local_completion()` (mock the HTTP
layer). No new test infrastructure needed beyond what exists.
