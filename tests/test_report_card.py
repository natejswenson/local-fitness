"""Rubric tests for agent/report_card.py.

The whole point of the module is that a grade is derived, not phrased — so
these tests pin the derivation. Everything above the persistence divider is
pure, so most of this file needs no DB at all.

The headline test is `test_easy_run_slower_than_expected_is_not_penalized`:
without direction gating, every recovery run fails on pace.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from local_fitness import db
from local_fitness.agent import report_card as rc

# A reference stand-in with round numbers, so every expectation below is
# arithmetic a reader can check in their head.
REF = {
    "mode": "rolling_60d",
    "n": 20,
    "pool": "running",
    "median_distance_m": 10000.0,
    "median_pace_sec_per_km": 300.0,   # 5:00/km
    "median_hr": 150.0,
    "median_load": 100.0,
}


def card_for(activity, *, plan=None, reference=REF, splits=()):
    return rc.build_card(activity, list(splits), plan, reference)


# --- the band table --------------------------------------------------------

@pytest.mark.parametrize("d,expected", [
    (0.0, "A"), (0.05, "A"), (0.0500000001, "A"),   # boundary lands in A
    (0.06, "B"), (0.10, "B"),
    (0.11, "C"), (0.20, "C"),
    (0.21, "D"), (0.35, "D"),
    (0.36, "F"), (5.0, "F"),
])
def test_grade_band_boundaries(d, expected):
    assert rc.base_letter(rc.grade_from_deviation(d)) == expected


def test_grade_none_in_none_out():
    assert rc.grade_from_deviation(None) is None


def test_grade_modifier_tracks_position_in_band():
    # A band spans (0, 0.05]: bottom third "+", top third "-".
    assert rc.grade_from_deviation(0.005) == "A+"
    assert rc.grade_from_deviation(0.025) == "A"
    assert rc.grade_from_deviation(0.048) == "A-"
    # F is unbounded above, so it carries no modifier.
    assert rc.grade_from_deviation(2.0) == "F"


def test_widen_scales_every_boundary():
    # 0.07 is a B normally; widened 1.5x the A band reaches 0.075.
    assert rc.base_letter(rc.grade_from_deviation(0.07)) == "B"
    assert rc.base_letter(rc.grade_from_deviation(0.07, rc.STEADY_WIDEN)) == "A"


def test_base_letter_strips_modifier():
    assert rc.base_letter("B+") == "B"
    assert rc.base_letter("D-") == "D"
    assert rc.base_letter(None) is None


# --- distance: two-sided under a plan, one-sided under the median ----------

def test_plan_distance_is_two_sided():
    """Overshooting a prescription costs the same as undershooting it — a
    12-miler on a 10-mile plan is over-cooking the plan."""
    over = rc.distance_deviation(12000, 10000, two_sided=True)
    under = rc.distance_deviation(8000, 10000, two_sided=True)
    assert over == pytest.approx(under) == pytest.approx(0.2)


def test_rolling_distance_is_one_sided_low():
    """Going longer than your norm is never a penalty."""
    assert rc.distance_deviation(15000, 10000, two_sided=False) == 0.0
    assert rc.distance_deviation(6000, 10000, two_sided=False) == pytest.approx(0.4)


def test_distance_deviation_guards_nulls_and_zero():
    assert rc.distance_deviation(None, 10000, two_sided=True) is None
    assert rc.distance_deviation(10000, None, two_sided=True) is None
    assert rc.distance_deviation(10000, 0, two_sided=True) is None


# --- pace: the recovery-run problem ---------------------------------------

def test_easy_run_slower_than_expected_is_not_penalized():
    """THE headline case. An easy run 12% slower than the easy expectation is
    exactly what an easy run should be. Without direction gating this is an F.
    """
    expected = rc.PACE_FACTORS["easy"] * REF["median_pace_sec_per_km"]  # 330
    actual = expected * 1.12
    assert rc.pace_deviation(actual, expected, "easy") == 0.0
    assert rc.base_letter(rc.grade_from_deviation(
        rc.pace_deviation(actual, expected, "easy"))) == "A"


def test_easy_run_too_fast_is_penalized():
    """The gate is directional, not merely disabled — running the easy day
    hard is still a miss."""
    expected = 330.0
    d = rc.pace_deviation(expected * 0.80, expected, "easy")
    assert d == pytest.approx(0.20)
    assert rc.base_letter(rc.grade_from_deviation(d)) == "C"


def test_quality_run_faster_than_target_is_an_A_uncapped():
    assert rc.pace_deviation(240.0, 300.0, "quality") == 0.0
    assert rc.pace_deviation(1.0, 300.0, "quality") == 0.0


def test_quality_run_too_slow_is_penalized():
    d = rc.pace_deviation(360.0, 300.0, "quality")
    assert d == pytest.approx(0.2)


def test_steady_pace_is_two_sided():
    assert rc.pace_deviation(330.0, 300.0, "steady") == pytest.approx(0.1)
    assert rc.pace_deviation(270.0, 300.0, "steady") == pytest.approx(0.1)


def test_by_feel_plan_day_gets_no_pace_grade():
    """A prescribed distance with a null target pace is explicitly by-feel.
    It must not be graded, and its weight must redistribute."""
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 10000, "avg_pace_sec_per_km": 400,
         "avg_hr": 140, "training_load": 90},
        plan={"type": "easy", "target_distance_m": 10000,
              "target_pace_sec_per_km": None, "seq": 1},
    )
    assert card["metrics"]["pace"]["grade"] is None
    assert card["metrics"]["pace"]["note"]
    assert card["overall"]["graded_metrics"] == 3


# --- HR: appropriateness to intent, never "lower is better" ---------------

def test_easy_run_above_hr_ceiling_is_downgraded():
    # ceiling 0.88 * 150 = 132
    assert rc.hr_deviation(132, 150, "easy") == 0.0
    assert rc.hr_deviation(120, 150, "easy") == 0.0   # comfortably under: fine
    assert rc.hr_deviation(145, 150, "easy") > 0.0


def test_quality_run_below_hr_floor_is_downgraded():
    # floor 1.02 * 150 = 153
    assert rc.hr_deviation(160, 150, "quality") == 0.0
    assert rc.hr_deviation(130, 150, "quality") > 0.0


def test_same_hr_grades_differently_under_different_intents():
    """The core claim: HR is meaningless without intent. 160bpm is a great
    quality day and a blown easy day."""
    easy = rc.hr_deviation(160, 150, "easy")
    quality = rc.hr_deviation(160, 150, "quality")
    assert easy > 0.0
    assert quality == 0.0


def test_steady_hr_band_is_two_sided():
    assert rc.hr_deviation(150, 150, "steady") == 0.0
    assert rc.hr_deviation(120, 150, "steady") > 0.0
    assert rc.hr_deviation(180, 150, "steady") > 0.0


def test_hr_deviation_guards_nulls():
    assert rc.hr_deviation(None, 150, "easy") is None
    assert rc.hr_deviation(150, None, "easy") is None


# --- training load ---------------------------------------------------------

def test_load_above_expectation_is_an_A():
    assert rc.load_deviation(150, 100) == 0.0


def test_load_below_expectation_is_penalized():
    assert rc.load_deviation(70, 100) == pytest.approx(0.3)


def test_load_is_intent_scaled_so_easy_days_dont_fail():
    """The same gap that broke pace broke load: an easy day banks less load by
    design. Graded against the raw median this run is a D; intent-scaled it's
    an A."""
    activity = {"date": "2026-07-19", "distance_meters": 7500,
                "avg_pace_sec_per_km": 340, "avg_hr": 130, "training_load": 75}
    card = card_for(activity, plan={"type": "easy", "seq": 1})
    assert card["metrics"]["load"]["expected"] == pytest.approx(75.0)  # 0.75 * 100
    assert rc.base_letter(card["metrics"]["load"]["grade"]) == "A"


def test_load_spike_is_advisory_not_a_downgrade():
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 10000, "avg_pace_sec_per_km": 300,
         "avg_hr": 150, "training_load": 250},
    )
    assert card["metrics"]["load"]["spike"] is True
    assert rc.base_letter(card["metrics"]["load"]["grade"]) == "A"


def test_load_is_na_when_null():
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 10000, "avg_pace_sec_per_km": 300,
         "avg_hr": 150, "training_load": None},
    )
    assert card["metrics"]["load"]["grade"] is None


# --- intent inference ------------------------------------------------------

def test_infer_intent_long_beats_easy_on_a_low_hr_long_run():
    """Ordering guard: a long run is usually run at easy HR. If "easy" won,
    the long run would be graded against a 0.75x distance expectation and get
    a free A on the one metric that should be interrogating it."""
    assert rc.infer_intent(
        {"distance_meters": 15000, "avg_hr": 130}, REF) == "long"


def test_infer_intent_easy_quality_and_steady():
    assert rc.infer_intent({"distance_meters": 8000, "avg_hr": 130}, REF) == "easy"
    assert rc.infer_intent({"distance_meters": 8000, "avg_hr": 165}, REF) == "quality"
    assert rc.infer_intent({"distance_meters": 8000, "avg_hr": 150}, REF) == "steady"


def test_infer_intent_null_hr_falls_back_to_steady():
    assert rc.infer_intent({"distance_meters": 8000, "avg_hr": None}, REF) == "steady"


@pytest.mark.parametrize("name", ["Recovery jog", "easy shakeout", "Shake Out"])
def test_activity_name_overrides_inference(name):
    """A run named "recovery" is an easy run no matter what its HR says."""
    assert rc.infer_intent(
        {"distance_meters": 8000, "avg_hr": 170, "activity_name": name}, REF) == "easy"


def test_plan_type_wins_over_inference():
    activity = {"date": "2026-07-19", "distance_meters": 8000, "avg_hr": 170,
                "avg_pace_sec_per_km": 300, "training_load": 100}
    card = card_for(activity, plan={"type": "easy", "seq": 1})
    assert (card["intent"], card["intent_source"]) == ("easy", "plan")
    card = card_for(activity)
    assert card["intent_source"] == "inferred"


def test_intent_class_collapses_plan_types():
    assert rc.intent_class("tempo") == "quality"
    assert rc.intent_class("interval") == "quality"
    assert rc.intent_class("race") == "quality"
    assert rc.intent_class("recovery") == "easy"
    assert rc.intent_class("cross") == "steady"
    assert rc.intent_class(None) == "steady"


def test_rest_day_prescription_falls_through_to_rolling():
    """A rest-day row has null targets — it's an intent signal, not a
    yardstick. Running anyway must still be graded, against the median."""
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 10000, "avg_pace_sec_per_km": 300,
         "avg_hr": 150, "training_load": 100},
        plan={"type": "rest", "seq": 1},
    )
    assert card["metrics"]["distance"]["reference"] == "rolling_60d"


# --- overall ---------------------------------------------------------------

def test_overall_renormalizes_over_gradeable_metrics():
    """One n/a metric must not drag the average toward zero — its weight
    redistributes across the rest."""
    all_a = {k: {"grade": "A"} for k in rc.METRIC_WEIGHTS}
    assert rc.overall_grade(all_a)["grade"] == "A"
    all_a["pace"] = {"grade": None}
    out = rc.overall_grade(all_a)
    assert out["grade"] == "A"
    assert out["graded_metrics"] == 3


def test_overall_with_no_gradeable_metrics_is_na_not_f():
    """"F" would read as a judgment we did not make."""
    out = rc.overall_grade({k: {"grade": None} for k in rc.METRIC_WEIGHTS})
    assert out["grade"] == "n/a"
    assert out["gpa"] is None


def test_overall_uses_base_letters_so_modifiers_cannot_move_it():
    with_mods = {k: {"grade": "B+"} for k in rc.METRIC_WEIGHTS}
    without = {k: {"grade": "B"} for k in rc.METRIC_WEIGHTS}
    assert rc.overall_grade(with_mods) == rc.overall_grade(without)


def test_overall_cuts():
    assert rc.overall_grade({"distance": {"grade": "F"}})["grade"] == "F"
    assert rc.overall_grade({"distance": {"grade": "C"}})["grade"] == "C"


# --- splits ----------------------------------------------------------------

MILE_SPLITS = [
    {"split_index": 0, "distance_meters": 1609.34, "duration_seconds": 600, "avg_hr": 140},
    {"split_index": 1, "distance_meters": 1609.34, "duration_seconds": 590, "avg_hr": 145},
    {"split_index": 2, "distance_meters": 1609.34, "duration_seconds": 585, "avg_hr": 150},
    {"split_index": 3, "distance_meters": 1609.34, "duration_seconds": 580, "avg_hr": 155},
    {"split_index": 4, "distance_meters": 91.17, "duration_seconds": 27, "avg_hr": 160},
]


def test_label_splits_detects_miles_and_is_one_indexed():
    out = rc.label_splits(MILE_SPLITS)
    assert out["unit"] == "Mile"
    assert out["rows"][0]["index"] == 1          # DB is 0-based, display is not
    assert out["rows"][-1]["partial"] is True


def test_label_splits_calls_non_mile_laps_laps():
    """Mislabeling a 1km lap as a mile is a lie on the card."""
    km = [{"split_index": i, "distance_meters": 1000.0, "avg_hr": 140} for i in range(4)]
    assert rc.label_splits(km)["unit"] == "Lap"


def test_label_splits_empty_is_unavailable():
    out = rc.label_splits([])
    assert out["available"] is False
    assert out["rows"] == []


def test_hr_drift_excludes_the_partial_lap():
    """A 90-meter fragment's HR is noise and must not move the drift number."""
    full = rc.label_splits(MILE_SPLITS)["rows"][:-1]
    assert rc.hr_drift_pct(full) == pytest.approx(
        rc.label_splits(MILE_SPLITS)["hr_drift_pct"])
    # front half 140,145 -> 142.5; back half 150,155 -> 152.5
    assert rc.label_splits(MILE_SPLITS)["hr_drift_pct"] == pytest.approx(7.0, abs=0.1)


def test_hr_drift_needs_enough_laps():
    assert rc.hr_drift_pct([{"avg_hr": 140}, {"avg_hr": 150}]) is None


def test_no_grade_reads_splits():
    """The load-bearing invariant: ~88% of activities have no splits, so a
    splits-dependent grade would mean different things on different rows."""
    activity = {"date": "2026-07-19", "distance_meters": 10000,
                "avg_pace_sec_per_km": 300, "avg_hr": 150, "training_load": 100}
    with_splits = card_for(activity, splits=MILE_SPLITS)
    without = card_for(activity)
    assert with_splits["metrics"] == without["metrics"]
    assert with_splits["overall"] == without["overall"]


# --- insufficient reference ------------------------------------------------

def test_insufficient_reference_grades_nothing():
    card = card_for(
        {"date": "2026-07-19", "distance_meters": 10000, "avg_pace_sec_per_km": 300,
         "avg_hr": 150, "training_load": 100},
        reference={"mode": "insufficient_data", "n": 3},
    )
    assert all(m["grade"] is None for m in card["metrics"].values())
    assert card["overall"]["grade"] == "n/a"


# --- rendering -------------------------------------------------------------

def test_render_markdown_tables_are_well_formed():
    card = card_for(
        {"date": "2026-07-19", "activity_name": "Morning Run", "distance_meters": 10000,
         "duration_seconds": 3000, "avg_pace_sec_per_km": 300, "avg_hr": 150,
         "training_load": 100},
        splits=MILE_SPLITS,
    )
    md = rc.render_markdown(card)
    table_lines = [ln for ln in md.splitlines() if ln.startswith("|")]
    assert table_lines
    # Every row of a given table has the same column count as its header.
    widths = {ln.count("|") for ln in table_lines}
    # Both tables are 5-col: metric (Metric/Actual/Expected/Delta/Grade) and
    # split (Mile/Pace/Avg HR/vs run/Elev). The split table's Distance column
    # was dropped as duplicative — the row label already IS the distance.
    assert widths == {6}
    assert "Morning Run" in md


def test_render_markdown_says_no_splits_when_absent():
    card = card_for({"date": "2026-07-19", "distance_meters": 10000,
                     "avg_pace_sec_per_km": 300, "avg_hr": 150, "training_load": 100})
    assert "No per-mile splits recorded" in rc.render_markdown(card)


def test_reference_line_names_the_yardstick():
    """The card must never leave the reference ambiguous."""
    plan_card = card_for(
        {"date": "2026-07-19", "distance_meters": 10000, "avg_pace_sec_per_km": 300,
         "avg_hr": 150, "training_load": 100},
        plan={"type": "easy", "target_distance_m": 10000,
              "target_pace_sec_per_km": 330, "seq": 1},
    )
    assert "training plan" in rc.reference_line(plan_card)
    rolling_card = card_for(
        {"date": "2026-07-19", "distance_meters": 10000, "avg_pace_sec_per_km": 300,
         "avg_hr": 150, "training_load": 100})
    assert "rolling median" in rc.reference_line(rolling_card)


def test_reference_line_drops_markdown_for_the_pdf():
    """The PDF escapes its HTML rather than rendering markdown, so leaving the
    asterisks in printed a literal `**training plan**` on the page."""
    card = card_for({"date": "2026-07-19", "distance_meters": 10000,
                     "avg_pace_sec_per_km": 300, "avg_hr": 150, "training_load": 100})
    assert "**" in rc.reference_line(card)
    assert "**" not in rc.reference_line(card, markdown=False)


def test_load_is_rounded_for_display():
    """Garmin's stored load carries ~13 meaningless decimals."""
    assert rc._fmt_load(51.28765869140625) == "51"


# === persistence ===========================================================

@pytest.fixture
def rc_db(tmp_path, monkeypatch):
    """A DB with enough comparable running history to build a real reference,
    plus one treadmill run so the exact-type-first rule is exercised."""
    p = tmp_path / "fitness.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    today = date.today()
    with db.connect(p) as conn:
        for i in range(1, 13):
            d = (today - timedelta(days=i)).isoformat()
            conn.execute(
                "INSERT INTO activities (activity_id, date, start_time, activity_type, "
                "activity_name, duration_seconds, distance_meters, avg_hr, "
                "avg_pace_sec_per_km, training_load) VALUES "
                "(?, ?, ?, 'running', 'Run', 3000, 10000, 150, 300, 100)",
                (100 + i, d, f"{d} 07:00:00"),
            )
        # Same-day treadmill session at a very different HR: it must NOT
        # pollute an outdoor run's reference.
        conn.execute(
            "INSERT INTO activities (activity_id, date, start_time, activity_type, "
            "activity_name, duration_seconds, distance_meters, avg_hr, "
            "avg_pace_sec_per_km, training_load) VALUES "
            "(200, ?, ?, 'treadmill_running', 'Treadmill', 3000, 10000, 110, 400, 40)",
            ((today - timedelta(days=2)).isoformat(),
             (today - timedelta(days=2)).isoformat() + " 18:00:00"),
        )
        # The activity under grading.
        conn.execute(
            "INSERT INTO activities (activity_id, date, start_time, activity_type, "
            "activity_name, duration_seconds, distance_meters, avg_hr, "
            "avg_pace_sec_per_km, training_load) VALUES "
            "(1, ?, ?, 'running', 'Morning Run', 3000, 10000, 150, 300, 100)",
            (today.isoformat(), today.isoformat() + " 07:00:00"),
        )
        for i, s in enumerate(MILE_SPLITS):
            conn.execute(
                "INSERT INTO activity_splits (activity_id, split_index, distance_meters, "
                "duration_seconds, avg_hr) VALUES (1, ?, ?, ?, ?)",
                (i, s["distance_meters"], s["duration_seconds"], s["avg_hr"]),
            )
        conn.execute(
            "INSERT INTO baselines (date, ctl, atl, tsb) VALUES (?, 40.0, 45.0, -5.0)",
            (today.isoformat(),),
        )
    return p


def test_rolling_reference_prefers_the_exact_activity_type(rc_db):
    """Measured on live data: pooling road with treadmill runs put median HR
    at 119 against an outdoor average of 140, and handed a normal easy run a
    D on heart rate."""
    with db.connect(rc_db) as conn:
        act = dict(conn.execute("SELECT * FROM activities WHERE activity_id=1").fetchone())
        ref = rc.rolling_reference(conn, act)
    assert ref["mode"] == "rolling_60d"
    assert ref["pool"] == "running"
    assert ref["widened"] is False
    assert ref["n"] == 12                      # the treadmill row is excluded
    assert ref["median_hr"] == pytest.approx(150.0)


def test_rolling_reference_excludes_the_graded_activity(rc_db):
    """A workout must never move its own goalposts."""
    with db.connect(rc_db) as conn:
        conn.execute("UPDATE activities SET training_load=9999 WHERE activity_id=1")
        act = dict(conn.execute("SELECT * FROM activities WHERE activity_id=1").fetchone())
        ref = rc.rolling_reference(conn, act)
    assert ref["median_load"] == pytest.approx(100.0)


def test_rolling_reference_window_ends_the_day_before(rc_db):
    with db.connect(rc_db) as conn:
        act = dict(conn.execute("SELECT * FROM activities WHERE activity_id=1").fetchone())
        ref = rc.rolling_reference(conn, act)
    assert ref["window_end"] == (date.today() - timedelta(days=1)).isoformat()


def test_rolling_reference_widens_when_the_exact_pool_is_thin(rc_db):
    """A treadmill run has only one same-type peer, so the pool widens to
    on-foot — and says so, rather than widening silently."""
    with db.connect(rc_db) as conn:
        act = dict(conn.execute("SELECT * FROM activities WHERE activity_id=200").fetchone())
        ref = rc.rolling_reference(conn, act)
    assert ref["widened"] is True
    assert ref["pool"] == "on-foot"
    assert ref["mode"] == "rolling_60d"


def test_rolling_reference_insufficient_below_the_floor(tmp_path, monkeypatch):
    p = tmp_path / "f.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", p)
    db.init_schema(p)
    today = date.today()
    with db.connect(p) as conn:
        for i in range(1, 5):     # 4 peers, one below MIN_REFERENCE_ACTIVITIES
            conn.execute(
                "INSERT INTO activities (activity_id, date, activity_type, "
                "distance_meters, avg_hr) VALUES (?, ?, 'running', 10000, 150)",
                (i, (today - timedelta(days=i)).isoformat()),
            )
        act = {"activity_id": 99, "date": today.isoformat(), "activity_type": "running"}
        ref = rc.rolling_reference(conn, act)
    assert ref["mode"] == "insufficient_data"
    assert ref["n"] == 4


def test_rolling_reference_handles_a_malformed_date():
    class _Conn:
        def execute(self, *a, **k):        # pragma: no cover - never reached
            raise AssertionError("must not query on a bad date")

    assert rc.rolling_reference(_Conn(), {"date": "nope"})["mode"] == "insufficient_data"


def test_load_inputs_defaults_to_the_most_recent_activity(rc_db):
    with db.connect(rc_db) as conn:
        inputs = rc.load_report_card_inputs(conn)
    assert inputs["activity"]["activity_id"] == 1
    assert len(inputs["splits"]) == len(MILE_SPLITS)
    assert inputs["context"]["ctl"] == 40.0


def test_load_inputs_skips_activities_with_no_distance(rc_db):
    """A strength session has nothing this card can grade, so the default must
    not land on one."""
    with db.connect(rc_db) as conn:
        conn.execute(
            "INSERT INTO activities (activity_id, date, start_time, activity_type, "
            "duration_seconds, distance_meters) VALUES (999, ?, ?, 'strength_training', "
            "1800, 0)",
            (date.today().isoformat(), date.today().isoformat() + " 20:00:00"),
        )
        inputs = rc.load_report_card_inputs(conn)
    assert inputs["activity"]["activity_id"] == 1


def test_explicit_activity_id_bypasses_the_distance_filter(rc_db):
    with db.connect(rc_db) as conn:
        conn.execute(
            "INSERT INTO activities (activity_id, date, activity_type, "
            "duration_seconds, distance_meters) VALUES (999, ?, 'strength_training', "
            "1800, 0)",
            (date.today().isoformat(),),
        )
        inputs = rc.load_report_card_inputs(conn, activity_id=999)
    assert inputs["activity"]["activity_id"] == 999


def test_load_inputs_by_date_and_miss(rc_db):
    with db.connect(rc_db) as conn:
        assert rc.load_report_card_inputs(
            conn, target_date=date.today().isoformat())["activity"]["activity_id"] == 1
        assert rc.load_report_card_inputs(conn, target_date="1999-01-01") is None


def test_load_inputs_reports_other_activities_on_the_date(rc_db):
    """A double-day must not silently hide its second session."""
    with db.connect(rc_db) as conn:
        conn.execute(
            "INSERT INTO activities (activity_id, date, start_time, activity_type, "
            "duration_seconds, distance_meters) VALUES (555, ?, ?, 'running', 1200, 5000)",
            (date.today().isoformat(), date.today().isoformat() + " 05:00:00"),
        )
        inputs = rc.load_report_card_inputs(conn)
    assert 555 in inputs["other_activities_on_date"]


def test_load_inputs_picks_the_planned_workout_for_that_date(rc_db):
    from local_fitness import plans

    today = date.today().isoformat()
    with db.connect(rc_db) as conn:
        conn.execute(
            "INSERT INTO training_plans (plan_id, status, goal_type, race_date, "
            "title, created_at) VALUES (7, 'active', '10k', ?, 'Plan', ?)",
            ((date.today() + timedelta(days=30)).isoformat(), today),
        )
        for seq, dist in ((2, 8000), (1, 12000)):
            conn.execute(
                "INSERT INTO plan_workouts (plan_id, date, seq, week_index, type, "
                "target_distance_m, description) VALUES (7, ?, ?, 1, 'easy', ?, 'Easy')",
                (today, seq, dist),
            )
        inputs = rc.load_report_card_inputs(conn)
    assert plans is not None
    # Lowest seq is the day's primary session.
    assert inputs["plan_workout"]["target_distance_m"] == 12000


def test_build_card_end_to_end_against_the_db(rc_db):
    with db.connect(rc_db) as conn:
        inputs = rc.load_report_card_inputs(conn)
    card = rc.build_card(
        inputs["activity"], inputs["splits"], inputs["plan_workout"],
        inputs["reference"], inputs["context"],
    )
    # Identical to the median on every axis -> straight As.
    assert card["overall"]["grade"] == "A"
    assert card["splits"]["unit"] == "Mile"
    assert "Mile 1" in rc.render_markdown(card)
