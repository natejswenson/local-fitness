"""Display-unit formatting for runner-facing output.

The DB stores raw SI-ish values — distances in meters, pace as
seconds-per-km, durations in seconds. The runner (and the UI) thinks in
miles and min/mile. This module centralizes the conversion + formatting so
read tools can attach human-readable ``*_mi`` / formatted fields alongside
the raw numbers without each call site re-deriving the math (and re-getting
the rounding or the divide-by-zero guard subtly wrong).

Every function is a pure leaf: no DB access, no imports from other
``local_fitness`` modules, and a hard null/zero guard so a paceless or
zero-distance workout never raises.

Env:
    LOCAL_FITNESS_DISPLAY_UNITS — "miles" (default) or another unit label.
        Read via :func:`display_units`; lets read tools decide whether to
        emit the mile-based convenience fields at all. Defaults to "miles"
        so a fresh clone behaves the way the UI expects.
"""
from __future__ import annotations

import os

# 1 international mile = 1609.344 meters, exactly.
_METERS_PER_MILE = 1609.344
# sec/km → sec/mi: one mile is 1.609344 km, so a per-km pace covers that
# many km in one mile.
_KM_PER_MILE = 1.609344


def to_miles(meters: float | None) -> float | None:
    """Meters → miles, rounded to 2 decimals. ``None`` in, ``None`` out.

    0 meters yields 0.0 (a real, if empty, value); only ``None`` propagates.
    """
    if meters is None:
        return None
    return round(meters / _METERS_PER_MILE, 2)


def format_pace_min_per_mi(sec_per_km: float | None) -> str | None:
    """Seconds-per-km → ``"M:SS"`` min/mile string (e.g. ``"8:05"``).

    Returns ``None`` when ``sec_per_km`` is ``None`` or falsy (0). A
    zero-distance or paceless workout has no pace to show — omit the field
    rather than divide by zero or render a bogus ``0:00``.
    """
    if not sec_per_km:
        return None
    sec_per_mi = sec_per_km * _KM_PER_MILE
    minutes, seconds = divmod(round(sec_per_mi), 60)
    return f"{minutes}:{seconds:02d}"


def format_duration(seconds: float | int | None) -> str | None:
    """Seconds → ``"M:SS"`` under an hour, ``"H:MM:SS"`` at/over an hour.

    e.g. ``1860 → "31:00"``, ``3750 → "1:02:30"``. ``None`` in, ``None``
    out; 0 yields ``"0:00"``.
    """
    if seconds is None:
        return None
    total = round(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_hm(seconds: float | int | None) -> str | None:
    """Seconds → hours-and-minutes sleep shape, e.g. ``27180 → "7h 33m"``.

    NOT ``format_duration``'s ``H:MM:SS`` — that reads as a run duration.
    The repo's deliberate sleep convention is hours-and-minutes: minutes are
    zero-padded at/over an hour (``"7h 05m"``, not ``"7h 5m"``) and the
    sub-hour form drops the hour entirely (``"45m"``). Reproduces
    ``brief_planner._hm``'s output exactly. ``None`` in, ``None`` out.
    """
    if seconds is None:
        return None
    h, m = divmod(int(round(seconds)) // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def display_units() -> str:
    """The configured display-unit label, lowercased. Defaults to "miles"."""
    return os.environ.get("LOCAL_FITNESS_DISPLAY_UNITS", "miles").lower()
