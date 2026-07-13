"""Tests for agent/units.py — the pure display-unit formatters.

Every function is a pure leaf (no DB, no imports), so these are plain
value-in/value-out assertions. The point of the file is to nail the
null/zero guards and the two branch points the rest of the suite skips:
a real (non-None) ``sec_per_km`` pace and the under-/over-an-hour split
in ``format_duration``.
"""
from __future__ import annotations

import pytest

from local_fitness.agent import units


# --- to_miles --------------------------------------------------------------

def test_to_miles_none_in_none_out():
    assert units.to_miles(None) is None


def test_to_miles_zero_is_real_zero():
    # 0 meters is a real (if empty) value — only None propagates.
    assert units.to_miles(0) == 0.0


def test_to_miles_converts_and_rounds():
    # 1609.344 m == exactly 1 mile; 10000 m → 6.21 mi.
    assert units.to_miles(1609.344) == 1.0
    assert units.to_miles(10000) == pytest.approx(6.21, abs=0.01)


# --- format_pace_min_per_mi -----------------------------------------------

def test_format_pace_none_or_zero_returns_none():
    # None or falsy (0) → no pace to show; omit rather than render 0:00.
    assert units.format_pace_min_per_mi(None) is None
    assert units.format_pace_min_per_mi(0) is None


def test_format_pace_non_none_sec_per_km():
    # 300 sec/km → 300 * 1.609344 = 482.8 sec/mi → round 483 → 8:03.
    assert units.format_pace_min_per_mi(300) == "8:03"


def test_format_pace_pads_seconds_to_two_digits():
    # 250 sec/km → 250 * 1.609344 = 402.3 → round 402 → 6:42.
    assert units.format_pace_min_per_mi(250) == "6:42"


# --- format_duration -------------------------------------------------------

def test_format_duration_none_in_none_out():
    assert units.format_duration(None) is None


def test_format_duration_zero_renders_zero():
    assert units.format_duration(0) == "0:00"


def test_format_duration_under_an_hour():
    assert units.format_duration(1860) == "31:00"


def test_format_duration_at_or_over_an_hour():
    # 3750 s → 1:02:30 (hours branch, zero-padded minutes + seconds).
    assert units.format_duration(3750) == "1:02:30"


# --- format_hm (3c) ---------------------------------------------------------

def test_format_hm_none_in_none_out():
    assert units.format_hm(None) is None


def test_format_hm_hours_and_minutes():
    # 27180 s = 7h 33m.
    assert units.format_hm(27180) == "7h 33m"


def test_format_hm_zero_pads_minutes():
    # 25500 s = 7h 05m — zero-padded, NOT "7h 5m".
    assert units.format_hm(25500) == "7h 05m"


def test_format_hm_sub_hour_drops_hour():
    # 2700 s = 45m, no "0h" prefix.
    assert units.format_hm(2700) == "45m"


def test_format_hm_rounds_and_accepts_float_seconds():
    # brief_planner._hm's callers feed float seconds (60-day means); the
    # function int(round())s internally before the divmod. 27239.6 rounds
    # to 27240s == exactly 7h 34m.
    assert units.format_hm(27239.6) == "7h 34m"


# --- display_units ---------------------------------------------------------

def test_display_units_defaults_to_miles(monkeypatch):
    monkeypatch.delenv("LOCAL_FITNESS_DISPLAY_UNITS", raising=False)
    assert units.display_units() == "miles"


def test_display_units_honors_env_and_lowercases(monkeypatch):
    monkeypatch.setenv("LOCAL_FITNESS_DISPLAY_UNITS", "KM")
    assert units.display_units() == "km"
