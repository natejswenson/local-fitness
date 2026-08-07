# `get_brief_email_settings`

> How the evening brief email is configured: enabled state, recipients, whether a sending credential is present, and the schedule. **Availability:** stdio + HTTP

## What it does

Reads back what the 19:00 job will actually do tonight. Read-only.

**Call this before [`update_brief_email_settings`](update_brief_email_settings.md).**
That tool replaces the recipient list wholesale rather than appending, so
"also send it to my work address" is only correct if you know what is already
there.

Settings resolve **DB setting > env var > default**, the same precedence as
every other knob in `config.py`. The DB layer is what the update tool writes,
which is why configuration is a sentence rather than a text edit.

## Parameters

None. Takes no arguments.

## Returns

```json
{
  "enabled": true,
  "to": ["you@gmail.com"],
  "to_is_explicit": false,
  "password_configured": true,
  "smtp_user": "you@gmail.com",
  "smtp_host": "smtp.gmail.com",
  "schedule": "19:00 daily, backstop 20:00 (launchd com.localfitness.briefmail)",
  "can_send": true,
  "blocked_reason": null
}
```

| Key | Meaning |
|---|---|
| `enabled` | `false` means the nightly job exits before pulling Garmin or generating anything. The conversational kill switch. |
| `to` | The addresses that would actually receive tonight's brief. |
| `to_is_explicit` | `false` means no recipient is configured and `to` above is the *fallback* — the sending account mailing itself. |
| `password_configured` | Whether an SMTP password is present. **A boolean and nothing more** — see gotchas. |
| `smtp_user` | The authenticating account, or `null` when unset. |
| `smtp_host` | Resolved SMTP host; `smtp.gmail.com` unless overridden. |
| `schedule` | Prose, not data. The send time is not a setting — see gotchas. |
| `can_send` | `enabled` AND a password AND a sending account. The single field worth checking before promising the user a brief tonight. |
| `blocked_reason` | Why `can_send` is `false`, naming the exact thing to fix. `null` when nothing is blocking. |

## Example

> "Am I still getting the evening brief?"

```json
{}
```

```json
{"enabled": false, "to": ["you@gmail.com"], "to_is_explicit": true,
 "password_configured": true, "can_send": false,
 "blocked_reason": "disabled via settings",
 "schedule": "19:00 daily, backstop 20:00 (launchd com.localfitness.briefmail)"}
```

The honest answer is no, and `blocked_reason` says which of the three possible
reasons it is — so the reply is "you turned it off, want it back on?" rather
than a guess about credentials.

## Gotchas

- **The SMTP password is never returned by this or any other tool.** `/mcp/` is
  served over the network and reachable from a phone; a settings tool that
  echoed the credential would publish a live Gmail app password to every client
  that can call it. `password_configured` answers the only question a
  configuration reader legitimately has. If the user wants to *change* it, the
  answer is always "put it in `<repo>/.env`", never a tool call.
- **`enabled: true` does not mean mail will arrive.** Check `can_send`. A brief
  with no password configured is enabled and undeliverable, and reporting
  "you're all set" off `enabled` alone is the obvious wrong answer.
- **The send time is not a setting and cannot be changed from here.** It lives
  in `ops/com.localfitness.briefmail.plist.template`; changing it means editing
  that file and re-running `./ops/install-launchd.sh briefmail` on the host. It
  is reported as prose specifically so it doesn't read as an editable field.
- **`schedule` describes the plist this repo ships, not the job actually
  installed on the machine.** Nothing here inspects `launchctl`. If the user
  edited their plist, this string is stale — say "as shipped" rather than
  asserting what will fire.
- **`to_is_explicit: false` is a real state worth naming.** It means nobody
  chose a recipient and the mail is going to the sending account by fallback.
  That's usually right for a personal deployment and usually wrong the moment
  someone else clones the repo.
- **This is a snapshot.** An update takes effect on the next send with no
  restart, but env-layer changes need the process restarted, since `.env` is
  read once at CLI startup.

## See also

- [`update_brief_email_settings`](update_brief_email_settings.md) — the write path
- [`generate_brief_report`](generate_brief_report.md) — the PDF sibling of the emailed report
