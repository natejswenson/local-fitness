# Training Plans Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use crucible:build to implement this plan task-by-task.

**Goal:** Add goal-driven training plans as a new tab — the AI drafts a periodized plan from the user's Garmin history, the user riffs in chat and commits, and the active plan is tracked on the tab and folded into the daily brief's workout takeaway.

**Architecture:** Two new SQLite tables (`training_plans`, `plan_workouts`); a new pure-logic module `plans.py` (validation, type-aware adherence, data-frontier slicing, Riegel projection, weekly rollup) with no I/O; three draft-only agent tools added to `ALL_TOOLS`; three cheap REST endpoints (`GET /api/plan`, commit, delete); the existing brief workout takeaway extended to carry the plan; a new React route/tab reusing the existing chart/table/chat primitives. The AI→SQLite write path is draft-only, enforced in code.

**Tech Stack:** Python 3 + FastAPI + sqlite3 + claude-agent-sdk (backend); Vite + React 19 + TS + Tailwind v4 + recharts (frontend); pytest + vitest.

**Source of truth:** `docs/plans/2026-06-15-training-plans-design.md` (§ refs below) and `docs/plans/2026-06-15-training-plans-contract.yaml`.

**Conventions to follow (from recon):**
- DB access: `with db.connect() as conn:` context manager (commits on clean exit). Schema is idempotent `CREATE TABLE IF NOT EXISTS` appended to the `SCHEMA` string in `src/local_fitness/db.py` (~lines 33-151); `init_schema()` runs it everywhere.
- Agent tool pattern: `@tool(name, description, schema)` returning `_text({...})`/`_err(...)`; append to `ALL_TOOLS` (`src/local_fitness/agent/tools.py:575`). Frozen-set validation BEFORE any SQL (pattern at `tools.py:21-30, 77-85`).
- REST: `@app.get/post/delete("/api/...")` in `src/local_fitness/web/server.py` — auto auth-gated by the `/api/` middleware; `pydantic.BaseModel` bodies; `Query(..., ge=, le=)` bounds; parameterized SQL only.
- Security rules (`CLAUDE.md`): new `/api/*` auto-gated; only add to `RATE_LIMITED_PREFIXES` if it calls Claude (these don't); whitelist columns, parameterize values; `tests/test_security.py` gets a case per auth-relevant path.
- Rebuild container after changes: `docker compose up -d --build local-fitness`.

---

## Phase 0 — Schema

### Task 0.1: Add the two tables to the schema

**Files:**
- Modify: `src/local_fitness/db.py` (the `SCHEMA` string, after the `settings` table ~line 150)
- Test: `tests/test_db.py`

**Step 1: Write the failing test**

```python
# tests/test_db.py — add
def test_training_plan_tables_exist(tmp_path):
    db_path = tmp_path / "t.db"
    db.init_schema(db_path)
    with db.connect(db_path) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert {"training_plans", "plan_workouts"} <= tables

def test_one_active_plan_unique_index(tmp_path):
    db_path = tmp_path / "t.db"
    db.init_schema(db_path)
    with db.connect(db_path) as conn:
        conn.execute("INSERT INTO training_plans (status, goal_type, race_date, created_at) "
                     "VALUES ('active','10k','2026-09-14','2026-06-15T00:00:00')")
        conn.execute("INSERT INTO training_plans (status, goal_type, race_date, created_at) "
                     "VALUES ('active','5k','2026-10-01','2026-06-15T00:00:00')")
        import pytest, sqlite3
        # second active must violate the partial unique index
    # assert raised — restructure with pytest.raises below
```

Rewrite the second test cleanly:

```python
def test_one_active_plan_unique_index(tmp_path):
    import sqlite3, pytest
    db_path = tmp_path / "t.db"
    db.init_schema(db_path)
    with pytest.raises(sqlite3.IntegrityError):
        with db.connect(db_path) as conn:
            conn.execute("INSERT INTO training_plans (status, goal_type, race_date, created_at) "
                         "VALUES ('active','10k','2026-09-14','2026-06-15T00:00:00')")
            conn.execute("INSERT INTO training_plans (status, goal_type, race_date, created_at) "
                         "VALUES ('active','5k','2026-10-01','2026-06-15T00:00:00')")
```

**Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_db.py -k training_plan -v`
Expected: FAIL (no such table).

**Step 3: Implement** — append to the `SCHEMA` string in `db.py` (verbatim from design §3):

```sql
CREATE TABLE IF NOT EXISTS training_plans (
    plan_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    status               TEXT NOT NULL,
    goal_type            TEXT NOT NULL,
    goal_distance_m      REAL,
    race_date            TEXT NOT NULL,
    target_time_seconds  INTEGER,
    title                TEXT,
    ability_snapshot     TEXT,
    created_at           TEXT NOT NULL,
    committed_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_plans_status ON training_plans(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_plan
    ON training_plans(status) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS plan_workouts (
    workout_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id              INTEGER NOT NULL,
    date                 TEXT NOT NULL,
    seq                  INTEGER NOT NULL DEFAULT 1,
    week_index           INTEGER NOT NULL,
    type                 TEXT NOT NULL,
    target_distance_m    REAL,
    target_pace_sec_per_km REAL,
    target_duration_sec  INTEGER,
    description          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plan_workouts_plan ON plan_workouts(plan_id);
CREATE INDEX IF NOT EXISTS idx_plan_workouts_date ON plan_workouts(date);
```

**Step 4: Run** — `uv run pytest tests/test_db.py -k training_plan -v` → PASS. Also `uv run pytest tests/test_db.py -v` (idempotency test still green).

**Step 5: Commit**

```bash
git add src/local_fitness/db.py tests/test_db.py
git commit -m "feat: add training_plans + plan_workouts schema with single-active index"
```

---

## Phase 1 — Pure plan logic (`plans.py`), no I/O

Create `src/local_fitness/plans.py` and `tests/test_plans.py`. These functions are pure (take rows/dicts, return verdicts/numbers) so they TDD cleanly and are reused by tools and endpoints. Constants live at module top:

```python
# adherence tolerances
DONE_FRACTION = 0.80
PARTIAL_FRACTION = 0.40
GOAL_TYPES = frozenset({"5k", "10k", "half", "full", "custom"})
WORKOUT_TYPES = frozenset({"easy", "long", "tempo", "interval", "rest", "race", "cross"})
MAX_WORKOUTS = 200
RIEGEL_EXP = 1.06
GOAL_DISTANCE_M = {"5k": 5000.0, "10k": 10000.0, "half": 21097.5, "full": 42195.0}
RUNNING_TYPES = ("running", "trail_running", "treadmill_running")  # activity_type substrings
```

### Task 1.1: Input validation (`validate_plan_input`)

**Files:** Create `src/local_fitness/plans.py`; Test `tests/test_plans.py`

**Step 1: Failing tests**

```python
from local_fitness import plans

def test_validate_rejects_empty_workouts():
    err = plans.validate_plan_input("10k", "2026-09-14", workouts=[], created_date="2026-06-15")
    assert err and "workout" in err.lower()

def test_validate_rejects_bad_goal_type():
    err = plans.validate_plan_input("marathon", "2026-09-14",
        workouts=[_wk()], created_date="2026-06-15")
    assert err and "goal_type" in err

def test_validate_rejects_nonfinite_distance():
    err = plans.validate_plan_input("10k", "2026-09-14",
        workouts=[_wk(target_distance_m=float("inf"))], created_date="2026-06-15")
    assert err

def test_validate_rejects_bad_date():
    err = plans.validate_plan_input("10k", "2026-13-99",
        workouts=[_wk()], created_date="2026-06-15")
    assert err

def test_validate_rejects_duplicate_date_seq():
    err = plans.validate_plan_input("10k", "2026-09-14",
        workouts=[_wk(date="2026-07-01", seq=1), _wk(date="2026-07-01", seq=1)],
        created_date="2026-06-15")
    assert err and "duplicate" in err.lower()

def test_validate_rejects_workout_after_race():
    err = plans.validate_plan_input("10k", "2026-09-14",
        workouts=[_wk(date="2026-09-20")], created_date="2026-06-15")
    assert err

def test_validate_rejects_too_many():
    err = plans.validate_plan_input("10k", "2026-09-14",
        workouts=[_wk(date=f"2026-07-{(i%28)+1:02d}", seq=i) for i in range(plans.MAX_WORKOUTS + 1)],
        created_date="2026-06-15")
    assert err

def test_validate_accepts_good_plan():
    err = plans.validate_plan_input("10k", "2026-09-14",
        workouts=[_wk(date="2026-07-01"), _wk(date="2026-07-02", type="rest", target_distance_m=None)],
        created_date="2026-06-15")
    assert err is None

def _wk(date="2026-07-01", seq=1, week_index=1, type="easy",
        target_distance_m=6000.0, target_pace_sec_per_km=None,
        target_duration_sec=None, description="6km easy"):
    return dict(date=date, seq=seq, week_index=week_index, type=type,
                target_distance_m=target_distance_m, target_pace_sec_per_km=target_pace_sec_per_km,
                target_duration_sec=target_duration_sec, description=description)
```

**Step 2:** Run `uv run pytest tests/test_plans.py -k validate -v` → FAIL (no module).

**Step 3: Implement** `validate_plan_input(goal_type, race_date, workouts, created_date, goal_distance_m=None, target_time_seconds=None) -> str | None`. Returns an error string or `None`. Checks, in order: `goal_type in GOAL_TYPES`; `race_date`/`created_date` parse via `date.fromisoformat`; `1 <= len(workouts) <= MAX_WORKOUTS`; each workout: `type in WORKOUT_TYPES`, `date` parses and is in `[created_date, race_date]`, numerics finite (`math.isfinite`) and non-negative (allow `None`), `week_index`/`seq` non-negative ints, `description` non-empty; no duplicate `(date, seq)`. Use a `set()` for the dup check.

**Step 4:** Run → PASS.

**Step 5: Commit** `feat: add training-plan input validation`

### Task 1.2: Type-aware adherence (`classify_workout`)

**Files:** `src/local_fitness/plans.py`; `tests/test_plans.py`

**Step 1: Failing tests** (design §3a table is the spec):

```python
def test_rest_day_always_compliant():
    assert plans.classify_workout({"type":"rest"}, day_activities=[]) == "compliant"
    assert plans.classify_workout({"type":"rest"}, day_activities=[_run(5000)]) == "compliant"

def test_easy_distance_thresholds():
    w = {"type":"easy","target_distance_m":6000.0}
    assert plans.classify_workout(w, [_run(6000)]) == "done"      # 100%
    assert plans.classify_workout(w, [_run(3000)]) == "partial"   # 50%
    assert plans.classify_workout(w, []) == "missed"

def test_easy_null_target_any_run_done():
    w = {"type":"easy","target_distance_m":None}
    assert plans.classify_workout(w, [_run(4000)]) == "done"
    assert plans.classify_workout(w, []) == "missed"

def test_multiple_runs_summed():
    w = {"type":"long","target_distance_m":10000.0}
    assert plans.classify_workout(w, [_run(6000), _run(5000)]) == "done"

def test_interval_graded_on_duration_not_distance():
    w = {"type":"interval","target_duration_sec":3600}
    # short total distance but a real session present
    assert plans.classify_workout(w, [_run(4000, duration=3600)]) == "done"
    assert plans.classify_workout(w, [_run(2000, duration=600)]) == "partial"
    assert plans.classify_workout(w, []) == "missed"

def test_cross_matches_non_running_only():
    w = {"type":"cross","target_duration_sec":1800}
    assert plans.classify_workout(w, [_act("cycling", duration=2000)]) == "done"
    assert plans.classify_workout(w, [_run(5000, duration=1800)]) == "missed"  # running doesn't satisfy cross

def _run(dist, duration=1800, atype="running"):
    return {"activity_type": atype, "distance_meters": dist, "duration_seconds": duration}
def _act(atype, duration=1800, dist=0):
    return {"activity_type": atype, "distance_meters": dist, "duration_seconds": duration}
```

**Step 2:** Run → FAIL.

**Step 3: Implement** `classify_workout(workout: dict, day_activities: list[dict]) -> str` returning `"done"|"partial"|"missed"|"compliant"`. Branch on `type`:
- `rest` → `"compliant"`.
- `easy|long|race` → sum `distance_meters` of running activities (activity_type contains a `RUNNING_TYPES` substring); if `target_distance_m` is None → `"done"` if any running activity else `"missed"`; else fraction vs target → done/partial/missed.
- `interval|tempo` → consider running activities; if none → `"missed"`; if `target_duration_sec` and summed `duration_seconds` < `PARTIAL_FRACTION * target` → `"partial"`; else `"done"`.
- `cross` → any non-running activity present → `"done"` else `"missed"`.

**Step 4:** Run → PASS. **Step 5: Commit** `feat: add type-aware workout adherence classifier`

### Task 1.3: Data-frontier grading (`grade_workout`) and slicing

**Files:** `plans.py`, `tests/test_plans.py`

**Step 1: Failing tests** (design §3b — days at/after the frontier are `pending`):

```python
def test_future_or_unsynced_day_is_pending():
    # frontier = last synced day; workout dated >= frontier is pending regardless of activities
    assert plans.grade_workout({"type":"easy","target_distance_m":6000.0,"date":"2026-07-10"},
        day_activities=[], frontier="2026-07-08") == "pending"

def test_past_day_is_graded():
    assert plans.grade_workout({"type":"easy","target_distance_m":6000.0,"date":"2026-07-05"},
        day_activities=[], frontier="2026-07-08") == "missed"

def test_day_equal_frontier_is_pending():
    assert plans.grade_workout({"type":"easy","target_distance_m":6000.0,"date":"2026-07-08"},
        day_activities=[], frontier="2026-07-08") == "pending"
```

**Step 3: Implement** `grade_workout(workout, day_activities, frontier) -> str`: if `workout["date"] >= frontier` (ISO string compare is valid for `YYYY-MM-DD`) → `"pending"`; else `classify_workout(...)`. (Document the ISO-lexicographic-compare assumption in a comment.)

**Step 4/5:** PASS; commit `feat: grade workouts against the data frontier`.

### Task 1.4: Riegel predicted finish + weekly mileage rollup

**Files:** `plans.py`, `tests/test_plans.py`

**Step 1: Failing tests**

```python
def test_riegel_projection():
    # 10k in 50:00 -> half (~21.0975k) projection: 3000 * (21097.5/10000)^1.06
    secs = plans.riegel_predict(best_distance_m=10000, best_time_s=3000, target_distance_m=21097.5)
    assert 6500 < secs < 7200

def test_riegel_none_without_effort():
    assert plans.riegel_predict(None, None, 10000.0) is None

def test_weekly_mileage_rollup():
    workouts = [
        {"week_index":1,"target_distance_m":6000.0,"date":"2026-07-01"},
        {"week_index":1,"target_distance_m":10000.0,"date":"2026-07-03"},
        {"week_index":2,"target_distance_m":8000.0,"date":"2026-07-08"},
    ]
    activities_by_date = {"2026-07-01":[_run(6000)], "2026-07-03":[_run(9000)]}
    rows = plans.weekly_mileage(workouts, activities_by_date)
    assert rows[0]["week"] == 1 and rows[0]["planned_km"] == 16.0 and rows[0]["actual_km"] == 15.0
    assert rows[1]["week"] == 2 and rows[1]["actual_km"] == 0.0
```

**Step 3: Implement** `riegel_predict(best_distance_m, best_time_s, target_distance_m) -> float|None` = `best_time_s * (target_distance_m/best_distance_m)**RIEGEL_EXP` (None if any input falsy). `weekly_mileage(workouts, activities_by_date) -> list[dict]` summing planned (target_distance_m) and actual (running distance summed per date) grouped by `week_index`, km rounded to 1 dp.

**Step 4/5:** PASS; commit `feat: add Riegel projection and weekly mileage rollup`.

---

## Phase 2 — Persistence helpers + agent tools

### Task 2.1: Plan persistence helpers (`plans.py` DB section)

Add I/O helpers that use `db.connect`. Keep SQL parameterized; column lists are code-defined.

**Files:** `src/local_fitness/plans.py`; `tests/test_plans_db.py`

**Step 1: Failing tests** (representative):

```python
from local_fitness import db, plans

def test_insert_and_get_draft(tmp_path):
    p = tmp_path/"t.db"; db.init_schema(p)
    pid = plans.insert_draft(dict(goal_type="10k", race_date="2026-09-14",
        target_time_seconds=3000, goal_distance_m=10000.0, title="Sub-50",
        ability_snapshot={"vo2":48}, created_at="2026-06-15T00:00:00"),
        workouts=[_wk(date="2026-07-01")], db_path=p)
    got = plans.get_plan(pid, db_path=p)
    assert got["status"] == "draft" and len(got["workouts"]) == 1

def test_insert_draft_archives_prior_draft(tmp_path):
    p = tmp_path/"t.db"; db.init_schema(p)
    pid1 = plans.insert_draft(_plan(), [_wk()], db_path=p)
    pid2 = plans.insert_draft(_plan(), [_wk()], db_path=p)
    assert plans.get_plan(pid1, db_path=p)["status"] == "archived"
    assert plans.get_plan(pid2, db_path=p)["status"] == "draft"

def test_revise_replaces_workouts_atomically(tmp_path):
    p = tmp_path/"t.db"; db.init_schema(p)
    pid = plans.insert_draft(_plan(), [_wk(date="2026-07-01")], db_path=p)
    plans.revise_draft(pid, fields={"title":"New"}, workouts=[_wk(date="2026-07-02"),_wk(date="2026-07-03")], db_path=p)
    got = plans.get_plan(pid, db_path=p)
    assert got["title"] == "New" and len(got["workouts"]) == 2

def test_revise_refuses_non_draft(tmp_path):
    p = tmp_path/"t.db"; db.init_schema(p)
    pid = plans.insert_draft(_plan(), [_wk()], db_path=p)
    plans.commit_plan(pid, now="2026-06-15T00:00:00", db_path=p)
    import pytest
    with pytest.raises(plans.NotDraftError):
        plans.revise_draft(pid, fields={"title":"x"}, workouts=None, db_path=p)

def test_commit_archives_prior_active(tmp_path):
    p = tmp_path/"t.db"; db.init_schema(p)
    a = plans.insert_draft(_plan(), [_wk()], db_path=p); plans.commit_plan(a, now="t", db_path=p)
    b = plans.insert_draft(_plan(), [_wk()], db_path=p); plans.commit_plan(b, now="t", db_path=p)
    assert plans.get_plan(a, db_path=p)["status"] == "archived"
    assert plans.get_plan(b, db_path=p)["status"] == "active"

def test_commit_rejects_nondraft(tmp_path):
    p = tmp_path/"t.db"; db.init_schema(p)
    pid = plans.insert_draft(_plan(), [_wk()], db_path=p)
    plans.commit_plan(pid, now="t", db_path=p)
    import pytest
    with pytest.raises(plans.NotDraftError):
        plans.commit_plan(pid, now="t", db_path=p)  # already active
```

**Step 3: Implement** in `plans.py`:
- `class NotDraftError(Exception)`, `class PlanNotFoundError(Exception)`.
- `_EDITABLE_PLAN_COLS = frozenset({"goal_type","race_date","target_time_seconds","goal_distance_m","title"})` — **excludes** `status`, `committed_at`, `plan_id`, `created_at`.
- `insert_draft(plan_fields, workouts, db_path=None) -> int`: in one `connect()` block, `UPDATE training_plans SET status='archived' WHERE status='draft'`, INSERT the plan with `status='draft'` (status hardcoded, `ability_snapshot` JSON-dumped), then INSERT each workout. Return `lastrowid`.
- `revise_draft(plan_id, fields, workouts, db_path=None)`: `SELECT status` → raise `PlanNotFoundError`/`NotDraftError` if missing/not draft. Filter `fields` to `_EDITABLE_PLAN_COLS` (drop unknown keys silently OR raise — raise on unknown for safety). UPDATE whitelisted columns (build `SET col=?` only from whitelisted keys). If `workouts is not None`: `DELETE FROM plan_workouts WHERE plan_id=?` then re-INSERT — all in the same transaction (single `connect()` block, which commits once).
- `commit_plan(plan_id, now, db_path=None)`: open `connect()`, `conn.execute("BEGIN IMMEDIATE")` — actually `db.connect` already opens a connection; issue `BEGIN IMMEDIATE` first. `SELECT status` (raise if missing/not draft), `UPDATE ... SET status='archived' WHERE status='active'`, `UPDATE ... SET status='active', committed_at=? WHERE plan_id=?`. The partial unique index backstops races.
- `delete_plan(plan_id, db_path=None)`: `UPDATE ... SET status='archived' WHERE plan_id=?`; raise `PlanNotFoundError` if rowcount 0.
- `get_plan(plan_id, db_path=None) -> dict|None`, `get_active_plan(...)`, `get_draft_plan(...)`: SELECT plan row + its workouts (ordered by `date, seq`), parse `ability_snapshot` JSON best-effort.

> **Note on `db.connect` + BEGIN IMMEDIATE:** `connect()` yields a `sqlite3.Connection` in autocommit-ish mode with a context manager that commits at exit. For `commit_plan`, run `conn.execute("BEGIN IMMEDIATE")` as the first statement inside the block to take the write lock up front; the context manager's `commit()` finalizes. Verify with the concurrency test in Phase 5.

**Step 4/5:** Run all `tests/test_plans_db.py` → PASS; commit `feat: add training-plan persistence (draft/revise/commit/delete) with single-active guard`.

### Task 2.2: Status assembly helper (`build_plan_detail`, `build_plan_status`)

**Files:** `plans.py`, `tests/test_plans_db.py`

Implement `build_plan_detail(plan, frontier, activities_by_date, best_effort) -> dict` returning the `PlanDetail` shape the tab needs: plan fields, `workouts` each with a graded `verdict` (via `grade_workout`), `weekly_mileage`, `ctl_series` left to the endpoint (it reuses `/api/training-load` logic), `predicted_finish_seconds` (Riegel), `adherence_pct` (graded done / graded total). And `build_plan_status(active_plan, frontier, activities_by_date) -> dict` for the brief: `{active: bool, goal, days_to_race, last_graded: {...}, today: {...}, adherence_pct}` — **structured fields only, description length-capped to ~120 chars.**

TDD with a fabricated plan + activities; assert `adherence_pct` ignores `pending` days and `verdict` values are correct. Commit `feat: assemble plan detail + brief status (frontier-aware)`.

### Task 2.3: Agent tools

**Files:** Modify `src/local_fitness/agent/tools.py` (add 3 tools; append to `ALL_TOOLS` at ~line 575); Test `tests/test_plan_tools.py`

**Step 1: Failing tests** — call the tool coroutines directly (they take `args: dict`, return the `{"content":[...]}` envelope; parse the inner JSON):

```python
import json, asyncio
from local_fitness.agent import tools
from local_fitness import db, plans

def _payload(res): return json.loads(res["content"][0]["text"])

def test_propose_creates_draft(tmp_path, monkeypatch):
    p = tmp_path/"t.db"; db.init_schema(p)
    monkeypatch.setattr(db, "get_db_path", lambda: p)
    res = asyncio.run(tools.propose_training_plan({
        "goal_type":"10k","race_date":"2026-09-14","target_time_seconds":3000,
        "workouts":[{"date":"2026-07-01","week_index":1,"type":"easy",
                     "target_distance_m":6000.0,"description":"6km easy"}]}))
    body = _payload(res); assert body["status"] == "draft"

def test_revise_cannot_set_status(tmp_path, monkeypatch):
    p = tmp_path/"t.db"; db.init_schema(p); monkeypatch.setattr(db,"get_db_path",lambda:p)
    pid = _payload(asyncio.run(tools.propose_training_plan(_good_args())))["plan_id"]
    # status is not in the schema at all -> SDK would drop it, but assert revise ignores it
    res = asyncio.run(tools.revise_training_plan({"plan_id":pid,"title":"X","status":"active"}))
    assert _payload(res).get("status") == "draft"
    assert plans.get_plan(pid, db_path=p)["status"] == "draft"

def test_propose_rejects_bad_input(tmp_path, monkeypatch):
    p = tmp_path/"t.db"; db.init_schema(p); monkeypatch.setattr(db,"get_db_path",lambda:p)
    res = asyncio.run(tools.propose_training_plan({"goal_type":"marathon","race_date":"2026-09-14","workouts":[]}))
    body = _payload(res); assert body.get("error")
```

**Step 3: Implement** three tools (full JSON schema dicts, `required` minimal):
- `propose_training_plan(args)`: pull fields; `goal_distance_m` defaults from `plans.GOAL_DISTANCE_M` when goal_type canonical and not given; `created_date = db.last_known_daily_date() or today`; `err = plans.validate_plan_input(...)`; on err `return _err(err)`. Else `pid = plans.insert_draft(...)`; `return _text({"plan_id":pid,"status":"draft"})`. **`status` is never read from args.**
- `revise_training_plan(args)`: require int `plan_id`; build `fields` from the whitelisted named params present in args (NEVER `status`); validate the resulting workout set if `workouts` present; `try: plans.revise_draft(...) except (NotDraftError, PlanNotFoundError) as e: return _err(str(e))`; `return _text({"plan_id":plan_id,"status":"draft"})`.
- `get_training_plan_status(_args)`: load active plan; if none `return _text({"active": False})`; else assemble via `plans.build_plan_status(...)` (needs frontier = `db.last_known_daily_date()` and that date's-window activities — query `activities` for the relevant dates).

Append all three to `ALL_TOOLS`. The SDK auto-exposes/allows them via `make_server()` + `allowed_tool_names()`.

**Step 4/5:** Run → PASS; commit `feat: add draft-only training-plan agent tools (propose/revise/status)`.

---

## Phase 3 — REST endpoints + CSP header

### Task 3.1: `GET /api/plan`

**Files:** Modify `src/local_fitness/web/server.py` (add route near the other GETs); Test `tests/test_web_plan.py` (use FastAPI `TestClient` with the app + a temp DB and a configured token, mirroring existing web tests).

**Steps:** Failing test asserts `200` with `{"active":null,"draft":null}` on an empty DB, and that after inserting+committing a plan the `active` block carries `workouts`, `weekly_mileage`, `predicted_finish_seconds`, `adherence_pct`. Implement handler: load active + draft via `plans.get_active_plan`/`get_draft_plan`, assemble each with `plans.build_plan_detail` (frontier from `db.last_known_daily_date()`, activities fetched once, best-effort run for Riegel from `activities`), reuse the existing training-load query for `ctl_series`. Return the bundle. Commit `feat: add GET /api/plan`.

### Task 3.2: `POST /api/plan/{plan_id}/commit` and `DELETE /api/plan/{plan_id}`

**Files:** `server.py`; `tests/test_web_plan.py`

**Steps:** Tests: commit of a draft → `200 {status:"active"}`; commit of missing id → `404`; commit of already-active/archived → `409`; delete → `200 {status:"archived"}`; delete missing → `404`. Implement with `{plan_id:int}` path params; map `PlanNotFoundError`→404, `NotDraftError`→409 (use `HTTPException`). Commit `feat: add plan commit + delete endpoints with 404/409 guards`.

### Task 3.3: CSP header (defense-in-depth)

**Files:** `server.py` `security_headers` middleware (~line 238); `tests/test_security.py`

**Steps:** Test asserts a response carries `Content-Security-Policy` containing `script-src 'self'`. Add the header in the existing `security_headers` middleware. Verify the SPA still loads (Vite-built assets are same-origin; if an inline bootstrap script exists, confirm it's allowed or hashed — check `web/dist/index.html` after build). Commit `feat: add CSP script-src self header`.

> **Verify:** after this, run `pnpm build` and load the app in the container — a too-strict CSP can blank the SPA. If the built `index.html` uses an inline module script, either allow `'self'` module scripts (external bundle is fine) or add the needed hash. Screenshot the loaded app.

---

## Phase 4 — Brief integration + plan-quality eval

### Task 4.1: Fold the plan into the workout takeaway

**Files:** Modify `src/local_fitness/agent/prompts.py` (the `briefing_prompt` workout-takeaway mandate, ~lines 228-234); Test `tests/test_brief_plan.py`

**Steps:** This is prompt text, not deterministic code — test what *is* deterministic: (a) `get_training_plan_status` returns `{active:false}` with no plan; (b) with an active plan it returns the structured status with a length-capped description. Add the prompt instruction (design §4c verbatim intent): when `get_training_plan_status` shows active, the workout takeaway opens with yesterday's adherence and prescribes today's session **reconciled against recovery (recovery wins on red-flag days)**; when inactive, produce the workout takeaway exactly as today. No new top-level field; no parallel card. Commit `feat: fold active training plan into the brief workout takeaway`.

> **Verify:** generate a brief with `uv run fitness brief` against a DB that has an active plan and one without; confirm the no-plan brief contains zero plan content and the active one folds it into the workout card. (LLM-dependent — eyeball once, then rely on the eval below.)

### Task 4.2: Plan-quality eval

**Files:** Create `tests/eval/test_plan_quality.py` (or extend the existing prompt-scorer harness referenced in `reference_ci_release`); a scorer that, given a generated plan, checks: weekly ramp ≤ ~10–15%/week, a taper in the final 1–2 weeks (declining volume), longest run within sane bounds for the goal, and race_date/goal alignment. Gate at a threshold. Commit `test: add training-plan quality eval`.

---

## Phase 5 — Frontend tab

### Task 5.1: Types + API client

**Files:** Modify `web/src/lib/types.ts` (add `PlanWorkout`, `TrainingPlan`, `PlanDetail`, `PlanResponse`); `web/src/lib/api.ts` (add `plan()`, `commitPlan(id)`, `deletePlan(id)` following the `getJson`/POST idioms at lines 80-120).

**Steps:** Add types mirroring the `PlanDetail` shape; add the three client methods. `pnpm tsc --noEmit` green. Commit `feat: add training-plan API client + types`.

### Task 5.2: Route + nav + page shell + empty state

**Files:** `web/src/main.tsx` (add `<Route path="plan" element={<TrainingPlan />} />` inside the `<Route element={<App />}>` block, ~lines 13-27); `web/src/components/Sidebar.tsx` (add one `items` entry, ~lines 5-9, with a lucide icon e.g. `Target`); Create `web/src/components/TrainingPlan.tsx`.

**Steps:** `TrainingPlan.tsx` fetches `api.plan()` on mount. When `active` and `draft` are both null → render the **empty state**: a prominent "Create a training plan" CTA that seeds the embedded `ChatPanel` (via `seedRequest`) with a starter prompt. Otherwise render the sections (Tasks 5.3-5.6). Screenshot empty state. Commit `feat: add Training Plan route, nav item, and empty state`.

### Task 5.3: GoalHeader

**Files:** `web/src/components/TrainingPlan.tsx` (or a `plan/GoalHeader.tsx`)

Render race type, countdown to `race_date`, target time, **computed `predicted_finish_seconds` vs target** (color by on/off track), `adherence_pct` stat, and `ability_snapshot` as **escaped text** (default JSX — never `dangerouslySetInnerHTML`). Include Create-new and Delete controls. **Delete of the active plan and commit-while-active both prompt a confirm dialog** ("This will replace/remove your current active plan"). Reuse `StatCard`/`Card` primitives + `fmt*` helpers from `@/lib/utils`. Screenshot. Commit `feat: add training-plan GoalHeader with predicted finish + confirmations`.

### Task 5.4: PlanCalendarTable

Render the workout schedule as a table (reuse the `Today.tsx` table idiom, lines 261-298: `sm:hidden` stacked cards + `hidden sm:block` table), ordered by `date, seq`, each past row tagged ✓ done / ⚠ partial / ✗ missed / · pending from `verdict`, `description` rendered as escaped text. Screenshot. Commit `feat: add plan calendar/adherence table`.

### Task 5.5: WeeklyMileageChart + FitnessTrajectoryChart

`WeeklyMileageChart`: recharts `BarChart` of `weekly_mileage` (planned vs actual km), themed like `TrainingLoadChart` (`Trends.tsx:96-159`, `isAnimationActive={false}`, themed axes/tooltip). `FitnessTrajectoryChart`: actual CTL `AreaChart`/`LineChart` from `ctl_series` + a race-day `ReferenceLine` (no target ramp — design §4d). Screenshot both. Commit `feat: add weekly-mileage and CTL-trajectory charts`.

### Task 5.6: Embedded chat + live draft refresh

Drop `<ChatPanel seedRequest={...}>` into the page seeded with plan/goal context. **Re-fetch `api.plan()` when a chat turn completes** so the draft calendar/charts visibly update as the user riffs (hook into ChatPanel's stream-done — pass an `onTurnComplete?` callback prop; add it to `ChatPanel.tsx` without disturbing existing usage in `Today.tsx`). Show the **Commit Plan** button when a draft exists. Screenshot a riff→draft-update cycle. Commit `feat: embed riff chat with live draft refresh + commit button`.

---

## Phase 6 — Security regression + integration

### Task 6.1: Security tests

**Files:** `tests/test_security.py`

Add cases: `GET /api/plan`, `POST /api/plan/{id}/commit`, `DELETE /api/plan/{id}` all return `401` without a bearer token; `plan_id` rejects non-int (`422`); commit-of-nonexistent `404`, commit-of-archived `409`; a concurrent double-commit yields exactly one active row (call `plans.commit_plan` on two drafts in two threads or assert the unique-index `IntegrityError` path); assert plan strings are not passed to any raw-HTML sink (grep test or a render test that the rendered output escapes `<script>`). Commit `test: security + concurrency regression for training plans`.

### Task 6.2: Full verification + container

**Steps (no new code):**
- `uv run pytest -x` (all Python green).
- `cd web && pnpm build && pnpm tsc --noEmit` (frontend green).
- `docker compose up -d --build local-fitness`; load `https://fitness.home.local`, exercise: create a plan via chat, watch the draft populate, commit, see the tab populate and the brief fold in the plan. Screenshots of the tab (empty + populated) and the brief with an active plan.
- Devlog entry in `devlog/`.
- Bump `pyproject.toml` version + CHANGELOG entry (functionality change → release per `feedback_release_policy`).

Commit `chore: devlog + version bump for training plans`.

---

## Task dependency graph

- Phase 0 → Phase 1 (logic needs no DB but tests use schema) → Phase 2 (tools need logic + persistence) → Phase 3 (endpoints need persistence) ‖ Phase 4 (brief needs `get_training_plan_status` from Phase 2).
- Phase 5 (frontend) needs Phase 3 endpoints; 5.1→5.2→{5.3,5.4,5.5,5.6} can parallelize after the shell.
- Phase 6 last.

## Definition of done (maps to contract invariants)

All `tests/test_plans*.py`, `test_plan_tools.py`, `test_web_plan.py`, `test_brief_plan.py`, `test_security.py` green; `status` never settable via a tool (test); exactly one active plan under concurrency (test); type-aware adherence + frontier `pending` correct (tests); brief emits no plan content without an active plan (test); CSP header present; frontend builds + screenshots captured; container rebuilt and exercised.
