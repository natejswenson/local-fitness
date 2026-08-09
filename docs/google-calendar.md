# Google Calendar setup

`fitness plan-calendar` makes Google Calendar **equal** your active training
plan: every prescribed session from today through the plan's last day, as an
all-day event. It updates what drifted and deletes what the plan no longer asks
for.

The four plan-editing MCP tools run the same sync themselves, so a change you
make in conversation reaches the calendar in the same turn. On macOS a launchd
job also reconciles at 19:05 with a 20:05 backstop — the brief email's cadence,
offset five minutes so the two evening jobs don't contend for the same wake.

A rest day writes nothing. No active plan writes nothing. Both exit 0.

This is a one-time setup. It takes about five minutes and needs a Google
account and a browser.

---

## Why this needs your own OAuth client

There is no shared credential to hand out — a public repo cannot ship one, and
a client secret in tracked code is a client secret on GitHub. So each clone
registers its own OAuth client against its own Google Cloud project. Nothing
leaves your machine: the token this produces lives in `<repo>/.env`
(gitignored) and is used by a local process talking directly to Google.

If you'd rather not do this, leave it unconfigured. The job exits 2 with a
message naming the missing variable and nothing else in the app is affected.

---

## 1. Create a project and enable the API

1. Go to [console.cloud.google.com](https://console.cloud.google.com/) and
   create a project (any name — `local-fitness` is fine).
2. **APIs & Services → Library →** search for **Google Calendar API** →
   **Enable**.

## 2. Configure the consent screen

**APIs & Services → OAuth consent screen.**

- User type: **External** (a personal Gmail account has no other option).
- App name, support email, developer email: anything sensible. You are the only
  user.
- Scopes: you can skip adding scopes here — the app requests
  `.../auth/calendar.events` at consent time.

Then the step that actually matters:

> ### Set the publishing status to **"In production"**
>
> An app left in **Testing** has its refresh tokens **expired by Google after 7
> days**. The nightly job would work for a week, then fail, every week, looking
> like a different bug each time. Publishing is a single button; because the app
> is unverified you'll see a one-time "Google hasn't verified this app" screen
> during consent — click **Advanced → Go to … (unsafe)**. That warning is about
> Google not having reviewed *your own* app, and the app is the script in this
> repo.

## 3. Create the OAuth client

**APIs & Services → Credentials → Create credentials → OAuth client ID.**

- Application type: **Desktop app**.
- Name: anything.

Copy the client ID and client secret into `<repo>/.env`:

```bash
LOCAL_FITNESS_GCAL_CLIENT_ID=1234567890-abcdefg.apps.googleusercontent.com
LOCAL_FITNESS_GCAL_CLIENT_SECRET=GOCSPX-…
```

No redirect URI needs registering — a Desktop-app client accepts loopback
redirects on any port, which is what `calendar-auth` uses.

## 4. Consent once

```bash
uv run fitness calendar-auth
```

This opens the consent screen, catches the redirect on a throwaway
`127.0.0.1` server, and prints a refresh token. Add it to `.env`:

```bash
LOCAL_FITNESS_GCAL_REFRESH_TOKEN=1//0e…
```

The token is **printed rather than written** on purpose: `.env` holds every
other credential in this deployment, and a bug in a program that edits it is a
bug that eats them.

## 5. Verify

```bash
# See what would be written, with no network call at all:
uv run fitness plan-calendar --dry-run

# Actually write it:
uv run fitness plan-calendar
```

Then check the two properties that matter:

- **Run it a second time.** It must report `0 created, 0 updated, 0 deleted` —
  not a second copy of your plan.
- **Delete one event in Google Calendar, then run it again.** It should say
  `1 day(s) skipped` and leave it deleted.

## 6. Install the nightly job (macOS)

```bash
./ops/install-launchd.sh plancal
launchctl start com.localfitness.plancal   # run it now to check
tail logs/plancal.launchd.err.log
```

On Linux, schedule `uv run fitness plan-calendar` with cron or a systemd timer
instead.

---

## How it behaves

| Situation | What happens |
|---|---|
| Rest day, or no plan row for a date | No event for that day |
| No active plan | Nothing written, exit 0 |
| Everything already matches | `0 created, 0 updated, 0 deleted` — one request |
| A day was edited | `updated` in place, no duplicate |
| A day became a rest day | The event is **deleted** |
| You deleted an event by hand | Skipped and reported — it is not put back |
| You abandoned the plan | Every remaining event is deleted |
| You committed a new plan | The old plan's remaining events are deleted first |
| A past date | Never touched, in either direction |
| Credentials missing | Exit 2, naming the missing variable |

Event ids are a hash of `(plan_id, date, seq)`, which is what makes all of that
true without a marker file: re-running is idempotent because of the *data*, not
because of bookkeeping that could drift from it. It also means the sync can only
ever see or touch events it created — it lists by its own tag and its own plan
id, and everything else on your calendar is invisible to it.

Events are **all-day and marked free**, not busy — an all-day block that marked
you busy would break every scheduling tool pointed at the calendar.

Two things it will not do, deliberately. It never rewrites the **past**:
yesterday's event records what was prescribed yesterday, and the plan may have
changed since. And it never puts back an event you **deleted** — that is how you
say "not this one", and a job that overruled you an hour later is a job you
would uninstall.

## Configuration

Everything except the secrets is set conversationally through MCP, the same as
the brief email. `enabled: false` stops future syncs; it does **not** remove
events already on the calendar.

- `get_plan_calendar_settings` — enabled state, target calendar, whether
  credentials are present
- `update_plan_calendar_settings` — `enabled`, `calendar_id`

The OAuth values are **not** settable through any tool. `/mcp/` is served over
the network and reachable from a phone; a tool that echoed the refresh token
would hand every client that can call the endpoint write access to your
calendar. Secrets stay in `.env`.

## Troubleshooting

**`invalid_grant` in the logs.** The refresh token was revoked or expired. The
overwhelmingly likely cause is an OAuth app still in **Testing** (see step 2) —
fix the publishing status first, then re-run `fitness calendar-auth`. Other
causes: you revoked access at
[myaccount.google.com/permissions](https://myaccount.google.com/permissions), or
the token went unused for six months.

**`calendar-auth` says Google returned no refresh token.** Google reuses an
existing grant and returns only an access token when the account has already
approved this client. Revoke it at
[myaccount.google.com/permissions](https://myaccount.google.com/permissions) and
run the command again.

**HTTP 404 on the calendar.** `calendar_id` points at a calendar this account
can't write to. `primary` always works; check with
`get_plan_calendar_settings`.

**A session never appears.** You probably deleted that event by hand at some
point — the sync reports how many days it skipped for that reason on every run,
and it will not put them back. Delete nothing and re-run, or edit the day in the
plan (which changes its content and does not resurrect the tombstone).

**The event is on the wrong day.** All-day events use the host's local date.
Unlike the container (see `docs/deployment.md`), this job only ever runs on the
host CLI, which inherits the OS timezone — but if you run it inside a container
for some reason, `TZ` must be set there or `date.today() + 1` is off by one for
part of the evening.
