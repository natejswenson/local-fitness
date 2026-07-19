"""Tests for the brief pipeline's stream-resilience layer (2026-07-19 facet
review): the idle-timeout watchdog, the bounded retry in ``generate_and_save``,
the honest empty-output diagnostic, and the env knobs behind all three.

Ground truth being defended: 5 of 8 nightly runs died on SDK stream failures
(idle-out or subprocess crash) and each death cost the whole day's brief —
one attempt, 3–5 minutes burned, and a misleading "no JSON found" error.
"""
from __future__ import annotations

import asyncio

import pytest

from local_fitness.agent import briefing


# --- _iter_with_idle_timeout ------------------------------------------------

def test_idle_timeout_passthrough_when_stream_is_live():
    async def src():
        yield 1
        yield 2

    async def run():
        return [m async for m in briefing._iter_with_idle_timeout(src(), 5.0)]

    assert asyncio.run(run()) == [1, 2]


def test_idle_timeout_raises_on_stalled_stream():
    async def src():
        yield 1
        await asyncio.sleep(30)  # never reached at 0.05s timeout
        yield 2  # pragma: no cover

    async def run():
        got = []
        async for m in briefing._iter_with_idle_timeout(src(), 0.05):
            got.append(m)
        return got

    with pytest.raises(briefing.BriefStreamIdleTimeout, match="no stream message"):
        asyncio.run(run())


def test_idle_timeout_zero_disables_watchdog():
    async def src():
        await asyncio.sleep(0.05)
        yield "slow-but-fine"

    async def run():
        return [m async for m in briefing._iter_with_idle_timeout(src(), 0)]

    assert asyncio.run(run()) == ["slow-but-fine"]


# --- empty-output diagnostic ------------------------------------------------

def test_finalize_brief_empty_raw_reports_dead_stream_not_json_parse():
    """chars=0 is a died-stream signature; the old path routed it into the
    JSON parser and emitted 'no JSON found in agent response:' — pointing
    operators at the parser (and at re-minting a healthy token) instead of
    at the stream."""
    async def run():
        return [e async for e in briefing._finalize_brief("   \n", "Nate", False, None)]

    events = asyncio.run(run())
    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert "produced no output" in events[0]["message"]
    assert "no JSON found" not in events[0]["message"]


# --- generate_and_save retry loop -------------------------------------------

def _install_stream(monkeypatch, script):
    """Replace generate_streaming with a fake that pops one event-list per
    attempt from ``script``."""
    calls = {"n": 0}

    def fake_stream(model=None, save=True):
        events = script[calls["n"]]
        calls["n"] += 1

        async def gen():
            for evt in events:
                yield evt

        return gen()

    monkeypatch.setattr(briefing, "generate_streaming", fake_stream)
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_RETRY_DELAY_S", "0")
    return calls


def test_generate_and_save_retries_transient_failure_then_succeeds(monkeypatch):
    calls = _install_stream(monkeypatch, [
        [{"type": "error", "message": "stream died"}],
        [{"type": "done", "brief": {"date": "2026-07-19"}}],
    ])
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_MAX_ATTEMPTS", "3")
    path = briefing.generate_and_save()
    assert calls["n"] == 2  # failed once, succeeded on attempt 2, stopped
    assert path.name == "2026-07-19.json"


def test_generate_and_save_single_success_makes_one_attempt(monkeypatch):
    calls = _install_stream(monkeypatch, [
        [{"type": "done", "brief": {"date": "2026-07-19"}}],
    ])
    briefing.generate_and_save()
    assert calls["n"] == 1


def test_generate_and_save_raises_after_exhausting_attempts(monkeypatch):
    calls = _install_stream(monkeypatch, [
        [{"type": "error", "message": "stream died"}],
        [{"type": "error", "message": "stream died again"}],
    ])
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_MAX_ATTEMPTS", "2")
    with pytest.raises(ValueError, match="stream died again"):
        briefing.generate_and_save()
    assert calls["n"] == 2


def test_generate_and_save_retries_missing_done_event(monkeypatch):
    """A stream that completes without done (the chars=0 'reason=normal' idle
    death) is a failure and gets retried, not returned as success."""
    calls = _install_stream(monkeypatch, [
        [],  # stream ended with neither done nor error
        [{"type": "done", "brief": {"date": "2026-07-19"}}],
    ])
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_MAX_ATTEMPTS", "3")
    path = briefing.generate_and_save()
    assert calls["n"] == 2
    assert path.name == "2026-07-19.json"


# --- env knobs ---------------------------------------------------------------

def test_resilience_knobs_parse_and_fall_back(monkeypatch):
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_IDLE_TIMEOUT_S", "45")
    assert briefing._stream_idle_timeout_s() == 45.0
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_IDLE_TIMEOUT_S", "junk")
    assert briefing._stream_idle_timeout_s() == briefing._DEFAULT_STREAM_IDLE_TIMEOUT_S

    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_MAX_ATTEMPTS", "5")
    assert briefing._brief_max_attempts() == 5
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_MAX_ATTEMPTS", "0")
    assert briefing._brief_max_attempts() == 1  # floor: always one attempt
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_MAX_ATTEMPTS", "junk")
    assert briefing._brief_max_attempts() == briefing._DEFAULT_MAX_ATTEMPTS

    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_RETRY_DELAY_S", "1.5")
    assert briefing._brief_retry_delay_s() == 1.5
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_RETRY_DELAY_S", "-3")
    assert briefing._brief_retry_delay_s() == 0.0  # floor: never negative

    monkeypatch.delenv("LOCAL_FITNESS_BRIEF_IDLE_TIMEOUT_S")
    monkeypatch.delenv("LOCAL_FITNESS_BRIEF_MAX_ATTEMPTS")
    monkeypatch.delenv("LOCAL_FITNESS_BRIEF_RETRY_DELAY_S")
    assert briefing._stream_idle_timeout_s() == briefing._DEFAULT_STREAM_IDLE_TIMEOUT_S
    assert briefing._brief_max_attempts() == briefing._DEFAULT_MAX_ATTEMPTS
    assert briefing._brief_retry_delay_s() == briefing._DEFAULT_RETRY_DELAY_S
