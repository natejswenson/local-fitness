# 2026-07-23 — Report cards become durable coach memory (0.32.0)

Report cards were the richest judgment the coach produced and the least
durable: four graded metrics, an intent-aware overall, a four-paragraph
verbal read — all recomputed on demand and thrown away after render. The
only trace was 0–2 reflected journal lines. Asking "how did my last five
interval days grade" meant re-rendering each card (~10s of SDK call per
cache miss, single-entry cache), and the phone couldn't render cards at all.

## The decision that shaped everything

Grades are **not recomputable history** — `build_card` grades the plan half
against the *currently active* plan, so re-grading an old run after a plan
change produces a different card. So a stored card is a **dated snapshot**:
the card actually shown, as graded on `graded_at`. No backfill (747
historical activities would grade against today's plan — actively wrong);
history accumulates as cards render.

## The mechanic: key identity decides the save

One atomic guarded UPSERT (`agent/card_store.py`), keyed on the read's
prompt hash (`workout_coach.read_cache_key`, the factored single key
definition):

- equal key → byte-identical no-op (a fast-path or file-cache hit never
  lands this render's recomputed grades under the stored render's words)
- new real key → whole fresh row (words + grades from one render)
- fallback (NULL key) → no-op over a real-read row, save otherwise

No SELECT-then-write window, `busy_timeout=5000`, awaited via
`asyncio.to_thread` so the stdio transport never blocks. Fail-silent: a
save failure costs the row, never the render.

## Gotchas found by the 6-round quality gate

- The template fallback and a real read are **structurally identical
  dicts** — fallback-ness is knowable only from the caller's try/except
  branch, never from shape.
- Field-level "carry the good read forward" creates mixed-render rows (old
  words above new grades — the exact grade-contradicts-prose failure this
  subsystem was hardened against). Whole-row-or-nothing is the only safe
  write.
- `model or "default"` — the literal, not `DEFAULT_MODEL` — is in the key's
  byte layout; a factored helper that "cleaned that up" would silently
  disable read reuse forever.

## What Nate gets

`list_report_cards` (trend any workout class in one instant call) and
`get_report_card` (the verbatim card + coach read from that day) — both in
`ALL_TOOLS`, so the phone gains full card-history Q&A. Re-rendering any old
card's PDF reuses its stored read: no more ~10s per alternation.
