"""Shared test setup.

``scripts/`` is a folder of standalone scripts, not a package, so we put it on
``sys.path`` and import each script by its stem (e.g. ``import score_prompt``).

``tests/evals/`` holds the fixture BUILDERS (``eval_fixtures`` for the brief,
``report_cards`` for the report card) alongside the eval tests themselves.
pytest only puts that directory on the path while collecting a module inside
it, so a test living elsewhere — ``tests/test_calibrate_report_card.py`` reuses
the report-card scenarios rather than fabricating a second corpus that could
drift from them — cannot import the builder without this.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _extra in (_ROOT / "scripts", _ROOT / "tests" / "evals"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))
SCRIPTS = _ROOT / "scripts"


@pytest.fixture(autouse=True)
def _no_live_sdk_calls(monkeypatch):
    """Hard-block live Claude Agent SDK calls for the whole suite.

    Added after `workout_report_card` grew a coaching read: unlike the brief's
    plan section (which only calls out when a plan has a workout for today),
    every report-card render generates one, so the report-card tests silently
    started making real network calls — the suite went from 10 seconds to 7
    minutes and cost real money, while still passing.

    Patching `claude_agent_sdk.query` rather than each caller is deliberate:
    it's the single choke point every generator funnels through
    (`plan_coach`, `workout_coach`, `briefing`), so a future module gets this
    protection without anyone remembering to wire it up. Each of those
    imports `query` inside the function body at call time, which is why
    patching the module attribute here reaches them.

    Callers are all built to degrade — the raise surfaces as a deterministic
    template fallback, so tests exercise the offline path by default. A test
    that wants specific generated text patches its module's generate function
    directly, which takes precedence over this.
    """
    import claude_agent_sdk

    def _blocked(*args, **kwargs):
        raise RuntimeError(
            "Live Claude Agent SDK call attempted in a test. Patch the "
            "generating function (e.g. workout_coach.generate_read_cached) "
            "in your test instead."
        )

    monkeypatch.setattr(claude_agent_sdk, "query", _blocked)


@pytest.fixture(autouse=True)
def _no_live_garmin_calls(monkeypatch):
    """Hard-block live Garmin API calls for the whole suite.

    Same lesson as the SDK guard above, learned the same way: the report card's
    PDF path resolves an HR trace, so a test that merely rendered a PDF started
    hitting Garmin's activity-details endpoint for a fixture activity id. It
    surfaced only as a 404 in the logs of an otherwise-passing test.

    Returns "no samples" rather than raising, because that IS the module's
    documented offline behavior — the card falls back to its per-lap chart, so
    the default test path exercises the degraded branch. A test that wants a
    trace patches `details.fetch_hr_samples` itself, which runs after this and
    therefore wins.
    """
    from local_fitness.ingest import details

    monkeypatch.setattr(details, "fetch_hr_samples", lambda *a, **k: [])


@pytest.fixture(autouse=True)
def _no_live_smtp_calls(monkeypatch):
    """Hard-block real mail sending for the whole suite.

    Third guard, same lesson as the SDK and Garmin ones (0.51.0): the evening
    brief path ends in ``smtplib``, and a test that walked far enough down
    ``cli.brief_email`` would connect to Gmail and deliver actual mail — a
    failure mode that is worse than slow, because it is not undoable and it
    lands in a real inbox.

    Raises rather than no-opping: unlike a missing HR trace, "the mail didn't
    send" is not a documented degraded path any caller handles, so a test that
    trips this has a wiring bug worth surfacing. A test that needs to exercise
    the transport patches ``mailer.smtplib`` itself, which runs after this and
    therefore wins.
    """
    import smtplib

    def _blocked(*args, **kwargs):
        raise RuntimeError(
            "Live SMTP connection attempted in a test. Patch "
            "mailer.smtplib.SMTP/SMTP_SSL in your test instead."
        )

    monkeypatch.setattr(smtplib, "SMTP", _blocked)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _blocked)


@pytest.fixture(autouse=True)
def _no_ambient_calendar_credentials(monkeypatch):
    """Strip `LOCAL_FITNESS_GCAL_*` from the environment for every test.

    Importing ``local_fitness.cli`` runs ``load_dotenv()`` at MODULE SCOPE, so
    the moment any test file imports it the developer's real ``<repo>/.env`` is
    merged into ``os.environ`` for the rest of the pytest process. Once that
    happens ``calendar_sync.blocked_reason()`` returns None everywhere, and
    unrelated plan-write tests start attempting a live sync — observed as
    ``test_commit_activates_draft`` finding an unexpected ``calendar`` key in
    its payload.

    The failure mode is the dangerous direction: **CI has no ``.env``, so CI
    stays green while the suite breaks on every configured machine.** That is
    the inverse of the 0.51.0 no-database bug (green locally, red in CI) and
    strictly worse, because nothing forces anyone to notice.

    Clearing them makes "not configured" the default, which is also the state a
    fresh clone is in — the behavior most tests should be asserting against
    anyway. A test that wants the calendar path sets these itself via
    ``monkeypatch.setenv``; autouse fixtures are set up first, so an explicit
    set always wins.
    """
    for name in ("LOCAL_FITNESS_GCAL_CLIENT_ID",
                 "LOCAL_FITNESS_GCAL_CLIENT_SECRET",
                 "LOCAL_FITNESS_GCAL_REFRESH_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_live_calendar_calls(monkeypatch):
    """Hard-block real Google Calendar traffic for the whole suite.

    Fourth guard, same lesson as the SDK, Garmin and SMTP ones: the calendar
    path ends in an HTTP POST that CREATES something in an account, and a test
    that walked far enough down ``cli.plan_calendar`` would put a real event on
    a real calendar. Like the mail send, that is worse than slow — it is not
    undoable from inside the test run.

    Raises rather than no-opping, for the SMTP guard's reason: no caller treats
    "the calendar write silently didn't happen" as a documented degraded path,
    so a test that trips this has a wiring bug worth surfacing. ``gcal`` funnels
    every request through one function precisely so this fixture has a single
    thing to patch; a test exercising the transport patches
    ``gcal._request`` itself, which runs after this and therefore wins.
    """
    from local_fitness.agent import gcal

    def _blocked(*args, **kwargs):
        raise RuntimeError(
            "Live Google Calendar request attempted in a test. Patch "
            "gcal._request in your test instead."
        )

    monkeypatch.setattr(gcal, "_request", _blocked)


@pytest.fixture(autouse=True)
def _fresh_profile_cache():
    """Drop ``coach.load_profile``'s memo around every test.

    The cache keys on the profile NAME only, while the read resolves through
    module-level ``coach._PROFILE_DIR`` — so a test that repoints that dir
    (``test_coach.py::test_missing_profile_dir_falls_back``) or writes a
    profile file would otherwise pin its result for every later test in the
    same process, in any file. Cleared before AND after so neither direction
    of leakage is possible.

    Suite-wide (not test_coach-local) precisely because the pollution crosses
    files: `test_prompts`, `test_personality`, `test_reflect` and
    `test_plan_coach` all call ``load_profile`` and would silently read a
    fallback persona.
    """
    from local_fitness.agent import coach

    coach.load_profile.cache_clear()
    yield
    coach.load_profile.cache_clear()
