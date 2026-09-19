"""Optional authenticated local-memory writer transport; legacy remains default."""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


class MemoryUnavailable(OSError):
    """Configured vault memory is unavailable; never fall back to legacy writes."""


def enabled(family: str) -> bool:
    value = os.environ.get(f"LOCAL_FITNESS_{family.upper()}_BACKEND", "legacy")
    if value not in ("legacy", "vault"):
        raise ValueError(f"Unknown {family} memory backend: {value}")
    return value == "vault"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise MemoryUnavailable("Memory writer redirects are refused")


def call(operation: str, args: dict | None = None):
    endpoint = os.environ.get("LOCAL_FITNESS_MEMORY_URL", "")
    parsed = urllib.parse.urlsplit(endpoint)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != "/memory"
    ):
        raise MemoryUnavailable(
            "Configure LOCAL_FITNESS_MEMORY_URL for the local writer"
        )
    if parsed.scheme == "http" and parsed.hostname not in (
        "127.0.0.1",
        "localhost",
        "host.docker.internal",
    ):
        raise MemoryUnavailable(
            "Plain HTTP memory transport is restricted to the local host"
        )
    token_path = os.environ.get("LOCAL_FITNESS_MEMORY_TOKEN_FILE")
    if not token_path:
        raise MemoryUnavailable("Configure LOCAL_FITNESS_MEMORY_TOKEN_FILE")
    token = Path(token_path).read_text().strip()
    if len(token) < 32:
        raise MemoryUnavailable("Invalid memory writer token")
    body = json.dumps(
        {"operation": operation, "args": args or {}, "request_id": str(uuid.uuid4())}
    ).encode()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    for attempt in range(2):
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with opener.open(request, timeout=10) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
            break
        except urllib.error.HTTPError as exc:
            raw = exc.read(65536)
            break
        except (OSError, TimeoutError) as exc:
            if attempt:
                raise MemoryUnavailable(
                    "Local memory writer unavailable; no legacy fallback"
                ) from exc
    try:
        payload = json.loads(raw)
    except (ValueError, UnboundLocalError) as exc:
        raise MemoryUnavailable("Invalid local memory response") from exc
    if not payload.get("ok"):
        if payload.get("error_type") == "IntegrityError":
            raise sqlite3.IntegrityError(payload["error"])
        if payload.get("error_type") in ("ValueError", "TypeError", "KeyError"):
            raise ValueError(payload["error"])
        raise MemoryUnavailable(payload.get("error", "Memory writer request failed"))
    return payload["result"]
