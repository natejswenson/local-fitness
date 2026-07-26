# `update_coach_personality`

> **WRITE TOOL.** Tunes the coach's voice conversationally — persona prose, the three list sections, per-topic intensity, and the five numeric dials. **Availability:** stdio + HTTP

## What it does

Patches the `PersonalitySpec` stored in the `coach_personality_spec` settings
key and/or writes the five dial settings. This is the only write path for the
coach's personality — there is no UI, the same agent-owned model as training
plans. Nothing here is ever hand-edited on disk.

**Virtual seeding.** With no stored spec, behavior is byte-identical to the
profile `.md` file. The first call materializes `seed_from_profile(active)` and
applies your patch on top, so an edit never starts from a blank persona.

**Call [`get_coach_personality`](get_coach_personality.md) first.** Every list
field here is add-one / remove-one against current state, and `identity`
*replaces* the prose wholesale. Patching blind is how you delete a persona you
meant to extend.

Takes effect on the next prompt render — no restart, no rebuild.

## Parameters

All fields are optional, but at least one must be present (or `reset: true`).

**Persona prose**

| Name | Type | Notes |
|---|---|---|
| `identity` | string | **Replaces** the persona prose entirely. Non-empty, ≤4000 chars. |

**Lists** — one item per call, each list capped at 12 items. Adds are
idempotent and removes match case-insensitively.

| Name | Type | Notes |
|---|---|---|
| `add_catchphrase` / `remove_catchphrase` | string | Signature lines. Add is capped at 120 chars. |
| `add_principle` / `remove_principle` | string | Add is capped at 200 chars. |
| `add_never_do` / `remove_never_do` | string | Add is capped at 200 chars. |

**Per-topic intensity**

| Name | Type | Notes |
|---|---|---|
| `set_intensity` | object | Non-empty map of topic slug → `off` \| `low` \| `medium` \| `high` \| `brutal`. Slugs must match `^[a-z0-9_]{1,40}$`; `known_topics` from the read tool lists the named ones. **`medium` clears the override** rather than storing one. Max 16 stored topics. |

**Numeric dials** — written to their own settings keys, never duplicated into
the spec.

| Name | Type | Range | Notes |
|---|---|---|---|
| `harshness` | integer | 0–10 | Prose calibration hint. At or above the harsh-block threshold the goal-based mandates assemble. |
| `warmth` | integer | 0–10 | |
| `push` | integer | 0–10 | |
| `roast_threshold` | number | 0–1.20 | Fraction of goal below which the tone hardens. |
| `praise_threshold` | number | 0–1.20 | Fraction of goal above which to celebrate. |

**Reset**

| Name | Type | Notes |
|---|---|---|
| `reset` | boolean | Alone: deletes the stored spec — back to the shipped profile. With other spec fields: re-seeds from the active profile, *then* applies the patch. Either way the dials are untouched. |

## Returns

A patch that changed the spec:

```json
{
  "updated": true,
  "profile": "hardass",
  "customized": true,
  "spec": {
    "base_profile": "hardass",
    "identity": "…",
    "catchphrases": ["The log doesn't negotiate."],
    "principles": [],
    "never_do": [],
    "intensity": {"step_goal_nagging": "off"},
    "updated_at": "2026-07-26T07:41:02"
  },
  "dials_changed": []
}
```

A bare `reset`:

```json
{"updated": true, "reset": true, "profile": "hardass",
 "customized": false, "dials_changed": []}
```

A dials-only call omits `spec` and lists the settings keys it wrote:

```json
{"updated": true, "profile": "hardass", "customized": true,
 "dials_changed": ["coach_harshness", "coach_warmth"]}
```

Errors are **collected, not short-circuited** — every problem in the call comes
back at once, `; `-joined, alongside the full editable vocabulary so the retry
can be correct:

```json
{"error": "harshness must be an integer 0-10; bad intensity level 'savage' for 'sleep' (one of ['off', 'low', 'medium', 'high', 'brutal']); unknown field 'tone'",
 "editable_fields": ["add_catchphrase", "add_never_do", "add_principle", "harshness", "identity", "praise_threshold", "push", "remove_catchphrase", "remove_never_do", "remove_principle", "reset", "roast_threshold", "set_intensity", "warmth"],
 "intensity_levels": ["off", "low", "medium", "high", "brutal"]}
```

Other failures: `nothing to update — pass at least one editable field` when the
call carries no actionable field, and `personality spec would exceed 8192
bytes — trim the identity or the lists first` when the serialized spec is too
big.

## Example

> "Stop nagging me about the step count, but get *worse* about missed quality days."

```json
{"set_intensity": {"step_goal_nagging": "off", "quality_day_misses": "brutal"}}
```

```json
{"updated": true, "profile": "hardass", "customized": true,
 "spec": {"base_profile": "hardass",
          "intensity": {"step_goal_nagging": "off", "quality_day_misses": "brutal"},
          "updated_at": "2026-07-26T07:41:02", "…": "…"},
 "dials_changed": []}
```

## Gotchas

- **Dials are written before the spec size check, so a rejected call can still
  have applied them.** `db.set_setting` runs for each dial, *then* the spec is
  built and its 8 KB cap enforced. An over-cap error therefore leaves the dials
  changed and the prose unchanged — re-read with
  [`get_coach_personality`](get_coach_personality.md) after any error rather
  than assuming the whole call rolled back. (Validation errors are the clean
  case: those return before anything is written.)
- **`identity` replaces; it does not append.** Passing a sentence about hills
  will leave the coach's entire persona as one sentence about hills. To extend,
  read the current identity, edit the text, and send the whole thing back.
- **A 13th list item is dropped silently.** `apply_patch` appends then slices to
  12, so an add past the cap succeeds with `updated: true` and the item is
  simply not there. Check the returned `spec` rather than trusting the flag.
  Same shape for intensity at 16 topics — the cap keeps the *oldest* overrides
  by insertion order.
- **`medium` is a delete, not a setting.** `set_intensity: {"sleep": "medium"}`
  removes any stored override for `sleep`, returning it to the profile's default
  treatment. It will not appear in the returned `intensity` map afterwards.
- **`reset: true` does not touch the dials.** "Put you back to normal" almost
  always means both — reset *and* the dials the user moved. Ask, or set them
  explicitly in the same call.
- **The spec is stamped with the active profile.** Every write sets
  `base_profile` to whatever profile is live. Switch profiles afterwards and the
  spec is **ignored but retained**
  (`base_profile_mismatch: true` on the read tool); switch back and the tuning
  returns intact.
- **Removes are case-insensitive; adds are case-insensitively deduped.** Adding
  a line that differs only in case is a no-op, and it still returns
  `updated: true`.
- **`LOCAL_FITNESS_COACH_SPEC=0` is the kill switch for a bad tune** — it
  ignores the stored spec without deleting it, so a rollback costs no data.
  Writes still succeed while it's set; they just don't take effect.
- **Custom topic slugs are allowed and not spell-checked.** `step_goal_naging`
  passes the slug regex and silently does nothing forever. Prefer a slug from
  `known_topics`.
- Not reachable from the scheduled brief loop — the brief composer's
  allow-list is read-only.

## See also

- [`get_coach_personality`](get_coach_personality.md) — read current state first; also confirms an edit landed
- [`save_user_note`](save_user_note.md) — the layer that *outranks* the spec (notes > spec > profile file)
- [`save_coach_memory`](save_coach_memory.md) — what the coach remembers, as opposed to how it talks
