"""Deterministic relationship ledger — the computed half of the coach's memory.

The coach's memory has two layers (2026-07-23 design): this module computes
the FACTS — adherence streaks, step-goal streaks, repeat patterns in logged
observations, notable recent results — in tested Python, and the journal
(``journal.py``) holds the coach's own written memories. The repo-wide rule
applies unchanged: the LLM phrases a judgment, it never derives one code can
compute. Every count and date the coach cites as a "receipt" ("third missed
quality day this month — Jul 12, Jul 19, today") comes from here, so a callback
can never be an invention.

Layout mirrors ``plans.py``/``report_card.py``: a pure section (stdlib-only,
plain dicts in, fully unit-testable) above the persistence divider, DB loading
below it. The divider reuses ``plans.build_plan_detail`` for verdicts rather
than re-grading — one grader, one truth.

Step streaks are deliberately computed **as of yesterday**: today's step count
is partial all day, and a block that flipped intra-day would bust the
prompt-hash caches (``plan_coach``/``workout_coach``) on every render instead
of once per day. The trailing-3-week report-card aggregate (0.34.0,
``report_card_facts``) follows the identical rule for the identical reason:
it is computed ONLY over cards with ``activity_date`` strictly before today,
so grading today's workout can never change today's ``memory_text`` — the
render that saves the card must never invalidate its own cache key.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

from . import interpret

#: Plan workout types that count as quality sessions for miss-tracking.
QUALITY_TYPES = frozenset({"tempo", "interval", "race"})

#: Numeric observation thresholds that make a reading count toward a pattern.
#: Chosen at the scale ends (mood/energy are 1-5-ish self-reports, soreness is
#: 0-10): a pattern is repeated *bad* readings, not any readings.
OBS_THRESHOLDS = {
    "mood": ("low_mood", lambda v: v <= 2),
    "energy": ("low_energy", lambda v: v <= 3),
    "soreness": ("high_soreness", lambda v: v >= 7),
}

#: Text observation types worth surfacing by occurrence count. ``injury``
#: matters from the first log; the rest need repetition to be a pattern.
OBS_TEXT_MIN_COUNT = {"injury": 1, "feeling": 2, "note": 2}

#: Actual distance at/above this fraction of target counts as overachievement.
OVERACHIEVE_FRACTION = 1.10

_PATTERN_WINDOW_DAYS = 30
_MISS_WINDOW_DAYS = 14
_QUALITY_WINDOW_DAYS = 28
_NOTABLE_WINDOW_DAYS = 14
_STEP_WINDOW_DAYS = 60
_STREAK_ENDED_MIN = 3

#: "Past 3 weeks" of report cards, as-of-yesterday (see module docstring).
_CARD_WINDOW_DAYS = 21
#: Trend split: days 1-10 back (recent) vs 11-21 back (earlier).
_CARD_RECENT_HALF_DAYS = 10
#: Render floor — one graded workout isn't a trend worth a receipt line.
_CARD_MIN_COUNT = 2
_CARD_TREND_MIN_PER_HALF = 2


def _iso(d: date) -> str:
    return d.isoformat()


def _parse(day: str) -> date | None:
    try:
        return date.fromisoformat(day)
    except (ValueError, TypeError):
        return None


def _fmt_day(day: str) -> str:
    d = _parse(day)
    return f"{d:%b} {d.day}" if d else day


# --- pure section -----------------------------------------------------------


def plan_adherence_facts(graded_workouts: list[dict], today: str) -> dict:
    """Adherence facts from already-graded plan workouts (the
    ``build_plan_detail``/``verdict`` rows — this function never grades).

    Streaks walk backward from the most recent settled (non-``pending``)
    workout on/before ``today``; ``compliant`` rest days are neutral — they
    neither extend nor break either streak. ``partial`` breaks both.
    """
    t = _parse(today)
    settled = sorted(
        (
            w for w in graded_workouts
            if w.get("verdict") not in (None, "pending")
            and (d := _parse(w.get("date") or "")) is not None
            and t is not None and d <= t
        ),
        key=lambda w: (w["date"], w.get("seq") or 1),
        reverse=True,
    )

    miss_streak = done_streak = 0
    counting_miss = counting_done = True
    for w in settled:
        v = w["verdict"]
        if v == "compliant":
            continue
        if counting_miss:
            if v == "missed":
                miss_streak += 1
            else:
                counting_miss = False
        if counting_done:
            if v == "done":
                done_streak += 1
            else:
                counting_done = False
        if not counting_miss and not counting_done:
            break

    def _within(w: dict, days: int) -> bool:
        d = _parse(w["date"])
        return d is not None and t is not None and (t - d).days < days

    misses = [w for w in settled if w["verdict"] == "missed"]
    misses_14d = sum(1 for w in misses if _within(w, _MISS_WINDOW_DAYS))
    quality_misses_28d = sum(
        1 for w in misses
        if w.get("type") in QUALITY_TYPES and _within(w, _QUALITY_WINDOW_DAYS)
    )
    last_miss = (
        {"date": misses[0]["date"], "type": misses[0].get("type")}
        if misses else None
    )
    return {
        "miss_streak": miss_streak,
        "done_streak": done_streak,
        "misses_14d": misses_14d,
        "quality_misses_28d": quality_misses_28d,
        "last_miss": last_miss,
    }


def step_streak_facts(daily_steps: list[dict], goal: int, today: str) -> dict:
    """Step-goal streaks as of YESTERDAY (see module docstring for why today's
    partial count is excluded). ``daily_steps`` rows are ``{"date", "steps"}``;
    a day missing from the data breaks a walk (no data is not a hit or a miss).
    """
    empty = {
        "current_hit_streak": 0, "current_miss_streak": 0,
        "best_streak_60d": 0, "streak_ended": None,
    }
    t = _parse(today)
    if t is None or not goal or goal <= 0:
        return empty
    by_date = {
        r["date"]: r.get("steps")
        for r in daily_steps
        if r.get("date") and r.get("steps") is not None
    }
    if not by_date:
        return empty

    yesterday = t - timedelta(days=1)

    def _hit(d: date) -> bool | None:
        steps = by_date.get(_iso(d))
        if steps is None:
            return None
        return steps >= goal

    # Walk back from yesterday: first the current streak (hits or misses),
    # then — if the current state is a miss streak — the hit streak that
    # preceded it, which is the streak that just ended.
    current_hit = current_miss = 0
    d = yesterday
    first = _hit(d)
    if first is True:
        while _hit(d) is True:
            current_hit += 1
            d -= timedelta(days=1)
    elif first is False:
        while _hit(d) is False:
            current_miss += 1
            d -= timedelta(days=1)

    streak_ended = None
    if current_miss > 0:
        ended_len = 0
        while _hit(d) is True:
            ended_len += 1
            d -= timedelta(days=1)
        if ended_len >= _STREAK_ENDED_MIN:
            # The streak "ended" on the first miss day after it.
            streak_ended = {
                "date": _iso(yesterday - timedelta(days=current_miss - 1)),
                "length": ended_len,
            }

    best = run = 0
    for back in range(_STEP_WINDOW_DAYS - 1, -1, -1):
        h = _hit(yesterday - timedelta(days=back))
        run = run + 1 if h is True else 0
        best = max(best, run)

    return {
        "current_hit_streak": current_hit,
        "current_miss_streak": current_miss,
        "best_streak_60d": best,
        "streak_ended": streak_ended,
    }


def observation_patterns(observations: list[dict], today: str) -> list[dict]:
    """Repeat-pattern counts over the trailing 30 days. Numeric types count
    readings past their threshold (``OBS_THRESHOLDS``); text types count
    occurrences (``OBS_TEXT_MIN_COUNT``) — no keyword NLP, Python counts and
    the LLM phrases. Rows are ``observations``-table dicts."""
    t = _parse(today)
    if t is None:
        return []
    counts: dict[str, dict] = {}
    for row in observations:
        d = _parse(row.get("observed_on") or "")
        if d is None or (t - d).days >= _PATTERN_WINDOW_DAYS or d > t:
            continue
        obs_type = row.get("obs_type")
        if obs_type in OBS_THRESHOLDS:
            name, test = OBS_THRESHOLDS[obs_type]
            v = row.get("value_num")
            if v is None or not test(v):
                continue
        elif obs_type in OBS_TEXT_MIN_COUNT:
            name = f"{obs_type}_logged"
        else:
            continue
        entry = counts.setdefault(
            name, {"pattern": name, "obs_type": obs_type, "count": 0,
                   "window_days": _PATTERN_WINDOW_DAYS, "last_date": None})
        entry["count"] += 1
        if entry["last_date"] is None or row["observed_on"] > entry["last_date"]:
            entry["last_date"] = row["observed_on"]

    out = []
    for entry in counts.values():
        floor = OBS_TEXT_MIN_COUNT.get(entry["obs_type"], 2)
        if entry["count"] >= floor:
            out.append(entry)
    return sorted(out, key=lambda e: (-e["count"], e["pattern"]))


def notable_results(graded_workouts: list[dict], today: str) -> list[dict]:
    """Recent results worth remembering FOR the athlete: quality sessions done
    as prescribed, and any day run at/over ``OVERACHIEVE_FRACTION`` of target.
    Last 14 days, newest first."""
    t = _parse(today)
    if t is None:
        return []
    out = []
    for w in graded_workouts:
        d = _parse(w.get("date") or "")
        if d is None or d > t or (t - d).days >= _NOTABLE_WINDOW_DAYS:
            continue
        if w.get("verdict") != "done":
            continue
        wtype = w.get("type")
        target = w.get("target_distance_m")
        actual = w.get("actual_distance_m")
        overachieved = bool(
            target and actual and actual >= OVERACHIEVE_FRACTION * target)
        if wtype in QUALITY_TYPES:
            out.append({"date": w["date"], "type": wtype,
                        "kind": "quality_done"})
        elif overachieved:
            out.append({"date": w["date"], "type": wtype,
                        "kind": "overachieved"})
    return sorted(out, key=lambda r: r["date"], reverse=True)


def report_card_facts(cards: list[dict], today: str) -> dict:
    """Trailing-3-week report-card aggregate, as of YESTERDAY.

    Only rows with ``activity_date`` strictly before ``today`` count — the
    same discipline as ``step_streak_facts`` (see module docstring): a card
    saved for TODAY's workout must never change today's ``memory_text``,
    since that render's own cache key depends on it. Enforced here
    independently of any SQL filter the caller applied, since this function
    is exercised directly on fabricated rows in tests.

    ``cards`` rows carry ``activity_date``/``gpa``/``overall_grade`` (the
    ``report_cards`` row shape). A row with no ``gpa`` or no
    ``overall_grade`` graded nothing usable and is skipped entirely — it
    never counts, never enters the mean.
    """
    t = _parse(today)
    in_window = []
    for row in cards:
        d = _parse(row.get("activity_date") or "")
        if d is None or t is None:
            continue
        age = (t - d).days
        if not (1 <= age <= _CARD_WINDOW_DAYS):
            continue
        if row.get("gpa") is None or not row.get("overall_grade"):
            continue
        in_window.append({**row, "_age": age})

    count = len(in_window)
    if count == 0:
        return {
            "count": 0, "mean_gpa": None, "grade_counts": {},
            "trend": "no data", "window_days": _CARD_WINDOW_DAYS,
        }

    mean_gpa = round(sum(r["gpa"] for r in in_window) / count, 2)

    grade_counts: dict[str, int] = {}
    for r in in_window:
        letter = r["overall_grade"][0]
        if letter in "ABCDF":
            grade_counts[letter] = grade_counts.get(letter, 0) + 1

    recent = [r["gpa"] for r in in_window if r["_age"] <= _CARD_RECENT_HALF_DAYS]
    earlier = [r["gpa"] for r in in_window if r["_age"] > _CARD_RECENT_HALF_DAYS]
    if len(recent) >= _CARD_TREND_MIN_PER_HALF and len(earlier) >= _CARD_TREND_MIN_PER_HALF:
        trend = interpret.delta_direction(
            interpret.pct_change(sum(recent) / len(recent), sum(earlier) / len(earlier)))
    else:
        trend = "no data"

    return {
        "count": count, "mean_gpa": mean_gpa, "grade_counts": grade_counts,
        "trend": trend, "window_days": _CARD_WINDOW_DAYS,
    }


def compute_ledger(plan_facts: dict | None, step_facts: dict | None,
                   obs_patterns: list[dict], notables: list[dict],
                   today: str, *, card_facts: dict | None = None) -> dict:
    return {
        "as_of": today,
        "plan": plan_facts,
        "steps": step_facts,
        "patterns": obs_patterns,
        "notables": notables,
        "cards": card_facts,
    }


def render_ledger_block(ledger: dict, user_name: str) -> str:
    """Deterministic receipt lines the coach may quote verbatim. Every number
    here is a computed fact; the block is the ONLY source the prompt allows
    callbacks to cite. Empty ledger → ``""`` (caller omits the section)."""
    lines: list[str] = []

    plan = ledger.get("plan") or {}
    if plan:
        if plan.get("miss_streak", 0) >= 2:
            lines.append(
                f"Plan: {plan['miss_streak']} prescribed sessions missed in a row.")
        elif plan.get("misses_14d", 0) > 0:
            lm = plan.get("last_miss") or {}
            last = (
                f" (last: {_fmt_day(lm['date'])} {lm.get('type') or 'session'})"
                if lm.get("date") else "")
            lines.append(
                f"Plan: {plan['misses_14d']} missed "
                f"session{'s' if plan['misses_14d'] != 1 else ''} in the last "
                f"14 days{last}.")
        elif plan.get("done_streak", 0) >= 2:
            lines.append(
                f"Plan: {plan['done_streak']} straight prescribed sessions hit.")
        if plan.get("quality_misses_28d", 0) > 0:
            lines.append(
                f"Quality days: {plan['quality_misses_28d']} "
                f"skipped in the last 28 days.")

    steps = ledger.get("steps") or {}
    ended = steps.get("streak_ended")
    if ended:
        lines.append(
            f"Steps: a {ended['length']}-day goal streak ended "
            f"{_fmt_day(ended['date'])}.")
    elif steps.get("current_miss_streak", 0) >= 2:
        lines.append(
            f"Steps: {steps['current_miss_streak']} straight days under goal "
            f"(through yesterday).")
    elif steps.get("current_hit_streak", 0) >= 2:
        line = (f"Steps: goal hit {steps['current_hit_streak']} days running "
                f"(through yesterday)")
        best = steps.get("best_streak_60d", 0)
        if best > steps["current_hit_streak"]:
            line += f"; best in 60 days is {best}"
        lines.append(line + ".")

    cards = ledger.get("cards") or {}
    if cards.get("count", 0) >= _CARD_MIN_COUNT and cards.get("mean_gpa") is not None:
        counts = cards.get("grade_counts") or {}
        dist = " ".join(f"{g}:{counts[g]}" for g in "ABCDF" if counts.get(g))
        line = (f"Report cards: {cards['count']} workouts graded in the last "
                f"3 weeks (through yesterday) — avg GPA {cards['mean_gpa']:.2f}")
        if dist:
            line += f" ({dist})"
        if cards.get("trend") in ("rising", "falling"):
            line += f"; grades {cards['trend']}"
        lines.append(line + ".")

    _PATTERN_PHRASES = {
        "low_mood": "Mood logged at 2/5 or below",
        "low_energy": "Energy logged at 3/5 or below",
        "high_soreness": "Soreness logged at 7/10 or above",
        "injury_logged": "Injury flagged",
        "feeling_logged": "A 'feeling' observation logged",
        "note_logged": "A self-note logged",
    }
    for p in ledger.get("patterns") or []:
        phrase = _PATTERN_PHRASES.get(p["pattern"])
        if not phrase:
            continue
        last = f" (last: {_fmt_day(p['last_date'])})" if p.get("last_date") else ""
        lines.append(
            f"{phrase} {p['count']}x in {p['window_days']} days{last}.")

    _NOTABLE_PHRASES = {
        "quality_done": "{type} day done as prescribed",
        "overachieved": "{type} day run past its target",
    }
    for n in ledger.get("notables") or []:
        phrase = _NOTABLE_PHRASES.get(n["kind"])
        if phrase:
            lines.append(
                f"{_fmt_day(n['date'])}: {phrase.format(type=n['type'])}.")

    if not lines:
        return ""
    return "\n".join(f"- {line}" for line in lines)


# --- persistence divider ----------------------------------------------------
# Everything below reads the DB. Loading reuses the already-tested graders and
# accessors (plans.build_plan_detail, db.get_setting) rather than re-deriving.


def load_ledger_inputs(conn: sqlite3.Connection, today: str,
                       *, step_goal: int) -> dict:
    from .. import db, plans

    active = plans.get_active_plan(conn=conn)
    graded: list[dict] = []
    if active is not None:
        frontier = db.last_known_daily_date(conn=conn)
        dates = [w["date"] for w in active["workouts"]] or [today]
        start = min(dates)
        end = max([today, *dates] + ([frontier] if frontier else []))
        activities_by_date = plans.load_activities_by_date(start, end, conn=conn)
        cfg = plans.resolve_grading_config(conn=conn)
        detail = plans.build_plan_detail(active, frontier, activities_by_date, cfg=cfg)
        graded = detail["workouts"]

    t = _parse(today) or date.today()
    window_start = _iso(t - timedelta(days=_STEP_WINDOW_DAYS + 1))
    steps_rows = [
        {"date": r[0], "steps": r[1]}
        for r in conn.execute(
            "SELECT date, steps FROM daily_metrics "
            "WHERE date >= ? AND date <= ? AND steps IS NOT NULL ORDER BY date",
            (window_start, today),
        )
    ]
    obs_rows = [
        {"observed_on": r[0], "obs_type": r[1], "value_num": r[2],
         "value_text": r[3]}
        for r in conn.execute(
            "SELECT observed_on, obs_type, value_num, value_text "
            "FROM observations WHERE observed_on >= ? AND observed_on <= ? "
            "ORDER BY observed_on",
            (window_start, today),
        )
    ]
    card_window_start = _iso(t - timedelta(days=_CARD_WINDOW_DAYS))
    try:
        card_rows = [
            {"activity_date": r[0], "gpa": r[1], "overall_grade": r[2]}
            for r in conn.execute(
                "SELECT activity_date, gpa, overall_grade FROM report_cards "
                "WHERE activity_date >= ? AND activity_date < ? "
                "ORDER BY activity_date",
                (card_window_start, today),
            )
        ]
    except sqlite3.OperationalError:
        # Pre-0.32.0 DB with no report_cards table: costs this one line,
        # never the whole memory block (memory.py fails the whole render to
        # "" on any exception, which is a much bigger loss).
        card_rows = []
    return {
        "graded_workouts": graded,
        "daily_steps": steps_rows,
        "observations": obs_rows,
        "report_cards": card_rows,
        "step_goal": step_goal,
    }


def compute_relationship_ledger(
    db_path: Path | None = None,
    conn: sqlite3.Connection | None = None,
    today: str | None = None,
) -> dict:
    """The full ledger from the live DB. Opens (and closes) its own connection
    unless handed one. Never called from a perf-benchmarked hot path — memory
    resolution happens only at SDK-call sites."""
    from .. import db

    today = today or _iso(date.today())

    def _run(c: sqlite3.Connection) -> dict:
        goal = int(db.get_setting("daily_step_goal", "10000", conn=c) or "10000")
        inputs = load_ledger_inputs(c, today, step_goal=goal)
        plan_facts = plan_adherence_facts(inputs["graded_workouts"], today)
        step_facts = step_streak_facts(
            inputs["daily_steps"], inputs["step_goal"], today)
        patterns = observation_patterns(inputs["observations"], today)
        notables = notable_results(inputs["graded_workouts"], today)
        card_facts = report_card_facts(inputs["report_cards"], today)
        return compute_ledger(plan_facts, step_facts, patterns, notables,
                              today, card_facts=card_facts)

    if conn is not None:
        return _run(conn)
    with db.connect(db_path) as c:
        return _run(c)
