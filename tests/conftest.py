"""Shared test setup.

``scripts/`` is a folder of standalone scripts, not a package, so we put it on
``sys.path`` and import each script by its stem (e.g. ``import score_prompt``).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


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
