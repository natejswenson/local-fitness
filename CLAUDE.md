# local-fitness — instructions for Claude

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
- **Test before claiming done.** `uv run pytest -x` for Python, `pnpm
  build` + `pnpm tsc --noEmit` for the frontend, `docker compose up
  -d --build local-fitness` for the container path. For UI, take a
  screenshot — never claim something looks better without the PNG.
- **Rebuild the container after every change.** It's the live
  deployment at `https://fitness.home.local`; stale containers serve
  stale code. This is durable: rebuild even when you "only" changed
  the frontend (the SPA gets baked into stage 1).
- **Devlog the change.** Each meaningful PR gets a `devlog/` entry —
  manual prefix today, `/devlog` skill (auto from git commits) going
  forward.
- **Commit messages explain why.** Short subject, body when motivation
  isn't obvious from the diff. Co-authored-by line stays.

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
  github-actions, weekly).

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
