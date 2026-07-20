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
