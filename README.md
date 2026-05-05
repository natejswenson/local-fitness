# local-fitness

Local Garmin-data agent. Three-year history sits in a SQLite DB, a daily
launchd job pulls overnight, and a Claude-powered agent writes you a morning
briefing — or chats about your training when you ask.

Built around a Garmin Instinct Solar (no overnight HRV Status — uses Body
Battery, RHR, sleep, stress, and per-workout Training Effect / training load).

## What it does

- **One-time historical backfill** from Garmin Connect's "Request your data" ZIP.
- **Daily auto-pull** via the unofficial `garminconnect` library (catches up if
  the laptop was closed for days).
- **Pre-computed baselines:** 60-day rolling mean/SD for RHR, sleep, body
  battery, stress, plus the Banister CTL/ATL/TSB training-load model.
- **Local Claude agent** (Sonnet 4.6 default, Opus 4.7 on demand) with 11 tools
  for querying the DB. Writes a daily morning briefing and supports an
  interactive REPL.
- Auth via your existing Claude Code subscription — no API key needed.

## Setup

```bash
# 1. Make sure Claude Code is logged in with your subscription
claude  # then /login if not already authenticated

# 2. Install deps and the `fitness` command
uv sync

# 3. (Optional) copy .env.example → .env and edit if you need to override
#    paths or supply Garmin creds via env (vs. macOS Keychain)
cp .env.example .env

# 4. Store Garmin creds in macOS Keychain + init DB
uv run fitness setup

# 5. Pull live data (catches up since last successful run)
uv run fitness pull

# 6. Optional: backfill 3 years of history once your Garmin export ZIP arrives
#    (Garmin Connect → Account → Account Information → Export Your Data;
#     they email a ZIP within a few days)
uv run fitness backfill ~/Downloads/garmin-export.zip

# 7. Recompute baselines + training load
uv run fitness recompute-baselines

# 8. Generate today's briefing on demand
uv run fitness brief

# 9. Install the daily launchd job (runs `fitness brief` at 6:30 AM,
#    catches up on next wake if the Mac was asleep)
./ops/install-launchd.sh

# 10. Build + serve the web UI (one-time build; `fitness serve` reads the dist/)
cd web && pnpm install && pnpm build && cd ..
uv run fitness serve --open  # opens http://127.0.0.1:8765 in your browser
```

## Usage

```bash
fitness pull                  # pull since last success
fitness brief                 # pull + recompute + briefing → briefings/YYYY-MM-DD.md
fitness brief --opus          # use Opus 4.7
fitness chat                  # interactive REPL
fitness serve                 # web UI at http://127.0.0.1:8765
fitness ask "should I run hard today?"
fitness ask "compare last 30 days vs prior 30 days for RHR" --opus
fitness status                # DB row counts and last ingest run info
```

## Project layout

```
local-fitness/
├── pyproject.toml
├── data/fitness.db                  # SQLite — gitignored
├── briefings/                       # daily markdown notes — gitignored
├── logs/                            # ingest + launchd logs — gitignored
├── src/local_fitness/
│   ├── db.py                        # schema + connection
│   ├── ingest/
│   │   ├── auth.py                  # Keychain helpers
│   │   ├── daily.py                 # garminconnect daily pull
│   │   ├── backfill.py              # historical export ZIP parser
│   │   └── baselines.py             # rolling stats + CTL/ATL/TSB
│   ├── agent/
│   │   ├── tools.py                 # 11 SDK tools (queries)
│   │   ├── prompts.py               # system prompt + grounding rules
│   │   ├── briefing.py              # daily briefing generator
│   │   └── chat.py                  # REPL + one-shot ask
│   ├── web/
│   │   └── server.py                # FastAPI app: REST + NDJSON-stream chat
│   └── cli.py                       # `fitness` Click entry point
├── web/                             # Vite + React + TS + Tailwind frontend
│   ├── src/
│   │   ├── App.tsx                  # layout + routes
│   │   ├── components/
│   │   │   ├── Chat.tsx             # streaming agent conversation
│   │   │   ├── Today.tsx            # brief + stat cards + recent workouts
│   │   │   ├── Trends.tsx           # CTL/ATL/TSB + multi-metric charts
│   │   │   ├── Sidebar.tsx
│   │   │   ├── StatCard.tsx
│   │   │   └── Card.tsx
│   │   ├── lib/{api,types,utils}.ts
│   │   └── index.css                # Tailwind v4 + theme tokens
│   └── dist/                        # built bundle — gitignored
└── ops/
    ├── com.local-fitness.daily.plist
    └── install-launchd.sh
```

## Web UI

`fitness serve` starts a localhost-only FastAPI server (default port 8765)
that exposes the DB + agent over REST and NDJSON-streamed chat, plus serves
the built React frontend.

Three views:
- **Chat** (default) — streaming conversation with the agent. Tool calls
  shown as inline pills so you see what data it's pulling. Sonnet/Opus toggle.
- **Today** — auto-generated morning brief at top, then stat cards for body
  battery, RHR, sleep, and form (TSB) with sparklines and 60-day baseline
  deltas. Recent workouts table at bottom.
- **Trends** — interactive Banister CTL/ATL/TSB area chart plus a metric
  picker (RHR, sleep, body battery, stress, VO₂ max) with 60-day baseline
  overlay where applicable. Date-range toggle (30d → all).

Dev mode: `cd web && pnpm dev` runs Vite at :5173 with API proxied to
`fitness serve` at :8765.

### Auth

The server gates `/api/*` with a bearer token via the
`LOCAL_FITNESS_API_TOKEN` env var. When binding to a non-loopback host
(container behind Traefik, anything other than `127.0.0.1`/`localhost`)
the token is **required** — the server refuses to start without it.
Loopback bind without a token still works for host-CLI dev.

Generate one:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Paste it into `.env` as `LOCAL_FITNESS_API_TOKEN=…`. On first load of the
web UI per device, a token-entry screen prompts for the same value;
the browser remembers it via `localStorage`. Cost-sensitive endpoints
(`/api/chat`, `/api/brief/generate*`) are also rate-limited per-IP at 20
requests / minute as defense-in-depth against subscription drain.

For container deployment behind a reverse proxy (e.g. Traefik), see
[`docs/deployment.md`](docs/deployment.md) — covers the compose
service block, required env vars, and the token-rotation flow.

## Database

SQLite at `./data/fitness.db` (project-relative; override with `LOCAL_FITNESS_DATA_DIR`).

Tables: `daily_metrics`, `body_battery_samples`, `stress_samples`,
`activities`, `activity_hr_zones`, `activity_splits`, `baselines`,
`ingest_runs`. Raw Garmin JSON is preserved on every wellness/activity row so
new fields can be derived later without re-pulling.

## Honest caveats

- `garminconnect` is reverse-engineered. Garmin changes their site occasionally
  and the library breaks for a few days until the community patches it. When
  that happens, `fitness pull` logs an auth error and the next day's briefing
  flags it.
- Subscription auth shares the same Claude Code rate-limit pool. One daily
  briefing is rounding error; heavy `fitness chat` sessions can compete with
  your Claude Code usage.
- Instinct Solar lacks overnight HRV Status (a 2022-and-newer Garmin feature),
  so recovery analysis leans on Body Battery + RHR + sleep + training load
  rather than HRV.
