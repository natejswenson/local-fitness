"""Tests for the conversational configuration of the evening brief email.

Two things are under test and they carry very different weight.

The ordinary half is precedence and validation: DB > env > default, a
replacement recipient list, a loud failure on a typo.

The half that matters is
``test_no_tool_output_ever_contains_the_smtp_password``. `/mcp/` is served over
the network and reachable from a phone, so a settings tool that echoed the
credential would publish a live Gmail app password to every client that can
call it. That test asserts against the *whole serialized payload* rather than
against named fields, so a future field added to either tool is covered
automatically — which is the only version of this check worth having, since
the leak would arrive via a field nobody thought to review.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from local_fitness import config, db
from local_fitness.agent import mailer, tools

SECRET = "zzzzsecretpwzzzz"


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
    for k in ("LOCAL_FITNESS_BRIEF_EMAIL_ENABLED", "LOCAL_FITNESS_BRIEF_EMAIL_TO",
              "LOCAL_FITNESS_SMTP_HOST", "LOCAL_FITNESS_SMTP_PORT",
              "LOCAL_FITNESS_BRIEF_EMAIL_FROM"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LOCAL_FITNESS_SMTP_USER", "me@gmail.com")
    monkeypatch.setenv("LOCAL_FITNESS_SMTP_PASSWORD", SECRET)
    return p


# --- the fresh-clone contract ----------------------------------------------

@pytest.fixture
def no_db(tmp_path, monkeypatch):
    """CI's condition, and a stranger's first five minutes: no database yet."""
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "nope" / "fitness.db")
    monkeypatch.setenv("LOCAL_FITNESS_SMTP_USER", "me@gmail.com")
    monkeypatch.setenv("LOCAL_FITNESS_SMTP_PASSWORD", SECRET)
    for k in ("LOCAL_FITNESS_BRIEF_EMAIL_ENABLED", "LOCAL_FITNESS_BRIEF_EMAIL_TO"):
        monkeypatch.delenv(k, raising=False)


def test_settings_resolve_with_no_database_at_all(no_db):
    # A fresh clone has no data/fitness.db until the first `fitness pull`, and
    # db.get_setting raises "no such table: settings" against one. Every knob
    # has a working env/default layer underneath, so an unreadable DB must
    # fall through rather than crash. This shipped broken for one commit: it
    # passed locally (a populated dev DB) and took out 21 tests in CI.
    assert config.brief_email_enabled() is True
    assert config.brief_email_to() == ()


def test_the_whole_send_path_configures_with_no_database(no_db):
    # The actual user-visible failure: `fitness brief-email` on a fresh clone.
    assert mailer.load_config().to == ("me@gmail.com",)


def test_the_pre_existing_resolvers_survive_it_too(no_db):
    # The fix lives in config._resolve, so these were latently exposed as well
    # — they simply had no caller that ran before schema init.
    assert config.user_name() == config.DEFAULT_USER_NAME
    assert config.coach_profile() == "hardass"


def test_env_still_wins_over_the_default_with_no_database(no_db, monkeypatch):
    # Falling through must reach the ENV layer, not jump straight to the
    # default — otherwise a .env-configured clone silently ignores its config.
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_EMAIL_ENABLED", "false")
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_EMAIL_TO", "env@x.com")
    assert config.brief_email_enabled() is False
    assert config.brief_email_to() == ("env@x.com",)


# --- the security invariant ------------------------------------------------

def test_no_tool_output_ever_contains_the_smtp_password(cfgdb):
    # Whole-payload assertion, deliberately not field-by-field: the leak that
    # actually happens is a field nobody thought to review.
    got, _ = call(tools.get_brief_email_settings, {})
    assert SECRET not in json.dumps(got)
    assert got["password_configured"] is True

    got, _ = call(tools.update_brief_email_settings, {"enabled": False})
    assert SECRET not in json.dumps(got)

    got, _ = call(tools.update_brief_email_settings, {"to": ["a@b.com"]})
    assert SECRET not in json.dumps(got)


def test_the_password_is_not_writable_through_the_update_tool(cfgdb):
    # A credential in the settings table would sit behind a network endpoint.
    got, err = call(tools.update_brief_email_settings,
                    {"password": "hunter2", "smtp_password": "hunter2"})
    assert err
    assert "unknown field" in got["error"]
    assert db.get_setting("brief_email_password") is None
    assert db.get_setting("smtp_password") is None


def test_password_configured_reports_presence_not_value(cfgdb, monkeypatch):
    assert mailer.password_configured() is True
    monkeypatch.setenv("LOCAL_FITNESS_SMTP_PASSWORD", "   ")
    assert mailer.password_configured() is False
    monkeypatch.delenv("LOCAL_FITNESS_SMTP_PASSWORD")
    assert mailer.password_configured() is False


# --- config precedence -----------------------------------------------------

def test_enabled_defaults_to_true_on_a_fresh_clone(cfgdb):
    # Safe because sending ALSO requires a password, which has no default.
    assert config.brief_email_enabled() is True


@pytest.mark.parametrize("raw,expected", [
    ("0", False), ("false", False), ("no", False), ("off", False),
    ("1", True), ("true", True), ("yes", True), ("on", True),
])
def test_enabled_parses_every_documented_token(cfgdb, monkeypatch, raw, expected):
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_EMAIL_ENABLED", raw)
    assert config.brief_email_enabled() is expected


def test_db_setting_outranks_the_env_var(cfgdb, monkeypatch):
    # The whole point of the MCP tools: a conversational edit beats .env.
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_EMAIL_ENABLED", "true")
    db.set_setting("brief_email_enabled", "false")
    assert config.brief_email_enabled() is False


def test_a_blank_db_value_falls_through_to_env(cfgdb, monkeypatch):
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_EMAIL_ENABLED", "false")
    db.set_setting("brief_email_enabled", "   ")
    assert config.brief_email_enabled() is False


def test_an_unparseable_value_falls_back_to_the_default(cfgdb):
    # Never silently flip to False — a garbage value must not stop the mail.
    db.set_setting("brief_email_enabled", "maybe")
    assert config.brief_email_enabled() is True


def test_recipients_default_to_empty_meaning_unconfigured(cfgdb):
    assert config.brief_email_to() == ()


def test_recipients_parse_and_trim(cfgdb, monkeypatch):
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_EMAIL_TO", " a@x.com , b@x.com ,")
    assert config.brief_email_to() == ("a@x.com", "b@x.com")


def test_a_recipient_string_of_only_separators_falls_back(cfgdb):
    # "configured to mail nobody" is a silent no-send; the fallback is safer.
    db.set_setting("brief_email_to", " , , ")
    assert config.brief_email_to() == ()


# --- mailer integration ----------------------------------------------------

def test_mailer_uses_the_db_recipient_over_env(cfgdb, monkeypatch):
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_EMAIL_TO", "env@x.com")
    db.set_setting("brief_email_to", "db@x.com")
    assert mailer.load_config().to == ("db@x.com",)


def test_mailer_falls_back_to_the_sending_account(cfgdb):
    assert mailer.load_config().to == ("me@gmail.com",)


def test_an_explicit_override_beats_the_db_setting(cfgdb):
    db.set_setting("brief_email_to", "db@x.com")
    assert mailer.load_config("cli@x.com").to == ("cli@x.com",)


# --- get tool --------------------------------------------------------------

def test_get_reports_the_effective_state(cfgdb):
    db.set_setting("brief_email_to", "a@x.com,b@x.com")
    got, err = call(tools.get_brief_email_settings, {})
    assert not err
    assert got["enabled"] is True
    assert got["to"] == ["a@x.com", "b@x.com"]
    assert got["to_is_explicit"] is True
    assert got["can_send"] is True
    assert got["blocked_reason"] is None
    assert "19:00" in got["schedule"]


def test_get_names_the_fallback_recipient_as_not_explicit(cfgdb):
    got, _ = call(tools.get_brief_email_settings, {})
    assert got["to"] == ["me@gmail.com"]
    assert got["to_is_explicit"] is False


def test_can_send_is_false_and_named_when_disabled(cfgdb):
    db.set_setting("brief_email_enabled", "false")
    got, _ = call(tools.get_brief_email_settings, {})
    assert got["can_send"] is False
    assert got["blocked_reason"] == "disabled via settings"


def test_can_send_is_false_and_named_when_the_password_is_missing(cfgdb, monkeypatch):
    # "enabled but undeliverable" is the state most likely to be misreported
    # as "you're all set".
    monkeypatch.delenv("LOCAL_FITNESS_SMTP_PASSWORD")
    got, _ = call(tools.get_brief_email_settings, {})
    assert got["enabled"] is True
    assert got["can_send"] is False
    assert "LOCAL_FITNESS_SMTP_PASSWORD" in got["blocked_reason"]


def test_can_send_is_false_and_named_when_the_sender_is_missing(cfgdb, monkeypatch):
    monkeypatch.delenv("LOCAL_FITNESS_SMTP_USER")
    got, _ = call(tools.get_brief_email_settings, {})
    assert got["can_send"] is False
    assert "LOCAL_FITNESS_SMTP_USER" in got["blocked_reason"]
    assert got["smtp_user"] is None


# --- update tool -----------------------------------------------------------

def test_disabling_writes_the_setting_and_reports_the_new_state(cfgdb):
    got, err = call(tools.update_brief_email_settings, {"enabled": False})
    assert not err
    assert got["changed"] == ["enabled"]
    assert got["enabled"] is False
    assert db.get_setting("brief_email_enabled") == "false"
    # And it is visible through the read path, not just the write's echo.
    assert call(tools.get_brief_email_settings, {})[0]["enabled"] is False


def test_recipients_are_replaced_wholesale_not_appended(cfgdb):
    db.set_setting("brief_email_to", "old@x.com")
    got, err = call(tools.update_brief_email_settings, {"to": ["new@x.com"]})
    assert not err
    assert got["to"] == ["new@x.com"]
    assert config.brief_email_to() == ("new@x.com",)


def test_multiple_recipients_round_trip(cfgdb):
    got, _ = call(tools.update_brief_email_settings,
                  {"to": ["a@x.com", "b@y.com"]})
    assert got["to"] == ["a@x.com", "b@y.com"]
    assert config.brief_email_to() == ("a@x.com", "b@y.com")


def test_a_bare_comma_separated_string_is_accepted(cfgdb):
    # The shape a model reaches for when there is one address.
    got, err = call(tools.update_brief_email_settings, {"to": "a@x.com, b@x.com"})
    assert not err
    assert got["to"] == ["a@x.com", "b@x.com"]


def test_both_fields_can_change_in_one_call(cfgdb):
    got, _ = call(tools.update_brief_email_settings,
                  {"enabled": False, "to": ["a@x.com"]})
    assert sorted(got["changed"]) == ["enabled", "to"]


def test_every_write_reports_whether_it_can_actually_send(cfgdb, monkeypatch):
    # "I turned it on" must never be the last word when a credential is missing.
    monkeypatch.delenv("LOCAL_FITNESS_SMTP_PASSWORD")
    got, _ = call(tools.update_brief_email_settings, {"enabled": True})
    assert got["password_configured"] is False


@pytest.mark.parametrize("bad", ["notanemail", "no@tld", "a b@x.com", "@x.com", ""])
def test_a_malformed_address_is_rejected_and_nothing_is_written(cfgdb, bad):
    got, err = call(tools.update_brief_email_settings, {"to": ["good@x.com", bad]})
    assert err
    assert "not a valid email" in got["error"] or "non-empty list" in got["error"]
    # All-or-nothing: the valid address in the same call must not land either.
    assert db.get_setting("brief_email_to") is None


def test_a_non_bool_enabled_is_rejected(cfgdb):
    got, err = call(tools.update_brief_email_settings, {"enabled": "yes"})
    assert err
    assert "must be true or false" in got["error"]
    assert db.get_setting("brief_email_enabled") is None


def test_an_unknown_field_fails_loudly_rather_than_being_ignored(cfgdb):
    got, err = call(tools.update_brief_email_settings,
                    {"enabled": False, "hour": 20})
    assert err
    assert "unknown field 'hour'" in got["error"]
    assert db.get_setting("brief_email_enabled") is None


def test_an_empty_call_is_an_error_not_a_silent_noop(cfgdb):
    got, err = call(tools.update_brief_email_settings, {})
    assert err
    assert "nothing to update" in got["error"]


def test_an_empty_recipient_list_is_rejected(cfgdb):
    got, err = call(tools.update_brief_email_settings, {"to": []})
    assert err
    assert "non-empty list" in got["error"]


# --- registry --------------------------------------------------------------

def test_both_tools_are_reachable_over_http_not_just_stdio(cfgdb):
    # Configuration from a phone is the point; LOCAL_ONLY_TOOLS would defeat it.
    names = {t.name for t in tools.ALL_TOOLS}
    assert {"get_brief_email_settings", "update_brief_email_settings"} <= names
    local_only = {t.name for t in tools.LOCAL_ONLY_TOOLS}
    assert not (local_only & {"get_brief_email_settings", "update_brief_email_settings"})
