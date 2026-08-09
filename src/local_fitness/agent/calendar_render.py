"""Turn a prescribed plan workout into a Google Calendar event body.

The PURE half of the calendar path — ``gcal.py`` is the I/O half, the same
divider ``email_render``/``mailer`` and ``plans``/its persistence section use.
Stdlib only, no DB, no network: every function here takes plain dicts and is
directly unit-testable.

Two contracts are load-bearing and both have tests pinning them:

* **The event is ALL-DAY, and an all-day ``end`` is EXCLUSIVE.** Google's API
  wants ``end.date`` = the day AFTER the last day covered, so a one-day event
  on the 9th ends on the 10th. Getting this wrong renders a zero-length event
  that some clients hide entirely — it fails by *disappearing*, not by erroring.
* **The event id is keyed on IDENTITY, never on content.** ``(plan_id, date,
  seq)`` and nothing else, so re-prescribing a day updates the event already on
  the calendar instead of leaving the old one beside a new one. A content hash
  would give you a second event every time the coach edits the session — which
  is exactly the shape of "helpful automation" that gets muted after a week.

The event is also ``transparent`` (free, not busy): an all-day block that marks
a whole day busy would break every meeting-scheduling heuristic anyone points
at the calendar, and this event is a reminder, not a commitment of the day.
"""
from __future__ import annotations

import hashlib
from datetime import date as Date
from datetime import timedelta

from . import units

#: Human labels for ``plan_workouts.type``. ``rest`` is deliberately absent —
#: it is the one type that produces no event at all, and leaving it out of the
#: map means a future type added to the schema fails the lookup loudly rather
#: than quietly inheriting rest's meaning.
TYPE_LABELS = {
    "easy": "Easy run",
    "long": "Long run",
    "tempo": "Tempo run",
    "interval": "Intervals",
    "race": "RACE",
    "cross": "Cross-training",
}

#: The type that means "do nothing today". A rest day creates no event, so an
#: entry from this job on your calendar always means go train.
REST_TYPE = "rest"

#: Prefix on every generated event id. Google requires ids to be base32hex
#: (``[a-v0-9]``, 5–1024 chars); every letter here is under 'v' and a sha256
#: hex digest is a subset of the same alphabet, so the whole id is valid by
#: construction rather than by sanitizing after the fact.
EVENT_ID_PREFIX = "lfplan"

#: How much of the digest to keep. 26 hex chars ≈ 104 bits — collision-proof
#: for a few hundred workouts, and short enough to eyeball in a URL.
_DIGEST_CHARS = 26

#: Marks an event as ours in ``extendedProperties.private``, so a future
#: cleanup/audit path can find exactly the events this job wrote and nothing
#: else on the calendar.
SOURCE_TAG = "local-fitness"


def event_id(plan_id: int, date: str, seq: int) -> str:
    """A stable Google Calendar event id for one prescribed session.

    Identity only — ``(plan_id, date, seq)``. Deriving it from the
    prescription's CONTENT instead would mean an edited session lands on a new
    id and the calendar accumulates one stale event per revision; keyed this
    way, ``gcal.upsert_event`` overwrites the same event in place.

    Abandoning a plan and starting another re-keys everything (``plan_id``
    changes), which is correct: those are different prescriptions for the same
    day and the old plan's events should not be silently rewritten.
    """
    raw = f"{plan_id}|{date}|{seq}".encode()
    return EVENT_ID_PREFIX + hashlib.sha256(raw).hexdigest()[:_DIGEST_CHARS]


def _distance_label(target_distance_m: float | None) -> str | None:
    """``6437.38`` → ``"4.0 mi"``. One decimal, always — a bare ``"4 mi"``
    beside ``"13.1 mi"`` reads as a different kind of number."""
    mi = units.to_miles(target_distance_m)
    if mi is None or mi <= 0:
        return None
    return f"{mi:.1f} mi"


def _duration_label(target_duration_sec: float | int | None) -> str | None:
    """``2700`` → ``"45 min"``. Whole minutes: a duration prescription is never
    meaningfully specified to the second, and ``"45:00"`` in a calendar title
    reads as a clock time."""
    if not target_duration_sec or target_duration_sec <= 0:
        return None
    return f"{round(target_duration_sec / 60)} min"


def build_summary(workout: dict) -> str:
    """The calendar title, e.g. ``"Easy run 4.0 mi @ 10:28/mi"``.

    Distance leads when there is one and duration stands in when there isn't —
    which matches how the plan prescribes: ``_DISTANCE_TYPES`` carry meters,
    ``_DURATION_TYPES`` (interval/tempo) carry seconds. An unknown type falls
    back to the raw value capitalized rather than dropping the session.
    """
    wtype = (workout.get("type") or "").strip().lower()
    label = TYPE_LABELS.get(wtype) or (wtype.capitalize() if wtype else "Workout")

    parts = [label]
    amount = (_distance_label(workout.get("target_distance_m"))
              or _duration_label(workout.get("target_duration_sec")))
    if amount:
        parts.append(amount)
    pace = units.format_pace_min_per_mi(workout.get("target_pace_sec_per_km"))
    if pace:
        parts.append(f"@ {pace}/mi")
    return " ".join(parts)


def build_description(workout: dict, plan_id: int) -> str:
    """The event body: the coach's prose, then the numbers it was graded on.

    Plain text, not HTML. Google renders a description's HTML in the web UI but
    strips most of it on mobile and in notification emails, so the one format
    that looks the same everywhere is the one to use.

    The trailing provenance line is what tells you a year from now why an event
    you don't remember creating is on your calendar.
    """
    lines: list[str] = []
    prose = (workout.get("description") or "").strip()
    if prose:
        lines.append(prose)

    facts: list[str] = []
    distance = _distance_label(workout.get("target_distance_m"))
    pace = units.format_pace_min_per_mi(workout.get("target_pace_sec_per_km"))
    if distance and pace:
        facts.append(f"Target: {distance} @ {pace}/mi")
    elif distance:
        facts.append(f"Target: {distance}")
    elif pace:
        facts.append(f"Target pace: {pace}/mi")

    duration = _duration_label(workout.get("target_duration_sec"))
    if duration:
        facts.append(f"Duration: {duration}")

    hr_max = workout.get("target_hr_max")
    if hr_max:
        # The one prescription the report card grades against a hard ceiling
        # (report_card's cap axis), so it belongs where you'll read it before
        # the run rather than only in the grade afterwards.
        facts.append(f"HR cap: {round(hr_max)} bpm")

    if facts:
        lines.append("\n".join(facts))
    lines.append(f"{SOURCE_TAG} · plan #{plan_id}")
    return "\n\n".join(lines)


def build_event(workout: dict | None, plan_id: int) -> dict | None:
    """One plan workout → a Google Calendar event resource, or ``None``.

    ``None`` means "nothing belongs on the calendar for this row": a rest day,
    or no row at all. Callers treat that as a clean no-op, never an error —
    most weeks have two rest days and a job that reported failure on those
    would cry wolf twice a week.
    """
    if not workout:
        return None
    wtype = (workout.get("type") or "").strip().lower()
    if wtype == REST_TYPE:
        return None
    day = workout.get("date")
    if not day:
        return None

    seq = int(workout.get("seq") or 1)
    # An all-day end is EXCLUSIVE — the day AFTER the last covered day. See the
    # module docstring: this one fails silently rather than loudly.
    end = (Date.fromisoformat(day) + timedelta(days=1)).isoformat()

    return {
        "id": event_id(plan_id, day, seq),
        "summary": build_summary(workout),
        "description": build_description(workout, plan_id),
        "start": {"date": day},
        "end": {"date": end},
        # Free, not busy. An all-day event that blocks the day would make every
        # scheduling tool think you're out.
        "transparency": "transparent",
        # Values must be strings — the API rejects non-string extended
        # properties outright.
        "extendedProperties": {
            "private": {
                "source": SOURCE_TAG,
                "kind": "plan-workout",
                "planId": str(plan_id),
                "planDate": day,
                "seq": str(seq),
            }
        },
    }


def build_events(workouts: list[dict], target_date: str, plan_id: int) -> list[dict]:
    """Every event for one date, in ``seq`` order (AM/PM doubles → two events).

    Supporting the double-day is free because ``event_id`` already keys on
    ``seq``; skipping it would silently drop the second session of the day, and
    a plan that prescribes one is exactly the plan you most want on a calendar.
    """
    rows = [w for w in workouts if w.get("date") == target_date]
    rows.sort(key=lambda w: int(w.get("seq") or 1))
    return [e for e in (build_event(w, plan_id) for w in rows) if e is not None]
