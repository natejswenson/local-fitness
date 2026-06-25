# local-fitness

A self-hosted, **agent-first** personal fitness coach. It pulls your
[Garmin Connect](https://connect.garmin.com) data into a local SQLite database
and computes rolling recovery and training-load baselines. You talk to your
"coach" from an **MCP client** (Claude Desktop, Code, or Mobile) pointed at
that data; a scheduled job composes a structured daily brief; and a localhost
web UI gives you a fast visual glance.

The web server itself runs **no Claude inference** — it serves your data and
the deterministic compute (baselines, CTL/ATL/TSB, training plans) over REST
and the [Model Context Protocol](https://modelcontextprotocol.io). All
*synthesis* — the daily brief, conversational coaching, plan drafting — happens
in the agent. Everything runs on your own machine; the only data that leaves is
the handful of metrics the agent reads when you ask it something or it writes a
brief.

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
- **Agent-first — your coach lives in an MCP client.** The fitness data and a
  set of grounded tools are exposed over the
  [Model Context Protocol](https://modelcontextprotocol.io), so you talk to your
  training from Claude Desktop, Code, or Mobile. The agent must call a tool to
  read real values before any claim — it never invents numbers. It can also
  *write*: compose a fresh daily brief, draft a training plan, log manual
  workouts and subjective notes (RPE, soreness, weight, mood…), and remember
  your preferences — all through the same tools. See
  [MCP](#mcp--talk-to-your-data-from-claude-directly).
- **A scheduled daily brief.** `fitness brief` (run on a `launchd`/cron
  schedule) composes a structured morning briefing and saves it as JSON; both
  the web UI and the MCP read it back. The composer is restricted to read-only
  tools, so an automated run can never mutate your data.
- **A localhost web UI** (React + FastAPI) — a fast visual glance, not a place
  you converse: today's brief, Trends, Dashboards, and your Training Plan. It
  reads your data over REST; the server runs no Claude inference.
- **Runner-facing units:** distances and pace render in miles / min-per-mile by
  default (`LOCAL_FITNESS_DISPLAY_UNITS`); raw metric values are always present.
- **Privacy by default:** the database, briefings, and logs stay on your
  machine and are gitignored.

## How it works

```
Garmin Connect ──pull──> SQLite (data/fitness.db) ──> baselines / CTL-ATL-TSB
                                                          │
                            deterministic tool layer ─────┤
                                                          │
        ┌──────────────────┬──────────────────────────────┴───────────────┐
     REST API           MCP server                         scheduled `fitness brief`
   (web UI: a          (Claude Desktop/Code/Mobile:         (composes the brief →
    visual glance)      coach + brief prompts, tools,        briefings/*.json)
                        resources, write surface)
```

The web-server process runs **no Claude inference** — it serves your data and
the deterministic compute (baselines, CTL/ATL/TSB, plan grading) over REST and
MCP. All synthesis — the daily brief, conversational coaching, plan drafting —
happens in a client agent. The same grounded tool layer backs the REST API, the
MCP server, and the scheduled brief composer, so there's one source of truth and
no duplication. Over MCP, write tools are available to interactive clients, but
the brief composer is restricted to read-only tools, so an automated briefing
can never mutate your data.

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
fitness brief                 # pull + recompute + briefing → briefings/YYYY-MM-DD.json
fitness brief --opus          # use the larger Opus model for one run
fitness serve                 # web UI at http://127.0.0.1:8765
fitness mcp-stdio             # serve the tools to an MCP client over stdio
fitness status                # DB row counts + last ingest run info
```

The brief composer defaults to a fast Sonnet model and switches to Opus on
demand (`--opus`). One daily briefing is a rounding error against a Claude
subscription. Conversational coaching now happens in your MCP client (see
[MCP](#mcp--talk-to-your-data-from-claude-directly)), not a built-in REPL — the
`chat`/`ask` commands were retired in the agent-first migration.

## Web UI

`fitness serve` starts a localhost-only FastAPI server (default port 8765)
that exposes your data and the deterministic compute over REST, mounts the MCP
endpoint at `/mcp/`, and serves the built React frontend. It runs **no Claude
inference** — it's a viewer, not a conversation surface. Four views:

- **Today** — the agent-written morning brief (key takeaways with embedded
  charts), a year-at-a-glance activity heatmap, today's planned session (from
  your active training plan), and a recent-workouts table. A banner nudges you
  to ask your coach for a fresh brief when newer data has landed.
- **Trends** — an interactive Banister CTL/ATL/TSB chart and a metric picker
  (RHR, sleep, Body Battery, stress, VO₂ max) with baseline overlays and a
  date-range toggle.
- **Dashboards** — activity heatmap, pace-efficiency / fatigue, and
  strength-volume views with range toggles.
- **Training Plan** — your active plan's schedule, weekly mileage, and CTL
  trajectory; review and commit (or delete) a draft your coach drafted over MCP.

The conversational coaching that used to live in an in-app chat now happens in
your MCP client. Each "ask your coach" affordance copies a ready-to-paste prompt
to the clipboard. Dev mode: `cd web && pnpm dev` runs Vite at `:5173` with the
API proxied to `fitness serve` at `:8765`.

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
web UI prompts for the same value and remembers it in `localStorage`. The same
token gates the MCP endpoint at `/mcp/`. Because the web server runs no Claude
inference, there are no cost-sensitive API endpoints to drain a subscription; an
in-memory per-IP rate limiter stays wired (currently a no-op) so any future
Claude-cost path can be capped in one line.

## MCP — talk to your data from Claude directly

The MCP is the **primary** way to interact with your coach. The fitness tools,
prompts, and resources are exposed over the [Model Context
Protocol](https://modelcontextprotocol.io), so any MCP client (Claude Desktop,
Code, or Mobile) can read *and* write your data — without the web UI. The same
tool layer backs everything, so the MCP surface auto-tracks the rest of the app
— no duplication.

**Connect (local, no token):**

```bash
claude mcp add --transport stdio fitness -- uv run fitness mcp-stdio
```

**Connect (over the running server, token-gated):** the server also mounts the
MCP endpoint at `/mcp/` behind the same `LOCAL_FITNESS_API_TOKEN` bearer gate.

```bash
claude mcp add --transport http fitness \
  https://<your-host>/mcp/ --header "Authorization: Bearer $TOKEN"
```

Once connected you get **25 tools**, **2 prompts**, and **2 resources**:

- **Prompts**
  - **`coach`** — assembles your full daily snapshot (metrics vs. baseline,
    training load, recent workouts in miles) *and* the coach persona + your
    saved preferences in one round-trip, then stays conversational for
    follow-ups. This is the everyday "talk to my coach" entry point.
  - **`brief`** — composes a fresh structured daily brief from the same snapshot
    and persists it via `save_brief`, so your next web-UI glance is up to date.
- **Read tools** — `daily_snapshot` (one-call status), `get_today_status`,
  `get_metric` / `get_metric_trend`, `query_workouts` / `get_workout_detail`,
  `compare_periods`, `correlate`, `find_anomalies`, `recovery_pattern`,
  `training_load_status`, and `run_sql` (**read-only**, enforced at the SQLite
  engine — any write/DDL fails regardless of phrasing).
- **Write tools** — `save_brief` (write today's brief), the training-plan tools
  `propose_training_plan` / `revise_training_plan` / `get_training_plan_status`
  (the agent only drafts; committing or deleting a plan is a human action in the
  UI), `log_observation` / `list_observations` / `delete_observation` (RPE,
  soreness, weight, mood, feeling, injury, notes), `log_manual_workout` /
  `delete_manual_workout` (non-Garmin sessions that feed the training-load
  model), and the user-notes tools `save_user_note` / `list_user_notes` /
  `update_user_note` / `delete_user_note` (durable coaching preferences).
- **Resources** — `fitness://schema` (queryable columns + read-only SQL guide)
  and `fitness://brief/latest` (your most recent brief as Markdown).

The DNS-rebinding guard on the HTTP transport requires the served host to be in
`LOCAL_FITNESS_MCP_ALLOWED_HOSTS` (defaults to common local hosts; set it to
your host or every request 421s).

> **Scheduled brief credentials.** The scheduled `fitness brief` composer is the
> one place the *server side* talks to Claude (a separate process from the web
> server). It authenticates with your Claude subscription via
> `CLAUDE_CODE_OAUTH_TOKEN` — put it in `.env` (the CLI auto-loads it, including
> under `launchd`). The web server and the MCP endpoint don't need it. See
> [`ops/`](ops/) and [`docs/deployment.md`](docs/deployment.md).

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
| `LOCAL_FITNESS_API_TOKEN` | Bearer token gating `/api/*` and `/mcp/` (required for non-loopback binds) | unset |
| `LOCAL_FITNESS_MCP_ALLOWED_HOSTS` | Host allowlist for the MCP transport's DNS-rebinding guard | common local hosts |
| `LOCAL_FITNESS_DISPLAY_UNITS` | Runner-facing display units; non-`miles` suppresses the `*_mi` convenience fields (raw values always present) | `miles` |

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
`activity_hr_zones`, `activity_splits`, `baselines`, `ingest_runs`, `settings`,
`observations` (manual logs: RPE, soreness, weight, mood…), and
`training_plans` / `plan_workouts` (goal-driven plans, single-active enforced by
a partial unique index). Raw Garmin JSON is preserved on every wellness/activity
row, so new fields can be derived later without re-pulling.

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
├── db.py                  # SQLite schema + connection helpers (read-only mode for run_sql)
├── ingest/                # Garmin auth, daily pull, backfill, baselines
├── agent/                 # tools, prompts, brief composer (briefing.py) + brief I/O (briefs.py)
├── web/server.py          # FastAPI app: REST + MCP mount + SPA (no Claude inference)
├── web/mcp_server.py      # MCP prompts/tools/resources wiring
└── cli.py                 # `fitness` Click entry point
web/                       # Vite + React + TS + Tailwind frontend (a viewer)
ops/                       # macOS launchd plist + installer for the scheduled brief
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

Work flows `feature/* → dev → main`; both `dev` and `main` are protected
(CI green + a PR required). CI runs all three checks on every push/PR to
`main`/`dev`; a `dev → main` promotion that bumps the version in
`pyproject.toml` auto-cuts a GitHub Release for it. See
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
