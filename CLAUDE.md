# local-fitness — instructions for Claude

> Maintainer-internal: this file is agent/ops guidance for the repo owner, not contributor onboarding — see README.md to get started.

This repo is a personal-fitness agent that has gone public on GitHub.
Two facts shape every decision:

1. **The app must work for me on my laptop.** I run `uv run fitness ...`
   and `docker compose up -d --build local-fitness` daily. Don't break
   either path.
2. **Anyone else cloning the public repo must be able to run it without
   knowing anything about my home network or my Garmin account.** No
   hardcoded paths, no hardcoded secrets, no LAN-specific assumptions
   in tracked code.

These two pull in opposite directions — the env-driven pattern below
is how we satisfy both.

## The env-driven pattern (apply to every new feature)

Anything that varies between *my deployment* and *a stranger's clone*
goes through `.env`:

- **Secrets** — credentials, bearer tokens, API keys → env vars only.
  Read in code via `os.environ.get("...")`. Document in `.env.example`
  with a commented-out placeholder. Never default to a real value.
- **Host-specific paths** — anything that would otherwise hardcode
  `/Users/...` or `~/localrepo/...` → an env var like
  `LOCAL_FITNESS_FOO_DIR` with a *project-relative* default
  (`Path(__file__).resolve().parents[N] / "foo"`). The default must
  work in a fresh clone without any env setup.
- **Deployment knobs** — bind host, ports, throttle windows, anything
  the container needs to override → env var with the host-CLI default
  baked into the code, the container value set in
  `docker-compose.yml`'s `environment:` block.
- **Personal data** — the SQLite DB, generated briefings, logs, user
  notes → already in `.gitignore` (`data/`, `briefings/`, `logs/`).
  Never relax those entries. Never commit fixtures derived from real
  data; if you need a fixture, fabricate it.

When you add a new env var:

1. Read it in code with a sensible default (project-relative path /
   conservative throttle / etc.). The default is what a stranger's
   clone uses on first run.
2. Add it to `.env.example` with a commented-out example value and
   a one-line explanation.
3. If it's required for the **container** deployment, also add it to
   `docs/deployment.md`'s compose snippet so future-you knows to
   wire it in the traefik repo's `.env`.
4. If it's a secret that's required when binding non-loopback, mirror
   the pattern in `serve()` — refuse to start without it (see
   `LOCAL_FITNESS_API_TOKEN` for the template).

## Security defaults that are non-negotiable

After the 2026-05-04 audit, these are guardrails. Don't regress them.

- **Every new server endpoint is auth-gated by default.** `_is_public_path()`
  in `web/server.py` denies by default — the bearer middleware covers
  everything except what's explicitly whitelisted there. If you add a new
  endpoint that genuinely should be public (like `/health`), whitelist it
  explicitly, not by relaxing the fallthrough.
- **Every new endpoint that calls Claude is rate-limited.** The
  middleware matches by prefix in `RATE_LIMITED_PREFIXES`. Add new
  Claude-cost paths to that tuple — don't just hope they stay cheap.
- **No SQL with user input via f-strings.** Whitelist column / table
  names against a frozen set, parameterize values via `?`. The
  pattern is locked in `agent/tools.py` and the existing route
  handlers — copy from there.
- **No path joining with user-supplied path segments without a
  containment check.** If you ever serve a file based on a URL
  parameter, `(BASE / param).resolve().relative_to(BASE.resolve())`
  is the pattern, with a fallback when it raises `ValueError`.
- **`tests/test_security.py` is the regression net.** Add a case
  there for any new auth-relevant code path. The audit found one
  HIGH; we don't want to find a second one in production.

## Workflow expectations

- **Plan first.** Non-trivial changes get a written plan (affected
  files, trade-offs, verification approach) before any code lands.
  Ask clarifying questions one at a time when the spec is ambiguous.
- **Everything gets tested — no exceptions.** Every change ships *with* tests
  in the same commit/PR: a new function or module gets its own test cases, a
  bugfix gets a regression test that fails before the fix and passes after, a
  new branch/edge case gets a case that exercises it. Tests must assert real
  behavior to our standard — **never coverage theater**: no `assert x is not
  None` stand-ins, no asserting a mock/stub replays its own canned value, no
  trivially-true checks. The bar is "would this test FAIL if the code under
  test were broken?" — if not, it isn't a test. Pin the actual transformed
  values, the real status/branch taken, the exact error. Cover the edge cases
  (empty, single, flat, negative, missing-data, boundary). The CI coverage gate
  is **85%** (`--cov-fail-under=85`); a PR that drops coverage or adds untested
  code is incomplete. Stop short of testing pure I/O glue (network/LLM/uvicorn)
  only where a test would merely assert a mock — and say so explicitly.
- **Test before claiming done.** `uv run pytest -x` for Python, `docker
  compose up -d --build local-fitness` for the container path.
- **The live deployment tracks `dev`, not `main`.** Nate's daily-use app at
  `https://fitness.home.local` (and the host `uv run fitness ...`) runs from the
  `dev` working branch — that's where all tested work lands. `main` is the
  public-consumption snapshot only (see *Branching & release strategy*). So the
  default loop is: land work on `dev`, then rebuild the container **from a `dev`
  checkout** so the live app is current. Do **not** promote to `main` or cut a
  release as part of normal work — that happens only when Nate explicitly asks.
- **Rebuild the container after every change.** Stale containers serve stale
  code. Check out `dev` first, then `docker compose up -d --build
  local-fitness` from `/Users/natejswenson/localrepo/traefik` (compose builds
  from the `../local-fitness` working tree, so the checked-out branch is what
  ships to the container).
- **What CI does and does NOT cover.** The `validate` job runs `pytest`
  (85% coverage gate), a separate perf-benchmark regression gate (see
  below), and `ruff`, the prompt scorer. **Ruff's ruleset is explicit**
  (0.38.0 — `[tool.ruff.lint] select` in `pyproject.toml`, currently
  `E4,E7,E9,F,I,UP,B,A`; before that it silently ran defaults while the
  code carried `noqa`s for rules that weren't on). The exclusions are
  documented in the config itself: never enable `ARG` (MCP handlers take
  `_args` by contract), and enable `BLE` + `RUF100` only TOGETHER after
  triaging the ~30 broad-except sites (RUF100 alone flags the existing
  BLE001 noqas as unused). **`tests/test_docs_drift.py` makes docs/mcp
  tested surface**: every tool in `ALL_TOOLS`/`LOCAL_ONLY_TOOLS` must have
  a page, no orphan pages, and the README tool counts must match
  `len()` of the registries — adding a tool without its page fails the
  build. A separate `docker-build` job
  compiles the full image (no push) so a base-image bump or `Dockerfile`
  change can't silently break `docker compose up --build` while CI stays
  green — but a green `docker-build` only proves the image *compiles*, not
  that the running container behaves correctly, so still rebuild and
  smoke-test locally after touching the `Dockerfile` or base images.
- **Perf-benchmark regression gate (`tests/test_perf_benchmarks.py`).**
  `pytest-benchmark`-based, separate axis from the coverage gate: latency
  AND `db.connect()`-open-count for the brief/plan hot paths
  (`assemble_brief_context`, `get_training_plan_progress`,
  `get_training_plan_status`, `_build_plan_section`, `daily_snapshot`),
  run against a synthetic multi-year fixture (`scripts/perf_fixture.py` —
  fabricated, never derived from real data). Skipped on every ordinary
  `pytest` run (`--benchmark-skip` in `pyproject.toml`'s `addopts`); the
  `validate` job's "Perf-benchmark regression gate" step explicitly opts
  back in with `--benchmark-only --no-cov`, comparing against the
  committed `.benchmarks/Linux-CPython-3.12-64bit/0001_*.json` baseline
  (`--benchmark-compare-fail=min:15%`). **Machine-id matched, non-negotiable**:
  pytest-benchmark nests saves under `platform.system()-implementation-
  pyver-arch`, so the baseline MUST be (re)captured via the
  `capture-perf-baseline.yml` `workflow_dispatch` job on `ubuntu-latest` —
  never locally on Nate's Mac (`Darwin-CPython-3.12-64bit` would never
  match, turning every subsequent `validate` run into a hard failure).
  Rebaseline only when intentionally resetting the comparison floor after
  a further perf improvement, not routinely — with ONE more legitimate
  trigger (first hit 2026-07-26): **runner-fleet drift**. GitHub's ubuntu
  runners are a mix of CPU models, and a baseline captured on one
  generation slowly stops describing the fleet — unchanged dev code was
  reading +13.7% against the July-9 floor, so ANY PR was one slow runner
  draw from a false failure. The tell that it's drift and not a real
  regression: (1) a local before/after A/B of the touched path is at
  parity, and (2) the last passing validate run on UNCHANGED code already
  shows a double-digit margin. Confirm both, then recapture via the
  workflow dispatched on `main` (pre-PR code = the honest floor) and
  hand-promote the artifact to the committed `0001_*.json`.
- **Devlog the change.** Each meaningful PR gets a `devlog/` entry —
  manual prefix today, `/devlog` skill (auto from git commits) going
  forward.
- **Commit messages explain why.** Short subject, body when motivation
  isn't obvious from the diff. Co-authored-by line stays.
- **Work through `feature/* → dev → main`.** Normal changes land via a PR
  into `dev`, then a `dev → main` promotion — never a direct push to
  `main`/`dev` (admin break-glass aside). See *Branching & release
  strategy* below for the full flow.
- **Keep CLAUDE.md current — in the same commit/PR.** Any change that
  alters the workflow, architecture, deploy/branch model, security
  contract, or an env var updates the relevant CLAUDE.md section as part
  of that same commit, not as a follow-up. CLAUDE.md is the source of
  truth future-you reads first; a diff that changes behavior but leaves
  CLAUDE.md stale is incomplete.

## Branching & release strategy

Mirrors the `natejswenson.io` model, adapted for a public repo with a
version-driven release.

- **Topology**: `feature/* → dev → main`. **`dev` is the live working branch**
  — all tested work lands there and the local container deploys from it (see
  *The live deployment tracks `dev`* above). **`main` is the public-consumption
  snapshot**: promoted from `dev` only deliberately and rarely (when Nate
  explicitly asks to release), never per-commit. `main` is the default branch on
  GitHub purely so the public lands on a stable snapshot. Both are protected: a
  PR is required (no direct push for normal flow), CI `validate` must be green,
  linear history, squash-only, branch auto-deleted on merge. Reviews are
  0-required (solo dev) so a green PR self-merges via native auto-merge
  (`gh pr merge --auto --squash`).
- **`enforce_admins: false`** is deliberate — Nate (sole admin) keeps a
  direct-push break-glass path. Protection is a discipline gate for the
  normal workflow, not a hard boundary.
- **Auto-tag on promotion**: `release.yml` is version-driven and
  retargeted to `[main]`. A `dev → main` promotion that bumps
  `pyproject.toml` version (+ matching `CHANGELOG` entry) auto-cuts
  `vX.Y.Z`; a no-bump promotion is an idempotent no-op release. This is
  the existing [release policy] — code/prompt change ⇒ version bump.
- **Dependabot** targets `dev` (`target-branch: dev` on all ecosystems),
  so dependency bumps flow through the same `dev → main` promotion.
  Dependabot PRs do not auto-merge for free — `gh pr merge --auto --squash`
  per PR (or add a dependabot-automerge Action if it gets tedious).
- **`workflow_run` evaluates the default branch's copy** of `release.yml`,
  so any change to its trigger must land on `main` to take effect.
- **`dev` is reset onto `main` after every promotion — now automated.** A
  squash-merged `dev → main` leaves `dev` with diverged history (identical
  tree, but ahead/behind by 1), so the *next* promotion PR would show phantom
  diffs. The `reset-dev-after-promotion.yml` workflow runs on every push to
  `main` and force-resets `dev` to main's SHA via `ops/reset-dev-to-main.sh`
  (which flips `dev`'s `allow_force_pushes` on, force-updates the ref, and
  restores protection — the old manual dance, scripted). It's idempotent
  (no-op when `dev` already equals `main`, e.g. an admin break-glass push).
  **Requires a `DEV_RESET_PAT` repo secret** (a PAT with Administration:write +
  Contents:write — the default `GITHUB_TOKEN` can't edit protection or
  force-push a protected branch); without it the job skips cleanly. Manual
  fallback: run `ops/reset-dev-to-main.sh` locally with an admin-authed `gh`.
- **`dev` and `main` are deletion-protected**, so the repo-wide
  delete-branch-on-merge does NOT eat `dev` on a promotion — only
  `feature/*` heads are auto-deleted.

## Answering fitness questions (in-repo Q&A)

When the user asks an ad-hoc question about their data ("show my plan through
today", "how's my training load", "what did I run last week"):

- **Use the structured `mcp__fitness__*` tools.** There's one for almost
  everything — `get_training_plan_progress` (graded plan day-by-day),
  `get_training_plan_status`, `query_workouts`, `get_metric_trend`,
  `daily_snapshot`, `training_load_status`, etc. Reach for `run_sql` only when
  no structured tool fits. For "scheduled vs actual" / "am I hitting my plan"
  chart asks, `plan_chart` (0.23.0) is THE tool — never hand-roll matplotlib
  or ASCII via Bash for that view. For "how did that run go / grade my
  workout / was that good" on a SINGLE session, `workout_report_card`
  (0.25.0) is THE tool — it returns a preformatted `markdown` card, which you
  render to the user VERBATIM rather than re-summarizing; don't assemble your
  own verdict from `get_workout_detail` when a graded one exists. **Never shell out to `sqlite3`/Bash for a DB read** —
  the agent did exactly that once and it dumped `PRAGMA` introspection and SQL
  errors at the user. One tool call when a tool exists.
- **`get_training_plan_progress`'s `workouts` list is windowed by default**
  (2026-07-13) — trailing 14 days + upcoming 7 days anchored to the data
  frontier, not the full plan. "Show my plan through today" on a plan older
  than 14 days needs the complete list, so pass `full=true` to get it; the
  rollups (`adherence_pct`, `days_to_race`, `goal_gap`, `this_week`, …) are
  always whole-plan regardless of the `workouts` window.
- **The agent owns the entire plan lifecycle — there is no UI.** To adjust a
  session, `update_plan_workout(date, type/distance_mi/pace_min_per_mi/duration_min/description)`
  re-prescribes ONE day on the *active* plan. `type='rest'` clears distance,
  pace **and** duration, and overwrites `description` with `"Rest day"`.
  **It cannot move or add a day.** `date` is the `UPDATE`'s key, not an editable
  column (`plans._EDITABLE_WORKOUT_COLS` = `type`, `target_distance_m`,
  `target_pace_sec_per_km`, `target_duration_sec`, `description`), and a date
  with no existing prescription errors rather than inserting. So "move Saturday's
  long run to Sunday" is **two calls** — rest the old day, prescribe the new one
  — and only works if the new day already exists on the plan. Structure changes
  (whole new plan) go through
  `propose_training_plan`/`revise_training_plan` (drafts), and the rest of the
  lifecycle — activating a draft, dropping a draft, or abandoning the active
  plan outright — is `commit_training_plan`/`discard_training_plan_draft`/
  `abandon_active_plan` (2026-07-09, UI-retirement design). The write boundary
  is enforced in `plans.py` (`update_active_workout` whitelists prescription
  columns only — it can't re-key/re-status/restructure). Don't hand-write
  `UPDATE` SQL — the tool exists.
- **Don't narrate the lookup.** The user wants the answer, not the mechanics.
  Lead with a one-line answer, then a clean table (at most ~4 columns, one-word
  headers, never a sentence in a cell) plus short coach text. Per-item detail
  (a plan, a week schedule) → one compact `label: value · label: value` line per
  item, not a wide grid.
- **Always render charts fully *in the reply*, never in a collapsed tool call.**
  When you produce a chart/graph (the `chart` styles, or an ad-hoc render),
  paste the full output into the message in a fenced code block so it shows
  expanded by default — then add the coach read. It's fine to compute the chart
  by running the renderer via Bash, but a chart left only in the Bash/tool-call
  output is collapsed in the UI and forces the user to hit Ctrl-O to see it,
  which Nate flagged as "very unfriendly." Reproduce the exact output in the
  reply. Applies to every chart, every time.
- This is advice, not an enforced gate — but with a tool that exists for the
  job, there's no reason to query the DB by hand.

## What's already wired

These are settled — don't redesign without a reason.

- **The web UI is retired (2026-07-09) — MCP is the only client surface.**
  The entire `web/` directory (React/Vite SPA), every UI-only REST route
  (`/api/*`), the background-sync-orchestration subsystem, and the SPA
  static-file serving are gone. `src/local_fitness/web/server.py` (note:
  a *different* `web/` — the Python package, not the deleted frontend
  directory) still exists and still hosts the authenticated MCP
  streamable-HTTP transport at `/mcp/` plus `/health` — "keep and improve
  the API" was the explicit design goal, not remove the server. The three
  plan-lifecycle MCP tools (`commit_training_plan`/
  `discard_training_plan_draft`/`abandon_active_plan`) exist specifically
  because the UI's commit/delete buttons were the only prior path for
  those actions — see *Answering fitness questions*'s plan-ownership
  bullet. See `docs/plans/2026-07-09-mcp-speed-and-ui-retirement-design.md`
  for the full rationale (also covers Part A: MCP tool speed/efficiency).
- **Brief composer = V2 (agent/code separation), default ON** since the
  2026-06-27 cutover. The pipeline is deterministic `brief_planner` (triggers,
  fixed priority, advisory tone → typed `BriefContext`) → ONE **toolless**
  generator (`max_turns=1`, no MCP) on the shrunk `brief_v2_*` prompt → advisory
  `grounding.flag` (a logged invention-rate *signal*, never a gate). The V1
  tool-driven monolith (`system_prompt`/`briefing_prompt`, `max_turns=20`) is the
  **instant rollback** — `LOCAL_FITNESS_BRIEF_V2=0` (or false/no/off). The planner
  is the tested half (`tests/test_brief_planner.py`, `test_grounding.py`); the
  generator is the eval'd half (`tests/evals/` fixtures + `baseline.json` +
  `scripts/{capture_baseline,shadow_run}.py`). **Only the in-process composer is
  V2** — the MCP `mcp__fitness__*` tools and the MCP `_brief_prompt` (chat /
  external-agent path) still use V1's tool-driven approach (a deliberate scope
  choice; `grounding.flag` is the reusable follow-up there).
- **One coach voice, composed by every prompt surface** (0.28.0). The profile
  was already resolved everywhere, but what surrounded it had drifted:
  `plan_coach`/`workout_coach` carried `persona` + `dials_line` yet omitted the
  profile heading and the notes-precedence rule, and **hardcoded the user's
  name into their prompt text**. Now: `prompts.coach_voice_block(user_name,
  profile, compact=)` and `prompts.user_notes_block(user_name, notes_text)` are
  the single definition, and every surface composes them. Both are **pure** —
  `notes_text` is passed in, never read — because `plan_coach` and
  `workout_coach` key their disk caches on a hash of the assembled prompt; a
  builder that did I/O would break caching. `compact=True` is the V2 brief's
  shorter variant (V2 is deliberately the shrunk prompt; don't let it grow).
  `briefing_prompt` is the brief's USER message and deliberately carries no
  persona — its profile-sensitivity is the `includes_harsh_block` gate.
  **`config.user_name()` is the ONLY resolver** (DB > env
  `LOCAL_FITNESS_USER_NAME` > `"the user"`); nothing calls
  `db.get_setting("user_name", ...)` with its own default any more, and
  `tests/test_prompts.py` fails the build if a prompt module puts a personal
  name in a non-docstring string literal. When adding a prompt surface, add it
  to `_voice_surfaces` in that file — the gate is what keeps this true.
- **The personality is tunable conversationally; the shipped default is the
  hardass accountability mirror** (0.31.0). `coach.DEFAULT_PROFILE` is
  `"hardass"` (rewritten as an original accountability-mirror persona — never
  naming/imitating a real coach, with a "Using your memory" receipts section
  and a never-do list that pins "red recovery day → order the rest") and
  `config.coach_profile()`'s default literal matches — config can't import
  coach (cycle), so `tests/test_coach.py::test_config_default_matches_coach_default`
  pins the pair. The tuning layer: `agent/personality.py` owns a
  `PersonalitySpec` stored as JSON in settings key `coach_personality_spec`
  (≤8 KB, identity ≤4000 chars, ≤12 items/list, ≤16 intensity topics),
  edited ONLY via the `get_coach_personality`/`update_coach_personality` MCP
  tools (agent-owned writes, like plans). **Virtual seeding**: no stored spec
  → behavior is byte-identical to the profile `.md` files; the first update
  materializes `seed_from_profile(active)` + patch. `resolve_coach_profile`
  parses the spec out of the `all_settings()` dict it already fetched (zero
  added reads) and attaches it as `CoachProfile.spec`;
  `profile.effective_persona` (spec render > file prose) is what
  `coach_voice_block` speaks. A spec whose `base_profile` mismatches the
  active profile is **ignored but retained** (switch back and the tuning
  returns; the get tool reports `base_profile_mismatch`). The 5 numeric dials
  stay in their existing settings keys — the update tool writes those keys,
  never duplicates them into the spec. Precedence: **notes > spec > profile
  file**. Kill switch `LOCAL_FITNESS_COACH_SPEC=0` ignores (never deletes)
  the spec. `scripts/score_profiles.py`'s hardass markers track the persona's
  actual signature lines — update them together.
- **Run-vs-walk is decided by measured pace everywhere, never by
  `activity_type`** (0.27.0). Garmin's label lies — walking-desk sessions log
  as `treadmill_running` — so `plans.GradingConfig.pace_gated_locomotion` (on
  by default) routes `_running_distance`/`_running_duration` through `_ran`,
  which delegates to `interpret.is_running_effort`. Measured consequences: an
  interval day reported 9.2 mi actual when the run was 5.95 mi, and that day's
  1:34:30 walk was on its own long enough to satisfy any rep-session duration
  target. **Easy/recovery days still count walking on purpose** — that is what
  makes a prescribed walk day gradeable, and the plan now has two a week — so
  do not "fix" `_foot_distance` to be run-only. A paceless row falls back to
  the label rather than vanishing from mileage. **`load_activities_by_date`
  MUST select `avg_pace_sec_per_km`**: without it every row falls back to the
  label and the whole gate is a silent no-op (it shipped that way for one
  render before a live PDF caught it). Since 0.35.0 the brief planner's run
  signals (`days_since_last_run`, `runs_14d`, `recent_te`) obey the same rule —
  `brief_planner._running` checks on-foot FIRST, then pace, then label
  fallback for paceless rows (on-foot-first is load-bearing: pace alone would
  promote a fast bike ride to a run) — and so does `plans.best_recent_effort`,
  which feeds the Riegel projection (there a paceless row is excluded rather
  than label-fallbacked: an unverifiable pace can't be a "best effort").
- **Analysis tools carry deterministic interpretation, not just raw numbers**
  (2026-07-13). `agent/interpret.py` is a pure, stdlib-only module (no I/O, no
  SDK) housing every classifier the brief path already computed in tested
  Python — `tsb_zone`, `pct_change`, `trend_direction`, `delta_direction`,
  `baseline_position`, `correlation_read`, `effect_size`, `sd_position` — and
  `status.py`/`brief_planner.py` delegate to it rather than keeping their own
  copies. The ad-hoc MCP tools (`training_load_status`, `correlate`,
  `find_anomalies`, `compare_periods`, `get_metric_trend`) attach the same
  fields (`tsb_zone`, `strength`/`direction`, `sd_distance`, `magnitude`,
  `slope_direction`, etc.) to their payloads instead of leaving the model to
  apply a static legend string by hand. The rule holds project-wide: the LLM
  phrases a judgment, it never derives one that tested Python can compute.
- **The coach has a two-layer memory on every voice surface** (0.30.0). Layer 1
  is the deterministic relationship ledger (`agent/ledger.py`, pure +
  persistence divider like `plans.py`): adherence miss/done streaks (reusing
  `plans.build_plan_detail`'s verdicts — never re-grading), step-goal streaks
  computed **as-of-yesterday** (today's partial count must never flip the block
  intra-day — that stability is what keeps the prompt-hash caches valid),
  observation repeat-patterns, notable results, and (0.34.0) a trailing-3-week
  report-card aggregate (`report_card_facts`: count, avg GPA, base-letter
  grade distribution, rising/falling trend via `interpret.pct_change` +
  `delta_direction`) computed ONLY over cards whose `activity_date` is
  strictly before today — the same as-of-yesterday discipline as step
  streaks, so grading today's run can never change today's `memory_text`.
  Layer 2 is the coach's own
  journal (`agent/journal.py`, `coach_journal` table — 60-entry HOT cap
  **archived on write, never deleted** since 0.33.0, 240-char lines, partial
  unique index on `(source, source_key, seq)`). The archive is searchable:
  `journal.search_entries` runs BM25 over a `coach_journal_fts` FTS5
  external-content index (sync triggers; query tokens quoted as phrases so
  MATCH syntax is inert; LIKE fallback when the SQLite build lacks FTS5),
  exposed as the `recall_coach_memories` tool in `ALL_TOOLS`. **The FTS DDL
  lives in `db.FTS_SCHEMA`, never in `SCHEMA`** — `executescript` aborts
  whole-script on error, so FTS5-in-SCHEMA would brick every table on an
  FTS5-less build; it self-heals on count mismatch against the `_docsize`
  shadow table (COUNT(*) on an external-content vtable reads through to the
  content table and always "matches"). Only `delete_coach_memory` removes
  entries for real; there is no prune path.
  `agent/memory.py` is the ONE resolver; `prompts.coach_memory_block` is the
  pure injection block whose header carries the grounding contract (callbacks
  may cite only listed facts; empty section → no callbacks). **Memory is
  passed INTO the four voice surfaces as `memory_text`, never resolved inside
  the builders** — the PDF coaches key disk caches on the prompt hash, and
  `prompts.py` builds `SYSTEM_PROMPT` at import (an internal DB read would
  open the DB on `import prompts`). The compact V2 variant is hard-capped at
  `memory.COMPACT_MAX_CHARS` (600) so V2 stays the shrunk prompt. Writes:
  `agent/reflect.py` auto-reflects after each **saved** brief
  (`generate_and_save` tail — post-persistence, fail-silent, +~10s on the
  launchd job) and each **first-render** report card (fire-and-forget task;
  `journal.has_event` pre-check + the unique index + `exclude_source_key`
  filtering make it idempotent and cache-cascade-proof: a card's own journal
  entries are excluded from that card's prompt, or reflecting would bust its
  cache forever). Chat writes via `save_coach_memory`/`list_coach_memories`/
  `delete_coach_memory` (in `ALL_TOOLS`), and since 0.33.0 the system prompt
  carries explicit capture directives (save durable facts when shared; a 1-2
  line session note after substantive conversations) plus a retrieval
  contract (search `recall_coach_memories` before claiming not to remember;
  never cite a memory the search didn't return — gated by
  `tests/test_prompts.py`). `LOCAL_FITNESS_COACH_MEMORY=0` is
  the kill switch for injection AND reflect; journal data survives it and
  recall still works (the switch never touches journal data). Memory
  resolution never runs inside a perf-benchmarked hot path — SDK-call sites
  only.
- **Daily brief job needs a Claude credential in `.env`.** The launchd job
  (`com.localfitness.brief` → `fitness brief --if-missing`) couples pull →
  recompute-baselines → generate → save atomically, firing at **06:30 with
  a 09:30 backstop slot** (same command both times; `--if-missing` makes
  any fire a no-op once today's brief exists, so the backstop only acts
  when the 06:30 run failed — re-run `ops/install-launchd.sh` after
  changing the template for it to take effect). Its *generate* step
  spawns a **headless** Claude via the Agent SDK, which authenticates from
  the process env only — `cli.py` `load_dotenv()`s `<repo>/.env`, so the
  token must live there as `CLAUDE_CODE_OAUTH_TOKEN` (Nate's Max token,
  minted with `claude setup-token`, no per-brief cost, expires ~yearly) or
  `ANTHROPIC_API_KEY` (never expires, bills per brief). A live Claude
  session (like this chat) can compose + `save_brief` fine because it
  already holds a token — but that's **not** the unattended path.
  **Failure signatures — read the log before touching the token** (the
  2026-07-19 facet review found an earlier draft of this note blamed the
  credential for what was actually SDK stream instability; following it
  meant re-minting a healthy token while failures continued):
  - *Credential missing/expired*: generation fails **before any first
    message** — no `brief_timing phase=first_message` line in
    `logs/brief.launchd.err.log`. Fix = put/refresh the token in `.env`
    (gitignored); re-mint on expiry.
  - *SDK stream death* (the common one): `ttfm_ms` present (~1–2.5s, so
    the token is fine), then either the stream idles out empty
    (`loop_exit reason=normal chars=0`, "produced no output") or the
    subprocess crashes mid-stream ("Fatal error in message reader",
    partial chars discarded). Transient — since 0.23.0 the pipeline
    self-heals: a 120s per-message idle watchdog kills a hung stream and
    `generate_and_save` makes up to 3 attempts
    (`LOCAL_FITNESS_BRIEF_IDLE_TIMEOUT_S` / `_BRIEF_MAX_ATTEMPTS` /
    `_BRIEF_RETRY_DELAY_S`, see `.env.example`). If all attempts fail,
    `fitness brief` fires a distinct macOS **failure** notification
    (silence no longer is the only signal) and exits non-zero.
  Either way the outcome is **no brief saves** (orphaned sync — pull ran,
  brief didn't). That state is now surfaced: `assemble_status()` (→
  `get_today_status` / `daily_snapshot`) carries `latest_brief_date` +
  `brief_stale_days`, and the `fitness://brief/latest` resource leads
  with a STALE banner when serving a brief older than today.
- **Garmin pulls reuse a cached session token** (since the 429 fix). `daily.py`
  `_client()` passes `_tokenstore_path()` to `client.login()` instead of a
  no-arg login, so a pull resumes the saved garminconnect session instead of a
  full SSO login every time (repeated logins trip Garmin's rate limit →
  `Mobile login returned 429`). The path defaults to
  `~/.garminconnect/garmin_tokens.json` (the host side of the container's
  `${HOME}/.garminconnect` bind-mount — host and container share one token, so
  the host's interactive first login seeds the container); `GARMINTOKENS`
  overrides it. **First run must be interactive** (`uv run fitness pull` once) so
  the MFA prompt can seed the token; after that the launchd job resumes from it.
  The cached OAuth token eventually expires (no fixed TTL) — when it lapses the
  non-interactive 06:30 job may hit a login/MFA and fail; the remedy is to
  re-seed with an interactive `uv run fitness pull`. `~/.garminconnect` is
  outside the repo; `Path.home()` resolves from `HOME`, so the launchd job and
  the seeding shell must share the same `HOME`. **Activity details are
  fetched once, not per pull** (0.36.0): `_ingest_activity_range` pre-checks
  stored `activity_hr_zones`/`activity_splits` ids and, for an activity dated
  before wall-clock today with BOTH present, refreshes only the summary row
  (INSERT OR REPLACE — the freshness feature) and skips the two detail calls
  plus their 0.3 s throttle. Today's activities always re-fetch (laps may
  still be finalizing — that guard is load-bearing). The day loop shares ONE
  connection with per-day commit/rollback (a failed day rolls back only its
  own writes), and only sleeps BETWEEN days.
- **The HTTP persona is memoized behind a `data_version` key** (0.36.0). In
  stateless mode every `/mcp/` request re-resolved the full persona (6
  connects + a whole ledger compute — ~5× the tool call it wrapped).
  `mcp_server._with_coach_persona` now memoizes on
  `(today, db_path, PRAGMA data_version, notes-file (mtime_ns, size))` — a
  dedicated read-only monitor connection reads `data_version`, which bumps on
  ANY other connection's commit (settings UPSERTs and journal archived-flips
  UPDATE in place, so rowid-style keys would miss them; that's why it's NOT
  MAX(rowid)). The notes stat covers the file-backed notes data_version can't
  see; the date covers the ledger's as-of-yesterday facts. No-DB/error →
  key None → resolve live, never cache (fresh-clone fail-open preserved);
  failures are never cached. Env-var changes still need a restart, as before
  the memo. The monitor conn must NEVER write (data_version only reports
  OTHER connections' commits) and re-opens if `db.get_db_path()` changes.
  `_persona_cache_clear()` resets it (tests use an autouse fixture).
- **The container MUST set `TZ`; UTC is not harmless** (0.39.0). A container
  with `TZ` unset runs UTC while the host CLI uses the machine's real zone,
  so from ~19:00 America/Chicago until midnight the container's
  `date.today()` is already TOMORROW — and this app is date-anchored
  everywhere ("today", "the last complete day", `ledger`'s as-of-yesterday
  streaks, every trailing trend window). Measured 2026-07-27 19:59 CDT
  (container: 2026-07-28 00:59 UTC), same image and same bind-mounted DB:
  host read `avg_stress` 28 (−12.5%) / TSB −22.41 "very fatigued" /
  `body_battery_max` 55, container read 17 (−46.9%) / −12.74 "fatigued" /
  `None`. The container was reproducing the exact false-recovery reading
  0.39.0 had just fixed. **Nothing errors** — you get a confident answer
  computed against the wrong day. Two reasons it hid for so long: the 06:30
  launchd brief is unaffected (UTC and Central share a calendar date at
  that hour), so the flagship surface looked fine while evening chat, report
  cards and PDF renders drifted; and 0.39.0 raised the stakes by anchoring
  the partial-day and current-form fixes explicitly to "the last COMPLETE
  day". Wired as `TZ=${LOCAL_FITNESS_TZ:-America/Chicago}` in the traefik
  repo's compose `environment:` block, documented in `docs/deployment.md`
  and `.env.example`. Verify with `docker exec <container> date` against
  the host's `date` — they must match. The host CLI needs nothing; it
  inherits the OS zone.
- **Path defaults**: `db.py`, `notes.py`, `briefing.py` all resolve to
  `_PROJECT_ROOT / ...` when env vars are unset.
- **MCP tool surface can trigger a Garmin sync, not just read.** `agent/tools.py`'s
  `sync_garmin_data` (in `ALL_TOOLS`, not in the brief loop's read-only
  allow-list) wraps `ingest.daily.pull(max_days=SYNC_MAX_DAYS)` +
  `ingest.baselines.recompute()` — a bite-sized, gap-aware pull, capped so a
  long absence doesn't turn one tool call into a multi-minute Garmin backfill.
  **`partial` is a normal outcome, not an error** (0.35.0): `pull` returns
  `partial` whenever any gap remains back to 2020, so treating it as an error
  made every sync for a user with one missing historical day return
  `is_error: true` AND skip the recompute — training load froze while fresh
  workouts landed. Now only hard failures (`auth_failure`/`not_configured`/
  `failure`/`interrupted`) are errors, the recompute fires whenever
  `days_pulled or activities_loaded` is nonzero, and the payload carries a
  deterministic one-line `sync_state` plus countable `days_failed`.
  Any MCP client wired to `fitness mcp-stdio` (Claude Desktop, opencode, etc.)
  can call it directly; before this tool existed, MCP-only clients had read
  access to the DB but no way to freshen it — only the CLI (`fitness pull`)
  could. `run_stdio()` in `web/mcp_server.py` serves `ALL_TOOLS`
  as-is, so a new tool here needs no separate wiring to reach `mcp-stdio`.
- **The two PDF-writing tools are stdio-only — `generate_brief_report` and
  `workout_report_card` (0.25.0); `generate_chart` moved into `ALL_TOOLS`
  (2026-07-13, MCP-speed-and-UX-01 fold-in Fix A).** The rule that decides
  membership: a tool that hands back a *filesystem path* is local-only,
  because a remote `/mcp/` caller gets a container-internal path it cannot
  retrieve. `generate_chart` renders a standalone matplotlib PNG on
  demand and now returns it as an inline MCP image content block (alongside
  the saved file path as text) — reachable over both `fitness mcp-stdio` and
  the networked `/mcp/` transport, since a client no longer needs the local
  file path to see the chart. `generate_brief_report` (`agent/tools.py`'s
  `LOCAL_ONLY_TOOLS`) renders a saved daily brief
  into a polished PDF (`agent/visuals.py`'s WeasyPrint pipeline, reusing the
  `budget` project's validated color theme) and stays stdio-only for the
  same reason as before: a PDF isn't representable as an MCP content block,
  so a phone-triggered call over `/mcp/` would get back a container-internal
  path with no way to retrieve the file. Since the 2026-07-09 UX pass, it
  writes to and auto-opens from an **ephemeral per-process tmp directory by
  default** (`tempfile.mkdtemp`, PID-embedded naming, cleaned up via `atexit`
  when the `fitness mcp-stdio` process exits — a fresh subprocess per
  opencode/Claude session, so this is the natural session-cleanup boundary)
  rather than the old persistent `./reports/`. A best-effort
  liveness-checked sweep also reaps any prior session's leaked directory
  (PID dead + >24h old) on each process's first call — `atexit` doesn't fire
  on `SIGKILL`/`SIGTERM`, so an abrupt kill can still leak until the sweep
  claims it; this is an accepted residual risk for a personal, single-user
  tool, not something hardened further. **Both PDF filenames are
  content-addressed** (0.28.2): `brief-<date>-<sha8>.pdf` and
  `report-card-<id>-<sha8>.pdf`, where `sha8` is `_render_tag()` — a sha256
  over the render's logical INPUTS (brief/card content + chart PNG bytes +
  brand theme + app version), NOT over the PDF bytes. Bytes-hashing was
  0.28.2's original design and it was wrong: WeasyPrint's PDF serialization
  is not byte-reproducible (the same HTML rendered twice in one process
  diverged on ~50% of paired Linux renders, measured 2026-07-23; macOS's
  allocator usually masks it), so the "identical content reuses one filename"
  half of the contract failed at random and its CI test was a coin flip.
  `generate_chart`'s PNG still uses `_content_tag()` (bytes) — matplotlib's
  PNG writer IS reproducible. This is not cosmetic — macOS `open`
  RE-FOCUSES an already-open Preview window for a path it has seen rather than
  reloading the bytes, so the old deterministic `brief-<date>.pdf` showed a
  STALE render on every re-generate. A user read yesterday's-looking page and
  concluded the whole data pipeline was stale (observed 2026-07-22, a real
  trust hit). The content tag means changed content always lands on a NEW
  filename (a genuinely fresh window) while identical content reuses the same
  file (idempotent — refocusing is correct when the bytes match). Don't revert
  to a static name. After a successful write, the file
  is auto-opened via macOS `open` (best-effort, never fails the tool call;
  logs a warning and no-ops on other platforms). Set
  `LOCAL_FITNESS_REPORTS_DIR` to opt back into a persistent directory (still
  auto-opened, no auto-cleanup) — see `.env.example`. If you have an
  existing populated `./reports/` from before this change, it's vestigial
  now and safe to delete by hand. `generate_brief_report` is registered ONLY
  via `run_stdio()`'s `build_server(extra_tools=agent_tools.LOCAL_ONLY_TOOLS)`
  call — structurally, not just by convention, unreachable over the
  authenticated streamable-HTTP `/mcp/` transport (`build_session_manager()`
  calls `build_server()` argument-free). WeasyPrint needs native
  Pango/HarfBuzz libs — `apt-get` on Linux/CI, but on macOS Homebrew's
  install isn't on the default dylib search path and needs
  `DYLD_LIBRARY_PATH=$(brew --prefix)/lib` (see `.env.example`).
- **`save_brief` advertises the real Brief JSON Schema** (0.28.2), not an opaque
  `{"brief": dict}`. `tools._save_brief_input_schema()` derives it from
  `schemas.Brief.model_json_schema()` (so it can't drift from what the server
  validates), hoists the pydantic `$defs` to the schema root so the nested
  `$ref`s resolve, and narrows `brief.required` to `["takeaways"]` because
  `briefs.save_brief` stamps `date`/`user_name`/`generated_at` server-side. The
  Agent SDK forwards a dict schema verbatim ONLY when it has a top-level string
  `type` + `properties` (else it treats the dict as a `{name: python-type}`
  shorthand and silently drops the rest) — the constant satisfies that, guarded
  by `test_save_brief_schema_meets_the_sdk_passthrough_condition`. Motivation: a
  live agent had to grep + Read `schemas.py` to learn the Takeaway/tone/metric
  shapes before it could save (2026-07-22); a filesystem-less MCP client
  (Claude Desktop, a phone over `/mcp/`) couldn't construct a valid brief at
  all. Now the contract carries the enum values and sub-object shapes.
- **Workout grading is deterministic Python, not model judgment** (0.25.0).
  `agent/report_card.py` backs the `workout_report_card` tool and follows the
  `interpret.py` rule: the LLM phrases a judgment, it never derives one code
  can compute. Four metrics (distance, pace, HR, training load) each reduce
  to ONE non-negative relative deviation `d` fed through ONE shared
  `GRADE_BANDS` table — four small deviation functions, one grader, so the
  rubric stays testable. Design constraints that are load-bearing, not
  incidental:
  - **The card always names its yardstick.** Plan-prescribed (distance and
    pace only — `plan_workouts` has no HR or load column) or a 60-day rolling
    *median* of comparable activities. Median, not mean: the history carries
    real training-load outliers. Under `MIN_REFERENCE_ACTIVITIES` (5) it
    returns n/a and says so rather than grading against noise.
  - **Direction gating.** An easy run is *supposed* to be slow — grading
    `|actual − expected|` would hand every recovery run an F. Easy/long days
    are penalized only for too FAST, quality days only for too SLOW, and each
    expectation is intent-scaled (`DISTANCE_FACTORS`/`PACE_FACTORS`/
    `LOAD_FACTORS`/`HR_BANDS`).
  - **Comparability is exact `activity_type` first**, widening to the on-foot
    class only when the pool is too thin. Measured on live data: pooling
    `running` with `treadmill_running` put median HR at 119 against an
    outdoor average of 140 and gave a normal easy run a D. Treadmill and road
    are different HR regimes and must not share a yardstick unless forced to.
  - **Comparability is ALSO gated on locomotion, measured not labelled**
    (2026-07-21; since 0.27.0 the constant and `is_running_effort` live in
    `agent/interpret.py`, re-exported here, so `plans.py` can share them
    without a `plans → report_card → plans` cycle. `RUN_PACE_CEILING_SEC_PER_MI`
    = a 13:00 mile). `activity_type`
    is Garmin's label and it lies: Nate's walking-desk sessions log as
    `treadmill_running`, and both the exact-type filter and `plans._is_running`
    (a substring match on "running") passed all of them through. On live data
    that 60-day pool held 46 activities split 16 real runs (8:40–11:46/mi, HR
    114–172) against 30 walking-pad sessions (14:08–84:20/mi, HR 76–120), so
    the "median comparable activity" was a 15:50/mi walk at 116 bpm and 22
    load — which handed a genuine interval session an A+ on **both** HR and
    load, 40% of the composite, for clearing a walking bar. A run now compares
    only against running-effort activities and a walk only against walking
    ones; a paceless row has an unknown mode and joins neither pool. The filter
    runs BEFORE widening, since widening is what would otherwise drag the whole
    walking corpus into a thin running pool. `reference_line` states the
    exclusion count on the card — it is invisible in the numbers otherwise.
  - **Splits are presentation-only, with exactly ONE documented exception** —
    no grade reads `activity_splits`. Only 87 of 747 activities have them
    (daily-sync ingest writes them, backfill never does), so a splits-dependent
    grade would be unavailable on ~88% of history and mean different things on
    different rows. The exception is **quality-day pace against a prescribed
    rep target**, and it exists because the alternative wasn't a strict grade
    but a broken one: a plan's interval pace describes the *reps*, while
    `avg_pace_sec_per_km` averages in the warmup, recovery jogs and cooldown,
    so that comparison returns F for every correctly-executed interval session
    (measured 2026-07-21: a 6:58/mi prescription averaged 10:42/mi → F, while
    its 4th mile ran 9:25 at 164 bpm). `fastest_rep_split` (0.35.0; formerly
    `fastest_full_split_pace`) is the only number that can answer "did you hit
    the reps" — and it selects by a distance floor (`QUALITY_MIN_SPLIT_M` =
    300 m), NOT the `partial` flag: partial is relative to the workout's own
    longest lap, so a manually-lapped session (2-mile warmup, then 800 m reps)
    marked every rep partial and graded the reps at warmup pace — a guaranteed
    F on exactly the workouts this exception exists to grade fairly. With no
    qualifying split the metric returns n/a **with a stated reason** and its
    weight redistributes — it never falls back to the average-vs-rep
    comparison, and never grades against warmup pace.
  - **A grade must never contradict the prose beside it.** Two live cases fixed
    2026-07-21: `load_deviation` was one-sided-low and uncapped, so a day at 81
    load against a 22 expectation printed **A+** directly above its own
    "**spike** — more than double your median day" note; it is now two-sided
    past `LOAD_SPIKE_FACTOR` (at or under the threshold is still a clean A).
    And `overall_grade` is now **intent-weighted** (`INTENT_METRIC_WEIGHTS`) —
    flat weights let HR + load (40%) outvote the one metric the session existed
    to satisfy, scoring a prescribed 10:28 easy run executed at 9:28 an overall
    B on a card whose own read called it "a race finish". An F on any metric
    also caps the overall at C (`capped_by`), stated in the notes.
  - The pure section is stdlib-only and unit-testable with plain dicts; DB
    access lives under a persistence divider, mirroring `plans.py`.
  - **The card's opening verbal read is `agent/workout_coach.py`** — a sibling
    of `plan_coach.py`, same shape (toolless single-shot SDK call, lazy
    imports to dodge the `tools -> workout_coach -> briefing -> tools` cycle,
    single-entry disk cache keyed on the pure `build_prompt` hash **plus
    `activity_id`**, deterministic `fallback_read`). Separate module because
    `plan_coach` preps a run not yet done from a prescription while this one
    judges a run already done from graded results — different inputs, tense,
    and failure mode. The model is told the grades are not its to revise: it
    phrases them, it never re-derives them. Its timeout is far longer than
    `plan_coach`'s 30s — see the latency bullet below.
  - **The read is FOUR labelled paragraphs, one per graded area** — not one
    blended paragraph. `READ_SECTIONS` is the contract; the model emits
    `DISTANCE:` / `PACE:` / `HEART RATE:` / `TRAINING LOAD:` lines and
    `parse_read` turns them into a typed dict. A generation missing any
    section raises and falls back to the deterministic template rather than
    rendering a blank paragraph — and is never cached. Budgeted in WORDS (45
    per paragraph), not sentences: a sentence cap produced sentences long
    enough to push the HR chart onto a second page.
  - **It must NEVER name a letter grade**, since the letters print in the
    table immediately below it, and it must not discuss CTL/ATL/TSB — that
    line is printed elsewhere on the card and asking for it bought a freshness
    lecture in place of a distance verdict.
  - **That rule is kept by not showing it the letters** (0.28.1). The user
    prompt carries `grade_severity()` words ("well off target") where it used
    to print `Distance: D- — actual 5.95 mi…`, and `_GRADE_TONE` states the ban
    without spelling out example letters (it used to list `"A"`, `"B-"`,
    `"C+"`, planting them in the same breath). Measured: 3.1% of paragraphs
    leaked and a leak **regenerated to the SAME letter**, because the retry saw
    the same prompt; after, 0 of 96 on the identical protocol with latency
    unchanged. **Severity, not silence** — deleting the judgment outright would
    let the read drift out of agreement with the table, since a +19% distance
    overshoot is a D- only because intent scaling says an interval day is no
    place for bonus miles, and that is not recoverable from the raw numbers.
    `find_grade_leak` plus ONE regeneration in `generate_read_cached` is the
    backstop, not the fix; it is deliberately narrow because a bare "A" is
    usually the article ("A blown interval session…") and a false positive
    throws away a clean read. Its specification is the two lists in
    `tests/test_workout_coach.py` — extend both before touching the pattern.
  - **It gets hindsight and foresight.** `load_report_card_inputs` supplies
    the trailing runs and the next 7 days of prescriptions (capped by
    `MAX_CONTEXT_ACTIVITIES`), so the read can place the run in the week
    instead of judging it in isolation. Both are prompt-only — like splits and
    the HR trace, no grade reads either.
  - **`workout_coach` owns its own model constant** (2026-07-21) — it does NOT
    follow `briefing.DEFAULT_MODEL`. That constant also drives the daily brief
    generator, where a model change is a prompt change that has to clear the
    scorer and a cross-model A/B first, so the coupling meant this call could
    not be tuned at all. The two share a vendor and nothing else: the brief
    reasons over a whole day of data, while this one phrases four 45-word
    paragraphs from grades `report_card.py` already computed and the prompt
    explicitly forbids it from re-deriving. Sized to that job: Sonnet tier
    (`DEFAULT_MODEL`), because nothing here is intelligence-bound — but not
    Haiku, since the four-section `READ_SECTIONS` contract is load-bearing (a
    missing section raises and drops the card to the deterministic template).
    **`effort` and `thinking` are set explicitly and are load-bearing, not
    polish**: current Sonnet runs adaptive thinking whenever `thinking` is
    unset, so moving the model ID forward without `effort="low"` +
    `thinking={"type": "disabled"}` would have made an already-67s call
    *slower*. Any change here gets the measured A/B (latency, `parse_read`
    success, word-budget compliance), never an eyeball — cost is not the
    deciding axis, latency and format compliance are.
  - **Budget ~10s for the SDK call** (`DEFAULT_TIMEOUT_S` is 90). Measured
    2026-07-21 over 12 generations on 2 real cards: the old config
    (sonnet-4-6, no effort, adaptive thinking) ran a **median 142.9s / max
    165.6s** — 15s inside its own 180s ceiling, where a timeout silently swaps
    the coach's voice for the template — while `effort="low"` + thinking off on
    current Sonnet runs a **median 10.0s / max 10.8s**. Same A/B: `effort`
    `medium` bought no latency back and more than doubled 45-word-budget
    overruns (10/24 paragraphs vs 4/24), and it leaked what `_GRADE_TONE`
    forbids outright — "F is F." and "B+ on paper". Haiku failed every call.
    The disk cache makes every repeat render instant.
  - **Every generated PDF is exactly ONE page (0.27.0).** A split-heavy card
    used to be 2 pages; it no longer is. `visuals.fit_one_page` lays the
    document out, counts `len(document.pages)`, and steps down the three-rung
    `DENSITY_PRESETS` ladder until it fits. The read's 45-word budget is still
    real — it is the swing factor in which rung gets used — but overflow is now
    caught by measurement rather than by a note in this file. Any card content
    added here should still be measured on `activity_id` 23685126977 (6
    splits) before shipping; the difference is that the ladder, not you,
    absorbs it.
  - **A metric's `Expected` column must be the number its grade was actually
    measured against.** HR broke this: it showed the bare rolling median while
    grading against a band edge, so a run at 136 vs a 146 median printed
    "-7%" next to a B+ when the finding was 6% ABOVE the ceiling that produced
    the grade. HR is the one metric held to a *range*, so it carries
    `expected_display` (the band), `band`, and `in_band`; `expected_text()`
    prefers that display and everything else formats its number.
  - **Calibrate bands against real data, not intuition.** The original easy-HR
    ceiling (0.88x median) demanded a number that appeared in 1 of 13 runs in
    the window — the median is taken over ALL comparable activities, which for
    a mostly-easy runner already sits near easy HR. That made HR a standing
    penalty rather than a judgment. Before changing `HR_BANDS`, check the
    proposed bound against the actual distribution.
  - **A plan target is an instruction; a rolling median is a reference.**
    Plan-referenced distance and pace are graded on bands tightened by
    `PLAN_TIGHTEN` (0.6). Without it both were held to the same tolerance and
    a prescribed 10:28 easy run executed at 9:28 scored a B-, letting the card
    print an overall A for a run its own read called "you never ran easy at
    all". The card must never contradict its own coaching line.
- **Report cards persist as dated snapshots — the coach's durable card
  memory** (0.32.0). Every `workout_report_card` render saves the full card
  (grades + the coach's read) to the `report_cards` table via
  `agent/card_store.py` (journal-style pure/persistence split). The stored
  row is **the card as actually shown, graded against the plan active at
  that render** — grades drift with the active plan, so this is a historical
  record, never a live view, and there is **no backfill** (history
  accumulates as cards render). The save is fail-silent through **one atomic
  guarded UPSERT keyed on the read's prompt key** (`busy_timeout=5000`,
  awaited via `asyncio.to_thread`): an **equal-key render is a byte-identical
  no-op** — so `graded_at` dates the most recent *distinct-key* render, and
  the letter a query tool shows can lag a fresh live render by within-bucket
  grade drift (labeled, not silent) — and a template-fallback render never
  overwrites a real-read row (a stored card's words and grades always come
  from ONE render; no splicing path exists). The stored read doubles as a
  **per-activity read cache**: `workout_coach.read_cache_key` is the single
  key definition, and a re-render whose prompt key matches the stored row
  reuses the read with no SDK call. Query surface: `list_report_cards` +
  `get_report_card` in `ALL_TOOLS` (pure JSON → reachable over stdio AND
  `/mcp/`; `workout_report_card` itself stays stdio-only). Two load-bearing
  assumptions: **no delete/prune path exists** (`load_read` reads outside
  the UPSERT's guard — a pruning tool would open a corrupting window; don't
  add one without revisiting the fast path), and **stored cards are never
  injected into `render_memory_for_prompt` as rows or prose** (would bust
  the prompt-hash caches and re-open the self-render cascade). The 0.34.0
  ledger fact (`ledger.report_card_facts`) is the one sanctioned exception
  and defines what a "parallel exclusion" means here: a deterministic
  AGGREGATE (numbers only, never `card_json`/`coach_read`), restricted to
  `activity_date < today`, and idempotent under re-saves (an equal-key
  re-render rewrites identical grades, so re-rendering a card cannot move
  the aggregate — bounding the one residual: grading a PRIOR-day workout
  mid-day flips memory once that day, costing at most one extra generation
  before the stored-key fast path re-converges; same magnitude as an
  intra-day journal write). A future injection of anything else — raw
  rows, prose, or a `graded_at`-scoped fact (`graded_at` mutates on every
  distinct-key re-render, so a fact keyed on it would change when a card is
  merely *viewed*) — still needs its own exclusion analysis first. If the
  prior-day flip ever proves annoying, the watertight v3 is a
  `first_graded_at` column set on INSERT only (the `init_schema` ALTER
  pattern), filtered `< today`. Design:
  `docs/plans/2026-07-23-report-card-persistence-design.md`.
- **Per-sample HR traces are fetched on demand, never backfilled**
  (`ingest/details.py`, 0.25.0). `get_activity_details` returns ~1700 samples
  per run; pulling that for all 747 activities is both a backfill nobody asked
  for and exactly the repeated-detail-call shape that trips Garmin's 429. So:
  one call for the one activity being graded, cached forever after in
  `activity_hr_samples`, and only when the PDF path asks
  (`load_report_card_inputs(hr_trace=True)` — `format='table'` stays purely
  local with no network). Every function there is best-effort by contract: a
  missing credential, an expired token, a payload with no HR channel, or a
  pre-0.25.0 DB missing the table all return "no samples", and the chart falls
  back to per-lap. **Read the metric channels BY NAME** via
  `metricDescriptors[].key` — `activityDetailMetrics[].metrics` is a positional
  array whose column order varies by device, so a hardcoded index silently
  reads cadence as heart rate. A failed fetch caches nothing, so a transient
  outage never pins an empty trace.
- **Tests must never reach the network — Claude OR Garmin.**
  `tests/conftest.py` has autouse fixtures blocking `claude_agent_sdk.query`
  and `ingest.details.fetch_hr_samples`. The first exists
  because `workout_report_card` generates a read on every render, so the suite
  silently started making real network calls — 10 seconds became 7 minutes, at
  real cost, while still passing green. Patching the single SDK choke point
  (rather than each caller) means a future generator module inherits the
  protection. Callers all degrade to deterministic fallbacks, so the default
  test path exercises the offline branch; a test that needs specific generated
  text patches its own module's generate function. The Garmin guard was added
  the same way and for the same reason: the report card's PDF path resolves an
  HR trace, so a test that merely rendered a PDF started calling Garmin's
  activity-details endpoint for a fixture id — surfacing only as a 404 in the
  logs of an otherwise-passing test. It returns "no samples" rather than
  raising, since that IS the documented offline behavior.
- **Every generated PDF/PNG is styled by a local-overridable brand theme**
  (2026-07-19). `agent/branding.py` owns the tokens: the checked-in default
  is **PRESS** (Nate's cross-project brand — warm paper #F5F0E6, ink
  #181510, dim #6E675C, ONE accent #E8501F, ink rules, no rounded
  corners/shadows/gradients; sans 800–900 structure voice, serif-italic
  commentary, mono data). Tones/verdicts are **typographic** (PRESS-strict):
  done/positive = ink, partial/caution = dim italic, rest = dim, MISSED/
  critical = the accent — zero orange on a good day is on-brand.
  `LOCAL_FITNESS_BRAND_FILE` (JSON, deep-merges over the default; see
  `.env.example`) lets any clone swap colors/fonts/identity without
  touching tracked code; a broken brand file never breaks a render.
  `fonts.mono_file` loads a real TTF via a data:-URI @font-face (Nate's
  `data/brand.json` points at his devlog IBM Plex Mono). `visuals.py`
  consumes the theme for BOTH the WeasyPrint report CSS (`_build_css`)
  and the matplotlib chart styles — the ASCII `chart` tool keeps its
  emoji heat ramp (PRESS is the *print* brand).
- **Both PDFs fit one page, and the fitting is measured, not tuned**
  (0.27.0). `visuals.fit_one_page(build_html, presets)` is renderer-agnostic:
  it takes a callable, renders at each `DENSITY_PRESETS` rung (roomy →
  compact → dense), and returns `(pdf_bytes, page_count, preset_index)` from
  the first rung that lays out to a single page. `chart_h_pt` is the
  load-bearing knob — charts are what break the page, so `img.chart` is capped
  by **height** with `width: auto`; a `max-width`-only cap lets the figure's
  own aspect decide the page budget and makes the ladder a no-op. Density is
  scalars threaded into the ONE stylesheet, never a second stylesheet, so a
  rule fixed at one density can't be missing at another.
  **`render_brief_pdf` returns `(bytes, page_count)`, not bytes** — a caller
  that gets 2 back has exhausted the ladder, and content (not type size) has
  to give. Only `generate_brief_report` may drop content: it truncates
  lowest-priority takeaways and prints `N further signals omitted for space`.
  Never let either PDF spill silently, and never hide a takeaway silently.
  When adding page content, add a case to `test_brief_always_fits...` /
  `test_generate_brief_report_is_always_exactly_one_page` rather than
  eyeballing a render.
- **The brief PDF (`generate_brief_report`) has a 2-column signal-card grid
  and a Training Plan section** (2026-07-09 redesign). `visuals.py`'s
  signal cards (formerly one stacked column) reflow into a flexbox grid
  robust to any takeaway count — an odd-count last card spans the full
  width (`.span-full`) rather than leaving a gap; never assume exactly 4.
  Below that, a Training Plan section (adherence %, days-to-race, this
  week's planned/actual mileage, a slip count, today's prescribed workout,
  and a last-7-days table graded done/partial/missed/rest/scheduled) is
  computed **live from `plans.py` at render time** — `tools.py`'s
  `_build_plan_section()` — keyed to the brief's own date, not
  `date.today()`; the `Brief`/`Takeaway` schema carries zero plan fields
  and never will. Note `plans.build_plan_detail()` has no "as of" date
  parameter at all — its verdicts are always graded against the real data
  frontier, never a hypothetical past perspective — so `target_date` is
  only used to pick which graded workout is "today" and slice the
  trailing window, never passed into `build_plan_detail` itself (a real
  bug caught during this build: its 4th positional arg is `best_effort`, a
  Riegel-projection dict, not a date). The section is omitted entirely
  (not shown empty) when there's no active plan or no plan data in the
  window. Today's coaching line comes from a **new `agent/plan_coach.py`
  module** — a Claude Agent SDK call (toolless, single-shot, same
  `briefing.DEFAULT_MODEL` the real daily brief uses) behind a
  single-entry disk cache since 0.23.0
  (`generate_coaching_line_cached`, keyed on the pure `build_prompt`
  output hash, stored next to the SQLite DB): identical inputs reuse the
  line without an SDK round-trip (the facet review counted 9 identical
  calls in one day of repeat renders), any input change regenerates, and
  failures are never cached. Deterministic template fallback
  (`fallback_coaching_line`) if the call fails for any reason (missing
  credential, network, timeout) — the PDF still generates either way.
  `plan_coach.py` imports `briefing` lazily, inside the function body,
  not at module scope: `briefing.py` already imports `tools.py` at module
  scope (as `agent_tools`), and `tools.py` imports `plan_coach.py` — a
  module-scope import there would close a real circular import.
- **Auth middleware**: `LOCAL_FITNESS_API_TOKEN` env var; constant-time
  bearer check; `_is_public_path` denies by default — only `/health` is
  explicitly whitelisted (2026-07-09 UI-retirement design; no SPA shell
  left that needs unauthenticated loading, so there's no reason for a
  blanket-public fallthrough). `/mcp/` and every other path require the
  bearer token whenever one is configured.
- **Rate limit**: in-memory token bucket on `RATE_LIMITED_PREFIXES`,
  loopback IPs exempt.
- **CI dep scanning**: `.github/dependabot.yml` (pip / docker /
  github-actions, weekly), `target-branch: dev` so bumps flow through the
  promotion path.
- **Branch protection**: `main` + `dev` both gated on the CI `validate`
  check + a PR, squash-only, linear history, `enforce_admins: false`
  (admin break-glass). Repo settings: auto-merge + delete-branch-on-merge
  on. See *Branching & release strategy*.

## File-layout reference

- `src/local_fitness/agent/` — Claude Agent SDK tools, prompts, briefing
  generator, chat loop.
- `src/local_fitness/ingest/` — Garmin auth, daily pull, ZIP backfill,
  baselines / CTL-ATL-TSB.
- `src/local_fitness/web/server.py` — FastAPI app + middleware stack
  (MCP transport host + `/health`; no UI to serve).
- `src/local_fitness/db.py` — SQLite schema + connection helpers.
- `tests/` — pytest. `test_security.py` is the audit-regression file.
- `docs/deployment.md` — what the deploying side wires into compose.
- `devlog/` — running notes per change.
