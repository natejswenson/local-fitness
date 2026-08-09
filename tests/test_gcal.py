"""The Google Calendar client.

Every test here drives a fake transport. ``tests/conftest.py`` blocks
``gcal._request`` for the whole suite (a live call would CREATE something in a
real account), so a test that wants the transport patches that one function —
which is exactly why the module funnels everything through it.
"""
from __future__ import annotations

import inspect

import pytest

from local_fitness import config, db
from local_fitness.agent import gcal

#: The REAL ``_request``, captured at import time — before conftest's autouse
#: guard replaces the module attribute for every test. Without this the two
#: transport-security tests below would inspect the *guard* instead of the
#: function they exist to check, and pass for the wrong reason.
REAL_REQUEST = gcal._request

CFG = gcal.GcalConfig(
    client_id="cid", client_secret="csecret",
    refresh_token="rtoken", calendar_id="primary",
)

EVENT = {
    "id": "lfplandeadbeef",
    "summary": "Easy run 4.0 mi @ 10:28/mi",
    "description": "Recovery 4mi.\n\nlocal-fitness · plan #4",
    "start": {"date": "2026-08-09"},
    "end": {"date": "2026-08-10"},
    "transparency": "transparent",
}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeTransport:
    """Records every request and replays a queued response per call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, *, headers=None, data=None,
                 json_body=None, params=None):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "data": data, "json": json_body, "params": params})
        return self.responses.pop(0)


@pytest.fixture
def transport(monkeypatch):
    def install(*responses):
        t = FakeTransport(*responses)
        monkeypatch.setattr(gcal, "_request", t)
        return t
    return install


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("LOCAL_FITNESS_GCAL_CLIENT_ID", "cid")
    monkeypatch.setenv("LOCAL_FITNESS_GCAL_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("LOCAL_FITNESS_GCAL_REFRESH_TOKEN", "rtoken")
    monkeypatch.delenv("LOCAL_FITNESS_PLAN_CALENDAR_ID", raising=False)


# --- configuration ---------------------------------------------------------

def test_load_config_resolves_all_three_credentials(creds, monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "fitness.db")
    cfg = gcal.load_config()
    assert (cfg.client_id, cfg.client_secret, cfg.refresh_token) == (
        "cid", "csecret", "rtoken")
    assert cfg.calendar_id == "primary"


@pytest.mark.parametrize("missing", [
    "LOCAL_FITNESS_GCAL_CLIENT_ID",
    "LOCAL_FITNESS_GCAL_CLIENT_SECRET",
    "LOCAL_FITNESS_GCAL_REFRESH_TOKEN",
])
def test_a_missing_credential_names_itself(creds, monkeypatch, missing):
    # This runs from launchd where a log line is the only diagnostic, so
    # "something isn't set" is not an error message.
    monkeypatch.delenv(missing)
    with pytest.raises(gcal.CalendarNotConfigured) as e:
        gcal.load_config()
    assert missing in str(e.value)


def test_a_whitespace_only_credential_reads_as_missing(creds, monkeypatch):
    # The shape a half-finished `.env` actually takes.
    monkeypatch.setenv("LOCAL_FITNESS_GCAL_REFRESH_TOKEN", "   ")
    with pytest.raises(gcal.CalendarNotConfigured):
        gcal.load_config()
    assert gcal.credentials_configured() is False


def test_credentials_configured_is_true_only_when_all_three_are_present(
        creds, monkeypatch):
    assert gcal.credentials_configured() is True
    monkeypatch.delenv("LOCAL_FITNESS_GCAL_CLIENT_SECRET")
    assert gcal.credentials_configured() is False


def test_an_explicit_calendar_id_overrides_the_setting(creds, monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "fitness.db")
    assert gcal.load_config("team@group.calendar.google.com").calendar_id == (
        "team@group.calendar.google.com")


def test_the_scope_is_events_only():
    # Least privilege: the broader `.../auth/calendar` scope would also grant
    # calendar creation and deletion, and narrowing it later forces re-consent.
    assert gcal.SCOPE.endswith("/auth/calendar.events")


# --- token refresh ---------------------------------------------------------

def test_access_token_posts_the_refresh_grant(transport):
    t = transport(FakeResponse(200, {"access_token": "at-123"}))
    assert gcal.access_token(CFG) == "at-123"

    (call,) = t.calls
    assert call["method"] == "POST"
    assert call["url"] == gcal.TOKEN_URL
    assert call["data"] == {
        "client_id": "cid", "client_secret": "csecret",
        "refresh_token": "rtoken", "grant_type": "refresh_token",
    }


def test_an_invalid_grant_names_the_testing_mode_trap(transport):
    # By far the likeliest failure, and the cause is almost never the token:
    # a Testing-mode OAuth app has its refresh tokens expired every 7 days, so
    # the job dies weekly and looks like a new bug each time.
    transport(FakeResponse(400, {"error": "invalid_grant",
                                 "error_description": "Token has been expired"}))
    with pytest.raises(gcal.CalendarAuthError) as e:
        gcal.access_token(CFG)
    message = str(e.value)
    assert "invalid_grant" not in message or "Token has been expired" in message
    assert "In production" in message
    assert "7 days" in message
    assert "calendar-auth" in message


def test_a_200_with_no_access_token_is_still_an_error(transport):
    transport(FakeResponse(200, {}))
    with pytest.raises(gcal.CalendarAuthError):
        gcal.access_token(CFG)


def test_a_non_json_token_response_does_not_raise_valueerror(transport):
    transport(FakeResponse(500, None, text="<html>502 Bad Gateway</html>"))
    with pytest.raises(gcal.CalendarAuthError) as e:
        gcal.access_token(CFG)
    assert "500" in str(e.value)


# --- upsert ----------------------------------------------------------------

def test_a_new_event_is_created_with_one_request(transport):
    t = transport(FakeResponse(200, {"htmlLink": "https://cal/x"}))
    got = gcal.upsert_event(EVENT, CFG, "at-123")

    assert got == {"action": "created", "id": EVENT["id"],
                   "html_link": "https://cal/x"}
    (call,) = t.calls
    assert call["method"] == "POST"
    assert call["url"].endswith("/calendars/primary/events")
    assert call["json"] is EVENT
    assert call["headers"] == {"Authorization": "Bearer at-123"}


def test_a_calendar_id_with_an_at_sign_is_percent_encoded(transport):
    # A calendar id is usually an address; an unencoded '@' would still work
    # but a '/' in a custom id would silently relocate the path segment.
    cfg = gcal.GcalConfig("cid", "csecret", "rtoken", "team@group.calendar.google.com")
    t = transport(FakeResponse(200, {"htmlLink": "https://cal/x"}))
    gcal.upsert_event(EVENT, cfg, "at-123")
    assert "team%40group.calendar.google.com" in t.calls[0]["url"]


def test_an_unchanged_event_is_read_but_never_rewritten(transport):
    # THE backstop case: 20:05 re-posts the same id and must not write.
    t = transport(
        FakeResponse(409, {"error": {"message": "duplicate"}}),
        FakeResponse(200, {**EVENT, "status": "confirmed",
                           "htmlLink": "https://cal/x"}),
    )
    got = gcal.upsert_event(EVENT, CFG, "at-123")

    assert got["action"] == "unchanged"
    assert [c["method"] for c in t.calls] == ["POST", "GET"]


def test_a_changed_prescription_updates_the_same_event_in_place(transport):
    # What a marker file could not do: the plan changed between 19:05 and
    # 20:05, so the backstop must update rather than skip as already-done.
    stale = {**EVENT, "summary": "Easy run 3.0 mi", "status": "confirmed"}
    t = transport(
        FakeResponse(409, {"error": {"message": "duplicate"}}),
        FakeResponse(200, stale),
        FakeResponse(200, {"htmlLink": "https://cal/updated"}),
    )
    got = gcal.upsert_event(EVENT, CFG, "at-123")

    assert got == {"action": "updated", "id": EVENT["id"],
                   "html_link": "https://cal/updated"}
    assert [c["method"] for c in t.calls] == ["POST", "GET", "PUT"]
    assert t.calls[2]["url"].endswith(f"/events/{EVENT['id']}")
    assert t.calls[2]["json"] is EVENT


@pytest.mark.parametrize("field,changed", [
    ("summary", "Long run 9.0 mi"),
    ("description", "different prose"),
    ("transparency", "opaque"),
])
def test_every_compared_field_triggers_an_update(transport, field, changed):
    t = transport(
        FakeResponse(409, {}),
        FakeResponse(200, {**EVENT, field: changed, "status": "confirmed"}),
        FakeResponse(200, {}),
    )
    assert gcal.upsert_event(EVENT, CFG, "at-123")["action"] == "updated"
    assert len(t.calls) == 3


@pytest.mark.parametrize("side", ["start", "end"])
def test_a_moved_date_triggers_an_update(transport, side):
    t = transport(
        FakeResponse(409, {}),
        FakeResponse(200, {**EVENT, side: {"date": "2027-01-01"},
                           "status": "confirmed"}),
        FakeResponse(200, {}),
    )
    assert gcal.upsert_event(EVENT, CFG, "at-123")["action"] == "updated"
    assert len(t.calls) == 3


def test_a_deleted_event_is_never_resurrected(transport):
    # Deleting the event is how a person says "not this one". A job that puts
    # it back an hour later is a job that gets uninstalled — so a `cancelled`
    # tombstone means STOP, and nothing may be written.
    t = transport(
        FakeResponse(409, {"error": {"message": "duplicate"}}),
        FakeResponse(200, {**EVENT, "status": "cancelled"}),
    )
    got = gcal.upsert_event(EVENT, CFG, "at-123")

    assert got["action"] == "skipped_cancelled"
    assert [c["method"] for c in t.calls] == ["POST", "GET"]
    assert not any(c["method"] == "PUT" for c in t.calls)


def test_an_unexpected_insert_status_raises(transport):
    transport(FakeResponse(403, {"error": {"message": "insufficient scope"}}))
    with pytest.raises(gcal.CalendarApiError) as e:
        gcal.upsert_event(EVENT, CFG, "at-123")
    assert "403" in str(e.value)


def test_a_409_whose_event_cannot_be_read_raises_rather_than_guessing(transport):
    # Neither "create" nor "leave alone" is safe here, so it fails loudly.
    transport(FakeResponse(409, {}), FakeResponse(404, {}))
    with pytest.raises(gcal.CalendarApiError) as e:
        gcal.upsert_event(EVENT, CFG, "at-123")
    assert "409" in str(e.value)


def test_a_failed_update_raises(transport):
    transport(
        FakeResponse(409, {}),
        FakeResponse(200, {**EVENT, "summary": "old", "status": "confirmed"}),
        FakeResponse(500, {}, text="boom"),
    )
    with pytest.raises(gcal.CalendarApiError) as e:
        gcal.upsert_event(EVENT, CFG, "at-123")
    assert "500" in str(e.value)


# --- transport security ----------------------------------------------------

def test_the_request_helper_never_disables_tls_verification():
    # 0.51.0's smtplib lesson, generalised: "it sent" passes happily while the
    # transport authenticates nothing, so the assertion is on the ARGUMENTS,
    # not the outcome. `requests` verifies by default; the way this goes wrong
    # is someone adding verify=False to silence a proxy error, so the rule is
    # that this call site has no such argument at all.
    # Docstring stripped: it *explains* the rule, so scanning it would make the
    # explanation fail the test it documents.
    body = inspect.getsource(REAL_REQUEST).replace(REAL_REQUEST.__doc__, "")
    assert "verify" not in body


def test_every_outbound_call_goes_through_the_one_choke_point():
    # What makes the conftest guard total. A module that called `requests`
    # directly somewhere else would be unblockable, and a test could reach a
    # real calendar.
    source = inspect.getsource(gcal)
    assert source.count("import requests") == 1
    assert "requests.request(" in source
    # Every other requests.* entry point (get/post/put/Session) would bypass it.
    for bypass in ("requests.get(", "requests.post(", "requests.put(",
                   "requests.Session("):
        assert bypass not in source, bypass


def test_the_transport_is_https_only():
    for url in (gcal.TOKEN_URL, gcal.AUTH_URL, gcal.CALENDAR_API):
        assert url.startswith("https://"), url


def test_a_request_carries_a_timeout(monkeypatch):
    # Unattended behind a backstop slot: a hung socket has to fail and let the
    # retry take it rather than pin the process until launchd's own timeout.
    seen = {}

    class FakeRequests:
        @staticmethod
        def request(method, url, **kwargs):
            seen.update(kwargs)
            return FakeResponse(200, {})

    monkeypatch.setitem(__import__("sys").modules, "requests", FakeRequests)
    REAL_REQUEST("GET", "https://example.test")
    assert seen["timeout"] == gcal.HTTP_TIMEOUT_S
    assert "verify" not in seen


# --- the consent flow ------------------------------------------------------

def test_the_authorization_url_asks_for_a_refresh_token():
    # access_type=offline AND prompt=consent together are what make Google
    # return a REFRESH token; offline alone is silently ignored on a
    # re-consent, and the flow then looks broken for no visible reason.
    url = gcal.authorization_url("cid", "http://127.0.0.1:5000", "st8", "chal")
    assert url.startswith(gcal.AUTH_URL + "?")
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "response_type=code" in url
    assert "code_challenge=chal" in url
    assert "code_challenge_method=S256" in url
    assert "state=st8" in url
    assert "calendar.events" in url


def test_exchange_code_returns_the_refresh_token(transport):
    t = transport(FakeResponse(200, {"refresh_token": "1//new",
                                     "access_token": "at"}))
    assert gcal.exchange_code("cid", "sec", "code-1", "http://127.0.0.1:1", "ver") == "1//new"
    assert t.calls[0]["data"]["grant_type"] == "authorization_code"
    assert t.calls[0]["data"]["code_verifier"] == "ver"


def test_exchange_code_explains_a_missing_refresh_token(transport):
    # Google reuses an existing grant and returns only an access token when the
    # account already approved this client — the fix is to revoke, not retry.
    transport(FakeResponse(200, {"access_token": "at"}))
    with pytest.raises(gcal.CalendarAuthError) as e:
        gcal.exchange_code("cid", "sec", "c", "http://127.0.0.1:1", "v")
    assert "revoke" in str(e.value)


def test_exchange_code_raises_on_a_rejected_code(transport):
    transport(FakeResponse(400, {"error": "invalid_request"}))
    with pytest.raises(gcal.CalendarAuthError):
        gcal.exchange_code("cid", "sec", "c", "http://127.0.0.1:1", "v")


# --- the settings layer ----------------------------------------------------

def test_the_calendar_id_setting_resolves_db_over_env_over_default(
        tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)

    monkeypatch.delenv("LOCAL_FITNESS_PLAN_CALENDAR_ID", raising=False)
    assert config.plan_calendar_id() == "primary"

    monkeypatch.setenv("LOCAL_FITNESS_PLAN_CALENDAR_ID", "env@group.calendar.google.com")
    assert config.plan_calendar_id() == "env@group.calendar.google.com"

    db.set_setting("plan_calendar_id", "db@group.calendar.google.com")
    assert config.plan_calendar_id() == "db@group.calendar.google.com"


def test_a_blank_calendar_id_falls_through_to_the_default(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    monkeypatch.setenv("LOCAL_FITNESS_PLAN_CALENDAR_ID", "   ")
    assert config.plan_calendar_id() == "primary"


def test_the_enabled_setting_defaults_on_and_parses_both_layers(
        tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)

    monkeypatch.delenv("LOCAL_FITNESS_PLAN_CALENDAR_ENABLED", raising=False)
    assert config.plan_calendar_enabled() is True

    monkeypatch.setenv("LOCAL_FITNESS_PLAN_CALENDAR_ENABLED", "0")
    assert config.plan_calendar_enabled() is False

    db.set_setting("plan_calendar_enabled", "true")
    assert config.plan_calendar_enabled() is True
