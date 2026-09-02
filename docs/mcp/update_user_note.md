# `update_user_note`

> **WRITE TOOL — destructive.** Replaces the coaching preference with a given content handle with new text and refreshes its timestamp. **Availability:** stdio + HTTP

## What it does

Rewrites one bullet in the user-notes file (`data/user_notes.md`, override
`LOCAL_FITNESS_NOTES_PATH`) in place. Use it when the user is *refining* an
existing durable coaching preference rather than adding a new one — that keeps
the prompt from filling up with near-duplicate instructions that then have to be
reconciled by the recency rule.

Reach for this over [`save_user_note`](save_user_note.md) whenever the new
preference overlaps something already saved. Reach for
[`log_observation`](log_observation.md) instead if what's being recorded is a
subjective *data point* about a specific day (RPE, soreness, weight, mood) — that
is a different family entirely: notes are instructions the coach follows,
observations are data it reads.

Preference management is **conversational by design** — there is no settings UI
and none is planned.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `handle` | string | yes | — | 8-character content address, from [`list_user_notes`](list_user_notes.md) or the `[handle]` prefixes in the system prompt's notes section. Normalised on the way in (whitespace stripped, one layer of surrounding `[ ]` dropped, lowercased); matched exactly — no prefix matching. Blank after normalising is rejected. |
| `note` | string | yes | — | Replacement text. Whitespace collapsed to single spaces, truncated to 800 chars + `…`, empty rejected. |

## Returns

```json
{
  "updated": true,
  "handle": "7c2f9a10",
  "timestamp": "2026-07-21T09:22:11",
  "text": "Roast me when I'm slipping, but not about steps."
}
```

`timestamp` is refreshed to now — an updated note becomes the *newest* note for
the purposes of the recency rule. `handle` in the response is the note's **new**
address: rewriting a note changes its content, so it changes the handle too —
the handle you called with is now stale and will not resolve again.

`duplicates` is also present, normally `1`. It is only greater than 1 when more
than one live bullet happened to share the exact same timestamp and text — a
hand-edited copy-paste, most likely (see Gotchas). The tool rewrites the first
one in file order and reports how many it saw.

Failure returns `is_error: true` with `{"error": "no note with handle '7c2f9a10' — it may have been updated, deleted, or rotated to the archive since you read it; call list_user_notes to re-read"}`, or `{"error": "handle is required"}` / `{"error": "new note text is required"}` for bad arguments.

## Example

> "Change the roasting note — I still want the harsh tone, just not about step count."

Read first, then target the handle exactly as shown:

```json
{"handle": "7c2f9a10", "note": "Roast me when I'm slipping, but never about step count."}
```

```json
{"updated": true, "handle": "b48e0d21", "timestamp": "2026-07-21T09:22:11",
 "text": "Roast me when I'm slipping, but never about step count.", "duplicates": 1}
```

## Gotchas

- **Confirm before overwriting.** The tool description says it and there is no
  undo — the previous text is gone from the file, and it is not archived.
- **CRITICAL — an edited note can never change the brief's JSON schema.** The
  `Brief`/`Takeaway` shape is fixed. A note that asks the brief to add a section
  or restructure its output can push the generator off-schema;
  `agent/briefs.py`'s `_salvage_takeaways()` is the safety net that recovers the
  takeaways array and drops the invented top-level fields. **A note that tries
  to restructure output is a bug, not a feature** — notes shape tone and
  emphasis, never structure.
- **A handle addresses content, not position.** Unlike the old raw file line
  number, it survives an unrelated `delete_user_note` or a rotation that
  renumbers the file — those no longer redirect this call onto the wrong
  preference. It stops resolving only when the *target itself* changed: it was
  updated (its own new handle differs), deleted, or rotated to the archive. In
  every one of those cases the call errors loudly instead of silently editing
  whatever now sits where the note used to be — re-read with `list_user_notes`
  rather than guessing.
- **A duplicated handle is acted on, not refused.** Two bullets only ever share
  a handle if they are byte-for-byte identical in timestamp and text — a
  hand-edit, most likely, since the module invites editing the file directly.
  The call rewrites the first one in file order and reports `duplicates: 2`;
  the surviving pair is unique again immediately afterward (the rewritten one
  has a new handle now).
- **Fails closed on non-bullet lines.** A handle can only ever match a parsed
  bullet, so hand-written prose in the file is never a valid target, and a
  missing notes file returns the same no-match error rather than creating one.
- Unlike [`save_user_note`](save_user_note.md), this never rotates to the
  archive — it rewrites a line in place, so a much longer replacement can push
  the live file past the 4 KB cap without triggering the rotation that only
  `append_note` performs.
- Not reachable from the brief loop — `_READ_ONLY_TOOL_NAMES` excludes every
  note-write tool so brief generation cannot mutate preferences.

## See also

- [`list_user_notes`](list_user_notes.md) — get the current handles first
- [`save_user_note`](save_user_note.md) — add a genuinely new preference instead
- [`delete_user_note`](delete_user_note.md) — drop it entirely
- [`log_observation`](log_observation.md) — the *other* family: subjective data, not instructions
