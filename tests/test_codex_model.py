from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from local_fitness.agent import codex_model

_REAL_GENERATE = codex_model.generate_codex_completion


def test_codex_completion_uses_isolated_noninteractive_exec(monkeypatch):
    monkeypatch.delenv("LOCAL_FITNESS_CODEX_BIN", raising=False)
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(argv=argv, kwargs=kwargs)
        output = argv[argv.index("--output-last-message") + 1]
        with open(output, "w", encoding="utf-8") as fh:
            json.dump({"takeaways": []}, fh)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(codex_model.subprocess, "run", fake_run)
    raw = _REAL_GENERATE("system", "user", model="gpt-test")

    assert json.loads(raw) == {"takeaways": []}
    assert captured["argv"][:2] == ["codex", "exec"]
    assert "--ephemeral" in captured["argv"]
    assert "--ignore-user-config" in captured["argv"]
    assert captured["argv"][-3:] == ["--model", "gpt-test", "-"]
    assert captured["kwargs"]["input"].startswith("system\n\nuser")
    assert captured["kwargs"]["cwd"] is not None


def test_codex_completion_honors_explicit_binary(monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        output = argv[argv.index("--output-last-message") + 1]
        with open(output, "w", encoding="utf-8") as fh:
            json.dump({"takeaways": []}, fh)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setenv("LOCAL_FITNESS_CODEX_BIN", "/opt/example/codex")
    monkeypatch.setattr(codex_model.subprocess, "run", fake_run)
    _REAL_GENERATE("system", "user")
    assert captured["argv"][0] == "/opt/example/codex"


def test_codex_completion_reports_nonzero_exit(monkeypatch):
    monkeypatch.setattr(
        codex_model.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="first\nauth failed\n"),
    )
    with pytest.raises(RuntimeError, match="auth failed"):
        _REAL_GENERATE("system", "user")


def test_codex_completion_reports_missing_binary(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(codex_model.subprocess, "run", missing)
    with pytest.raises(RuntimeError, match="Codex CLI not found"):
        _REAL_GENERATE("system", "user")


def test_codex_completion_reports_timeout(monkeypatch):
    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 5)

    monkeypatch.setattr(codex_model.subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="timed out"):
        _REAL_GENERATE("system", "user", timeout=5)


def test_codex_schema_requires_three_to_five_takeaways():
    schema = codex_model._output_schema()
    takeaways = schema["properties"]["takeaways"]
    assert (takeaways["minItems"], takeaways["maxItems"]) == (3, 5)
    assert takeaways["items"]["additionalProperties"] is False
