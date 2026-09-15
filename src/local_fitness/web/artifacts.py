"""Short-lived, bounded PDF downloads authorized by artifact capabilities.

The registry stores bytes, never paths. A capability grants only GET access to
one artifact and is not an API credential. Nothing survives a server restart.
"""
from __future__ import annotations

import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

TTL_SECONDS = 600
MAX_ARTIFACTS = 32
MAX_BYTES = 32 * 1024 * 1024
_PATH = re.compile(r"/reports/([A-Za-z0-9_-]{43})/download\.pdf\Z")
_LOCK = threading.Lock()


@dataclass(frozen=True)
class Artifact:
    data: bytes
    filename: str
    expires_at: float


_ARTIFACTS: dict[str, Artifact] = {}


def public_url() -> str:
    value = os.environ.get("LOCAL_FITNESS_PUBLIC_URL", "").rstrip("/")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.netloc
            or parsed.username or parsed.password or parsed.query
            or parsed.fragment or parsed.path):
        raise ValueError(
            "Remote PDF downloads need LOCAL_FITNESS_PUBLIC_URL set to the "
            "server's trusted http(s) origin. Use format='inline' to view the report."
        )
    return value


def _purge(now: float) -> None:
    for token in list(_ARTIFACTS):
        if _ARTIFACTS[token].expires_at <= now:
            del _ARTIFACTS[token]


def publish(data: bytes, filename: str) -> dict:
    origin = public_url()
    if not data or len(data) > MAX_BYTES:
        raise ValueError("PDF exceeds the download storage limit")
    # Names originate in our renderer, but guard the HTTP header boundary too.
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.pdf", filename):
        raise ValueError("invalid PDF download filename")
    now = time.monotonic()
    with _LOCK:
        _purge(now)
        while _ARTIFACTS and (
            len(_ARTIFACTS) >= MAX_ARTIFACTS
            or sum(len(a.data) for a in _ARTIFACTS.values()) + len(data) > MAX_BYTES
        ):
            del _ARTIFACTS[next(iter(_ARTIFACTS))]
        token = secrets.token_urlsafe(32)
        _ARTIFACTS[token] = Artifact(data, filename, now + TTL_SECONDS)
    return {
        "download_url": f"{origin}/reports/{token}/download.pdf",
        "download_expires_in_seconds": TTL_SECONDS,
    }


def resolve(path: str) -> Artifact | None:
    match = _PATH.fullmatch(path)
    if match is None:
        return None
    with _LOCK:
        _purge(time.monotonic())
        return _ARTIFACTS.get(match[1])
