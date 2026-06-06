# local-fitness

A self-hosted personal fitness coach powered by Claude. It pulls your
[Garmin Connect](https://connect.garmin.com) data into a local SQLite database,
computes rolling recovery and training-load baselines, and uses a Claude agent
to write you a daily morning briefing — or to answer questions about your
training in a chat.

Everything runs on your own machine. Your health data never goes to a
third-party service; the only thing that leaves is the handful of metrics the
agent sends to Anthropic when it writes a briefing or answers a question.

[![CI](https://github.com/natejswenson/local-fitness-dude/actions/workflows/ci.yml/badge.svg)](https://github.com/natejswenson/local-fitness-dude/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/natejswenson/local-fitness-dude)](https://github.com/natejswenson/local-fitness-dude/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)

> **Scope.** This is a single-user, self-hosted app — one person's Garmin
> account, one local database. It is not a multi-tenant service. It was built
> and is run daily on macOS; Linux/Docker is supported for the server, and the
> ingest works anywhere you can supply Garmin credentials (see
> [Cross-platform & Docker](#cross-platform--docker)).

## Features

- **Daily auto-pull** from Garmin Connect via the unofficial
  [`garminconnect`](https://github.com/cyberjunky/python-garminconnect) library
  — catches up automatically if your machine was off for days.
- **One-time historical backfill** from Garmin's official "Export Your Data"
  ZIP, so the agent reasons over your full history, not just recent days.
- **Pre-computed baselines:** 60-day rolling mean/SD for resting HR, sleep,
  Body Battery, and stress, plus the Banister **CTL/ATL/TSB** training-load
  model (fitness / fatigue / form).
- **A local Claude agent** with 15 read-only tools for querying the database.
  It writes a structured daily briefing and supports an interactive chat that
  shows which data it pulled.
- **A localhost web UI** (React + FastAPI) with Today, Trends, and Chat views.
- **Privacy by default:** the database, briefings, and logs stay on your
  machine and are gitignored.

## How it works

```
Garmin Connect ──pull──> SQLite (data/fitness.db) ──> baselines / CTL-ATL-TSB
                                                          │
                                  Claude agent (15 tools) ┘
                                          │
                          ┌───────────────┼────────────────┐
                       daily brief      chat            web UI
                    (briefings/*.md)   (REPL/web)   (Today/Trends/Chat)
```

The agent is grounded: it must call a tool to read real values before making
any claim about your data — it never invents numbers.

### A note on devices

Works with any Garmin device that syncs to Garmin Connect. It was built and
tested on a **Garmin Instinct Solar**, which does not report overnight HRV
Status, so recovery analysis is built around Body Battery, resting HR, sleep,
stress, and per-workout Training Effect / training load rather than HRV.
Devices that *do* report HRV still work — the agent just doesn't use the HRV
signal yet.

## Requirements

- **Python 3.12+** and [`uv`](https://docs.astral.sh/uv/)
- A **Garmin Connect** account with some history
- **Claude access via [Claude Code](https://claude.com/claude-code)** — the
  `claude-agent-sdk` uses your existing Claude subscription; no separate API
  key is required
- **Node.js + [pnpm](https://pnpm.io/)** — only if you want the web UI
- **macOS** is the primary platform (the bundled scheduler uses `launchd`, and
  credentials default to the system Keychain). Linux/other works too — see
  [Cross-platform & Docker](#cross-platform--docker).

## Quick start

```bash
# 1. Clone and install deps + the `fitness` command
git clone https://github.com/natejswenson/local-fitness-dude.git
cd local-fitness-dude
uv sync

# 2. (Optional) configuration overrides — defaults work out of the box
cp .env.example .env   # edit only if you need to (see Configuration)

# 3. Store Garmin credentials (macOS Keychain) and initialize the database
uv run fitness setup

# 4. Pull live data (catches up since the last successful run)
uv run fitness pull

# 5. (Optional) backfill your full history once the export ZIP arrives.
#    Garmin Connect → Account Settings → Export Your Data; they email a
#    ZIP within a few days.
uv run fitness backfill ~/Downloads/garmin-export.zip

# 6. Recompute baselines + training load
uv run fitness recompute-baselines

# 7. Generate today's briefing
uv run fitness brief

# 8. (macOS, optional) install the daily 6:30 AM job
./ops/install-launchd.sh

# 9. (Optional) build + serve the web UI
cd web && pnpm install && pnpm build && cd ..
uv run fitness serve --open   # http://127.0.0.1:8765
```

## Usage

```bash
fitness pull                  # pull since last success
fitness brief                 # pull + recompute + briefing → briefings/YYYY-MM-DD.md
fitness brief --opus          # use the larger Opus model for one run
fitness chat                  # interactive REPL
fitness serve                 # web UI at http://127.0.0.1:8765
fitness ask "should I run hard today?"
fitness ask "compare last 30 days vs prior 30 for RHR" --opus
fitness status                # DB row counts + last ingest run info
```

The agent defaults to a fast Sonnet model and switches to Opus on demand
(`--opus`). One daily briefing is a rounding error against a Claude
subscription; long chat sessions use more.

## Web UI

`fitness serve` starts a localhost-only FastAPI server (default port 8765)
that exposes the database and agent over REST + NDJSON-streamed chat, and
serves the built React frontend. Three views:

- **Today** — the auto-generated morning brief at top, then stat cards for
  Body Battery, RHR, sleep, and form (TSB) with sparklines and 60-day baseline
  deltas, plus a recent-workouts table.
- **Trends** — an interactive Banister CTL/ATL/TSB chart and a metric picker
  (RHR, sleep, Body Battery, stress, VO₂ max) with baseline overlays and a
  date-range toggle.
- **Chat** — a streaming conversation with the agent; tool calls render as
  inline pills so you can see what data it's pulling.

Dev mode: `cd web && pnpm dev` runs Vite at `:5173` with the API proxied to
`fitness serve` at `:8765`.

### Authentication

The server gates every `/api/*` endpoint with a bearer token via the
`LOCAL_FITNESS_API_TOKEN` env var. When bound to a non-loopback host (a
container, anything other than `127.0.0.1`/`localhost`) the token is
**required** — the server refuses to start without it. Loopback binds work
without a token for local dev convenience.

```bash
# generate one
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Put it in `.env` as `LOCAL_FITNESS_API_TOKEN=…`. On first load per device the
web UI prompts for the same value and remembers it in `localStorage`.
Cost-sensitive endpoints (`/api/chat`, `/api/brief/generate*`) are also
rate-limited per IP as defense-in-depth against subscription drain.

## Configuration

Every variable is optional; defaults are project-relative and work in a fresh
clone. Copy `.env.example` to `.env` and set only what you need.

| Variable | Purpose | Default |
|---|---|---|
| `GARMIN_EMAIL` / `GARMIN_PASSWORD` | Garmin credentials via env (required where the system Keychain isn't reachable, e.g. containers). When both are set they win over the Keychain. | unset → use Keychain (`fitness setup`) |
| `LOCAL_FITNESS_DATA_DIR` | Where the SQLite DB + notes live | `./data` |
| `LOCAL_FITNESS_BRIEFINGS_DIR` | Where daily briefings are written | `./briefings` |
| `LOCAL_FITNESS_NOTES_PATH` | The agent's durable user-notes file | `./data/user_notes.md` |
| `LOCAL_FITNESS_HOST` | Bind host for `fitness serve` | `127.0.0.1` |
| `LOCAL_FITNESS_API_TOKEN` | Bearer token gating `/api/*` (required for non-loopback binds) | unset |

## Cross-platform & Docker

The ingest and agent run anywhere Python does. On non-macOS hosts (or in
containers), supply Garmin credentials via `GARMIN_EMAIL` / `GARMIN_PASSWORD`
instead of `fitness setup`, point `LOCAL_FITNESS_DATA_DIR` at a persistent
volume, and schedule `fitness brief` with cron/systemd (the `launchd` job is
macOS-only).

For running the web server in a container behind a reverse proxy, see
[`docs/deployment.md`](docs/deployment.md) — it covers the compose service
block, required env vars, and the token-rotation flow.

## Database

SQLite at `./data/fitness.db` (override with `LOCAL_FITNESS_DATA_DIR`). Tables:
`daily_metrics`, `body_battery_samples`, `stress_samples`, `activities`,
`activity_hr_zones`, `activity_splits`, `baselines`, `ingest_runs`, `settings`.
Raw Garmin JSON is preserved on every wellness/activity row, so new fields can
be derived later without re-pulling.

## Privacy & data

- The database, briefings, and logs live on your machine and are gitignored —
  none of your data is committed to the repo.
- The app talks to exactly two external services: **Garmin Connect**, to fetch
  *your own* data, and **Anthropic**, to run the Claude agent. When the agent
  writes a briefing or answers a question, the specific metrics it queries are
  sent to Anthropic as part of the prompt.
- There is no analytics, telemetry, or third-party tracking.

## Project layout

```
src/local_fitness/
├── db.py                  # SQLite schema + connection helpers
├── ingest/                # Garmin auth, daily pull, backfill, baselines
├── agent/                 # Claude tools, prompts, briefing generator, chat
├── web/server.py          # FastAPI app: REST + NDJSON-streamed chat + SPA
└── cli.py                 # `fitness` Click entry point
web/                       # Vite + React + TS + Tailwind frontend
ops/                       # macOS launchd plist + installer
scripts/score_prompt.py    # eval that scores agent/prompts.py (gates CI)
tests/                     # pytest suite (run: uv run pytest)
docs/deployment.md         # container / reverse-proxy deployment
```

## Development

```bash
uv run pytest                       # tests + coverage gate
uv run ruff check .                 # lint
uv run python scripts/score_prompt.py  # score the agent prompt
```

CI runs all three on every push/PR to `master`; a green build on `master`
auto-cuts a GitHub Release for the version in `pyproject.toml`. See
[`CHANGELOG.md`](CHANGELOG.md).

## Contributing

Issues and pull requests are welcome. This is a personal project shared
publicly, so the bar is "does it keep working for a stranger's clone": anything
host-specific (paths, secrets, ports) goes through an env var with a
project-relative default — never hardcode `/Users/...` or real credentials. New
`/api/*` endpoints are auth-gated by default. Run the checks above before
opening a PR.

## Caveats

- `garminconnect` is reverse-engineered. Garmin occasionally changes their
  site and the library breaks for a few days until the community patches it.
  When that happens, `fitness pull` logs an auth error and the next briefing
  flags it.
- Claude subscription auth shares the same rate-limit pool as the rest of your
  Claude Code usage. A daily briefing is negligible; heavy chat sessions can
  compete with it.
- No overnight HRV on the Instinct Solar (a 2022-and-newer Garmin feature), so
  recovery analysis leans on Body Battery + RHR + sleep + training load.

## License

[MIT](LICENSE) © 2026 Nate Swenson
