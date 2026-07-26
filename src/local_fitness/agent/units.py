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

# 1 international mile = 1609.344 meters, exactly. Public since 0.37.0 —
# these two are THE constants; redeclarations elsewhere are being retired
# (report_card.MILE_M, interpret._KM_PER_MILE point back here or are pinned
# equal by test).
METERS_PER_MILE = 1609.344
# sec/km → sec/mi: one mile is 1.609344 km, so a per-km pace covers that
# many km in one mile.
KM_PER_MILE = 1.609344
# Backward-compat aliases for the pre-0.37.0 private names.
_METERS_PER_MILE = METERS_PER_MILE
_KM_PER_MILE = KM_PER_MILE


def from_miles(miles: float | None) -> float | None:
    """Miles → meters — :func:`to_miles`'s inverse, same null contract.

    NOT rounded: the result is a stored quantity (plan targets, filters),
    not a display value, and rounding meters would move a 6.0 mi
    prescription by feet for nothing."""
    if miles is None:
        return None
    return float(miles) * METERS_PER_MILE


def parse_pace_min_per_mi(value: str | float | int | None) -> float | None:
    """A user/model-supplied per-mile pace → seconds per MILE, or ``None``.

    Accepts the two shapes an agent plausibly sends:

    * ``"M:SS"`` (e.g. ``"9:39"`` → 579) — the app's own display format,
      round-tripping :func:`format_pace_min_per_mi`'s output. Seconds must
      be two digits under 60.
    * a bare number — DECIMAL minutes (``9.65`` → 579). This is the trap
      the string form exists to close: ``9.39`` is 9:23, NOT 9:39, and a
      model copying a display string as a float silently re-prescribes a
      16 s/mi faster pace. Callers should say so in their schema docs.

    Anything else (malformed string, non-positive, ``"9:75"``) → ``None`` —
    the caller decides whether that's an error or an omitted field."""
    if value is None:
        return None
    if isinstance(value, str):
        import re

        m = re.fullmatch(r"\s*(\d{1,2}):([0-5]\d)\s*", value)
        if not m:
            return None
        total = int(m.group(1)) * 60 + int(m.group(2))
        return float(total) if total > 0 else None
    try:
        minutes = float(value)
    except (TypeError, ValueError):
        return None
    if minutes <= 0:
        return None
    return minutes * 60.0


def pace_sec_per_mi_to_sec_per_km(sec_per_mi: float | None) -> float | None:
    """Seconds-per-mile → seconds-per-km (the stored unit). Null-safe."""
    if not sec_per_mi:
        return None
    return sec_per_mi / KM_PER_MILE


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
