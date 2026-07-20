# Facet-review backlog clearance (0.25.0)

**2026-07-19**

Third and final tranche of the day's facet-review loop (0.23.0 resilience →
0.24.0 round-2 fixes → this). Cleared every deferred item except the ones
deliberately closed as no-fix.

## Shipped

- **seq (double-day) support**: `update_plan_workout` takes `seq` (1=AM
  default, 2=PM); progress payloads carry `seq`. Validation rejects
  `seq<1` — note the classic `args.get("seq") or 1` footgun silently
  coerced `seq:0` to 1; the explicit None-check version is what's shipped.
- **Partial unique index** `idx_plan_workouts_day` on
  (plan_id, date, seq) — `CREATE UNIQUE INDEX IF NOT EXISTS` in the
  `init_schema` script, so existing DBs pick it up on next start.
- **Chart cosmetics D2/D3**: flat non-zero bars paint mid-heat 🟨 (was
  coldest-blue full bars reading as "low"); short-window line charts drop
  the end label when both can't fit under the canvas.
- **A/B-gated prompt edits** (the round-2 drift findings that live in
  prompt text): the "read-only access" self-contradiction in
  `system_prompt` (now: Garmin metrics read-only, writes via dedicated
  tools) and the "so the UI can render" phrasing in both brief prompts
  (UI retired 2026-07-09).

## How the prompt edits were verified (the gate, per policy)

`ab_brief.py --run` is pre-existingly flaky (see memory), so per the
documented alternative: `scripts/score_prompt.py` 11/11 both before and
after, plus a **differential rendered-prompt diff** — all four prompt
surfaces (`system_prompt`, `briefing_prompt`, `brief_v2_system_prompt`,
`brief_v2_user_prompt` with a minimal `BriefContext`) rendered to files
before and after the edit; the diff showed exactly the three intended
wording changes and zero collateral drift. That's the blast-radius proof;
the scorer is the contract proof.

## Closed as no-fix

- LOW-1 (progress `workouts:[]` on old plans without `full=true`) —
  documented foot-gun, description + CLAUDE.md both flag it.
- The 07-06/07-07 "tool didn't exist" session findings — resolved by
  tools shipped 07-09.

## Loop status

The facet-review → implement → ship loop is now drained: three releases in
one day, all landed on dev, container rebuilt each time, launchd job
reinstalled with the 09:30 backstop. Remaining watch item: the morning
brief hit-rate this week (first real 06:30 + 09:30 exercise of retry +
backstop).
