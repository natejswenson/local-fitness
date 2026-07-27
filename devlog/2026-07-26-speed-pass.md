# 2026-07-26 — Speed pass: the persona tax, the sync sleeps, the N+1s (0.36.0)

## Why

Batch 2 of the three-axis audit. The speed findings shared a shape: work
repeated per-request or per-row that a key, a cache, or a range query does
once. The headline was the MCP persona — in stateless HTTP mode the server
re-resolved the entire coach persona (6 `db.connect()` opens plus a full
relationship-ledger compute, ~2.7 ms) on *every single request*, roughly 5×
the cost of the `get_metric`-class tool call it decorated.

## What

- **S1 persona memo** — `(today, db_path, PRAGMA data_version, notes stat)`.
  `data_version` over MAX(rowid) because settings UPSERTs and journal
  archived-flips UPDATE in place; a rowid key silently misses exactly the
  writes that change the persona. Monitor connection is read-only and
  path-aware; failures and the no-DB fresh-clone path never cache.
- **S2 column pruning + index** — `SELECT *` was dragging 50 KB `raw_json`
  blobs through a temp B-tree to pick ONE activity row (7.7 ms measured →
  ~0.2 ms pruned, ~0.002 ms with `idx_activities_date_start`).
- **S3 single-pass baselines** — per-day AVG + duplicate SD scans → one
  fetch + rolling window + `executemany` (3 statements at any lookback,
  pinned by a statement-count test and a bit-identical-vs-old-SQL oracle).
  Kept `_sd` over a deque instead of sum-of-squares — the shortcut
  catastrophically cancels on near-constant windows (identical sleep values
  would print an SD of 2.7e-4, or go negative).
- **S4 sync fast-path** — stored past activities skip zone/split re-fetches
  and their 0.3 s sleeps; the 0.5 s day-throttle no longer fires after the
  final day; one connection for the run with per-day commit/rollback.
- **S5–S11** — plan_coach multi-entry cache (the two-date thrash), lazy V1
  memory in the brief, deferred garminconnect import (28 ms off every stdio
  session start), recovery_pattern N+1 → 3 range queries, one-conn
  read pipelines for card/report/reflect/personality, lru_cached profile
  files, `json_extract` card reads, shared WeasyPrint font/image caches
  across the one-page ladder.

## Gotchas

- **Four subagents hit the session usage limit mid-implementation.** The
  fleet landed roughly half the work (S2, S8 handler-side, S11, half of S3,
  S9) before dying; the rest was finished solo in the main session. Partial
  multi-agent output was coherent enough to build on directly — the file-
  disjoint work split is what made that safe.
- `PRAGMA data_version` only moves for commits by OTHER connections — the
  monitor must never write, and it must re-open when `db.get_db_path()`
  changes or it watches a dead file (a real bug found by the test suite's
  per-test tmp DBs, which would also have bitten any runtime path change).
- WeasyPrint's per-call `FontConfiguration` is deliberate: each registration
  writes a temp font file, so a module-global config on a long-lived server
  accumulates them without bound.
- The CI perf baseline was recaptured this same day (runner-fleet drift —
  see CLAUDE.md's rebaseline note); these wins ride on top of the fresh
  floor rather than masking inside the stale one.
