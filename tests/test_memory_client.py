"""Vault transport boundaries: retry identity, unavailable writes, and narrow MCP."""

import asyncio
import json
import sqlite3
import urllib.error
from io import BytesIO

import pytest

from local_fitness import memory_client, notes
from local_fitness.agent import journal
from local_fitness.web import mcp_server


@pytest.fixture
def configured(monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("synthetic-test-token-" * 3)
    monkeypatch.setenv("LOCAL_FITNESS_MEMORY_TOKEN_FILE", str(token))
    monkeypatch.setenv("LOCAL_FITNESS_MEMORY_URL", "http://127.0.0.1:8766/memory")


class Reply(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def test_lost_reply_retries_same_uuid_and_body(configured, monkeypatch):
    bodies = []

    class Opener:
        def open(self, request, timeout):
            bodies.append(request.data)
            if len(bodies) == 1:
                raise OSError("lost reply after persistence")
            return Reply(b'{"ok":true,"result":{"handle":"12345678"}}')

    monkeypatch.setattr(
        memory_client.urllib.request, "build_opener", lambda *args: Opener()
    )
    result = memory_client.call("notes.append_note", {"text": "synthetic"})
    assert result == {"handle": "12345678"}
    assert len(bodies) == 2 and bodies[0] == bodies[1]
    assert json.loads(bodies[0])["request_id"]


def test_unavailable_write_never_creates_legacy_file(configured, monkeypatch, tmp_path):
    path = tmp_path / "legacy.md"
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(path))
    monkeypatch.setenv("LOCAL_FITNESS_PREFERENCES_BACKEND", "vault")

    class Opener:
        def open(self, *args, **kwargs):
            raise OSError("offline")

    monkeypatch.setattr(
        memory_client.urllib.request, "build_opener", lambda *args: Opener()
    )
    with pytest.raises(memory_client.MemoryUnavailable):
        notes.append_note("must not be lost")
    assert not path.exists()
    assert notes.render_for_prompt() == ""


def test_duplicate_event_retains_integrity_error_contract(configured, monkeypatch):
    class Opener:
        def open(self, *args, **kwargs):
            raise urllib.error.HTTPError(
                "http://localhost/memory",
                409,
                "conflict",
                {},
                BytesIO(
                    b'{"ok":false,"error_type":"IntegrityError","error":"duplicate"}'
                ),
            )

    monkeypatch.setattr(
        memory_client.urllib.request, "build_opener", lambda *args: Opener()
    )
    monkeypatch.setenv("LOCAL_FITNESS_JOURNAL_BACKEND", "vault")
    with pytest.raises(sqlite3.IntegrityError):
        journal.save_entry("fixture", source="brief", source_key="same")


def test_transport_rejects_remote_plaintext_before_sending(configured, monkeypatch):
    monkeypatch.setenv("LOCAL_FITNESS_MEMORY_URL", "http://example.com/memory")
    with pytest.raises(memory_client.MemoryUnavailable, match="local host"):
        memory_client.call("revision")


def test_unknown_backend_does_not_silently_select_legacy(monkeypatch):
    monkeypatch.setenv("LOCAL_FITNESS_PREFERENCES_BACKEND", "misspelled")
    with pytest.raises(ValueError):
        notes.append_note("fixture")


def test_memory_only_server_advertises_only_owned_memory_tools():
    from mcp.types import ListToolsRequest

    server = mcp_server.build_server(memory_only=True)
    response = asyncio.run(
        server.request_handlers[ListToolsRequest](ListToolsRequest(method="tools/list"))
    )
    names = {tool.name for tool in response.root.tools}
    assert names == mcp_server.MEMORY_TOOL_NAMES
    assert "update_plan_workouts" not in names
    assert not server.instructions
