"""Bounded, expiring PDF capability storage; no filesystem access."""
from urllib.parse import urlsplit

import pytest

from local_fitness.web import artifacts


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("LOCAL_FITNESS_PUBLIC_URL", "https://fitness.example.test/")
    artifacts._ARTIFACTS.clear()
    yield
    artifacts._ARTIFACTS.clear()


def publish(data=b"%PDF-fixture"):
    return urlsplit(artifacts.publish(data, "report-card-1-12345678.pdf")["download_url"]).path


def test_capability_matches_exactly_one_path_and_expires(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(artifacts.time, "monotonic", lambda: now[0])
    path = publish()
    assert artifacts.resolve(path).data == b"%PDF-fixture"
    for invalid in [path + "/", path + "?query", path.replace("download", "other"),
                    "/mcp/", "/reports/../.env", "/reports/" + "x" * 43 + "/download.pdf"]:
        assert artifacts.resolve(invalid) is None
    now[0] += artifacts.TTL_SECONDS
    assert artifacts.resolve(path) is None
    assert artifacts._ARTIFACTS == {}


def test_storage_evicts_oldest_by_count_and_total_bytes(monkeypatch):
    monkeypatch.setattr(artifacts, "MAX_ARTIFACTS", 2)
    monkeypatch.setattr(artifacts, "MAX_BYTES", 10)
    first, second, third = publish(b"1"), publish(b"2"), publish(b"3")
    assert artifacts.resolve(first) is None
    assert artifacts.resolve(second).data == b"2"
    fourth = publish(b"1234567890")
    assert artifacts.resolve(second) is None
    assert artifacts.resolve(third) is None
    assert artifacts.resolve(fourth).data == b"1234567890"
    with pytest.raises(ValueError, match="storage limit"):
        publish(b"12345678901")
    assert artifacts.resolve(fourth).data == b"1234567890"


@pytest.mark.parametrize("origin", ["", "file:///tmp", "https://user:pass@host", "https://host/path",
                                    "https://host?a=1", "https://host#part"])
def test_invalid_origins_offer_inline_recovery(monkeypatch, origin):
    monkeypatch.setenv("LOCAL_FITNESS_PUBLIC_URL", origin)
    with pytest.raises(ValueError, match="format='inline'"):
        publish()
    assert artifacts._ARTIFACTS == {}


@pytest.mark.parametrize("filename", ["../data.pdf", 'bad".pdf', "bad\r\nHeader.pdf", "file.png"])
def test_filename_cannot_escape_or_inject_headers(filename):
    with pytest.raises(ValueError, match="invalid PDF download filename"):
        artifacts.publish(b"pdf", filename)
    assert artifacts._ARTIFACTS == {}
