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

# 3. Store Garmin creds in macOS Keychain + init DB
uv run fitness setup

# 4. Pull live data (catches up since last successful run)
uv run fitness pull

# 5. Optional: backfill 3 years of history once your Garmin export ZIP arrives
#    (Garmin Connect → Account → Account Information → Export Your Data;
#     they email a ZIP within a few days)
uv run fitness backfill ~/Downloads/garmin-export.zip

# 6. Recompute baselines + training load
uv run fitness recompute-baselines

# 7. Generate today's briefing on demand
uv run fitness brief

# 8. Install the daily launchd job (runs `fitness brief` at 6:30 AM,
#    catches up on next wake if the Mac was asleep)
./ops/install-launchd.sh
```

## Usage

```bash
fitness pull                  # pull since last success
fitness brief                 # pull + recompute + briefing → briefings/YYYY-MM-DD.md
fitness brief --opus          # use Opus 4.7
fitness chat                  # interactive REPL
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
│   └── cli.py                       # `fitness` Click entry point
└── ops/
    ├── com.local-fitness.daily.plist
    └── install-launchd.sh
```

## Database

SQLite at `~/localrepo/local-fitness/data/fitness.db`.

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
