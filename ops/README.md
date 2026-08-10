# ops/ — scheduled brief jobs (macOS launchd)

The daily brief is composed by a **separate, scheduled process** — not the
web server. In the agent-first architecture the web server holds no Claude
inference; the brief is written out-of-band by `fitness brief`, which runs
the Claude-bound headless agent (in-process MCP via `make_server()`) and
persists the result through `briefs.save_brief()`.

On macOS, `launchd` runs three jobs a day:

| Job | When | Command | What it does |
| --- | --- | --- | --- |
| `com.localfitness.brief` | 08:30, backstop 09:30 | `fitness brief --if-missing` | Generates and saves the day's brief |
| `com.localfitness.briefmail` | 19:00, backstop 20:00 | `fitness brief-email --if-unsent` | Pulls fresh Garmin data, regenerates the brief against it, emails it |
| `com.localfitness.plancal` | 19:05, backstop 20:05 | `fitness plan-calendar` | Reconciles Google Calendar against the remaining training plan |


## Install

```bash
./ops/install-launchd.sh
```

This resolves your `uv` binary and this repo's path, fills them into each
`ops/com.localfitness.*.plist.template`, writes the rendered plists to
`~/Library/LaunchAgents/`, and loads them. Pass a job name
(`./ops/install-launchd.sh briefmail`) to install just one. If the Mac is
asleep at a fire time, launchd runs the missed job once at the next wake.

All three are **LaunchAgents, not LaunchDaemons**, and must stay that way: the
bundled Claude SDK CLI reads its credential from the login keychain, which
a user agent can reach and a system daemon cannot.

## Credentials

The job needs `CLAUDE_CODE_OAUTH_TOKEN` (your Claude Max subscription — no
per-token API billing). The CLI auto-loads `.env` from the repo root via
`load_dotenv()`, so put the token in `<repo>/.env` (gitignored). It is
**not** stored in the plist. The scheduled run talks to the MCP in-process,
so it needs neither `LOCAL_FITNESS_API_TOKEN` nor an allowed-host entry.

## The evening email job

`fitness brief-email` does four things in order: pull Garmin, recompute
baselines, regenerate the brief, send it.

Regenerating **overwrites** `briefings/<today>.json`. That is intended — by
19:00 the day's training is in the data and the morning brief is describing a
day that hadn't happened yet, so the evening version is the better record.
The coach does not journal the day twice; `reflect` keys on
`("brief", <date>)` and pre-checks `journal.has_event`.

Dedupe differs from the morning job's, and the difference matters. `brief
--if-missing` keys on the saved brief file, a test that is useless here
because the morning job already wrote one. `brief-email --if-unsent` keys on a
per-date **sent marker** (`briefings/.emailed-<date>`) written only after a
confirmed send — so the 20:00 backstop re-sends exactly when 19:00 failed, and
never when it succeeded.

Delivery is plain `smtplib` with no Claude anywhere in the path. That is
deliberate: the Gmail MCP connector can only create a *draft*, and any
connector route would require a model to retype the chart PNGs as base64 into
a tool call. Sending directly is what keeps the charts at full fidelity.

### Check the composed mail without sending

```bash
uv run fitness brief-email --no-pull --no-generate --dry-run /tmp/brief.eml
```

Writes the full MIME message to disk and opens no socket. Works before any
password is configured, which is the point.

## The calendar job

`fitness plan-calendar` makes Google Calendar **equal** the active training
plan: every prescribed session from today through the plan's last day as an
all-day event, updated where it drifted, and **deleted** where the plan no
longer asks for it. It is the cheapest of the three jobs by a wide margin — no
Garmin call, no Claude call, and in the steady state a single HTTPS request.

Since 0.53.0 the four plan-write MCP tools (`update_plan_workout`,
`update_plan_workouts`, `commit_training_plan`, `abandon_active_plan`) run the
same reconcile themselves, so a plan edit reaches the calendar in the same turn.
That makes this job a **reconciler**, not the only writer: it repairs a sync
that failed mid-edit, a plan changed through `run_sql`, or an event that
drifted.

It is a separate job rather than a tail step on `brief-email` for two reasons.
It shares none of that job's cost, so a Google outage should not make the email
job look broken; and as a tail step it would get **no retry at all**, because
by the time it ran the `.emailed-<date>` marker already short-circuits the
20:00 backstop.

It also takes **no dedupe flag**, and that is the interesting part. Event ids
are hashed from `(plan_id, date, seq)`, so the backstop lists what is there,
finds it already equal to the plan, and issues zero writes. Idempotence is a
property of the data instead of a marker file that can drift from it — and it
handles what a marker could not: if the plan *changed* between 19:05 and 20:05,
the backstop applies the change rather than skipping it as already-done.

Two rails on the delete side, both worth knowing:

- **The past is never touched.** Yesterday's event records what was prescribed
  yesterday; the plan may have changed since, and rewriting history to match is
  not a sync.
- **A deleted event is a tombstone and is left alone.** Deleting the event is
  how a person says "not this one"; a job that puts it back an hour later is a
  job that gets uninstalled. The run reports how many days it skipped for this
  reason, because otherwise a session that never appears has no explanation.

Setup is a one-time Google Cloud + OAuth walkthrough:
**[`docs/google-calendar.md`](../docs/google-calendar.md)**.

### Check the plan without writing it

```bash
uv run fitness plan-calendar --dry-run
```

Prints every event body and opens no socket. Works before any credential
exists, same as the mail dry-run.

## Verify / manage

```bash
launchctl start com.localfitness.brief       # run the morning job now
launchctl start com.localfitness.briefmail   # run the evening job now
launchctl start com.localfitness.plancal     # run the calendar job now
tail -f logs/brief.launchd.err.log           # morning output
tail -f logs/briefmail.launchd.err.log       # evening output
tail -f logs/plancal.launchd.err.log         # calendar output
./ops/uninstall-launchd.sh                   # remove all jobs
```

Success for the morning job looks like a fresh `briefings/<today>.json` and a
non-error exit. For the evening job, add a `.emailed-<today>` marker beside it
and a message in your inbox. For the calendar job, the plan on your calendar — and
**0 created, 0 updated, 0 deleted** on a second run, which is the property
worth checking. Exit code 2
means mail (or the calendar OAuth client) is not configured — the log line
names the missing variable.

## Linux

No launchd. Schedule `uv run fitness brief`, `uv run fitness brief-email` and
`uv run fitness plan-calendar` with cron or systemd timers, ensuring
`CLAUDE_CODE_OAUTH_TOKEN`, the `LOCAL_FITNESS_SMTP_*` settings and the
`LOCAL_FITNESS_GCAL_*` values are in `<repo>/.env`.
