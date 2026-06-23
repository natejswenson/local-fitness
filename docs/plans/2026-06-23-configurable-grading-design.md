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

def _blank(raw) -> bool:
    # An empty/whitespace-only stored value (DB or env) is treated as UNSET.
    return raw is not None and str(raw).strip() == ""

def _resolve(key, env, default, cast, db_path=None):
    raw = db.get_setting(key, db_path=db_path)          # 1. DB (live override)
    if _blank(raw):                                     #    "" / "   " → UNSET
        raw = None
    if raw is None:
        raw = os.environ.get(env)                       # 2. env (.env)
        if _blank(raw):                                 #    "" / "   " → UNSET
            raw = None
    if raw is None:
        return default                                  # 3. hardcoded default
    try:
        return cast(raw)
    except (ValueError, TypeError):
        return default                                  # bad value → default

_BOOL_TRUE = {"1", "true", "yes", "on"}
_BOOL_FALSE = {"0", "false", "no", "off"}

def _as_bool(s) -> bool:
    tok = str(s).strip().lower()
    if tok in _BOOL_TRUE:
        return True
    if tok in _BOOL_FALSE:
        return False
    raise ValueError(f"not a recognized bool: {s!r}")   # → except clause → default
```

Mirrors the existing `units.display_units()` env-accessor style.

**Empty-string / unrecognized handling (SIG-1).** `db.get_setting` returns the
stored string verbatim, and `fitness config set count_walks_easy ""` stores
`""` — which is *not* `None`. Without normalization, `raw=""` would short-circuit
the DB→env→default fall-through, and a naive `_as_bool("")` would return a
"successful" `False` instead of the intended default `True`. Two fixes close
this:

1. `_resolve` normalizes a blank (empty- or whitespace-only) raw value to `None`
   **at each layer** *before* casting, so a blank DB value correctly falls
   through to env, and a blank env value falls through to the default.
2. `_as_bool` is **strict**: it accepts only the known truthy/falsy tokens and
   **raises `ValueError`** for anything else (including a stray non-blank token
   like `"maybe"`), so the existing `except (ValueError, TypeError) → default`
   path catches it.

Together these restore the guarantee for bools: **DB > env > default, and any
unrecognized value falls back to the default** (rather than silently flipping a
knob to `False`).

## Threading into pure code (the load-bearing constraint)

`plans.py`'s grading functions are **pure** (no DB/settings access) — that's
what keeps them unit-testable. The `GradingConfig` field defaults **equal the
module constants**, so a default `GradingConfig()` reproduces today's grading
behavior exactly; the existing *behavioral* tests (e.g. target 6000 / actual
3000 → `partial`) stay green without passing a `cfg`. The accessors must NOT be
called from inside the pure functions. Instead:

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
- **`partial_fraction` is threaded into BOTH bands (SIG-2b).** `classify_workout`
  currently uses the `PARTIAL_FRACTION` constant in *two* places: the distance
  band (`plans.py:226`) and the interval/tempo **duration** band (`plans.py:235`,
  `actual < PARTIAL_FRACTION * target`). Both must read `cfg.partial_fraction`,
  not the bare constant, so the knob is consistent across distance- and
  duration-graded workouts. Likewise the distance band uses `cfg.done_fraction`.
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

**Complete config coverage (verified — no bypass).** These three are the *only*
production callers of `classify_workout`/`grade_workout`/`weekly_mileage`/
`build_plan_*`; `status.py`, `briefs.py`, and `cli.py` do not grade workouts, so
no path silently uses the hardcoded defaults. Crucially the **brief** reaches
grading only through `get_training_plan_status` (`tools.py:1185`), which is one
of the three threaded sites — so the brief grades with the user's config too,
not just the web tab. `resolve_grading_config` is the single entry point where
the fraction-pair validation runs; the standalone `config.grade_*` accessors
validate only their own cast (not the pair), so the grading path MUST go through
`resolve_grading_config` (it does).

## Riegel lookback

`config.riegel_lookback_days(db_path)` (default 120) read at the two existing
call sites: `server.py` `_assemble_plan_detail` and `tools.py`
`get_training_plan_progress`. This also collapses the duplicated
`_RIEGEL_LOOKBACK_DAYS` / `_PLAN_RIEGEL_LOOKBACK_DAYS` constants into one source
of truth (both currently 120).

## Validation of resolved values (SIG-2)

A value can cast cleanly yet be **nonsense** for grading, and the pure functions
have no guardrails: `done_fraction=2.0`, `partial_fraction=-1`, or
`partial > done` would invert the bands (the `partial` band at `plans.py:226-228`
becomes unreachable), and a negative/zero `riegel_lookback_days` yields a future
cutoff so `best_recent_effort` finds nothing and the projected finish silently
vanishes. So resolution **validates**, and on any violation falls back to the
**default** for the offending knob(s) — never raising to the user, consistent
with the env-driven "conservative default" philosophy. Bad config degrades to
shipped behavior; it doesn't break grading.

- **Fraction pair** (validated in `resolve_grading_config`): require
  `0 <= partial_fraction <= done_fraction <= 1`. If the ordering or range is
  violated, revert **both** to their defaults (`0.40` / `0.80`) as a pair — so
  the bands can never invert (reverting only one could still leave
  `partial > done`).
- **Riegel lookback** (validated in the `riegel_lookback_days` accessor):
  require `1 <= lookback <= 3650` (≈10 yr upper guard). On violation, use the
  default `120`.

```python
def resolve_grading_config(db_path=None) -> GradingConfig:
    settings = db.all_settings(db_path=db_path)   # one connect (db.py:320)
    env = os.environ                              # read once
    done = _resolve_from(settings, env, "grade_done_fraction",
                         "LOCAL_FITNESS_GRADE_DONE_FRACTION", DONE_FRACTION, float)
    partial = _resolve_from(settings, env, "grade_partial_fraction",
                         "LOCAL_FITNESS_GRADE_PARTIAL_FRACTION", PARTIAL_FRACTION, float)
    # Invariant: 0 <= partial <= done <= 1, else BOTH revert to defaults (no inversion).
    if not (0 <= partial <= done <= 1):
        done, partial = DONE_FRACTION, PARTIAL_FRACTION
    return GradingConfig(
        done_fraction=done,
        partial_fraction=partial,
        count_walks_easy=_resolve_from(settings, env, "count_walks_easy",
                         "LOCAL_FITNESS_COUNT_WALKS_EASY", True, _as_bool),
        count_walks_mileage=_resolve_from(settings, env, "count_walks_mileage",
                         "LOCAL_FITNESS_COUNT_WALKS_MILEAGE", False, _as_bool),
    )

def riegel_lookback_days(db_path=None) -> int:
    n = _resolve("riegel_lookback_days", "LOCAL_FITNESS_RIEGEL_LOOKBACK_DAYS",
                 120, int, db_path=db_path)
    return n if 1 <= n <= 3650 else 120          # invariant: lookback >= 1
```

(`_resolve_from(settings, env, ...)` is the batched twin of `_resolve` — same
blank-normalization and `except → default` semantics, but it reads the
pre-fetched `settings` dict and `env` mapping instead of opening a connection.)

## Surfaced actuals vs. verdict under `count_walks_easy=False` (SIG-3)

When `count_walks_easy=False`, a recovery walk on an easy day grades `missed`
(it no longer counts), yet `_workout_actuals` still surfaces the **walk**
distance/pace on the row — because `_workout_actuals` is always foot-based. This
is intentional and **not** a contradiction; no code change is needed:

- **Surfaced actuals are config-independent — "what you did."** This is the same
  principle shipped in 0.8.0: a walk on a long day already shows the walk
  distance with a `missed` verdict. The row reports the activity you actually
  recorded, regardless of whether it satisfied the prescription.
- **The verdict reflects whether it counted per config — "did it qualify."**
  With `count_walks_easy=False`, a walk doesn't satisfy an easy prescription, so
  the verdict is `missed`.
- **Coloring is verdict-driven, so the row stays honest.** The 0.8.0 frontend
  colors a row red **iff `verdict === 'missed'`** — it keys off the verdict, not
  the surfaced distance. So a `missed` walk row colors red, matching the verdict,
  even though it displays the walk distance. There is no visual contradiction
  between the surfaced distance and the verdict.

Net: surfaced actuals describe what happened; the verdict describes whether it
counted; verdict-driven coloring keeps the two reconciled on screen. So
`_workout_actuals` stays always foot-based — **no change**.

## "Update my local settings file"

Since every default equals the current value, the app behaves identically with
zero config — a fresh clone and this deployment match. The user explicitly asked
to record their current values, so we still write them — but `.env` is framed as
an **explicit-defaults / override file, not a live-state mirror.** Because
precedence is DB > env, once the user runs `fitness config set <knob>`, the DB
wins and `.env` no longer reflects live behavior; and `.env` is gitignored, so
it's unverifiable in review. Therefore:

- **`.env.example` (TRACKED, reviewable) is the canonical documentation of the
  five knobs.** Add all five (commented out, each with its default and a
  one-line explanation), per the env-driven pattern. This is the artifact a
  reviewer reads to understand the knobs.
- **Write the five `LOCAL_FITNESS_*` vars (current values) into the local `.env`
  as a convenience**, with a one-line comment in the block noting the override
  semantics, e.g.:
  ```bash
  # Explicit defaults — these equal the shipped behavior. NOTE: `fitness config
  # set <knob>` writes the DB, which overrides env live (DB > env), so after any
  # such command this file no longer reflects live behavior.
  LOCAL_FITNESS_COUNT_WALKS_EASY=true
  # ...four more
  ```
- No DB seeding — the settings-table layer is for live per-knob overrides via
  `fitness config set`, not needed when values equal the defaults.

## API surface

- `config.py` (new): `count_walks_easy(db_path=None) -> bool`,
  `count_walks_mileage(db_path=None) -> bool`,
  `grade_done_fraction(db_path=None) -> float`,
  `grade_partial_fraction(db_path=None) -> float`,
  `riegel_lookback_days(db_path=None) -> int`. All resolve DB → env → default.
  These standalone accessors remain for single-knob use; `riegel_lookback_days`
  also enforces the `1 <= n <= 3650` guard.
- `plans.GradingConfig` (new frozen dataclass) + `plans.resolve_grading_config(db_path) -> GradingConfig`.
  `resolve_grading_config` **batches** its reads — one `db.all_settings()`
  (`db.py:320`) plus one `os.environ` read, then builds `GradingConfig` — rather
  than five separate `db.get_setting()` connections. It also runs the
  fraction-pair validation (revert both to defaults if `0 <= partial <= done <= 1`
  is violated).
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
- Accessor precedence is DB → env → default in every getter; a blank
  (empty/whitespace-only) DB or env value is treated as UNSET and falls through,
  and an unrecognized value falls back to the default (`_as_bool` raises on
  unknown tokens).
- **Resolved config always satisfies `0 <= partial <= done <= 1` and
  `lookback >= 1`; any violating value falls back to defaults** (the fraction
  pair reverts together so the bands can't invert).
- No new HTTP endpoint, no auth/SQL surface; grading output shape unchanged
  (no new fields), so no frontend change.

Requires tests:
- `config._resolve`: DB wins over env wins over default; bad cast → default;
  `_as_bool` truth table.
- **Blank → UNSET (SIG-1):** a blank DB value (`""` / `"   "`) falls through to
  env; a blank env value falls through to the default; `_as_bool("")` and
  `_as_bool("maybe")` raise → `count_walks_easy` resolves to its default `True`,
  not `False`.
- **Validation fallback (SIG-2):** `done_fraction=2.0`, `partial_fraction=-1`,
  and `partial > done` each revert **both** fractions to `0.40`/`0.80`;
  `riegel_lookback_days` of `0`, `-5`, and `>3650` revert to `120`.
- **`partial_fraction` threads into the duration band (SIG-2b):** a custom
  `partial_fraction` shifts the interval/tempo `partial` boundary
  (`plans.py:235`), not just the distance band.
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

Reviewed via `/quality-gate` (artifact type: design) on `general-purpose` agents
(the `crucible-*` agent types / receipt-cairn infra are not installed here, so the
Opus recall guarantee was not enforced; findings were still code-grounded). Two
red-team rounds + a tightened look-harder pass. Terminal verdict **PASS
(clean-pass)**: 0 Fatal / 0 Significant on a fresh round, confirmed by look-harder
(which also exhaustively verified there is no config-bypass grading path). Score
trajectory 3 → 0.

Round 1 caught three real precedence/validation holes the first draft missed: an
**empty-string DB value silently flipping bool knobs to `False`** (fixed with
blank-normalization at each layer + a strict `_as_bool`); **no validation of
resolved numbers** (`partial > done` silently inverts the grade bands; negative
`riegel_lookback_days` kills the projected finish) — fixed with a revert-both /
clamp-to-default validation step; and that **`partial_fraction` is reused in the
duration band** (`plans.py:235`), so the knob had to thread into both bands. The
`.env` deliverable was reframed (tracked `.env.example` is canonical; the live
`.env` write is a convenience with documented override semantics).
