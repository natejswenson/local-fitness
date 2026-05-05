# 2026-05-05 — custom dashboards

Three new dashboards under `/dashboards`. First feature work after the
public-prep + security-hardening sequence — kept to the env-driven
pattern in `CLAUDE.md` (no new env vars needed since these are pure
read-only views; auth and rate-limit inherit from the middleware).

## What landed

### Backend

Three new GET endpoints in `web/server.py`, all auth-gated by the
existing middleware via the `/api/` prefix. No new SQL surfaces — every
query uses parameterized values, and the only column-name interpolation
is a static activity-type LIKE pattern list.

- `GET /api/activity-heatmap?days=N` → per-day aggregate
  `{date, activity_count, total_load, total_duration_seconds, dominant_type}`.
  Returns rows only for active days; the frontend fills the calendar.
- `GET /api/strength-volume?weeks=N` → weekly buckets
  `{iso_week, week_start, sessions, total_duration_min, total_load,
  total_calories}`, plus `last_session_date` and `total_sessions`.
  Defaults to 104 weeks because Instinct Solar's strength logging is
  sparse; short windows often look empty even when historical data is
  rich.
- `GET /api/pace-efficiency?days=N&min_distance_km=2` → per-run
  `{date, avg_hr, avg_pace_sec_per_km, distance_meters,
  hr_per_kmh, tsb, ctl, atl}`. Filtered to running-family activities
  with both HR and pace recorded; `min_distance_km` keeps treadmill
  warm-ups out of the trend.

### Frontend

- `web/src/components/Dashboards.tsx` (~440 lines, single file
  containing the page + three panels). Same Card/RangeToggle pattern
  as `Trends`; same Recharts + CSS-vars approach for theming.
- New `/dashboards` route in `main.tsx`.
- Sidebar entry (Today / Trends / **Dashboards**), `LayoutGrid` icon.
- `lib/api.ts` and `lib/types.ts` extended with the three new methods +
  response shapes.

#### Activity heatmap

Hand-rolled SVG calendar (52 weeks × 7 rows for the 1y view, scales for
shorter / longer ranges). Colour ramp goes from `surface-2` (rest day)
through green to deep red, scaled against the max load in the visible
window. Hover footer surfaces per-day detail; the totals strip on the
right shows cumulative active-days + cumulative load. No new chart
library — keeps the bundle lean.

#### Pace efficiency & fatigue

`ComposedChart` with two y-axes:
- **Left axis:** per-run dots (sparse) + a 5-run rolling average line
  for `hr_per_kmh = avg_hr × pace_sec_per_km / 3600`. Lower = better
  (less HR for the same speed). Rising trend on the rolling line is
  the fatigue signal.
- **Right axis:** dashed TSB overlay (Banister Training Stress
  Balance). Negative TSB = accumulated fatigue. The two lines together
  let you see whether rising HR/pace ratio tracks intentional load
  build-up or unrecovered drift.

Filtered server-side to running activities ≥ 2 km; treadmill warm-ups
under 2 km don't pollute the trend.

#### Strength volume

Bar chart of weekly session count. **Empty-state copy is intentional**:
"Instinct Solar doesn't record sets/reps/weight, so this tracks
frequency + duration only." Default window is 2y; the 5y toggle
reveals the older sessions (last one was 2022-02-17 — the watch's
strength tagging hasn't been used much).

## Verification

- `uv run pytest -x` — 11 / 11 passing (4 smoke + 7 security; new
  `test_dashboards_require_auth` covers the three endpoints + auth
  regression).
- `pnpm build` + `pnpm tsc --noEmit` — clean.
- `docker compose up -d --build local-fitness` — recreated fresh,
  healthy on first probe.
- Curl through `https://fitness.home.local`:
  - `/api/activity-heatmap?days=180` → 62 active days
  - `/api/strength-volume?weeks=260` → 3 sessions, last 2022-02-17
  - `/api/pace-efficiency?days=180` → 55 runs
- Playwright headless drove the full flow: login screen → token paste
  (pre-seeded to localStorage) → `/dashboards` → all three cards render
  with real data. Screenshots captured for the 1y heatmap and the
  5y strength view.

## What I didn't do

- No manual sets/reps/weight log layer. Per the design call, v1 ships
  the honest-with-watch-data view only. Add the manual layer later if
  the strength dashboard turns out to be load-bearing for training
  decisions.
- No Grafana sidecar. The whole point of staying in-app was to keep the
  deployment surface flat and the auth + theming consistent.
