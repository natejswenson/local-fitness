---
title: "V2 Everywhere — migrate all brief-composition surfaces off the V1 monolith"
date: "2026-06-27"
status: quality-gated PASS (5 rounds, weighted score 9→5→3→2→0; 0 Fatal / 0 Significant)
supersedes-scope-of: "2026-06-26-agent-code-separation-design.md (which deferred chat/MCP as out-of-scope)"
---

# V2 Everywhere

## Goal

Eliminate the V1 brief prompt (`prompts.briefing_prompt()`) and route **every**
brief-composition surface through the V2 architecture: the deterministic
`brief_planner` (triggers, priority, advisory tone, typed `BriefContext`) +
grounding. Today only the **in-process** composer is V2; the **MCP `brief`
prompt** (used regularly from an external client) and the **chat / `coach`
prompt** still run V1-era, tool-driven, ungrounded.

## Honest framing — what "everywhere" can and cannot mean

V2's grounding is *sound* only because the in-process generator is **toolless**:
it cannot fetch a number, so every number traces to the pre-assembled
`BriefContext`. **You cannot force an external MCP agent (Claude Desktop) or the
chat loop to be toolless** — the MCP server advertises the full tool set and the
client decides what to call. So "V2 everywhere" is NOT "toolless everywhere." It
is two separable benefits, ported as far as each surface allows:

1. **Reasoning-in-code** (the planner): portable everywhere — expose the
   `BriefContext` so even an external agent reasons from pre-computed, tested
   context instead of orchestrating 8 tools in-prompt.
2. **Grounding** (invention-rate signal): portable ONLY to surfaces where the
   **server holds BOTH the composed prose AND its compose-time context**. That is
   the **in-process brief composer alone**: it composes the brief in-process, so it
   can run the advisory check against the `BriefContext` it just held. The **MCP
   `brief` path cannot be grounded server-side** — `_brief_prompt()` emits prompt
   TEXT and never sees the agent's composed brief; that prose arrives later at the
   separate, Claude-free `save_brief` tool, in a DIFFERENT stateless request
   (`stateless_http=True`), where no compose-time context is in hand. So the
   MCP-composed brief is **ungrounded by construction**. **All chat is ungrounded
   by the same root cause** — the coach runs in an external MCP client
   (Claude Desktop/Code/Mobile); the server only mounts chat at `/mcp` and never
   holds the agent's composed prose. The server can't co-locate externally-composed
   prose with its context. Honest: grounding is advisory, and it only reaches the
   in-process brief composer.

The unifying insight: **grounding is sound only where the server co-locates the
composed prose AND its compose-time context** — the in-process brief composer
alone. Reasoning-in-code (the typed `BriefContext`) ports everywhere; grounding
does NOT. Every surface still composes from the same
`brief_v2_*` instructions (the MCP path adds a "then call `save_brief`" tail — see
Component B), but only the in-process brief is logged via `grounding.log_grounding`
and the eval harness is scored via the `grounding.invention_rate` primitive (which
RE-ASSEMBLES its own frozen-fixture context in-process — see Component C). The MCP
path is scored by neither.

## Architecture (target)

```
 brief_planner.assemble_brief_context()  ──┐  [DETERMINISTIC, tested — the one pre-pass]
   (triggers, priority, tone, full payload) │
                                            ▼
   ┌─────────────────── BriefContext (typed) ───────────────────┐
   │                          │                                  │
   ▼                          ▼                                  ▼
 ① in-process composer      ② MCP `brief` prompt            ③ get_brief_context()
   briefing.generate_streaming  (external agent writes         MCP tool — returns the
   TOOLLESS, max_turns=1         from the SAME context +        BriefContext JSON for any
   (V1 branch DELETED)          brief_v2_MCP instructions,      external client/agent
                                then calls save_brief)
   │                          │                                  │
   │ ① log_grounding(brief,   │ ② UNGROUNDED by construction:    │ ③ (no compose here —
   │   context) ADVISORY with │   server returns prompt TEXT;    │   tool returns context
   │   the context IT HOLDS — │   the composed prose arrives     │   JSON for the client)
   │   never re-read at save  │   later at the save_brief TOOL   │
   │                          │   (Claude-free, a DIFFERENT      │
   │                          │   stateless request) — no        │
   │                          │   server-side co-location of     │
   │                          │   prose + compose-time context   │
   ▼                          ▼                                  ▼
        briefs.save_brief(payload)
          - validates + stamps (UNCHANGED — stays Claude-free; does NOT
            import brief_planner/grounding, does NOT re-assemble context)
          → briefings/<date>.json
```

## Components

### A. In-process composer — delete the V1 fallback
- `briefing.generate_streaming`: remove the `else` (V1) branch, the
  `LOCAL_FITNESS_BRIEF_V2` flag, `_brief_v2_enabled`, `_FALSY`. V2 is
  unconditional. (The private `briefing._log_grounding` was already replaced by the
  shared `grounding.log_grounding` in Phase 1; deleting the V1 branch here removes
  the last V1-only code — see C and the Phase 4 row.)
- **Pre-req: a bake period.** V2 has run the live 06:30 brief for ≥1 week with no
  regressions before this lands (deleting the fallback removes the rollback).

### B. MCP `brief` prompt → V2 (`mcp_server._brief_prompt`) — reasoning-in-code ONLY
- **Scope: this path ports reasoning-in-code, NOT grounding.** `_brief_prompt()`
  inlines the assembled `BriefContext` into the prompt and returns prompt TEXT —
  that is the entire server-side surface for the MCP brief. The agent's composed
  prose never comes back here; it lands at the separate, Claude-free `save_brief`
  tool, in a different stateless request. There is **no server-side place that
  co-locates the composed prose AND the compose-time context for the MCP path**, so
  the **MCP-composed brief is ungrounded by construction** — the same root cause as
  all chat being ungrounded (chat runs in an external MCP client; the server never
  holds the composed prose — see Component F). B inlines the tested context; it
  does not and cannot run grounding.
- Replace the injected `briefing_prompt()` + `_render_status(assemble_status())`
  with `brief_planner.assemble_brief_context()` rendered through a **persist-via-tool
  variant** of the V2 prompt.
- **Why not reuse `brief_v2_user_prompt` verbatim:** its tail is "# Output JSON
  only … Return ONLY the JSON object — no fence, no preamble." That tail is correct
  for the in-process **toolless, single-turn** generator, whose return value IS the
  JSON. The MCP flow is the opposite contract: the external agent must compose the
  brief **and then call the `save_brief` tool** to persist it. So the same tail
  would actively mislead the MCP agent.
- **The fix — parameterize the prompt's tail.** Add
  `brief_v2_user_prompt(context, …, persist_via_tool: bool = False)` (or a thin
  `brief_v2_mcp_prompt` wrapper). When `persist_via_tool=True`, the closing block
  becomes: "Compose the brief as a JSON object with exactly one top-level key
  `takeaways` … then call the `save_brief` tool with that object to persist it. Do
  NOT call any other tool — the data above is complete." The system prompt
  (`brief_v2_system_prompt()`) and the voice/data/schema body are **shared verbatim**
  with the in-process path — only the persist tail differs.
- **Folding the system prompt into the user text (MCP has no system channel).**
  `_brief_prompt()` returns a single **user-role** `PromptMessage` with no system
  channel (mcp_server.py:242-250), so the `brief_v2_system_prompt()` body cannot ride
  a separate system role here — it must be **prepended into the user text** (the way
  `_coach_prompt` prepends its persona), or the voice framing is silently dropped.
  "Shared verbatim" means the same body text; the delivery channel differs (system
  role in-process, folded into user text over MCP).
- The agent *can* still call other tools (not enforceable), but it has no reason to —
  the context is complete. There is no grounding backstop on this path (the server
  never sees the composed prose), so the only defense against drift is the completeness
  of the inlined context plus the "don't call other tools" instruction. Honest:
  ungrounded by construction.
- Net: the external brief is composed from the same tested context + same scoped
  voice/schema as the in-process brief, differing in the persist mechanism (tool call
  vs return value), the lack of a hard toolless guarantee, AND the absence of a
  grounding check (which the server structurally cannot run here).

### C. Grounding — reusable primitives + an advisory logging wrapper — LOG-ONLY
- **Two distinct shapes — keep them straight.** `grounding` exposes (a) the **scoring
  primitives** `grounding.flag` and `grounding.invention_rate(brief, context)` — both
  already public — which RETURN a value (a flag list / a float), and (b) a thin
  **advisory LOGGING wrapper** `grounding.log_grounding(brief, context)` that calls the
  primitives and **logs the signal only** (`brief_grounding invention_rate=… flags=…`),
  returning None. These are not interchangeable: a logging-only design throws the score
  away; the eval harness needs the RETURN value of the primitive.
- **Thread-the-context is the PRIMARY design (not re-assembly at save).** Each caller
  runs grounding **with the `BriefContext` it holds**, so the grounded pool is exactly
  the pool the brief was composed against:
  - **in-process composer** (`briefing.generate_streaming`): already holds
    `brief_context` post-stream — calls `log_grounding` (the advisory wrapper); this is
    the existing private `_log_grounding`, promoted to the shared `grounding` module.
    - **Logger-scope coupling (must handle in Phase 1).** The private
      `briefing._log_grounding` emits on logger `local_fitness.agent.briefing`;
      promoting it to `grounding.log_grounding` moves the emit to
      `local_fitness.agent.grounding`. `test_v2_logs_grounding_signal_without_altering_brief`
      (test_briefing.py:585) scopes `caplog.at_level(logging.INFO,
      logger="local_fitness.agent.briefing")`, so it will **no longer capture the
      record** after the move. Handle ONE of two ways: EITHER keep the emit on the
      briefing logger (the wrapper logs via the briefing module's logger, or briefing
      wraps the call) OR widen that test's `logger=` scope to
      `local_fitness.agent.grounding`. This is part of Phase 1's "suite green" bar.
  - **eval harness** (`shadow_run.py`): calls the **`invention_rate` primitive
    directly** for its RETURN value (it scores per scenario), against a context it
    RE-ASSEMBLES itself — acceptable ONLY because its fixtures are frozen, so a
    re-assembled context equals the compose-time pool. `capture_baseline.py` and
    `ab_brief.py` consume **no grounding today** — they call `briefing._generate(save=False)`
    and never hold a `BriefContext`.
  - **MCP `brief` handler**: does **NOT** call grounding — it never holds the composed
    prose (Component B). The MCP brief is ungrounded by construction.
- **Grounding is NOT moved inside `briefs.save_brief`, and is NOT the sole gate.**
  Three hard reasons, all load-bearing:
  1. **The eval path bypasses `save_brief` entirely.** `shadow_run` generates with
     `save=False` and never touches `save_brief`, yet it **consumes the
     `invention_rate` primitive's return value** (scoring it per scenario). The
     primitives must therefore stay **reusable, public functions the harness calls
     directly** (`grounding.flag` / `grounding.invention_rate`) — burying grounding
     inside `save_brief` would put the score behind a path the harness never hits.
     (`capture_baseline`/`ab_brief` don't score grounding at all — so this reason rests
     on `shadow_run`, the one eval caller that does.)
  2. **`briefs.py` must stay Claude-free + minimal** (its module docstring forbids
     importing `briefing`, `tools`, or the Agent SDK — that acyclic boundary is what
     lets the web/MCP servers read brief I/O without pulling a Claude loop into their
     import graph). `grounding` depends on `brief_planner`/`schemas`/typed
     `BriefContext`; pulling it into `save_brief` would break that contract. Keep
     `brief_planner` and `grounding` OUT of `briefs.py`.
  3. **Re-reading the DB at save time can DIFF from the compose-time pool.** A brief
     takes ~230s to generate (mostly model thinking); a `save_brief` that re-assembled
     the context would ground against a *fresher* DB than the one the brief was written
     from → false "inventions" on the SOUNDEST path. Threading the compose-time context
     eliminates this by construction.
- **DECIDED: log-only** — grounding does NOT stamp the brief, does NOT add a
  `_grounding` field, the `Brief` schema is unchanged, and it never rejects or
  mutates. A future UI "review pill" is a separate change if ever wanted.
- **The two in-process callers obtain the context differently — both valid.** The
  in-process composer **threads** its live `brief_context` into `log_grounding` (reason
  #3 above: never re-read at save time). The `shadow_run` harness **re-assembles** its
  own context — which is sound ONLY because its fixtures are frozen, so the re-assembled
  pool equals the compose-time pool. Re-assembly against the live DB is the anti-pattern
  reason #3 forbids; against a frozen fixture it is fine.

### D. New MCP tool `get_brief_context()`
- Returns `assemble_brief_context().model_dump()` — the planner output as JSON.
- **Its value lives in chat / F1, not the brief path.** Component B already inlines
  the assembled context directly into the MCP `brief` prompt, so the *brief* surface
  never needs this tool. D exists so the **chat** agent (and any other external
  client) can pull the pre-reasoned context in one call instead of orchestrating 8
  reads — it is the reusable bridge that ports reasoning-in-code to tool-having Q&A
  surfaces. (Don't justify D by the brief path; B covers that.)
- **Wiring (corrected):** to be *exposed* over MCP a tool must be in **`ALL_TOOLS`**
  (`tools.py`) — that's the list `make_server()` advertises. Add `get_brief_context`
  there. `read_only_tool_names()` is a *derived subset* (`_READ_ONLY_TOOL_NAMES`)
  whose only **production** consumer is the V1 in-process brief loop's allow-list
  (`briefing.py:217`) — which this design DELETES — so it is the wrong wiring target.
  (It also has **test** consumers — `tests/test_plan_tools.py` and `tests/test_tools.py`,
  incl. the "brief loop's allow-list" contract comment at `test_tools.py:917` — so the
  **helper itself is RETAINED**, not deleted; only its production call-site goes away.
  Once `briefing.py:217` is removed in Phase 4 the helper is production-orphaned and
  that `test_tools.py:917` comment goes stale → refresh it as a Phase 4 cleanup, see the
  Phase 4 row.) (If a read-only subset survives for any other consumer, add the name to
  `_READ_ONLY_TOOL_NAMES` too; but `ALL_TOOLS` is what makes it callable.)
- Auth/rate-limit: read-only, no Claude cost.

### E. Delete `briefing_prompt()` + retarget scorers/tests
Once A + B orphan it (5 consumers today):
- `prompts.briefing_prompt()` + `BRIEFING_PROMPT` constant → deleted.
- **The scorer retarget REWRITES the assertion strings — it is not a pure repoint.**
  The current markers are V1-prompt literals that do **not** appear verbatim in the
  V2 prompt; pointing the scorers at V2 text without changing the strings would fail:
  - `scripts/score_profiles.py`: `_SCHEMA_FIXED = ("non-negotiable", "exactly one
    key", "takeaways")` and `_HARSH_MARKER = "be sharp. be harsh"`. The V2 equivalents
    read **"exactly one top-level key"** (in `brief_v2_user_prompt`) and **"be sharp
    and harsh"** (in `_v2_voice_mandates`). Rewrite the marker tuples to the V2 strings
    and call **`_v2_voice_mandates(user_name, daily_step_goal, profile)`** for the
    harsh-block check — it is callable WITHOUT a `BriefContext`, so the per-profile
    gating test keeps working unchanged; "non-negotiable" has no V2 analogue and is
    dropped. The **universal** checks (`_TONE_WORDS` / `_SCHEMA_FIXED`,
    score_profiles.py:50-52) currently run against `briefing_prompt(...)` (the `bp`
    variable); retargeting them to **`brief_v2_user_prompt(context, …)`** requires the
    same **golden `BriefContext` fixture** as score_prompt (the V2 user prompt takes a
    context, the V1 one didn't) — so the fixture is shared by both scorers, not
    score_prompt-only.
  - `scripts/score_prompt.py`: two separate rewrites, one of which is **logic, not a
    string swap**:
    - **The metric-consistency check needs its extraction LOGIC rewritten.** Its
      highest-value check pulls the metric enum out of the prompt via
      `re.search(r"one of:\s*([a-z0-9_ |]+)", briefing)` (score_prompt.py:65) — keyed on
      the V1 literal **"one of:"**. The V2 prompt renders metrics as
      `"<rhr|sleep_seconds|…>"` (pipe-delimited inside angle brackets, prompts.py:724) —
      there is **no "one of:" literal**, so the regex returns `None`, `prompt_metrics`
      is empty, and `metrics_consistent` goes false. So the regex/parse LOGIC must be
      rewritten to extract the enum from the V2 `<a|b|c>` form (e.g.
      `re.search(r"<([a-z0-9_]+(?:\|[a-z0-9_]+)+)>", …)`), not merely repoint the
      assertion strings. (The **tone** check already keys on a `"tone": "a | b | c"`
      block that survives in V2, so the tone regex needs no change — only the metric
      regex does.)
    - **The schema substring checks are a string rewrite:** `"non-negotiable" … "fixed"
      … "exactly one key" … "takeaways"` → the V2 phrasing ("exactly one top-level key",
      "takeaways") against `brief_v2_user_prompt(context, …)` (built with a fixture/golden
      `BriefContext`), and/or assert the schema guarantee against `schemas.Brief` where it
      now structurally lives.
- `tests/test_prompts.py` (`test_briefing_prompt_*`), `tests/test_briefing.py`,
  `tests/test_coach.py` → retarget the **prompt-string** assertions to the V2 prompt
  (same string-rewrite caveat). **Not all of `test_briefing.py` is a prompt retarget,
  though:** three tests exercise the **deleted flag/V1-routing symbols and behavior**,
  not prompt text, and must be **DELETED** (not rewritten) in the same Phase 4 step that
  deletes `_brief_v2_enabled`/`_FALSY`/`LOCAL_FITNESS_BRIEF_V2`/the V1 branch —
  `test_brief_v2_enabled_by_default` (test_briefing.py:528) and `test_brief_v2_flag_parsing`
  (:533) call `briefing._brief_v2_enabled()` directly (→ `AttributeError`), and
  `test_v1_fallback_routes_tools` (:543) asserts the V1 `mcp_servers`/`max_turns==20`
  routing under `LOCAL_FITNESS_BRIEF_V2=0` (→ fails when V2 is unconditional). See the
  Phase 4 row.
- **Also `tests/test_prompts.py:156`** (`test_module_constants_built`) asserts
  `prompts.BRIEFING_PROMPT` (the module constant at prompts.py:734) is a non-empty
  `str`. Deleting the `BRIEFING_PROMPT` constant strands this assertion, so that line
  must be dropped/retargeted **in the same Phase 4 step** as the constant deletion
  (it keeps the parallel `SYSTEM_PROMPT` check, since `system_prompt()` stays).
- `prompts.system_prompt()` STAYS — it is the shared **chat** persona, not
  V1-brief-specific (used by `mcp_server` coach prompt + web chat).

### F. Chat / `coach` prompt Q&A — DECIDED: F1 only (expose `get_brief_context()`)
Open-ended Q&A is not brief composition and cannot use a fixed `BriefContext`.
**Chat ships F1-only — there is nothing to ground.** F1 = expose
`get_brief_context()` (Component D) and nudge the `coach` persona to prefer it for
"how am I doing / today's read" questions, so the chat agent reasons from the
pre-computed, tested `BriefContext` instead of orchestrating 8 reads in-prompt.
That ports **reasoning-in-code** to chat; it does NOT (and cannot) ground it.

- **Scope:** F1 is a prompt nudge plus the `get_brief_context()` exposure
  (already added to `ALL_TOOLS` in Phase 1, Component D). No new module, no
  capture plumbing, no unit-normalization work — none of that is reachable.
- **Why chat grounding is INFEASIBLE in the current architecture (the honest
  limit):** turn-scoped grounding would require the server to hold the agent's
  composed prose alongside the turn's tool results. **The server never holds chat
  prose.** There is no in-process chat loop in the repo: the only in-process Claude
  `query()` loop is `briefing.py` (the brief composer). The web "chat"
  (`web/src/components/AskCoach.tsx`) is a copy-prompt-to-clipboard UI; the actual
  coach runs in an **external MCP client** (Claude Desktop / Code / Mobile), and
  `web/server.py` mounts chat only at `/mcp`. The server sees tool calls/results
  but never the agent's final answer — that text is client-side and is never sent
  back. So server-side chat grounding has **zero reachable surface** — the same
  root cause as the MCP-brief being ungrounded by construction (the server can't
  co-locate externally-composed prose with its context). Exposing
  `get_brief_context()` (reasoning-in-code) is all that ports to chat.
- **Future work (pointer only, out of scope):** turn-scoped chat grounding would
  become buildable only if a new **in-process web-chat subsystem** were added (the
  server runs the chat loop itself and holds the prose). That is its own design if
  ever wanted — not part of this one.

## Phasing

| # | Phase | DONE |
|---|---|---|
| 1 | **Grounding → shared reusable primitives + logging wrapper** (C): move `briefing._log_grounding` to `grounding.log_grounding(brief, context)` (the advisory wrapper, returns None) and **delete the private helper now**; repoint **BOTH** in-process call-sites in `briefing.py` (the save path AND the `save=False` path) to the shared wrapper, and `shadow_run` calls the **`grounding.invention_rate` primitive directly** for its RETURN value (against its own re-assembled frozen-fixture context). `capture_baseline`/`ab_brief` score no grounding — unchanged. **No save-boundary move** — `save_brief` stays Claude-free. **+ `get_brief_context` tool** (D) added to `ALL_TOOLS`. **+ refresh the pre-existing STALE comments** (`shadow_run.py:21`, `capture_baseline.py:14`, `schemas.py` `BriefContext`, **`capture_baseline.py:202`** — the emitted note STRING, **and `tests/evals/baseline.json:4`** — the persisted note) that say "grounding.flag lands in Phase 4" — that wording collides with THIS design's Phase 4 (= delete V1); reword them to reflect that grounding primitives are already public and shared here. **CAUTION: `tests/evals/test_capture_baseline.py:121` ASSERTS `"grounding.flag" in doc["note"]`** — refreshing the `capture_baseline.py:202` note STRING (or the baseline.json fixture) will trip that assertion, so the test assertion must be updated in lockstep with any note-string change. Additive; the V1 branch never grounded and still doesn't, so `test_v1_path_does_not_run_grounding` stays green through Phase 3. | in-process brief logs via `log_grounding` from both call-sites; `shadow_run` scores `invention_rate` per scenario; `get_brief_context` returns a valid BriefContext over MCP; stale "Phase 4" comments refreshed; **`test_v2_logs_grounding_signal_without_altering_brief` (test_briefing.py:585) still captures the grounding record after the logger move — emit kept on the briefing logger OR the test's `logger=` scope widened to `local_fitness.agent.grounding`**; suite green |
| 2 | **MCP `brief` prompt → V2** (B) — repoint to `brief_v2_*` (persist-via-tool variant) + inlined BriefContext. **Reasoning-in-code ONLY — the handler does NOT call grounding** (it never holds the composed prose; ungrounded by construction). | external `brief` prompt composes from BriefContext, instructs `save_brief`; a recorded MCP-flow test asserts no `briefing_prompt` text; design states the MCP brief is ungrounded by construction (no grounding call on this path) |
| 3 | **Bake**: V2 live brief stable ≥1 week (in-process + MCP) | no regressions observed |
| 4 | **Delete V1** (A + E) — remove the V1 fallback branch + `LOCAL_FITNESS_BRIEF_V2` flag + `briefing_prompt()` **and the `BRIEFING_PROMPT` constant**, **update the `coach.py` docstring reference** to `prompts.briefing_prompt` (`coach.py:13`) so the grep gate is actually met, rewrite scorers to V2 (incl. score_prompt's **metric-extraction regex/logic** rewritten to the V2 `<a\|b\|c>` form, not just strings; tone regex unchanged), **drop the `test_prompts.py:156` `BRIEFING_PROMPT` assertion** with the constant, **and DELETE the flag/V1-routing tests in this SAME phase** — they exercise deleted symbols/behavior, **NOT** prompt strings, so they cannot be "retargeted": `test_v1_path_does_not_run_grounding` (its subject — the V1 branch — is gone here), **`test_brief_v2_enabled_by_default` (test_briefing.py:528)** and **`test_brief_v2_flag_parsing` (test_briefing.py:533)** (both call `briefing._brief_v2_enabled()` directly → `AttributeError` once it's deleted), and **`test_v1_fallback_routes_tools` (test_briefing.py:543)** (sets `LOCAL_FITNESS_BRIEF_V2=0` and asserts `mcp_servers and max_turns==20` → fails when V2 is unconditional). Never strand them. **+ the `read_only_tool_names()` helper is production-orphaned once briefing.py:217 (its only prod call-site) is deleted — update the now-stale "brief loop's allow-list" contract comment at `test_tools.py:917`; the helper itself is RETAINED (still used by `tests/test_plan_tools.py`/`tests/test_tools.py`), not deleted.** | `grep briefing_prompt` empty (incl. the `coach.py` docstring); no V1 branch; no `BRIEFING_PROMPT` constant; score_prompt metric-extraction parses V2 format; score_prompt/profiles rewritten + pytest green; flag/V1-routing tests (test_briefing.py:528/:533/:543 + `test_v1_path_does_not_run_grounding`) deleted; `test_tools.py:917` allow-list comment refreshed |
| 5 | **Chat F1** (F) — coach persona prefers `get_brief_context` for status questions (reasoning-in-code; chat grounding is infeasible — no in-process chat surface) | coach prompt references the context tool |

## Risks

- **Lose rollback** when V1 is deleted (Phase 4) → mitigated by the bake week +
  keeping V1 for one release after cutover.
- **External agent ignores "don't call other tools"** → not preventable, **and there
  is NO grounding backstop on the MCP path** — the server emits prompt TEXT and never
  sees the composed prose, so the MCP brief is **ungrounded by construction** (same
  root cause as all chat being ungrounded — external MCP prose is never server-visible;
  see Components B/C/F). Mitigation is the completeness of the inlined `BriefContext`
  (no incentive to fetch more) and the explicit "don't call other tools" instruction —
  not a drift log. Acceptable: the worst case is a brief that leaned on a tool result
  instead of the inlined context, still schema-validated at `save_brief`; it is simply
  unscored.
- **Grounding false positives** (derived baselines, "14 days" windows, %s) → already
  advisory + LOG-ONLY (no UI pill), so FPs are noise in a log line, not a user-facing
  false alarm. These apply only to the **in-process** brief composer and the
  `shadow_run` eval (the only two paths that run grounding); the MCP brief path and all
  chat run no grounding, so they carry no FPs. (window-suppression + baseline exposure
  already done.)
- **~~`save_brief` context race~~ — RESOLVED by design.** Grounding is no longer at
  `save_brief` and never re-reads the **live** DB at save time: the in-process composer
  threads its **compose-time** `BriefContext` into `grounding.log_grounding`, and
  `shadow_run` scores against a re-assembled **frozen-fixture** context — both equal the
  pool the brief was written from. No race; no false "inventions" from a fresher
  same-day DB on the soundest path. (See Component C.)

## Decisions locked (2026-06-27)
- **Grounding is LOG-ONLY** everywhere — no `_grounding` field, Brief schema
  unchanged, no UI pill.
- **Chat = F1 only** (`get_brief_context` exposure + coach-persona nudge). **F2
  (turn-scoped chat grounding) is INFEASIBLE in the current architecture — there is no
  in-process chat surface** (all chat runs in an external MCP client; the server never
  holds the agent's composed prose), so it is **deferred to a future in-process-chat
  project** (its own design if ever wanted — see Component F "Future work"). F1 ports
  reasoning-in-code to chat; nothing grounds it. (Resolves old open-Q #3.)
- **Grounding runs only where the server holds the composed prose + its compose-time
  context — the in-process brief composer alone.** It threads its live context into the
  advisory `grounding.log_grounding` wrapper; the `shadow_run` eval calls the
  `grounding.invention_rate` primitive for its return value against a re-assembled
  frozen-fixture context. Grounding is **never inside `briefs.save_brief`** (which stays
  Claude-free and never re-reads the live DB at save). The **MCP `brief` path runs no
  grounding, and all chat runs no grounding — both ungrounded by construction** (the
  server never sees the externally-composed prose; same root cause). This resolves the
  old open-Q "re-assemble vs thread" — thread for the live in-process path, re-assemble
  only against frozen fixtures — and keeps the `shadow_run` eval path scored via the
  reusable primitive. (See Components B, C, and F.)
- **Deleting `briefing_prompt()` breaks NO client contract.** An MCP prompt's *text*
  is server-controlled and never part of the wire contract — clients depend only on
  the prompt **NAME** (`brief`) and the **`save_brief` tool**, both preserved. There
  is no versioned-wording guarantee to honor; swapping the prompt body to V2 is
  invisible to any conforming MCP client. (Resolves old open-Q #2.)

## Open questions for quality-gate
- *(none open — the three prior open questions are resolved in "Decisions locked"
  above: re-assemble-vs-thread → thread; `briefing_prompt()` deletion → no client
  contract; chat scope → F1 only, since F2 turn-grounding is infeasible with no
  in-process chat surface — deferred to a future in-process-chat project.)*
