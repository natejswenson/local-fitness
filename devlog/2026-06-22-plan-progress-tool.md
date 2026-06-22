# 2026-06-22 — The agent stopped spelunking the DB by hand

I asked my own coach "show me my training plan completed through today," and
watched it drop to `sqlite3` in a Bash shell — `PRAGMA table_info`, two SQL
errors, raw column dumps — before finally producing a table. The mechanics I
didn't care about were louder than the answer I did. So I fixed the thing that
caused it.

## Two gaps, not one

The reflex diagnosis was "format the output better." Half right. The real story
was two compounding gaps, and a design pass (`/design` → `/quality-gate`)
forced me to see both:

1. **There was no tool to call.** `get_training_plan_status` returns a slim
   summary — today's session plus the single most-recent graded day. The full
   graded plan, day-by-day, already existed in `build_plan_detail()`, but it was
   wired only to the web tab, never exposed as an MCP tool. With nothing clean to
   call, the agent improvised with raw SQL. No amount of "format nicely" would
   have fixed that — it needed a tool.

2. **Nothing told it not to spelunk.** The chat-formatting discipline that keeps
   the brief clean only loads via the `/coach` MCP prompt. A normal in-repo
   question never sees it.

Neither lever alone fixes the experience. So: add the tool (the load-bearing
fix), and add an advisory nudge in the two places that actually reach the
surfaces — `CLAUDE.md` for in-repo Claude Code, the existing chat block in
`system_prompt()` for MCP clients.

## What the quality gate caught

Six adversarial rounds (score 5 → 0). It never found a Fatal, but it did catch
real defects my first draft hand-waved: the tool is a *deliberate projection*,
not a "thin mirror" — `build_plan_detail` needs a `best_effort` arg, returns
fields the API surface shouldn't, doesn't compute `days_to_race`, and has no
no-plan guard. It also caught a false bug-rationale that one of my own fix rounds
introduced (a frontier-window "spurious missed" scenario that's structurally
unreachable — the change is fine as a *parity* rule, the justification was
wrong). The substance settled by round 2; the rest was the gate sanding down
verification prose until it was honest.

## The honest footnote on verification

The prompt edit lives in the shared `system_prompt()`, so per my own rule it has
to clear the cross-model brief A/B. The A/B harness errored — but it errored
*identically* with my edit stashed, so the failure is a pre-existing flake in
`ab_brief.py`'s `_generate` path (the eval-mode brief model sometimes replies
"Brief saved…" in prose instead of raw JSON), not my regression. The real
`uv run fitness brief` path works. Static scorer green. I verified attribution
by differential testing rather than eyeballing a green checkmark I couldn't get.

That harness bug is now on the list. Not today's problem.
