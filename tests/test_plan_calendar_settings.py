"""The `get/update_plan_calendar_settings` MCP pair.

Sibling of ``test_brief_email_settings.py`` and it carries the same two
non-negotiables, for the same reasons.

**The secret must never reach a tool payload.** `/mcp/` is served over the
network and reachable from a phone, so a tool that echoed the OAuth refresh
token would hand every client that can call the endpoint write access to the
user's calendar. `test_no_tool_output_ever_contains_the_calendar_secrets`
asserts against the *whole serialized payload* rather than named fields, so a
future field added to either tool is covered automatically — the leak that
actually happens arrives via a field nobody thought to review.

**Everything must resolve with no database at all.** A fresh clone has no
`data/fitness.db` until the first `fitness pull`, and CI never has one. This
exact class of bug shipped in 0.51.0 and took out 21 tests, because it passed
locally against a populated dev DB.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from local_fitness import config, db
from local_fitness.agent import gcal, tools

CLIENT_SECRET = "GOCSPX-zzzzclientsecretzzzz"
REFRESH_TOKEN = "1//0ezzzzrefreshtokenzzzz"


def call(tool, args):
    result = asyncio.run(tool.handler(args))
    text = result["content"][0]["text"]
    try:
        return json.loads(text), result.get("is_error", False)
    except json.JSONDecodeError:
        return text, result.get("is_error", False)


@pytest.fixture
def cfgdb(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    for k in ("LOCAL_FITNESS_PLAN_CALENDAR_ENABLED", "LOCAL_FITNESS_PLAN_CALENDAR_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LOCAL_FITNESS_GCAL_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("LOCAL_FITNESS_GCAL_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("LOCAL_FITNESS_GCAL_REFRESH_TOKEN", REFRESH_TOKEN)
    return p


# --- the security invariant ------------------------------------------------

def test_no_tool_output_ever_contains_the_calendar_secrets(cfgdb):
    # Whole-payload assertion, deliberately not field-by-field.
    got, _ = call(tools.get_plan_calendar_settings, {})
    blob = json.dumps(got)
    assert CLIENT_SECRET not in blob
    assert REFRESH_TOKEN not in blob
    assert got["credentials_configured"] is True

    got, _ = call(tools.update_plan_calendar_settings, {"enabled": False})
    assert CLIENT_SECRET not in json.dumps(got)
    assert REFRESH_TOKEN not in json.dumps(got)

    got, _ = call(tools.update_plan_calendar_settings, {"calendar_id": "primary"})
    assert CLIENT_SECRET not in json.dumps(got)
    assert REFRESH_TOKEN not in json.dumps(got)


def test_the_credentials_are_not_writable_through_the_update_tool(cfgdb):
    # A credential in the settings table would sit behind a network endpoint.
    got, err = call(tools.update_plan_calendar_settings,
                    {"refresh_token": "1//stolen", "client_secret": "x"})
    assert err
    assert "unknown field" in got["error"]
    assert db.get_setting("refresh_token", db_path=cfgdb) is None


# --- the fresh-clone contract ----------------------------------------------

@pytest.fixture
def no_db(tmp_path, monkeypatch):
    """CI's condition, and a stranger's first five minutes: no database yet."""
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "nope" / "fitness.db")
    for k in ("LOCAL_FITNESS_PLAN_CALENDAR_ENABLED", "LOCAL_FITNESS_PLAN_CALENDAR_ID"):
        monkeypatch.delenv(k, raising=False)


def test_settings_resolve_with_no_database_at_all(no_db):
    assert config.plan_calendar_enabled() is True
    assert config.plan_calendar_id() == "primary"


def test_env_still_wins_over_the_default_with_no_database(no_db, monkeypatch):
    # Falling through must reach the ENV layer, not jump straight to the
    # default — otherwise a .env-configured clone silently ignores its config.
    monkeypatch.setenv("LOCAL_FITNESS_PLAN_CALENDAR_ENABLED", "false")
    monkeypatch.setenv("LOCAL_FITNESS_PLAN_CALENDAR_ID", "env@group.calendar.google.com")
    assert config.plan_calendar_enabled() is False
    assert config.plan_calendar_id() == "env@group.calendar.google.com"


def test_the_read_tool_survives_a_missing_database(no_db, monkeypatch):
    monkeypatch.delenv("LOCAL_FITNESS_GCAL_REFRESH_TOKEN", raising=False)
    got, err = call(tools.get_plan_calendar_settings, {})
    assert not err
    assert got["enabled"] is True
    assert got["can_write"] is False


# --- the read tool ---------------------------------------------------------

def test_can_write_requires_both_enabled_and_credentials(cfgdb, monkeypatch):
    got, _ = call(tools.get_plan_calendar_settings, {})
    assert got["can_write"] is True and got["blocked_reason"] is None

    db.set_setting("plan_calendar_enabled", "false", db_path=cfgdb)
    got, _ = call(tools.get_plan_calendar_settings, {})
    assert got["can_write"] is False
    assert got["blocked_reason"] == "disabled via settings"

    db.set_setting("plan_calendar_enabled", "true", db_path=cfgdb)
    monkeypatch.delenv("LOCAL_FITNESS_GCAL_REFRESH_TOKEN")
    got, _ = call(tools.get_plan_calendar_settings, {})
    assert got["can_write"] is False
    # Names the fix, not just the symptom — this is read from a phone.
    assert "calendar-auth" in got["blocked_reason"]


def test_the_read_tool_reports_the_schedule_as_prose(cfgdb):
    # Prose, not data: the run time lives in the launchd plist, and a tool that
    # returned it as a field would imply it could be written back.
    got, _ = call(tools.get_plan_calendar_settings, {})
    assert got["schedule"] == tools._PLAN_CALENDAR_SCHEDULE
    assert "plancal" in got["schedule"]
    assert "time" not in got  # no editable-looking twin


def test_the_read_tool_states_that_a_rest_day_creates_nothing(cfgdb):
    # "Will tomorrow show up?" must be answerable without inferring it.
    got, _ = call(tools.get_plan_calendar_settings, {})
    assert "rest day creates nothing" in got["creates_events_for"]
    assert got["requires_active_plan"] is True


# --- the write tool --------------------------------------------------------

def test_enabled_round_trips_through_the_db_layer(cfgdb):
    got, err = call(tools.update_plan_calendar_settings, {"enabled": False})
    assert not err
    assert got["changed"] == ["enabled"] and got["enabled"] is False
    assert config.plan_calendar_enabled() is False

    got, _ = call(tools.update_plan_calendar_settings, {"enabled": True})
    assert config.plan_calendar_enabled() is True


def test_calendar_id_round_trips_and_is_re_read_not_echoed(cfgdb):
    got, err = call(tools.update_plan_calendar_settings,
                    {"calendar_id": "  team@group.calendar.google.com  "})
    assert not err
    assert got["calendar_id"] == "team@group.calendar.google.com"
    assert config.plan_calendar_id() == "team@group.calendar.google.com"


def test_both_fields_can_be_written_in_one_call(cfgdb):
    got, err = call(tools.update_plan_calendar_settings,
                    {"enabled": False, "calendar_id": "primary"})
    assert not err
    assert sorted(got["changed"]) == ["calendar_id", "enabled"]


def test_a_nonsense_calendar_id_is_rejected_here_not_at_1905(cfgdb):
    # Otherwise it becomes a 404 nobody sees until the event doesn't appear.
    got, err = call(tools.update_plan_calendar_settings, {"calendar_id": "my calendar"})
    assert err and "calendar_id" in got["error"]
    assert config.plan_calendar_id() == "primary"


def test_primary_is_accepted_even_though_it_is_not_an_address(cfgdb):
    got, err = call(tools.update_plan_calendar_settings, {"calendar_id": "primary"})
    assert not err and got["calendar_id"] == "primary"


@pytest.mark.parametrize("bad", [{"enabled": "yes"}, {"calendar_id": ""},
                                 {"calendar_id": 42}])
def test_malformed_values_are_rejected_without_writing(cfgdb, bad):
    got, err = call(tools.update_plan_calendar_settings, bad)
    assert err
    assert config.plan_calendar_enabled() is True
    assert config.plan_calendar_id() == "primary"


def test_an_empty_call_is_an_error_not_a_silent_no_op(cfgdb):
    got, err = call(tools.update_plan_calendar_settings, {})
    assert err and "nothing to update" in got["error"]


def test_every_write_reports_whether_the_job_can_actually_run(cfgdb, monkeypatch):
    # "I turned it on" must never be the last word when the write would still
    # be blocked by a missing credential.
    monkeypatch.delenv("LOCAL_FITNESS_GCAL_REFRESH_TOKEN")
    got, _ = call(tools.update_plan_calendar_settings, {"enabled": True})
    assert got["credentials_configured"] is False
    assert gcal.credentials_configured() is False


# --- registration ----------------------------------------------------------

def test_both_tools_are_reachable_over_http_not_just_stdio():
    # Neither returns a filesystem path, so the local-only rule doesn't apply —
    # and configuring the calendar from a phone is the point.
    names = {t.name for t in tools.ALL_TOOLS}
    assert {"get_plan_calendar_settings", "update_plan_calendar_settings"} <= names
    local = {t.name for t in tools.LOCAL_ONLY_TOOLS}
    assert not ({"get_plan_calendar_settings", "update_plan_calendar_settings"} & local)
