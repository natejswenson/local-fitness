# `save_user_note`

> **WRITE TOOL.** Appends a NEW durable coaching preference to the notes file that is injected into every future system prompt. **Availability:** stdio + HTTP

## What it does

User notes are durable **coaching preferences** — instructions about how the
coach should behave ("stop roasting my step count", "lead with the workout
card", "I prefer morning runs"). They are stored as bullets in a Markdown file
(`data/user_notes.md`, override with `LOCAL_FITNESS_NOTES_PATH`) and injected
verbatim into the system prompt by `prompts.system_prompt()` and
`prompts.brief_v2_system_prompt()`, so a saved note shapes every subsequent
brief and chat turn.

Reach for this only when the user states a *lasting* preference. If you want to
record a subjective **data point** about a specific day — RPE, soreness, weight,
mood, how a run felt — that is [`log_observation`](log_observation.md), not this.
Notes are instructions the coach follows; observations are data the coach reads.
If a similar note already exists, ask first and use
[`update_user_note`](update_user_note.md) rather than piling on a duplicate.

Preference management is **conversational by design** — there is no settings UI
and none is planned. Add, list, edit, and delete all happen through these tools
in chat.

## Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `note` | string | yes | — | One sentence. Whitespace is collapsed to single spaces (newlines folded), so a multi-line note can't break the bullet structure. Truncated to 800 chars + `…`. Empty/whitespace-only is rejected. |

## Returns

`notes.append_note()` writes `- <ISO timestamp> — <text>` as the last line of
the live file and the handler returns the resulting `Note`:

```json
{
  "saved": true,
  "handle": "d70a5c19",
  "timestamp": "2026-07-21T09:14:02",
  "text": "Stop roasting my step count — I care about running volume, not steps."
}
```

`handle` is an 8-character lowercase hex content address derived from the note's
own `timestamp` and `text` — not a file position. It is what you pass to
[`update_user_note`](update_user_note.md) / [`delete_user_note`](delete_user_note.md).

On failure (empty note text): `{"error": "..."}` with `is_error: true`.

## Example

> "From now on don't lecture me about sleep on weekends, I'm never going to fix that."

```json
{"note": "Don't comment on weekend sleep — it's a known, accepted gap."}
```

```json
{"saved": true, "handle": "c19de402", "timestamp": "2026-07-21T09:14:02",
 "text": "Don't comment on weekend sleep — it's a known, accepted gap."}
```

## Gotchas

- **CRITICAL — a note can never change the brief's JSON schema.** The daily
  brief's output shape (`Brief` / `Takeaway`) is fixed. A note like "show a
  snapshot table at the top of the brief" can talk the generator into emitting
  a deviating top-level object; `agent/briefs.py`'s `_salvage_takeaways()` is
  the safety net — it walks the payload for the first list-of-dicts with
  `headline` keys, salvages it as the takeaways array, logs a warning, and
  discards the invented top-level fields. **A note that tries to restructure
  brief output is a bug, not a feature.** Notes may change tone, emphasis, and
  what gets called out. They may not add fields.
- **The returned `handle` is a content address, not a position.** It's derived
  from this note's own timestamp and text, so it keeps resolving correctly
  through an unrelated `delete_user_note` or a rotation that renumbers the
  file — that no longer redirects a later `update_user_note` /
  `delete_user_note` call onto the wrong preference. It stops resolving only
  once *this* note itself is updated, deleted, or rotated to the archive; at
  that point `list_user_notes` is how you get a live handle again.
- **4 KB live cap with silent rotation.** If the append would push the file past
  `LIVE_FILE_MAX_BYTES` (4096), the oldest bullets are dropped from the live
  file and appended to `user_notes.archive.md` *before* the write. Archived
  notes are no longer injected into the prompt and no longer appear in
  `list_user_notes` — they are effectively forgotten. The tool result does not
  tell you rotation happened.
- **Recency is a prompt instruction, not enforced code.** Nothing dedupes or
  reconciles conflicting notes. `render_for_prompt()` orders them newest-first
  and the system prompt says "prefer the newer note when two conflict" — that
  is the whole contract. Two contradictory notes both stay in the prompt.
- **Writes are serialised by a sidecar lock file** (`user_notes.md.lock`) and
  land via a same-directory temp file that's atomically renamed over the
  original — so two chat sessions can't corrupt the file, and no reader
  (this server, or a text editor with the file open) ever sees a partial
  write. Process-local locking, single-host only.
- The brief loop can't call this. `_READ_ONLY_TOOL_NAMES` in `agent/tools.py`
  excludes every note-write tool, so brief generation can read notes (via the
  prompt) but never writes one.

## See also

- [`list_user_notes`](list_user_notes.md) — read notes back with their handles
- [`update_user_note`](update_user_note.md) — refine an existing preference in place
- [`delete_user_note`](delete_user_note.md) — drop a preference
- [`log_observation`](log_observation.md) — the *other* family: timestamped subjective data, not preferences
- [`daily_snapshot`](daily_snapshot.md) — returns `user_notes` alongside today's metrics
