"""The pure calendar renderer.

Two properties carry the whole feature and each has its own section below: the
all-day END is EXCLUSIVE (get it wrong and the event silently doesn't render),
and the event id is derived from IDENTITY, not content (get it wrong and every
prescription edit leaves a stale duplicate on the calendar).
"""
from __future__ import annotations

import re

import pytest

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




# --- selecting the plan's remaining sessions -------------------------------

def test_build_plan_events_starts_at_the_start_date():
    workouts = [
        {**EASY, "date": "2026-08-07"},   # past
        {**EASY, "date": "2026-08-08"},   # the boundary is INCLUSIVE
        {**EASY, "date": "2026-08-09"},
    ]
    events = cr.build_plan_events(workouts, 4, "2026-08-08")
    assert [e["start"]["date"] for e in events] == ["2026-08-08", "2026-08-09"]


def test_build_plan_events_orders_by_date_then_seq():
    workouts = [
        {**EASY, "date": "2026-08-10", "seq": 2},
        {**EASY, "date": "2026-08-09", "seq": 2},
        {**EASY, "date": "2026-08-10", "seq": 1},
        {**EASY, "date": "2026-08-09", "seq": 1},
    ]
    events = cr.build_plan_events(workouts, 4, "2026-08-01")
    got = [(e["start"]["date"], e["extendedProperties"]["private"]["seq"])
           for e in events]
    assert got == [("2026-08-09", "1"), ("2026-08-09", "2"),
                   ("2026-08-10", "1"), ("2026-08-10", "2")]


def test_build_plan_events_drops_rest_days_only():
    workouts = [{**REST, "date": "2026-08-09"}, {**EASY, "date": "2026-08-10"}]
    events = cr.build_plan_events(workouts, 4, "2026-08-01")
    assert [e["start"]["date"] for e in events] == ["2026-08-10"]


def test_build_plan_events_is_empty_when_nothing_remains():
    assert cr.build_plan_events([EASY], 4, "2027-01-01") == []
    assert cr.build_plan_events([], 4, "2026-08-01") == []


def test_build_plan_events_refuses_rather_than_truncating_over_the_cap():
    # A half-written calendar is worse than one that didn't get written: you
    # would trust the days that made it and never learn about the ones that
    # didn't.
    workouts = [{**EASY, "date": f"2030-01-{d:02d}"} for d in range(1, 32)]
    workouts *= 7                       # 217 rows, over the 200 cap
    with pytest.raises(cr.TooManyEvents) as e:
        cr.build_plan_events(workouts, 4, "2029-01-01")
    assert str(cr.MAX_SYNC_EVENTS) in str(e.value)


def test_the_cap_matches_the_plan_row_limit():
    # A plan cannot hold more prescriptions than this, so hitting the cap means
    # bad input rather than a long plan.
    from local_fitness import plans

    assert cr.MAX_SYNC_EVENTS == plans.MAX_WORKOUTS


# --- reconcile -------------------------------------------------------------
#
# The whole reason this is a reconcile and not an upsert loop is the DELETE
# side. Every row of the contract table gets a test, and the two rails on
# deletion (the past, and tombstones) get two more.

def _on_calendar(event, **over):
    """What `events.list` echoes back for an event we wrote."""
    row = {**event, "status": "confirmed"}
    row.update(over)
    return row


def test_a_desired_event_that_is_absent_is_created():
    want = cr.build_event(EASY, 4)
    got = cr.reconcile([want], [], "2026-08-01")
    assert got["create"] == [want]
    assert got["update"] == got["delete"] == []


def test_an_identical_event_produces_no_request_at_all():
    # The property the nightly reconcile depends on: 42 events, zero writes.
    want = cr.build_event(EASY, 4)
    got = cr.reconcile([want], [_on_calendar(want)], "2026-08-01")
    assert got["unchanged"] == [want["id"]]
    assert got["create"] == got["update"] == got["delete"] == []


@pytest.mark.parametrize("field,changed", [
    ("summary", "Long run 9.0 mi"),
    ("description", "different prose"),
    ("transparency", "opaque"),
])
def test_a_changed_field_produces_an_update(field, changed):
    want = cr.build_event(EASY, 4)
    got = cr.reconcile([want], [_on_calendar(want, **{field: changed})],
                       "2026-08-01")
    assert got["update"] == [want]
    assert got["create"] == got["delete"] == []


def test_a_moved_date_produces_an_update():
    want = cr.build_event(EASY, 4)
    stale = _on_calendar(want, start={"date": "2026-09-09"})
    assert cr.reconcile([want], [stale], "2026-08-01")["update"] == [want]


def test_a_timestamp_shaped_date_still_compares_equal():
    # The API has been seen echoing `2026-08-09T00:00:00Z` where it wrote
    # `2026-08-09`. Treating that as a difference would rewrite all 42 events
    # on every single run.
    want = cr.build_event(EASY, 4)
    echoed = _on_calendar(want, start={"date": "2026-08-09T00:00:00Z"},
                          end={"date": "2026-08-10T00:00:00Z"})
    got = cr.reconcile([want], [echoed], "2026-08-01")
    assert got["unchanged"] == [want["id"]] and got["update"] == []


def test_an_event_the_plan_no_longer_wants_is_deleted():
    # THE case an upsert cannot express — a session turned into a rest day.
    orphan = _on_calendar(cr.build_event(EASY, 4))
    got = cr.reconcile([], [orphan], "2026-08-01")
    assert got["delete"] == [orphan]
    assert got["create"] == got["update"] == []


def test_a_rest_day_transition_deletes_the_event_it_replaces():
    # End to end through the builder, since this is the live shape of it.
    before = cr.build_plan_events([EASY], 4, "2026-08-01")
    after = cr.build_plan_events([{**EASY, "type": "rest"}], 4, "2026-08-01")
    assert after == []
    got = cr.reconcile(after, [_on_calendar(before[0])], "2026-08-01")
    assert [row["id"] for row in got["delete"]] == [before[0]["id"]]


def test_the_past_is_never_deleted():
    # Yesterday's event records what was prescribed yesterday. The plan may
    # have changed since; rewriting history to match is not a sync.
    old = _on_calendar(cr.build_event({**EASY, "date": "2026-07-01"}, 4))
    got = cr.reconcile([], [old], "2026-08-08")
    assert got["delete"] == []


def test_an_existing_past_event_is_invisible_to_the_diff():
    # Not updated, not deleted, not even counted as unchanged — skipped
    # outright. Callers pass `build_plan_events` output, which is already
    # start-filtered, so the desired side never mentions a past day either.
    stale_past = _on_calendar(
        cr.build_event({**EASY, "date": "2026-07-01"}, 4), summary="whatever")
    got = cr.reconcile([], [stale_past], "2026-08-08")
    assert got["update"] == got["delete"] == got["unchanged"] == []


def test_a_hand_deleted_event_is_never_resurrected():
    # Deleting the event is how a person says "not this one".
    want = cr.build_event(EASY, 4)
    got = cr.reconcile([want], [_on_calendar(want, status="cancelled")],
                       "2026-08-01")
    assert got["create"] == got["update"] == got["delete"] == []
    assert got["skipped_cancelled"] == [want["id"]]


def test_a_tombstone_for_a_dropped_day_is_not_re_deleted():
    # Doubly gone: the plan dropped it AND the calendar deleted it. Reporting
    # it would burn a line every run forever; deleting it, a request.
    gone = _on_calendar(cr.build_event(EASY, 4), status="cancelled")
    got = cr.reconcile([], [gone], "2026-08-01")
    assert got["delete"] == [] and got["skipped_cancelled"] == []


def test_reconciling_against_its_own_result_is_a_no_op():
    # The property the 20:05 backstop rests on, stated directly.
    desired = cr.build_plan_events(
        [{**EASY, "date": "2026-08-09"}, {**EASY, "date": "2026-08-10"}],
        4, "2026-08-01")
    settled = [_on_calendar(e) for e in desired]
    got = cr.reconcile(desired, settled, "2026-08-01")
    assert got["create"] == got["update"] == got["delete"] == []
    assert len(got["unchanged"]) == 2


def test_a_mixed_reconcile_sorts_every_event_into_exactly_one_bucket():
    keep = cr.build_event({**EASY, "date": "2026-08-09"}, 4)
    edit = cr.build_event({**EASY, "date": "2026-08-10"}, 4)
    add = cr.build_event({**EASY, "date": "2026-08-11"}, 4)
    drop = cr.build_event({**EASY, "date": "2026-08-12"}, 4)

    got = cr.reconcile(
        [keep, edit, add],
        [_on_calendar(keep), _on_calendar(edit, summary="stale"),
         _on_calendar(drop)],
        "2026-08-01")

    assert [e["id"] for e in got["create"]] == [add["id"]]
    assert [e["id"] for e in got["update"]] == [edit["id"]]
    assert [e["id"] for e in got["delete"]] == [drop["id"]]
    assert got["unchanged"] == [keep["id"]]


def test_a_distance_only_prescription_describes_its_distance():
    no_pace = {**EASY, "target_pace_sec_per_km": None}
    got = cr.build_description(no_pace, 4)
    assert "Target: 4.0 mi" in got
    assert "@" not in got


# --- reminders -------------------------------------------------------------
#
# The calendar is deliberately SILENT. An all-day event starts at midnight, so
# the calendar's own 30-minute default fires at 23:30 the night before — 41
# consecutive nights of it. And a day-of reminder is not expressible at all:
# Google accepts a negative `minutes` with HTTP 200 and clamps it to 0.

def test_every_event_states_no_reminders_explicitly():
    # Explicitly, not by omission: an event with no `reminders` key inherits
    # the CALENDAR's default, which is the 23:30 popup this exists to stop.
    reminders = cr.build_event(EASY, 4)["reminders"]
    assert reminders == {"useDefault": False, "overrides": []}


def test_the_reminder_constant_is_not_shared_between_events():
    # A shared dict would let one caller's mutation reach every other event.
    a, b = cr.build_event(EASY, 4), cr.build_event(EASY, 5)
    assert a["reminders"] is not b["reminders"]
    assert a["reminders"] is not cr.NO_REMINDERS


def test_googles_echo_of_no_reminders_compares_equal():
    # THE test. Google drops the empty `overrides` list on the way back, so a
    # raw dict comparison would report a difference forever and rewrite all 41
    # events on every single sync. Measured shape, not a guess.
    want = cr.build_event(EASY, 4)
    echoed = {**want, "status": "confirmed", "reminders": {"useDefault": False}}
    assert cr._differs(echoed, want) is False


def test_an_event_carrying_the_calendar_default_is_a_difference():
    # This is what every event written before 0.54.0 looks like, and it is what
    # makes the next sync repair them rather than leaving them noisy.
    want = cr.build_event(EASY, 4)
    inherited = {**want, "status": "confirmed", "reminders": {"useDefault": True}}
    assert cr._differs(inherited, want) is True


def test_a_stray_override_is_a_difference():
    want = cr.build_event(EASY, 4)
    noisy = {**want, "status": "confirmed",
             "reminders": {"useDefault": False,
                           "overrides": [{"method": "popup", "minutes": 30}]}}
    assert cr._differs(noisy, want) is True


def test_override_order_is_not_a_difference():
    # Their order is the server's to choose, not ours.
    want = {**cr.build_event(EASY, 4),
            "reminders": {"useDefault": False, "overrides": [
                {"method": "popup", "minutes": 30},
                {"method": "email", "minutes": 60}]}}
    reordered = {**want, "reminders": {"useDefault": False, "overrides": [
        {"method": "email", "minutes": 60},
        {"method": "popup", "minutes": 30}]}}
    assert cr._differs(reordered, want) is False


def test_a_missing_reminders_key_reads_as_the_calendar_default():
    # An event Google returns with no `reminders` key at all is inheriting the
    # default, so it must NOT compare equal to our explicit silence.
    want = cr.build_event(EASY, 4)
    bare = {k: v for k, v in want.items() if k != "reminders"}
    assert cr._differs(bare, want) is True


def test_a_reminder_change_is_seen_by_the_reconcile():
    # End to end: the field is compared where it actually matters.
    want = cr.build_event(EASY, 4)
    noisy = {**want, "status": "confirmed", "reminders": {"useDefault": True}}
    got = cr.reconcile([want], [noisy], "2026-08-01")
    assert got["update"] == [want] and got["unchanged"] == []
