# Google Calendar setup

`fitness plan-calendar` writes the **next day's** prescribed session from your
active training plan to Google Calendar as an all-day event. On macOS it runs
from launchd at 19:05 with a 20:05 backstop — the brief email's cadence, offset
five minutes so the two evening jobs don't contend for the same wake.

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

- **Run it a second time.** You should get `unchanged`, not a duplicate event.
- **Delete the event in Google Calendar, then run it again.** It should report
  `skipped_cancelled` and stay deleted.

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
| Rest day, or no plan row for tomorrow | Nothing written, exit 0 |
| No active plan | Nothing written, exit 0 |
| Event already there, plan unchanged | `unchanged` — no write |
| Event already there, plan edited since | `updated` in place, no duplicate |
| You deleted the event | `skipped_cancelled` — it is not put back |
| Credentials missing | Exit 2, naming the missing variable |

The event id is a hash of `(plan_id, date, seq)`, which is what makes all of
that true without a marker file: re-running is idempotent because of the *data*,
not because of bookkeeping that could drift from it. It also means the job can
only ever see or touch events it created.

Events are **all-day and marked free**, not busy — an all-day block that marked
you busy would break every scheduling tool pointed at the calendar.

## Configuration

Everything except the secrets is set conversationally through MCP, the same as
the brief email:

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

**The event is on the wrong day.** All-day events use the host's local date.
Unlike the container (see `docs/deployment.md`), this job only ever runs on the
host CLI, which inherits the OS timezone — but if you run it inside a container
for some reason, `TZ` must be set there or `date.today() + 1` is off by one for
part of the evening.
