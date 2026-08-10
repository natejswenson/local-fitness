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

``reconcile`` at the bottom is the third contract, added in 0.53.0 when the
calendar went from carrying tomorrow to carrying the whole remaining plan: the
diff has a DELETE side. An upsert can only add or correct, so it cannot express
"this day is no longer a session" — see that function's docstring.
"""
from __future__ import annotations

import hashlib
from datetime import date as Date
from datetime import timedelta

from . import units
from .interpret import is_running_effort

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

#: What the same types are called when the PRESCRIBED pace says walk.
#: ``plan_workouts.type`` has no walk value — CLAUDE.md prescribes walks as
#: ``easy`` deliberately, because that is what makes them gradeable — so the
#: type alone cannot tell you what the session is. Titling a 3.5 mi recovery
#: walk "Easy run" contradicts the pace printed beside it and the description
#: below it, which is the "never print two quantities in one column" rule
#: showing up in words instead of numbers.
WALK_TYPE_LABELS = {"easy": "Walk", "long": "Long walk"}

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

#: No reminders, stated EXPLICITLY. Omitting the key is not the same thing —
#: the calendar's own default applies, and on a personal calendar that default
#: is typically a 30-minute popup. An all-day event starts at MIDNIGHT, so "30
#: minutes before" fires at 23:30 the night before: 41 consecutive nights of
#: late-evening notifications, which is how the whole calendar gets muted.
#:
#: **A reminder on the day of an all-day event is not expressible, and Google
#: fails at this SILENTLY.** ``minutes`` is strictly *before* the start, and a
#: negative value is accepted with HTTP 200 and then clamped to 0 — measured
#: 2026-08-09: ``minutes: -480`` (08:00 day-of) stored as ``minutes: 0``
#: (midnight). Nothing errors; you simply get a different reminder than the one
#: you asked for. If a morning-of nudge is ever wanted, the events have to stop
#: being all-day and become timed blocks — there is no third option, and no
#: amount of arithmetic on this constant will produce one.
NO_REMINDERS = {"useDefault": False, "overrides": []}


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


def workout_label(workout: dict) -> str:
    """What to call this session — decided by prescribed PACE, not by type.

    ``plan_workouts.type`` cannot answer this. A prescribed walk is stored as
    ``easy`` on purpose (that is what makes it gradeable — see CLAUDE.md), so
    the type says "run" for a session the plan intends as a walk. The pace does
    know, and ``interpret.is_running_effort`` is the repo's single definition
    of that boundary — the same one the report card grades against, so a
    session the card treats as a walk can never be titled a run.

    A paceless prescription keeps its type label: mode is genuinely unknown
    there, and guessing "walk" would mislabel every by-feel easy day.
    """
    wtype = (workout.get("type") or "").strip().lower()
    running = is_running_effort(workout.get("target_pace_sec_per_km"))
    if running is False:
        return WALK_TYPE_LABELS.get(wtype, "Walk")
    return TYPE_LABELS.get(wtype) or (wtype.capitalize() if wtype else "Workout")


def build_summary(workout: dict) -> str:
    """The calendar title, e.g. ``"Easy run 4.0 mi @ 10:28/mi"``.

    Distance leads when there is one and duration stands in when there isn't —
    which matches how the plan prescribes: ``_DISTANCE_TYPES`` carry meters,
    ``_DURATION_TYPES`` (interval/tempo) carry seconds. An unknown type falls
    back to the raw value capitalized rather than dropping the session.
    """
    parts = [workout_label(workout)]
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
        # Silent by default — see NO_REMINDERS. The calendar is a reference you
        # look at, not something that interrupts you at 23:30 every night.
        "reminders": dict(NO_REMINDERS),
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


#: Blast-radius bound on one sync. Equal to ``plans.MAX_WORKOUTS`` — a plan
#: cannot hold more prescriptions than this, so hitting it means something is
#: wrong with the inputs rather than that a plan is long. Over the cap
#: ``build_plan_events`` REFUSES; it never truncates, because a silently
#: half-written calendar is worse than one that didn't get written (the
#: ``warm_report_cards --max-calls`` precedent).
MAX_SYNC_EVENTS = 200


class TooManyEvents(ValueError):
    """A sync would exceed ``MAX_SYNC_EVENTS``."""


def build_plan_events(workouts: list[dict], plan_id: int, start: str) -> list[dict]:
    """Every event a plan should have on the calendar from ``start`` onward.

    Ordered by ``(date, seq)``, rest days dropped, one event per remaining
    prescription — including both halves of an AM/PM double day, which is free
    because ``event_id`` already keys on ``seq``.

    ``start`` bounds the FUTURE only. Days already elapsed are deliberately not
    rebuilt: their events are a record of what was prescribed at the time, and
    reconciling them would either recreate deleted history or rewrite it to
    match a plan that has since been edited.
    """
    rows = [w for w in workouts if (w.get("date") or "") >= start]
    rows.sort(key=lambda w: ((w.get("date") or ""), int(w.get("seq") or 1)))
    events = [e for e in (build_event(w, plan_id) for w in rows) if e is not None]
    if len(events) > MAX_SYNC_EVENTS:
        raise TooManyEvents(
            f"{len(events)} events would be written, over the "
            f"{MAX_SYNC_EVENTS} cap — refusing rather than writing part of a "
            "plan to the calendar"
        )
    return events


#: The fields that decide whether a stored event still says what the plan says.
#: Ids and extended properties are identity — they cannot differ, because the id
#: is how the event was matched in the first place.
COMPARED_FIELDS = ("summary", "description", "transparency")

#: Google's word for a deleted event. It is a TOMBSTONE, not an absence: the id
#: stays claimed, a re-insert 409s, and a blind update resurrects it.
CANCELLED = "cancelled"


def _event_date(event: dict) -> str:
    """The start date of an all-day event, tolerating the timestamp form.

    ``events.list`` echoes ``start.date`` for all-day events, but the API has
    been seen to return ``2026-08-09T00:00:00Z`` on some paths; slicing to 10
    chars normalizes both without a parse that could raise mid-reconcile.
    """
    return str((event.get("start") or {}).get("date") or "")[:10]


def _reminder_key(event: dict):
    """Reminders reduced to something two events can be compared on.

    Normalizing is not tidiness, it is the difference between a quiet sync and
    one that rewrites all 41 events every single run. Google does not echo back
    what you send: ``{"useDefault": false, "overrides": []}`` comes back as
    ``{"useDefault": false}`` with the empty list dropped (measured
    2026-08-09), so a raw dict comparison reports a difference forever and the
    reconcile never converges. Overrides become a SET for the same reason —
    their order is the server's to choose, not ours.
    """
    reminders = event.get("reminders")
    if reminders is None:
        # No key at all is UNKNOWN, not silence. Reading it as "no reminders"
        # would make an event that is actually inheriting the calendar's 23:30
        # popup compare equal to our explicit silence and never get repaired.
        # Returning a value nothing else can equal costs at most one redundant
        # write; the other reading costs a nightly notification forever.
        return None
    overrides = frozenset(
        (o.get("method"), o.get("minutes"))
        for o in (reminders.get("overrides") or [])
    )
    return bool(reminders.get("useDefault")), overrides


def _differs(existing: dict, desired: dict) -> bool:
    if any(existing.get(f) != desired.get(f) for f in COMPARED_FIELDS):
        return True
    if _reminder_key(existing) != _reminder_key(desired):
        return True
    return any(
        str((existing.get(side) or {}).get("date") or "")[:10] != desired[side]["date"]
        for side in ("start", "end")
    )


def reconcile(desired: list[dict], existing: list[dict], start: str) -> dict:
    """Diff what the plan says against what is on the calendar.

    Returns ``{"create": [...], "update": [...], "delete": [...],
    "unchanged": [...], "skipped_cancelled": [...]}`` — events for the first
    two, whole existing-event dicts for ``delete``, ids for the last two.

    | desired | on calendar | outcome |
    |---------|-------------|---------|
    | yes     | absent      | create  |
    | yes     | differs     | update  |
    | yes     | identical   | unchanged — no request at all |
    | yes     | cancelled   | skipped_cancelled |
    | **no**  | present     | **delete** |

    **That last row is why this is a reconcile and not an upsert loop.** An
    upsert can only ever add or correct; it has no way to express "this day is
    no longer a session", so a run turned into a rest day, or a plan abandoned
    outright, would leave the calendar confidently prescribing work that the
    plan no longer asks for. Deletion is the whole reason this function exists.

    Two rails on that deletion, both tested:

    * ``existing`` is filtered to ``date >= start`` first, so **the past is
      never deleted**. Yesterday's event describes what was prescribed
      yesterday; the plan may have changed since, and rewriting history to
      match is not a sync, it is amnesia.
    * A ``cancelled`` event is neither resurrected nor re-deleted. Deleting the
      event is how a person says "not this one" — a sync that put it back would
      be uninstalled within the week, and a sync that "deleted" it again would
      burn a request per run forever.

    Callers must pass only events they own (``gcal.list_plan_events`` scopes by
    ``planId``); nothing here inspects provenance, so an unscoped ``existing``
    would put someone's dentist appointment in ``delete``.
    """
    by_id = {e["id"]: e for e in desired}
    create, update, unchanged, delete, skipped = [], [], [], [], []

    seen: set[str] = set()
    for row in existing:
        eid = row.get("id")
        if eid is None or _event_date(row) < start:
            # Past events are the record of what was prescribed. Untouched.
            continue
        seen.add(eid)
        want = by_id.get(eid)
        if row.get("status") == CANCELLED:
            # Only a tombstone the plan still WANTS is worth reporting; one for
            # a day the plan dropped is doubly gone and not news.
            if want is not None:
                skipped.append(eid)
            continue
        if want is None:
            delete.append(row)
        elif _differs(row, want):
            update.append(want)
        else:
            unchanged.append(eid)

    create = [e for e in desired if e["id"] not in seen]
    return {"create": create, "update": update, "delete": delete,
            "unchanged": unchanged, "skipped_cancelled": skipped}
