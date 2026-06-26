"""Functional tests for the data/dashboard GET endpoints and the auto-sync
state machine in ``web/server.py``.

Covers the previously-unhit read endpoints (today / metric / training-load /
workouts / workout / heatmap / strength-volume / pace-efficiency / status /
config), the pure ``_is_transient`` classifier, ``_sync_state_dict``, and the
async sync machine (``_trigger_sync`` / ``_run_sync`` / ``_schedule_retry``)
exercised with a monkeypatched ``daily.pull`` so no network is touched.

Mirrors the ``srv`` fixture + reload pattern from ``test_web_plan.py`` /
``test_web_brief.py``.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib
from datetime import date, datetime, timedelta

import httpx
import pytest

from local_fitness import db


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def srv(tmp_path, monkeypatch):
    db_path = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", db_path)
    monkeypatch.delenv("LOCAL_FITNESS_API_TOKEN", raising=False)
    db.init_schema(db_path)
    with db.connect(db_path) as conn:  # data frontier = today
        conn.execute("INSERT INTO daily_metrics (date, rhr) VALUES (?, 50)",
                     (date.today().isoformat(),))
    from local_fitness.web import server as s
    importlib.reload(s)
    return s


@pytest.fixture
def srv_auth(tmp_path, monkeypatch):
    """Same server, but with a configured API token so the auth middleware
    actually gates /api/* (exercises _is_public_path + the bearer check)."""
    db_path = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setenv("LOCAL_FITNESS_API_TOKEN", "s3cr3t-token")
    db.init_schema(db_path)
    from local_fitness.web import server as s
    importlib.reload(s)
    return s


def _client(srv):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=srv.app), base_url="http://t")


# ---------------------------------------------------------------- seeding --

def _seed_data(dbp):
    """Seed daily_metrics, baselines, and a few activities (run + strength)."""
    today = date.today()
    d0 = today.isoformat()
    d1 = (today - timedelta(days=1)).isoformat()
    d2 = (today - timedelta(days=2)).isoformat()
    with db.connect(dbp) as conn:
        # Enrich today + a couple prior days of wellness.
        conn.execute(
            "INSERT OR REPLACE INTO daily_metrics "
            "(date, rhr, sleep_seconds, sleep_score, avg_stress, max_stress, "
            " body_battery_min, body_battery_max, steps, training_status, "
            " intensity_minutes_moderate, intensity_minutes_vigorous) "
            "VALUES (?,48,28800,82,30,70,15,95,9000,'productive',40,10)", (d0,))
        conn.execute(
            "INSERT OR REPLACE INTO daily_metrics "
            "(date, rhr, sleep_seconds, sleep_score, avg_stress, body_battery_max, steps) "
            "VALUES (?,52,25200,75,40,88,8000)", (d1,))
        conn.execute(
            "INSERT OR REPLACE INTO daily_metrics "
            "(date, rhr, sleep_seconds, sleep_score, avg_stress, body_battery_max, steps) "
            "VALUES (?,49,30600,88,25,99,12000)", (d2,))
        # Baselines (rolling means + Banister load model).
        for d in (d0, d1, d2):
            conn.execute(
                "INSERT OR REPLACE INTO baselines "
                "(date, rhr_60day_mean, sleep_seconds_60day_mean, "
                " body_battery_max_60day_mean, stress_60day_mean, ctl, atl, tsb) "
                "VALUES (?,50.0,27000.0,90.0,35.0,42.0,38.0,4.0)", (d,))
        # A running activity (feeds workouts / pace-efficiency / heatmap).
        conn.execute(
            "INSERT INTO activities "
            "(activity_id, date, start_time, activity_type, activity_name, "
            " duration_seconds, distance_meters, avg_hr, max_hr, "
            " avg_pace_sec_per_km, elevation_gain_meters, aerobic_te, "
            " anaerobic_te, training_load, calories) "
            "VALUES (101,?,?,?,?,3000,8000.0,150,175,330.0,40.0,3.2,0.5,75.0,500)",
            (d0, f"{d0}T07:00:00", "running", "Morning Run"))
        # A strength activity (feeds strength-volume).
        conn.execute(
            "INSERT INTO activities "
            "(activity_id, date, start_time, activity_type, activity_name, "
            " duration_seconds, distance_meters, training_load, calories) "
            "VALUES (102,?,?,?,?,2400,0.0,30.0,200)",
            (d1, f"{d1}T18:00:00", "strength_training", "Lifting"))
        # HR zones + splits for the run (feeds /api/workout/{id}).
        conn.execute("INSERT INTO activity_hr_zones (activity_id, zone, seconds_in_zone) "
                     "VALUES (101,1,600),(101,2,1800),(101,3,600)")
        conn.execute("INSERT INTO activity_splits "
                     "(activity_id, split_index, distance_meters, duration_seconds, avg_hr) "
                     "VALUES (101,0,1000.0,330,148),(101,1,1000.0,335,152)")


def _insert_run(dbp, *, source="daily", status="success",
                completed_at=None, started_at="2026-06-25T06:00:00",
                error=None, last_date=None):
    with db.connect(dbp) as conn:
        conn.execute(
            "INSERT INTO ingest_runs "
            "(started_at, completed_at, status, last_date_fetched, error_message, source) "
            "VALUES (?,?,?,?,?,?)",
            (started_at, completed_at, status, last_date, error, source),
        )


# ---------------------------------------------------------------- GET endpoints --

@pytest.mark.anyio
async def test_status(srv):
    _seed_data(db.DEFAULT_DB_PATH)
    async with _client(srv) as c:
        r = await c.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["row_counts"]["activities"] == 2
    assert body["date_range"]["last"] == date.today().isoformat()
    assert "last_ingest_run" in body


@pytest.mark.anyio
async def test_config(srv):
    db.set_setting("user_name", "Tester")
    async with _client(srv) as c:
        r = await c.get("/api/config")
    assert r.status_code == 200
    body = r.json()
    # The seeded setting must surface in both the top-level name and the
    # passthrough settings map, not just be present as a key.
    assert body["user_name"] == "Tester"
    assert body["settings"]["user_name"] == "Tester"


@pytest.mark.anyio
async def test_today(srv):
    _seed_data(db.DEFAULT_DB_PATH)
    async with _client(srv) as c:
        r = await c.get("/api/today")
    assert r.status_code == 200
    body = r.json()
    assert body["today"] == date.today().isoformat()
    assert body["latest"]["rhr"] == 48
    assert len(body["recent_14d"]) >= 3
    assert body["baseline"]["ctl"] == 42.0


@pytest.mark.anyio
async def test_metric_ok(srv):
    _seed_data(db.DEFAULT_DB_PATH)
    async with _client(srv) as c:
        r = await c.get("/api/metric/rhr?days=30")
    assert r.status_code == 200
    body = r.json()
    assert body["metric"] == "rhr"
    assert body["days"] == 30
    assert {row["value"] for row in body["values"]} == {48, 52, 49}
    # rhr is one of the two metrics with a baseline series.
    assert body["baseline"] and body["baseline"][0]["value"] == 50.0


@pytest.mark.anyio
async def test_metric_no_baseline_series(srv):
    _seed_data(db.DEFAULT_DB_PATH)
    async with _client(srv) as c:
        r = await c.get("/api/metric/steps")
    assert r.status_code == 200
    assert r.json()["baseline"] is None


@pytest.mark.anyio
async def test_metric_unknown_400(srv):
    async with _client(srv) as c:
        r = await c.get("/api/metric/bogus")
    assert r.status_code == 400


@pytest.mark.anyio
async def test_metric_days_out_of_range_422(srv):
    async with _client(srv) as c:
        r = await c.get("/api/metric/rhr?days=0")
    assert r.status_code == 422


@pytest.mark.anyio
async def test_training_load(srv):
    _seed_data(db.DEFAULT_DB_PATH)
    async with _client(srv) as c:
        r = await c.get("/api/training-load?days=90")
    assert r.status_code == 200
    body = r.json()
    assert body["days"] == 90
    assert len(body["values"]) == 3
    assert body["values"][0]["ctl"] == 42.0


@pytest.mark.anyio
async def test_workouts_all(srv):
    _seed_data(db.DEFAULT_DB_PATH)
    async with _client(srv) as c:
        r = await c.get("/api/workouts")
    assert r.status_code == 200
    assert len(r.json()["workouts"]) == 2


@pytest.mark.anyio
async def test_workouts_filtered(srv):
    _seed_data(db.DEFAULT_DB_PATH)
    async with _client(srv) as c:
        r = await c.get("/api/workouts?activity_type=running&days=7&limit=10")
    assert r.status_code == 200
    workouts = r.json()["workouts"]
    assert len(workouts) == 1
    assert workouts[0]["activity_type"] == "running"


@pytest.mark.anyio
async def test_workout_detail(srv):
    _seed_data(db.DEFAULT_DB_PATH)
    async with _client(srv) as c:
        r = await c.get("/api/workout/101")
    assert r.status_code == 200
    body = r.json()
    assert body["activity"]["activity_id"] == 101
    assert "raw_json" not in body["activity"]
    assert len(body["hr_zones"]) == 3
    assert len(body["splits"]) == 2


@pytest.mark.anyio
async def test_workout_missing_404(srv):
    async with _client(srv) as c:
        r = await c.get("/api/workout/9999")
    assert r.status_code == 404


@pytest.mark.anyio
async def test_activity_heatmap(srv):
    _seed_data(db.DEFAULT_DB_PATH)
    async with _client(srv) as c:
        r = await c.get("/api/activity-heatmap?days=365")
    assert r.status_code == 200
    body = r.json()
    assert body["days"] == 365
    assert len(body["values"]) == 3
    today_cell = next(v for v in body["values"] if v["date"] == date.today().isoformat())
    assert today_cell["activity_count"] == 1
    assert today_cell["dominant_type"] == "running"
    assert today_cell["activities"][0]["activity_id"] == 101
    assert today_cell["load_state"]["ctl"] == 42.0
    # Percentile ranks computed across the window (0..100, best == 0).
    assert today_cell["recovery_pct"]["rhr"] is not None


@pytest.mark.anyio
async def test_strength_volume(srv):
    _seed_data(db.DEFAULT_DB_PATH)
    async with _client(srv) as c:
        r = await c.get("/api/strength-volume?weeks=104")
    assert r.status_code == 200
    body = r.json()
    assert body["weeks"] == 104
    assert body["total_sessions"] == 1
    assert body["last_session_date"] == (date.today() - timedelta(days=1)).isoformat()
    assert body["values"][0]["sessions"] == 1


@pytest.mark.anyio
async def test_pace_efficiency(srv):
    _seed_data(db.DEFAULT_DB_PATH)
    async with _client(srv) as c:
        r = await c.get("/api/pace-efficiency?days=180&min_distance_km=1.0")
    assert r.status_code == 200
    body = r.json()
    assert body["min_distance_km"] == 1.0
    assert len(body["values"]) == 1
    row = body["values"][0]
    # hr_per_kmh = avg_hr * pace_sec / 3600 = 150 * 330 / 3600 = 13.75
    assert row["hr_per_kmh"] == 13.75
    assert row["tsb"] == 4.0


@pytest.mark.anyio
async def test_pace_efficiency_excludes_short_runs(srv):
    _seed_data(db.DEFAULT_DB_PATH)
    async with _client(srv) as c:
        # 8km run filtered out by a 10km minimum.
        r = await c.get("/api/pace-efficiency?min_distance_km=10")
    assert r.status_code == 200
    assert r.json()["values"] == []


# ---------------------------------------------------------------- _is_transient --

@pytest.mark.parametrize("status,error,expected", [
    ("failure", "HTTP 429 rate limit exceeded", True),
    ("partial", "connection reset by peer", True),
    ("failure", "request timed out", True),
    ("failure", "503 service unavailable", True),
    ("failure", "auth_failure: invalid credentials", False),
    ("failure", None, False),
    ("success", "429", False),       # wrong status short-circuits
    (None, "timeout", False),
    ("not_configured", "no creds", False),
])
def test_is_transient(srv, status, error, expected):
    assert srv._is_transient(status, error) is expected


# ---------------------------------------------------------------- _sync_state_dict --

def test_sync_state_dict_no_runs(srv):
    st = srv._sync_state_dict()
    assert st["is_running"] is False
    assert st["last_status"] is None
    assert st["next_eligible_at"] is None
    assert st["seconds_until_eligible"] == 0
    assert st["throttle_seconds"] == srv.SYNC_THROTTLE_SECONDS
    assert st["data_through_date"] == date.today().isoformat()
    assert st["days_behind"] == 0


def test_sync_state_dict_throttled_window(srv):
    now = datetime.now().isoformat(timespec="seconds")
    _insert_run(db.DEFAULT_DB_PATH, status="success", completed_at=now,
                last_date=date.today().isoformat())
    st = srv._sync_state_dict()
    assert st["last_status"] == "success"
    assert st["last_completed_at"] == now
    assert st["next_eligible_at"] is not None
    assert st["seconds_until_eligible"] > 0


# ---------------------------------------------------------------- _run_sync --

@pytest.mark.anyio
async def test_run_sync_success_recomputes(srv, monkeypatch):
    calls = {}
    monkeypatch.setattr(srv.daily_ingest, "pull",
                        lambda **k: {"status": "success", "days_pulled": 3})
    monkeypatch.setattr(srv.baselines_mod, "recompute",
                        lambda **k: calls.setdefault("recompute", True) or 0)
    srv._retry_count = 5
    await srv._run_sync()
    assert calls.get("recompute") is True
    assert srv._retry_count == 0
    assert srv._sync_running is False
    assert srv._sync_started_at is None


@pytest.mark.anyio
async def test_run_sync_success_no_new_days_skips_recompute(srv, monkeypatch):
    calls = {}
    monkeypatch.setattr(srv.daily_ingest, "pull",
                        lambda **k: {"status": "success", "days_pulled": 0})
    monkeypatch.setattr(srv.baselines_mod, "recompute",
                        lambda **k: calls.setdefault("recompute", True))
    await srv._run_sync()
    assert "recompute" not in calls
    assert srv._sync_running is False


@pytest.mark.anyio
async def test_run_sync_transient_schedules_retry(srv, monkeypatch):
    monkeypatch.setattr(srv, "SYNC_RETRY_BACKOFFS", [3600])  # keep the retry dormant
    monkeypatch.setattr(srv.daily_ingest, "pull",
                        lambda **k: {"status": "failure", "error": "HTTP 429 rate limit"})
    srv._retry_count = 0
    await srv._run_sync()
    assert srv._retry_count == 1
    assert srv._retry_task is not None
    srv._retry_task.cancel()
    with contextlib.suppress(BaseException):
        await srv._retry_task


@pytest.mark.anyio
async def test_run_sync_nontransient_no_retry(srv, monkeypatch):
    monkeypatch.setattr(srv.daily_ingest, "pull",
                        lambda **k: {"status": "failure", "error": "auth_failure"})
    srv._retry_count = 3
    await srv._run_sync()
    assert srv._retry_count == 0  # reset, clean slate
    assert srv._retry_task is None  # no retry armed for a user-actionable failure


@pytest.mark.anyio
async def test_run_sync_exception_resets_running(srv, monkeypatch):
    def _boom(**k):
        raise RuntimeError("garth blew up")
    monkeypatch.setattr(srv.daily_ingest, "pull", _boom)
    await srv._run_sync()  # must not propagate
    assert srv._sync_running is False
    assert srv._sync_started_at is None


# ---------------------------------------------------------------- _trigger_sync --

@pytest.mark.anyio
async def test_trigger_already_running(srv):
    srv._sync_running = True
    try:
        res = await srv._trigger_sync()
    finally:
        srv._sync_running = False
    assert res["started"] is False
    assert res["reason"] == "already_running"


@pytest.mark.anyio
async def test_trigger_throttled(srv):
    now = datetime.now().isoformat(timespec="seconds")
    _insert_run(db.DEFAULT_DB_PATH, status="success", completed_at=now)
    res = await srv._trigger_sync(force=False)
    assert res["started"] is False
    assert res["reason"] == "throttled"


@pytest.mark.anyio
async def test_trigger_starts_when_forced(srv, monkeypatch):
    now = datetime.now().isoformat(timespec="seconds")
    _insert_run(db.DEFAULT_DB_PATH, status="success", completed_at=now)  # would throttle
    monkeypatch.setattr(srv.daily_ingest, "pull",
                        lambda **k: {"status": "success", "days_pulled": 0})
    res = await srv._trigger_sync(force=True)
    assert res["started"] is True
    assert srv._sync_task is not None
    await srv._sync_task  # let the worker finish
    assert srv._sync_running is False


@pytest.mark.anyio
async def test_trigger_starts_when_no_prior_run(srv, monkeypatch):
    monkeypatch.setattr(srv.daily_ingest, "pull",
                        lambda **k: {"status": "success", "days_pulled": 0})
    res = await srv._trigger_sync(force=False)
    assert res["started"] is True
    await srv._sync_task


# ---------------------------------------------------------------- sync endpoints --

@pytest.mark.anyio
async def test_api_sync_endpoint(srv, monkeypatch):
    monkeypatch.setattr(srv.daily_ingest, "pull",
                        lambda **k: {"status": "success", "days_pulled": 0})
    async with _client(srv) as c:
        r = await c.post("/api/sync?force=true")
    assert r.status_code == 200
    assert r.json()["started"] is True
    if srv._sync_task is not None:
        await srv._sync_task


@pytest.mark.anyio
async def test_api_sync_status_endpoint(srv):
    async with _client(srv) as c:
        r = await c.get("/api/sync/status")
    assert r.status_code == 200
    body = r.json()
    assert body["is_running"] is False
    assert "days_behind" in body
    assert body["max_days_per_pull"] == srv.SYNC_MAX_DAYS


# ---------------------------------------------------------------- health / auth-verify --

@pytest.mark.anyio
async def test_health(srv):
    async with _client(srv) as c:
        r = await c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_auth_verify_open_when_no_token(srv):
    async with _client(srv) as c:
        r = await c.get("/api/auth/verify")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "auth_required": False}


# ---------------------------------------------------------------- auth gating (token set) --

@pytest.mark.anyio
async def test_auth_rejects_without_bearer(srv_auth):
    async with _client(srv_auth) as c:
        r = await c.get("/api/today")
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}


@pytest.mark.anyio
async def test_auth_rejects_wrong_token(srv_auth):
    async with _client(srv_auth) as c:
        r = await c.get("/api/today", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


@pytest.mark.anyio
async def test_auth_accepts_valid_token(srv_auth):
    async with _client(srv_auth) as c:
        r = await c.get("/api/auth/verify", headers={"Authorization": "Bearer s3cr3t-token"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "auth_required": True}


@pytest.mark.anyio
async def test_health_public_even_with_token(srv_auth):
    async with _client(srv_auth) as c:
        r = await c.get("/health")
    assert r.status_code == 200


@pytest.mark.anyio
async def test_mcp_path_gated_with_token(srv_auth):
    # /mcp lives outside /api/ but must NOT be treated as public.
    async with _client(srv_auth) as c:
        r = await c.get("/mcp/")
    assert r.status_code == 401


# ---------------------------------------------------------------- notes CRUD --

@pytest.mark.anyio
async def test_notes_crud(srv, monkeypatch, tmp_path):
    notes_path = tmp_path / "user_notes.md"
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(notes_path))
    async with _client(srv) as c:
        # Empty to start.
        r = await c.get("/api/notes")
        assert r.status_code == 200
        assert r.json()["notes"] == []
        # Create.
        r = await c.post("/api/notes", json={"text": "prefers morning runs"})
        assert r.status_code == 200
        assert r.json()["saved"] is True
        # Reads back.
        r = await c.get("/api/notes")
        notes = r.json()["notes"]
        assert len(notes) == 1 and notes[0]["text"] == "prefers morning runs"
        line = notes[0]["line"]
        # Delete it.
        r = await c.delete(f"/api/notes/{line}")
        assert r.status_code == 200 and r.json()["deleted"] is True


@pytest.mark.anyio
async def test_notes_create_blank_400(srv, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "n.md"))
    async with _client(srv) as c:
        r = await c.post("/api/notes", json={"text": "   "})
    assert r.status_code == 400


@pytest.mark.anyio
async def test_notes_delete_missing_404(srv, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "n.md"))
    async with _client(srv) as c:
        r = await c.delete("/api/notes/999")
    assert r.status_code == 404


# ---------------------------------------------------------------- percentile edges --

@pytest.mark.anyio
async def test_heatmap_single_day_percentile_branches(srv):
    # Base fixture seeds exactly one daily_metrics row (today, rhr only).
    # rhr population has n==1 (single-element branch); sleep/bb/stress
    # populations are empty (the not-pairs branch).
    async with _client(srv) as c:
        r = await c.get("/api/activity-heatmap?days=30")
    assert r.status_code == 200
    cells = r.json()["values"]
    assert len(cells) == 1
    pct = cells[0]["recovery_pct"]
    assert pct["rhr"] == 0.0          # single value ranks as the best
    assert pct["sleep_seconds"] is None  # empty population → no rank


# ---------------------------------------------------------------- _schedule_retry --

@pytest.mark.anyio
async def test_schedule_retry_cancels_prior_task(srv, monkeypatch):
    monkeypatch.setattr(srv, "SYNC_RETRY_BACKOFFS", [3600])  # dormant
    srv._retry_count = 0
    srv._schedule_retry()
    first = srv._retry_task
    assert first is not None and srv._retry_count == 1
    # Second call must cancel the still-pending first task and re-arm.
    srv._schedule_retry()
    second = srv._retry_task
    assert second is not first and srv._retry_count == 2
    # The second arm must have cancelled the still-pending first task. Let the
    # cancellation propagate, then observe that it actually happened — if the
    # impl dropped its `_retry_task.cancel()`, `first` would still be pending
    # (with a 3600s backoff it never completes on its own) and this fails.
    await asyncio.sleep(0)
    assert first.cancelled()
    second.cancel()
    with contextlib.suppress(BaseException):
        await second


# ---------------------------------------------------------------- SPA-shell public path --

@pytest.mark.anyio
async def test_root_public(srv):
    # "/" is public (SPA shell when dist is built, JSON stub otherwise).
    async with _client(srv) as c:
        r = await c.get("/")
    assert r.status_code == 200


@pytest.mark.anyio
async def test_root_public_even_with_token(srv_auth):
    # The SPA shell ("/") is public even when the API token is configured
    # (exercises _is_public_path's non-/api fall-through).
    async with _client(srv_auth) as c:
        r = await c.get("/")
    assert r.status_code == 200


# ---------------------------------------------------------------- rate-limit middleware --

@pytest.mark.anyio
async def test_rate_limit_throttles_non_loopback(srv, monkeypatch):
    # The prefix tuple ships empty (no Claude-cost paths today); re-arm it
    # so the bucket logic is exercised, and drive from a non-loopback IP.
    monkeypatch.setattr(srv, "RATE_LIMITED_PREFIXES", ("/api/",))
    monkeypatch.setattr(srv, "RATE_LIMIT_MAX_REQUESTS", 3)
    transport = httpx.ASGITransport(app=srv.app, client=("203.0.113.7", 5555))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        statuses = [(await c.get("/api/sync/status")).status_code for _ in range(5)]
    assert statuses[:3] == [200, 200, 200]
    assert 429 in statuses


@pytest.mark.anyio
async def test_rate_limit_exempts_loopback(srv, monkeypatch):
    monkeypatch.setattr(srv, "RATE_LIMITED_PREFIXES", ("/api/",))
    monkeypatch.setattr(srv, "RATE_LIMIT_MAX_REQUESTS", 2)
    transport = httpx.ASGITransport(app=srv.app, client=("127.0.0.1", 5555))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        statuses = [(await c.get("/api/sync/status")).status_code for _ in range(5)]
    assert statuses == [200] * 5  # loopback never throttled


# ---------------------------------------------------------------- plan (empty shapes) --
# (brief content lives in test_web_brief.py with a monkeypatched briefings dir.)

@pytest.mark.anyio
async def test_plan_empty_and_404s(srv):
    async with _client(srv) as c:
        assert (await c.get("/api/plan")).json() == {"active": None, "draft": None}
        assert (await c.get("/api/plan/draft")).json() == {"draft": None}
        assert (await c.post("/api/plan/9999/commit")).status_code == 404
        assert (await c.delete("/api/plan/9999")).status_code == 404
