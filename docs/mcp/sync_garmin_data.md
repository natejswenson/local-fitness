# `sync_garmin_data`

> Triggers a real network pull from Garmin Connect and writes to the DB, then recomputes baselines and training load. **Availability:** stdio + HTTP

## What it does

This is a **write tool with a network side effect** — the only one on the MCP
surface that reaches outside the database. It calls
`ingest.daily.pull(max_days=SYNC_MAX_DAYS)`, which logs an `ingest_runs` row,
logs into Garmin Connect (resuming a cached session token), fetches wellness +
activity data for the target dates, and `INSERT OR REPLACE`s into
`daily_metrics`, `body_battery_samples`, `stress_samples`, `activities`,
`activity_hr_zones`, and `activity_splits`. Whenever the pull landed anything at
all — `days_pulled > 0` **or** `activities_loaded > 0`, regardless of status — it
then runs `ingest.baselines.recompute(lookback_days=90)`, rewriting 90 days of
rolling baselines and Banister CTL/ATL/TSB.

The pull is **gap-aware**: it targets the union of every missing date since
`EARLIEST_BACKFILL_DATE` (2020-09-01, when Nate's Instinct Solar launched) and the
last `FRESHNESS_WINDOW_DAYS` (3) days, sorted most-recent-first so today gets
filled before 2023 does. The always-refresh window exists because Garmin's daily
totals finalize at day-end — without it, a 5pm sync's partial step count would
stay in the DB forever, since gap-aware targeting skips any date that already has
a row.

It exists because MCP-only clients (Claude Desktop, opencode, the phone via
`/mcp/`) previously had read access to the DB but **no way to freshen it** — only
the CLI could pull. Every other tool reads what's already stored.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| — | — | — | — | Takes no arguments. Schema is `{}`. Lookback, freshness window, and cap are all fixed in code. |

## Returns

The `daily.pull()` summary dict, plus a `sync_state` line the tool adds:

```json
{
  "days_pulled": 4,
  "activities_loaded": 2,
  "status": "success",
  "last_date": "2026-07-21",
  "error": null,
  "gap_days_remaining": 0,
  "deferred_count": 0,
  "days_failed": 0,
  "sync_state": "success — synced 4 day(s), 2 activity row(s) through 2026-07-21; baselines recomputed"
}
```

| Field | Meaning |
|---|---|
| `days_pulled` | Dates whose wellness ingest succeeded this run |
| `activities_loaded` | Activity rows written across the pulled date range |
| `status` | `success` · `skipped` · `partial` · `auth_failure` · `not_configured` · `failure` · `interrupted` |
| `last_date` | Most recent date successfully ingested, ISO, or `null` |
| `error` | Human-readable failure/partial reason, or `null` |
| `gap_days_remaining` | Dates still missing between 2020-09-01 and today, **after** this run |
| `deferred_count` | Target dates dropped by the `SYNC_MAX_DAYS` cap; they'll be picked up on subsequent runs |
| `days_failed` | Targeted dates whose ingest raised this run (the countable form of what `error` spells out in prose) |
| `sync_state` | One-line deterministic read of the run — status, what landed, what's still outstanding, and whether baselines were recomputed. Relay this rather than re-deriving a verdict from the raw counts. |

`status: "skipped"` means there was nothing to do (`days_pulled: 0`) — it is not
an error. **Neither is `status: "partial"`**: `gap_days_remaining` is measured
back to 2020-09-01, so a DB that has never been fully backfilled reports
`partial` on every run even when today's sync landed perfectly.

Only a hard failure — `auth_failure`, `not_configured`, `failure`, or
`interrupted` — returns an MCP error payload (`is_error: true`), carrying
`error`, `status`, `days_pulled`, `days_failed`, and `last_date`:

```json
{
  "error": "mfa_required: Garmin requested MFA but no interactive callback is available. Run `uv run fitness pull` in your terminal once to authenticate; subsequent pulls reuse the cached session.",
  "status": "auth_failure",
  "days_pulled": 0,
  "days_failed": 0,
  "last_date": null
}
```

## Example

**Ask:** "Sync my Garmin data, then tell me how last night's sleep looked."

```json
{}
```

Abridged output:

```json
{
  "days_pulled": 3,
  "activities_loaded": 1,
  "status": "success",
  "last_date": "2026-07-21",
  "error": null,
  "gap_days_remaining": 0,
  "deferred_count": 0,
  "days_failed": 0,
  "sync_state": "success — synced 3 day(s), 1 activity row(s) through 2026-07-21; baselines recomputed"
}
```

Baselines were recomputed (new data landed), so a follow-up `daily_snapshot` /
`training_load_status` call now reflects it.

## Gotchas

- **Capped at `SYNC_MAX_DAYS = 30` dates per call.** Deliberate: a long absence
  must not turn one chat tool call into a multi-minute Garmin backfill. Older gaps
  come back as `deferred_count` and are picked up by subsequent calls. Note the
  CLI's `fitness pull` passes **no** cap — use it, not this tool, for a real
  catch-up.
- **It is not fast.** `daily.pull` sleeps 0.5s between days and 0.3s between
  activities on top of Garmin's own latency, so a full 30-day call is tens of
  seconds. Call it when the user asks to sync or when data looks stale, not
  speculatively.
- **First run must be interactive.** Repeated full SSO logins trip Garmin's rate
  limiter (`Mobile login returned 429`), so `_client()` passes
  `_tokenstore_path()` into `client.login()` and resumes a cached garminconnect
  session. Seeding that cache requires answering an MFA prompt, and this tool
  passes **no** MFA callback — the default `_no_mfa_callback` raises immediately,
  surfacing as `status: "auth_failure"` with an `mfa_required:` message. Fix by
  running `uv run fitness pull` once in a terminal; every later call resumes from
  the token.
- **The cached OAuth token eventually expires** (no fixed TTL). When it lapses,
  this tool and the unattended 06:30 launchd job both start failing with
  `auth_failure`; the remedy is the same interactive `uv run fitness pull`
  re-seed.
- **Token path:** `~/.garminconnect/garmin_tokens.json` by default, overridable
  with `GARMINTOKENS` (the container sets it explicitly). That path is the host
  side of the container's `${HOME}/.garminconnect` bind-mount, so host and
  container share one token and the host's interactive login seeds the container.
  `Path.home()` resolves from `HOME`, so the launchd job and the seeding shell
  must share the same `HOME`.
- **Credentials come from macOS Keychain, or `GARMIN_EMAIL` + `GARMIN_PASSWORD`
  when both are set** (env wins — required in the container, which can't reach the
  host Keychain). Neither available ⇒ `status: "not_configured"` with "Garmin
  credentials not stored. Run `fitness setup` first."
- **`partial` is the normal steady state, not a failure.** It used to be both
  gated out of the recompute *and* returned as `is_error: true`, so a single
  missing day back in 2023 meant every sync reported an error while new workouts
  landed and CTL/ATL/TSB silently froze — `query_workouts` showed the run,
  `training_load_status` didn't move. Now the recompute fires on any landed data
  and only hard failures are errors. Use `sync_state` (and `days_failed` /
  `gap_days_remaining`) to tell "today synced fine, old history is still
  incomplete" from "this sync failed". A real backfill is still `fitness pull`,
  not this tool.
- **Never reached by the automated brief.** `sync_garmin_data` is in `ALL_TOOLS`
  but deliberately absent from `_READ_ONLY_TOOL_NAMES`, the brief loop's explicit
  allow-list, so a generated briefing can never trigger a network pull. The daily
  06:30 launchd job couples pull → recompute → generate at the *CLI* level
  (`fitness brief`), not through this tool.
- **One bad day doesn't poison the run**, but an auth failure does — per-day
  ingest is wrapped in try/except and failures land in the `error` string;
  `GarminConnectAuthenticationError` aborts the whole run.
- **It writes to Garmin-owned tables.** Everything else on the MCP surface treats
  Garmin metrics as read-only. Manual data (`log_manual_workout`,
  `log_observation`) goes through its own tools and lives on negative
  `activity_id`s / the `observations` table, so a sync can't clobber it.

## See also

- [`daily_snapshot`](daily_snapshot.md) / [`get_today_status`](get_today_status.md) — read the data this tool just landed; they also carry `latest_brief_date` / `brief_stale_days`
- [`training_load_status`](training_load_status.md) — CTL/ATL/TSB, which only move after the baseline recompute this tool triggers
- [`query_workouts`](query_workouts.md) — confirm newly-pulled activities
- [`log_manual_workout`](log_manual_workout.md) — for workouts Garmin never captured; a sync will not create them
- [`run_sql`](run_sql.md) — read-only ad-hoc access to every table this tool writes
