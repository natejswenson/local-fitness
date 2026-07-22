# `update_user_note`

> **WRITE TOOL — destructive.** Replaces the coaching preference at a given line index with new text and refreshes its timestamp. **Availability:** stdio + HTTP

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
| `line` | integer | yes | — | 0-indexed raw file line number, from [`list_user_notes`](list_user_notes.md) or the `[n]` prefixes in the system prompt's notes section. Must be an `int`; a non-int is rejected. |
| `note` | string | yes | — | Replacement text. Whitespace collapsed to single spaces, truncated to 800 chars + `…`, empty rejected. |

## Returns

```json
{
  "updated": true,
  "line": 3,
  "timestamp": "2026-07-21T09:22:11",
  "text": "Roast me when I'm slipping, but not about steps."
}
```

`timestamp` is refreshed to now — an updated note becomes the *newest* note for
the purposes of the recency rule, even though its line index (and therefore its
position in the file) does not move.

Failure returns `is_error: true` with `{"error": "no note at line 3"}` when the
index is out of range or the line isn't a bullet, or `{"error": "line index is
required"}` / `{"error": "new note text is required"}` for bad arguments.

## Example

> "Change the roasting note — I still want the harsh tone, just not about step count."

Read first, then target the index:

```json
{"line": 1, "note": "Roast me when I'm slipping, but never about step count."}
```

```json
{"updated": true, "line": 1, "timestamp": "2026-07-21T09:22:11",
 "text": "Roast me when I'm slipping, but never about step count."}
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
- **Always re-read the index immediately before calling.** Line indices are raw
  file line numbers and are invalidated by any delete (later lines shift down)
  and by an append that triggers the 4 KB rotation (everything renumbers). A
  stale index silently edits the *wrong* preference — it will not error, because
  the target line is still a valid bullet.
- **Fails closed on non-bullet lines.** If `line` points at hand-written prose
  (a line not starting with `- `) the update is refused rather than clobbering
  it, and a missing notes file returns "no note at line N" rather than creating
  one.
- Unlike [`save_user_note`](save_user_note.md), this never rotates to the
  archive — it rewrites a line in place, so a much longer replacement can push
  the live file past the 4 KB cap without triggering the rotation that only
  `append_note` performs.
- Not reachable from the brief loop — `_READ_ONLY_TOOL_NAMES` excludes every
  note-write tool so brief generation cannot mutate preferences.

## See also

- [`list_user_notes`](list_user_notes.md) — get the current line indices first
- [`save_user_note`](save_user_note.md) — add a genuinely new preference instead
- [`delete_user_note`](delete_user_note.md) — drop it entirely
- [`log_observation`](log_observation.md) — the *other* family: subjective data, not instructions
