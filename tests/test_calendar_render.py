"""The pure calendar renderer.

Two properties carry the whole feature and each has its own section below: the
all-day END is EXCLUSIVE (get it wrong and the event silently doesn't render),
and the event id is derived from IDENTITY, not content (get it wrong and every
prescription edit leaves a stale duplicate on the calendar).
"""
from __future__ import annotations

import re

from local_fitness.agent import calendar_render as cr

EASY = {
    "date": "2026-08-09",
    "seq": 1,
    "type": "easy",
    "target_distance_m": 6437.38,      # 4.00 mi
    "target_pace_sec_per_km": 390.2224,  # 10:28/mi
    "target_duration_sec": None,
    "target_hr_max": 140.0,
    "description": "Recovery 4mi. Keep HR under 140.",
}

INTERVALS = {
    "date": "2026-08-09",
    "seq": 1,
    "type": "interval",
    "target_distance_m": None,
    "target_pace_sec_per_km": None,
    "target_duration_sec": 2700,
    "target_hr_max": None,
    "description": "6x800m at 10K effort, 90s jog recovery.",
}

REST = {
    "date": "2026-08-09",
    "seq": 1,
    "type": "rest",
    "target_distance_m": None,
    "target_pace_sec_per_km": None,
    "target_duration_sec": None,
    "target_hr_max": None,
    "description": "Rest day",
}


# --- the all-day window ----------------------------------------------------

def test_an_all_day_event_ends_on_the_following_day():
    # Google treats an all-day `end.date` as EXCLUSIVE. Same-day start and end
    # is a zero-length event that several clients hide entirely — this fails by
    # disappearing, not by erroring, which is why it gets its own test.
    event = cr.build_event(EASY, 4)
    assert event["start"] == {"date": "2026-08-09"}
    assert event["end"] == {"date": "2026-08-10"}


def test_the_all_day_window_crosses_a_month_boundary():
    event = cr.build_event({**EASY, "date": "2026-08-31"}, 4)
    assert event["start"]["date"] == "2026-08-31"
    assert event["end"]["date"] == "2026-09-01"


def test_the_event_is_marked_free_not_busy():
    # An all-day block that marks the whole day busy breaks every scheduling
    # tool pointed at the calendar.
    assert cr.build_event(EASY, 4)["transparency"] == "transparent"


# --- the event id ----------------------------------------------------------

def test_the_event_id_does_not_move_when_the_prescription_changes():
    # THE property the upsert depends on. A content-derived id would put a
    # second event on the calendar every time the coach re-prescribes a day.
    base = cr.build_event(EASY, 4)["id"]
    for field, value in (
        ("target_distance_m", 16093.4),
        ("target_pace_sec_per_km", 300.0),
        ("target_hr_max", 155.0),
        ("type", "long"),
        ("description", "Something else entirely."),
    ):
        assert cr.build_event({**EASY, field: value}, 4)["id"] == base, field


def test_the_event_id_moves_with_every_identity_field():
    base = cr.event_id(4, "2026-08-09", 1)
    assert cr.event_id(5, "2026-08-09", 1) != base       # different plan
    assert cr.event_id(4, "2026-08-10", 1) != base       # different day
    assert cr.event_id(4, "2026-08-09", 2) != base       # PM of a double day


def test_the_event_id_is_a_legal_google_calendar_id():
    # Google requires base32hex: [a-v0-9], 5-1024 chars. A sha256 hex digest
    # plus this prefix satisfies it by construction — this test is what keeps a
    # future prefix change (or a switch to b64) from producing 400s at 19:05.
    for plan_id, day, seq in ((4, "2026-08-09", 1), (99999, "2020-01-01", 12)):
        got = cr.event_id(plan_id, day, seq)
        assert re.fullmatch(r"[a-v0-9]{5,1024}", got), got


def test_the_event_id_is_stable_across_calls():
    assert cr.event_id(4, "2026-08-09", 1) == cr.event_id(4, "2026-08-09", 1)


# --- the title -------------------------------------------------------------

def test_a_distance_prescription_titles_with_distance_and_pace():
    assert cr.build_summary(EASY) == "Easy run 4.0 mi @ 10:28/mi"


def test_a_duration_prescription_titles_with_minutes():
    # interval/tempo days carry seconds, not meters — the title has to fall
    # back to duration or it would read "Intervals" and nothing else.
    assert cr.build_summary(INTERVALS) == "Intervals 45 min"


def test_distance_wins_over_duration_when_a_workout_carries_both():
    both = {**EASY, "target_duration_sec": 2700}
    assert cr.build_summary(both) == "Easy run 4.0 mi @ 10:28/mi"


def test_a_prescription_with_no_targets_still_titles():
    bare = {**EASY, "target_distance_m": None, "target_pace_sec_per_km": None}
    assert cr.build_summary(bare) == "Easy run"


def test_distance_always_carries_one_decimal():
    # "4 mi" beside "13.1 mi" reads as a different kind of number.
    assert "5.0 mi" in cr.build_summary({**EASY, "target_distance_m": 8046.72})
    assert "13.1 mi" in cr.build_summary({**EASY, "target_distance_m": 21082.4})


def test_every_workout_type_has_a_readable_title():
    for wtype, expected in (
        ("easy", "Easy run"), ("long", "Long run"), ("tempo", "Tempo run"),
        ("interval", "Intervals"), ("race", "RACE"), ("cross", "Cross-training"),
    ):
        bare = {**EASY, "type": wtype, "target_distance_m": None,
                "target_pace_sec_per_km": None, "target_duration_sec": None}
        assert cr.build_summary(bare) == expected


def test_an_unknown_type_falls_back_rather_than_dropping_the_session():
    bare = {**EASY, "type": "swim", "target_distance_m": None,
            "target_pace_sec_per_km": None}
    assert cr.build_summary(bare) == "Swim"


def test_a_zero_distance_is_not_rendered_as_a_target():
    assert cr.build_summary({**EASY, "target_distance_m": 0}) == "Easy run @ 10:28/mi"


# --- the description -------------------------------------------------------

def test_the_description_carries_prose_targets_hr_cap_and_provenance():
    assert cr.build_description(EASY, 4) == (
        "Recovery 4mi. Keep HR under 140.\n"
        "\n"
        "Target: 4.0 mi @ 10:28/mi\n"
        "HR cap: 140 bpm\n"
        "\n"
        "local-fitness · plan #4"
    )


def test_the_description_omits_a_missing_hr_cap():
    assert "HR cap" not in cr.build_description({**EASY, "target_hr_max": None}, 4)


def test_a_duration_workout_describes_its_duration():
    got = cr.build_description(INTERVALS, 7)
    assert "Duration: 45 min" in got
    assert "Target:" not in got
    assert got.endswith("local-fitness · plan #7")


def test_a_pace_only_prescription_says_so():
    pace_only = {**EASY, "target_distance_m": None}
    assert "Target pace: 10:28/mi" in cr.build_description(pace_only, 4)


def test_a_prescription_with_no_prose_still_gets_a_description():
    got = cr.build_description({**EASY, "description": ""}, 4)
    assert got.startswith("Target: 4.0 mi @ 10:28/mi")
    assert got.endswith("local-fitness · plan #4")


# --- what does and doesn't produce an event --------------------------------

def test_a_rest_day_produces_no_event():
    # The decision that makes the calendar readable: an entry from this job
    # always means go train.
    assert cr.build_event(REST, 4) is None


def test_a_missing_workout_produces_no_event():
    assert cr.build_event(None, 4) is None
    assert cr.build_event({}, 4) is None


def test_a_workout_with_no_date_produces_no_event():
    assert cr.build_event({**EASY, "date": None}, 4) is None


def test_the_extended_properties_tag_the_event_as_ours():
    private = cr.build_event(EASY, 4)["extendedProperties"]["private"]
    assert private["source"] == "local-fitness"
    assert private["kind"] == "plan-workout"
    assert private["planId"] == "4"
    assert private["planDate"] == "2026-08-09"
    # The API rejects non-string extended property values outright.
    assert all(isinstance(v, str) for v in private.values())


# --- selecting the day's sessions ------------------------------------------

def test_build_events_selects_only_the_target_date():
    workouts = [
        {**EASY, "date": "2026-08-08"},
        {**EASY, "date": "2026-08-09"},
        {**EASY, "date": "2026-08-10"},
    ]
    events = cr.build_events(workouts, "2026-08-09", 4)
    assert [e["start"]["date"] for e in events] == ["2026-08-09"]


def test_build_events_returns_both_halves_of_a_double_day_in_seq_order():
    workouts = [
        {**EASY, "seq": 2, "type": "easy", "description": "PM shakeout"},
        {**INTERVALS, "seq": 1},
    ]
    events = cr.build_events(workouts, "2026-08-09", 4)
    assert [e["summary"] for e in events] == ["Intervals 45 min", "Easy run 4.0 mi @ 10:28/mi"]
    # Distinct ids, or the second insert would 409 against the first.
    assert events[0]["id"] != events[1]["id"]


def test_build_events_drops_a_rest_day_without_dropping_a_session_beside_it():
    workouts = [{**REST, "seq": 1}, {**EASY, "seq": 2}]
    events = cr.build_events(workouts, "2026-08-09", 4)
    assert [e["summary"] for e in events] == ["Easy run 4.0 mi @ 10:28/mi"]


def test_build_events_is_empty_when_the_date_has_no_row():
    assert cr.build_events([EASY], "2026-12-25", 4) == []
    assert cr.build_events([], "2026-08-09", 4) == []
