# 2026-05-06 — heatmap as the Today top card

The activity heatmap is the favorite view; it now sets the visual
frame on the daily brief instead of being buried under `/dashboards`.

## What landed

- New `<ActivityHeatmap days={N} highlightToday />` reusable component
  in `web/src/components/ActivityHeatmap.tsx`. Self-contained — fetches
  its own data, manages hover state, computes ranking, renders the
  grid + totals + rich tooltip.
- `Dashboards.tsx` slimmed: the heatmap panel now wraps
  `<ActivityHeatmap />` with the existing range toggle + chat insight.
  The `HeatmapGrid`, `HeatmapTotals`, `ScaleLegend`, `MS_DAY`,
  `startOfDayUTC`, and `rangeWindowLabel` helpers moved into the new
  file. Net code in Dashboards.tsx: ≈220 lines smaller.
- `Today.tsx` mounts a new "Year at a glance" card directly between
  the stale-brief banner and the Key Takeaways grid, rendering
  `<ActivityHeatmap days={365} highlightToday />`.
- `highlightToday` prop adds an accent-stroke ring (1.5px) to today's
  cell so the eye instantly maps "where we are right now" against the
  full year.

## What I deliberately didn't do

- **No chat-seeding chips on the Today version.** They live on the
  Dashboards page; on Today, the heatmap is meant to set tone, not
  compete with the brief's takeaways for attention. The user can
  navigate to `/dashboards` for the analytical conversation.
- **No range toggle on Today.** Fixed at 365d. The "year at a glance"
  framing is the point.
- **No data refetch optimization.** Today and Dashboards each fetch
  their own copy of `/api/activity-heatmap?days=365`. They're never
  mounted at the same time (different routes), so no memo is needed.

## Verification

- `pnpm tsc --noEmit` + `pnpm build` clean.
- `uv run pytest -x` 11/11.
- Container rebuilt healthy on first probe.
- Playwright on `/`:
  - "YEAR AT A GLANCE" label rendered ✓
  - 365 heatmap rects on Today page ✓
  - Exactly 1 cell carries an accent stroke (today's cell) ✓
- Visual: today's cell is the bright-green-ringed rest cell at the
  right edge of the Wednesday row; totals strip below reads
  "195 activities across 183 active days · cumulative load 12411 ·
  hover any day for full stats".
