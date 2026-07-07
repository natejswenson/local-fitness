"""Tests for agent/local_model.py — the Ollama HTTP client.

No real network calls: urllib.request.urlopen is mocked. Focus is the
contract generate_streaming's "ollama:" dispatch branch depends on: a clean
string return on success, and ALL failure modes (connection error, malformed
body, unexpected response shape) normalized into one RuntimeError shape
rather than three different low-level exception types.
"""
from __future__ import annotations

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from local_fitness.agent import local_model


def _fake_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def test_generate_local_completion_returns_content_on_success():
    with patch("urllib.request.urlopen", return_value=_fake_response(
        {"message": {"content": '{"takeaways": []}'}}
    )) as mock_urlopen:
        result = local_model.generate_local_completion(
            "system prompt", "user prompt", model="gemma4")
    assert result == '{"takeaways": []}'
    # Request shape: think disabled by default, correct model/messages.
    req = mock_urlopen.call_args[0][0]
    body = json.loads(req.data)
    assert body["model"] == "gemma4"
    assert body["stream"] is False
    assert body["think"] is False
    assert body["messages"] == [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "user prompt"},
    ]


def test_generate_local_completion_passes_through_think_and_temperature():
    with patch("urllib.request.urlopen", return_value=_fake_response(
        {"message": {"content": "ok"}}
    )) as mock_urlopen:
        local_model.generate_local_completion(
            "s", "u", model="gemma4", think=True, temperature=0.9)
    body = json.loads(mock_urlopen.call_args[0][0].data)
    assert body["think"] is True
    assert body["options"]["temperature"] == 0.9


def test_generate_local_completion_omits_format_by_default():
    with patch("urllib.request.urlopen", return_value=_fake_response(
        {"message": {"content": "ok"}}
    )) as mock_urlopen:
        local_model.generate_local_completion("s", "u")
    body = json.loads(mock_urlopen.call_args[0][0].data)
    assert "format" not in body


def test_generate_local_completion_passes_through_format_schema():
    schema = {"type": "object", "properties": {"takeaways": {"type": "array"}}}
    with patch("urllib.request.urlopen", return_value=_fake_response(
        {"message": {"content": "ok"}}
    )) as mock_urlopen:
        local_model.generate_local_completion("s", "u", format=schema)
    body = json.loads(mock_urlopen.call_args[0][0].data)
    assert body["format"] == schema


def test_connection_error_normalized_to_runtime_error():
    with patch("urllib.request.urlopen",
              side_effect=urllib.error.URLError("connection refused")):
        with pytest.raises(RuntimeError, match="ollama call failed \\(connection\\)"):
            local_model.generate_local_completion("s", "u")


def test_http_error_normalized_to_runtime_error():
    # HTTPError is a URLError subclass, so it's caught by the same branch.
    err = urllib.error.HTTPError("http://x", 500, "boom", {}, None)
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(RuntimeError, match="ollama call failed \\(connection\\)"):
            local_model.generate_local_completion("s", "u")


def test_malformed_json_body_normalized_to_runtime_error():
    resp = MagicMock()
    resp.read.return_value = b"not json at all"
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    with patch("urllib.request.urlopen", return_value=resp):
        with pytest.raises(RuntimeError, match="malformed response body"):
            local_model.generate_local_completion("s", "u")


@pytest.mark.parametrize("payload", [
    {},
    {"message": {}},
    {"message": {"content": None}},
    {"message": "not a dict"},
])
def test_unexpected_response_shape_normalized_to_runtime_error(payload):
    with patch("urllib.request.urlopen", return_value=_fake_response(payload)):
        with pytest.raises(RuntimeError, match="unexpected response shape"):
            local_model.generate_local_completion("s", "u")
