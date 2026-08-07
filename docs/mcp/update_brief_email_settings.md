# `update_brief_email_settings`

> Configure the evening brief email conversationally — stop or resume the nightly send, or change who receives it. **Availability:** stdio + HTTP

## What it does

The agent-owned write path for the evening email. There is no UI, so this tool
and [`get_brief_email_settings`](get_brief_email_settings.md) are the whole
configuration surface — the same arrangement as the training plan and the coach
personality.

Writes the **DB layer** of `config.py`'s `DB setting > env var > default`
precedence, so a value set here outranks whatever is in `.env` and takes effect
on the next send with no restart.

**Read [`get_brief_email_settings`](get_brief_email_settings.md) first.** `to`
*replaces* the recipient list rather than appending to it.

## Parameters

| Field | Type | Meaning |
|---|---|---|
| `enabled` | boolean | `false` stops the nightly email; `true` resumes it. |
| `to` | array of strings | **Full replacement** recipient list. A bare comma-separated string is also accepted. |

Both optional; pass at least one. Unknown fields are rejected rather than
ignored, so a typo fails loudly instead of silently changing nothing.

## Returns

```json
{
  "updated": true,
  "changed": ["to"],
  "enabled": true,
  "to": ["you@gmail.com", "you@work.com"],
  "schedule": "19:00 daily, backstop 20:00 (launchd com.localfitness.briefmail)",
  "password_configured": true
}
```

`changed` lists only the fields this call actually wrote. `enabled` and `to`
are re-resolved after the write, so they are the new effective state rather
than an echo of the arguments.

## Example

> "Stop emailing me the brief."

```json
{"enabled": false}
```

```json
{"updated": true, "changed": ["enabled"], "enabled": false,
 "to": ["you@gmail.com"], "password_configured": true}
```

> "Also send it to nate@work.com."

Read first — the existing list is `["you@gmail.com"]` — then send **both**:

```json
{"to": ["you@gmail.com", "nate@work.com"]}
```

Sending only the new address would have silently unsubscribed the old one.

## Gotchas

- **`to` replaces, it does not append.** The single most likely way to misuse
  this tool is honoring "also send it to X" with `{"to": ["X"]}`, which drops
  every existing recipient. Read the current list first, every time.
- **The SMTP password is not settable here, by design.** It is a secret and
  lives only in `<repo>/.env`. A tool that wrote credentials into the settings
  table would put a live Gmail app password behind a network-reachable
  endpoint. When the user wants to change it, tell them the file — do not look
  for a tool.
- **`enabled: true` does not guarantee delivery.** `password_configured` is
  returned on every write precisely so "I turned it on" is never the last word
  when the send is still blocked by a missing credential. If it comes back
  `false`, say so in the same breath.
- **The send time is not settable.** It lives in the launchd plist; changing it
  means editing `ops/com.localfitness.briefmail.plist.template` and re-running
  `./ops/install-launchd.sh briefmail`. `schedule` is returned as prose to make
  that obvious.
- **Disabling stops the whole job, not just the send.** The CLI checks
  `enabled` before pulling Garmin and before regenerating the brief, so a
  disabled setup spends no Garmin call and no LLM run. It also means the
  *morning* brief is unaffected — that is a separate job
  (`com.localfitness.brief`) with its own schedule.
- **Email validation is deliberately loose** (`something@something.something`).
  It catches a truncated or transposed address before it becomes a silent
  nightly bounce; it does not adjudicate exotic-but-legal addresses. A rejected
  address that the user insists is real is a bug in the check, not in the
  address.

## See also

- [`get_brief_email_settings`](get_brief_email_settings.md) — read this first
- [`update_coach_personality`](update_coach_personality.md) — the same conversational-config pattern
