"""Tests for agent/status.py (assemble_status) + the daily_snapshot tool +
the coach MCP prompt's notes-once invariant.

``assemble_status`` is the single source of the daily snapshot: a pure read
that must never raise on an empty/new DB. The coach prompt embeds the user's
saved notes exactly once (via the persona, not the rendered snapshot).
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta

import pytest

from local_fitness import db
from local_fitness.agent import status as status_mod
from local_fitness.agent import tools
from local_fitness.agent.status import assemble_status


@pytest.fixture
def empty_db(tmp_path, monkeypatch):
    """A freshly-init'd DB with no metrics/activities/baselines."""
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    db.init_schema(p)
    return p


@pytest.fixture
def seeded_status_db(tmp_path, monkeypatch):
    """Seeded with today's daily_metrics + a baselines row + one workout.

    Carries TWO baselines rows (yesterday + today) so Fix 9's "current form
    is the last COMPLETE day" has a real yesterday row to resolve to — a
    single today-only baselines row (the old fixture shape) can never
    represent "current form" under that rule. Today's row is deliberately
    a DIFFERENT tsb/ctl/atl from yesterday's so a test asserting on
    `training_load` values (current form) can't accidentally pass by reading
    the projection instead.
    """
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    db.init_schema(p)
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    with db.connect(p) as conn:
        conn.execute(
            "INSERT INTO daily_metrics (date, rhr, sleep_seconds, sleep_score, "
            "avg_stress, body_battery_min, body_battery_max, steps) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (today, 55, 27000, 80, 30, 20, 90, 9000),
        )
        # Yesterday: the last COMPLETE day — this is "current form" under Fix 9.
        conn.execute(
            "INSERT INTO baselines (date, rhr_60day_mean, rhr_60day_sd, "
            "body_battery_max_60day_mean, ctl, atl, tsb) "
            "VALUES (?, 50.0, 2.0, 88.0, 40.0, 45.0, -5.0)",
            (yesterday,),
        )
        # Today: a same-day projection, deliberately different so it can't be
        # confused with current form in an assertion.
        conn.execute(
            "INSERT INTO baselines (date, rhr_60day_mean, rhr_60day_sd, "
            "body_battery_max_60day_mean, ctl, atl, tsb) "
            "VALUES (?, 50.0, 2.0, 88.0, 40.2, 48.0, -7.8)",
            (today,),
        )
        conn.execute(
            "INSERT INTO activities (activity_id, date, start_time, activity_type, "
            "activity_name, duration_seconds, distance_meters, avg_hr, training_load) "
            "VALUES (1, ?, ?, 'running', 'Morning Run', 3600, 10000, 150, 80.0)",
            (today, today + "T07:00:00"),
        )
    return p


def test_assemble_status_empty_db_well_formed(empty_db):
    status = assemble_status()  # must not raise
    assert set(status.keys()) >= {
        "date", "metrics", "training_load", "recent_workouts", "user_notes"
    }
    assert status["date"] == date.today().isoformat()
    assert isinstance(status["metrics"], list) and status["metrics"]
    tl = status["training_load"]
    assert tl["ctl"] is None and tl["atl"] is None and tl["tsb"] is None
    assert tl["interpretation"] == "no training-load data yet"
    assert status["recent_workouts"] == []
    assert status["user_notes"] == []


def test_daily_snapshot_tool_empty_db(empty_db):
    result = asyncio.run(tools.daily_snapshot.handler({}))
    assert not result.get("is_error")
    payload = json.loads(result["content"][0]["text"])  # valid JSON, no raise
    assert payload["date"] == date.today().isoformat()
    assert "metrics" in payload


def test_assemble_status_honors_injected_today(empty_db):
    """An injected `today` drives both the snapshot date AND the trend-window
    cutoff (the latent bug: _metric_rows used wall-clock date.today())."""
    from datetime import date, timedelta

    target = (date.today() - timedelta(days=400)).isoformat()
    # steps is a PARTIAL_DAY_METRICS trend_arrow metric (Fix 8) — its "value"
    # anchors on the day BEFORE the injected `today`, never `today` itself, so
    # the row is seeded on target-1 to prove the window is relative to the
    # injected `today`, not wall-clock.
    yesterday = (date.fromisoformat(target) - timedelta(days=1)).isoformat()
    with db.connect(empty_db) as conn:
        conn.execute(
            "INSERT INTO daily_metrics (date, steps) VALUES (?, ?)", (yesterday, 8200)
        )
    status = assemble_status(today=target)
    assert status["date"] == target
    steps_row = next(m for m in status["metrics"] if m["metric"] == "steps")
    assert steps_row["value"] == 8200  # the row on target-1 was found in-window
    assert steps_row["partial_today_excluded"] is True


def test_assemble_status_defaults_to_today(empty_db):
    status = assemble_status()  # no arg → wall-clock, unchanged for bare callers
    assert status["date"] == date.today().isoformat()


def test_assemble_status_baseline_delta_and_workout(seeded_status_db):
    status = assemble_status()

    rhr_row = next(m for m in status["metrics"] if m["metric"] == "rhr")
    assert rhr_row["treatment"] == "baseline_delta"
    assert rhr_row["value"] == 55
    assert rhr_row["baseline"] == 50.0
    # (55 - 50) / 50 * 100 = +10.0
    assert rhr_row["delta_pct"] == 10.0
    assert rhr_row["arrow"] == "↑"

    assert status["recent_workouts"]
    w = status["recent_workouts"][0]
    # 10000 m → 6.21 mi (units display = miles)
    assert w["distance_mi"] == pytest.approx(6.21, abs=0.01)


# --------------------------------------------------------------------------- #
# Fix 3: status._recent_workouts mirrors tools._augment_workout's measured
# `effort` field (run/walk/null) — Garmin's activity_type label lies (a
# walking-desk session logs as treadmill_running), so this must be derived
# from pace, never the label, on this path too.
# --------------------------------------------------------------------------- #
def test_recent_workouts_effort_field_pins_walk_run_null(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    db.init_schema(p)
    today = date.today()
    from datetime import timedelta
    rows = [
        (1, (today - timedelta(days=1)).isoformat(), 1090.0),  # walk
        (2, (today - timedelta(days=2)).isoformat(), 333.0),   # run
        (3, (today - timedelta(days=3)).isoformat(), None),    # paceless
    ]
    with db.connect(p) as conn:
        for aid, d, pace in rows:
            conn.execute(
                "INSERT INTO activities (activity_id, date, start_time, "
                "activity_type, distance_meters, avg_pace_sec_per_km, "
                "duration_seconds) VALUES (?, ?, ?, 'treadmill_running', 5000, ?, 1800)",
                (aid, d, d + "T07:00:00", pace),
            )

    status = assemble_status()
    by_id = {w["activity_id"]: w for w in status["recent_workouts"]}
    assert by_id[1]["effort"] == "walk"
    assert by_id[2]["effort"] == "run"
    assert by_id[3]["effort"] is None
    # Additive — the mislabeled walk's activity_type is untouched.
    assert by_id[1]["activity_type"] == "treadmill_running"


def test_coach_prompt_renders_each_note_once(seeded_status_db):
    # Save exactly one user note via the real tool (writes to the env-pointed file).
    saved = asyncio.run(tools.save_user_note.handler({"note": "lead with the workout card"}))
    assert not saved.get("is_error")

    from mcp import types

    from local_fitness.web import mcp_server

    server = mcp_server.build_server()
    handler = server.request_handlers[types.GetPromptRequest]
    req = types.GetPromptRequest(
        method="prompts/get",
        params=types.GetPromptRequestParams(name="coach", arguments=None),
    )
    res = asyncio.run(handler(req))
    text = res.root.messages[0].content.text
    assert text.count("lead with the workout card") == 1


def test_coach_prompt_includes_output_formatting_contract(seeded_status_db):
    # The coach prompt must steer the model toward narrow/monospace-friendly
    # layouts so its reply renders cleanly in the MCP client.
    from mcp import types

    from local_fitness.web import mcp_server

    server = mcp_server.build_server()
    handler = server.request_handlers[types.GetPromptRequest]
    req = types.GetPromptRequest(
        method="prompts/get",
        params=types.GetPromptRequestParams(name="coach", arguments=None),
    )
    res = asyncio.run(handler(req))
    text = res.root.messages[0].content.text
    # The contract is inherited via the embedded system_prompt persona.
    assert "Formatting your chat replies" in text
    assert "NOT one wide grid" in text


# --- pure direction/slope/interpretation helpers ---------------------------

def test_arrow_directions():
    assert status_mod._arrow(1) == "↑"
    assert status_mod._arrow(-1) == "↓"
    assert status_mod._arrow(0) == "→"


def test_slope_arrow_too_few_points_returns_none():
    assert status_mod._slope_arrow([]) is None
    assert status_mod._slope_arrow([5.0]) is None


def test_slope_arrow_reads_trend_direction():
    assert status_mod._slope_arrow([1.0, 2.0, 3.0]) == "↑"
    assert status_mod._slope_arrow([3.0, 2.0, 1.0]) == "↓"
    assert status_mod._slope_arrow([2.0, 2.0, 2.0]) == "→"


def test_tsb_interpretation_all_bands():
    assert status_mod._tsb_interpretation(None) == "no training-load data yet"
    assert status_mod._tsb_interpretation(-25) == "very fatigued"
    assert status_mod._tsb_interpretation(-15) == "fatigued"
    assert status_mod._tsb_interpretation(10) == "fresh"
    assert status_mod._tsb_interpretation(0) == "neutral"


def test_tsb_interpretation_delegates_to_interpret_tsb_zone():
    """WS1: status._tsb_interpretation delegates to interpret.tsb_zone so
    the brief path and training_load_status agree by construction."""
    from local_fitness.agent import interpret

    for tsb in (None, -25, -20, -15, -10, 0, 5, 10):
        assert status_mod._tsb_interpretation(tsb) == interpret.tsb_zone(tsb)


# --- assemble_status: trend slope + pace-bearing workout -------------------

@pytest.fixture
def trend_status_db(tmp_path, monkeypatch):
    """Seeded with several days of trend metrics + a paced workout, so the
    trend-slope path (>=2 points) and the formatted-pace field both fire."""
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    db.init_schema(p)
    from datetime import timedelta

    today = date.today()
    with db.connect(p) as conn:
        # In date order (oldest→newest) steps DECLINE → a down slope arrow.
        # offset 0 is today; older days carry the higher counts.
        for offset, steps in enumerate((9000, 10000, 11000, 12000)):
            d = (today - timedelta(days=offset)).isoformat()
            conn.execute(
                "INSERT INTO daily_metrics (date, steps, sleep_score, max_stress) "
                "VALUES (?, ?, ?, ?)",
                (d, steps, 70 + offset, 40 - offset),
            )
        # Workout carries a real pace so the formatted pace field is emitted.
        conn.execute(
            "INSERT INTO activities (activity_id, date, start_time, activity_type, "
            "activity_name, duration_seconds, distance_meters, avg_hr, "
            "avg_pace_sec_per_km, training_load) "
            "VALUES (1, ?, ?, 'running', 'Paced Run', 1800, 5000, 150, 300.0, 50.0)",
            (today.isoformat(), today.isoformat() + "T07:00:00"),
        )
    return p


def test_assemble_status_trend_slope_and_pace(trend_status_db):
    status = assemble_status()

    steps_row = next(m for m in status["metrics"] if m["metric"] == "steps")
    assert steps_row["treatment"] == "trend_arrow"
    # Descending steps across the window → a downward slope arrow (covers
    # both the >=2-point slope path and the negative-direction arrow).
    assert steps_row["arrow"] == "↓"

    w = status["recent_workouts"][0]
    # 300 sec/km → ~8:03 min/mi; the formatted-pace field is present.
    assert "pace_min_per_mi" in w
    assert w["duration_formatted"] == "30:00"


def test_assemble_status_seeded_tsb_interpretation(seeded_status_db):
    # Current form comes from YESTERDAY's row (tsb=-5.0 -> "neutral"), not
    # today's projection (tsb=-7.8) — Fix 9.
    status = assemble_status()
    tl = status["training_load"]
    assert tl["tsb"] == -5.0
    assert tl["interpretation"] == "neutral"
    assert tl["current_form_date"] == (date.today() - timedelta(days=1)).isoformat()
    # Today's own row rides along separately, clearly labelled as a projection.
    assert tl["projected_end_of_day"]["tsb"] == -7.8
    assert tl["projected_end_of_day"]["ctl"] == 40.2


def test_training_load_current_baseline_is_not_stale(seeded_status_db):
    # The baselines row is dated today, so as_of == today and staleness is 0 —
    # the served CTL/ATL/TSB really is current.
    today = date.today().isoformat()
    tl = assemble_status()["training_load"]
    assert tl["as_of"] == today
    assert tl["baseline_stale_days"] == 0


def test_training_load_flags_a_frozen_data_frontier(seeded_status_db):
    # A frozen frontier (pulls failing / laptop off) leaves the newest baselines
    # row days behind. TSB decays daily even with no workouts, so the payload
    # must disclose that the CTL/ATL/TSB is 4 days old rather than present it as
    # today's freshness. The row is dated `today`; asking for the snapshot as of
    # 4 days later exercises exactly that gap.
    from datetime import timedelta

    row_date = date.today()
    as_of_query = (row_date + timedelta(days=4)).isoformat()
    tl = assemble_status(today=as_of_query)["training_load"]
    assert tl["as_of"] == row_date.isoformat()
    assert tl["baseline_stale_days"] == 4
    # The CTL/ATL/TSB numbers are still served (the disclosure rides alongside).
    # From 4 days out both fixture rows are "complete" — current form is the
    # MOST RECENT of the two (today's row, tsb=-7.8), not the older one.
    assert tl["current_form_date"] == row_date.isoformat()
    assert tl["tsb"] == -7.8


def test_training_load_empty_db_carries_null_staleness(empty_db):
    tl = assemble_status()["training_load"]
    assert tl["as_of"] is None
    assert tl["baseline_stale_days"] is None


# --------------------------------------------------------------------------- #
# Fix 9: _baseline_row_before + _training_load's current-form/projection split
# --------------------------------------------------------------------------- #
def test_baseline_row_before_excludes_todays_own_row(empty_db):
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    with db.connect(empty_db) as conn:
        conn.execute("INSERT INTO baselines (date, ctl) VALUES (?, 12.0)", (yesterday,))
        conn.execute("INSERT INTO baselines (date, ctl) VALUES (?, 99.0)", (today,))
        row = status_mod._baseline_row_before(conn, today)
    assert row["date"] == yesterday
    assert row["ctl"] == 12.0  # never today's 99.0


def test_baseline_row_before_falls_back_across_a_gap(empty_db):
    # No row exactly at yesterday — falls back to the latest one still < today.
    today = date.today().isoformat()
    three_ago = (date.today() - timedelta(days=3)).isoformat()
    with db.connect(empty_db) as conn:
        conn.execute("INSERT INTO baselines (date, ctl) VALUES (?, 33.0)", (three_ago,))
        row = status_mod._baseline_row_before(conn, today)
    assert row["date"] == three_ago
    assert row["ctl"] == 33.0


def test_baseline_row_before_none_when_only_todays_row_exists(empty_db):
    today = date.today().isoformat()
    with db.connect(empty_db) as conn:
        conn.execute("INSERT INTO baselines (date, ctl) VALUES (?, 50.0)", (today,))
        row = status_mod._baseline_row_before(conn, today)
    assert row is None


def test_training_load_reports_no_data_but_keeps_as_of_when_only_today_exists(
    empty_db,
):
    # A single-day-old DB: baseline exists (pipeline is NOT stale) but there is
    # no complete day yet, so current form is genuinely unavailable — the two
    # facts must not be conflated (a stale pipeline is a real alarm; "current
    # form always lags by a day" never is).
    today = date.today().isoformat()
    with db.connect(empty_db) as conn:
        conn.execute("INSERT INTO baselines (date, ctl, atl, tsb) VALUES (?, 40.0, 45.0, -5.0)", (today,))
    status = assemble_status()
    tl = status["training_load"]
    assert tl["tsb"] is None
    assert tl["interpretation"] == "no training-load data yet"
    assert tl["as_of"] == today          # pipeline freshness signal survives
    assert tl["baseline_stale_days"] == 0
    assert tl["projected_end_of_day"]["tsb"] == -5.0  # today's row still surfaced


# --- 3c: sleep_seconds row value_formatted/baseline_formatted --------------

@pytest.fixture
def sleep_status_db(tmp_path, monkeypatch):
    """Seeded with a sleep_seconds baseline mean so the sleep row's
    value_formatted/baseline_formatted (units.format_hm) fields fire."""
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    db.init_schema(p)
    today = date.today().isoformat()
    with db.connect(p) as conn:
        conn.execute(
            "INSERT INTO daily_metrics (date, sleep_seconds) VALUES (?, ?)",
            (today, 27180),
        )
        conn.execute(
            "INSERT INTO baselines (date, sleep_seconds_60day_mean, "
            "sleep_seconds_60day_sd) VALUES (?, 25500.0, 900.0)",
            (today,),
        )
    return p


def test_sleep_row_carries_formatted_hm_fields(sleep_status_db):
    status = assemble_status()
    sleep_row = next(m for m in status["metrics"] if m["metric"] == "sleep_seconds")
    assert sleep_row["value_formatted"] == "7h 33m"
    assert sleep_row["baseline_formatted"] == "7h 05m"


def test_non_sleep_baseline_delta_rows_have_no_formatted_fields(sleep_status_db):
    # rhr is baseline_delta too, but only sleep_seconds gets the hm treatment.
    status = assemble_status()
    rhr_row = next(m for m in status["metrics"] if m["metric"] == "rhr")
    assert "value_formatted" not in rhr_row
    assert "baseline_formatted" not in rhr_row


def test_sleep_row_cross_path_identity_with_brief_planner_hm(sleep_status_db):
    # 3c's cross-path invariant: brief_planner._hm and status._metric_rows'
    # value_formatted render the exact same seconds identically, by
    # construction (both delegate to units.format_hm).
    from local_fitness.agent import brief_planner

    status = assemble_status()
    sleep_row = next(m for m in status["metrics"] if m["metric"] == "sleep_seconds")
    assert sleep_row["value_formatted"] == brief_planner._hm(sleep_row["value"])
    assert sleep_row["baseline_formatted"] == brief_planner._hm(sleep_row["baseline"])


# --- brief freshness (2026-07-19 facet review: orphaned-sync detection) ------

def test_brief_freshness_reports_latest_date_and_staleness(empty_db, tmp_path, monkeypatch):
    from local_fitness.agent import briefs
    bdir = tmp_path / "briefings"
    bdir.mkdir()
    (bdir / "2026-07-14.json").write_text("{}", encoding="utf-8")
    (bdir / "2026-07-16.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(briefs, "DEFAULT_BRIEFINGS_DIR", bdir)
    s = assemble_status(today="2026-07-19")
    assert s["latest_brief_date"] == "2026-07-16"
    assert s["brief_stale_days"] == 3  # three mornings with no brief


def test_brief_freshness_zero_when_todays_brief_exists(empty_db, tmp_path, monkeypatch):
    from local_fitness.agent import briefs
    bdir = tmp_path / "briefings"
    bdir.mkdir()
    (bdir / "2026-07-19.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(briefs, "DEFAULT_BRIEFINGS_DIR", bdir)
    s = assemble_status(today="2026-07-19")
    assert s["latest_brief_date"] == "2026-07-19"
    assert s["brief_stale_days"] == 0


def test_brief_freshness_none_when_no_briefs(empty_db, tmp_path, monkeypatch):
    from local_fitness.agent import briefs
    monkeypatch.setattr(briefs, "DEFAULT_BRIEFINGS_DIR", tmp_path / "missing")
    s = assemble_status(today="2026-07-19")
    assert s["latest_brief_date"] is None
    assert s["brief_stale_days"] is None


def test_brief_freshness_ignores_non_date_junk_files(empty_db, tmp_path, monkeypatch):
    from local_fitness.agent import briefs
    bdir = tmp_path / "briefings"
    bdir.mkdir()
    (bdir / "2026-07-15.json").write_text("{}", encoding="utf-8")
    (bdir / "zzz-not-a-date.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(briefs, "DEFAULT_BRIEFINGS_DIR", bdir)
    s = assemble_status(today="2026-07-19")
    # The junk filename is skipped, not a poison pill — the real brief wins.
    assert s["latest_brief_date"] == "2026-07-15"
    assert s["brief_stale_days"] == 4


# --------------------------------------------------------------------------- #
# Fix 8: partial-day metrics anchor derived comparisons on YESTERDAY, not
# today. Measured live 2026-07-27: avg_stress read 17 off 50 overnight-only
# samples (00:00-02:27), narrated in the brief as "-47%, recovery holding"
# against a 32 baseline, when every complete day that week ran 24-32.
# --------------------------------------------------------------------------- #
def test_avg_stress_baseline_delta_anchors_on_yesterday_not_todays_partial_read(
    tmp_path, monkeypatch
):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    db.init_schema(p)
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    with db.connect(p) as conn:
        # Today: a partial overnight-only read. Yesterday: a real complete day.
        conn.execute("INSERT INTO daily_metrics (date, avg_stress) VALUES (?, ?)", (today, 17))
        conn.execute("INSERT INTO daily_metrics (date, avg_stress) VALUES (?, ?)", (yesterday, 30))
        conn.execute(
            "INSERT INTO baselines (date, stress_60day_mean) VALUES (?, 32.0)", (today,)
        )
    status = assemble_status()
    row = next(m for m in status["metrics"] if m["metric"] == "avg_stress")
    assert row["value"] == 30  # yesterday's complete reading, NOT today's partial 17
    # (30 - 32) / 32 * 100 = -6.25 -> rounds to -6.2 (banker's rounding), a
    # world away from the false -47% today's 17 would have produced.
    assert row["delta_pct"] == -6.2
    assert row["partial_today_excluded"] is True


def test_steps_trend_arrow_reads_rising_not_flat_when_today_is_null(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    db.init_schema(p)
    today = date.today()
    with db.connect(p) as conn:
        # A clear rising trend over the last 7 COMPLETE days; today has no row
        # yet (sync hasn't run). Including a phantom "today" gap must not
        # flatten the read.
        for i, steps in enumerate((8000, 9000, 10000, 11000, 12000, 13000, 14000)):
            d = (today - timedelta(days=7 - i)).isoformat()
            conn.execute("INSERT INTO daily_metrics (date, steps) VALUES (?, ?)", (d, steps))
    status = assemble_status()
    row = next(m for m in status["metrics"] if m["metric"] == "steps")
    assert row["value"] == 14000  # yesterday's value, not an absent today
    assert row["arrow"] == "↑"
    assert row["partial_today_excluded"] is True


def test_raw_treatment_cumulative_metric_keeps_todays_live_value_but_flags_it(
    tmp_path, monkeypatch
):
    # active_calories is in PARTIAL_DAY_METRICS but gets "raw" treatment (no
    # baseline/trend judgment exists to corrupt) — a live "how are my calories
    # today" answer must show TODAY's actual so-far number, not silently swap
    # to yesterday's, but should still say it's partial.
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    db.init_schema(p)
    today = date.today().isoformat()
    with db.connect(p) as conn:
        conn.execute(
            "INSERT INTO daily_metrics (date, active_calories) VALUES (?, ?)", (today, 120)
        )
    status = assemble_status()
    row = next(m for m in status["metrics"] if m["metric"] == "active_calories")
    assert row["treatment"] == "raw"
    assert row["value"] == 120  # today's live partial number, NOT excluded
    assert row["partial_today"] is True


def test_non_partial_metrics_unaffected_rhr_and_sleep(tmp_path, monkeypatch):
    # rhr and sleep_seconds are NOT in PARTIAL_DAY_METRICS — same-morning
    # readings are legitimately complete (sleep_score/rhr land at wake time),
    # and this fix must not touch them.
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    monkeypatch.setenv("LOCAL_FITNESS_NOTES_PATH", str(tmp_path / "user_notes.md"))
    db.init_schema(p)
    today = date.today().isoformat()
    with db.connect(p) as conn:
        conn.execute(
            "INSERT INTO daily_metrics (date, rhr, sleep_seconds) VALUES (?, ?, ?)",
            (today, 55, 27000),
        )
        conn.execute(
            "INSERT INTO baselines (date, rhr_60day_mean, sleep_seconds_60day_mean) "
            "VALUES (?, 50.0, 26000.0)", (today,),
        )
    status = assemble_status()
    rhr_row = next(m for m in status["metrics"] if m["metric"] == "rhr")
    sleep_row = next(m for m in status["metrics"] if m["metric"] == "sleep_seconds")
    assert rhr_row["value"] == 55  # today's own reading, unchanged
    assert "partial_today_excluded" not in rhr_row
    assert sleep_row["value"] == 27000
    assert "partial_today_excluded" not in sleep_row


# --------------------------------------------------------------------------- #
# 0.59.0: the settling guard is OPT-IN (settling_guard=True) — the brief
# pipeline pulls immediately before reading, so its snapshot is fresh by
# construction and must stay byte-identical (its output feeds the eval'd
# grounding pool). Only the daily_snapshot MCP tool opts in.
# --------------------------------------------------------------------------- #
def _stamp_status_pull(p, completed_at: str, last_date_fetched: str):
    with db.connect(p) as conn:
        conn.execute(
            "INSERT INTO ingest_runs (started_at, completed_at, status, "
            "last_date_fetched, source) VALUES (?, ?, 'success', ?, 'daily')",
            (completed_at, completed_at, last_date_fetched),
        )


def test_settling_guard_defaults_off_so_the_brief_pipeline_is_unchanged(
    seeded_status_db,
):
    # No ingest_runs rows at all — maximally stale — and the UNGUARDED call
    # still reports today's rhr (55) with a computed delta and no provisional
    # keys anywhere. This is the brief-pipeline / eval-fixture contract.
    status = assemble_status()
    rhr_row = next(r for r in status["metrics"] if r["metric"] == "rhr")
    assert rhr_row["value"] == 55
    assert rhr_row["delta_pct"] == 10.0  # (55 - 50) / 50
    assert "provisional_today_excluded" not in rhr_row
    assert "provisional_today" not in rhr_row
    assert "data_as_of" not in status


def test_settling_guard_stale_anchors_rhr_to_yesterday(seeded_status_db):
    # seeded_status_db has no yesterday daily_metrics row, so the guarded
    # stale read reports value=None (nothing settled to report) — never
    # today's unsettled 55 — with the raw snapshot kept, labeled.
    status = assemble_status(settling_guard=True)
    rhr_row = next(r for r in status["metrics"] if r["metric"] == "rhr")
    assert rhr_row["provisional_today_excluded"] is True
    assert rhr_row["provisional_today_value"] == 55
    assert rhr_row["value"] is None
    assert rhr_row["delta_pct"] is None
    assert "data_as_of" not in status


def test_settling_guard_fresh_keeps_today_labeled(seeded_status_db):
    from datetime import datetime

    now = datetime.now().isoformat()
    _stamp_status_pull(seeded_status_db, now, date.today().isoformat())
    status = assemble_status(settling_guard=True)
    rhr_row = next(r for r in status["metrics"] if r["metric"] == "rhr")
    assert rhr_row["value"] == 55
    assert rhr_row["delta_pct"] == 10.0
    assert rhr_row["provisional_today"] is True
    assert "provisional_today_excluded" not in rhr_row
    assert status["data_as_of"] == now


def test_settling_guard_leaves_partial_tallies_alone(seeded_status_db):
    # steps is a PARTIAL_DAY tally — its treatment (yesterday anchor,
    # partial_today_excluded) predates the guard and must not change under it.
    status = assemble_status(settling_guard=True)
    steps_row = next(r for r in status["metrics"] if r["metric"] == "steps")
    assert steps_row["partial_today_excluded"] is True
    assert "provisional_today_excluded" not in steps_row
    assert "provisional_today" not in steps_row
