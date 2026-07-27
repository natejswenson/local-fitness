# 2026-07-26 — Accuracy pass: wrong numbers stop reaching the coach (0.35.0)

## Why

A three-axis audit (speed / UX-accuracy / maintainability, three parallel
read-only agents over the whole tree) surfaced ~35 evidence-backed findings.
This is batch 1 of 4: the accuracy class — places where a number the coach
speaks with full confidence was simply wrong, or where two surfaces
disagreed about the same day. The worst of them compounded: `sync_garmin_data`
returned `is_error: true` for any user with a single missing historical day
(*and* skipped the CTL/ATL/TSB recompute, so every downstream tool served
frozen training load while claiming currency), and the brief planner counted
walking-desk sessions as runs — the exact `treadmill_running` label lie the
rest of the app was fixed against in 0.27.0 — so the brief could never say
"you haven't run in eight days."

## What

Ten fixes (A1–A10), implemented by four parallel agents on disjoint file
groups, each with value-pinning tests written against the bug first:

- **A1 sync honesty** — `partial` is a normal payload with a deterministic
  `sync_state` line + `days_failed`; recompute fires on any landed data;
  only `auth_failure`/`not_configured`/`failure`/`interrupted` are errors.
- **A2 pace-gated brief signals** — `brief_planner._running` now mirrors
  `plans._ran`: on-foot label first (pace alone would promote a fast bike
  ride), then `interpret.is_running_effort`, label fallback only for
  paceless rows.
- **A3 scoped disclaimer** — a plan-graded card with thin history no longer
  prints "Not enough comparable history to grade" under a real grade; the
  caveat names only the actually-ungraded metrics.
- **A4 prescription keys** — `workout_coach._describe_prescription` read
  `distance_mi`/`pace_min_per_mi` off rows that carry `target_distance_m`/
  `target_pace_sec_per_km`; every number silently dropped from the
  "setting up for" block. One-time read-cache bust per card.
- **A5 double days** — the date branch graded the day's *last* session
  against the *first* session's prescription; now first-vs-first, and
  `other_activities_on_date` identifies the ungraded session instead of
  returning bare ids.
- **A6 manual-lap intervals** — `fastest_rep_split` selects by a 300 m
  floor (`QUALITY_MIN_SPLIT_M`) instead of the `partial` flag, which was
  relative to the workout's own longest lap and graded 800 m reps at
  2-mile-warmup pace (guaranteed F on correctly-run interval sessions).
- **A7 projection basis** — Riegel predictions carry
  `projection_basis` + `projection_confidence`; `best_recent_effort` is
  pace-gated and prefers efforts ≥ goal/4 with a labeled low-confidence
  fallback to 2 km rather than dropping the projection.
- **A8 honest adherence** — `sessions_adherence_pct` + `rest_days_counted`
  beside the untouched `adherence_pct`; a week of kept rest days and
  skipped runs can no longer headline 43% unqualified.
- **A9 dated `last_graded`** — `_slim_workout` carries `date`/`seq`.
- **A10 brief context freshness** — `continuity` populates over MCP
  (the tool never passed `recent_briefs` in), and `BriefContext` gains
  `data_frontier`/`baseline_stale_days`/`brief_stale_days`/`tsb_zone` —
  all optional, all inside the existing single connection (the ==1-connect
  perf gate still holds).

## Gotchas

- The perf gate's connect-count assertion is the reason A10's four fields
  ride the existing conn — `brief_stale_days` comes from a *filesystem*
  listing precisely because a second `db.connect()` would fail the gate.
- The `partial`-status set had a hidden member: `interrupted` can end a
  pull with `error=None`, which would have fallen through to a success
  payload — the error branch needs its own fallback message.
- Grounding was checked and NOT tripped by the new BriefContext integers
  (`tests/test_grounding.py` + `tests/evals` green with the fields flowing
  into the V2 prompt via `exclude_none`); the exclude-from-prompt fallback
  in the plan was not needed.
- Agents ran a falsification protocol: stash the source fix, re-run the new
  tests, require failures, restore. Sync: 13 fail pre-fix; brief: 7.
