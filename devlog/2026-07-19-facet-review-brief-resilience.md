# Facet review → brief resilience, failure visibility, honest monitors

**2026-07-19**

A four-facet review (accuracy / completeness / efficiency / agent UX) of
three weeks of real evidence — extracted Claude session transcripts, the
launchd job logs, and the briefings directory — each facet run as an
independent reviewer over the same evidence pack, findings synthesized into
one change set shipped as 0.23.0 on `feature/facet-review-resilience`.

## What the review found

- **The nightly brief was failing ~half its runs, silently.** Only 4 of the
  last ~9 mornings produced a brief (07-11/13/15/16); 07-17/18/19 were three
  consecutive missed days. Root cause settled by cross-facet evidence: NOT a
  dead credential (every failed run had `ttfm_ms` ≈ 1–2.5s — the SDK
  connected fine; successes and failures interleaved day to day) but Agent
  SDK stream instability, in two sub-modes: an idle stream that exits
  "normally" with `chars=0` after burning 3–5 minutes (~38 min wasted over
  21 days, zero output), and a subprocess crash mid-stream discarding
  partial takeaways. One attempt per night, no timeout, no retry, and the
  only user-visible failure signal was the *absence* of the morning
  notification.
- **The empty-output error lied.** `chars=0` fell into the JSON parser and
  surfaced as "no JSON found in agent response:" — and CLAUDE.md's
  remediation note blamed the credential, so the documented fix (re-mint
  the token) would never have helped.
- **The grounding monitor was saturated and useless.** Every logged brief:
  `invention_rate=1.000`, all false positives from cross-unit magnitude
  collisions (HR cap 140 bpm matching a 147% steps-vs-goal value inside the
  12% NEARBY band). Pinned at 1.0, it could never catch a real invention.
- **9 identical SDK calls in one day** from repeat PDF renders — the
  coaching line regenerated fresh per render with byte-identical inputs.
- Verified healthy (no fix needed): pull path gap-awareness, MCP tool
  payload sizes, tool error ergonomics (`allowed=[...]` enum returns),
  saved-brief numeric accuracy (spot-checks all exact), miles rendering.

## What shipped (0.23.0)

- **Stream resilience** (`briefing.py`): `_iter_with_idle_timeout` watchdog
  (120s per-message gap → `BriefStreamIdleTimeout`, fail fast instead of
  minutes of nothing) + bounded retry in `generate_and_save` (3 attempts,
  20s apart — the failures are transient, a fresh attempt routinely
  succeeds). Env knobs: `LOCAL_FITNESS_BRIEF_IDLE_TIMEOUT_S` /
  `_BRIEF_MAX_ATTEMPTS` / `_BRIEF_RETRY_DELAY_S`.
- **Honest diagnostics**: empty output now reports "generator produced no
  output — stream died" before the parser ever runs; CLAUDE.md's failure
  signature rewritten to distinguish credential-missing (no first message)
  from stream death (ttfm present, then empty/partial).
- **Failure visibility**: `fitness brief` fires a distinct macOS FAILURE
  notification and exits non-zero; `assemble_status()` carries
  `latest_brief_date` + `brief_stale_days`; `fitness://brief/latest` leads
  with a STALE banner when serving yesterday's (or older) brief.
- **Grounding un-saturated**: kind-partitioned matching (percent tokens vs
  percent pool values, plain vs plain) kills the cross-unit false-positive
  class while same-kind near-misses still flag.
- **Coaching-line cache**: `generate_coaching_line_cached`, single-entry
  disk cache keyed on the pure `build_prompt` hash; failures never cached.

## Deferred to backlog (found, not shipped)

- Plan-vs-actual two-series chart tool (agent currently hand-rolls
  matplotlib via Bash for "scheduled vs actual" asks — risks the
  charts-render-in-reply rule).
- launchd-level second `StartCalendarInterval` as a belt-and-braces retry.
- Partial-takeaway salvage at finalize (retry makes it mostly moot; salvage
  would need a degraded-brief schema decision).
- Bar-style long-window renderer (a "90-day bar graph" ask was served with
  `combo` because bar caps at ~2 weeks).

## Gotchas

- `asyncio.wait_for` uses the event-loop monotonic clock, which — like
  `time.perf_counter` — does not tick while the laptop sleeps. The 07-19
  run showed 73 min of wall-clock against 265s measured: sleep mid-stream
  inflates apparent hangs. The watchdog still fires within 120s of *awake*
  time, which is the budget that matters.
- Existing tests that monkeypatch `generate_coaching_line` needed their
  fakes to accept the `model=`/`timeout=` kwargs the cached wrapper passes
  through — a TypeError there silently triggered the fallback line and
  failed the PDF assertions two tests downstream.
- `GroundedValue.unit` is a frozen literal set (`"pct"`, not `"%"`) —
  worth remembering when fabricating grounding fixtures.
