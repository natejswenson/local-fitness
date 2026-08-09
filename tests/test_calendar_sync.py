"""The reconcile orchestration and the plan-write hooks.

``test_calendar_render.py`` proves the diff is right and ``test_gcal.py`` proves
each request is right; what is under test here is that the two are wired
together into the property the whole design rests on:

    **A second sync writes nothing.**

Everything else follows from it — the 20:05 backstop, the hook firing on every
plan edit, and a manual re-run all being free. The other half is the failure
contract: a plan write is COMMITTED before the calendar is touched, so no
calendar outcome may ever turn it into a failed tool call.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date as Date
from datetime import timedelta

import pytest

from local_fitness import db, plans
from local_fitness.agent import calendar_render, calendar_sync, gcal
from local_fitness.agent import tools as agent_tools

TODAY = Date.today().isoformat()
TOMORROW = (Date.today() + timedelta(days=1)).isoformat()
NEXT_WEEK = (Date.today() + timedelta(days=7)).isoformat()
YESTERDAY = (Date.today() - timedelta(days=1)).isoformat()


def _workout(date, seq=1, wtype="easy", **over):
    row = {
        "date": date, "seq": seq, "week_index": 1, "type": wtype,
        "target_distance_m": 8046.72, "target_pace_sec_per_km": 360.0,
        "target_duration_sec": None, "target_hr_max": 140.0,
        "description": "Easy 5mi. Keep HR under 140.",
    }
    row.update(over)
    return row


def _plan_fields():
    return {"title": "T", "goal_type": "10k",
            "race_date": (Date.today() + timedelta(days=90)).isoformat(),
            "created_at": "2026-08-01T00:00:00"}


class FakeCalendar:
    """An in-memory Google Calendar with the behaviours that matter.

    Not a mock replaying canned values — it holds state, so a second sync
    genuinely finds what the first one wrote, and `assert calls == 1` means the
    reconcile really did nothing rather than that a stub said so. Tombstones
    behave like Google's: a deleted event keeps its id and stays visible to a
    `showDeleted` listing.
    """

    def __init__(self):
        self.events: dict[str, dict] = {}
        self.calls: list[str] = []

    def install(self, monkeypatch):
        monkeypatch.setattr(gcal, "access_token", lambda cfg: "at-123")
        monkeypatch.setattr(gcal, "list_plan_events", self._list)
        monkeypatch.setattr(gcal, "upsert_event", self._upsert)
        monkeypatch.setattr(gcal, "delete_event", self._delete)
        return self

    def _list(self, cfg, token, plan_id):
        self.calls.append("list")
        return [e for e in self.events.values()
                if e["extendedProperties"]["private"]["planId"] == str(plan_id)]

    def _upsert(self, event, cfg, token):
        self.calls.append("upsert")
        self.events[event["id"]] = {**event, "status": "confirmed"}
        return {"action": "created", "id": event["id"], "html_link": None}

    def _delete(self, event_id, cfg, token):
        self.calls.append("delete")
        if event_id in self.events:                 # tombstone, not removal
            self.events[event_id]["status"] = "cancelled"
        return {"action": "deleted", "id": event_id, "already_gone": False}

    # --- helpers a test reads ---
    def live(self):
        return {k: v for k, v in self.events.items() if v["status"] != "cancelled"}

    def summaries(self):
        return sorted(e["summary"] for e in self.live().values())

    def delete_by_hand(self, event_id):
        self.events[event_id]["status"] = "cancelled"

    def reset_calls(self):
        self.calls.clear()


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """A DB, credentials in the env, and a fake calendar — seed(workouts)."""
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    monkeypatch.setenv("LOCAL_FITNESS_GCAL_CLIENT_ID", "cid")
    monkeypatch.setenv("LOCAL_FITNESS_GCAL_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("LOCAL_FITNESS_GCAL_REFRESH_TOKEN", "rtoken")
    monkeypatch.delenv("LOCAL_FITNESS_PLAN_CALENDAR_ID", raising=False)
    monkeypatch.delenv("LOCAL_FITNESS_PLAN_CALENDAR_ENABLED", raising=False)
    cal = FakeCalendar().install(monkeypatch)

    def seed(workouts, commit=True):
        pid = plans.insert_draft(_plan_fields(), workouts, db_path=p)
        if commit:
            plans.commit_plan(pid, now="t", db_path=p)
        return pid

    seed.calendar = cal
    seed.db_path = p
    return seed


def call(tool, args):
    result = asyncio.run(tool.handler(args))
    text = result["content"][0]["text"]
    try:
        return json.loads(text), result.get("is_error", False)
    except json.JSONDecodeError:
        return text, result.get("is_error", False)


# --- the property everything rests on --------------------------------------

def test_a_second_sync_writes_nothing(wired):
    cal = wired.calendar
    wired([_workout(TODAY), _workout(TOMORROW), _workout(NEXT_WEEK)])

    first = calendar_sync.sync_active_plan()
    assert (first["created"], first["updated"], first["deleted"]) == (3, 0, 0)

    cal.reset_calls()
    second = calendar_sync.sync_active_plan()

    assert (second["created"], second["updated"], second["deleted"]) == (0, 0, 0)
    assert second["unchanged"] == 3
    # ONE request in the steady state: the listing, and nothing else.
    assert cal.calls == ["list"]


def test_an_edited_day_updates_exactly_one_event(wired):
    cal = wired.calendar
    wired([_workout(TODAY), _workout(TOMORROW), _workout(NEXT_WEEK)])
    calendar_sync.sync_active_plan()

    plans.update_active_workout(
        TOMORROW, {"target_distance_m": 16093.4, "type": "long"},
        db_path=wired.db_path)
    cal.reset_calls()
    got = calendar_sync.sync_active_plan()

    assert (got["created"], got["updated"], got["deleted"]) == (0, 1, 0)
    assert got["changed_dates"] == [TOMORROW]
    assert cal.calls == ["list", "upsert"]
    assert "Long run 10.0 mi @ 9:39/mi" in cal.summaries()


def test_a_day_turned_to_rest_deletes_exactly_one_event(wired):
    cal = wired.calendar
    wired([_workout(TODAY), _workout(TOMORROW)])
    calendar_sync.sync_active_plan()
    assert len(cal.live()) == 2

    plans.update_active_workout(TOMORROW, {"type": "rest"}, db_path=wired.db_path)
    cal.reset_calls()
    got = calendar_sync.sync_active_plan()

    assert (got["created"], got["updated"], got["deleted"]) == (0, 0, 1)
    assert cal.calls == ["list", "delete"]
    assert len(cal.live()) == 1


def test_a_hand_deleted_event_is_not_recreated_on_the_next_sync(wired):
    cal = wired.calendar
    wired([_workout(TODAY), _workout(TOMORROW)])
    calendar_sync.sync_active_plan()
    victim = next(e["id"] for e in cal.live().values()
                  if e["start"]["date"] == TOMORROW)
    cal.delete_by_hand(victim)

    cal.reset_calls()
    got = calendar_sync.sync_active_plan()

    assert got["created"] == 0
    assert got["skipped_deleted_by_hand"] == 1
    assert cal.calls == ["list"]


def test_the_past_survives_a_sync_that_would_otherwise_drop_it(wired):
    cal = wired.calendar
    wired([_workout(YESTERDAY), _workout(TODAY)])
    # Sync from a week ago so yesterday's event gets written at all…
    calendar_sync.sync_active_plan(start=YESTERDAY)
    assert len(cal.live()) == 2

    # …then a normal sync (from today) must leave it alone, not reap it.
    cal.reset_calls()
    got = calendar_sync.sync_active_plan()
    assert got["deleted"] == 0
    assert len(cal.live()) == 2


# --- statuses that are outcomes, not exceptions ----------------------------

def test_no_active_plan_is_a_status_not_a_raise(wired):
    got = calendar_sync.sync_active_plan()
    assert got["status"] == "no_active_plan"
    assert wired.calendar.calls == []


def test_a_draft_is_not_synced(wired):
    wired([_workout(TOMORROW)], commit=False)
    assert calendar_sync.sync_active_plan()["status"] == "no_active_plan"
    assert wired.calendar.calls == []


def test_the_kill_switch_blocks_before_any_request(wired, monkeypatch):
    wired([_workout(TOMORROW)])
    monkeypatch.setenv("LOCAL_FITNESS_PLAN_CALENDAR_ENABLED", "0")
    got = calendar_sync.sync_active_plan()
    assert got["status"] == "blocked"
    assert wired.calendar.calls == []


def test_missing_credentials_block_without_touching_the_db(wired, monkeypatch):
    # Credentials are checked first because that check is a pure env read.
    monkeypatch.delenv("LOCAL_FITNESS_GCAL_REFRESH_TOKEN")
    assert "credentials" in calendar_sync.blocked_reason()


def test_dry_run_ignores_the_switch_and_opens_nothing(wired, monkeypatch):
    wired([_workout(TODAY), _workout(TOMORROW)])
    monkeypatch.setenv("LOCAL_FITNESS_PLAN_CALENDAR_ENABLED", "0")
    got = calendar_sync.sync_active_plan(dry_run=True)
    assert got["status"] == "dry_run" and len(got["events"]) == 2
    assert wired.calendar.calls == []


# --- abandoning ------------------------------------------------------------

def test_abandoning_clears_the_future_and_keeps_the_past(wired):
    cal = wired.calendar
    pid = wired([_workout(YESTERDAY), _workout(TODAY), _workout(NEXT_WEEK)])
    calendar_sync.sync_active_plan(start=YESTERDAY)
    assert len(cal.live()) == 3

    got = calendar_sync.remove_plan_events(pid)

    assert got["deleted"] == 2
    assert [e["start"]["date"] for e in cal.live().values()] == [YESTERDAY]


def test_abandoning_twice_deletes_nothing_the_second_time(wired):
    pid = wired([_workout(TODAY)])
    calendar_sync.sync_active_plan()
    calendar_sync.remove_plan_events(pid)

    wired.calendar.reset_calls()
    assert calendar_sync.remove_plan_events(pid)["deleted"] == 0
    assert wired.calendar.calls == ["list"]


def test_committing_a_new_plan_clears_the_old_plan_s_events(wired):
    # `event_id` keys on plan_id, so the new plan lands on entirely different
    # ids — the old plan's sessions would otherwise sit beside the new ones,
    # both tagged and both looking authoritative.
    cal = wired.calendar
    old = wired([_workout(TOMORROW, description="OLD PLAN")])
    calendar_sync.sync_active_plan()
    assert len(cal.live()) == 1

    new = plans.insert_draft(_plan_fields(), [_workout(TOMORROW, description="NEW PLAN")],
                             db_path=wired.db_path)
    plans.commit_plan(new, now="t", db_path=wired.db_path)
    got = calendar_sync.sync_after_commit(old)

    assert len(cal.live()) == 1
    assert "NEW PLAN" in next(iter(cal.live().values()))["description"]
    assert got["superseded_plan_deleted"] == 1


# --- the failure contract --------------------------------------------------

def test_a_calendar_outage_never_fails_the_plan_write(wired, monkeypatch):
    # The write is already committed and is the source of truth. If this raised,
    # the model would reasonably retry the edit — which is how a transport
    # problem becomes a data problem.
    wired([_workout(TOMORROW)])

    def _down(*a, **k):
        raise gcal.CalendarApiError("Google is down")

    monkeypatch.setattr(gcal, "list_plan_events", _down)

    body, err = call(agent_tools.update_plan_workout,
                     {"date": TOMORROW, "distance_mi": 7.0})

    assert not err
    assert body["distance_meters"] == pytest.approx(11265.4, abs=1)
    assert body["calendar"]["status"] == "error"
    assert "Google is down" in body["calendar"]["error"]
    # And the DB really changed, despite the calendar failing.
    with db.connect(wired.db_path) as conn:
        row = plans.get_active_plan(conn=conn)["workouts"][0]
    assert row["target_distance_m"] == pytest.approx(11265.4, abs=1)


def test_an_unconfigured_clone_gets_no_calendar_key_at_all(wired, monkeypatch):
    # Not `null`, not an error — omitted. Otherwise every plan edit anyone ever
    # makes carries a line of noise about a feature they never set up.
    wired([_workout(TOMORROW)])
    monkeypatch.delenv("LOCAL_FITNESS_GCAL_REFRESH_TOKEN")

    body, err = call(agent_tools.update_plan_workout,
                     {"date": TOMORROW, "distance_mi": 7.0})
    assert not err and "calendar" not in body
    assert wired.calendar.calls == []


# --- the hooks -------------------------------------------------------------

def test_update_plan_workout_syncs_the_calendar(wired):
    cal = wired.calendar
    wired([_workout(TODAY), _workout(TOMORROW)])
    calendar_sync.sync_active_plan()

    body, err = call(agent_tools.update_plan_workout,
                     {"date": TOMORROW, "type": "long", "distance_mi": 10.0})

    assert not err
    assert body["calendar"]["updated"] == 1
    assert body["calendar"]["changed_dates"] == [TOMORROW]
    assert "Long run 10.0 mi @ 9:39/mi" in cal.summaries()


def test_update_plan_workouts_syncs_the_whole_batch_once(wired):
    cal = wired.calendar
    wired([_workout(TODAY), _workout(TOMORROW)])
    calendar_sync.sync_active_plan()
    cal.reset_calls()

    body, err = call(agent_tools.update_plan_workouts, {"updates": [
        {"date": TODAY, "type": "rest"},
        {"date": TOMORROW, "type": "long", "distance_mi": 12.0},
    ]})

    assert not err and body["updated"] == 2
    assert (body["calendar"]["updated"], body["calendar"]["deleted"]) == (1, 1)
    # One reconcile for the batch, not one per entry.
    assert cal.calls.count("list") == 1


def test_commit_training_plan_syncs_the_new_plan(wired):
    cal = wired.calendar
    pid = wired([_workout(TOMORROW)], commit=False)

    body, err = call(agent_tools.commit_training_plan, {"plan_id": pid})

    assert not err and body["status"] == "active"
    assert body["calendar"]["created"] == 1
    assert len(cal.live()) == 1


def test_abandon_active_plan_clears_the_calendar(wired):
    cal = wired.calendar
    wired([_workout(TODAY), _workout(NEXT_WEEK)])
    calendar_sync.sync_active_plan()

    body, err = call(agent_tools.abandon_active_plan, {})

    assert not err and body["status"] == "archived"
    assert body["calendar"]["deleted"] == 2
    assert cal.live() == {}


def test_the_draft_tools_never_touch_the_calendar(wired):
    # A draft governs nothing, so it belongs on no calendar.
    pid = wired([_workout(TOMORROW)], commit=False)
    body, err = call(agent_tools.revise_training_plan, {"plan_id": pid, "title": "X"})
    assert not err and "calendar" not in body

    body, err = call(agent_tools.discard_training_plan_draft, {"plan_id": pid})
    assert not err and "calendar" not in body
    assert wired.calendar.calls == []


def test_a_failed_plan_write_never_reaches_the_calendar(wired):
    wired([_workout(TOMORROW)])
    body, err = call(agent_tools.update_plan_workout,
                     {"date": "2099-01-01", "distance_mi": 5.0})
    assert err
    assert wired.calendar.calls == []


def test_the_cap_surfaces_as_an_error_rather_than_a_partial_calendar(
        wired, monkeypatch):
    wired([_workout(TOMORROW)])
    monkeypatch.setattr(calendar_render, "MAX_SYNC_EVENTS", 0)

    body, err = call(agent_tools.update_plan_workout,
                     {"date": TOMORROW, "distance_mi": 7.0})

    assert not err                       # the plan write still succeeded
    assert body["calendar"]["status"] == "error"
    assert wired.calendar.calls == []    # nothing was written


def test_the_abandon_hook_is_fail_soft_too(wired, monkeypatch):
    # Same contract as the edit hook, and it needs its own test: abandon takes
    # a different code path (remove, not sync) and archiving is irreversible,
    # so a raise here would report failure for work that definitely happened.
    wired([_workout(TOMORROW)])
    monkeypatch.setattr(
        gcal, "list_plan_events",
        lambda *a, **k: (_ for _ in ()).throw(gcal.CalendarApiError("down")))

    body, err = call(agent_tools.abandon_active_plan, {})

    assert not err and body["status"] == "archived"
    assert body["calendar"]["status"] == "error"
    with db.connect(wired.db_path) as conn:
        assert plans.get_active_plan(conn=conn) is None


def test_the_commit_hook_is_fail_soft_too(wired, monkeypatch):
    pid = wired([_workout(TOMORROW)], commit=False)
    monkeypatch.setattr(
        gcal, "list_plan_events",
        lambda *a, **k: (_ for _ in ()).throw(gcal.CalendarApiError("down")))

    body, err = call(agent_tools.commit_training_plan, {"plan_id": pid})

    assert not err and body["status"] == "active"
    assert body["calendar"]["status"] == "error"
    with db.connect(wired.db_path) as conn:
        assert plans.get_active_plan(conn=conn)["plan_id"] == pid


def test_removing_events_respects_the_kill_switch(wired, monkeypatch):
    pid = wired([_workout(TOMORROW)])
    monkeypatch.setenv("LOCAL_FITNESS_PLAN_CALENDAR_ENABLED", "0")
    got = calendar_sync.remove_plan_events(pid)
    assert got["status"] == "blocked"
    assert wired.calendar.calls == []
