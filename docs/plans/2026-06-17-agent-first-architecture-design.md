---
ticket: "#25"
title: "Agent-first split: data+compute server, synthesis in the agent"
date: "2026-06-17"
source: "design"
---

# Agent-first split: data+compute server, synthesis in the agent

## Goal

Make local-fitness **agent-first**: the web-server PROCESS holds zero LLM logic
and only serves/stores data; *all coaching synthesis* (briefs, ad-hoc analysis,
plan drafting/revision, dashboard insights) happens in a client agent
(Claude Code / Desktop / Mobile) against the MCP.

This is a **real migration, not a non-breaking change.** Two things change
shape:

- **Unchanged:** the data + deterministic-compute REST endpoints and every
  chart/data view in the UI. Those keep working byte-for-byte.
- **Reworked:** brief generation and the three chat-driven UI flows. The
  server's two Claude loops (`briefing.py`'s loop, `chat.py`) are retired, the
  `/api/chat*` and `/api/brief/generate*` endpoints are removed, and the UI's
  Today brief + TrainingPlan drafting + Dashboards insights are reworked from
  *interactive synthesis surfaces* into *viewers* of agent-written output.

The accepted cost of going fully agent-first (Option B): **ad-hoc questions,
plan drafting, and dashboard insights move to an MCP client.** On a phone that
means an MCP-capable client (Claude Mobile) — the web UI on the phone becomes a
viewer + commit surface, not a place you converse with the coach. We take that
cost deliberately; see "Mobile UX cost" below.

## Build base & dependencies

This design targets the **INTEGRATED** state of the codebase — **not bare
master.** The base is:

> `master` + the MCP-hardening fixes (**PR #23**) + the training-plans
> integration (**PR #24**).

The branch `design/agent-first` is cut from that integrated tree. On this base,
the following already exist and the design assumes them throughout:

- **`src/local_fitness/plans.py`** — the plan subsystem (draft/active,
  `get_draft_plan`, `insert_draft`, `revise_draft`, adherence grading). The
  `/api/plan/draft` endpoint and the TrainingPlan viewer rework both depend on
  it.
- **`scripts/ab_brief.py`** — the A/B brief feature comparison (the
  mandated-content regression net). The single-write-gate reasoning and the
  `ab_brief --run` notes assume it exists and imports
  `local_fitness.agent.briefing._generate`.
- **`web/src/components/TrainingPlan.tsx`** — the existing plan tab (today an
  embedded `ChatPanel` drafting surface) that this design reworks into a
  viewer + commit surface.
- **The `ALLOWED_CHAT_MODELS` whitelist** in `web/server.py` — guarding the
  `/api/chat` model field; removed in Phase 3 along with `ChatRequest`.
- **The plan MCP tools** (`propose_training_plan` / `revise_training_plan`,
  DRAFT-only) registered in `agent/tools.py`.
- **`ALL_TOOLS` is 24** on this base — so adding the new `save_brief` tool
  makes it **25** (the smoke-test bump below is 24 → 25, not from any other
  count).

If this design is read against **bare master** — which lacks the plan
subsystem, `ab_brief.py`, the plan MCP tools, and `TrainingPlan.tsx` — every
reference to those will appear to dangle and the tool count will not line up.
It must be built on the integrated base above to avoid that branch-confusion.

## The key distinction (why the naive "MCP serves data, all logic in the agent" is wrong)

"All logic moves to the agent" is wrong for **deterministic computation**.
CTL/ATL/TSB (Banister EWMA over ~1,800 days), 60-day baselines, plan adherence
grading, Riegel prediction, the snapshot assembler are **math**, not coaching
judgment. An LLM computes these unreliably, expensively, and non-reproducibly.
They MUST stay as deterministic code, served by the REST API and the MCP.

The real cut is **deterministic-compute (stays, served) vs LLM-synthesis
(moves to the agent)**. The codebase makes this clean: the true LLM logic on
the server is two modules — `briefing.py` (the brief, a server-side Claude
loop) and `chat.py` (the UI chat, another server Claude loop). The three UI
chat surfaces all funnel through `chat.py` via `/api/chat`. Everything else is
deterministic.

## What chat is actually load-bearing for (don't under-count it)

There is **no standalone "chat tab."** `ChatPanel` is embedded in three places,
and one of them is the *only* path to a core feature:

1. **TrainingPlan.tsx** — the embedded `ChatPanel` (with `onTurnComplete`
   refetch) is the **only way to create or revise a plan.** Plan drafting
   happens by the chat agent calling `propose_training_plan` /
   `revise_training_plan` (DRAFT-only MCP tools) through `/api/chat`. The empty
   state's "Create a training plan" button seeds that chat. There is no
   non-chat path to a draft.
2. **Today.tsx** — an embedded `ChatPanel` for "tell me more about this
   takeaway" follow-ups (seeded by the takeaway "Ask" action).
3. **Dashboards.tsx** — per-panel `DashboardInsight` components (each a chat
   surface sharing one session via `/api/chat` + `/api/chat/{id}/end`) stream
   the agent's read of each chart.

Retiring `chat.py` and `/api/chat*` therefore **breaks plan creation, the
takeaway follow-up, and all dashboard insights** — not "a chat tab." The UI
rework below replaces each with either a viewer or an explicit "ask your coach
in an MCP client" affordance.

## Decisions (cascading)

- **D1 — Retire both server-side Claude loops.** Delete `briefing.py`'s
  in-server loop wiring and `chat.py`. The web-server PROCESS runs no Claude
  inference. Full agent-first.
- **D2 — Split brief persistence (Claude-FREE) from the brief composer
  (Claude-bound).** Introduce a new module `agent/briefs.py` that owns ALL
  non-LLM brief I/O — read, write, salvage — and imports `schemas` + `db` but
  NOT `claude_agent_sdk`. The Claude-bound headless *composer* stays in
  `briefing.py` (it imports `claude_agent_sdk` + `agent/briefs.py`), runs the
  Claude loop to PRODUCE a `Brief` dict, then hands it to
  `briefs.save_brief(payload)` to persist. The composer is imported ONLY by the
  scheduled `fitness brief` job and `scripts/ab_brief.py` — never by the web
  server or `mcp_server.py`. This split is what makes the "no Claude inference
  symbols / no composer in `server.py` or `mcp_server.py`" invariant genuinely
  true (today both import from `briefing.py`, which runs a Claude loop and
  imports `claude_agent_sdk` at module top) and avoids a
  `tools → briefing → tools` cycle (`briefs.py` does not import `tools.py`).
  (The MCP tool-registration helpers `create_sdk_mcp_server` / `tool` reached via
  `agent/tools.py` stay — they perform no inference; see Invariants.)
- **D3 — Briefs stay file-based, with exactly ONE writer.** Briefs persist as
  `briefings/YYYY-MM-DD.json` (`Brief` pydantic); `/api/brief`,
  `briefs.load_today()` / `briefs.load_latest()`, and the
  `fitness://brief/latest` resource read them. **`briefs.save_brief(payload)` is
  the single function that writes a brief** — used identically by the scheduled
  job (via the composer), the `save_brief` MCP tool, and `ab_brief.py`'s live
  mode. The UI read path is unchanged. (DB-table storage rejected: negligible
  space savings, and it would force changes to `/api/brief` + the resource + a
  migration.) **Concurrent same-day writes are last-writer-wins by design:** the
  atomic temp+`os.replace` prevents file corruption, and when two writers race
  for `briefings/<today>.json` the newest brief simply wins — no merge, no lock.
  Acceptable for a single-user app.
- **D4 — The web SERVER PROCESS never invokes Claude; brief generation is a
  separate process.** Auto-brief = a scheduled **local headless agent run**
  (launchd → `fitness brief` → composes a `Brief` → `briefs.save_brief`).
  On-demand = ask your agent in an MCP client. The UI loses its
  "Generate"/"Regenerate" buttons, its auto-regen, its brief stream, and its
  embedded chat surfaces; it becomes a viewer of the latest agent-written brief
  + a plan-draft review/commit surface.
- **D5 — `/api/brief` falls back to the latest brief, not just today's.** When
  `briefings/<today>.json` is absent, `/api/brief` serves `briefs.load_latest()`
  (most-recent-by-glob) so the Today tab never goes empty while any brief exists
  on disk — protecting the "the app must work for me on my laptop"
  non-negotiable when the Mac slept through the scheduled hour. The
  "no brief yet — ask your coach" empty state shows ONLY when there is NO brief
  on disk at all. The freshness banner is driven by `isBriefStale`
  (Today.tsx:330–333), which compares the **date portion** of the served
  brief's `generated_at` against `data_through_date` —
  `brief.generated_at.slice(0, 10) < dataThrough` — and returns `false` when
  either value is missing. `data_through_date` comes from
  `db.last_known_daily_date()` (server.py:841) and is independent of which brief
  is served. This comparison **survives the `load_latest()` fallback unchanged**:
  when today's file is absent and `load_latest()` serves a prior-day brief, that
  brief's `generated_at` date is older than the current `data_through_date`
  *iff* newer daily data has landed, so the banner lights exactly then. If the
  Mac slept through both the brief AND the data pull, `data_through_date` is also
  stale and is not greater than the brief's `generated_at` date, so the banner
  does NOT light — correct, because the served brief still summarizes the
  freshest data on disk (the comparison reflects data freshness, not brief age).

## Architecture: three planes

```
                              agent/briefs.py (Claude-FREE: read/write/salvage)
                                  ▲ save_brief()        ▲ load_today/load_latest
                                  │                     │
SQLite ──┬─ deterministic compute (baselines, plans, status) ── code, no LLM
         │
         ├─ REST API (data + compute only) ─────────────────────► React UI (viewer)
         │     └─ /api/brief → briefs.load_today() ?? briefs.load_latest()
         └─ MCP server (tools + prompts + resources) ───────────► client agent (synthesis)
               │  save_brief tool → briefs.save_brief()            Claude Code/Desktop/Mobile
               └─ fitness://brief/latest → briefs.load_latest()

briefing.py (Claude-bound composer: imports claude_agent_sdk + agent/briefs.py)
   └─ scheduled `fitness brief` (launchd) / scripts/ab_brief.py ── compose Brief ── briefs.save_brief() ─► briefings/*.json
```

- **Data + compute plane (server, no LLM):** SQLite plus all deterministic
  derivations (`ingest/baselines.py`, `src/local_fitness/plans.py` — at the
  package root, NOT under `agent/` — and `agent/status.py`). Exposed
  two ways: the existing REST endpoints (for the UI) and the MCP tools/resources
  (for agents). "Thin" means *no coaching judgment server-side*, not *dumb*.
- **Brief-I/O plane (`agent/briefs.py`, no LLM):** owns every read and write of
  `briefings/*.json` — `save_brief` (the single integrity gate), `load_today`,
  `load_latest`, `_recent_briefs_summary`, and the JSON-salvage helpers. Imports
  `schemas` + `db`; never `claude_agent_sdk`. Both the web server and the MCP
  server import brief I/O from here.
- **Agent plane (client):** Claude does all synthesis against the MCP, and
  writes its outputs (briefs) back through the `save_brief` MCP tool — which is
  a thin wrapper over `briefs.save_brief`.
- **Scheduled brief job (separate process):** a launchd-driven headless run that
  uses the Claude-bound composer in `briefing.py`, which composes a `Brief` and
  persists it via `briefs.save_brief`. Not part of the web server process.
- **UI:** unchanged for data/charts; the brief is "display today's brief, or
  the latest brief if today's hasn't been written yet"; plan drafting becomes
  review-the-draft + commit; the embedded chat surfaces are removed.

## Components

### Removed from the web-server process (the LLM logic)
- `agent/chat.py` — the interactive REPL / `_ask_once` Claude loop. Deleted.
- The web server's `/api/chat`, `/api/chat/{session_id}/end` endpoints, the
  `ChatRequest` model, the `ALLOWED_CHAT_MODELS` whitelist, the per-session
  `ClaudeSDKClient` plumbing (`_chat_sessions`, `_get_or_create_session`,
  `_options`, the lifespan teardown of chat sessions), and the `"/api/chat"`
  entry in `RATE_LIMITED_PREFIXES`.
- `/api/brief/generate`, `/api/brief/generate/stream` (server-loop triggers),
  the `BriefGenerateRequest` model, and the `"/api/brief/generate"` entry in
  `RATE_LIMITED_PREFIXES`. (With both prefixes gone, `RATE_LIMITED_PREFIXES`
  becomes empty — the rate-limit middleware then no-ops; leave the middleware
  in place so re-adding a Claude-cost path is a one-line change.)
- `web/server.py`'s import of `claude_agent_sdk` *for inference*
  (`ClaudeSDKClient`, `ClaudeAgentOptions`, message-block types) and its import
  of `agent.briefing` (`from ..agent import briefing as briefing_mod`, today at
  server.py:53). (`create_sdk_mcp_server` — a tool-registration helper with zero
  LLM — stays; it builds the MCP tool server.)
- `web/server.py`'s call to `briefing_mod.load_today()` (today at server.py:840)
  is repointed at `agent/briefs.py`: the import becomes
  `from ..agent import briefs as briefs_mod` and `/api/brief` reads
  `briefs_mod.load_today()` with a `briefs_mod.load_latest()` fallback (see D5
  and the API surface). The server NEVER imports the Claude-bound composer
  (`briefing.py`), so its module-load import graph no longer pulls in
  `claude_agent_sdk` transitively.
- `web/mcp_server.py`'s import `from ..agent.briefing import DEFAULT_BRIEFINGS_DIR`
  (today at mcp_server.py:29) is repointed to `agent/briefs.py`, and its
  hand-rolled `_latest_brief_markdown()` glob is replaced by a call to
  `briefs.load_latest()` → `_render_brief()`. This removes the LAST
  `briefing.py` import from the MCP import graph.

### Deliberate dead-code cleanup (tests)
- `tests/test_security.py::test_chat_request_model_whitelist` — deleted along
  with the `ChatRequest`/`ALLOWED_CHAT_MODELS` it guards.
- `tests/test_smoke.py::test_imports` — drop `chat` from the
  `from local_fitness.agent import briefing, chat, prompts` line. (`briefing`
  and `prompts` still import; `chat` no longer exists.)
- `tests/test_smoke.py::test_tool_schemas_well_formed` — bump the hardcoded
  `assert len(agent_tools.ALL_TOOLS) == 24` to `== 25` (the new `save_brief`
  tool). This is the ONLY tool-count test that needs touching:
  `tests/test_mcp_server.py:40`'s `served == {t.name for t in ALL_TOOLS}` is a
  set-equality against the same list, so it auto-tracks `save_brief` with no
  edit.

### New — `agent/briefs.py` (Claude-FREE brief I/O; the module split)
This is the load-bearing refactor. A new module `src/local_fitness/agent/briefs.py`
owns ALL brief persistence + the non-LLM helpers, moved out of `briefing.py`:
- `DEFAULT_BRIEFINGS_DIR` (+ `_default_briefings_dir`) — the env-driven path
  resolution, moved verbatim.
- `load_today()` — today's file only (the existing `briefing.load_today`).
- **`load_latest()`** — NEW: most-recent-by-glob across `briefings/*.json`
  (filenames are `YYYY-MM-DD.json`, so a lexical sort is chronological), skipping
  unparseable/partial files. This is the same pick-most-recent logic
  `mcp_server._latest_brief_markdown()` hand-rolls today; both `/api/brief`'s
  fallback and the `fitness://brief/latest` resource consume it.
- `_recent_briefs_summary()` — the 7-day continuity rendering (reads the dir;
  no LLM). The `brief` MCP prompt handler calls it in-process.
- The JSON-salvage helpers: `_salvage_takeaways`, `_extract_json`,
  `_strip_inline_control_chars`, `_fix_numeric_gaps_outside_strings`. (The
  streaming-only `_iter_partial_takeaways` is NOT needed here — it stays with
  the composer in `briefing.py`, since only the streaming loop uses it.)
- **`save_brief(payload)`** — the canonical write function and single integrity
  gate. In order: salvage → server-stamp BEFORE validation → validate against the
  `Brief` schema → atomic write (temp file + `os.replace`). The stamp matches the
  composer's existing logic at briefing.py:548–550 exactly: `date` and
  `user_name` use `setdefault` (a payload value is honored if present), while
  **`generated_at` is FORCED via unconditional assignment** (`payload["generated_at"]
  = now`, briefing.py:550) so the agent can neither backdate nor omit the field
  the UI stale-detection depends on. (`date` is additionally re-forced to today
  in `save_brief` — see the contract step 2 — to keep the on-disk filename and
  the in-document date consistent.) See the
  "save_brief contract" below. Imports `schemas` + `db`; **never**
  `claude_agent_sdk`, and **never** `tools.py` (so there is no `tools → briefs →
  tools` cycle — the dependency points one way: `tools.py` → `briefs.py`).

### Kept — the Claude-bound composer stays in `briefing.py`, NOT in the web server
- **The headless brief composer** (`briefing.generate_streaming` /
  `briefing._generate`) keeps importing `claude_agent_sdk` and now also imports
  `agent/briefs.py`. It runs the Claude loop to PRODUCE a `Brief` dict, then
  calls `briefs.save_brief(payload)` to persist (it no longer writes the file
  itself). **Precise mechanism (this is the FATAL-1 fix; today the file is
  written TWICE):**
  - **Today's double write (the bug being removed):** `generate_streaming`'s
    `save=True` branch writes `briefings/<date>.json` to `DEFAULT_BRIEFINGS_DIR`
    itself (briefing.py:558–563), and then `generate_and_save` writes a *second*
    copy to `out_dir or DEFAULT_BRIEFINGS_DIR` (briefing.py:587–590). Neither
    goes through any integrity gate, and `out_dir` is dead capability — `cli.py`
    calls `generate_and_save(model=...)` with no `out_dir` (cli.py:148), so the
    second write always targets `DEFAULT_BRIEFINGS_DIR` too.
  - **`generate_streaming`'s `save=True` branch is rebased onto `save_brief`:**
    instead of writing the file itself, it calls `briefs.save_brief(payload)`
    (the same salvaged-stamped payload it builds today at briefing.py:548–552).
    The `save` flag's meaning narrows to "persist via the gate" (`save=True`) vs
    "return the brief without persisting" (`save=False`, the eval/scoring path).
    The direct `out_path.write_text` at briefing.py:558–563 is deleted; the
    composer no longer touches the filesystem.
  - **The `done` event keeps emitting a validated `Brief` — supplied by
    `save_brief`, not re-validated in the composer.** Today `generate_streaming`
    validates the payload itself (`Brief.model_validate`, briefing.py:552) and
    the `done` event emits `brief.model_dump()` (briefing.py:565); the
    `/api/brief/generate/stream` endpoint forwards that `done` event verbatim
    (server.py:899–904, only annotating `data_through_date`). After the rebase
    the composer must NOT validate a second time in parallel with `save_brief`'s
    own validate — that would be two validates of the same payload, and on the
    `save=True` path the on-disk write and the streamed object could diverge.
    The resolution (see "save_brief contract"): **`briefs.save_brief(payload)`
    validates ONCE and RETURNS the validated `Brief`** (its result becomes
    `{saved, date, path, brief}`). `generate_streaming`'s `save=True` branch
    then emits the SAME `Brief` object `save_brief` validated and persisted —
    `yield {"type": "done", "brief": result["brief"].model_dump()}` — so there is
    a single validate + single write and the streamed `done` object is
    byte-for-byte the object on disk. On the `save=False` path (no persist), the
    composer validates locally as today to produce the `done` Brief, since
    `save_brief` is not called.
  - **This makes the Phase 1 `generate_streaming` rebase a BEHAVIOR change to
    the still-live `/api/brief/generate/stream` endpoint, not purely additive.**
    That endpoint remains LIVE through Phases 1–2 (it is only removed in
    Phase 3 step 13), so during the migration window an on-demand UI regen runs
    `save=True` → `briefs.save_brief`. The write path therefore changes for that
    live endpoint from a bare `out_path.write_text` to `save_brief`'s
    salvage → server-stamp → validate → atomic-write path. The user-visible
    contract is preserved (the `done` event still carries a validated `Brief`,
    now the one `save_brief` returned, and the endpoint still annotates
    `data_through_date`), but the persistence mechanism underneath it is new
    code running on a live path — it MUST work during the window. Call this out
    honestly in Phase 1 (it is not "additive only"). After Phase 3 the stream
    endpoint is gone, so this only matters during the window.
  - **`generate_and_save` drops its second direct write entirely.** It runs the
    composer with `save=True` (so persistence happens exactly once, inside
    `generate_streaming` → `briefs.save_brief`) and returns
    `Path(result["path"])` extracted from `save_brief`'s
    `{saved, date, path, brief}` dict. The `out_dir` parameter is **DROPPED** —
    `cli.py` never passes one
    (cli.py:148 calls `generate_and_save(model=...)`), so it is dead capability;
    remove it from the signature.
  - **Return contract preserved:** `generate_and_save` stays typed `-> Path` and
    `cli.py:149` (`fitness brief`) still echoes that Path
    ("Brief written to: {path}"). The CLI output is unchanged.
  - **Net invariant (the precise single-writer statement):** there is exactly
    ONE write site for `briefings/<date>.json` — `briefs.save_brief` — and it is
    invoked once per brief, by the composer (via `generate_streaming`), the MCP
    `save_brief` tool, and `ab_brief --run` alike. The "single-writer" acceptance
    grep (`briefings/` write sites → exactly one, in `briefs.py`) then passes.
    There is also exactly ONE validate per persisted brief: `save_brief`
    validates and returns the `Brief`, and `generate_streaming` emits that same
    object in its `done` event rather than validating again — no parallel
    in-composer validate on the `save=True` path.
  - (After Phase 3, `cli.py` is the only remaining caller of `generate_and_save`.)
  The composer keeps (a) the scheduled brief job and (b) `scripts/ab_brief.py`
  working; `ab_brief.py` imports `local_fitness.agent.briefing._generate`
  directly (and `_generate` drains `generate_streaming`, so `ab_brief --run`
  persists through the same gate — see the `ab_brief --run` note below).
  *Invariant:* nothing in `web/server.py`'s or `web/mcp_server.py`'s import graph
  pulls in `briefing.py` (the Claude-bound composer); they import brief I/O from
  `agent/briefs.py` only. (Note: `claude_agent_sdk` IS still in the graph via
  `agent/tools.py`'s `create_sdk_mcp_server` / `tool` import — that is MCP
  tool-registration plumbing with zero inference, and is allowed. The invariant
  is "no inference / no composer," not "no `claude_agent_sdk` symbol at all.")
- **`briefing_prompt()`** (lives in `agent/prompts.py`) — KEPT. `scripts/score_prompt.py`
  and `tests/test_score_prompt.py` cross-check it against the schema, and the
  new `brief` MCP prompt reuses its text. Unchanged.

### Added (so the agent + scheduled job can do it all)
- **`save_brief(brief_json)` MCP tool** (in `agent/tools.py`, `ALL_TOOLS`): a
  thin wrapper that calls `briefs.save_brief(brief_json)` — the integrity gate
  lives in `agent/briefs.py`, not in the tool. The tool DROPS the returned
  `brief` key and returns `_text({"saved", "date", "path"})` like every other
  tool (it never `json.dumps` the pydantic `Brief`); `_err(...)` on validation
  failure. Adding it to `ALL_TOOLS` bumps the tool count from 24 → 25 (one
  smoke-test edit; see below). See "save_brief contract" below.
- **`brief` MCP prompt** (in `web/mcp_server.py`, standalone server only):
  returns the briefing instructions + the `Brief` JSON schema + continuity
  rules + the assembled data, so an agent (or `fitness brief`'s headless run)
  composes a schema-valid `Brief` and calls `save_brief`. Parallels the
  existing `coach` prompt — and resolves its inputs in-process exactly the way
  `coach` resolves `user_name` + `assemble_status()`. See "brief prompt runtime
  inputs." **This requires editing BOTH `mcp_server.py` prompt handlers, not
  just "paralleling coach":** (1) append a second `types.Prompt` to
  `_list_prompts` (mcp_server.py:185, currently returns a one-element list), and
  (2) convert `_get_prompt`'s `if name != "coach": raise ValueError(...)` guard
  (mcp_server.py:212) into a two-arm dispatch (`coach` | `brief`). Don't miss the
  guard — leaving it as-is makes the `brief` prompt unreachable.
- **`GET /api/plan/draft` REST endpoint** (in `web/server.py`): reads
  `plans.get_draft_plan()` (wrapped in `_assemble_plan_detail`) so the
  TrainingPlan viewer can show "you have a draft — review and commit" without a
  chat surface. (`/api/plan` already returns both `active` and `draft`; the
  dedicated endpoint lets the viewer poll the draft cheaply and keeps the
  draft-review flow explicit.)

### Kept untouched
- `/api/brief` keeps its route, response shape, and `data_through_date`
  field — only its read source changes: `briefs.load_today()` with a
  `briefs.load_latest()` fallback (D5), in place of `briefing_mod.load_today()`.
- Every data/compute endpoint (`/api/status`, `/api/today`,
  `/api/training-load`, `/api/plan`, `/api/plan/{id}/commit`, DELETE
  `/api/plan/{id}`, `/api/workouts`, charts, notes, sync, `/api/config`,
  `/api/auth/verify`, `/health`).
- All deterministic compute (`baselines.recompute`, `plans.*`,
  `status.assemble_status`).
- The plan write boundary (draft via MCP tools, commit/delete via REST — human).
- The MCP tool/resource surface built from `make_server()`, including the
  mounted `/mcp/` streamable-HTTP server and `fitness mcp-stdio`.
- The auth + security-headers middleware. The rate-limit middleware stays in
  place even though its prefix tuple is now empty.

## UI rework (real frontend work, not "remove a button")

### Today.tsx — brief becomes a true viewer
The current component is **not** a passive viewer: on empty it auto-regenerates
(`autoRegenAttemptedRef` → `regenerateBrief`), it STREAMS takeaways
(`api.briefGenerateStream` → `streamedTakeaways`), it has a Generate/Regenerate
button, and a stale banner that itself triggers a regen. Under B, rewrite the
brief lifecycle to:

- Fetch the brief from `/api/brief` and render it. **No auto-regen, no
  stream, no `streamedTakeaways`, no `briefLoading`/`regenerateBrief`, no
  `seedRequest`** (the takeaway "Ask" action is removed — there's no embedded
  chat to seed). Because `/api/brief` now falls back to `load_latest()` (D5),
  the tab shows the most-recent brief when today's hasn't been written yet,
  rather than going empty.
- Empty state: "No brief yet — ask your coach to write one." This shows ONLY
  when `briefings/` is empty (no brief on disk at all) — i.e. `/api/brief`
  returns `{brief: null}`. As soon as ANY brief exists, the fallback serves it
  and the empty state never appears. No CTA that hits the server for inference.
- **Keep the stale banner.** `isBriefStale` (Today.tsx:330–333) needs **no
  logic change** for the fallback — verified against the actual function. It is:
  `if (!brief?.generated_at || !dataThrough) return false; return
  brief.generated_at.slice(0, 10) < dataThrough` — a lexical compare of the
  served brief's `generated_at` *date portion* against `data_through_date`
  (which the component already holds in `dataThrough`, sourced from
  `db.last_known_daily_date()` at server.py:841, independent of which brief is
  served). Because the comparison reads only the served brief's `generated_at`
  and `data_through_date` — neither of which changes shape under the
  `load_latest()` fallback — it survives the fallback UNCHANGED: when a prior-day
  brief is served and newer data has landed, that brief's `generated_at` date is
  `< data_through_date` and the banner lights; when no new data landed (Mac
  slept through both brief and pull), `data_through_date` is also stale and the
  banner correctly stays dark. The ONLY change is presentational: the banner
  becomes *informational* ("Newer data landed since this brief was written — ask
  your coach for a fresh one") instead of a regen button.
- Remove the `ChatPanel` import + the `<ChatPanel seedRequest=… />` block and
  the divider above it.
- Keep everything deterministic: the heatmap card, `TodayGoal` (reads
  `/api/plan`), the recent-workouts table. These are unaffected.
- Remove the now-dead `api.briefGenerateStream` / `api.briefGenerate` from
  `web/src/lib/api.ts`, plus the `BriefStreamEvent` type usage.

This is real work: it touches state, effects, the render tree, and the API
client — scope it as such.

### TrainingPlan.tsx — viewer + commit; drafting moves to the agent
- **Remove** the embedded `<ChatPanel … onTurnComplete={refetch} />` card and
  the `seedRequest` plumbing.
- The tab becomes: show the **active** plan + show a pending **DRAFT** (via
  `GET /api/plan/draft`, or the `draft` field of `/api/plan`) + **Commit**
  (existing `POST /api/plan/{id}/commit`) + **Delete** (existing `DELETE
  /api/plan/{id}`). The `DraftBanner` stays but its copy changes from "riff with
  the coach below, then commit" to "Review the draft your coach wrote, then
  commit to start tracking."
- **Plan drafting moves to the agent**: `propose_training_plan` /
  `revise_training_plan` are called from an MCP client (Claude Desktop/Code/
  Mobile), not the web UI. The UI never proposes/revises; it only reviews +
  commits + deletes.
- **Empty-state CTA** changes from "Create a training plan" (which seeded a
  chat) to "Ask your coach to draft a plan" — copy that points the user at
  their MCP client. No button that opens a server chat.
- Add a small `api.planDraft()` to `api.ts` hitting `GET /api/plan/draft`
  (or reuse `/api/plan`'s `draft`).

### Dashboards.tsx — data viz stays, insights move to the agent
- **Remove** every `<DashboardInsight />` (the three per-panel chat surfaces),
  the shared `sessionId` + `api.chatEnd` teardown, and the `ModelToggle` (it
  only steered the insight model).
- The charts (`ActivityHeatmap`, pace-efficiency, strength-volume) and their
  range toggles **stay** — they read `/api/activity-heatmap`,
  `/api/pace-efficiency`, `/api/strength-volume`, all unchanged.
- The `DashboardInsight` component and its prompts become dead UI; delete the
  component or leave it unreferenced (prefer delete to avoid confusion).
- Insight questions move to the agent: a user opens Claude Mobile/Desktop and
  asks the same questions against the MCP (the prompts that were canned in
  `DashboardInsight` are good seed material for docs, not server code).

### ChatPanel + api.ts
- `ChatPanel.tsx` has no remaining mount point — delete it (and the now-dead
  `api.chat` / `api.chatEnd` in `api.ts`, plus the `ChatEvent`/`BriefStreamEvent`
  type imports that only those used).

### Mobile UX cost (accepted)
Plan drafting, takeaway follow-ups, and dashboard insights all previously
worked in the phone's web UI (chat embedded in the page). Under B they require
an **MCP client on the phone** (Claude Mobile pointed at the LAN MCP endpoint).
The phone web UI is reduced to: view the brief, view/commit/delete a plan draft,
browse charts and workouts. This is the deliberate trade of going agent-first —
one synthesis surface (the agent) instead of two (agent + server chat). Document
it; don't pretend the phone keeps conversational coaching in-app.

## Data flow

- **Read (UI):** browser → REST → SQLite + deterministic compute. Unchanged.
- **Read (agent):** Claude → MCP tools/resources → SQLite + deterministic
  compute. Unchanged.
- **Brief (write):** scheduled `fitness brief` (the composer in `briefing.py`)
  OR the `save_brief` MCP tool (you, ad hoc, in an MCP client) OR
  `ab_brief.py`'s live mode → all funnel through **`briefs.save_brief(payload)`**
  → salvage → server-stamp → validate against `Brief` → atomic write of
  `briefings/<today>.json`. Exactly one writer; one integrity gate.
- **Brief (read, UI):** browser → `/api/brief` → `briefs.load_today()`, falling
  back to `briefs.load_latest()` when today's file is absent (D5). The change is
  *who wrote the file* and that the UI never goes empty while any brief exists.
- **Brief (read, agent):** MCP `fitness://brief/latest` resource →
  `briefs.load_latest()` (latest-by-glob across `briefings/*.json`, NOT
  today-only) → `_render_brief()`.
- **Plan (write):** agent in an MCP client → `propose/revise_training_plan`
  (DRAFT-only) → `plans.insert_draft`/`revise_draft`. **Commit/delete:** human
  via REST. Unchanged boundary.
- **Plan (read, UI):** browser → `/api/plan` + `/api/plan/draft` → SQLite.

## Brief generation mechanism

- **Auto (the scheduled job):** a launchd `StartCalendarInterval` job runs
  **`fitness brief`** — the retained headless Agent-SDK composer in
  `briefing.py` — which pulls, recomputes baselines, composes a `Brief`, and
  persists it via `briefs.save_brief` (NOT a direct file write — see the FATAL-1
  note in Migration).
  - **Prefer `fitness brief` over `claude -p "/fitness:brief"`** because it
    reuses the existing composer code, avoids juggling MCP transport/auth for a
    cron-style job, and keeps `ab_brief.py` / `score_prompt.py` working against
    the same code path. The migration target is "web server holds no LLM," NOT
    "the brief must transit the MCP." `claude -p "/fitness:brief"` (against the
    `brief` MCP prompt) remains a valid alternative — use it if you want the
    scheduled job to exercise the exact same prompt path an interactive agent
    would. Either way, `briefs.save_brief` is the write gate.
  - **Credential / auth model of the scheduled run (operational, on the
    daily-driver path).** The composer talks to the MCP **in-process** via
    `make_server()` — the Agent SDK is given
    `mcp_servers={agent_tools.SERVER_NAME: agent_tools.make_server()}`
    (briefing.py:368–370), an in-process SDK MCP server, NOT the mounted
    streamable-HTTP `/mcp/` endpoint. So the scheduled run needs **no bearer API
    token (`LOCAL_FITNESS_API_TOKEN`) and no allowed-host config** — there is no
    MCP transport to authenticate. It needs only **Claude credentials at
    runtime** (`CLAUDE_CODE_OAUTH_TOKEN`, exactly as `scripts/ab_brief.py`
    relies on for the Max subscription). This *strengthens* the "prefer `fitness
    brief` over `claude -p`" rationale above: there is no MCP transport or auth
    to juggle, only Claude creds.
  - **launchd runs in a minimal environment** — no login shell, so none of the
    interactive-shell env (incl. `CLAUDE_CODE_OAUTH_TOKEN`) is present by
    default. The plist (Phase 1 step 6, the net-new artifact) MUST therefore
    both:
    - **Inject `CLAUDE_CODE_OAUTH_TOKEN`** — either via the plist's
      `EnvironmentVariables` dict, OR by having the job load the project `.env`
      (which the app already reads) before invoking the composer. Loading `.env`
      is preferred (one source of truth; the token lives where the rest of the
      app config does).
    - **Load the project venv and run from the project dir** — i.e.
      `uv run fitness brief` with `WorkingDirectory` set to the project root, so
      the right interpreter + dependencies + project-relative path defaults
      resolve.
  - **Env-driven-pattern docs (required by the project convention).** Document
    `CLAUDE_CODE_OAUTH_TOKEN` in `.env.example` (commented-out placeholder + a
    one-line note that the scheduled `fitness brief` job needs it) and in
    `docs/deployment.md` (so future-you wires it into whatever runs the cron-style
    job). It is a **secret** → env var only, never defaulted to a real value.
  - **Scheduling artifact is NET-NEW critical-path work — not a one-liner.** The
    README references `ops/install-launchd.sh` and an `ops/` plist, but **`ops/`
    does not exist in this branch** (it's untracked / never written). This
    migration must author the launchd plist + installer from scratch (or
    document the cron/systemd equivalent). It is the load-bearing brief-delivery
    path on a **not-always-on laptop**, will **never have run** before this
    migration, and is far higher-risk than any unit test in this plan — weight
    it accordingly in estimation and verification (the Phase-1 gate below exists
    precisely for this).
  - **Laptop-asleep catch-up (honest):** launchd runs a missed
    `StartCalendarInterval` job at next wake — but only ONCE, and the run uses
    the wall-clock time it actually fires. If the Mac is closed past the brief
    hour and opened late morning, the brief is written late (and `generated_at`
    reflects the late time, so the stale banner behaves correctly). If the Mac
    is asleep the whole day, no brief is written that day and the UI shows the
    last one — the user can ask their agent for one anytime. We accept this for
    a personal app on a not-always-on laptop.
- **On-demand:** ask the agent in any MCP client; it composes + `save_brief`s.
- The web server PROCESS is never in either path.

## save_brief contract (necessary AND sufficient)

`briefs.save_brief(payload: dict)` (wrapped by the `save_brief` MCP tool) must be
more than a thin validator — it inherits the robustness the server loop had.
**Stamping happens BEFORE validation**, so a clean agent payload like
`{"takeaways": [...]}` (no `date`/`user_name`/`generated_at`) validates — this
mirrors the existing `briefing.generate_streaming` logic at briefing.py:548–550
(`setdefault("date", ...)`, `setdefault("user_name", ...)`, and the FORCED
`payload["generated_at"] = ...` at briefing.py:550). In order:

1. **Salvage malformed JSON first.** Reuse the helpers now in `agent/briefs.py`
   (`_salvage_takeaways`, `_extract_json` / `_strip_inline_control_chars` /
   `_fix_numeric_gaps_outside_strings`) to repair the deviating shapes models
   routinely emit (nested `takeaways`, raw control chars in keys, stray numeric
   whitespace). `payload` may arrive as a dict already, so accept a dict and run
   `_salvage_takeaways` on it; if a tool transport hands it a string, run the
   full `_extract_json` path.
2. **Stamp server-side BEFORE validating**, never trusting the payload:
   `payload.setdefault("user_name", db.get_setting("user_name", DEFAULT_USER_NAME))`,
   `payload["date"] = date.today().isoformat()` (force — ignore any payload
   `date`), `payload["generated_at"] = datetime.now().isoformat()` (force).
   (`generated_at` powers the UI stale-detection; it MUST be server-set so the
   agent can't backdate or omit it. Forcing `date` to today keeps the on-disk
   filename and the in-document date consistent.)
3. **Validate** the salvaged, stamped payload against the `Brief` pydantic schema
   (`agent/schemas.py`). On failure: return `_err("brief failed schema
   validation: <detail>")` and write **nothing**. Because step 2 already
   supplied the required `date`/`user_name`, a clean `{"takeaways": [...]}`
   payload passes here.
4. **Atomic write**: write `briefings/<today>.json` via temp file + `os.replace`
   so `/api/brief` (`load_today` / `load_latest`) and `fitness://brief/latest`
   never read a half-written file. Write the SAME validated `Brief` object from
   step 3 (`brief.model_dump_json(indent=2)`).
5. Return `{"saved": true, "date": "<YYYY-MM-DD>", "path": "...", "brief":
   <validated Brief>}`. The returned `brief` is the very object validated in
   step 3 and persisted in step 4 — a SINGLE validate + SINGLE write. This is
   what lets the composer's `generate_streaming` (`save=True`) emit its `done`
   event from `save_brief`'s result instead of validating a second time in
   parallel, so the streamed `Brief` and the on-disk `Brief` cannot diverge.
   **Scope of the "single validate" invariant:** it means "one validate before
   the on-disk WRITE," i.e. one validate per persisted brief inside the write
   gate. It does NOT forbid `_generate` (briefing.py:579) from running its
   in-memory `Brief.model_validate(last_brief)` on the drained `done`-event dict
   before returning the `Brief` to the CLI — that is harmless (no second write,
   no second gate) and is load-bearing for `_generate`'s `-> Brief` return, so it
   must NOT be stripped when implementing "single validate."
   **Two distinct return shapes, by caller:**
   - **IN-PROCESS callers** (`generate_streaming`'s `done` event,
     `generate_and_save` extracting the `Path`) consume `briefs.save_brief`'s
     return dict directly: `{saved, date, path, brief}` where `brief` is the
     validated `Brief` OBJECT. This is the single-validate source.
   - **The `save_brief` MCP tool** (in `tools.py`) is a thin wrapper: it calls
     `briefs.save_brief(payload)`, **DROPS the `brief` key**, and returns
     `_text({"saved": ..., "date": ..., "path": ...})` — the content-block shape
     every other tool uses (tools.py:97). The agent doesn't need the full `Brief`
     echoed back; `saved`/`date`/`path` is the sensible tool return. On
     validation failure it returns `_err(...)`. It NEVER passes the pydantic
     `Brief` object through `_text` / `json.dumps` (that would raise `TypeError`).

**Honest limit:** pydantic validation enforces *shape* (one top-level
`takeaways` key, 1–5 items, valid tone/metric enums) — it does NOT enforce the
brief's **semantic mandates** (a steps takeaway every brief, a workout takeaway,
plan-awareness when a plan is active). Those depend on the `brief` prompt being
followed. So `save_brief` guarantees the UI never renders a structurally broken
brief, but brief *quality* still rides on prompt-following — a real, accepted
risk, the same one the server loop carried. The mandated-content regression net
stays in `scripts/ab_brief.py` (the A/B feature comparison checks `has_steps`,
plan-folding, takeaway count).

**Note on `ab_brief --run` (live mode):** it composes via the same composer and
therefore persists through `briefs.save_brief` — i.e. it shares the single write
gate and overwrites the live `briefings/<today>.json`. Concretely,
`ab_brief.py` (in the run loop) calls `briefing._generate(model=...)`, which drains
`generate_streaming` **without passing `save=`** — so it takes the *default*
`save=True` and persists through the gate. (A reader auditing `ab_brief.py`
won't see `save=True` written there; it's the generator default.) This is
pre-existing behavior (`save=True` today), not a regression this design
introduces; `--mock` remains the side-effect-free CI path (it never calls
`_generate`).

## brief prompt runtime inputs

The `brief` MCP prompt handler resolves its inputs **in-process at request
time**, mirroring how the existing `coach` prompt resolves `user_name` +
`assemble_status()`:

- `user_name` ← `db.get_setting("user_name", prompts.DEFAULT_USER_NAME)`.
- `daily_step_goal` ← `int(db.get_setting("daily_step_goal", "10000"))` (same
  parse-with-fallback as `briefing.generate_streaming`).
- `recent_briefs_summary` ← `briefs._recent_briefs_summary()` (reads the last
  7 days from `briefings/`; this helper moved to `agent/briefs.py` with the rest
  of the brief I/O, so the MCP prompt handler resolves it without importing the
  Claude-bound composer).
- The prompt body ← `prompts.briefing_prompt(user_name, daily_step_goal,
  recent_briefs_summary)` plus the assembled snapshot (`assemble_status()` /
  `_render_status`, as `coach` already does), so the agent has the data inline
  and a clear instruction to call `save_brief`.

One source of truth: the embedded schema/instructions come from
`briefing_prompt()` + `agent/schemas.py`, the same modules `save_brief`
validates against — so the prompt and the validator can't drift.

## API surface

```
# New module function (agent/briefs.py — the single writer, Claude-FREE)
briefs.save_brief(payload: dict) -> dict
    Salvage → stamp (date=today, user_name=setdefault, generated_at=now) BEFORE
    validation → validate ONCE against Brief → atomic-write briefings/<today>.json.
    On invalid: _err("brief failed schema validation: <detail>"), write nothing.
    On valid: return {"saved": true, "date": "<YYYY-MM-DD>", "path": "...",
        "brief": <the validated Brief OBJECT>} — the SAME object validated and
        written, so generate_streaming's done event emits it without re-validating.
        This dict (with the live Brief object) is the IN-PROCESS shape; the
        save_brief MCP tool drops the brief key before _text-wrapping (see below).
briefs.load_today() -> Brief | None      # today's file only
briefs.load_latest() -> Brief | None     # most-recent-by-glob across briefings/*.json

# Changed composer signatures (agent/briefing.py — Claude-bound; out_dir dropped)
briefing.generate_streaming(model=..., save=True)   # save=True now persists via
    briefs.save_brief (no direct write); the done event emits the Brief
    save_brief returned (single validate); save=False validates locally and
    returns the brief without persisting
briefing.generate_and_save(model=...) -> Path        # out_dir parameter REMOVED
    (dead — cli.py never passed it); runs the composer save=True, returns
    Path(save_brief_result["path"]); cli.py:149 echoes it unchanged

# New MCP tool (agent/tools.py, ALL_TOOLS) — thin wrapper over briefs.save_brief
save_brief(brief_json: dict) -> dict      # calls briefs.save_brief(brief_json),
    # then DROPS the brief key and returns _text({"saved", "date", "path"}) like
    # every other tool (tools.py:97). On validation failure: _err(...).
    # NEVER json.dumps the pydantic Brief object — only the {saved,date,path} scalars.

# New MCP prompt (web/mcp_server.py, standalone server — stdio + streamable-HTTP)
brief()  -> a user-role prompt = briefing_prompt() instructions + Brief schema +
            continuity rules + assembled snapshot; resolves user_name /
            daily_step_goal / recent_briefs_summary in-process; instructs the
            agent to compose a schema-valid Brief and call save_brief.

# New REST endpoint (web/server.py)
GET /api/plan/draft        # plans.get_draft_plan() via _assemble_plan_detail; null when none

# Removed REST endpoints
DELETE  /api/chat, /api/chat/{session_id}/end
DELETE  /api/brief/generate, /api/brief/generate/stream

# Unchanged REST endpoints (UI) — same route + response shape
GET /api/brief            # briefs.load_today() ?? briefs.load_latest() — today's, else most-recent (now agent-written)
GET /api/plan             # { active, draft } — both via _assemble_plan_detail
POST /api/plan/{id}/commit, DELETE /api/plan/{id}    # human commit / archive
GET /api/status, /api/today, /api/training-load, /api/workouts,
    /api/metric/{name}, /api/activity-heatmap, /api/strength-volume,
    /api/pace-efficiency, /api/workout/{activity_id}, /api/notes,
    /api/config, /api/auth/verify, /health
POST /api/sync ; GET /api/sync/status   # SyncIndicator on Today; kept
POST /api/notes, DELETE /api/notes/{line_index}    # notes CRUD; kept
```
**This list is illustrative, not exhaustive — the rule is: ONLY `/api/chat*` and
`/api/brief/generate*` are removed; every other endpoint is kept untouched.** Do
not read an endpoint's absence from this list as a removal signal (e.g.
`/api/sync*` backs the Today sync indicator and stays).

## Invariants

### Checkable by inspection
- **No Claude INFERENCE symbols in the server/MCP source, and neither imports
  the Claude-bound composer** (the precise, achievable statement of "the
  server/MCP process holds no Claude inference"). **This is an END-STATE
  invariant — it holds at the END of Phase 3, not during Phase 1.** Through
  Phases 1–2, `web/server.py` still imports `agent/briefing.py` (the
  `/api/brief/generate*` endpoints keep it load-bearing); that import is dropped
  in Phase 3 step 13 together with those endpoints. `mcp_server.py` drops its
  `agent/briefing` import in Phase 1 step 3 (it had only the read path). Only
  once Phase 3 completes do BOTH source files satisfy the grep below. Concretely:
  no Claude
  *inference* symbols (`query`, `ClaudeSDKClient`, `ClaudeAgentOptions`, a
  running agent loop) appear in `web/server.py` or `web/mcp_server.py` source,
  and neither imports the Claude-bound composer module (`agent/briefing.py`).
  After the split, both reach brief I/O only through `agent/briefs.py` (which
  imports `schemas` + `db` only). The tool-registration helpers
  `create_sdk_mcp_server` / `tool` (used by `agent/tools.py` to build the MCP
  tool server) are explicitly allowed — they perform NO inference;
  `agent/tools.py` stays in the graph (it backs `make_server()`), so importing
  the server DOES pull `claude_agent_sdk` into `sys.modules` via that plumbing,
  and that is expected. Checkable by grep:
  `grep -nE 'query\(|ClaudeSDKClient|ClaudeAgentOptions' src/local_fitness/web/server.py src/local_fitness/web/mcp_server.py`
  returns nothing in code (only comment/string matches like "query the fitness
  DB" are acceptable — the check targets code), AND grep confirms neither
  `server.py` nor `mcp_server.py` imports `agent.briefing`.
- **Brief generation is a SEPARATE process.** `agent/briefing.py` (the composer)
  is imported ONLY by the `fitness brief` CLI command and `scripts/ab_brief.py` —
  never by `web/server.py` or `web/mcp_server.py`.
- **The scheduled run needs only Claude creds — no MCP transport auth.** The
  composer wires the MCP **in-process** via `make_server()`
  (`mcp_servers={SERVER_NAME: make_server()}`, briefing.py:368–370), not the
  mounted `/mcp/` HTTP endpoint, so the launchd job requires neither
  `LOCAL_FITNESS_API_TOKEN` nor an allowed-host — only `CLAUDE_CODE_OAUTH_TOKEN`
  at runtime. The plist injects that token (via `EnvironmentVariables` or by
  loading the project `.env`) and runs `uv run fitness brief` from the project
  dir. `CLAUDE_CODE_OAUTH_TOKEN` is documented in `.env.example` + `docs/deployment.md`.
- **`agent/briefs.py` is acyclic and Claude-free.** It imports `schemas` + `db`,
  NOT `claude_agent_sdk` and NOT `tools.py`. The dependency arrow is one-way:
  `tools.py` → `briefs.py`, `briefing.py` → `briefs.py`, `server.py` →
  `briefs.py`, `mcp_server.py` → `briefs.py`. No `tools → briefs → tools` cycle.
- All deterministic compute (`baselines`, `plans`, `status`) stays in code and
  is unchanged.
- **`briefs.save_brief` is the SINGLE writer of `briefings/*.json`.** There is
  exactly ONE write site (the temp+`os.replace` inside `save_brief`), invoked
  once per brief by the scheduled job (via the composer's `generate_streaming` →
  `save_brief`), the `save_brief` MCP tool, and `ab_brief --run` (via
  `_generate` → `generate_streaming` default `save=True`). No other code path
  writes the file: `generate_streaming`'s old direct write and
  `generate_and_save`'s second direct write are BOTH removed, and the dead
  `out_dir` parameter is dropped. It salvages → stamps `date`/`user_name`
  (`setdefault`) + `generated_at` (forced) server-side BEFORE validation →
  validates against `Brief` → atomic-writes (temp + `os.replace`). Checkable:
  `grep -rn "briefings/" src/local_fitness | grep -E "write_text|os.replace|\.write\("`
  resolves to exactly one write site, in `agent/briefs.py`.
- `/api/brief` reads `load_today()` then falls back to `load_latest()`;
  `fitness://brief/latest` reads `load_latest()`. All read `briefings/*.json` via
  `agent/briefs.py`; the atomic write protects every reader.
- `RATE_LIMITED_PREFIXES` is empty after removal; the rate-limit middleware is
  retained (no-op) so re-adding a Claude-cost path is one line. (If a future
  reviewer prefers, the middleware may be dropped — but the auth + headers
  middleware MUST stay.)
- **`cli.py` imports cleanly with `chat.py` gone.** `cli.py` currently has a
  module-top `from .agent import chat as chat_mod` (cli.py:31) and two commands
  that use it — `chat` (→ `chat_mod.run`) and `ask` (→ `chat_mod.ask`); these
  were the server-chat REPL. Deleting `agent/chat.py` while that import stays
  would break EVERY `fitness` subcommand at import time — including
  `fitness brief` and `fitness serve`, the laptop daily-driver paths this design
  promises to keep. So Phase 3 ALSO **removes `cli.py`'s
  `from .agent import chat as chat_mod` import AND the `chat` and `ask`
  subcommands** (and prunes their mention from the module docstring + the
  `setup` "Next:" hint). `fitness brief` is KEPT — it now composes via the
  Claude-bound composer → `briefs.save_brief`. Acceptance check: after the chat
  removal, `uv run fitness --help` and each of
  `brief`/`serve`/`pull`/`setup`/`baselines`/`mcp-stdio` import and run cleanly,
  with no top-level import of the deleted `chat` module.

### Requires tests
- **`briefs.save_brief` accepts a clean agent payload** — `{"takeaways": [...]}`
  with NO `date`/`user_name`/`generated_at` validates and writes, because the
  server-stamp step runs BEFORE `Brief.model_validate`. (Guards the
  stamp-before-validate ordering.)
- **`briefs.save_brief` rejects schema-invalid JSON** (bad tone,
  non-list/empty/over-5 takeaways, unsalvageable shape) with `_err` and writes
  **no** file.
- **`briefs.save_brief` salvages a deviating shape** (e.g.
  `{snapshot:…, takeaways:[…]}` or a string with a ```json fence) into a valid
  on-disk brief — the server loop's robustness is preserved.
- **`briefs.save_brief` stamps `generated_at` server-side** even when the payload
  omits it or supplies a bogus value, and stamps `date = today` ignoring any
  payload `date`.
- **`briefs.save_brief` write is atomic** — no reader ever observes a partial
  file (temp-file-then-rename; assert the temp file isn't the served path).
- **The `save_brief` MCP tool delegates to `briefs.save_brief`** — a tool-level
  call writes the same on-disk brief a direct `briefs.save_brief` call would
  (the tool is a thin wrapper; no second write path).
- **`briefs.load_latest()` picks the most-recent brief by filename date** and
  skips unparseable/partial files, returning `None` on an empty/missing dir.
- **generated_at round-trip (replaces the ill-posed "byte-identical" test):**
  `briefs.save_brief` writes a schema-valid brief with a server-set
  `generated_at` that `/api/brief` serves and the UI renders **identically to a
  server-written brief**, with **stale-detection intact** (older `generated_at`
  than `data_through_date` → stale banner; equal/newer → no banner). Assert the
  served `brief.generated_at` is present and parseable, not that bytes match.
- **`GET /api/plan/draft`** returns the draft (via `_assemble_plan_detail`) when
  one exists and `null` when none — and does NOT require chat.
- **Removing the chat/generate endpoints breaks no retained data endpoint** —
  the existing UI data-endpoint tests (`test_web_plan.py`, etc.) stay green; a
  request to `/api/chat` or `/api/brief/generate` now 404s.
- **`/api/brief` falls back to the latest brief when today's is absent** (D5):
  with `briefings/<yesterday>.json` present but no today file, `/api/brief`
  returns yesterday's brief (`cached: true`, `data_through_date` set) so the
  Today tab is non-empty. The stale banner lights **only** when the served
  brief is a prior-day brief AND newer daily data has landed since it was
  written — i.e. `generated_at` < `data_through_date`. The test must set up that
  exact condition (yesterday's brief + a `data_through_date` that reflects
  freshly-ingested data) to assert the banner. **Note:** if no new data was
  ingested either (the Mac slept through both the brief AND the daily pull),
  `generated_at` may not be older than `data_through_date`, so the banner may
  not light — that's correct, not a bug; the brief is stale-by-date but the data
  it summarizes is still the newest there is.
- **Empty `briefings/` (no brief at all)** → `/api/brief` returns its existing
  `{brief: null, cached: false, ...}` response (the genuine empty state).
- **Import hygiene:** `tests/test_smoke.py::test_imports` (with `chat` dropped)
  passes; `from local_fitness import cli` imports with no `chat` module present;
  `tests/test_smoke.py::test_tool_schemas_well_formed` passes with the count
  bumped to 25.
- **No-inference grep check (the machine-checkable form of the headline
  invariant).** A `sys.modules`-based test is NOT implementable here:
  `agent/tools.py` does `from claude_agent_sdk import create_sdk_mcp_server, tool`
  (tools.py:14, tool-registration plumbing, zero inference) and stays in the
  server/MCP import graph via `make_server()`, so importing
  `local_fitness.web.server` necessarily pulls `claude_agent_sdk` into
  `sys.modules`. Instead, assert by grep over source: (a)
  `grep -nE 'query\(|ClaudeSDKClient|ClaudeAgentOptions' src/local_fitness/web/server.py src/local_fitness/web/mcp_server.py`
  returns nothing in code (only incidental comment/string matches like "query
  the fitness DB" are acceptable; the check targets code, not prose); and (b)
  grep confirms NEITHER `server.py` nor `mcp_server.py` imports `agent.briefing`
  (the Claude-bound composer). `agent/tools.py` importing
  `create_sdk_mcp_server` / `tool` is allowed-and-expected — it is the MCP
  plumbing, not inference.

## Failure modes
- **Agent emits invalid brief JSON that salvage can't repair:**
  `briefs.save_brief` rejects it; no file written; the UI keeps showing the
  *previous* valid brief (today's, or the latest via the fallback). The LLM can
  never corrupt the UI. (Core integrity property.)
- **Agent emits a deviating-but-recoverable shape:** salvage repairs it before
  validation, so a "snapshot table" note can't 500 the brief — same safety net
  the server loop had.
- **Brief is structurally valid but semantically thin** (missing the steps
  takeaway, ignores an active plan): `briefs.save_brief` can't catch this — it's
  a prompt-following risk. `ab_brief.py` (live mode now writing through the same
  gate; `--mock` for CI) is the regression net; accepted risk.
- **Mac asleep at brief time:** launchd fires the missed job once at next wake
  (late `generated_at`), or — if asleep all day — no brief is written that day.
  Because `/api/brief` falls back to `load_latest()` (D5), the Today tab still
  shows the most-recent brief (with the stale banner lit), not an empty state;
  the user can ask their agent for a fresh one anytime.
- **Scheduled run can't reach Claude (OAuth token expired / job misconfigured):**
  the launchd `fitness brief` job fails and **no new brief is written** that day.
  The daily-driver UI **degrades gracefully** — `/api/brief`'s `load_latest()`
  fallback (D5) serves the last brief with the stale banner, so the Today tab is
  never empty. Recovery is manual: the user re-authenticates
  (`CLAUDE_CODE_OAUTH_TOKEN` refreshed in `.env`) and the next scheduled run
  writes a fresh brief. The Phase-1 gate proves the path works *once*; this
  `load_latest()` fallback is the *ongoing* guard against a silent gap.
  **Accepted gap:** there is no auto-alert if the scheduled run silently stops —
  the stale banner is the only signal. (A monitor/alert on brief age is a
  reasonable future add but is out of scope here.)
- **`briefs.save_brief` partial write / crash:** temp file + atomic rename means
  `/api/brief` and `fitness://brief/latest` never read a half-written file;
  a crash mid-write leaves the previous brief intact.
- **Concurrent same-day writes (e.g. the scheduled job and an ad-hoc agent
  `save_brief` on the same day):** last-writer-wins by design — the atomic
  temp+`os.replace` prevents corruption, the newest brief overwrites the older,
  and there is no merge or lock. Acceptable for a single-user app.
- **`/api/brief` vs resource freshness alignment:** both now resolve to the
  most-recent brief when today's is absent — `/api/brief` via `load_today()` →
  `load_latest()`, the resource via `load_latest()` directly. So on a day with no
  new brief the UI and the MCP resource agree (both serve yesterday's), removing
  the prior skew. The freshness banner distinguishes "today's" from "a prior
  day's" for the user.
- **Schema drift (`Brief` model changes):** the `brief` prompt's embedded schema
  and `briefs.save_brief`'s validator both derive from `agent/schemas.py` — one
  source of truth, so they can't diverge. `score_prompt.py` fails loudly if
  `briefing_prompt()` drifts from the schema.

## Migration (safe order: additive first, removals last)

**Phase 1 — additive, nothing user-visible breaks:**
1. **Create `agent/briefs.py` (the module split).** Move `DEFAULT_BRIEFINGS_DIR`,
   `load_today`, `_recent_briefs_summary`, and the salvage helpers
   (`_salvage_takeaways` / `_extract_json` / `_strip_inline_control_chars` /
   `_fix_numeric_gaps_outside_strings`) out of `briefing.py` into `briefs.py`;
   add `load_latest()` and the canonical `save_brief(payload)` (salvage →
   stamp-before-validate → validate → atomic write). `briefs.py` imports
   `schemas` + `db` only. Re-point `briefing.py` to import these from `briefs.py`
   (the composer keeps `_iter_partial_takeaways` + `generate_streaming`).
   **Refactor `briefing.generate_streaming` / `generate_and_save` to compose-
   then-`briefs.save_brief`** (the FATAL-1 fix — today the file is written
   twice, neither via a gate):
   - In `generate_streaming`, replace the `save=True` direct write
     (briefing.py:558–563) with a call to `briefs.save_brief(payload)`. `save`
     now means "persist via the gate" vs "return without persisting." Emit the
     `done` event from `save_brief`'s returned `Brief`
     (`result["brief"].model_dump()`), so the composer no longer validates a
     second time in parallel on the `save=True` path (single validate + single
     write). On `save=False`, validate locally as today (save_brief isn't called).
   - In `generate_and_save`, **delete the second direct write** (briefing.py:587–590)
     and the now-dead `out_dir` parameter; run the composer with `save=True` and
     return `Path(result["path"])` from `save_brief`'s `{saved, date, path, brief}`
     dict (keeping the `-> Path` contract that cli.py:149 echoes). `cli.py:148`
     already calls `generate_and_save(model=...)` with no `out_dir`, so dropping
     it is safe.
   - **This is a BEHAVIOR change to the still-live `/api/brief/generate/stream`
     endpoint, not purely additive.** That endpoint stays LIVE through
     Phases 1–2 (removed only in Phase 3 step 13) and calls
     `briefing_mod.generate_streaming` (server.py:899), forwarding the `done`
     event verbatim (server.py:899–904). After this rebase, an on-demand UI
     regen during the migration window writes via `save_brief`'s
     salvage → stamp → validate → atomic path instead of a bare
     `out_path.write_text`. The `done` event still carries a validated `Brief`
     (now the one `save_brief` returned) and the endpoint still annotates
     `data_through_date`, so the wire contract holds — but new persistence code
     runs on a live path here and MUST work during the window. After Phase 3 the
     endpoint is gone; this matters only during Phases 1–2.
   The result: the scheduled `fitness brief` job (and `ab_brief --run`) go THROUGH
   the single gate exactly once, not around it. Add `briefs.py` tests.
2. Add the `save_brief` MCP tool (thin wrapper over `briefs.save_brief`) to
   `ALL_TOOLS`; bump `test_smoke.py` count 24 → 25 (`test_mcp_server.py`
   set-equality auto-tracks). Add the tool-delegation test.
3. Add the `brief` MCP prompt (in-process input resolution, using
   `briefs._recent_briefs_summary`) to `web/mcp_server.py`; advertise it on stdio
   + streamable-HTTP. Re-point `mcp_server.py`'s `DEFAULT_BRIEFINGS_DIR` import
   and `_latest_brief_markdown` to `briefs.load_latest()`.
4. **Re-point only the `/api/brief` READ in `web/server.py` to `agent/briefs.py`**
   — `briefs.load_today()` with a `briefs.load_latest()` fallback (D5) — and add
   the fallback test. This phase does NOT remove the `from ..agent import briefing
   as briefing_mod` import (server.py:53): the `/api/brief/generate*` endpoints
   still call `briefing_mod.generate_streaming`/`generate_and_save` and are not
   removed until Phase 3. So at the END of Phase 1, `server.py` still imports
   `briefing` (for the generate endpoints) AND `briefs` (for the read) — that is
   expected; the composer import is dropped together with the generate endpoints
   in Phase 3 step 13.
5. Add `GET /api/plan/draft`.
6. Author the launchd plist + installer under `ops/` (CREATE — it doesn't
   exist; NET-NEW critical-path work, never run before) wired to `uv run fitness
   brief` with `WorkingDirectory` = project root. The plist must make
   `CLAUDE_CODE_OAUTH_TOKEN` available to the minimal launchd environment — via
   `EnvironmentVariables` or by loading the project `.env` (preferred). Document
   `CLAUDE_CODE_OAUTH_TOKEN` in `.env.example` (commented) + `docs/deployment.md`.
   **Verify** a scheduled headless run composes a valid brief and persists it via
   `briefs.save_brief` end-to-end (in-process MCP via `make_server()`, so no
   bearer token / allowed-host needed — only Claude creds).

**Gate:** do not proceed to Phase 2 until the Phase-1 scheduled job has written
at least one valid brief that `/api/brief` serves and the UI renders. Briefs
must never stop flowing.

**Phase 2 — UI rework (still no server removal):**
7. Today.tsx → viewer (drop auto-regen/stream/buttons/embedded chat; keep stale
   banner as informational; empty state only when `briefings/` has no brief).
8. TrainingPlan.tsx → draft-review + commit/delete (drop embedded chat; empty
   state points at the agent; consume `/api/plan/draft`).
9. Dashboards.tsx → charts only (drop `DashboardInsight` / session / model
   toggle).
10. Delete `ChatPanel.tsx`; prune dead `api.ts` methods (`chat`, `chatEnd`,
    `briefGenerate`, `briefGenerateStream`) and dead types.
11. `pnpm build` + `pnpm tsc --noEmit` green; screenshot Today/TrainingPlan/
    Dashboards.

**Phase 3 — remove server LLM (gated on Phases 1–2; this phase achieves the
END-STATE "no Claude inference / no `agent/briefing` import in `server.py` +
`mcp_server.py`" invariant):**
12. Delete `agent/chat.py`. In the SAME step, remove `cli.py`'s module-top
    `from .agent import chat as chat_mod` (cli.py:31) AND the `chat` and `ask`
    subcommands that used it (the server-chat REPL), plus their mention in the
    module docstring and the `setup` "Next:" hint. (`fitness brief`/`serve`/
    `pull`/`setup`/`baselines`/`mcp-stdio` stay; without this, the dangling
    `chat_mod` import would break EVERY subcommand at import time.) Verify
    `uv run fitness --help` and `fitness brief` both run.
13. Remove `/api/chat*` + `/api/brief/generate*` and their models/whitelist,
    the per-session client plumbing, and the (now-empty) entries in
    `RATE_LIMITED_PREFIXES`. Drop the remaining `claude_agent_sdk` inference
    imports from `web/server.py`. In the SAME step, drop
    `from ..agent import briefing as briefing_mod` (server.py:53) — it is removed
    here, alongside the `/api/brief/generate*` endpoints that were its last
    consumer (the GET `/api/brief` read was already repointed to `briefs` in
    Phase 1 step 4, but the generate endpoints kept the composer import
    load-bearing through Phases 1–2). With this, `server.py` reaches brief I/O
    only through `agent/briefs.py`, satisfying the END-STATE no-composer
    invariant.
14. Delete `tests/test_security.py::test_chat_request_model_whitelist`; update
    `tests/test_smoke.py` import line (drop `chat`).
15. Rebuild the container (`docker compose up -d --build local-fitness`); verify
    `/api/brief` serves the agent-written brief and the charts/plan still work.

## Acceptance criteria
- `uv run pytest -x` green, including the new `briefs.save_brief` validation /
  salvage / stamp-before-validate / atomic-write / generated_at round-trip tests,
  the `briefs.load_latest()` + `/api/brief` fallback tests, the
  `save_brief`-tool-delegation test, the `/api/plan/draft` test, the import-graph
  assertion, the bumped tool count (25), the updated smoke import, and all
  retained data-endpoint tests.
- **No-inference grep check (the precise invariant):**
  `grep -nE 'query\(|ClaudeSDKClient|ClaudeAgentOptions' src/local_fitness/web/server.py src/local_fitness/web/mcp_server.py`
  returns nothing in code (no inference symbols / agent loop in either source),
  and grep confirms neither imports `agent.briefing` — both reach brief I/O only
  through `agent/briefs.py`. (`agent/tools.py`'s
  `from claude_agent_sdk import create_sdk_mcp_server, tool` is allowed and
  expected — MCP tool plumbing, zero inference — so a `sys.modules` test would be
  ill-posed; grep over source is the implementable check.) `chat.py` is gone and
  the `chat`/`ask` CLI commands with it; the composer (`agent/briefing.py`) is
  reachable only from `fitness brief` / `scripts/ab_brief.py`.
- **Single-writer check:** `briefs.save_brief` is the only function that writes
  `briefings/*.json`; the scheduled job (via `generate_streaming`), the MCP tool,
  and `ab_brief --run` (via `_generate` → `generate_streaming` default
  `save=True`) all route through it. `generate_streaming`'s old direct write and
  `generate_and_save`'s second write (plus the dead `out_dir` param) are removed.
  Grep for `briefings/` write sites turns up exactly one, in `agent/briefs.py`.
- `pnpm build` + `pnpm tsc --noEmit` green; Today/TrainingPlan/Dashboards
  render as viewers (screenshots attached) with no embedded chat; Today shows the
  most-recent brief (not an empty state) when today's file is absent but a prior
  brief exists.
- **NET-NEW launchd path (highest-risk item):** the authored `ops/` plist +
  installer actually schedules `uv run fitness brief` from the project dir with
  `CLAUDE_CODE_OAUTH_TOKEN` injected (via `EnvironmentVariables` or `.env`); a
  scheduled headless run produces a brief end-to-end via `briefs.save_brief`
  (in-process MCP through `make_server()` — no bearer token / allowed-host) and
  `/api/brief` serves it; the stale banner lights when newer data lands.
  `CLAUDE_CODE_OAUTH_TOKEN` is documented in `.env.example` + `docs/deployment.md`.
  This is the Phase-1 gate — treat it as the dominant acceptance risk, not an
  equal-weight checkbox.
- `scripts/ab_brief.py --mock <fixtures>` (the gate-safe CI path) and
  `scripts/score_prompt.py` still pass (the composer + `briefing_prompt()` are
  intact; live `--run` mode now also persists through `briefs.save_brief`).
- `docker compose up -d --build local-fitness` healthy.

## Implementation notes (guardrails for the build)
- **Do not "tidy away" `briefing.py`'s `import tools as agent_tools` or
  `from .schemas import Brief`.** After the split, the composer still needs
  `make_server()` / `read_only_tool_names()` (for the in-process MCP) and `Brief`
  (for the validate step). `briefs.py` imports neither `briefing` nor `tools`, so
  the only arrows are `briefing → {briefs, tools, schemas}` and `tools → briefs`
  — no cycle. Removing the `tools` import to "clean up" breaks the composer.
- **`save_brief` enforces STRUCTURE, not brief richness.** `Brief.takeaways` is
  `min_length=1, max_length=5`, so `save_brief` will persist a structurally-valid
  1- or 2-takeaway brief. The stricter "3-5 substantive takeaways, steps +
  workout + plan-awareness" bar is enforced only by `ab_brief` (the A/B net) and
  the `brief` prompt's mandates — not by the write gate. Brief *quality* rides on
  prompt-following; the gate guarantees integrity, not richness.

## Out of scope
- A remote/cloud scheduled agent (the MCP is LAN-only behind Traefik).
- Moving any deterministic compute into the agent.
- Re-adding an in-app web chat surface. (If conversational coaching in the phone
  web UI is later wanted, that's a new design — it's deliberately removed here.)
