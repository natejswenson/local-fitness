# Codex coaching and fitness operational repair

## Problem and scope

The brief can use Codex, but plan coaching, workout commentary and journal
reflection still unconditionally call Claude. A deployment without Claude access
therefore saves briefs but loses those features. Route these three existing
single-shot generators through the selected subscription provider. Do not change
prompts, grades, plans, memory ownership, or delivery recipients.

## Implementation

- Extend the existing isolated Codex transport with a strict text-output schema.
- Resolve `LOCAL_FITNESS_COACH_PROVIDER`, falling back to the brief provider and
  then Claude for fresh clones. An explicit model belongs to the selected provider;
  Codex otherwise resolves its own optional coach-model override, then brief-model
  override, then CLI default. Never pass a Claude default model to Codex.
- Route each generator before constructing Claude options. Keep subprocess
  deadlines, missing/invalid output failures, four-section parsing, and fallback
  behavior. No tools or MCP are granted to Codex.
- Include the Codex provider/model selection in plan and workout cache identity;
  preserve existing Claude keys for compatible deployments.
- Isolate ambient memory backend settings in tests, as the network integrations
  already are, while allowing dedicated vault tests to explicitly opt in.
- Document the provider settings and bump the implementation version.

## Verification and adversarial review

Baseline: 2,929 tests passed with 95.35% coverage under explicit legacy test
backend settings. An unisolated run reproduced a live-memory transport failure.
Check that Codex never receives a Claude model, an invalid provider cannot reuse
a cache, changed Codex models invalidate caches, malformed/empty text fails, and
failed/unparseable generations remain uncached. Exercise all three real entry
points with fabricated input, inspect the actual subprocess schema/arguments,
and retain the existing Claude-option tests. Run the full suite, Ruff and prompt
scorer. This checkout has no `.dev/preview.json`; use its existing local checks.
Keep feature work separate from the canonical shared runner. Delivery through
the protected dev branch must precede refreshing canonical runner source.

Operational audit: Garmin sync, database integrity, memory reads, SMTP TLS/login,
email dry-run, Calendar OAuth/read and HTTPS/MCP authentication passed. After
delivery, verify real Codex coaching/reflection and canonical runner health,
without manually sending email or changing calendar prescriptions.
