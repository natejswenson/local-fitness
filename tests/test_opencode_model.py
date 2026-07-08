"""Tests for agent/opencode_model.py — the opencode-CLI transport.

No real subprocess calls: subprocess.run is monkeypatched (matching the
convention in tests/test_cli.py), returning fake CompletedProcess objects for
each success/failure case. Honesty disclaimer (mirrors test_local_model.py
and the original gemma4 shadow-run design's own disclaimer): these tests
prove the code's OWN logic is correct given what it's told the subprocess
returned — they do NOT prove opencode's real CLI output actually matches
what's mocked here. That real-binary gap is closed manually (see the design
doc's acceptance criteria #1 and #3), not by this file.
"""
from __future__ import annotations

import json
import logging
import subprocess
from types import SimpleNamespace

import pytest

from local_fitness.agent import opencode_model


def _completed(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _ndjson(*texts: str) -> str:
    lines = [json.dumps({"type": "text", "part": {"text": t}}) for t in texts]
    lines.append(json.dumps({"type": "step_finish"}))
    return "\n".join(lines)


def _fake_run(agent_list_result, run_result):
    """Route subprocess.run by its argv shape: ["opencode","agent","list"]
    vs. the "opencode run" invocation."""
    def _run(argv, **kw):
        if argv[:2] == ["opencode", "agent"]:
            return agent_list_result
        return run_result
    return _run


def test_generate_opencode_completion_returns_joined_text_on_success(monkeypatch):
    monkeypatch.setattr(opencode_model.subprocess, "run", _fake_run(
        _completed(stdout="fitness-brief\n"),
        _completed(stdout=_ndjson("Hello ", "world")),
    ))
    result = opencode_model.generate_opencode_completion(
        "sys", "usr", model="opencode/deepseek-v4-flash-free", agent="fitness-brief")
    assert result == "Hello world"


def test_argv_ordering_flags_before_double_dash_message_last(monkeypatch):
    """Catches the exact regression this design almost shipped: `--` must be
    the LAST flag-region element, immediately before the free-text message,
    with -m/--format/--agent all appearing BEFORE it. An output-content-only
    test can't distinguish 'flag ignored' from 'flag correctly parsed' —
    only inspecting the literal argv list structure proves the ordering."""
    captured: dict = {}

    def _run(argv, **kw):
        if argv[:2] == ["opencode", "agent"]:
            return _completed(stdout="fitness-brief\n")
        captured["argv"] = argv
        return _completed(stdout=_ndjson("ok"))

    monkeypatch.setattr(opencode_model.subprocess, "run", _run)
    opencode_model.generate_opencode_completion(
        "sys", "usr", model="opencode/deepseek-v4-flash-free", agent="fitness-brief")

    argv = captured["argv"]
    dash_idx = argv.index("--")
    for flag, value in (("-m", "opencode/deepseek-v4-flash-free"),
                        ("--format", "json"), ("--agent", "fitness-brief")):
        flag_idx = argv.index(flag)
        assert flag_idx < dash_idx
        assert argv[flag_idx + 1] == value
    assert dash_idx == len(argv) - 2
    assert argv[-1] == "sys\n\nusr"


def test_precheck_subprocess_itself_failing_is_distinct_from_agent_missing(monkeypatch):
    """opencode itself erroring (non-zero exit on the pre-check) must not be
    misreported as 'agent not found' — different remediations."""
    monkeypatch.setattr(opencode_model.subprocess, "run", _fake_run(
        _completed(returncode=1, stderr="opencode: not authenticated\n"), _completed()))
    with pytest.raises(RuntimeError, match="opencode itself failed to respond"):
        opencode_model.generate_opencode_completion(
            "sys", "usr", model="opencode/deepseek-v4-flash-free", agent="fitness-brief")


def test_precheck_succeeds_but_agent_missing_points_at_env_example(monkeypatch):
    monkeypatch.setattr(opencode_model.subprocess, "run", _fake_run(
        _completed(stdout="build\nplan\n"), _completed()))
    with pytest.raises(RuntimeError, match=r"\.env\.example"):
        opencode_model.generate_opencode_completion(
            "sys", "usr", model="opencode/deepseek-v4-flash-free", agent="fitness-brief")


def test_precheck_binary_missing_raises_distinct_error(monkeypatch):
    def _run(argv, **kw):
        raise FileNotFoundError("opencode not found")

    monkeypatch.setattr(opencode_model.subprocess, "run", _run)
    with pytest.raises(RuntimeError, match="opencode binary not found"):
        opencode_model.generate_opencode_completion(
            "sys", "usr", model="opencode/deepseek-v4-flash-free", agent="fitness-brief")


def test_precheck_timeout_raises_distinct_error(monkeypatch):
    def _run(argv, **kw):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=5)

    monkeypatch.setattr(opencode_model.subprocess, "run", _run)
    with pytest.raises(RuntimeError, match="pre-check timed out"):
        opencode_model.generate_opencode_completion(
            "sys", "usr", model="opencode/deepseek-v4-flash-free", agent="fitness-brief", timeout=5)


def test_run_binary_missing_after_precheck_passes(monkeypatch):
    def _run(argv, **kw):
        if argv[:2] == ["opencode", "agent"]:
            return _completed(stdout="fitness-brief\n")
        raise FileNotFoundError("opencode not found")

    monkeypatch.setattr(opencode_model.subprocess, "run", _run)
    with pytest.raises(RuntimeError, match="opencode binary not found"):
        opencode_model.generate_opencode_completion(
            "sys", "usr", model="opencode/deepseek-v4-flash-free", agent="fitness-brief")


def test_run_timeout_raises(monkeypatch):
    def _run(argv, **kw):
        if argv[:2] == ["opencode", "agent"]:
            return _completed(stdout="fitness-brief\n")
        raise subprocess.TimeoutExpired(cmd=argv, timeout=5)

    monkeypatch.setattr(opencode_model.subprocess, "run", _run)
    with pytest.raises(RuntimeError, match="opencode run timed out"):
        opencode_model.generate_opencode_completion(
            "sys", "usr", model="opencode/deepseek-v4-flash-free", agent="fitness-brief", timeout=5)


def test_run_nonzero_exit_raises(monkeypatch):
    monkeypatch.setattr(opencode_model.subprocess, "run", _fake_run(
        _completed(stdout="fitness-brief\n"),
        _completed(returncode=2, stderr="boom\n"),
    ))
    with pytest.raises(RuntimeError, match="opencode run failed \\(exit 2\\)"):
        opencode_model.generate_opencode_completion(
            "sys", "usr", model="opencode/deepseek-v4-flash-free", agent="fitness-brief")


def test_empty_text_events_raises(monkeypatch):
    monkeypatch.setattr(opencode_model.subprocess, "run", _fake_run(
        _completed(stdout="fitness-brief\n"),
        _completed(stdout='{"type": "step_finish"}'),
    ))
    with pytest.raises(RuntimeError, match="empty response"):
        opencode_model.generate_opencode_completion(
            "sys", "usr", model="opencode/deepseek-v4-flash-free", agent="fitness-brief")


def test_malformed_json_line_raises(monkeypatch):
    monkeypatch.setattr(opencode_model.subprocess, "run", _fake_run(
        _completed(stdout="fitness-brief\n"),
        _completed(stdout="not json at all"),
    ))
    with pytest.raises(RuntimeError, match="malformed NDJSON line"):
        opencode_model.generate_opencode_completion(
            "sys", "usr", model="opencode/deepseek-v4-flash-free", agent="fitness-brief")


def test_stderr_fallback_marker_detected_after_successful_precheck(monkeypatch):
    """The secondary output-integrity backstop: even if the pre-check
    passes, a stderr fallback substring on the actual run must still reject
    the output rather than trust a response from the wrong agent."""
    monkeypatch.setattr(opencode_model.subprocess, "run", _fake_run(
        _completed(stdout="fitness-brief\n"),
        _completed(stdout=_ndjson("ok"),
                   stderr="fitness-brief not found. Falling back to default agent\n"),
    ))
    with pytest.raises(RuntimeError, match=r"\.env\.example"):
        opencode_model.generate_opencode_completion(
            "sys", "usr", model="opencode/deepseek-v4-flash-free", agent="fitness-brief")


def test_off_machine_warning_log_fires_for_non_ollama_provider(monkeypatch, caplog):
    monkeypatch.setattr(opencode_model.subprocess, "run", _fake_run(
        _completed(stdout="fitness-brief\n"),
        _completed(stdout=_ndjson("ok")),
    ))
    with caplog.at_level(logging.WARNING, logger="local_fitness.agent.opencode_model"):
        opencode_model.generate_opencode_completion(
            "sys", "usr", model="opencode/deepseek-v4-flash-free", agent="fitness-brief")
    assert any("off-machine provider" in r.message for r in caplog.records)


def test_off_machine_warning_not_logged_before_precheck_failure(monkeypatch, caplog):
    """The warning log must sit AFTER the pre-check, not at function entry —
    a pre-check failure must emit no off-machine log at all."""
    monkeypatch.setattr(opencode_model.subprocess, "run", _fake_run(
        _completed(stdout="build\nplan\n"), _completed()))
    with caplog.at_level(logging.WARNING, logger="local_fitness.agent.opencode_model"):
        with pytest.raises(RuntimeError):
            opencode_model.generate_opencode_completion(
                "sys", "usr", model="opencode/deepseek-v4-flash-free", agent="fitness-brief")
    assert not any("off-machine" in r.message for r in caplog.records)
