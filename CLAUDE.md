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
  below), and `ruff`, the prompt scorer. A separate `docker-build` job
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
  a further perf improvement, not routinely.
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
  everything — `get_training_plan_progress` (full graded plan day-by-day),
  `get_training_plan_status`, `query_workouts`, `get_metric_trend`,
  `daily_snapshot`, `training_load_status`, etc. Reach for `run_sql` only when
  no structured tool fits. **Never shell out to `sqlite3`/Bash for a DB read** —
  the agent did exactly that once and it dumped `PRAGMA` introspection and SQL
  errors at the user. One tool call when a tool exists.
- **The agent owns the entire plan lifecycle — there is no UI.** When the user
  wants to change their plan (move a long run, swap days, adjust a session),
  edit it with `update_plan_workout(date, type/distance_mi/pace_min_per_mi/description)`
  — it re-prescribes one day on the *active* plan (`type='rest'` clears
  distance/pace). Structure changes (whole new plan) go through
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
- **Daily brief job needs a Claude credential in `.env`.** The 06:30
  launchd job (`com.localfitness.brief` → `fitness brief`) couples pull →
  recompute-baselines → generate → save atomically. Its *generate* step
  spawns a **headless** Claude via the Agent SDK, which authenticates from
  the process env only — `cli.py` `load_dotenv()`s `<repo>/.env`, so the
  token must live there as `CLAUDE_CODE_OAUTH_TOKEN` (Nate's Max token,
  minted with `claude setup-token`, no per-brief cost, expires ~yearly) or
  `ANTHROPIC_API_KEY` (never expires, bills per brief). A live Claude
  session (like this chat) can compose + `save_brief` fine because it
  already holds a token — but that's **not** the unattended path.
  **Failure signature** when the token is missing/expired: pull succeeds
  (`Pull: success` in `logs/brief.launchd.out.log`) but generation returns
  empty (`chars=0 takeaways_yielded=0`, "no JSON found in agent response"
  in `logs/brief.launchd.err.log`), so **no brief saves** (orphaned sync —
  pull ran, brief didn't) — surfaces as `get_brief_context`'s
  `data_through_date` outrunning the brief's own `date` on the next MCP
  query. Fix = put/refresh the token in `.env` (gitignored); re-mint on
  expiry.
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
  the seeding shell must share the same `HOME`.
- **Path defaults**: `db.py`, `notes.py`, `briefing.py` all resolve to
  `_PROJECT_ROOT / ...` when env vars are unset.
- **MCP tool surface can trigger a Garmin sync, not just read.** `agent/tools.py`'s
  `sync_garmin_data` (in `ALL_TOOLS`, not in the brief loop's read-only
  allow-list) wraps `ingest.daily.pull(max_days=SYNC_MAX_DAYS)` +
  `ingest.baselines.recompute()` — a bite-sized, gap-aware pull, capped so a
  long absence doesn't turn one tool call into a multi-minute Garmin backfill.
  Any MCP client wired to `fitness mcp-stdio` (Claude Desktop, opencode, etc.)
  can call it directly; before this tool existed, MCP-only clients had read
  access to the DB but no way to freshen it — only the CLI (`fitness pull`)
  could. `run_stdio()` in `web/mcp_server.py` serves `ALL_TOOLS`
  as-is, so a new tool here needs no separate wiring to reach `mcp-stdio`.
- **`generate_brief_report`/`generate_chart` are stdio-only, never `ALL_TOOLS`.**
  These two MCP tools (`agent/tools.py`'s `LOCAL_ONLY_TOOLS`) render a saved
  daily brief into a polished PDF (`agent/visuals.py`'s WeasyPrint pipeline,
  reusing the `budget` project's validated color theme) and render a
  standalone matplotlib PNG chart on demand. Since the 2026-07-09 UX pass,
  both write to and auto-open from an **ephemeral per-process tmp
  directory by default** (`tempfile.mkdtemp`, PID-embedded naming, cleaned
  up via `atexit` when the `fitness mcp-stdio` process exits — a fresh
  subprocess per opencode/Claude session, so this is the natural
  session-cleanup boundary) rather than the old persistent `./reports/`.
  A best-effort liveness-checked sweep also reaps any prior session's
  leaked directory (PID dead + >24h old) on each process's first call —
  `atexit` doesn't fire on `SIGKILL`/`SIGTERM`, so an abrupt kill can still
  leak until the sweep claims it; this is an accepted residual risk for a
  personal, single-user tool, not something hardened further. After a
  successful write, the file is auto-opened via macOS `open` (best-effort,
  never fails the tool call; logs a warning and no-ops on other platforms).
  Set `LOCAL_FITNESS_REPORTS_DIR` to opt back into a persistent directory
  (still auto-opened, no auto-cleanup) — see `.env.example`. If you have an
  existing populated `./reports/` from before this change, it's vestigial
  now and safe to delete by hand. They're registered ONLY via `run_stdio()`'s
  `build_server(extra_tools=agent_tools.LOCAL_ONLY_TOOLS)` call — structurally,
  not just by convention, unreachable over the authenticated streamable-HTTP
  `/mcp/` transport (`build_session_manager()` calls `build_server()`
  argument-free), since a phone-triggered call over that transport would get
  back a container-internal path with no way to retrieve the file. WeasyPrint
  needs native Pango/HarfBuzz libs — `apt-get` on Linux/CI, but on macOS
  Homebrew's install isn't on the default dylib search path and needs
  `DYLD_LIBRARY_PATH=$(brew --prefix)/lib` (see `.env.example`).
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
  `briefing.DEFAULT_MODEL` the real daily brief uses) called fresh on
  every render, with a deterministic template fallback
  (`fallback_coaching_line`) if that call fails for any reason (missing
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
