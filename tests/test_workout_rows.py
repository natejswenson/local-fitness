"""workout_rows — the one definition of a workout row as a tool payload.

The module exists to kill two measured problems at once: status.py's inline
byte-copy of tools._augment_workout (two definitions of one field set), and
the ~25% of every list payload spent shipping raw/formatted pairs
(distance_meters beside distance_mi, etc. — 2026-08-10 audit). These tests
pin the compact shape's rules, because each one is a contract a consumer
(mcp_server._render_status, the model's own field expectations) relies on.
"""
from __future__ import annotations

import pytest

from local_fitness.agent import workout_rows


def _row(**overrides) -> dict:
    base = {
        "activity_id": 42,
        "date": "2026-08-01",
        "activity_type": "running",
        "activity_name": "Morning Run",
        "duration_seconds": 3600,
        "distance_meters": 8046.7,  # ~5 mi
        "avg_hr": 140,
        "max_hr": 160,
        "avg_pace_sec_per_km": 447.0,  # ~12:00/mi -> running effort
        "elevation_gain_meters": 30.0,
        "aerobic_te": 3.0,
        "anaerobic_te": 0.5,
        "training_load": 55.0,
    }
    base.update(overrides)
    return base


def test_display_row_drops_raw_twins_in_miles_mode(monkeypatch):
    monkeypatch.delenv("LOCAL_FITNESS_DISPLAY_UNITS", raising=False)
    w = workout_rows.display_workout(_row())
    # Display forms present…
    assert w["distance_mi"] == pytest.approx(5.0, abs=0.01)
    assert w["pace_min_per_mi"]
    assert w["duration_formatted"]
    # …and the raw columns they replace are gone.
    assert "distance_meters" not in w
    assert "avg_pace_sec_per_km" not in w
    assert "duration_seconds" not in w
    # Identity/handle fields always survive — activity_id is what
    # get_workout_detail / workout_report_card take.
    assert w["activity_id"] == 42 and w["date"] == "2026-08-01"


def test_display_row_keeps_raw_trio_in_km_mode(monkeypatch):
    """In km mode the raw trio IS the display form — dropping it would leave
    a row with no distance at all, since distance_mi is miles-gated."""
    monkeypatch.setenv("LOCAL_FITNESS_DISPLAY_UNITS", "km")
    w = workout_rows.display_workout(_row())
    assert "distance_mi" not in w
    assert w["distance_meters"] == 8046.7
    # pace/duration formatted twins are unit-independent and still attach,
    # but the raw columns stay because... no: the pair rule only fires in
    # miles mode, so raw pace/duration survive alongside their twins here.
    assert w["avg_pace_sec_per_km"] == 447.0
    assert w["duration_seconds"] == 3600


def test_paceless_row_has_no_pace_key_at_all(monkeypatch):
    """A paceless row gets no pace_min_per_mi twin (nothing to format) and
    its None raw value is omitted like every other None — the pace is absent
    because it was never measured, and effort: null (below) is the signal
    that says so."""
    monkeypatch.delenv("LOCAL_FITNESS_DISPLAY_UNITS", raising=False)
    w = workout_rows.display_workout(_row(avg_pace_sec_per_km=None))
    assert "pace_min_per_mi" not in w
    assert "avg_pace_sec_per_km" not in w


def test_effort_null_is_kept_for_paceless_rows(monkeypatch):
    """The run-vs-walk rule: a paceless row has an UNKNOWN mode. The key must
    survive as null — omitting it would make unknown indistinguishable from
    unqueried."""
    monkeypatch.delenv("LOCAL_FITNESS_DISPLAY_UNITS", raising=False)
    w = workout_rows.display_workout(_row(avg_pace_sec_per_km=None))
    assert "effort" in w
    assert w["effort"] is None


def test_effort_classifies_by_measured_pace_not_label(monkeypatch):
    monkeypatch.delenv("LOCAL_FITNESS_DISPLAY_UNITS", raising=False)
    run = workout_rows.display_workout(_row(avg_pace_sec_per_km=350.0))
    walk = workout_rows.display_workout(
        _row(activity_type="treadmill_running", avg_pace_sec_per_km=560.0)
    )
    assert run["effort"] == "run"
    # Garmin's label says running; the measured pace says walk. Pace wins.
    assert walk["effort"] == "walk"
    assert walk["activity_type"] == "treadmill_running"  # label kept, never rewritten


def test_none_valued_optionals_are_omitted(monkeypatch):
    """A treadmill row's elevation_gain_meters: null said nothing 50 times
    over — measured 13 of 15 stored cards had an entirely empty Elev column."""
    monkeypatch.delenv("LOCAL_FITNESS_DISPLAY_UNITS", raising=False)
    w = workout_rows.display_workout(_row(
        activity_name=None, avg_hr=None, max_hr=None,
        elevation_gain_meters=None, aerobic_te=None, anaerobic_te=None,
        training_load=None,
    ))
    for key in ("activity_name", "avg_hr", "max_hr", "elevation_gain_meters",
                "aerobic_te", "anaerobic_te", "training_load"):
        assert key not in w


def test_zero_distance_converts_to_zero_miles_missing_stays_missing(monkeypatch):
    """to_miles(0) is 0.0 — a real (if empty) value, so the twin lands and
    the raw column drops: the quantity survives as 0.0 mi. Only a MISSING
    distance (None) has no twin, and its None is omitted with the rest."""
    monkeypatch.delenv("LOCAL_FITNESS_DISPLAY_UNITS", raising=False)
    zero = workout_rows.display_workout(_row(distance_meters=0))
    assert zero["distance_mi"] == 0.0
    assert "distance_meters" not in zero
    missing = workout_rows.display_workout(_row(distance_meters=None))
    assert "distance_mi" not in missing
    assert "distance_meters" not in missing


def test_augment_workout_keeps_raw_and_display_side_by_side(monkeypatch):
    """The DETAIL shape (get_workout_detail, write echoes) is additive only."""
    monkeypatch.delenv("LOCAL_FITNESS_DISPLAY_UNITS", raising=False)
    w = workout_rows.augment_workout(_row())
    assert w["distance_meters"] == 8046.7 and "distance_mi" in w
    assert w["avg_pace_sec_per_km"] == 447.0 and "pace_min_per_mi" in w
    assert w["duration_seconds"] == 3600 and "duration_formatted" in w


def test_display_never_disagrees_with_augment(monkeypatch):
    """display_workout is augment_workout minus keys — every surviving value
    must be byte-identical to the augmented one, so the two shapes can never
    drift on what a field means."""
    monkeypatch.delenv("LOCAL_FITNESS_DISPLAY_UNITS", raising=False)
    augmented = workout_rows.augment_workout(_row())
    compact = workout_rows.display_workout(_row())
    for key, value in compact.items():
        assert augmented[key] == value
