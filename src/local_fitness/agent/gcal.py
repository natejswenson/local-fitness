"""Google Calendar delivery for the next day's prescribed session.

The I/O half of the calendar path — ``calendar_render`` is the pure half, the
same divider ``mailer``/``email_render`` uses.

**Why this is a raw REST client and not the Google Calendar MCP connector.**
0.51.0 settled the two questions to ask before routing a *scheduled* delivery
through a connector: does it actually send, and do the bytes have to pass
through a model turn? The Calendar connector passes both — ``create_event``
really creates, and an event body is a few hundred bytes. It fails a third test
that generalises further: **a connector is authenticated interactively in a
chat client and is structurally absent from a launchd Python process.** The job
that runs at 19:05 has no model in it, and a job with no model cannot call a
model-mediated tool at all. Any future scheduled integration hits the same wall,
so the question to ask first is "will the thing that runs this be a model?".

Deliberately dependency-light: the refresh-token grant is a plain form POST and
an event insert is a JSON POST, so this needs ``requests`` and nothing else — no
``google-api-python-client``, no ``google-auth``. That is ~40 lines of protocol
against two vendored dependency trees.

Secrets live only in ``<repo>/.env``. Nothing here is readable through an MCP
tool: ``/mcp/`` is served over the network and reachable from a phone, so a tool
that echoed the refresh token would hand every client that can call the endpoint
write access to the user's calendar. ``credentials_configured()`` answers the
only question a configuration reader legitimately has.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from urllib.parse import quote

from .. import config

LOG = logging.getLogger(__name__)

#: Google's OAuth 2.0 token endpoint (refresh-token and authorization-code
#: grants both post here).
TOKEN_URL = "https://oauth2.googleapis.com/token"
#: The consent screen the one-time `fitness calendar-auth` flow opens.
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"

#: Least privilege: create/edit EVENTS only. The broader `.../auth/calendar`
#: scope would also grant calendar creation and deletion, and nothing here
#: needs either. Narrowing it later would force a re-consent, so it is worth
#: getting right the first time.
SCOPE = "https://www.googleapis.com/auth/calendar.events"

#: The job runs unattended behind a backstop slot, so a hung socket has to fail
#: and let the retry take it rather than pin the process.
HTTP_TIMEOUT_S = 30


class CalendarNotConfigured(RuntimeError):
    """A required credential or setting is missing.

    Distinct from an API failure on purpose, exactly as ``mailer``'s is: this
    one is fixed by putting a value in ``.env`` and is never worth retrying, so
    the CLI reports it differently (and exits 2) rather than treating it as a
    transient outage.
    """


class CalendarAuthError(RuntimeError):
    """Google refused the refresh token — re-consent is needed."""


class CalendarApiError(RuntimeError):
    """The Calendar API returned an unexpected status."""


@dataclass(frozen=True)
class GcalConfig:
    client_id: str
    client_secret: str
    refresh_token: str
    calendar_id: str


def _env(name: str, default: str | None = None) -> str | None:
    """Read an env var, treating whitespace-only as unset — the shape a
    half-finished ``.env`` actually takes (``mailer._env``'s reasoning)."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip()


#: Every credential this needs, paired with the sentence that fixes it. Kept as
#: data so `load_config` and `credentials_configured` can never disagree about
#: what "configured" means.
_REQUIRED = (
    ("LOCAL_FITNESS_GCAL_CLIENT_ID",
     "the OAuth client id from your Google Cloud project"),
    ("LOCAL_FITNESS_GCAL_CLIENT_SECRET",
     "the OAuth client secret from the same client"),
    ("LOCAL_FITNESS_GCAL_REFRESH_TOKEN",
     "run `uv run fitness calendar-auth` once and paste the token it prints"),
)


def credentials_configured() -> bool:
    """Whether all three OAuth values are present — WITHOUT exposing any.

    The only thing the MCP surface may learn about the credential, mirroring
    ``mailer.password_configured``.
    """
    return all(_env(name) for name, _ in _REQUIRED)


def load_config(calendar_id: str | None = None) -> GcalConfig:
    """Resolve OAuth credentials + target calendar.

    Raises ``CalendarNotConfigured`` naming the exact missing variable and what
    to do about it: this runs from launchd where a log line is the only
    diagnostic, so "something isn't set" is not an error message.
    """
    values = {}
    for name, remedy in _REQUIRED:
        got = _env(name)
        if not got:
            raise CalendarNotConfigured(
                f"{name} is not set — {remedy}. Put it in <repo>/.env "
                "(never in .env.example, which is tracked). Setup steps: "
                "docs/google-calendar.md"
            )
        values[name] = got

    return GcalConfig(
        client_id=values["LOCAL_FITNESS_GCAL_CLIENT_ID"],
        client_secret=values["LOCAL_FITNESS_GCAL_CLIENT_SECRET"],
        refresh_token=values["LOCAL_FITNESS_GCAL_REFRESH_TOKEN"],
        calendar_id=calendar_id or config.plan_calendar_id(),
    )


def _request(method: str, url: str, *, headers=None, data=None,
             json_body=None, params=None):
    """The single HTTP choke point for this module.

    Everything funnels through here for two reasons: the test suite blocks ONE
    function to guarantee no test can reach Google (the same shape as the
    Claude-SDK, Garmin and SMTP guards in ``tests/conftest.py``), and TLS
    verification is configured in exactly one place.

    ``verify`` is never passed. ``requests`` verifies certificates by default,
    and the way this goes wrong is someone adding ``verify=False`` to silence a
    corporate-proxy error — so the rule is that this call site has no such
    argument at all, and ``tests/test_gcal.py`` asserts on the arguments rather
    than on whether the call succeeded (0.51.0's ``smtplib`` lesson: "it sent"
    passes happily while the transport is unauthenticated).
    """
    import requests

    return requests.request(
        method, url, headers=headers, data=data, json=json_body,
        params=params, timeout=HTTP_TIMEOUT_S,
    )


def _body(resp) -> dict:
    """Parse a JSON response, tolerating an empty or non-JSON one."""
    try:
        parsed = resp.json()
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def access_token(cfg: GcalConfig) -> str:
    """Exchange the long-lived refresh token for a ~1h access token."""
    resp = _request(
        "POST", TOKEN_URL,
        data={
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "refresh_token": cfg.refresh_token,
            "grant_type": "refresh_token",
        },
    )
    payload = _body(resp)
    if resp.status_code != 200 or not payload.get("access_token"):
        detail = payload.get("error_description") or payload.get("error") or ""
        hint = ""
        if payload.get("error") == "invalid_grant":
            # By far the likeliest failure, and the cause is almost never the
            # token itself: an OAuth app left in "Testing" publishing status
            # has its refresh tokens expired by Google after 7 days, so this
            # job dies every week and looks like a different bug each time.
            hint = (
                " — the refresh token was revoked or expired. If your OAuth "
                "consent screen is still in 'Testing', set it to 'In "
                "production': Google expires Testing-mode refresh tokens after "
                "7 days. Then re-run `uv run fitness calendar-auth`."
            )
        raise CalendarAuthError(
            f"Google rejected the refresh token (HTTP {resp.status_code}) "
            f"{detail}{hint}"
        )
    return payload["access_token"]


def _events_url(cfg: GcalConfig) -> str:
    # quote(safe="") because a calendar id is usually an email address and the
    # '@' must be percent-encoded to stay inside one path segment.
    return f"{CALENDAR_API}/calendars/{quote(cfg.calendar_id, safe='')}/events"


def _matches(existing: dict, event: dict) -> bool:
    """Whether the event already on the calendar says what the plan now says.

    Delegates to ``calendar_render._differs`` rather than keeping a second
    field list. The comparison decides "do we write?" in two places — here, on
    the 409 path, and in ``reconcile`` — and two definitions of "the same
    event" would drift into one path rewriting what the other calls unchanged.
    """
    from . import calendar_render

    return not calendar_render._differs(existing, event)


def upsert_event(event: dict, cfg: GcalConfig, token: str) -> dict:
    """Create the event, or bring an existing one in line with the plan.

    Returns ``{"action": ..., "id": ..., "html_link": ...}`` where action is
    ``created`` / ``updated`` / ``unchanged`` / ``skipped_cancelled``.

    The whole design rests on ``calendar_render.event_id`` being deterministic,
    which is what makes this idempotent WITHOUT a marker file: the 20:05
    backstop re-posts the same id, gets a 409, sees identical content and does
    nothing. That is why this command has no ``--if-unsent`` equivalent — the
    dedupe is a property of the data rather than a file that can drift from it.

    ``skipped_cancelled`` is the one branch worth arguing about. A 409 on a
    DELETED event is not "already there" — Google keeps a tombstone, and a
    blind PUT would resurrect it. Deleting the event is how a person says "not
    this one", and a job that puts it back every hour is a job that gets
    uninstalled. So a cancelled tombstone is left exactly as it is.
    """
    url = _events_url(cfg)
    headers = {"Authorization": f"Bearer {token}"}

    resp = _request("POST", url, headers=headers, json_body=event)
    if resp.status_code in (200, 201):
        created = _body(resp)
        LOG.info("plan_calendar created id=%s summary=%r",
                 event["id"], event.get("summary"))
        return {"action": "created", "id": event["id"],
                "html_link": created.get("htmlLink")}

    if resp.status_code != 409:
        raise CalendarApiError(
            f"Calendar insert failed (HTTP {resp.status_code}): "
            f"{_body(resp).get('error', resp.text[:300])}"
        )

    # 409 == an event with this id already exists (or once did).
    one = f"{url}/{event['id']}"
    got = _request("GET", one, headers=headers)
    if got.status_code != 200:
        raise CalendarApiError(
            f"Calendar insert returned 409 but the event could not be read "
            f"(HTTP {got.status_code}): {_body(got).get('error', got.text[:300])}"
        )
    existing = _body(got)

    if existing.get("status") == "cancelled":
        LOG.info("plan_calendar skipped id=%s — deleted on the calendar",
                 event["id"])
        return {"action": "skipped_cancelled", "id": event["id"],
                "html_link": existing.get("htmlLink")}

    if _matches(existing, event):
        return {"action": "unchanged", "id": event["id"],
                "html_link": existing.get("htmlLink")}

    put = _request("PUT", one, headers=headers, json_body=event)
    if put.status_code != 200:
        raise CalendarApiError(
            f"Calendar update failed (HTTP {put.status_code}): "
            f"{_body(put).get('error', put.text[:300])}"
        )
    LOG.info("plan_calendar updated id=%s summary=%r",
             event["id"], event.get("summary"))
    return {"action": "updated", "id": event["id"],
            "html_link": _body(put).get("htmlLink")}


#: Page size for the events listing. Google caps this at 2500; a plan is at
#: most `calendar_render.MAX_SYNC_EVENTS` (200) events, so one page covers
#: every real case and the pagination loop below is the safety net rather than
#: the normal path.
LIST_PAGE_SIZE = 250

#: Refuses to page forever. `maxResults` × this is far above any real plan, so
#: reaching it means a pageToken loop, not a big calendar.
MAX_LIST_PAGES = 10


def list_plan_events(cfg: GcalConfig, token: str, plan_id: int) -> list[dict]:
    """Every event WE wrote for one plan, tombstones included.

    Two query parameters carry the whole safety argument:

    * ``privateExtendedProperty`` filters to events tagged by
      ``calendar_render.build_event`` — ``source=local-fitness`` AND
      ``planId=<id>``. Google ANDs repeated values, so this returns our events
      for this plan and nothing else. **The caller deletes from this list**, so
      an unscoped listing would put someone's dentist appointment in the delete
      set. Scoping by ``planId`` is also what stops a sync from reaching back
      and rewriting a previous plan's events.
    * ``showDeleted=true`` is load-bearing in the other direction: a deleted
      event is a *tombstone* that still owns its id, and the reconcile has to
      SEE it to leave it alone. Without this flag a manually-deleted event
      looks absent, the sync creates it again, and the one thing a person can
      do to say "not this one" stops working.
    """
    url = _events_url(cfg)
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "privateExtendedProperty": [
            f"source={calendar_render_source_tag()}",
            f"planId={plan_id}",
        ],
        "showDeleted": "true",
        "maxResults": LIST_PAGE_SIZE,
        "singleEvents": "true",
    }

    out: list[dict] = []
    page_token = None
    for _ in range(MAX_LIST_PAGES):
        query = dict(params)
        if page_token:
            query["pageToken"] = page_token
        resp = _request("GET", url, headers=headers, params=query)
        if resp.status_code != 200:
            raise CalendarApiError(
                f"Calendar list failed (HTTP {resp.status_code}): "
                f"{_body(resp).get('error', resp.text[:300])}"
            )
        payload = _body(resp)
        out.extend(payload.get("items") or [])
        page_token = payload.get("nextPageToken")
        if not page_token:
            return out
    raise CalendarApiError(
        f"Calendar list did not terminate after {MAX_LIST_PAGES} pages")


def calendar_render_source_tag() -> str:
    """``calendar_render.SOURCE_TAG``, imported lazily.

    A function rather than a module-scope import because ``calendar_render``
    is the pure layer and this is the transport: keeping the dependency inside
    the call means the pure module stays importable on its own, which is what
    lets its tests run with no transport in the picture at all.
    """
    from . import calendar_render

    return calendar_render.SOURCE_TAG


def delete_event(event_id: str, cfg: GcalConfig, token: str) -> dict:
    """Remove one event. Idempotent: an already-gone event is a success.

    Google answers 410 for an event it has already deleted and 404 for one it
    never had; both mean "the calendar is in the state you asked for", so
    neither is an error. Treating them as failures would make the nightly
    reconcile noisy exactly when it had nothing to do — and would turn a
    half-finished sync into a run that can never complete.
    """
    resp = _request(
        "DELETE", f"{_events_url(cfg)}/{event_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    if resp.status_code in (200, 204, 404, 410):
        LOG.info("plan_calendar deleted id=%s status=%s",
                 event_id, resp.status_code)
        return {"action": "deleted", "id": event_id,
                "already_gone": resp.status_code in (404, 410)}
    raise CalendarApiError(
        f"Calendar delete failed (HTTP {resp.status_code}): "
        f"{_body(resp).get('error', resp.text[:300])}"
    )


# --- one-time consent (drives `fitness calendar-auth`) ---------------------

def authorization_url(client_id: str, redirect_uri: str, state: str,
                      code_challenge: str) -> str:
    """The consent-screen URL for the installed-app (loopback) flow.

    ``access_type=offline`` + ``prompt=consent`` together are what make Google
    return a REFRESH token: offline alone is silently ignored on a re-consent
    for an app the user already approved, so a second run of the setup would
    hand back an access token only and the whole flow would look broken.
    """
    from urllib.parse import urlencode

    return AUTH_URL + "?" + urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    })


def exchange_code(client_id: str, client_secret: str, code: str,
                  redirect_uri: str, code_verifier: str) -> str:
    """Authorization code → refresh token. Raises if Google returns none."""
    resp = _request(
        "POST", TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
    )
    payload = _body(resp)
    if resp.status_code != 200:
        detail = payload.get("error_description") or payload.get("error") or ""
        raise CalendarAuthError(
            f"Code exchange failed (HTTP {resp.status_code}) {detail}")
    token = payload.get("refresh_token")
    if not token:
        raise CalendarAuthError(
            "Google returned no refresh token. This happens when the account "
            "has already granted this client and Google reuses the existing "
            "grant — revoke it at myaccount.google.com/permissions and re-run."
        )
    return token
