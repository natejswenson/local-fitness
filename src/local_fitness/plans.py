"""Training-plan pure logic — no I/O.

Validation, type-aware adherence, data-frontier grading, Riegel projection,
and weekly-mileage rollup. These functions take plain dicts/rows and return
verdicts/numbers so they unit-test cleanly and are reused by the agent tools
(``agent/tools.py``) and the REST endpoints (``web/server.py``). The DB
persistence helpers live below the pure section.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date as _date
from pathlib import Path

from . import config, db

# --- constants -------------------------------------------------------------

DONE_FRACTION = 0.80
PARTIAL_FRACTION = 0.40


# --- user-tunable grading config -------------------------------------------

@dataclass(frozen=True)
class GradingConfig:
    """Resolved grading knobs threaded into the pure grading functions. Field
    defaults equal the module constants, so a default ``GradingConfig()``
    reproduces the historical hardcoded behavior exactly (existing tests pass
    no ``cfg``)."""
    done_fraction: float = DONE_FRACTION
    partial_fraction: float = PARTIAL_FRACTION
    count_walks_easy: bool = True
    count_walks_mileage: bool = False


def resolve_grading_config(db_path=None) -> GradingConfig:
    """Read the grading knobs (DB > env > default), validate, and build a
    ``GradingConfig``. Does I/O (one batched settings read); callers resolve it
    once per request and thread the result into the pure grading functions.

    The fraction pair must satisfy ``0 <= partial <= done <= 1``; on any
    violation BOTH revert to their defaults so the grade bands can never invert
    (a ``partial > done`` would make ``partial`` unreachable)."""
    settings = db.all_settings(db_path=db_path)
    done = config._resolve_from(
        settings, "grade_done_fraction", "LOCAL_FITNESS_GRADE_DONE_FRACTION",
        DONE_FRACTION, float)
    partial = config._resolve_from(
        settings, "grade_partial_fraction", "LOCAL_FITNESS_GRADE_PARTIAL_FRACTION",
        PARTIAL_FRACTION, float)
    if not (0 <= partial <= done <= 1):
        done, partial = DONE_FRACTION, PARTIAL_FRACTION
    return GradingConfig(
        done_fraction=done,
        partial_fraction=partial,
        count_walks_easy=config._resolve_from(
            settings, "count_walks_easy", "LOCAL_FITNESS_COUNT_WALKS_EASY",
            config.DEFAULT_COUNT_WALKS_EASY, config._as_bool),
        count_walks_mileage=config._resolve_from(
            settings, "count_walks_mileage", "LOCAL_FITNESS_COUNT_WALKS_MILEAGE",
            config.DEFAULT_COUNT_WALKS_MILEAGE, config._as_bool),
    )

GOAL_TYPES = frozenset({"5k", "10k", "half", "full", "custom"})
WORKOUT_TYPES = frozenset({"easy", "long", "tempo", "interval", "rest", "race", "cross"})

MAX_WORKOUTS = 200
RIEGEL_EXP = 1.06

#: plan-quality gate: a week may grow at most ~15% over the prior week
#: (the safe-progression rule), with a small additive slack for float edges.
RAMP_CEILING = 1.15
RAMP_TOLERANCE_KM = 0.5

#: canonical race distances (metres); 'custom' has no canonical distance
GOAL_DISTANCE_M = {"5k": 5000.0, "10k": 10000.0, "half": 21097.5, "full": 42195.0}

#: substrings that mark an activity_type as a run
_RUNNING_SUBSTRINGS = ("running", "run")

#: substrings that mark an activity_type as a walk/hike (on-foot, non-running).
#: NOTE: before relying on this in production, confirm `SELECT DISTINCT
#: activity_type FROM activities` has no type that spuriously contains "walk"/
#: "hik" — observed types are running, treadmill_running, walking (no collision).
_WALKING_SUBSTRINGS = ("walk", "hik")

# workout types graded on distance vs. duration
_DISTANCE_TYPES = frozenset({"easy", "long", "race"})
_DURATION_TYPES = frozenset({"interval", "tempo"})

# numeric workout fields that, when present, must be finite and non-negative
_NUMERIC_FIELDS = ("target_distance_m", "target_pace_sec_per_km",
                   "target_duration_sec", "week_index", "seq")


# --- helpers ---------------------------------------------------------------

def _is_running(activity_type: str | None) -> bool:
    at = (activity_type or "").lower()
    return any(s in at for s in _RUNNING_SUBSTRINGS)


def _is_walking(activity_type: str | None) -> bool:
    at = (activity_type or "").lower()
    return any(s in at for s in _WALKING_SUBSTRINGS)


def _is_on_foot(activity_type: str | None) -> bool:
    """Running OR walking — what counts toward easy/recovery foot distance."""
    return _is_running(activity_type) or _is_walking(activity_type)


def _parse_iso(value: str) -> _date | None:
    try:
        return _date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _running_distance(activities: list[dict]) -> float:
    return sum(
        (a.get("distance_meters") or 0.0)
        for a in activities
        if _is_running(a.get("activity_type"))
    )


def _running_duration(activities: list[dict]) -> float:
    return sum(
        (a.get("duration_seconds") or 0.0)
        for a in activities
        if _is_running(a.get("activity_type"))
    )


def _foot_distance(activities: list[dict]) -> float:
    """Distance (m) from on-foot activities — running OR walking. Used for
    easy/recovery grading and for surfaced actuals (a recovery walk counts)."""
    return sum(
        (a.get("distance_meters") or 0.0)
        for a in activities
        if _is_on_foot(a.get("activity_type"))
    )


def _foot_duration(activities: list[dict]) -> float:
    return sum(
        (a.get("duration_seconds") or 0.0)
        for a in activities
        if _is_on_foot(a.get("activity_type"))
    )


def _normalize_activity_types(activities: list[dict]) -> list[str]:
    """Normalized, deduped, sorted activity classes for a day: running | walking
    | other. Surfaced so the plan view/agent can say 'walked' vs 'ran'."""
    classes: set[str] = set()
    for a in activities:
        at = a.get("activity_type")
        if _is_running(at):
            classes.add("running")
        elif _is_walking(at):
            classes.add("walking")
        elif at:
            classes.add("other")
    return sorted(classes)


# --- Task 1.1: validation --------------------------------------------------

def validate_plan_input(
    goal_type: str,
    race_date: str,
    workouts: list[dict],
    created_date: str,
    goal_distance_m: float | None = None,
    target_time_seconds: int | None = None,
) -> str | None:
    """Return an error string, or ``None`` if the plan input is well-formed."""
    if goal_type not in GOAL_TYPES:
        return f"unknown goal_type '{goal_type}'; expected one of {sorted(GOAL_TYPES)}"

    race = _parse_iso(race_date)
    if race is None:
        return f"race_date '{race_date}' is not an ISO date"
    created = _parse_iso(created_date)
    if created is None:
        return f"created_date '{created_date}' is not an ISO date"

    if not isinstance(workouts, list) or not workouts:
        return "at least one workout is required"
    if len(workouts) > MAX_WORKOUTS:
        return f"too many workouts ({len(workouts)} > {MAX_WORKOUTS})"

    for n in (goal_distance_m, target_time_seconds):
        if n is not None and (not math.isfinite(n) or n < 0):
            return "goal_distance_m and target_time_seconds must be finite and non-negative"

    seen: set[tuple[str, int]] = set()
    for i, w in enumerate(workouts):
        wtype = w.get("type")
        if wtype not in WORKOUT_TYPES:
            return f"workout {i}: unknown type '{wtype}'; expected one of {sorted(WORKOUT_TYPES)}"

        wdate = _parse_iso(w.get("date"))
        if wdate is None:
            return f"workout {i}: date '{w.get('date')}' is not an ISO date"
        if wdate < created or wdate > race:
            return f"workout {i}: date {w.get('date')} outside [created, race_date]"

        for field in _NUMERIC_FIELDS:
            v = w.get(field)
            if v is None:
                continue
            # Reject wrong-typed values with the function's clean indexed error
            # rather than letting math.isfinite() raise a raw TypeError. Exclude
            # bool explicitly: isinstance(True, int) is True in Python.
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                return f"workout {i}: {field} must be a number"
            if not math.isfinite(v) or v < 0:
                return f"workout {i}: {field} must be finite and non-negative"

        desc = w.get("description")
        # Reject a non-string description with a clean indexed error rather than
        # letting .strip() raise a raw AttributeError on a dict/list.
        if desc is not None and not isinstance(desc, str):
            return f"workout {i}: description must be a string"
        if not (desc or "").strip():
            return f"workout {i}: description is required"

        seq = int(w.get("seq") or 1)
        key = (w["date"], seq)
        if key in seen:
            return f"workout {i}: duplicate (date, seq) {key}"
        seen.add(key)

    return None


# --- Task 1.2: type-aware adherence ---------------------------------------

def classify_workout(
    workout: dict, day_activities: list[dict], cfg: GradingConfig = GradingConfig()
) -> str:
    """Grade one prescribed workout against that day's activities.

    Returns ``done`` | ``partial`` | ``missed`` | ``compliant`` (rest days).
    Distance is used only for the types where distance is the target; quality
    sessions grade on duration, cross-training on any non-running activity.
    ``cfg`` carries the user's tunable thresholds and walk-counting toggle; the
    default reproduces the historical hardcoded behavior.
    """
    wtype = workout.get("type")

    if wtype == "rest":
        return "compliant"

    if wtype in _DISTANCE_TYPES:
        target = workout.get("target_distance_m")
        # Easy/recovery days count walking when enabled (active recovery is the
        # intent); long/race require running specificity, so walks don't count.
        actual = (
            _foot_distance(day_activities)
            if (wtype == "easy" and cfg.count_walks_easy)
            else _running_distance(day_activities)
        )
        if not target:  # null/0 target → "by feel": any qualifying activity counts
            return "done" if actual > 0 else "missed"
        frac = actual / target
        if frac >= cfg.done_fraction:
            return "done"
        if frac >= cfg.partial_fraction:
            return "partial"
        return "missed"

    if wtype in _DURATION_TYPES:
        actual = _running_duration(day_activities)
        if actual <= 0:
            return "missed"
        target = workout.get("target_duration_sec")
        if target and actual < cfg.partial_fraction * target:
            return "partial"
        return "done"

    if wtype == "cross":
        has_cross = any(
            not _is_running(a.get("activity_type")) for a in day_activities
        )
        return "done" if has_cross else "missed"

    # unknown type: treat as missed unless something happened
    return "done" if day_activities else "missed"


# --- Task 1.3: data-frontier grading --------------------------------------

def grade_workout(
    workout: dict, day_activities: list[dict], frontier: str | None,
    cfg: GradingConfig = GradingConfig(),
) -> str:
    """Grade a prescribed workout, holding not-yet-credited days as ``pending``.

    Grade first, then keep ``pending`` only when the verdict is a *negative* one
    (``missed`` or ``partial``) AND the day's data window is still open — i.e. at
    or after the data frontier (``db.last_known_daily_date()``), the most recent
    day Garmin data has arrived for. A ``done``/``compliant`` day grades
    immediately, even today: a completed workout that's already synced should
    show its verdict, not ``pending``. Holding ``partial`` too prevents a mid-day
    half-done run from prematurely counting 0.5 in adherence and then self-healing
    later in the day. ISO ``YYYY-MM-DD`` strings compare lexicographically in date
    order, so plain string comparison is safe.
    """
    verdict = classify_workout(workout, day_activities, cfg)
    if verdict in ("missed", "partial") and (
        frontier is None or workout.get("date", "") >= frontier
    ):
        return "pending"
    return verdict


# --- Task 1.4: Riegel projection + weekly mileage -------------------------

def riegel_predict(
    best_distance_m: float | None,
    best_time_s: float | None,
    target_distance_m: float | None,
) -> float | None:
    """Riegel endurance projection: t2 = t1 * (d2/d1)^1.06. ``None`` if unknown."""
    if not best_distance_m or not best_time_s or not target_distance_m:
        return None
    return best_time_s * (target_distance_m / best_distance_m) ** RIEGEL_EXP


def weekly_mileage(
    workouts: list[dict], activities_by_date: dict[str, list[dict]],
    cfg: GradingConfig = GradingConfig(),
) -> list[dict]:
    """Planned vs. actual km per ``week_index`` (actual counts each date once).

    Actual mileage is running-only by default (it is a run-volume metric); when
    ``cfg.count_walks_mileage`` is set it includes walking too."""
    dist_fn = _foot_distance if cfg.count_walks_mileage else _running_distance
    planned: dict[int, float] = {}
    week_dates: dict[int, set[str]] = {}
    for w in workouts:
        wk = int(w.get("week_index") or 0)
        planned[wk] = planned.get(wk, 0.0) + (w.get("target_distance_m") or 0.0)
        week_dates.setdefault(wk, set()).add(w.get("date"))

    rows = []
    for wk in sorted(planned):
        actual_m = sum(
            dist_fn(activities_by_date.get(d, []))
            for d in week_dates.get(wk, set())
        )
        rows.append({
            "week": wk,
            "planned_km": round(planned[wk] / 1000.0, 1),
            "actual_km": round(actual_m / 1000.0, 1),
        })
    return rows


def score_plan(workouts: list[dict], race_date: str | None = None) -> dict:
    """Deterministic structural quality gate for a generated plan.

    Checks that weekly mileage ramps safely (≤ ~15%/week) and tapers into the
    race (final week below the peak). Free to run — no model call — so it can
    gate plan generation in CI alongside the LLM-authored prompt evals.
    """
    wk_km: dict[int, float] = {}
    for w in workouts:
        wk = int(w.get("week_index") or 0)
        wk_km[wk] = wk_km.get(wk, 0.0) + (w.get("target_distance_m") or 0.0) / 1000.0
    weeks = [wk_km[k] for k in sorted(wk_km)]

    ramp_ok = all(
        weeks[i] <= weeks[i - 1] * RAMP_CEILING + RAMP_TOLERANCE_KM
        for i in range(1, len(weeks))
        if weeks[i - 1] > 0
    )
    has_taper = len(weeks) >= 2 and weeks[-1] < max(weeks)
    checks = {"ramp_ok": ramp_ok, "has_taper": has_taper, "nonempty": bool(workouts)}
    score = sum(1 for v in checks.values() if v) / len(checks)
    return {**checks, "score": round(score, 2)}


# ===========================================================================
# Persistence — the agent→SQLite write path.
#
# Plan *structure* is still draft-gated: `insert_draft` hardcodes 'draft',
# `revise_draft` is draft-only, and activation/deletion (commit_plan/delete_plan)
# flip status. Single-active is enforced by the partial unique index; commit
# relies on it as the race backstop.
#
# Per-day prescriptions on the ACTIVE plan, however, are agent-writable via
# `update_active_workout` — the agent is the plan write path and the web UI is
# view-only (owner's design decision). That edit whitelists prescription columns
# only (never date/seq/week_index/plan_id/status), so it can re-prescribe a day
# but can never re-key, re-status, or restructure the plan.
# ===========================================================================

#: columns the AI may edit on a draft — status/committed_at/plan_id/created_at
#: are deliberately excluded so a tool call can never activate or re-key a plan.
_EDITABLE_PLAN_COLS = frozenset(
    {"goal_type", "race_date", "target_time_seconds", "goal_distance_m", "title"}
)

_WORKOUT_COLS = (
    "date", "seq", "week_index", "type",
    "target_distance_m", "target_pace_sec_per_km", "target_duration_sec", "description",
)

#: the prescription columns the agent may edit on an ACTIVE plan's workout.
#: date/seq/week_index/plan_id/workout_id are identity/structure — never editable
#: here, so a day can be re-prescribed but the plan can't be re-keyed or moved.
_EDITABLE_WORKOUT_COLS = frozenset(
    {"type", "target_distance_m", "target_pace_sec_per_km", "target_duration_sec", "description"}
)

#: valid plan_workouts.type values (mirrors the schema CHECK in db.py).
WORKOUT_TYPES = frozenset({"easy", "long", "tempo", "interval", "rest", "race", "cross"})


class PlanNotFoundError(Exception):
    """Raised when a plan_id does not exist."""


class NotDraftError(Exception):
    """Raised when a write/commit targets a plan that is not in 'draft' status."""


class NoActivePlanError(Exception):
    """Raised when an active-plan edit finds no active plan."""


def _insert_workouts(conn, plan_id: int, workouts: list[dict]) -> None:
    for w in workouts:
        row = {"plan_id": plan_id, **{c: w.get(c) for c in _WORKOUT_COLS}}
        if row.get("seq") is None:
            row["seq"] = 1
        cols = ", ".join(row.keys())
        ph = ", ".join(f":{k}" for k in row)
        conn.execute(f"INSERT INTO plan_workouts ({cols}) VALUES ({ph})", row)


def insert_draft(plan_fields: dict, workouts: list[dict], db_path: Path | None = None) -> int:
    """Insert a new draft plan + its workouts atomically; archive any prior draft.

    `status` is hardcoded to 'draft' — it is never taken from `plan_fields`.
    """
    row = {
        "status": "draft",  # hardcoded — never from input
        "goal_type": plan_fields["goal_type"],
        "goal_distance_m": plan_fields.get("goal_distance_m"),
        "race_date": plan_fields["race_date"],
        "target_time_seconds": plan_fields.get("target_time_seconds"),
        "title": plan_fields.get("title"),
        "ability_snapshot": _dump_snapshot(plan_fields.get("ability_snapshot")),
        "created_at": plan_fields["created_at"],
    }
    with db.connect(db_path) as conn:
        conn.execute("UPDATE training_plans SET status='archived' WHERE status='draft'")
        cols = ", ".join(row.keys())
        ph = ", ".join(f":{k}" for k in row)
        cur = conn.execute(f"INSERT INTO training_plans ({cols}) VALUES ({ph})", row)
        plan_id = cur.lastrowid
        _insert_workouts(conn, plan_id, workouts)
    return plan_id


def revise_draft(
    plan_id: int,
    fields: dict | None,
    workouts: list[dict] | None,
    db_path: Path | None = None,
) -> None:
    """Update whitelisted goal fields and/or wholesale-replace workouts.

    Guards that the target row is a draft. Rejects any field outside
    `_EDITABLE_PLAN_COLS` (so `status` can never be set through this path).
    The delete+reinsert of workouts is one transaction (atomic replace).
    """
    fields = fields or {}
    bad = set(fields) - _EDITABLE_PLAN_COLS
    if bad:
        raise ValueError(f"non-editable plan field(s): {sorted(bad)}")

    with db.connect(db_path) as conn:
        cur = conn.execute("SELECT status FROM training_plans WHERE plan_id=?", (plan_id,))
        found = cur.fetchone()
        if found is None:
            raise PlanNotFoundError(f"no plan {plan_id}")
        if found["status"] != "draft":
            raise NotDraftError(f"plan {plan_id} is '{found['status']}', not draft")

        if fields:
            sets = ", ".join(f"{c}=:{c}" for c in fields)  # keys are whitelisted
            conn.execute(
                f"UPDATE training_plans SET {sets} WHERE plan_id=:plan_id",
                {**fields, "plan_id": plan_id},
            )
        if workouts is not None:
            conn.execute("DELETE FROM plan_workouts WHERE plan_id=?", (plan_id,))
            _insert_workouts(conn, plan_id, workouts)


def update_active_workout(
    date: str,
    fields: dict,
    db_path: Path | None = None,
    seq: int = 1,
) -> dict:
    """Edit one day's prescription (by date + seq) on the ACTIVE plan.

    The agent owns plan writes — this is the active-plan counterpart to
    ``revise_draft``. Only prescription columns in ``_EDITABLE_WORKOUT_COLS`` may
    change (so a day can be re-prescribed but the plan can't be re-keyed/moved);
    ``type`` is validated against ``WORKOUT_TYPES``. Returns the updated row as a
    dict. Raises ``NoActivePlanError`` (no active plan), ``ValueError`` (bad
    field / unknown type / no workout on that date / nothing to update).
    """
    bad = set(fields) - _EDITABLE_WORKOUT_COLS
    if bad:
        raise ValueError(f"non-editable workout field(s): {sorted(bad)}")
    if not fields:
        raise ValueError("no fields to update")
    if "type" in fields and fields["type"] not in WORKOUT_TYPES:
        raise ValueError(f"unknown workout type '{fields['type']}'")

    with db.connect(db_path) as conn:
        active = conn.execute(
            "SELECT plan_id FROM training_plans WHERE status='active'"
        ).fetchone()
        if active is None:
            raise NoActivePlanError("no active plan")
        plan_id = active["plan_id"]
        sets = ", ".join(f"{c}=:{c}" for c in fields)  # keys whitelisted above
        cur = conn.execute(
            f"UPDATE plan_workouts SET {sets} "
            "WHERE plan_id=:plan_id AND date=:date AND seq=:seq",
            {**fields, "plan_id": plan_id, "date": date, "seq": seq},
        )
        if cur.rowcount == 0:
            raise ValueError(f"no workout on {date} (seq {seq}) in the active plan")
        row = conn.execute(
            "SELECT * FROM plan_workouts WHERE plan_id=? AND date=? AND seq=?",
            (plan_id, date, seq),
        ).fetchone()
        return dict(row)


def commit_plan(plan_id: int, now: str, db_path: Path | None = None) -> None:
    """Flip a draft to active, archiving any prior active plan.

    The partial unique index `idx_one_active_plan` is the race backstop: a
    concurrent second commit fails with IntegrityError rather than producing
    two active rows.
    """
    with db.connect(db_path) as conn:
        cur = conn.execute("SELECT status FROM training_plans WHERE plan_id=?", (plan_id,))
        found = cur.fetchone()
        if found is None:
            raise PlanNotFoundError(f"no plan {plan_id}")
        if found["status"] != "draft":
            raise NotDraftError(f"plan {plan_id} is '{found['status']}', not draft")
        conn.execute("UPDATE training_plans SET status='archived' WHERE status='active'")
        conn.execute(
            "UPDATE training_plans SET status='active', committed_at=? WHERE plan_id=?",
            (now, plan_id),
        )


def delete_plan(plan_id: int, db_path: Path | None = None) -> None:
    """Soft-delete: archive the plan so history survives."""
    with db.connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE training_plans SET status='archived' WHERE plan_id=?", (plan_id,)
        )
        if cur.rowcount == 0:
            raise PlanNotFoundError(f"no plan {plan_id}")


def _dump_snapshot(snapshot) -> str | None:
    if snapshot is None:
        return None
    if isinstance(snapshot, str):
        return snapshot
    try:
        return json.dumps(snapshot)
    except (TypeError, ValueError):
        return None


def _row_to_plan(row) -> dict:
    plan = dict(row)
    raw = plan.get("ability_snapshot")
    if raw:
        try:
            plan["ability_snapshot"] = json.loads(raw)
        except (TypeError, ValueError):
            pass  # leave as-is (best-effort, never trusted)
    return plan


def _load_workouts(conn, plan_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM plan_workouts WHERE plan_id=? ORDER BY date, seq", (plan_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_plan(plan_id: int, db_path: Path | None = None) -> dict | None:
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM training_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            return None
        plan = _row_to_plan(row)
        plan["workouts"] = _load_workouts(conn, plan_id)
        return plan


def _get_by_status(status: str, db_path: Path | None) -> dict | None:
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM training_plans WHERE status=? ORDER BY plan_id DESC LIMIT 1",
            (status,),
        ).fetchone()
        if row is None:
            return None
        plan = _row_to_plan(row)
        plan["workouts"] = _load_workouts(conn, plan["plan_id"])
        return plan


def get_active_plan(db_path: Path | None = None) -> dict | None:
    return _get_by_status("active", db_path)


def load_activities_by_date(
    start: str, end: str, db_path: Path | None = None
) -> dict[str, list[dict]]:
    """Activities in [start, end] grouped by date — input to adherence grading."""
    out: dict[str, list[dict]] = {}
    with db.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT date, activity_type, distance_meters, duration_seconds "
            "FROM activities WHERE date >= ? AND date <= ? ORDER BY date",
            (start, end),
        ).fetchall()
    for r in rows:
        out.setdefault(r["date"], []).append(dict(r))
    return out


def best_recent_effort(
    cutoff: str, db_path: Path | None = None, min_distance_m: float = 2000.0
) -> dict | None:
    """Fastest recent running effort since `cutoff` as {distance_m, time_s} for Riegel."""
    with db.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT activity_type, distance_meters, duration_seconds, avg_pace_sec_per_km "
            "FROM activities WHERE date >= ? AND distance_meters >= ? "
            "AND avg_pace_sec_per_km IS NOT NULL",
            (cutoff, min_distance_m),
        ).fetchall()
    best = None
    best_pace = None
    for r in rows:
        if not _is_running(r["activity_type"]):
            continue
        pace = r["avg_pace_sec_per_km"]
        if best_pace is None or pace < best_pace:
            best = {"distance_m": r["distance_meters"], "time_s": r["duration_seconds"]}
            best_pace = pace
    return best


def get_draft_plan(db_path: Path | None = None) -> dict | None:
    return _get_by_status("draft", db_path)


# --- assembly for the tab + brief -----------------------------------------

def _adherence_pct(graded_workouts: list[dict]) -> int | None:
    """Percent adherence over graded (non-pending) workouts. partial = half."""
    graded = [w for w in graded_workouts if w["verdict"] != "pending"]
    if not graded:
        return None
    credit = {"done": 1.0, "compliant": 1.0, "partial": 0.5, "missed": 0.0}
    score = sum(credit.get(w["verdict"], 0.0) for w in graded)
    return round(100 * score / len(graded))


def _workout_actuals(
    day_activities: list[dict],
) -> tuple[float, float | None, list[str]]:
    """Foot-based actual distance (m), aggregate pace (sec/km), and the day's
    normalized activity classes — surfaced so a recovery walk is visible.

    Distance and pace cover on-foot activity (running + walking), so on a
    walk-only day ``pace`` is *walking* pace: this is the actual pace of what was
    done, not specifically running pace. ``activity_types`` is the normalized,
    deduped, sorted set of activity classes for the day (``running``/``walking``/
    ``other``). Surfacing is foot-based on every day regardless of workout type;
    the verdict's type-awareness lives in ``classify_workout``, not here.
    """
    dist = _foot_distance(day_activities)
    dur = _foot_duration(day_activities)
    pace = (dur / (dist / 1000.0)) if dist > 0 else None
    return dist, pace, _normalize_activity_types(day_activities)


def build_plan_detail(
    plan: dict,
    frontier: str | None,
    activities_by_date: dict[str, list[dict]],
    best_effort: dict | None = None,
    cfg: GradingConfig = GradingConfig(),
) -> dict:
    """Assemble the full PlanDetail the tab renders (workouts graded, rollups).

    Surfaced actuals (``actual_distance_m``/``_pace``/``_activity_types``) stay
    foot-based regardless of ``cfg`` — they show what was actually done; the
    ``verdict`` reflects whether it counted per ``cfg``."""
    graded = []
    for w in plan["workouts"]:
        day = activities_by_date.get(w["date"], [])
        actual_dist, actual_pace, actual_types = _workout_actuals(day)
        graded.append({
            **w,
            "verdict": grade_workout(w, day, frontier, cfg),
            "actual_distance_m": actual_dist,
            "actual_pace_sec_per_km": actual_pace,
            "actual_activity_types": actual_types,
        })
    predicted = None
    if best_effort:
        predicted = riegel_predict(
            best_effort.get("distance_m"), best_effort.get("time_s"),
            plan.get("goal_distance_m"),
        )
    return {
        **{k: v for k, v in plan.items() if k != "workouts"},
        "workouts": graded,
        "weekly_mileage": weekly_mileage(plan["workouts"], activities_by_date, cfg),
        "predicted_finish_seconds": predicted,
        "adherence_pct": _adherence_pct(graded),
    }


def _slim_workout(workout: dict | None) -> dict | None:
    """Structured fields only + a length-capped description (anti-injection)."""
    if workout is None:
        return None
    desc = (workout.get("description") or "")[:120]
    return {
        "type": workout.get("type"),
        "target_distance_m": workout.get("target_distance_m"),
        "target_pace_sec_per_km": workout.get("target_pace_sec_per_km"),
        "target_duration_sec": workout.get("target_duration_sec"),
        "description": desc,
        "verdict": workout.get("verdict"),
    }


def build_plan_status(
    plan: dict | None,
    frontier: str | None,
    activities_by_date: dict[str, list[dict]],
    today: str,
    cfg: GradingConfig = GradingConfig(),
) -> dict:
    """Structured status for the brief. Returns {'active': False} when no plan."""
    if plan is None:
        return {"active": False}

    graded = [
        {**w, "verdict": grade_workout(w, activities_by_date.get(w["date"], []), frontier, cfg)}
        for w in plan["workouts"]
    ]
    today_w = next((w for w in graded if w["date"] == today), None)
    last_graded = next(
        (w for w in sorted(graded, key=lambda x: x["date"], reverse=True)
         if w["verdict"] != "pending"),
        None,
    )
    race = _parse_iso(plan["race_date"])
    today_d = _parse_iso(today)
    days_to_race = (race - today_d).days if race and today_d else None

    return {
        "active": True,
        "goal_type": plan.get("goal_type"),
        "race_date": plan.get("race_date"),
        "target_time_seconds": plan.get("target_time_seconds"),
        "days_to_race": days_to_race,
        "adherence_pct": _adherence_pct(graded),
        "today": _slim_workout(today_w),
        "last_graded": _slim_workout(last_graded),
    }
