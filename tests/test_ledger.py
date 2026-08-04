"""The deterministic relationship ledger — pure functions on fabricated dicts
(never real data, per CLAUDE.md) plus one divider integration case."""
from __future__ import annotations

from datetime import date, timedelta

from local_fitness import db
from local_fitness.agent import ledger

TODAY = "2026-07-23"


def _w(day: str, verdict: str, wtype: str = "easy", seq: int = 1,
       target_m: float | None = None, actual_m: float | None = None,
       run_m: float | None = None, walk_m: float | None = None) -> dict:
    return {"date": day, "verdict": verdict, "type": wtype, "seq": seq,
            "target_distance_m": target_m, "actual_distance_m": actual_m,
            "actual_run_distance_m": run_m, "actual_walk_distance_m": walk_m}


# --- plan_adherence_facts ---------------------------------------------------


def test_plan_facts_empty_graded_list_is_all_zero():
    facts = ledger.plan_adherence_facts([], TODAY)
    assert facts == {
        "miss_streak": 0, "done_streak": 0, "misses_14d": 0,
        "quality_misses_28d": 0, "last_miss": None,
    }


def test_plan_facts_miss_streak_counts_consecutive_misses():
    graded = [
        _w("2026-07-20", "missed", "interval"),
        _w("2026-07-21", "missed"),
        _w("2026-07-22", "missed"),
    ]
    facts = ledger.plan_adherence_facts(graded, TODAY)
    assert facts["miss_streak"] == 3
    assert facts["done_streak"] == 0
    assert facts["last_miss"] == {"date": "2026-07-22", "type": "easy"}


def test_plan_facts_rest_days_are_neutral_partial_breaks_both():
    graded = [
        _w("2026-07-18", "done"),
        _w("2026-07-19", "compliant", "rest"),   # rest: neither breaks nor counts
        _w("2026-07-20", "done"),
        _w("2026-07-21", "compliant", "rest"),
        _w("2026-07-22", "done"),
    ]
    assert ledger.plan_adherence_facts(graded, TODAY)["done_streak"] == 3

    graded.insert(2, _w("2026-07-19", "partial", seq=2))
    facts = ledger.plan_adherence_facts(graded, TODAY)
    assert facts["done_streak"] == 2  # partial on the 19th stops the walk


def test_plan_facts_pending_and_future_workouts_are_ignored():
    graded = [
        _w("2026-07-22", "done"),
        _w("2026-07-23", "pending"),
        _w("2026-07-25", "missed"),   # future relative to TODAY
    ]
    facts = ledger.plan_adherence_facts(graded, TODAY)
    assert facts["done_streak"] == 1
    assert facts["misses_14d"] == 0
    assert facts["last_miss"] is None


def test_plan_facts_window_edges_and_quality_counting():
    graded = [
        _w("2026-07-09", "missed", "tempo"),     # 14 days back — outside 14d
        _w("2026-07-10", "missed", "interval"),  # 13 days back — inside
        _w("2026-06-25", "missed", "race"),      # 28 days back — outside 28d
        _w("2026-06-26", "missed", "tempo"),     # 27 days back — inside
        _w("2026-07-22", "done"),
    ]
    facts = ledger.plan_adherence_facts(graded, TODAY)
    assert facts["misses_14d"] == 1
    # quality misses: interval(7-10) + tempo(6-26) inside 28d; tempo(7-09) also
    # inside 28d though outside 14d
    assert facts["quality_misses_28d"] == 3


# --- step_streak_facts ------------------------------------------------------


def _steps(days_back_to_steps: dict[int, int], today: str = TODAY) -> list[dict]:
    t = date.fromisoformat(today)
    return [
        {"date": (t - timedelta(days=back)).isoformat(), "steps": steps}
        for back, steps in days_back_to_steps.items()
    ]


def test_step_facts_hit_streak_through_yesterday_excludes_today():
    rows = _steps({0: 500, 1: 10000, 2: 10500, 3: 12000, 4: 4000})
    facts = ledger.step_streak_facts(rows, 10000, TODAY)
    # Today's 500 (a partial count) must not read as a miss.
    assert facts["current_hit_streak"] == 3
    assert facts["current_miss_streak"] == 0
    assert facts["streak_ended"] is None


def test_step_facts_exactly_at_goal_counts_as_hit():
    rows = _steps({1: 10000})
    assert ledger.step_streak_facts(rows, 10000, TODAY)["current_hit_streak"] == 1


def test_step_facts_streak_ended_needs_three_hits_before_the_miss():
    # 12-day streak, then 2 misses ending yesterday.
    rows = _steps({b: 4000 for b in (1, 2)} | {b: 11000 for b in range(3, 15)})
    facts = ledger.step_streak_facts(rows, 10000, TODAY)
    assert facts["current_miss_streak"] == 2
    assert facts["streak_ended"] == {"date": "2026-07-21", "length": 12}

    # Only 2 hits before the miss — too short to mourn.
    short = _steps({1: 4000, 2: 11000, 3: 11000})
    assert ledger.step_streak_facts(short, 10000, TODAY)["streak_ended"] is None


def test_step_facts_gap_in_data_breaks_a_streak():
    rows = _steps({1: 11000, 2: 11000, 4: 11000})  # day 3 missing
    assert ledger.step_streak_facts(rows, 10000, TODAY)["current_hit_streak"] == 2


def test_step_facts_best_streak_and_degenerate_inputs():
    rows = _steps({b: 11000 for b in range(1, 6)} | {6: 100}
                  | {b: 11000 for b in range(7, 15)})
    facts = ledger.step_streak_facts(rows, 10000, TODAY)
    assert facts["current_hit_streak"] == 5
    assert facts["best_streak_60d"] == 8
    assert ledger.step_streak_facts([], 10000, TODAY)["best_streak_60d"] == 0
    assert ledger.step_streak_facts(rows, 0, TODAY)["current_hit_streak"] == 0


# --- observation_patterns ---------------------------------------------------


def _obs(day: str, obs_type: str, num: float | None = None,
         text: str | None = None) -> dict:
    return {"observed_on": day, "obs_type": obs_type,
            "value_num": num, "value_text": text}


def test_observation_patterns_thresholds_are_edges():
    rows = [
        _obs("2026-07-20", "mood", 2),    # counts (<=2)
        _obs("2026-07-21", "mood", 2),
        _obs("2026-07-22", "mood", 3),    # does not count
        _obs("2026-07-20", "soreness", 7),  # counts (>=7)
        _obs("2026-07-21", "soreness", 6),  # does not
    ]
    patterns = {p["pattern"]: p for p in ledger.observation_patterns(rows, TODAY)}
    assert patterns["low_mood"]["count"] == 2
    assert patterns["low_mood"]["last_date"] == "2026-07-21"
    assert "high_soreness" not in patterns  # single reading is not a pattern


def test_observation_patterns_injury_counts_from_one():
    rows = [_obs("2026-07-22", "injury", text="right knee")]
    patterns = ledger.observation_patterns(rows, TODAY)
    assert patterns == [{
        "pattern": "injury_logged", "obs_type": "injury", "count": 1,
        "window_days": 30, "last_date": "2026-07-22",
    }]


def test_observation_patterns_window_excludes_day_30_and_future():
    rows = [
        _obs("2026-06-23", "injury", text="old"),    # exactly 30 back — out
        _obs("2026-07-25", "injury", text="future"),  # future — out
    ]
    assert ledger.observation_patterns(rows, TODAY) == []


# --- notable_results --------------------------------------------------------


def test_notables_quality_done_and_overachieve_boundary():
    graded = [
        _w("2026-07-21", "done", "interval"),
        _w("2026-07-20", "done", "easy", target_m=5000, actual_m=5500),   # exactly 1.10
        _w("2026-07-19", "done", "easy", target_m=5000, actual_m=5400),   # under
        _w("2026-07-18", "missed", "tempo"),
        _w("2026-07-01", "done", "race"),  # 22 days back — outside 14d
    ]
    notables = ledger.notable_results(graded, TODAY)
    assert notables == [
        {"date": "2026-07-21", "type": "interval", "kind": "quality_done"},
        {"date": "2026-07-20", "type": "easy", "kind": "overachieved"},
    ]


# --- notable_results: suppressing a false "done as prescribed" receipt ------
# A quality day's plan verdict is distance-based ("done" = target distance
# hit), but a graded report card can still say the execution was a D/F (pace,
# HR, load blown). Promoting that "done" as a receipt hands the coach a false
# callback that contradicts its own report-card memory (0.38.2 fix).


def test_notables_suppresses_quality_done_when_same_date_card_rates_badly():
    graded = [_w("2026-07-21", "done", "interval")]
    cards = [_card("2026-07-21", 1.8)]
    assert ledger.notable_results(graded, TODAY, cards=cards) == []


def test_notables_suppresses_quality_done_for_a_floored_card_too():
    graded = [_w("2026-07-21", "done", "tempo")]
    cards = [_card("2026-07-21", 1.0)]
    assert ledger.notable_results(graded, TODAY, cards=cards) == []


def test_notables_keeps_quality_done_when_same_date_card_rates_well():
    graded = [_w("2026-07-21", "done", "interval")]
    cards = [_card("2026-07-21", 4.6)]
    assert ledger.notable_results(graded, TODAY, cards=cards) == [
        {"date": "2026-07-21", "type": "interval", "kind": "quality_done"},
    ]


def test_notables_keeps_quality_done_when_no_card_for_that_date():
    graded = [_w("2026-07-21", "done", "interval")]
    # A card exists, but for a different date — must not suppress.
    cards = [_card("2026-07-20", 1.5)]
    assert ledger.notable_results(graded, TODAY, cards=cards) == [
        {"date": "2026-07-21", "type": "interval", "kind": "quality_done"},
    ]
    # No cards at all — today's behavior, unaffected.
    assert ledger.notable_results(graded, TODAY) == [
        {"date": "2026-07-21", "type": "interval", "kind": "quality_done"},
    ]


# --- notable_results: overachievement is gated on RUN distance, not the ---
# --- on-foot total (Fix 4b) -------------------------------------------------
# Plan adherence's ``actual_distance_m`` is the on-foot total (run + walk).
# Grading "overachieved" from it lets a large WALK on an easy day (or a
# separate walking session that day) manufacture a false "run past its
# target" receipt on a day the run matched its target exactly, or on a day
# with no running at all.


def test_notables_no_overachieve_when_run_matches_target_but_walk_is_large():
    """Real evidence, 2026-07-26: a 6436.3m run @ 8:56/mi matched its
    6437.376m target to within ~1m, plus a separate 4069.9m walk that day.
    actual_distance_m (foot total) = 10506.2m -> 1.63x target, which used to
    fire 'run past its target'. The run itself did not overachieve."""
    graded = [_w("2026-07-26", "done", "easy", target_m=6437.376,
                  actual_m=10506.2, run_m=6436.3, walk_m=4069.9)]
    assert ledger.notable_results(graded, "2026-07-27") == []


def test_notables_overachieve_still_fires_on_real_run_overachievement():
    graded = [_w("2026-07-26", "done", "easy", target_m=6437.376,
                  actual_m=10506.2, run_m=7100.0, walk_m=3406.2)]  # run alone >= 1.10x
    assert ledger.notable_results(graded, "2026-07-27") == [
        {"date": "2026-07-26", "type": "easy", "kind": "overachieved"},
    ]


def test_notables_no_overachieve_on_a_pure_walk_day():
    """2026-07-11 evidence: a single 4798.9m walk (zero running) against a
    4023.36m easy target = 1.19x on the foot total. A day with no running at
    all must never earn 'run past its target'."""
    graded = [_w("2026-07-26", "done", "easy", target_m=4023.36,
                  actual_m=4798.9, run_m=0.0, walk_m=4798.9)]
    assert ledger.notable_results(graded, "2026-07-27") == []


def test_notables_overachieve_falls_back_to_actual_distance_without_the_split():
    """Rows with no run/walk split (older/synthetic data, no
    actual_run_distance_m key set) fall back to actual_distance_m, same as
    tools.weekly_rollup's established fallback — unaffected regression."""
    graded = [_w("2026-07-20", "done", "easy", target_m=5000, actual_m=5500)]
    assert ledger.notable_results(graded, TODAY) == [
        {"date": "2026-07-20", "type": "easy", "kind": "overachieved"},
    ]


def test_notables_suppression_is_scoped_to_quality_done_not_overachieved():
    """An overachieved easy day isn't the receipt this suppresses — it isn't
    claiming 'done as prescribed' the way a quality day's callback does."""
    graded = [_w("2026-07-20", "done", "easy", target_m=5000, actual_m=5500)]
    cards = [_card("2026-07-20", 1.5)]
    assert ledger.notable_results(graded, TODAY, cards=cards) == [
        {"date": "2026-07-20", "type": "easy", "kind": "overachieved"},
    ]


# --- render_ledger_block ----------------------------------------------------


def test_render_empty_ledger_is_empty_string():
    led = ledger.compute_ledger(
        ledger.plan_adherence_facts([], TODAY),
        ledger.step_streak_facts([], 10000, TODAY),
        [], [], TODAY)
    assert ledger.render_ledger_block(led, "Alex") == ""


def test_render_ledger_pins_the_receipt_lines():
    led = {
        "as_of": TODAY,
        "plan": {"miss_streak": 0, "done_streak": 0, "misses_14d": 2,
                 "quality_misses_28d": 1,
                 "last_miss": {"date": "2026-07-19", "type": "interval"}},
        "steps": {"current_hit_streak": 0, "current_miss_streak": 2,
                  "best_streak_60d": 12,
                  "streak_ended": {"date": "2026-07-20", "length": 12}},
        "patterns": [{"pattern": "high_soreness", "obs_type": "soreness",
                      "count": 4, "window_days": 30, "last_date": "2026-07-22"}],
        "notables": [{"date": "2026-07-21", "type": "interval",
                      "kind": "quality_done"}],
    }
    block = ledger.render_ledger_block(led, "Alex")
    assert "- Plan: 2 missed sessions in the last 14 days (last: Jul 19 interval)." in block
    assert "- Quality days: 1 skipped in the last 28 days." in block
    assert "- Steps: a 12-day goal streak ended Jul 20." in block
    assert "- Soreness logged at 7/10 or above 4x in 30 days (last: Jul 22)." in block
    assert "- Jul 21: interval day done as prescribed." in block


def test_render_ledger_streak_line_variants():
    hit = {"current_hit_streak": 5, "current_miss_streak": 0,
           "best_streak_60d": 9, "streak_ended": None}
    block = ledger.render_ledger_block(
        {"as_of": TODAY, "plan": {}, "steps": hit, "patterns": [],
         "notables": []}, "Alex")
    assert "goal hit 5 days running (through yesterday); best in 60 days is 9." in block

    streaky = {"miss_streak": 3, "done_streak": 0, "misses_14d": 3,
               "quality_misses_28d": 0, "last_miss": {"date": "2026-07-22",
                                                      "type": "easy"}}
    block = ledger.render_ledger_block(
        {"as_of": TODAY, "plan": streaky, "steps": {}, "patterns": [],
         "notables": []}, "Alex")
    assert "- Plan: 3 prescribed sessions missed in a row." in block


# --- persistence divider ----------------------------------------------------


def test_compute_relationship_ledger_from_a_seeded_db(tmp_path, monkeypatch):
    """The divider wires real tables to the pure functions: steps rows and an
    observation land in the ledger with the values pinned."""
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    today = date.today()
    with db.connect(p) as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('daily_step_goal', '8000')")
        for back in range(1, 5):
            conn.execute(
                "INSERT INTO daily_metrics (date, steps) VALUES (?, ?)",
                ((today - timedelta(days=back)).isoformat(), 9000),
            )
        conn.execute(
            "INSERT INTO observations (observed_on, created_at, obs_type, "
            "value_text) VALUES (?, ?, 'injury', 'left calf')",
            ((today - timedelta(days=2)).isoformat(), "2026-07-21T08:00:00"),
        )
    led = ledger.compute_relationship_ledger(db_path=p)
    assert led["steps"]["current_hit_streak"] == 4
    assert led["patterns"][0]["pattern"] == "injury_logged"
    assert led["plan"] == ledger.plan_adherence_facts([], led["as_of"])
    assert "goal hit 4 days running" in ledger.render_ledger_block(led, "Alex")


# --- report_card_facts -------------------------------------------------------


def _card(day: str, stars: float | None) -> dict:
    return {"activity_date": day, "overall_stars": stars}


def test_card_facts_empty_is_zeroed():
    facts = ledger.report_card_facts([], TODAY)
    assert facts == {
        "count": 0, "mean_stars": None, "verdict_counts": {},
        "trend": "no data", "window_days": ledger._CARD_WINDOW_DAYS,
    }
    led = ledger.compute_ledger(
        ledger.plan_adherence_facts([], TODAY),
        ledger.step_streak_facts([], 10000, TODAY),
        [], [], TODAY, card_facts=facts)
    assert "Report cards" not in ledger.render_ledger_block(led, "Alex")


def test_card_facts_excludes_today_and_future():
    cards = [
        _card(TODAY, 5.0),                     # today: out
        _card("2026-07-24", 5.0),              # tomorrow: out
        _card("2026-07-22", 4.0),              # yesterday: in
        _card("2026-07-21", 3.0),              # 2 days back: in
    ]
    facts = ledger.report_card_facts(cards, TODAY)
    assert facts["count"] == 2
    assert facts["mean_stars"] == 3.5


def test_card_facts_window_edge_day21_in_day22_out():
    t = date.fromisoformat(TODAY)
    edge_in = (t - timedelta(days=ledger._CARD_WINDOW_DAYS)).isoformat()
    edge_out = (t - timedelta(days=ledger._CARD_WINDOW_DAYS + 1)).isoformat()
    facts = ledger.report_card_facts(
        [_card(edge_in, 4.0), _card(edge_out, 5.0)], TODAY)
    assert facts["count"] == 1
    assert facts["mean_stars"] == 4.0


def test_card_facts_mean_stars_and_verdict_counts():
    cards = [
        _card("2026-07-22", 4.4),
        _card("2026-07-21", 3.8),
        _card("2026-07-20", None),        # no score: skipped entirely
        _card("2026-07-18", 4.95),
    ]
    facts = ledger.report_card_facts(cards, TODAY)
    assert facts["count"] == 3
    assert facts["mean_stars"] == round((4.4 + 3.8 + 4.95) / 3, 2)
    # Bucketed by VERDICT WORD, not by quarter star: this line is read aloud.
    assert facts["verdict_counts"] == {
        "on target": 1, "slightly off target": 1, "dead on": 1}


def test_a_pre_star_card_is_skipped_not_synthesized():
    """The 0.50.0 cutover, handled by the existing skip rather than a migration.

    A card stored under the letter rubric has overall_stars NULL. Inventing a
    star score from its stored letter would mix two scales inside one mean —
    the exact category error this module keeps getting burned by — so it drops
    out until the card is re-rendered.
    """
    legacy = {"activity_date": "2026-07-22", "gpa": 4.0, "overall_grade": "A",
              "overall_stars": None}
    assert ledger.report_card_facts([legacy], TODAY)["count"] == 0
    assert ledger.report_card_facts(
        [legacy, _card("2026-07-21", 3.0)], TODAY)["mean_stars"] == 3.0


def test_card_facts_trend_halves_and_min_count():
    # Recent half (days 1-10 back) averages well above earlier (11-21 back).
    recent = [_card(f"2026-07-{22 - i:02d}", 4.8) for i in range(3)]
    earlier = [_card(f"2026-07-{10 - i:02d}", 3.0) for i in range(3)]
    facts = ledger.report_card_facts(recent + earlier, TODAY)
    assert facts["trend"] == "rising"

    flat = ([_card("2026-07-22", 4.0)] * 2
            + [_card("2026-07-10", 4.0)] * 2)
    assert ledger.report_card_facts(flat, TODAY)["trend"] == "flat"

    # Only 1 card in the earlier half: under-populated, no trend claimed.
    under = [_card("2026-07-22", 4.0), _card("2026-07-21", 4.5),
              _card("2026-07-10", 3.0)]
    assert ledger.report_card_facts(under, TODAY)["trend"] == "no data"


def test_render_ledger_pins_the_card_line():
    facts = {"count": 5, "mean_stars": 4.12,
              "verdict_counts": {"on target": 2, "slightly off target": 2,
                                 "missed badly": 1},
              "trend": "rising", "window_days": 21}
    led = {"as_of": TODAY, "plan": {}, "steps": {}, "patterns": [],
           "notables": [], "cards": facts}
    block = ledger.render_ledger_block(led, "Alex")
    assert ("- Report cards: 5 workouts rated in the last 3 weeks "
            "(through yesterday) — avg 4.12 of 5 (2 on target, 2 slightly off "
            "target, 1 missed badly); ratings rising.") in block

    # Below the render floor: no line at all.
    one = {"count": 1, "mean_stars": 5.0, "verdict_counts": {"dead on": 1},
           "trend": "no data", "window_days": 21}
    led_one = {"as_of": TODAY, "plan": {}, "steps": {}, "patterns": [],
               "notables": [], "cards": one}
    assert "Report cards" not in ledger.render_ledger_block(led_one, "Alex")


def test_compute_relationship_ledger_reads_report_cards_table(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    today = date.today()

    def _insert(conn, back, stars):
        d = (today - timedelta(days=back)).isoformat()
        conn.execute(
            "INSERT INTO report_cards (activity_id, activity_date, graded_at, "
            "overall_stars, card_json) VALUES (?, ?, ?, ?, '{}')",
            (1000 + back, d, "2026-07-01T00:00:00", stars),
        )

    with db.connect(p) as conn:
        _insert(conn, 0, 5.0)        # today: must be excluded
        _insert(conn, 1, 4.0)
        _insert(conn, 2, 4.5)
        _insert(conn, 3, 3.0)

    led = ledger.compute_relationship_ledger(db_path=p)
    assert led["cards"]["count"] == 3
    assert "Report cards: 3 workouts rated" in ledger.render_ledger_block(led, "Alex")


def test_compute_relationship_ledger_wires_cards_into_notables(tmp_path, monkeypatch):
    """Integration: the divider passes the already-loaded report_cards rows
    into notable_results (no second DB round trip), so a same-date badly-rated
    card suppresses the false 'done as prescribed' receipt end-to-end."""
    from local_fitness import plans as plans_mod

    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    workout_date = (date.today() - timedelta(days=1)).isoformat()

    monkeypatch.setattr(plans_mod, "get_active_plan", lambda conn=None: {"workouts": []})
    monkeypatch.setattr(
        plans_mod, "load_activities_by_date",
        lambda start, end, conn=None: {})
    monkeypatch.setattr(plans_mod, "resolve_grading_config", lambda conn=None: None)
    monkeypatch.setattr(
        plans_mod, "build_plan_detail",
        lambda active, frontier, activities, cfg=None: {
            "workouts": [_w(workout_date, "done", "interval")]})

    with db.connect(p) as conn:
        conn.execute(
            "INSERT INTO report_cards (activity_id, activity_date, graded_at, "
            "overall_stars, card_json) VALUES (?, ?, ?, ?, '{}')",
            (5001, workout_date, "2026-07-01T00:00:00", ledger.BAD_CARD_MAX_STARS),
        )

    led = ledger.compute_relationship_ledger(db_path=p)
    assert led["notables"] == []


def test_ledger_survives_missing_report_cards_table(tmp_path, monkeypatch):
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    with db.connect(p) as conn:
        conn.execute("DROP TABLE report_cards")
    led = ledger.compute_relationship_ledger(db_path=p)
    assert led["cards"]["count"] == 0
    assert led["cards"]["trend"] == "no data"
