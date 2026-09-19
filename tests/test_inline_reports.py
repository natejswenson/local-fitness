"""ChatGPT reporting journeys through the production HTTP/MCP stack.

Synthetic Garmin data only. The tests inspect the actual wire envelopes and
rendered image/PDF bytes, not a stub's prebuilt report response.
"""
from __future__ import annotations

import asyncio
import base64
import importlib
import io
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pdfplumber
import pytest
from PIL import Image
from starlette.testclient import TestClient

from local_fitness import db
from local_fitness.agent import card_store, tools, visuals
from local_fitness.ingest import details
from local_fitness.web import artifacts, mcp_server


@pytest.fixture
def report_db(tmp_path, monkeypatch):
    path = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", path)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "notes.md"))
    db.init_schema(path)
    today = date.today()
    with db.connect() as conn:
        for back in range(13):
            day = (today - timedelta(days=back)).isoformat()
            conn.execute(
                "INSERT INTO activities (activity_id, date, start_time, activity_type, "
                "activity_name, duration_seconds, distance_meters, avg_hr, "
                "avg_pace_sec_per_km, training_load) VALUES "
                "(?, ?, ?, 'running', 'Morning Run', 3000, 10000, 150, 300, 100)",
                (back + 1, day, day + "T07:00:00"))
            conn.execute("INSERT INTO daily_metrics (date, rhr, steps) VALUES (?, 50, 10000)",
                         (day,))
        for idx, hr in enumerate((140, 145, 150, 155)):
            conn.execute(
                "INSERT INTO activity_splits (activity_id, split_index, distance_meters, "
                "duration_seconds, avg_hr) VALUES (1, ?, 1609.34, 600, ?)", (idx, hr))
    # Fail visibly if the immediate path attempts any network/generation/open.
    generate = AsyncMock(side_effect=AssertionError("no model on inline path"))
    reflect = AsyncMock(side_effect=AssertionError("no reflection on inline path"))
    fetch = Mock(side_effect=AssertionError("no Garmin on inline path"))
    open_file = AsyncMock(side_effect=AssertionError("no local file for HTTP"))
    monkeypatch.setattr(tools.workout_coach, "generate_read_cached", generate)
    monkeypatch.setattr(tools.reflect, "reflect_after_report_card", reflect)
    monkeypatch.setattr(details, "fetch_hr_samples", fetch)
    monkeypatch.setattr(tools, "_auto_open", open_file)
    yield path
    generate.assert_not_called()
    reflect.assert_not_called()
    fetch.assert_not_called()
    open_file.assert_not_called()


@pytest.fixture
def remote(report_db, monkeypatch):
    monkeypatch.setenv("LOCAL_FITNESS_API_TOKEN", "test-secret")
    monkeypatch.setenv("LOCAL_FITNESS_MCP_ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("LOCAL_FITNESS_PUBLIC_URL", "https://testserver")
    from local_fitness.web import server

    mcp_server._persona_cache_clear()
    artifacts._ARTIFACTS.clear()
    importlib.reload(server)
    with TestClient(server.app) as client:
        client.headers.update({"Authorization": "Bearer test-secret",
                               "Accept": "application/json, text/event-stream"})
        init = client.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "report-journey", "version": "1"}}})
        assert init.status_code == 200, init.text
        yield client
    artifacts._ARTIFACTS.clear()
    mcp_server._persona_cache_clear()


def call_remote(client, name, args=None):
    response = client.post("/mcp/", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": name, "arguments": args or {}}})
    assert response.status_code == 200, response.text
    return response.json()["result"]


def png_from(result):
    images = [c for c in result["content"] if c["type"] == "image"]
    assert len(images) == 1
    assert images[0]["mimeType"] == "image/png"
    png = base64.b64decode(images[0]["data"], validate=True)
    with Image.open(io.BytesIO(png)) as im:
        assert im.format == "PNG"
        assert im.width >= 900 and im.height >= 250
        assert im.convert("L").getextrema()[0] < 100  # actual drawn ink, not empty paper
    return png


def test_remote_create_repeat_and_retrieve_report(remote):
    report = call_remote(remote, "workout_report_card")
    assert not report["isError"]
    body = report["structuredContent"]
    assert body["activity_id"] == 1
    assert body["coaching_source"] == "deterministic"
    assert body["ratings"]["distance"] in report["content"][0]["text"]
    assert body["overall"]["stars"] == 5.0  # matches this run's rolling reference
    assert "computed summary" in report["content"][0]["text"]
    assert "sync_garmin_data(force=true)" in report["content"][0]["text"]
    png = png_from(report)
    repeat = call_remote(remote, "workout_report_card")
    assert repeat == report
    stored = call_remote(remote, "get_report_card", {"activity_id": 1})
    assert not stored["isError"]
    assert "Stored snapshot" in stored["content"][0]["text"]
    assert stored["structuredContent"]["card"]["overall"] == body["overall"]
    assert png_from(stored) == png
    assert "path" not in body


def test_inline_reuses_matching_generated_read(report_db, monkeypatch):
    # An explicit local generation warms the per-activity cache. The immediate
    # path then has to reuse it without invoking either generator or reflection.
    monkeypatch.setattr(tools.memory, "memory_enabled", lambda: False)

    async def generate(_profile, card, **_kwargs):
        read = tools.workout_coach.fallback_read(card)
        read["distance"] = "Held the prescribed distance throughout."
        return read

    with monkeypatch.context() as patch:
        patch.setattr(tools.workout_coach, "generate_read_cached", generate)
        asyncio.run(tools.workout_report_card.handler({"format": "table", "coaching": "generate"}))
    before = card_store.load_card(1)
    result = asyncio.run(tools.workout_report_card.handler({}))
    assert result["structuredContent"]["coaching_source"] == "cached"
    assert "Held the prescribed distance throughout." in result["content"][0]["text"]
    assert result["structuredContent"]["overall"]["stars"] == before["card"]["overall"]["stars"]
    assert card_store.load_card(1) == before


def test_missing_splits_still_delivers_report(remote):
    with db.connect() as conn:
        conn.execute("DELETE FROM activity_splits")
    report = call_remote(remote, "workout_report_card")
    assert not report["isError"]
    assert report["structuredContent"]["splits_available"] is False
    assert [c["type"] for c in report["content"]] == ["text"]
    assert "Lap splits are missing" in report["content"][0]["text"]
    assert "No stored HR trace" in report["content"][0]["text"]


def test_cached_hr_trace_is_available_without_garmin(remote):
    with db.connect() as conn:
        conn.execute("DELETE FROM activity_splits")
        details.store_hr_samples(conn, 1, [details.HrSample(i * 100, 135 + i, i * 35)
                                           for i in range(1, 25)])
    report = call_remote(remote, "workout_report_card")
    assert not report["isError"]
    png_from(report)
    assert "No stored HR trace" not in report["content"][0]["text"]
    replay = call_remote(remote, "get_report_card", {"activity_id": 1})
    assert png_from(replay) == png_from(report)


def test_chart_failure_preserves_report(remote, monkeypatch):
    monkeypatch.setattr(visuals, "render_split_hr_png", Mock(side_effect=RuntimeError("broken")))
    report = call_remote(remote, "workout_report_card")
    assert not report["isError"]
    assert "HR chart could not be rendered" in report["content"][0]["text"]
    assert report["structuredContent"]["overall"]["stars"] == 5.0


def test_remote_pdf_is_a_downloadable_file(remote):
    report = call_remote(remote, "workout_report_card", {"format": "pdf"})
    assert not report["isError"], report
    body = report["structuredContent"]
    assert "path" not in body
    assert body["download_expires_in_seconds"] == 600
    assert body["download_url"].startswith("https://testserver/reports/")
    remote.headers.pop("Authorization")
    download = remote.get(body["download_url"])
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/pdf"
    assert download.headers["cache-control"] == "private, no-store"
    with pdfplumber.open(io.BytesIO(download.content)) as pdf:
        assert len(pdf.pages) == 1
        assert "Morning Run" in pdf.pages[0].extract_text()
        assert len(pdf.pages[0].images) == 1
    assert "Download PDF" in report["content"][0]["text"]


def test_remote_expensive_coaching_rejected_even_with_local_argument(remote):
    report = call_remote(remote, "workout_report_card", {"coaching": "generate", "local_exports": True})
    assert report["isError"]
    assert "local-only" in report["content"][0]["text"]
    assert card_store.load_card(1) is None
    assert tools.LOCAL_REPORT_EXPORTS.get() is True  # call context never leaks


def test_remote_pdf_without_origin_is_actionable_and_does_not_persist(remote, monkeypatch):
    monkeypatch.delenv("LOCAL_FITNESS_PUBLIC_URL")
    result = call_remote(remote, "workout_report_card", {"format": "pdf"})
    assert result["isError"]
    assert "format='inline'" in result["content"][0]["text"]
    assert card_store.load_card(1) is None


def test_remote_metric_chart_defaults_to_inline_and_updates(remote):
    first = call_remote(remote, "chart", {"metric": "rhr", "days": 7})
    assert not first["isError"]
    png = png_from(first)
    assert first["structuredContent"]["metric"] == "rhr"
    assert "path" not in first["structuredContent"]
    with db.connect() as conn:
        conn.execute("UPDATE daily_metrics SET rhr = 70 WHERE date = ?", (date.today().isoformat(),))
    second = call_remote(remote, "chart", {"metric": "rhr", "days": 7})
    assert png_from(second) != png
    calendar = call_remote(remote, "chart", {"metric": "rhr", "days": 7, "style": "calendar"})
    assert [c["type"] for c in calendar["content"]] == ["text"]
    assert "Mon→Sun" in calendar["content"][0]["text"]


def test_coverage_notice_distinguishes_historical_pull(report_db):
    today = date.today().isoformat()
    card = {"splits": {"available": True}}
    with db.connect() as conn:
        conn.execute("INSERT INTO ingest_runs (source, started_at, completed_at, status, "
                     "last_date_fetched) VALUES ('daily', ?, ?, 'success', '2020-01-01')",
                     (datetime.now().isoformat(), datetime.now().isoformat()))
        assert tools._report_data_quality(conn, card)["refresh_recommended"] is True
        conn.execute("UPDATE ingest_runs SET last_date_fetched = ?", (today,))
        quality = tools._report_data_quality(conn, card)
    assert quality["daily_metrics_through"] == today
    assert quality["refresh_recommended"] is False
    assert quality["messages"] == []


def test_remote_plan_chart_uses_the_shared_plan_rows(remote, monkeypatch):
    from local_fitness import plans

    proposed = call_remote(remote, "propose_training_plan", {
        "goal_type": "10k", "race_date": (date.today() + timedelta(days=90)).isoformat(),
        "target_time_seconds": 3000,
        "workouts": [{"date": date.today().isoformat(), "week_index": 1, "type": "easy",
                      "target_distance_m": 10000, "description": "Easy run"}],
    })
    import json

    plan_id = json.loads(proposed["content"][0]["text"])["plan_id"]
    plans.commit_plan(plan_id, now=datetime.now().isoformat())
    report = call_remote(remote, "plan_chart")
    assert not report["isError"]
    png_from(report)
    assert report["structuredContent"]["rows"] == [{
        "label": date.today().isoformat()[5:] + " easy", "verdict": "done",
        "planned": 6.21, "actual": 6.21, "rest": False}]
    weekly = call_remote(remote, "plan_chart", {"weekly": True})
    png_from(weekly)
    assert weekly["structuredContent"]["rows"][0]["actual"] == 6.2
    assert "not workout execution" in weekly["content"][0]["text"]
    monkeypatch.setattr(visuals, "render_plan_chart_png", Mock(side_effect=RuntimeError("broken")))
    fallback = call_remote(remote, "plan_chart")
    assert not fallback["isError"]
    assert "text follows" in fallback["content"][0]["text"]
    assert "easy" in fallback["content"][0]["text"]
    assert [c["type"] for c in fallback["content"]] == ["text"]
