"""Tests for the prompt eval (scripts/score_prompt.py).

The scorer is the eval that gates CI; it must pass on the real prompt and,
crucially, FAIL when the prompt drifts from the schema contract.
"""
from __future__ import annotations

import types

import score_prompt

from local_fitness.agent import prompts, schemas


def test_scorer_passes_on_live_prompt(capsys):
    rc = score_prompt.score()
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASSED" in out
    # all checks pass → 11/11 style line
    assert "Score:" in out


def test_all_checks_pass_on_live_prompt():
    checks = score_prompt.build_checks()
    assert checks  # non-empty
    assert all(ok for _desc, ok in checks)


def test_scorer_fails_when_metric_drifts_from_schema(monkeypatch, capsys):
    # Remove a metric from the schema enum so the prompt now advertises a
    # metric the schema rejects — the cross-check must catch it.
    bad = tuple(m for m in schemas.MetricName.__args__ if m != "rhr")
    monkeypatch.setattr(schemas, "MetricName", types.SimpleNamespace(__args__=bad))
    rc = score_prompt.score()
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out


def test_scorer_fails_when_tone_drifts_from_schema(monkeypatch):
    bad = schemas.Tone.__args__ + ("ecstatic",)
    monkeypatch.setattr(schemas, "Tone", types.SimpleNamespace(__args__=bad))
    checks = dict(score_prompt.build_checks())
    assert checks["briefing prompt's tones match schema Tone values exactly"] is False


def test_probe_passes_when_wiring_present():
    # The live prompt folds notes in → probe returns True.
    assert score_prompt._probe_notes_injection(prompts) is True


def test_scorer_detects_missing_notes_injection(monkeypatch):
    # If system_prompt stopped incorporating render_for_prompt's output, the
    # probe must fail. Simulate that by making system_prompt ignore notes.
    monkeypatch.setattr(prompts, "system_prompt", lambda *a, **k: "no notes here")
    assert score_prompt._probe_notes_injection(prompts) is False


def test_version_regex_rejects_garbage():
    assert score_prompt._VERSION_RE.match("0.1.0")
    assert score_prompt._VERSION_RE.match("1.2.3rc1")
    assert not score_prompt._VERSION_RE.match("")
    assert not score_prompt._VERSION_RE.match("not-a-version")


def test_pyproject_version_readable():
    assert score_prompt._pyproject_version()  # non-empty string


def test_main_exits_zero(monkeypatch):
    import pytest

    monkeypatch.setattr(score_prompt.sys, "argv", ["score_prompt.py"])
    with pytest.raises(SystemExit) as exc:
        score_prompt.main()
    assert exc.value.code == 0
