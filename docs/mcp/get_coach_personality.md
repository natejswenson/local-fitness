# `get_coach_personality`

> The live coach personality in one call: active profile, the tuned spec (or the profile-file seed), the five numeric dials, and journal size. **Availability:** stdio + HTTP

## What it does

Reads back exactly what the coach is currently speaking with — profile name,
persona prose, per-topic intensity overrides, dials, and the vocabulary an edit
is allowed to use. Read-only.

**Call this before [`update_coach_personality`](update_coach_personality.md).**
That tool patches; a patch applied to an assumed state produces a personality
nobody asked for. This is also how you answer "what kind of coach are you right
now" without guessing.

The personality has two layers. The four profile `.md` files
(`agent/coach_profiles/`: `hardass` — the shipped default — plus `adaptive`,
`supportive`, `neutral`) are the **seeds**. The first
`update_coach_personality` call materializes a `PersonalitySpec` into the
`coach_personality_spec` settings key, and from then on the spec is the source
of truth for the persona prose. Until then the tool shows you the seed, so the
output shape is the same whether or not anything has been tuned — `customized`
is what distinguishes them.

Precedence across the whole voice stack: **user notes > spec > profile file.**

## Parameters

None. Takes no arguments.

## Returns

```json
{
  "profile": "hardass",
  "customized": true,
  "base_profile_mismatch": false,
  "spec": {
    "base_profile": "hardass",
    "identity": "You are the coach who holds up the training log and refuses to look away…",
    "catchphrases": ["The log doesn't negotiate."],
    "principles": ["Name the miss before the excuse gets there."],
    "never_do": ["Never soften a red recovery day — order the rest."],
    "intensity": {"step_goal_nagging": "off", "quality_day_misses": "brutal"},
    "updated_at": "2026-07-26T07:41:02"
  },
  "dials": {
    "harshness": 8, "warmth": 3, "push": 9,
    "roast_threshold": 0.85, "praise_threshold": 0.95
  },
  "intensity_levels": ["off", "low", "medium", "high", "brutal"],
  "known_topics": ["conditioning", "excuses", "plan_adherence", "praise",
                   "quality_day_misses", "recovery", "sleep", "step_goal_nagging"],
  "journal_entries": 60,
  "journal_archived": 154,
  "memory_enabled": true
}
```

| Key | Meaning |
|---|---|
| `profile` | Active profile name — `coach_profile` setting > `LOCAL_FITNESS_COACH_PROFILE` env > `"hardass"`. |
| `customized` | `true` when a **usable** stored spec is in force. `false` means `spec` below is the profile-file seed, not something anyone tuned. |
| `base_profile_mismatch` | `true` when a stored spec exists but was tuned for a *different* profile. It is ignored (not deleted) — switch the profile back and the tuning returns. |
| `spec` | The **effective** spec: the stored one when usable, otherwise `seed_from_profile(active)`. Never `null`. |
| `dials` | The five numeric dials as resolved (DB setting > env > the profile file's own front-matter value). |
| `intensity_levels` | The five legal levels, in order. The enum an edit must use. |
| `known_topics` | `TOPIC_WHITELIST`, sorted — the named topics with stable keys. Custom slugs are allowed too (see gotchas). |
| `journal_entries` | **Hot** journal count (`archived = 0`) — the entries actually injected into prompts, capped at 60. |
| `journal_archived` | Archived count. Not in any prompt, still searchable via [`recall_coach_memories`](recall_coach_memories.md). |
| `memory_enabled` | `false` when `LOCAL_FITNESS_COACH_MEMORY` is `0`/`false`/`no`/`off` — memory injection and auto-reflect are off, though the journal data and search are untouched. |

Inside `spec`: `identity` is the persona prose, `catchphrases` / `principles` /
`never_do` are the edited lists (≤12 items each), `intensity` maps topic slugs
to levels (≤16 topics, only *overrides* — a topic at `medium` is absent, not
listed), and `updated_at` is `null` on a seed.

## Example

> "Are you always this harsh, or did I do that?"

```json
{}
```

```json
{"profile": "hardass", "customized": true, "base_profile_mismatch": false,
 "spec": {"base_profile": "hardass", "intensity": {"step_goal_nagging": "off"},
          "updated_at": "2026-07-26T07:41:02", "…": "…"},
 "dials": {"harshness": 8, "warmth": 3, "push": 9,
           "roast_threshold": 0.85, "praise_threshold": 0.95},
 "journal_entries": 60, "journal_archived": 154, "memory_enabled": true}
```

`customized: true` with an `updated_at` is the honest answer: the base is
shipped, the edge is tuned.

## Gotchas

- **`customized: false` does not mean the spec is empty.** It means nothing is
  *stored* — `spec` is the seed rendered from the active profile file, shown so
  an edit has something concrete to patch. Reporting the seed's prose to the
  user as "your customizations" is wrong.
- **`base_profile_mismatch: true` is a silent no-op state.** The stored spec is
  being ignored right now, but retained on purpose. Two honest fixes: switch the
  profile back (the tuning returns intact) or
  [`update_coach_personality`](update_coach_personality.md) with `reset: true`
  to discard it. Don't describe the tuning as lost.
- **`known_topics` is a convenience list, not a validator.** Any slug matching
  `^[a-z0-9_]{1,40}$` is accepted by the update tool — the coach is allowed to
  grow new topics conversationally. Prefer a whitelisted slug when one fits, so
  the same concept doesn't accumulate three keys.
- **The dials are not in the spec, and that's deliberate.** They live in their
  own settings keys (`coach_harshness`, …) and are resolved independently, so
  `reset: true` clears the tuned prose but leaves the dials where they are. Read
  both halves before claiming what a reset will do.
- **`LOCAL_FITNESS_COACH_SPEC=0` ignores the stored spec without deleting it,**
  and that kill switch is not reflected in this payload — a spec suppressed that
  way still reads as `customized: true` here. `memory_enabled` covers the *other*
  switch (`LOCAL_FITNESS_COACH_MEMORY`), not this one.
- **`journal_entries` maxes out at 60 by construction.** Seeing exactly 60 means
  the cap is doing its job, not that the coach has 60 memories — check
  `journal_archived` for the real total.
- **This is a snapshot, not a live handle.** An edit takes effect on the *next*
  prompt render, with no restart; re-read after updating if you want to state
  the new state accurately.

## See also

- [`update_coach_personality`](update_coach_personality.md) — the write path; read this first
- [`list_coach_memories`](list_coach_memories.md) — the journal behind those two counts
- [`list_user_notes`](list_user_notes.md) — the layer that outranks the spec
