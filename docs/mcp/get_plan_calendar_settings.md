# `get_plan_calendar_settings`

> How the Google Calendar sync is configured: enabled state, target calendar, whether OAuth credentials are present, and the schedule. **Availability:** stdio + HTTP

## What it does

Reads back whether the training plan is being mirrored to Google Calendar, and
to which calendar. Read-only.

**Call this before [`update_plan_calendar_settings`](update_plan_calendar_settings.md)**
so an edit patches what is actually there.

Settings resolve **DB setting > env var > default**, the same precedence as
every other knob in `config.py`. The DB layer is what the update tool writes,
which is why configuration is a sentence rather than a text edit.

## Parameters

None. Takes no arguments.

## Returns

```json
{
  "enabled": true,
  "calendar_id": "primary",
  "credentials_configured": true,
  "schedule": "reconciled on every plan edit, plus 19:05 daily with a 20:05 backstop (launchd com.localfitness.plancal)",
  "creates_events_for": "every prescribed session on the active plan from today through its last day, as all-day events; a rest day creates nothing, and a day the plan drops is DELETED from the calendar",
  "requires_active_plan": true,
  "can_write": true,
  "blocked_reason": null
}
```

| Key | Meaning |
|---|---|
| `enabled` | `false` means neither the nightly job nor a plan edit touches the calendar. The conversational kill switch. It does **not** remove events already there. |
| `calendar_id` | Which calendar gets the event. `primary` is the authenticated account's default. |
| `credentials_configured` | Whether all three OAuth values are present in `.env`. **A boolean and nothing more** — see gotchas. |
| `schedule` | Prose, not data. The run time is not a setting — see gotchas. Note the sync also fires on every plan edit, not only on the schedule. |
| `creates_events_for` | What actually gets written, stated so the answer to "will tomorrow show up?" doesn't have to be inferred. |
| `requires_active_plan` | Always `true`. With no active plan the job is a clean no-op, not a failure. |
| `can_write` | `enabled` AND credentials present. The single field worth checking before promising the user an event tonight. |
| `blocked_reason` | Why `can_write` is `false`, naming the exact thing to fix. `null` when nothing is blocking. |

## Example

> "Is my training plan on my calendar?"

```json
{}
```

```json
{"enabled": true, "calendar_id": "primary", "credentials_configured": false,
 "can_write": false,
 "blocked_reason": "OAuth credentials are not set in <repo>/.env — run `uv run fitness calendar-auth` (see docs/google-calendar.md)",
 "schedule": "reconciled on every plan edit, plus 19:05 daily…"}
```

The honest answer is no, and `blocked_reason` says which of the two possible
reasons it is — so the reply names the setup step instead of guessing.

## Gotchas

- **The OAuth client secret and refresh token are never returned by this or any
  other tool.** `/mcp/` is served over the network and reachable from a phone; a
  tool that echoed the refresh token would hand every client that can call the
  endpoint write access to the user's calendar. `credentials_configured`
  answers the only question a configuration reader legitimately has. Changing
  them is always "put it in `<repo>/.env`", never a tool call.
- **`enabled: true` does not mean events will appear.** Check `can_write` — and
  note that even `can_write: true` produces nothing with no active plan, or on a
  rest day, both of which are normal outcomes rather than errors.
- **The sync DELETES as well as writes.** A day the plan drops, a plan
  abandoned, a plan superseded by a commit — all remove events. Two things it
  never does: touch a past date, or restore an event the user deleted by hand.
  Say that plainly if asked what it has access to.
- **The run time is not a setting and cannot be changed from here.** It lives in
  `ops/com.localfitness.plancal.plist.template`; changing it means editing that
  file and re-running `./ops/install-launchd.sh plancal` on the host. It is
  reported as prose specifically so it doesn't read as an editable field.
- **`schedule` describes the plist this repo ships, not the job actually
  installed on the machine.** Nothing here inspects `launchctl`.
- **The sync only ever sees events it created.** It lists by its own tag and
  its own plan id, and ids are derived from `(plan_id, date, seq)` — nothing
  else on the calendar is reachable from here.
- **This is a snapshot.** An update takes effect on the next run with no
  restart, but env-layer changes need the process restarted, since `.env` is
  read once at CLI startup.

## See also

- [`update_plan_calendar_settings`](update_plan_calendar_settings.md) — the write path
- [`get_training_plan_status`](get_training_plan_status.md) — the plan this reads from
- [`get_brief_email_settings`](get_brief_email_settings.md) — the other evening delivery surface
