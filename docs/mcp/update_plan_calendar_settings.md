# `update_plan_calendar_settings`

> Turn the nightly Google Calendar event on or off, and pick which calendar it lands on. **Availability:** stdio + HTTP

## What it does

The agent-owned write path for the calendar sync — there is no UI, so "stop
putting my runs on my calendar" is a tool call, not a text edit and not
`launchctl`.

Writes the DB layer of `plan_calendar_enabled` / `plan_calendar_id`, which sits
above the env layer in `config.py`'s precedence. Takes effect on the next run;
nothing restarts.

**Read [`get_plan_calendar_settings`](get_plan_calendar_settings.md) first.**

## Parameters

| Name | Type | Required | Meaning |
|---|---|---|---|
| `enabled` | boolean | no | `false` stops the nightly event; `true` resumes it. |
| `calendar_id` | string | no | `primary` (the default) or a specific calendar's address. |

Both optional, but pass at least one — an empty call is an error rather than a
silent no-op.

## Returns

```json
{
  "updated": true,
  "changed": ["enabled"],
  "enabled": false,
  "calendar_id": "primary",
  "schedule": "19:05 daily, backstop 20:05 (launchd com.localfitness.plancal)",
  "credentials_configured": true
}
```

`changed` lists only the fields this call actually wrote. `enabled` and
`calendar_id` are re-read from the DB afterwards, so they are the new effective
values rather than an echo of the request.

## Example

> "Stop adding my runs to my calendar."

```json
{"enabled": false}
```

```json
{"updated": true, "changed": ["enabled"], "enabled": false,
 "calendar_id": "primary", "credentials_configured": true}
```

Confirm in one line and stop. Nothing already on the calendar is removed — say
so if the user's phrasing suggests they expect a cleanup.

## Gotchas

- **OAuth credentials are not settable here.** They are secrets and live only in
  `<repo>/.env`; `credentials_configured` is reported on every write so
  "I turned it on" can never be the last word when the write would still be
  blocked. A `password`-shaped argument is rejected as an unknown field.
- **Turning it off does not delete existing events.** It stops new ones. If the
  user wants tomorrow's event gone, they delete it in Google Calendar — and the
  job will not put it back (a deleted event is a tombstone the sync leaves
  alone, deliberately).
- **`calendar_id` must be `primary` or an address-shaped id.** Anything else is
  rejected here rather than becoming a 404 at 19:05 that nobody sees until the
  event doesn't appear.
- **Changing `calendar_id` does not move existing events.** Events already
  written stay on the old calendar; new ones go to the new one.
- **The run time is not settable.** See the read tool's gotchas — it lives in
  the launchd plist on the host.

## See also

- [`get_plan_calendar_settings`](get_plan_calendar_settings.md) — read it first
- [`update_brief_email_settings`](update_brief_email_settings.md) — the same shape for the evening email
