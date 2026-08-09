"""Tests for `fitness plan-calendar` (the 19:05 launchd job's entry point).

Scope matches ``test_cli_brief_email.py``: WIRING, not the downstream modules.
The diff is covered by ``test_calendar_render.py``, each request by
``test_gcal.py``, and the reconcile end to end by ``test_calendar_sync.py``.
What is under test here is what the COMMAND does with each outcome — above all
that the ordinary nothing-to-do cases exit 0, print a sentence, and never reach
the network.

Those cases are the load-bearing piece, the way the sent marker is in the email
job. No active plan and a disabled switch are not failures; a job that exited
non-zero on them would be muted inside a fortnight. Everything else the command
adds is reporting, and the one report that must not be dropped is
`skipped_deleted_by_hand` — without it, a session that never appears on the
calendar has no explanation anywhere.
"""
from __future__ import annotations

import json
from datetime import date as Date
from datetime import timedelta

import pytest
from click.testing import CliRunner

from local_fitness import cli, db, plans
from local_fitness.agent import calendar_sync, gcal

TODAY = Date.today().isoformat()
TOMORROW = (Date.today() + timedelta(days=1)).isoformat()


@pytest.fixture
def runner():
    return CliRunner()


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


@pytest.fixture
def planned(tmp_path, monkeypatch):
    """A DB with an ACTIVE plan; call it with the workouts to seed."""
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)

    def seed(workouts):
        pid = plans.insert_draft(_plan_fields(), workouts, db_path=p)
        plans.commit_plan(pid, now="t", db_path=p)
        return pid

    return seed


@pytest.fixture
def calendar(monkeypatch):
    """Credentials present + a recording stub for the sync entry point."""
    monkeypatch.setenv("LOCAL_FITNESS_GCAL_CLIENT_ID", "cid")
    monkeypatch.setenv("LOCAL_FITNESS_GCAL_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("LOCAL_FITNESS_GCAL_REFRESH_TOKEN", "rtoken")
    monkeypatch.delenv("LOCAL_FITNESS_PLAN_CALENDAR_ID", raising=False)
    monkeypatch.delenv("LOCAL_FITNESS_PLAN_CALENDAR_ENABLED", raising=False)
    monkeypatch.setattr(cli, "_notify", lambda *a: None)

    seen: list[dict] = []

    def install(result=None, raises=None):
        def _sync(start=None, dry_run=False, **kw):
            seen.append({"start": start, "dry_run": dry_run})
            if raises is not None:
                raise raises
            return result
        monkeypatch.setattr(calendar_sync, "sync_active_plan", _sync)
        return seen

    install.seen = seen
    return install


SYNCED = {
    "status": "synced", "plan_id": 4, "calendar_id": "primary", "start": TODAY,
    "created": 2, "updated": 1, "deleted": 1, "unchanged": 38,
    "skipped_deleted_by_hand": 0, "changed_dates": [TODAY, TOMORROW],
}


# --- the happy path --------------------------------------------------------

def test_a_sync_reports_every_bucket(runner, calendar):
    calendar(SYNCED)
    result = runner.invoke(cli.main, ["plan-calendar"])

    assert result.exit_code == 0, result.output
    assert "2 created" in result.output
    assert "1 updated" in result.output
    assert "1 deleted" in result.output
    assert "38 unchanged" in result.output
    assert "primary" in result.output


def test_the_changed_dates_are_named(runner, calendar):
    # Counts can't answer "did MY edit land"; the dates can.
    calendar(SYNCED)
    result = runner.invoke(cli.main, ["plan-calendar"])
    assert f"changed: {TODAY}, {TOMORROW}" in result.output


def test_a_hand_deleted_day_is_explained_not_swallowed(runner, calendar):
    # Without this line, a session that never appears has no explanation.
    calendar({**SYNCED, "skipped_deleted_by_hand": 3})
    result = runner.invoke(cli.main, ["plan-calendar"])
    assert "3 day(s) skipped" in result.output
    assert "not being put back" in result.output


def test_a_quiet_sync_says_nothing_changed(runner, calendar):
    quiet = {**SYNCED, "created": 0, "updated": 0, "deleted": 0,
             "unchanged": 42, "changed_dates": []}
    calendar(quiet)
    result = runner.invoke(cli.main, ["plan-calendar"])
    assert result.exit_code == 0
    assert "0 created, 0 updated, 0 deleted, 42 unchanged" in result.output
    assert "changed:" not in result.output


def test_a_quiet_sync_fires_no_notification(runner, calendar, monkeypatch):
    # The 20:05 backstop runs every night and normally changes nothing. A
    # nightly "calendar updated" toast for zero changes is how a notification
    # stops being read.
    notified: list[str] = []
    monkeypatch.setattr(cli, "_notify", notified.append)
    calendar({**SYNCED, "created": 0, "updated": 0, "deleted": 0,
              "changed_dates": []})
    runner.invoke(cli.main, ["plan-calendar"])
    assert notified == []


def test_a_sync_that_changed_something_does_notify(runner, calendar, monkeypatch):
    notified: list[str] = []
    monkeypatch.setattr(cli, "_notify", notified.append)
    calendar(SYNCED)
    runner.invoke(cli.main, ["plan-calendar"])
    assert notified and "4 day(s)" in notified[0]


def test_from_overrides_the_start_date(runner, calendar):
    seen = calendar(SYNCED)
    runner.invoke(cli.main, ["plan-calendar", "--from", "2026-01-01"])
    assert seen[0]["start"] == "2026-01-01"


def test_the_default_start_is_left_to_the_sync(runner, calendar):
    # `sync_active_plan` owns "today", so the CLI must not compute a second one.
    seen = calendar(SYNCED)
    runner.invoke(cli.main, ["plan-calendar"])
    assert seen[0]["start"] is None


# --- the no-op paths: quiet, exit 0 ----------------------------------------

def test_no_active_plan_exits_zero_with_a_sentence(runner, calendar):
    calendar({"status": "no_active_plan", "reason": "no active training plan"})
    result = runner.invoke(cli.main, ["plan-calendar"])
    assert result.exit_code == 0
    assert "Nothing to sync — no active training plan." in result.output


def test_a_blocked_sync_exits_zero_with_its_reason(runner, calendar):
    calendar({"status": "blocked", "reason": "disabled via settings"})
    result = runner.invoke(cli.main, ["plan-calendar"])
    assert result.exit_code == 0
    assert "disabled via settings" in result.output


def test_a_real_no_active_plan_never_reaches_the_network(runner, planned,
                                                         monkeypatch):
    # Same as above but through the REAL sync, so the guard order is exercised
    # rather than asserted about a stub.
    monkeypatch.setenv("LOCAL_FITNESS_GCAL_CLIENT_ID", "cid")
    monkeypatch.setenv("LOCAL_FITNESS_GCAL_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("LOCAL_FITNESS_GCAL_REFRESH_TOKEN", "rtoken")
    called: list[str] = []
    monkeypatch.setattr(gcal, "access_token", lambda cfg: called.append("t"))

    result = runner.invoke(cli.main, ["plan-calendar"])
    assert result.exit_code == 0 and called == []


def test_the_kill_switch_stops_it_before_the_plan_is_read(runner, planned,
                                                          monkeypatch):
    planned([_workout(TOMORROW)])
    monkeypatch.setenv("LOCAL_FITNESS_GCAL_REFRESH_TOKEN", "rtoken")
    monkeypatch.setenv("LOCAL_FITNESS_GCAL_CLIENT_ID", "cid")
    monkeypatch.setenv("LOCAL_FITNESS_GCAL_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("LOCAL_FITNESS_PLAN_CALENDAR_ENABLED", "0")
    called: list[str] = []
    monkeypatch.setattr(gcal, "access_token", lambda cfg: called.append("t"))

    result = runner.invoke(cli.main, ["plan-calendar"])
    assert result.exit_code == 0
    assert called == []
    assert "disabled" in result.output


# --- failures --------------------------------------------------------------

def test_missing_credentials_exit_2_naming_the_variable(runner, planned,
                                                         monkeypatch):
    planned([_workout(TOMORROW)])
    monkeypatch.setenv("LOCAL_FITNESS_GCAL_CLIENT_ID", "cid")
    monkeypatch.setenv("LOCAL_FITNESS_GCAL_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("LOCAL_FITNESS_GCAL_REFRESH_TOKEN", "rtoken")
    monkeypatch.setattr(
        calendar_sync, "sync_active_plan",
        lambda **kw: (_ for _ in ()).throw(
            gcal.CalendarNotConfigured("LOCAL_FITNESS_GCAL_CLIENT_ID is not set")))

    result = runner.invoke(cli.main, ["plan-calendar"])
    assert result.exit_code == 2
    assert "LOCAL_FITNESS_GCAL_CLIENT_ID" in result.output


def test_over_the_cap_exits_2_rather_than_writing_part_of_a_plan(runner,
                                                                 calendar):
    from local_fitness.agent import calendar_render

    calendar(raises=calendar_render.TooManyEvents("217 events, over the 200 cap"))
    result = runner.invoke(cli.main, ["plan-calendar"])
    assert result.exit_code == 2
    assert "over the 200 cap" in result.output


def test_a_transport_failure_exits_nonzero_and_notifies(runner, calendar,
                                                         monkeypatch):
    # launchd's only signals are the exit code and the log.
    notified: list[str] = []
    monkeypatch.setattr(cli, "_notify", notified.append)
    calendar(raises=gcal.CalendarApiError("boom"))

    result = runner.invoke(cli.main, ["plan-calendar"])
    assert result.exit_code != 0
    assert notified and "FAILED" in notified[0]


def test_no_notify_suppresses_the_failure_notification(runner, calendar,
                                                        monkeypatch):
    notified: list[str] = []
    monkeypatch.setattr(cli, "_notify", notified.append)
    calendar(raises=gcal.CalendarApiError("boom"))

    runner.invoke(cli.main, ["plan-calendar", "--no-notify"])
    assert notified == []


# --- dry run ---------------------------------------------------------------

def test_dry_run_prints_every_event_and_opens_no_socket(runner, planned,
                                                         monkeypatch):
    planned([_workout(TODAY), _workout(TOMORROW)])
    called: list[str] = []
    monkeypatch.setattr(gcal, "access_token", lambda cfg: called.append("t"))

    result = runner.invoke(cli.main, ["plan-calendar", "--dry-run"])

    assert result.exit_code == 0 and called == []
    payload = json.loads(result.output[:result.output.rindex("]") + 1])
    assert [e["start"]["date"] for e in payload] == [TODAY, TOMORROW]
    assert "2 event(s)" in result.output and "Nothing was sent" in result.output


def test_dry_run_works_with_no_credentials_at_all(runner, planned, monkeypatch):
    # Inspectable BEFORE setup, which is exactly when the real path refuses.
    planned([_workout(TOMORROW)])
    for var in ("LOCAL_FITNESS_GCAL_CLIENT_ID", "LOCAL_FITNESS_GCAL_CLIENT_SECRET",
                "LOCAL_FITNESS_GCAL_REFRESH_TOKEN"):
        monkeypatch.delenv(var, raising=False)

    result = runner.invoke(cli.main, ["plan-calendar", "--dry-run"])
    assert result.exit_code == 0 and "Nothing was sent" in result.output


def test_dry_run_ignores_the_kill_switch(runner, planned, monkeypatch):
    # A disabled setup must still be inspectable, or debugging "why is nothing
    # on my calendar" starts by re-enabling the thing you're debugging.
    planned([_workout(TOMORROW)])
    monkeypatch.setenv("LOCAL_FITNESS_PLAN_CALENDAR_ENABLED", "0")

    result = runner.invoke(cli.main, ["plan-calendar", "--dry-run"])
    assert result.exit_code == 0 and "Nothing was sent" in result.output


# --- calendar-auth ---------------------------------------------------------
#
# The socket half (`_capture_oauth_redirect`) is left untested on purpose — it
# binds a port, opens a browser and serves one request, so a test of it would
# assert that a stub replayed its own canned value. What IS tested is every
# decision made on the captured result, because one of them is a security
# check: the `state` nonce is the only thing distinguishing our own redirect
# from an attacker-initiated one.

@pytest.fixture
def consent(monkeypatch):
    """Stub the loopback capture; return a setter for what it 'caught'."""
    monkeypatch.setenv("LOCAL_FITNESS_GCAL_CLIENT_ID", "cid")
    monkeypatch.setenv("LOCAL_FITNESS_GCAL_CLIENT_SECRET", "csecret")
    seen: dict[str, object] = {}

    def install(response, *, echo_state=True):
        def _capture(build_url):
            url = build_url("http://127.0.0.1:9999")
            seen["url"] = url
            state = url.split("state=")[1].split("&")[0]
            payload = dict(response)
            if echo_state:
                payload.setdefault("state", state)
            return "http://127.0.0.1:9999", payload

        monkeypatch.setattr(cli, "_capture_oauth_redirect", _capture)
        return seen

    return install


def test_calendar_auth_prints_the_refresh_token_for_dotenv(
        runner, consent, monkeypatch):
    # PRINTED, never written: `.env` holds every other credential in this
    # deployment, and a bug in a program that edits it is a bug that eats them.
    seen = consent({"code": "auth-code-1"})
    monkeypatch.setattr(
        gcal, "exchange_code",
        lambda cid, sec, code, uri, ver: f"1//token-for-{code}")

    result = runner.invoke(cli.main, ["calendar-auth"])
    assert result.exit_code == 0
    assert "LOCAL_FITNESS_GCAL_REFRESH_TOKEN=1//token-for-auth-code-1" in result.output
    assert "code_challenge_method=S256" in seen["url"]


def test_calendar_auth_aborts_on_a_state_mismatch(runner, consent, monkeypatch):
    # The nonce is the whole point: a mismatched state means the response did
    # not come from the request we made, so the code must NOT be exchanged.
    consent({"code": "attacker-code", "state": "not-ours"}, echo_state=False)

    def _boom(*a, **k):
        raise AssertionError("a mismatched state still reached the exchange")

    monkeypatch.setattr(gcal, "exchange_code", _boom)

    result = runner.invoke(cli.main, ["calendar-auth"])
    assert result.exit_code == 1
    assert "State mismatch" in result.output


def test_calendar_auth_reports_a_denied_consent(runner, consent, monkeypatch):
    consent({"error": "access_denied"})
    monkeypatch.setattr(gcal, "exchange_code",
                        lambda *a, **k: pytest.fail("exchanged without a code"))

    result = runner.invoke(cli.main, ["calendar-auth"])
    assert result.exit_code == 1
    assert "access_denied" in result.output


def test_calendar_auth_refuses_without_a_client_id(runner, monkeypatch):
    monkeypatch.delenv("LOCAL_FITNESS_GCAL_CLIENT_ID", raising=False)
    monkeypatch.setenv("LOCAL_FITNESS_GCAL_CLIENT_SECRET", "csecret")

    def _boom(*a, **k):
        raise AssertionError("a browser was opened with no client configured")

    monkeypatch.setattr(cli, "_capture_oauth_redirect", _boom)

    result = runner.invoke(cli.main, ["calendar-auth"])
    assert result.exit_code == 2
    assert "LOCAL_FITNESS_GCAL_CLIENT_ID" in result.output
