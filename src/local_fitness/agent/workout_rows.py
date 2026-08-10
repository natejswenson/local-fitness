"""The one definition of a workout row as a tool payload.

Pure (stdlib + units + interpret only — importable from both ``tools`` and
``status`` without a cycle, which is why this module exists: ``status`` used
to carry a byte-for-byte inline copy of ``tools._augment_workout`` to dodge a
status <-> tools import loop, and two copies of one field set is exactly the
drift the payload contract can't afford).

Two shapes, one rule each:

- ``augment_workout`` — convenience fields ALONGSIDE the raw columns. For
  detail surfaces (``get_workout_detail``, the manual-workout and plan-write
  echoes) where the caller asked for one thing in full.
- ``display_workout`` — the compact LIST row. Raw/formatted pairs were ~25%
  of every ``query_workouts``/``daily_snapshot`` payload (measured 2026-08-10:
  ``distance_meters``+``distance_mi``, ``avg_pace_sec_per_km``+
  ``pace_min_per_mi``, ``duration_seconds``+``duration_formatted`` = 2,229 of
  12,248 chars over 28 rows), so list surfaces keep only the display form in
  miles mode. In km mode the raw trio IS the display form and stays — dropping
  it there would leave a row with no distance at all, since ``distance_mi``
  is miles-gated. None-valued optional fields are omitted entirely (a
  treadmill row's ``elevation_gain_meters: null`` says nothing 50 times over).

``effort`` is the MEASURED run-vs-walk read (``interpret.is_running_effort``,
pace only) — Garmin's ``activity_type`` label lies (walking-desk sessions log
as ``treadmill_running``), so this field is additive, never a replacement for
the label, and never used to filter or exclude anything here. It is kept even
when ``None``: a paceless row has an UNKNOWN mode, and omitting the key would
make unknown indistinguishable from unqueried.
"""
from __future__ import annotations

from . import interpret, units

#: The raw columns whose display twins replace them in a compact row.
_RAW_DISPLAY_PAIRS = (
    ("distance_meters", "distance_mi"),
    ("avg_pace_sec_per_km", "pace_min_per_mi"),
    ("duration_seconds", "duration_formatted"),
)

#: The one key a compact row keeps even when None — a paceless row has an
#: UNKNOWN run-vs-walk mode, and omitting the key would make unknown
#: indistinguishable from unqueried. Every other None (no-HR strap, treadmill
#: elevation, unmeasured pace) is omitted: a null field 50 rows over says
#: nothing the absence doesn't.
_KEEP_WHEN_NONE = frozenset({"effort"})


def augment_workout(w: dict) -> dict:
    """Attach mile / formatted convenience fields ALONGSIDE the raw columns.

    A convenience field is only added when units.py returns non-None (null /
    zero → omitted). ``distance_mi`` is suppressed entirely when display units
    aren't miles.
    """
    if units.display_units() == "miles":
        distance_mi = units.to_miles(w.get("distance_meters"))
        if distance_mi is not None:
            w["distance_mi"] = distance_mi
    pace = units.format_pace_min_per_mi(w.get("avg_pace_sec_per_km"))
    if pace is not None:
        w["pace_min_per_mi"] = pace
    duration = units.format_duration(w.get("duration_seconds"))
    if duration is not None:
        w["duration_formatted"] = duration
    mode = interpret.is_running_effort(w.get("avg_pace_sec_per_km"))
    w["effort"] = {True: "run", False: "walk", None: None}[mode]
    return w


def display_workout(w: dict) -> dict:
    """The compact list row: display fields only, None values omitted.

    Starts from ``augment_workout`` so the two shapes can never disagree on
    what a field means — this one only *removes*. A raw field is dropped only
    when its display twin actually landed on the row (miles mode AND a usable
    value); a zero-distance row keeps its raw column rather than losing the
    quantity entirely. None values are omitted (except ``effort`` — see
    ``_KEEP_WHEN_NONE``), so a paceless row simply has no pace key at all.
    """
    w = augment_workout(w)
    if units.display_units() == "miles":
        for raw, display in _RAW_DISPLAY_PAIRS:
            if display in w:
                w.pop(raw, None)
    return {
        k: v for k, v in w.items()
        if v is not None or k in _KEEP_WHEN_NONE
    }
