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

- **Every new `/api/*` endpoint is auth-gated by default.** The bearer
  middleware in `web/server.py` covers anything under `/api/`. If you
  add a new endpoint that genuinely should be public (like `/health`),
  whitelist it explicitly in `_is_public_path()`, not by sneaking it
  outside the prefix.
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
- **Test before claiming done.** `uv run pytest -x` for Python, `pnpm
  build` + `pnpm tsc --noEmit` for the frontend, `docker compose up
  -d --build local-fitness` for the container path. For UI, take a
  screenshot — never claim something looks better without the PNG.
- **The live deployment tracks `dev`, not `main`.** Nate's daily-use app at
  `https://fitness.home.local` (and the host `uv run fitness ...`) runs from the
  `dev` working branch — that's where all tested work lands. `main` is the
  public-consumption snapshot only (see *Branching & release strategy*). So the
  default loop is: land work on `dev`, then rebuild the container **from a `dev`
  checkout** so the live app is current. Do **not** promote to `main` or cut a
  release as part of normal work — that happens only when Nate explicitly asks.
- **Rebuild the container after every change.** Stale containers serve stale
  code. This is durable: rebuild even when you "only" changed the frontend (the
  SPA gets baked into stage 1). Check out `dev` first, then `docker compose up
  -d --build local-fitness` from `/Users/natejswenson/localrepo/traefik` (compose
  builds from the `../local-fitness` working tree, so the checked-out branch is
  what ships to the container).
- **What CI does and does NOT cover.** The `validate` job runs `pytest`
  (85% coverage gate), `ruff`, the prompt scorer, and `pnpm build`
  (`tsc -b && vite build`) for the frontend. It does **NOT** run
  `docker build`. So a green CI proves the Python suite + the frontend
  build/type-check pass — but a `node`/base-image bump or `Dockerfile`
  change can still pass CI and break `docker compose up --build` (this bit
  us once: `node:26` dropped bundled `corepack`). Always rebuild the
  container yourself after touching the `Dockerfile`, base images, or web
  deps. There are no frontend unit tests yet — CI type-checks and builds
  the SPA but does not test it.
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
- **The agent owns plan writes; the web UI is view-only.** When the user wants
  to change their plan (move a long run, swap days, adjust a session), edit it
  with `update_plan_workout(date, type/distance_mi/pace_min_per_mi/description)`
  — it re-prescribes one day on the *active* plan (`type='rest'` clears
  distance/pace). Do **not** route them through the draft→commit-in-UI flow; the
  UI is for visual display. Structure changes (whole new plan) still go through
  `propose_training_plan`/`revise_training_plan` (drafts). The write boundary is
  enforced in `plans.py` (`update_active_workout` whitelists prescription columns
  only — it can't re-key/re-status/restructure). Don't hand-write `UPDATE` SQL —
  the tool exists.
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

- **Path defaults**: `db.py`, `notes.py`, `briefing.py`, `web/server.py`
  all resolve to `_PROJECT_ROOT / ...` when env vars are unset.
- **Auth middleware**: `LOCAL_FITNESS_API_TOKEN` env var; constant-time
  bearer check; `/health` and `/{full_path:path}` (SPA shell) are public.
- **Rate limit**: in-memory token bucket on `RATE_LIMITED_PREFIXES`,
  loopback IPs exempt.
- **Frontend auth**: `web/src/lib/api.ts` `authedFetch` adds Bearer
  from `localStorage`; `AuthGate` wraps the route tree and re-prompts
  on 401 mid-session.
- **CI dep scanning**: `.github/dependabot.yml` (pip / npm / docker /
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
- `src/local_fitness/web/server.py` — FastAPI app + middleware stack.
- `src/local_fitness/db.py` — SQLite schema + connection helpers.
- `web/src/` — Vite + React + TS + Tailwind frontend.
- `tests/` — pytest. `test_security.py` is the audit-regression file.
- `docs/deployment.md` — what the deploying side wires into compose.
- `devlog/` — running notes per change.
