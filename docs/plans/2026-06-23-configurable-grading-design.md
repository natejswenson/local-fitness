---
ticket: "N/A (interactive design)"
title: "User-configurable fitness behavior (grading + projection knobs)"
date: "2026-06-23"
source: "design"
---

# User-configurable fitness behavior

`local-fitness` is a public repo, but several behavioral choices are hardcoded
to one user's preferences — so a stranger cloning it is locked into them. These
are app *configuration* (coaching/grading philosophy), not secrets or PII
(already validated clean). This makes them user-configurable, defaulting to
today's hardcoded values so a fresh clone behaves identically.

Scope is local-fitness only. Out of scope: the shared-skills config question
(devlog/ghostwriter) raised separately.

## What's hardcoded today (and becomes configurable)

| Knob | Today | Source | What it controls |
|---|---|---|---|
| Walks count on easy days | `true` | `plans.py` `classify_workout` (easy → `_foot_distance`) | Does a recovery walk satisfy an easy/recovery prescription |
| Walks count in weekly mileage | `false` | `plans.py` `weekly_mileage` (`_running_distance` only) | Whether the weekly-mileage rollup includes walking |
| "Done" fraction | `0.80` | `plans.py:20` `DONE_FRACTION` | actual/target ≥ this = `done` |
| "Partial" fraction | `0.40` | `plans.py:21` `PARTIAL_FRACTION` | actual/target ≥ this = `partial`, below = `missed` |
| Riegel lookback | `120` days | `server.py:389` `_RIEGEL_LOOKBACK_DAYS`, `tools.py` `_PLAN_RIEGEL_LOOKBACK_DAYS` | How far back to find a best effort for the projected finish |

**Already configurable (no work):** `daily_step_goal` (settings table), display
units (`LOCAL_FITNESS_DISPLAY_UNITS`), brief effort (`LOCAL_FITNESS_BRIEF_EFFORT`).

**Deliberately deferred (arguable/niche; keep v1 lean):** anomaly SD threshold
(`tools.py:394`), baseline window `WINDOW_DAYS=60` (also baked into DB column
names — not a clean swap), CTL/ATL time constants (`CTL_TC=42`/`ATL_TC=7`),
recovery tolerances (`recovery_pattern` 95%/103%), trend windows, Riegel
`min_distance_m`. These can follow the same pattern later.

## Config resolution — settings table > env var > default

There are three existing config surfaces (env `.env`, the `settings` DB table,
`user_notes.md`). This adds **no fourth surface**. A new accessor module reads
each knob with the precedence the user chose:

1. `db.get_setting(key)` — live, per-user, set via `fitness config set <key> <value>`
2. `os.environ.get("LOCAL_FITNESS_<KNOB>")` — file-based, set in `.env`
3. hardcoded default — equals today's value (so a fresh clone is unchanged)

```python
# src/local_fitness/config.py  (new)
import os
from . import db

def _resolve(key, env, default, cast, db_path=None):
    raw = db.get_setting(key, db_path=db_path)          # 1. DB (live override)
    if raw is None:
        raw = os.environ.get(env)                       # 2. env (.env)
    if raw is None:
        return default                                  # 3. hardcoded default
    try:
        return cast(raw)
    except (ValueError, TypeError):
        return default                                  # bad value → default

def _as_bool(s) -> bool:
    return str(s).strip().lower() in ("1", "true", "yes", "on")
```

Mirrors the existing `units.display_units()` env-accessor style.

## Threading into pure code (the load-bearing constraint)

`plans.py`'s grading functions are **pure** (no DB/settings access) — that's
what keeps them unit-testable, and the existing tests assert against the module
constants. The accessors must NOT be called from inside the pure functions.
Instead:

- A frozen `GradingConfig` dataclass whose **field defaults are the current
  constants**:
  ```python
  @dataclass(frozen=True)
  class GradingConfig:
      done_fraction: float = DONE_FRACTION          # 0.80
      partial_fraction: float = PARTIAL_FRACTION     # 0.40
      count_walks_easy: bool = True
      count_walks_mileage: bool = False
  ```
- `classify_workout`, `grade_workout`, `weekly_mileage` gain an optional last
  param `cfg: GradingConfig = GradingConfig()`. They read `cfg.done_fraction`
  etc. instead of the bare constants. Existing test/call sites that pass no
  `cfg` get the defaults → behavior and tests unchanged.
- The walk-counting rule becomes config-driven: `classify_workout` uses
  `_foot_distance` for `easy` **iff `cfg.count_walks_easy`** (else
  `_running_distance`); `weekly_mileage` uses `_foot_distance` **iff
  `cfg.count_walks_mileage`** (else `_running_distance`).
- `build_plan_detail` and `build_plan_status` do **not** take `db_path` (verified:
  `plans.py:595`/`:643` — their callers pre-load activities). They gain an
  optional `cfg: GradingConfig = GradingConfig()` and thread it into
  `grade_workout` (→ `classify_workout`) and `weekly_mileage`.
- The **three call sites** resolve `cfg` once via
  `plans.resolve_grading_config(db_path=None)` (default DB) and pass it in:
  `agent/tools.py:1185` (`get_training_plan_status` → `build_plan_status`),
  `agent/tools.py:1219` (`get_training_plan_progress` → `build_plan_detail`),
  and `web/server.py:402` (`_assemble_plan_detail` → `build_plan_detail`).

`grade_workout`'s outcome-based pending logic is unchanged; it just forwards
`cfg` to `classify_workout`.

## Riegel lookback

`config.riegel_lookback_days(db_path)` (default 120) read at the two existing
call sites: `server.py` `_assemble_plan_detail` and `tools.py`
`get_training_plan_progress`. This also collapses the duplicated
`_RIEGEL_LOOKBACK_DAYS` / `_PLAN_RIEGEL_LOOKBACK_DAYS` constants into one source
of truth (both currently 120).

## "Update my local settings file"

Since every default equals the current value, the app behaves identically with
zero config — a fresh clone and this deployment match. To record the current
choices explicitly:
- Add all five knobs to `.env.example` (commented out, each with its default and
  a one-line explanation), per the env-driven pattern.
- Write the five `LOCAL_FITNESS_*` vars (set to current values) into the local
  `.env` so they're explicit. (The `.env` file is gitignored; it is the
  user's local settings file.)
- No DB seeding — the settings-table layer is for live per-knob overrides via
  `fitness config set`, not needed when values equal the defaults.

## API surface

- `config.py` (new): `count_walks_easy(db_path=None) -> bool`,
  `count_walks_mileage(db_path=None) -> bool`,
  `grade_done_fraction(db_path=None) -> float`,
  `grade_partial_fraction(db_path=None) -> float`,
  `riegel_lookback_days(db_path=None) -> int`. All resolve DB → env → default.
- `plans.GradingConfig` (new frozen dataclass) + `plans.resolve_grading_config(db_path) -> GradingConfig`.
- `plans.classify_workout(workout, day_activities, cfg=GradingConfig())` — added optional param.
- `plans.grade_workout(workout, day_activities, frontier, cfg=GradingConfig())` — added optional param.
- `plans.weekly_mileage(workouts, activities_by_date, cfg=GradingConfig())` — added optional last param (current sig `plans.py:285` takes no `cfg`).
- `build_plan_detail(plan, frontier, activities_by_date, best_effort=None, cfg=GradingConfig())` and `build_plan_status(plan, frontier, activities_by_date, today, cfg=GradingConfig())` — gain an optional trailing `cfg`. The three call sites (`tools.py:1185`, `tools.py:1219`, `server.py:402`) resolve `cfg = plans.resolve_grading_config()` and pass it.

## Invariants

Checkable by inspection:
- The pure functions (`classify_workout`/`grade_workout`/`weekly_mileage`) never
  call `db.get_setting` or `os.environ` — config enters only via the `cfg` param.
- Every `GradingConfig` field default equals the corresponding current module
  constant / current behavior.
- Accessor precedence is DB → env → default in every getter.
- No new HTTP endpoint, no auth/SQL surface; grading output shape unchanged
  (no new fields), so no frontend change.

Requires tests:
- `config._resolve`: DB wins over env wins over default; bad cast → default;
  `_as_bool` truth table.
- `GradingConfig` defaults reproduce current grading: `classify_workout` with
  no `cfg` == with `cfg=GradingConfig()` for done/partial/missed cases.
- `count_walks_easy=False` makes an easy-day walk grade `missed` (toggles the
  0.8.0 behavior back off); `=True` keeps it `done`.
- `count_walks_mileage=True` includes a walk in `weekly_mileage`; `False` excludes it.
- Custom `done_fraction`/`partial_fraction` shift the done/partial/missed bands.
- `riegel_lookback_days` default 120; overridden via env and via DB setting.

## Testing strategy

- `uv run pytest -x` — new `test_config.py` (precedence + parsing) and
  `GradingConfig` threading cases in `test_plans.py`; all existing grading tests
  stay green (they pass no `cfg`).
- No prompt change → no `score_prompt.py` / `ab_brief.py` gate.
- Rebuild the container so the deployed app honors the settings.

## Obligations (repo rules)

- Version bump in `pyproject.toml` + CHANGELOG entry (functionality change).
- `devlog/` entry.
- `.env.example` updated with the five knobs; local `.env` populated with
  current values.
- No new endpoint / no auth surface change → `test_security.py` untouched.

## Quality-gate provenance

(Filled in after the `/quality-gate` pass.)
