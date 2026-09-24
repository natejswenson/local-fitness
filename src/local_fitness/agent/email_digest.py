"""Small, factual evening digest. Reads are separate from pure composition.

The email is an outline, not another report or a coaching-generation surface.
Missing input stays missing; no model, chart, Garmin call or write happens here.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path

from .. import db, plans
from . import calendar_render, interpret, units
from .schemas import Brief

LOG = logging.getLogger(__name__)
MAX_WORDS = 80
FULL_SESSION_CUE = "Outline only. Full instructions in your fitness chat."
BRIEF_CUE = "Your briefing needs a closer look. Ask your fitness chat."


@dataclass
class EmailInputs:
    daily: dict = field(default_factory=dict)
    activities: list[dict] | None = None
    tomorrow: list[dict] | None = None
    has_plan: bool | None = None
    synced_at: str | None = None
    historical: bool = False
    distance_unit: str = "mi"


def load_inputs(target: str, db_path: Path | None = None) -> EmailInputs:
    """Read only this day's facts and target+1's plan; fail soft by section.

    Passing the path explicitly avoids get_db_path's mkdir on fresh clones.
    Archived dates do not reuse a present-day sync as historical provenance.
    """
    from .tools import data_as_of_today

    next_day = (date.fromisoformat(target) + timedelta(days=1)).isoformat()
    result = EmailInputs(historical=target < date.today().isoformat(),
                         distance_unit="mi" if units.display_units() == "miles" else "km")
    try:
        with db.connect_readonly(db_path or db.DEFAULT_DB_PATH) as conn:
            try:
                row = conn.execute(
                    "SELECT steps, sleep_seconds FROM daily_metrics WHERE date=?", (target,)
                ).fetchone()
                result.daily = dict(row) if row else {}
                result.activities = [dict(r) for r in conn.execute(
                    "SELECT activity_type, distance_meters, duration_seconds, "
                    "avg_pace_sec_per_km FROM activities WHERE date=?", (target,))]
            except sqlite3.Error:
                LOG.warning("Evening email activity data unavailable", exc_info=True)
            try:
                active = plans.get_active_plan(conn=conn)
                result.has_plan = active is not None
                result.tomorrow = sorted(
                    [w for w in active["workouts"] if w["date"] == next_day],
                    key=lambda w: w.get("seq", 1),
                ) if active else []
            except sqlite3.Error:
                LOG.warning("Evening email plan unavailable", exc_info=True)
            if not result.historical:
                result.synced_at = data_as_of_today(conn, target)
    except sqlite3.Error:
        LOG.warning("Evening email database unavailable", exc_info=True)
    return result


@dataclass(frozen=True)
class Digest:
    date: str
    headline: str
    activity_note: str
    stats: tuple[tuple[str, str], ...]
    insight: str
    insight_label: str
    critical: bool
    tomorrow_label: str
    tomorrow: tuple[str, ...]
    tomorrow_note: str
    provenance: str

    def lines(self) -> list[str]:
        """Single content definition for HTML, text and the word budget."""
        return ["LOCAL FITNESS", self.date, "Your daily TL;DR", self.headline,
                self.activity_note,
                *(f"{value} {label}" for value, label in self.stats),
                self.insight_label, self.insight, self.tomorrow_label,
                *self.tomorrow, self.tomorrow_note, self.provenance]


def _distance(meters: float, unit: str) -> str:
    value = meters / (units.METERS_PER_MILE if unit == "mi" else 1000)
    return f"{value:.1f} {unit}"


def _activity(inputs: EmailInputs) -> tuple[str, str]:
    rows = inputs.activities
    if rows is None:
        return "Activity unavailable.", ""
    if not rows:
        return "No workouts logged.", ""
    foot = [r for r in rows if plans._is_on_foot(r.get("activity_type"))]
    if len(foot) == len(rows) and all(r.get("distance_meters") is not None for r in foot):
        modes = {interpret.is_running_effort(r.get("avg_pace_sec_per_km")) for r in foot}
        label = "run" if modes == {True} else "walked" if modes == {False} else "on foot"
        total = sum(r["distance_meters"] for r in foot)
        headline = f"{_distance(total, inputs.distance_unit)} {label}."
        if modes == {True, False}:
            run = sum(r["distance_meters"] for r in foot
                      if interpret.is_running_effort(r.get("avg_pace_sec_per_km")) is True)
            return headline, (f"{_distance(run, inputs.distance_unit)} run · "
                              f"{_distance(total - run, inputs.distance_unit)} walked")
        return headline, "Logged workouts"
    count = len(rows)
    return f"{count} workout{'s' if count != 1 else ''} logged.", ""


def _outline(workout: dict, unit: str) -> str:
    # The database's type is a bounded enum; keep malformed legacy text out.
    w = dict(workout)
    if w.get("type") not in plans.WORKOUT_TYPES:
        w["type"] = ""
    parts = [calendar_render.workout_label(w)]
    if w.get("type") == "rest":
        return parts[0]
    if w.get("target_distance_m"):
        parts.append(_distance(w["target_distance_m"], unit))
    if w.get("target_duration_sec"):
        parts.append(units.format_hm(w["target_duration_sec"]))
    pace = w.get("target_pace_sec_per_km")
    if pace:
        if unit == "mi":
            parts.append(f"@ {units.format_pace_min_per_mi(pace)}/mi")
        else:
            minutes, seconds = divmod(round(pace), 60)
            parts.append(f"@ {minutes}:{seconds:02d}/km")
    if w.get("target_hr_max"):
        parts.append(f"HR ≤{w['target_hr_max']:g}")
    return " · ".join(parts)


def _stamp(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).strftime("%b %d %H:%M")
    except ValueError:
        return None


def compose(brief: Brief, inputs: EmailInputs) -> Digest:
    """Budget complete source text, never lop a condition off coaching advice."""
    day = date.fromisoformat(brief.date)
    headline, activity_note = _activity(inputs)
    rows = inputs.activities
    duration = (sum(r["duration_seconds"] for r in rows)
                if rows and all(r.get("duration_seconds") is not None for r in rows) else None)
    steps = inputs.daily.get("steps")
    stats = ((f"{steps:,}" if steps is not None else "—", "Steps so far"),
             (units.format_hm(duration) or "—", "Workout time"),
             (units.format_hm(inputs.daily.get("sleep_seconds")) or "—", "Last night's sleep"))
    selected = min(brief.takeaways, key=lambda t: {
        "critical": 0, "caution": 1, "positive": 2, "neutral": 3}[t.tone])
    # Render source prose only when it fits WHOLE and is plain, not markdown.
    # Escape at the HTML boundary as well. No raw HTML or script is interpreted.
    heading = selected.headline.rstrip()
    separator = " " if heading.endswith((".", "!", "?", ":")) else ". "
    prose = " ".join(f"{heading}{separator}{selected.summary}".split())
    safe = (len(prose) <= 180 and len(prose.split()) <= 26
            and not re.search(r"[<>|\[\]`*_#]", prose)
            and not any(len(word) > 30 for word in prose.split()))
    insight = prose if safe else BRIEF_CUE
    generated = _stamp(brief.generated_at)
    insight_label = f"Brief · {generated}" if generated else "Saved brief"
    if inputs.tomorrow is None:
        tomorrow, note = ("Plan unavailable.",), ""
    elif not inputs.has_plan:
        tomorrow, note = ("No active plan.",), ""
    elif not inputs.tomorrow:
        tomorrow, note = ("No session scheduled.",), ""
    else:
        tomorrow = tuple(_outline(w, inputs.distance_unit) for w in inputs.tomorrow[:2])
        if len(inputs.tomorrow) == 2:
            tomorrow = tuple(f"{index}. {line}" for index, line in enumerate(tomorrow, 1))
        if len(inputs.tomorrow) > 2:
            tomorrow = (f"{len(inputs.tomorrow)} sessions planned.",)
        note = FULL_SESSION_CUE
    synced = _stamp(inputs.synced_at)
    provenance = ("Archived day · current saved plan" if inputs.historical else
                  f"Synced {synced}" if synced else "Sync time unavailable")
    digest = Digest(day.strftime("%a, %b %d"), headline, activity_note, stats,
                    insight, insight_label, selected.tone == "critical",
                    f"Tomorrow · {(day + timedelta(days=1)).strftime('%b %d')}",
                    tomorrow, note, provenance)
    if len(" ".join(digest.lines()).split()) > MAX_WORDS:
        # Activity and tomorrow are the priorities. Drop optional commentary
        # before collapsing useful prescriptions; critical context keeps a cue.
        digest = replace(digest, insight=BRIEF_CUE if digest.critical else "",
                         insight_label=digest.insight_label if digest.critical else "")
    if len(" ".join(digest.lines()).split()) > MAX_WORDS:
        digest = replace(digest, tomorrow=(f"{len(inputs.tomorrow or [])} sessions planned.",),
                         tomorrow_note=FULL_SESSION_CUE)
    return digest
