# 2026-07-23 — Tunable personality + the accountability mirror (0.31.0)

Second half of the memory build (0.30.0 gave the coach memory; this gives
it a self). Two changes: the personality became a conversationally-edited,
DB-stored spec, and the shipped default became a rewritten hard-ass.

## The spec

`agent/personality.py` — a bounded `PersonalitySpec` (identity prose,
catchphrases, principles, never-do, per-topic intensity) stored as JSON in
the settings table and edited only through `update_coach_personality`.
"Coach is too repetitive about the step goal" → `set_intensity:
{step_goal_nagging: low}` → live on the next render. The profile `.md`
files are now seeds: virtual seeding means an untuned clone renders
byte-identically to before, and the first tune materializes
seed-plus-patch.

Load-bearing details:

- **The spec rides the existing settings read.** `resolve_coach_profile`
  already fetched `all_settings()`; parsing the spec out of that dict adds
  zero connections to a path that runs at every MCP connect.
- **`personality` must not import `coach`** (coach imports personality) —
  `seed_from_profile` is duck-typed.
- **Mismatch = ignored, not deleted.** A spec tuned for hardass while
  supportive is active is retained; switching back restores the tuning.
- **Everything is capped** (8 KB spec, 4000-char identity, 12-item lists,
  16 topics) because the spec feeds every prompt forever.

## The default

`DEFAULT_PROFILE = "hardass"`, and `hardass.md` grew from 5 bullets into a
real personality — the accountability mirror. Original persona, no real
coach named or imitated; the ethos: the log doesn't lie, motivation is
weather / discipline is climate, the miss you explain is the miss you
repeat. Its "Using your memory" section is what makes 0.30.0 pay off — it
spends the ledger as receipts, names patterns, and collects on promises,
under a hard NEVER-invent rule. Frontmatter (9/1/10 · 1.00/1.05) is
unchanged so both harsh-block gates work untouched.

## Gotchas

- `config.coach_profile()`'s default is a copied literal (config↔coach
  cycle); a cross-check test pins it to `coach.DEFAULT_PROFILE`.
- `scripts/score_profiles.py` pins the hardass persona's accountability
  markers — a persona rewrite must update the marker list in the same
  commit or CI fails.
- `prompts.DEFAULT` replaced `ADAPTIVE` as the default-arg constant
  (alias kept); the scored default prompt is now the hardass rendering.

1603 tests green, 94.9% coverage, score_prompt 11/11,
score_profiles 27/27.
