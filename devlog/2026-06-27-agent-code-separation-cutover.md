# Agent/code separation — brief composer cut over to V2

**2026-06-27**

The daily brief was one big LLM call: a ~25k-char prompt that told the model to
orchestrate 8 tool calls, evaluate every trigger threshold in prose, pick a tone,
and write the brief — reasoning and generation tangled together, none of it
testable. Per ["Your Prompt Is Too Big"](https://chasepd.github.io/posts/your-prompt-is-too-big/),
we split it: the **deterministic reasoning** moves into code covered by tests; the
**LLM** does only what it's good at — prose in voice.

## What shipped (Phases 0–4, all on one branch)

- **`agent/brief_planner.py`** (the tested half): trigger predicates extracted
  verbatim from the prompt as pure functions with one `_TRIGGERS` threshold
  block, a fixed priority rank, an advisory `suggest_tone`, and
  `assemble_brief_context()` — which gathers everything the prompt used to fetch
  (snapshot, training load, baselines, the **actual 14-day workout list**,
  anomalies, plan + adherence, continuity) into a typed `BriefContext`.
- **Toolless generator**: `brief_v2_*` prompt (shrunk ~50%, voice exemplars
  kept) runs with **no MCP server and `max_turns=1`** on the `BriefContext` —
  so it can't fetch a number, which is what makes grounding sound.
- **`agent/grounding.py`** (advisory): matches each prose number against the
  context's `GroundedValue` displays; flags a token close-but-unequal to a known
  metric (likely corruption) and logs an invention-rate **signal**. Never gates,
  drops, or reprompts — a measurement, not a corrective round-trip.
- **Eval net**: golden fabricated fixtures + a committed `baseline.json` (V1
  fingerprints) + `scripts/shadow_run.py`, which runs V2 across the fixtures and
  checks structural parity before any cutover.

## The cutover

`LOCAL_FITNESS_BRIEF_V2` now defaults **ON**. The V1 monolith is retained as the
instant rollback (`LOCAL_FITNESS_BRIEF_V2=0`). Only the in-process composer is
V2; the MCP tools and chat path still use V1 (deliberate scope choice).

Gated on evidence, not vibes: the live shadow-run held **structural parity on all
6 fixtures**. The first run "failed" invention-rate on 3 — diagnosed as grounding
false positives (derived baselines, "14 days" windows, continuity recalls), not
the model inventing — so invention-rate was made advisory (structural parity is
the hard gate) and the worst FP classes were cut.

## Two real bugs the shadow-run + a real-data read caught

- **`workouts_14d` was gutted to summary counts** in the first planner cut, so a
  V2 brief said "yesterday's long run" generically. Restored the actual workout
  list → the brief now leads with "you ran 6.01mi at HR 169, TE 5.0 — plan said a
  2.5mi shakeout" and grades the adherence miss.
- **Thin-data invention**: on a near-empty DB the toolless model extrapolated.
  Added a prompt rule to go number-light / by-feel when the context is sparse.

## Tested to standard

759 passing, 92.8% coverage. `brief_planner` and `grounding` are 100% covered;
the planner imports no Claude SDK (asserted). Predicates each fire exactly on
their documented condition; every tone branch reproduced; grounding flags a
corrupted value and not a correctly-converted miles/duration token; the shadow-run
parity checks are unit-tested over synthetic records.
