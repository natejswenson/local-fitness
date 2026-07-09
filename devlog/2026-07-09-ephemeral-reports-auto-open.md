# Ephemeral, auto-opened PDF/chart reports

**2026-07-09 · v0.19.0**

Nate flagged that generating a daily report or a chart via the opencode
"fitness" agent felt clunky: after the terminal/ASCII version rendered,
getting the "pretty" PDF/PNG version required a second explicit ask, and
even then the file just landed in `./reports/` — nothing opened it, nothing
ever cleaned it up.

Digging into the actual opencode session that prompted this also surfaced a
separate, already-fixed bug: `generate_brief_report` was failing outright
with a WeasyPrint `libgobject-2.0-0` load error because `.env` never set
`DYLD_LIBRARY_PATH` for Homebrew's Pango/HarfBuzz libs on macOS.

## Design decisions (from a Claude Code plan-mode session + a 12-round
quality gate)

- **Ephemeral-by-default, override-for-persistent.** `generate_brief_report`/
  `generate_chart` now default to a per-process `tempfile.mkdtemp()`
  directory instead of the persistent `./reports/`. `LOCAL_FITNESS_REPORTS_DIR`
  still works exactly as before as an explicit opt-out.
- **PID-embedded naming + liveness-checked sweep**, not lock files. The
  directory name embeds the owning process's PID
  (`local-fitness-reports-<pid>-<random>`); a best-effort sweep on each
  process's first call removes only directories that are BOTH >24h old AND
  whose embedded PID is dead (`os.kill(pid, 0)` raising
  `ProcessLookupError`) — this avoids a pure age-based sweep deleting a
  live, long-idle session's directory out from under it. Zombie-PID and
  PID-reuse edge cases can still extend leakage beyond one stale session in
  rare cases; not worth a real liveness registry for a personal,
  single-user tool.
- **`threading.Lock`, not `asyncio.Lock`, guards the memoization cache.**
  `_default_reports_dir()` is dispatched via `asyncio.to_thread` (so its
  filesystem I/O never blocks the event loop), which means its body runs on
  real OS worker threads — an `asyncio.Lock` wouldn't coordinate across
  those. A `threading.Lock` around the whole check-then-set-and-create
  sequence closes a real race where two concurrent tool calls could each
  create their own ephemeral directory. Verified with a genuine
  multi-thread test (`ThreadPoolExecutor` + `threading.Barrier`), not just
  sequential calls.
- **Auto-open is unconditional and best-effort.** After a successful write,
  both tools call macOS `open` on the result — stdout/stderr redirected to
  `DEVNULL` (this process's stdout is the live JSON-RPC framing channel for
  the whole stdio MCP session; anything `open` wrote there would corrupt
  it), non-zero exit codes logged (not just exceptions — `check=False`
  never raises on a bad exit code), and a deliberate ~1.5s grace-period
  sleep after `open` returns to narrow (not eliminate) a race between
  process-exit cleanup and the still-loading Preview window.
- **`_default_reports_dir()`'s new I/O gets the same clean-error contract
  as everything else in the file** — `tempfile.mkdtemp()` can now raise
  `OSError` where it never could before, so both call sites catch it and
  return a normal `_err(...)` response instead of a raw exception.

## What the quality gate caught

Twelve rounds of adversarial review (weighted score trajectory: 3 → 8 → 3 →
1 → 2 → 2 → 2 → 1 → 1 → 0 → 1 → 0) surfaced and fixed several real issues
along the way: a genuine cross-thread race in the memoization cache once
the directory-resolution function moved onto a worker thread; a personal
home-directory path that would have leaked into the public `.env.example`;
a `subprocess.run` call that didn't redirect stdout/stderr despite running
inside a stdio-transport server; a manual smoke-test script that was
actually broken (missing a required schema field); and a test suite gap
where mocking `subprocess.run` alone didn't stop a real 1.5s sleep from
firing on every pytest run.

## Follow-up outside this repo

The opencode "fitness" agent's own prompt
(`~/.config/opencode/opencode.jsonc`, a personal dotfile, not part of this
PR) needed a matching update so it actually calls `generate_brief_report`/
`generate_chart` proactively instead of only on explicit request — applied
directly after merge.

## Explicitly out of scope

- The DeepSeek `save_brief` schema-retry friction observed in the same
  investigated opencode session (needed 3 attempts to hit the right field
  names) — a separate, weak-model tool-use issue.
- PNG→PDF conversion for `generate_chart` — confirmed with Nate that PNG
  (opens fine in Preview) satisfies the original ask; no format change.
