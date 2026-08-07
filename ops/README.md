# ops/ — scheduled brief jobs (macOS launchd)

The daily brief is composed by a **separate, scheduled process** — not the
web server. In the agent-first architecture the web server holds no Claude
inference; the brief is written out-of-band by `fitness brief`, which runs
the Claude-bound headless agent (in-process MCP via `make_server()`) and
persists the result through `briefs.save_brief()`.

On macOS, `launchd` runs two jobs a day:

| Job | When | Command | What it does |
| --- | --- | --- | --- |
| `com.localfitness.brief` | 06:30, backstop 09:30 | `fitness brief --if-missing` | Generates and saves the day's brief |
| `com.localfitness.briefmail` | 19:00, backstop 20:00 | `fitness brief-email --if-unsent` | Pulls fresh Garmin data, regenerates the brief against it, emails it |


## Install

```bash
./ops/install-launchd.sh
```

This resolves your `uv` binary and this repo's path, fills them into each
`ops/com.localfitness.*.plist.template`, writes the rendered plists to
`~/Library/LaunchAgents/`, and loads them. Pass a job name
(`./ops/install-launchd.sh briefmail`) to install just one. If the Mac is
asleep at a fire time, launchd runs the missed job once at the next wake.

Both are **LaunchAgents, not LaunchDaemons**, and must stay that way: the
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
19:00 the day's training is in the data and the 06:30 brief is describing a
day that hadn't happened yet, so the evening version is the better record.
The coach does not journal the day twice; `reflect` keys on
`("brief", <date>)` and pre-checks `journal.has_event`.

Dedupe differs from the morning job's, and the difference matters. `brief
--if-missing` keys on the saved brief file, a test that is useless here
because the 06:30 job already wrote one. `brief-email --if-unsent` keys on a
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

## Verify / manage

```bash
launchctl start com.localfitness.brief       # run the morning job now
launchctl start com.localfitness.briefmail   # run the evening job now
tail -f logs/brief.launchd.err.log           # morning output
tail -f logs/briefmail.launchd.err.log       # evening output
./ops/uninstall-launchd.sh                   # remove both jobs
```

Success for the morning job looks like a fresh `briefings/<today>.json` and a
non-error exit. For the evening job, add a `.emailed-<today>` marker beside it
and a message in your inbox. Exit code 2 means mail is not configured — the
log line names the missing variable.

## Linux

No launchd. Schedule `uv run fitness brief` and `uv run fitness brief-email`
with cron or systemd timers, ensuring `CLAUDE_CODE_OAUTH_TOKEN` and the
`LOCAL_FITNESS_SMTP_*` settings are in `<repo>/.env`.
