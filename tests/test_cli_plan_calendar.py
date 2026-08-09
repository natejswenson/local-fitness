"""Tests for `fitness plan-calendar` (the 19:05 launchd job's entry point).

Scope matches ``test_cli_brief_email.py``: WIRING, not the downstream modules.
The event body is covered by ``test_calendar_render.py`` and the upsert by
``test_gcal.py``; what is under test here is the ordering and the guards —
which steps run, which are skipped, what the exit code is, and above all
**that the no-op paths never reach the network**.

Those no-op paths are the load-bearing piece here, the way the sent marker is
in the email job. A rest day and "no active plan" are ordinary outcomes that
happen several times a week, so they must exit 0 and quietly — a job that
reported failure on them would be muted inside a fortnight. And the kill switch
has to fire before the plan read, not just before the write: a switch that
still does the work and throws the result away is not a switch.
"""
from __future__ import annotations

import json
from datetime import date as Date
from datetime import timedelta

import pytest
from click.testing import CliRunner

from local_fitness import cli, db, plans
from local_fitness.agent import gcal

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


@pytest.fixture
def planned(tmp_path, monkeypatch):
    """A DB with an ACTIVE plan prescribing an easy run tomorrow."""
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)

    def seed(workouts):
        pid = plans.insert_draft(_plan_fields(), workouts, db_path=p)
        plans.commit_plan(pid, now="t", db_path=p)
        return pid

    return seed


def _plan_fields():
    return {
        "title": "T", "goal_type": "10k",
        "race_date": (Date.today() + timedelta(days=90)).isoformat(),
        "created_at": "2026-08-01T00:00:00",
    }


@pytest.fixture
def calendar(monkeypatch):
    """Credentials present + a recording stub for the two gcal entry points."""
    monkeypatch.setenv("LOCAL_FITNESS_GCAL_CLIENT_ID", "cid")
    monkeypatch.setenv("LOCAL_FITNESS_GCAL_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("LOCAL_FITNESS_GCAL_REFRESH_TOKEN", "rtoken")
    monkeypatch.delenv("LOCAL_FITNESS_PLAN_CALENDAR_ID", raising=False)
    monkeypatch.delenv("LOCAL_FITNESS_PLAN_CALENDAR_ENABLED", raising=False)

    written: list[dict] = []

    def _token(cfg):
        written.append({"kind": "token"})
        return "at-123"

    def _upsert(event, cfg, token):
        written.append({"kind": "upsert", "event": event})
        return {"action": "created", "id": event["id"], "html_link": "https://cal/x"}

    monkeypatch.setattr(gcal, "access_token", _token)
    monkeypatch.setattr(gcal, "upsert_event", _upsert)
    monkeypatch.setattr(cli, "_notify", lambda *a: None)
    return written


# --- the happy path --------------------------------------------------------

def test_tomorrows_session_is_written_to_the_calendar(runner, planned, calendar):
    planned([_workout(TOMORROW)])
    result = runner.invoke(cli.main, ["plan-calendar"])

    assert result.exit_code == 0, result.output
    upserts = [c for c in calendar if c["kind"] == "upsert"]
    assert len(upserts) == 1
    event = upserts[0]["event"]
    assert event["summary"] == "Easy run 5.0 mi @ 9:39/mi"
    assert event["start"]["date"] == TOMORROW
    assert "created" in result.output


def test_it_targets_tomorrow_not_today(runner, planned, calendar):
    # The whole point of the job: you get tomorrow's session tonight. Grabbing
    # today's would be a reminder that arrives after the run.
    today = Date.today().isoformat()
    planned([
        _workout(today, description="TODAY — should not be scheduled"),
        _workout(TOMORROW, description="TOMORROW"),
    ])
    runner.invoke(cli.main, ["plan-calendar"])

    (upsert,) = [c for c in calendar if c["kind"] == "upsert"]
    assert "TOMORROW" in upsert["event"]["description"]


def test_an_explicit_date_overrides_tomorrow(runner, planned, calendar):
    far = (Date.today() + timedelta(days=5)).isoformat()
    planned([_workout(TOMORROW), _workout(far, description="FRIDAY")])
    runner.invoke(cli.main, ["plan-calendar", "--date", far])

    (upsert,) = [c for c in calendar if c["kind"] == "upsert"]
    assert upsert["event"]["start"]["date"] == far


def test_both_halves_of_a_double_day_are_written(runner, planned, calendar):
    planned([_workout(TOMORROW, seq=1),
             _workout(TOMORROW, seq=2, wtype="tempo", target_duration_sec=1800)])
    result = runner.invoke(cli.main, ["plan-calendar"])

    assert result.exit_code == 0
    assert len([c for c in calendar if c["kind"] == "upsert"]) == 2
    assert "2 of 2 event(s) written" in result.output


def test_one_token_is_fetched_for_the_whole_run(runner, planned, calendar):
    planned([_workout(TOMORROW, seq=1), _workout(TOMORROW, seq=2)])
    runner.invoke(cli.main, ["plan-calendar"])
    assert len([c for c in calendar if c["kind"] == "token"]) == 1


# --- the no-op paths: quiet, exit 0, and NO network ------------------------

def test_a_rest_day_writes_nothing_and_succeeds(runner, planned, calendar):
    planned([_workout(TOMORROW, wtype="rest", target_distance_m=None,
                      target_pace_sec_per_km=None, target_hr_max=None,
                      description="Rest day")])
    result = runner.invoke(cli.main, ["plan-calendar"])

    assert result.exit_code == 0
    assert calendar == []          # not even a token was fetched
    assert "Nothing prescribed" in result.output


def test_a_date_with_no_prescription_writes_nothing_and_succeeds(
        runner, planned, calendar):
    planned([_workout(Date.today().isoformat())])   # nothing for tomorrow
    result = runner.invoke(cli.main, ["plan-calendar"])

    assert result.exit_code == 0
    assert calendar == []
    assert "Nothing prescribed" in result.output


def test_no_active_plan_writes_nothing_and_succeeds(runner, planned, calendar):
    # `planned` is not called: schema exists, no plan in it.
    result = runner.invoke(cli.main, ["plan-calendar"])

    assert result.exit_code == 0
    assert calendar == []
    assert "No active training plan" in result.output


def test_a_draft_plan_is_not_treated_as_active(runner, tmp_path, monkeypatch,
                                               calendar):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    # commit_plan deliberately NOT called — the plan stays a draft.
    plans.insert_draft(_plan_fields(), [_workout(TOMORROW)], db_path=p)

    result = runner.invoke(cli.main, ["plan-calendar"])
    assert result.exit_code == 0
    assert calendar == []
    assert "No active training plan" in result.output


# --- the guards ------------------------------------------------------------

def test_the_kill_switch_fires_before_the_plan_is_even_read(
        runner, planned, calendar, monkeypatch):
    planned([_workout(TOMORROW)])
    monkeypatch.setenv("LOCAL_FITNESS_PLAN_CALENDAR_ENABLED", "0")

    def _boom(*a, **k):
        raise AssertionError("the plan was read despite the kill switch")

    monkeypatch.setattr(cli, "_load_plan_events", _boom)

    result = runner.invoke(cli.main, ["plan-calendar"])
    assert result.exit_code == 0
    assert calendar == []
    assert "disabled" in result.output


def test_missing_credentials_exit_2_naming_the_variable(
        runner, planned, calendar, monkeypatch):
    planned([_workout(TOMORROW)])
    monkeypatch.delenv("LOCAL_FITNESS_GCAL_REFRESH_TOKEN")

    result = runner.invoke(cli.main, ["plan-calendar"])
    assert result.exit_code == 2
    assert "LOCAL_FITNESS_GCAL_REFRESH_TOKEN" in result.output
    assert not [c for c in calendar if c["kind"] == "upsert"]


def test_an_api_failure_propagates_as_a_nonzero_exit(
        runner, planned, calendar, monkeypatch):
    # launchd's only signal is the exit code and the log; swallowing this would
    # make a broken calendar indistinguishable from a rest day.
    planned([_workout(TOMORROW)])
    notified: list[str] = []
    monkeypatch.setattr(cli, "_notify", notified.append)
    monkeypatch.setattr(gcal, "upsert_event", _raise)

    result = runner.invoke(cli.main, ["plan-calendar"])
    assert result.exit_code != 0
    assert notified and "FAILED" in notified[0]


def _raise(*args, **kwargs):
    raise gcal.CalendarApiError("boom")


def test_no_notify_suppresses_the_failure_notification(
        runner, planned, calendar, monkeypatch):
    planned([_workout(TOMORROW)])
    notified: list[str] = []
    monkeypatch.setattr(cli, "_notify", notified.append)
    monkeypatch.setattr(gcal, "upsert_event", _raise)

    runner.invoke(cli.main, ["plan-calendar", "--no-notify"])
    assert notified == []


# --- dry run ---------------------------------------------------------------

def test_dry_run_prints_the_event_and_opens_no_socket(runner, planned, calendar):
    planned([_workout(TOMORROW)])
    result = runner.invoke(cli.main, ["plan-calendar", "--dry-run"])

    assert result.exit_code == 0
    assert calendar == []
    payload = json.loads(result.output[:result.output.rindex("]") + 1])
    assert payload[0]["start"]["date"] == TOMORROW
    assert payload[0]["end"]["date"] == (
        (Date.today() + timedelta(days=2)).isoformat())
    assert "Nothing was sent" in result.output


def test_dry_run_works_with_no_credentials_at_all(
        runner, planned, calendar, monkeypatch):
    # Same contract as `brief-email --dry-run`: inspectable BEFORE setup, which
    # is exactly when the real path refuses.
    planned([_workout(TOMORROW)])
    for var in ("LOCAL_FITNESS_GCAL_CLIENT_ID", "LOCAL_FITNESS_GCAL_CLIENT_SECRET",
                "LOCAL_FITNESS_GCAL_REFRESH_TOKEN"):
        monkeypatch.delenv(var)

    result = runner.invoke(cli.main, ["plan-calendar", "--dry-run"])
    assert result.exit_code == 0
    assert "Nothing was sent" in result.output


def test_dry_run_ignores_the_kill_switch(runner, planned, calendar, monkeypatch):
    # A disabled setup must still be inspectable, or debugging "why is nothing
    # on my calendar" starts by re-enabling the thing you're debugging.
    planned([_workout(TOMORROW)])
    monkeypatch.setenv("LOCAL_FITNESS_PLAN_CALENDAR_ENABLED", "0")

    result = runner.invoke(cli.main, ["plan-calendar", "--dry-run"])
    assert result.exit_code == 0
    assert "Nothing was sent" in result.output


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
