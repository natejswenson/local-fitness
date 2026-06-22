---
ticket: "N/A (interactive design)"
title: "Clean fitness Q&A — capability tool + always-on presentation contract"
date: "2026-06-22"
source: "design"
---

# Clean fitness Q&A — capability + presentation

## Problem

When the user asks the fitness agent an ad-hoc question via Claude Code (e.g.
"show me my training plan completed through today"), the interaction surfaces a
lot of junk the user does not care about: multiple exploratory `Bash` calls,
`sqlite3` schema introspection (`PRAGMA table_info`), trial-and-error SQL with
errors, and raw column dumps. The user wants the clean table + coach text, not
the lookup mechanics. They want this to be systemic across the fitness
skill/MCP, not a one-off.

## Root cause (two compounding gaps)

1. **Missing capability.** There is no MCP tool that returns the *graded
   training plan day-by-day*. `get_training_plan_status`
   (`agent/tools.py:1167`) deliberately returns a slim summary — `build_plan_status`
   (`plans.py:569`) returns 8 keys: `active`, `goal_type`, `race_date`,
   `target_time_seconds`, `days_to_race`, `adherence_pct`, and the two
   `_slim_workout` projections `today` and `last_graded` — i.e. at most one
   prescribed day plus one most-recent graded day, NOT the full list. That
   keeps the brief cheap. The full graded list already exists in
   `plans.build_plan_detail()` (`plans.py:522`), computed by reusable,
   unit-tested grading (`classify_workout` @ `plans.py:152` reads each day's
   activities; `grade_workout` @ `plans.py:197-207` delegates to it after a
   frontier check), but is wired only to the `/api/plan` web route
   (`_assemble_plan_detail`, `server.py:404`) — not exposed as a tool. With no
   clean call to make, the agent improvised with raw `sqlite3` in Bash.

2. **Missing presentation discipline (always-on).** The block in
   `system_prompt()` headed "Formatting your chat replies (NOT the JSON brief)"
   (`prompts.py:80-94`) governs the agent's CHAT / conversational prose — and
   that prose is exactly the surface we want clean for ad-hoc Q&A via `/coach`.
   It is **NOT** what formats the brief: the block self-scopes ("This governs
   your conversational prose only — the structured JSON brief is separate and
   its schema is unchanged", `prompts.py:93-94`), and the brief's own formatting
   lives in a separate "JSON formatting rules" block (`prompts.py:496-501`). But
   that chat-formatting text only enters context when the `/coach` MCP prompt is
   loaded. For a normal in-repo Claude Code question, it is absent — nothing
   tells the agent to prefer structured tools, never echo raw exploration, or
   format as a table.

Both gaps fired at once. Neither lever alone fixes the experience: Lever A
without B still yields junky prose; Lever B without A still has no tool to call.

## Lever A — new MCP tool `get_training_plan_progress`

This is a deliberate **projection wrapper**, not a thin mirror. It assembles
inputs the way the web call site does, guards the no-plan case that
`build_plan_detail` does not, and returns a slimmed payload — not
`build_plan_detail`'s verbatim output.

- **No-active-plan guard (must come first).** `build_plan_detail`
  (`plans.py:522`) has NO `None` guard — it dereferences `plan["workouts"]`
  immediately and would raise on `None`. So the tool must call
  `plans.get_active_plan()` and, when it is `None`, return `{active: false}`
  BEFORE building anything (parity with `get_training_plan_status`,
  `tools.py:1177`).

- **Input assembly — mirror `get_training_plan_status`'s frontier-INCLUSIVE
  `end` (`tools.py:1179-1184`) for BEHAVIORAL PARITY, NOT the web call site's
  frontier-exclusive one (`server.py:398`).** `build_plan_detail(plan, frontier,
  activities_by_date, best_effort)` needs four inputs:
  - `frontier = db.last_known_daily_date()`
  - `activities_by_date = plans.load_activities_by_date(start, end)`. Compute the
    date list with the **same empty-plan guard the existing tool uses** —
    `dates = [w["date"] for w in active["workouts"]] or [today]`
    (`tools.py:1181`) — so a (non-production) plan with zero workouts cannot make
    `min([])`/`max([])` raise. Then `start = min(dates)` and
    `end = max([today, *dates] + ([frontier] if frontier else []))`.
    **The `end` is frontier-INCLUSIVE — for parity, not bug-prevention.** Grading
    keys each workout by its OWN date (`activities_by_date.get(w["date"], [])`,
    `plans.py:531`, `plans.py:580`), and the window `[min(dates), max([today,
    *dates])]` always contains every workout's own date regardless of the
    frontier. The `+[frontier]` term only widens `end` when `frontier > today`
    (future-dated wellness rows), which does not happen in practice. So there is
    **no known input** where the exclusive window grades differently from the
    inclusive one. We mirror `get_training_plan_status`'s frontier-inclusive
    `end` (`tools.py:1183`) so the two plan tools compute **identical grading
    windows** — parity with the existing tool removes a latent divergence rather
    than fixing an observable bug. We deliberately do NOT copy the web tab's
    `_assemble_plan_detail` frontier-exclusive form (`server.py:398`): mirror the
    tool, not the tab, so the two MCP plan tools stay congruent.
  - `best_effort = plans.best_recent_effort(cutoff)` with
    `cutoff = today − 120 days` (the `_RIEGEL_LOOKBACK_DAYS` constant at
    `server.py:389`). This drives `predicted_finish_seconds` via Riegel; pass
    it so the tool can surface a projected finish. (We do NOT replicate
    `ctl_series`, which the tab adds afterward — that's tab-only chrome.)

- **Return shape: a deliberate projection (NOT verbatim).** `build_plan_detail`
  returns every non-`workouts` key spread from the plan row
  (`**{k: v for k, v in plan.items() if k != "workouts"}`) — including
  `plan_id`, `status`, `ability_snapshot`, `goal_distance_m`, `title`,
  `committed_at`, `created_at` — plus `weekly_mileage`,
  `predicted_finish_seconds`, and `adherence_pct`. The tool returns a curated
  subset:
  - **Kept:** `goal_type`, `race_date`, `target_time_seconds`,
    `adherence_pct`, `predicted_finish_seconds`, and `workouts[]`.
    - **Note for the test author:** `predicted_finish_seconds` may be `None`.
      `build_plan_detail` sets it to `None` when `best_effort` is falsy
      (`plans.py:539-544`) — e.g. no qualifying recent effort in the 120-day
      window. The test must tolerate `None` here, not assert an int.
  - **Dropped (from `build_plan_detail`'s output):** `plan_id`, `status`,
    `ability_snapshot`, `goal_distance_m`, `title`, `committed_at`,
    `created_at`, `weekly_mileage` (internal/identifiers the agent does not
    need to answer a plan-progress question). `ctl_series` is NOT in this list
    because `build_plan_detail` never returns it — it is injected afterward by
    `_assemble_plan_detail` (`server.py:404`) as tab-only chrome, so there is
    nothing to drop.
  - **Computed (not from `build_plan_detail`):** `days_to_race`. This key is
    produced only by `build_plan_status` (`plans.py:589-591`), NOT by
    `build_plan_detail`. The wrapper computes it the same way as
    `build_plan_status` but with one deliberate hardening: read the date via
    `plan.get("race_date")` (NOT the subscript `plan["race_date"]` that
    `build_plan_status` uses at `plans.py:589`), then parse via `_parse_iso`,
    then `(race − today).days` **only `if race and today_d else None`**
    (`plans.py:589-591`).
    - **Note for the test author / implementer:** `build_plan_status` indexes
      `plan["race_date"]` directly (`plans.py:589`), which raises `KeyError` on
      a genuinely MISSING key; `_parse_iso` only guards `None` / unparseable
      VALUES, not an absent key. In production `race_date` is always present (it
      is a `SELECT *` column and may be `NULL`), so the slim path never hits
      that `KeyError`. The wrapper must use `.get("race_date")` so the stated
      test — "absent `race_date` yields `days_to_race = None`, never an
      exception" — actually passes. With a bare subscript the absent-key test
      would `KeyError` instead of returning `None`.

  - **Per-workout projection:** each `workouts[]` entry carries `date`,
    `week_index`, `type`, `target_distance_m`, `target_pace_sec_per_km`,
    `target_duration_sec`, `description`, and the computed `verdict`
    (`done` | `partial` | `missed` | `compliant` | `pending`). The graded
    array from `build_plan_detail` already includes `verdict`,
    `actual_distance_m`, and `actual_pace_sec_per_km`; we keep `verdict` and
    may keep the two `actual_*` fields (they're cheap and useful), dropping
    nothing load-bearing.
  - **Description length — conscious divergence from the slim path.** This tool
    keeps the **full, RAW** workout `description`, whereas `get_training_plan_status`'s
    `_slim_workout` caps it to 120 chars (`plans.py:558`) as anti-injection
    hygiene. Decision: keep the full description here — the user invokes this
    tool about *their own* committed plan (descriptions they/we authored), so
    the injection surface is low and the truncation would lose detail they
    asked to see. This is a deliberate, acknowledged divergence from the slim
    path, not an oversight; if untrusted plan text ever becomes a vector, revisit.

- **`adherence_pct` availability:** confirmed present — `build_plan_detail`
  sets `"adherence_pct": _adherence_pct(graded)` (`plans.py:550`). No new field
  needed.

- **Why a separate tool, not a `detail: bool` flag on the existing one:** the
  real isolation is code-level. `_READ_ONLY_TOOL_NAMES` (`tools.py:1251`) is a
  frozen ALLOW-LIST and the brief loop runs with exactly that set
  (`briefing.py:162`, `read_only_tool_names()`). `get_training_plan_status` is
  already on that allow-list, so a `detail: bool` flag on it would expand
  *that already-permitted tool's* behavior inside the brief loop — the model
  could request the full graded list during a brief, defeating the slim path.
  A separate tool that we deliberately leave OUT of `_READ_ONLY_TOOL_NAMES`
  can never enter the brief's tool set at all, whatever the model does. The
  separate tool is the cleaner choice; keeping the brief's cheap summary path
  untouched is the consequence.

- **Grading reuse:** zero new grading logic. `build_plan_detail` already
  returns the full graded `workouts` array.

- **Registration:** add to `ALL_TOOLS` (`tools.py:1208`) so MCP clients (stdio
  + deployed HTTP) see it. **Do NOT** add to `_READ_ONLY_TOOL_NAMES`
  (`tools.py:1251`) — that frozen tuple gates the brief loop, which does not
  need this tool.

- **Date filtering:** the tool returns the full graded list (past + today +
  future). "Through today" is a presentation concern handled by the model, and
  returning everything also answers "what's coming up."

## Lever B — presentation contract (Lever A is the load-bearing fix; this is an advisory nudge)

Be honest about what each piece buys. **Lever A is the load-bearing fix for the
in-repo primary path.** Once a clean one-call tool *exists*, the agent has a
direct alternative and no reason to spelunk the DB with `sqlite3` in Bash. The
text rules below *reinforce* that but do not, by themselves, guarantee it.

The user chose "CLAUDE.md + MCP layer." We honor that with the two homes that
don't pollute a function-selection contract. We explicitly **do NOT** edit the
`run_sql` tool description: a tool's description is its function-selection
contract for every MCP client, it is already large (it embeds the schema), and
injecting "prefer other tools / don't shell to `sqlite3`" meta-guidance there
degrades selection for every caller. That edit is dropped.

1. **`CLAUDE.md`** — new "Answering fitness questions" section (there is none
   today). This is read as **context** for in-repo Claude Code (the user's
   primary surface). It is an **ADVISORY nudge**, not an enforced tool-selection
   gate: the harness does not block a `sqlite3`/Bash read just because CLAUDE.md
   says to prefer `mcp__fitness__*`. The reported symptom (raw `sqlite3` in Bash)
   in fact occurred *with* CLAUDE.md already loaded — which is the whole reason
   Lever A, not more prose, is the real fix.
2. **Strengthen the EXISTING chat block in `prompts.system_prompt()`
   (`prompts.py:80-94`)** — the block headed "Formatting your chat replies (NOT
   the JSON brief)", which governs the agent's CHAT / MCP conversational replies
   reaching Claude Desktop and deployed `fitness.home.local`. We edit *this*
   block so it also tells the agent to prefer structured `mcp__fitness__*`
   tools and not narrate the lookup — i.e. it continues to govern chat/MCP
   prose, the exact surface we care about. Edit the existing block; do NOT add
   a third near-duplicate. Same status: advisory prompt context, not
   enforcement; it mainly closes the gap for the MCP surfaces that never see
   CLAUDE.md. Note: this block is self-scoped to chat (it explicitly says the
   JSON brief is separate, `prompts.py:93-94`), which is additional reason to
   expect the brief JSON path is unaffected — but that is to be confirmed by
   the A/B, not assumed (see Testing strategy: editing this block still changes
   the shared `system_prompt()` bytes the brief loads at `briefing.py:163`).

Contract text (intent):

> When answering a fitness question: prefer the structured `mcp__fitness__*`
> tools; use `run_sql` only when no structured tool fits; never shell out to
> `sqlite3`/Bash for DB reads. Don't narrate the lookup — lead with a one-line
> answer, then a clean table plus coach text. One call when a tool exists.

- **Wording reconciliation:** the existing block (`prompts.py:84`) already says
  tables "at most ~4 columns." Match that phrasing — do NOT introduce a
  conflicting "≤4 columns" rule. The CLAUDE.md text should likewise say
  "~4 columns" so the two homes agree.

- **Clear-eyed note on what actually moves the primary path.** The two things
  that change the in-repo experience are: (1) **Lever A** — the structured tool
  now EXISTS to call, the load-bearing fix; and (2) a **NEW explicit advisory
  rule** ("never shell to `sqlite3`; prefer `mcp__fitness__*`") that was simply
  absent from CLAUDE.md before. The rule reinforces but does not enforce.

- **Future hardening (deferred — NOT in this v1).** If the soft advisory nudge
  proves insufficient in practice (the agent keeps shelling to `sqlite3`
  despite the tool existing), the reactive enforcement option is a `PreToolUse`
  hook that blocks `Bash`/`sqlite3` reads against the fitness DB and points the
  agent at the `mcp__fitness__*` tools. That is a real lever but it is explicitly
  **out of scope here** (YAGNI): add it only if measured behavior shows the nudge
  failing. Do not build it now.

## API surface

- `mcp__fitness__get_training_plan_progress() -> dict`
  - Returns `{active: false}` when `get_active_plan()` is `None` (guarded
    BEFORE `build_plan_detail`, which has no `None` guard).
  - Else `{active: true, goal_type, race_date, target_time_seconds,
    days_to_race, adherence_pct, predicted_finish_seconds, workouts: [ {date,
    week_index, type, target_distance_m, target_pace_sec_per_km,
    target_duration_sec, description, verdict, actual_distance_m,
    actual_pace_sec_per_km}, ... ]}`.
  - `predicted_finish_seconds` may be `None` (no qualifying recent effort).
  - `days_to_race` is **computed by the wrapper** (date math from `race_date`
    vs today), because `build_plan_detail` does not emit it. The wrapper reads
    the date via `plan.get("race_date")` (NOT a bare subscript) so an absent
    key yields `None`, never `KeyError`; it is also `None` when
    `race_date`/`today` is `NULL` or unparseable (guard parity with the
    value-level guard in `build_plan_status`, `plans.py:589-591`).

## Invariants

Checkable by inspection:
- The new tool appears in `ALL_TOOLS` and NOT in `_READ_ONLY_TOOL_NAMES`.
- The tool's `activities_by_date` window uses a **frontier-inclusive** `end`
  (`max([today, *dates] + ([frontier] if frontier else []))`), matching
  `get_training_plan_status` (`tools.py:1183`), NOT the web tab's
  frontier-exclusive form (`server.py:398`). This is a **parity** invariant: the
  two MCP plan tools must compute identical grading windows. There is no known
  input where the exclusive form grades differently — parity removes a latent
  divergence, it does not fix an observed failure.
- The date list is guarded with `or [today]` (`tools.py:1181`) so a zero-workout
  plan cannot raise on `min([])`/`max([])`.
- `days_to_race` is read via `plan.get("race_date")` and guarded the same way
  as `build_plan_status`'s value-level guard (`plans.py:589-591`): `None` when
  `race_date` is absent, `NULL`, or unparseable (or when `today` is
  unparseable), never an exception.
- No new SQL is written; the tool assembles inputs (`get_active_plan`,
  `last_known_daily_date`, `load_activities_by_date`, `best_recent_effort`)
  and calls `build_plan_detail`, then projects/derives the returned shape
  (including computing `days_to_race`). It does not return `build_plan_detail`'s
  payload verbatim, and it adds no grading logic.
- `get_training_plan_status` return shape is unchanged (brief path intact).

Requires tests:
- Tool returns `{active: false}` with no active plan.
- With an active plan, `workouts` length equals the prescribed count and each
  entry has a `verdict` in the allowed set; `days_to_race` matches
  `(race_date − today).days`; `predicted_finish_seconds` is an int OR `None`.
- `days_to_race` is `None` (not an exception) when `race_date` is absent or
  unparseable. **Test-author note:** this asserts the wrapper's `.get(...)`
  hardening — it exercises the absent-key path that the slim path's bare
  `plan["race_date"]` subscript would `KeyError` on. Construct the absent-key
  case explicitly (a plan dict with no `race_date` key) AND the `None`/
  unparseable value case.
- **Parity check (not a "fails-without-the-fix" regression):** for the same
  active plan, `get_training_plan_progress`'s grading window and per-workout
  `verdict`s match `get_training_plan_status`'s. This asserts the two tools
  agree; it is NOT a test that fails without the frontier-inclusive `end`,
  because no such failing input can be constructed (every workout is keyed by
  its own date, which the window always covers).

## Testing strategy

- `uv run pytest -x` — new unit test for `get_training_plan_progress` shape +
  verdict set + computed `days_to_race` (incl. the absent-key AND
  `None`/unparseable `race_date` guard cases) + `predicted_finish_seconds`
  nullability + a **parity assertion** that the progress tool's grading
  window/verdicts match `get_training_plan_status` for the same plan; existing
  `get_training_plan_status` tests stay green.
- **Prompt-change gate (this can BLOCK the edit).** The brief loads the FULL
  `system_prompt()` verbatim (`briefing.py:163`), so editing the chat block
  changes the brief's prompt bytes — which is WHY the A/B gate is required even
  though the block is self-scoped to chat. We do NOT claim the brief's
  serialized output is byte-unchanged — that's untestable here:
  `score_prompt.py` is a static grounded checker (no brief generation), and
  `ab_brief.py --run` makes nondeterministic LLM calls. The gate is two parts,
  both using the ACTUAL tooling (no numeric score, no baseline):
  1. **`score_prompt.py` must stay GREEN.** It is an all-or-nothing BINARY
     gate: it prints `passed/total` but exits non-zero unless `passed == total`.
     The pass condition is simply that *every* required check passes (exit 0).
     The printed percentage is NOT a baseline-able continuous score and must
     not be treated as one.
  2. **`ab_brief.py --run` must report `consistent: true` across 3 runs.**
     `compare()` (`scripts/ab_brief.py:62-86`) returns
     `{consistent: bool, divergences: [...]}` — it is a divergence checker, NOT
     a numeric scorer, and is nondeterministic.
     - **Exactly what `compare()` flags (structural only).** The checker emits a
       divergence for, and ONLY for: (a) the mandated steps takeaway missing in
       any brief (`has_steps` false); (b) a takeaway count outside `[3,5]`;
       (c) a takeaway count that varies by more than 1 across briefs; and (d) the
       plan fold/leak check — plan content NOT folded in when a plan is active,
       or plan content present when none is. That is the complete set the A/B
       gate catches on the brief side.
     - **Honest gap — tone is NOT checked.** `extract_features` computes a
       `tones` fingerprint (`ab_brief.py:55`) and `_report` prints it
       (`ab_brief.py:119`), but `compare()` never feeds `tones` into a
       divergence check. So **tone shifts in the brief are NOT caught by the
       existing A/B checker** — the A/B's brief-side coverage is *structural
       only* (count, steps-takeaway presence, count variance, plan-fold). If
       tone-coverage is wanted for this edit, the implementer must add a
       tone-divergence check to `compare()`; otherwise tone drift will pass the
       gate silently.
     - **How to run it (avoid conflating with `--runs`).** Use the harness's own
       multi-run fan-out in a single invocation:
       `uv run python scripts/ab_brief.py --run --runs 3`
       — `--runs` already does `models × runs` generations per call (default 2),
       so this is NOT 3 separate process invocations. (Three separate `--run`
       calls would also satisfy the intent, but the single `--runs 3` command is
       the canonical one; do not read "run it 3 times" as "invoke the script 3
       times.")
     - **BLOCK condition.** The edit is **BLOCKED if any run reports a divergence
       attributable to the edit** — i.e. a reproducible structural shift in the
       brief among exactly the four things `compare()` checks (steps-takeaway
       presence, takeaway count in `[3,5]`, count variance, plan-fold). Tone is
       not among them.
     The hypothesis that the change is "chat-block scoped and the brief's fixed
     JSON schema overrides it" is exactly what these runs must CONFIRM — not an
     assumption that lets us skip them.
  - **What "BLOCK" means (kept honest).** A block is EITHER (a) a binary
    `score_prompt.py` failure (any check red, exit non-zero), OR (b) a
    reproducible A/B divergence across the 3 runs attributable to the edit
    (among the four structural checks above). It is NOT a numeric regression
    against a captured baseline — no repo tool produces a baseline-able numeric
    brief score, so we do not invent one.
- Rebuild the container so the deployed app serves the new tool.

## Obligations (repo rules)

- Version bump in `pyproject.toml` + CHANGELOG entry (functionality + prompt
  change).
- `devlog/` entry.
- No new endpoint / no auth surface change → `test_security.py` untouched.

## Quality-gate provenance

Reviewed via `/quality-gate` (artifact type: design). Six fresh-eyes
adversarial rounds + a tightened-rubric look-harder pass, all on
`general-purpose` agents (the `crucible-*` agent types and receipt/cairn
infrastructure were not installed, so the Opus recall guarantee was not
enforced; findings were still code-grounded). Score trajectory 5 → 2 → 1 → 2 →
1 → 0; terminal verdict **PASS (clean-pass)**: 0 Fatal / 0 Significant on a
fresh round, confirmed by look-harder. Every Fatal stayed at zero throughout;
all resolved findings were API-contract and verification-prose precision, not
mechanism defects.
