"""Tests for the report-card calibration gate.

The gate is a measurement instrument, and the 2026-07-22 grade-leak work is the
precedent here: *"a measurement instrument needs its own test suite before its
output is allowed to drive a decision."* That one shipped a detector which
counted the article "A" as a letter grade and reported a 5x-wrong leak rate.

The load-bearing behavior is the ASYMMETRY — concentration in a failing grade
is a defect signal, concentration in a passing grade is not — because this
check's first draft got that wrong and flagged distance (79% A, all five bands
in use) as degenerate alongside the genuinely broken HR axis.

The live-DB path is exercised against the fabricated ``tests/evals`` scenario
databases, so these stay CI-safe: no network, no real data.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path

import calibrate_report_card as cal
import pytest
from report_cards import GRADED_DATE, build_scenario_db

from local_fitness import db

# Long enough to reach the whole fabricated pool from the graded date.
_WINDOW = 400


def _counts(**kw) -> Counter:
    return Counter({k: v for k, v in kw.items()})


# --- the verdict rule ------------------------------------------------------

def test_punitive_skew_fails():
    """The 0.40.0 HR-cap axis: 84% of runs D or F. A rubric that fails most of
    a season is measuring the wrong thing."""
    status, reason = cal.verdict(_counts(A=7, B=0, C=0, D=4, F=32),
                                 max_fail_share=0.60, max_empty=2)
    assert status == "FAIL"
    assert "punitive skew" in reason and "84%" in reason


def test_dead_bands_fail():
    """Two letters never reached over a whole window: the scale has collapsed
    even if the surviving grades look reasonable."""
    status, reason = cal.verdict(_counts(A=20, B=6, C=0, D=4, F=0),
                                 max_fail_share=0.60, max_empty=2)
    assert status == "FAIL"
    assert "dead bands" in reason and "C, F" in reason


def test_a_healthy_skew_toward_passing_is_not_a_failure():
    """THE asymmetry, and the correction to this check's first draft.

    Distance on live data grades 79% A with all five bands in use — that is a
    runner hitting his distances, not a broken table. A symmetric
    "any letter over 60%" rule failed it, which would have made the gate cry
    wolf on three of five metrics and trained everyone to ignore it.
    """
    healthy = _counts(A=33, B=2, C=1, D=5, F=1)          # 79% A, 14% D/F
    assert cal.verdict(healthy, max_fail_share=0.60, max_empty=2)[0] == "ok"

    # Same shape, mirrored onto the failing end — now it must fire.
    punitive = _counts(A=1, B=5, C=1, D=2, F=33)          # 79% F, 83% D/F
    assert cal.verdict(punitive, max_fail_share=0.60, max_empty=2)[0] == "FAIL"


def test_one_empty_band_is_tolerated():
    """A compliant athlete genuinely never earns an F; that alone is not a
    finding, which is why the threshold is two."""
    status, reason = cal.verdict(_counts(A=20, B=6, C=3, D=4, F=0),
                                 max_fail_share=0.60, max_empty=2)
    assert status == "ok"
    assert "4/5 bands" in reason


def test_a_thin_sample_abstains_rather_than_failing():
    """Under MIN_SAMPLE the histogram is noise. Abstaining is the same rule
    MIN_REFERENCE_ACTIVITIES applies to the reference pool."""
    status, reason = cal.verdict(_counts(A=1, F=2), max_fail_share=0.60, max_empty=2)
    assert status == "skip"
    assert "need 10" in reason
    # ...and it abstains even though 67% D/F would otherwise be punitive skew.
    assert sum(_counts(A=1, F=2).values()) < cal.MIN_SAMPLE


# --- collection ------------------------------------------------------------

def test_capped_and_uncapped_hr_are_tallied_separately():
    """They are graded by different constants (HR_CAP_* vs HR_BANDS), so a
    pooled histogram would let a degenerate regime hide behind a healthy one."""
    cards = [
        {"metrics": {"hr": {"grade": "F", "cap": 140.0}}},
        {"metrics": {"hr": {"grade": "A+"}}},
    ]
    tally = cal.collect(cards)
    assert tally["hr (prescribed cap)"] == Counter({"F": 1})
    assert tally["hr (rolling band)"] == Counter({"A": 1})


def test_abstained_and_stimulus_metrics_are_not_counted():
    """An n/a metric was never graded, and load carries no letter at all — both
    would corrupt the distribution if tallied."""
    cards = [{"metrics": {
        "distance": {"grade": "A+"},
        "pace": {"grade": None},          # abstained
        "continuity": {"grade": "n/a"},   # abstained, string form
        "load": {"grade": "F"},           # stimulus: never graded
    }}]
    tally = cal.collect(cards)
    assert tally["distance"] == Counter({"A": 1})
    assert tally["pace"] == Counter()
    assert tally["continuity"] == Counter()
    assert "load" not in tally


# --- the database path -----------------------------------------------------

def test_the_connection_is_physically_read_only(tmp_path):
    """Not "we only SELECT" — SQLite must refuse the write. This is Nate's live
    data and the guarantee has to be structural."""
    path = build_scenario_db("obedient_easy_clean", tmp_path / "fitness.db")
    conn = cal.open_readonly(path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM report_cards")
    finally:
        conn.close()


def test_only_running_efforts_are_selected(tmp_path):
    """The walk_mislabelled fixture holds 16 runs and 30 walking-desk sessions,
    every one typed `treadmill_running`. A label-based selection takes all 46.
    """
    path = build_scenario_db("walk_mislabelled", tmp_path / "fitness.db")
    conn = cal.open_readonly(path)
    try:
        ids = cal.running_activity_ids(conn, _WINDOW, GRADED_DATE)
    finally:
        conn.close()
    assert len(ids) == 17          # 16 pool runs + the graded activity
    assert 1 in ids                # the graded run itself


def test_a_paceless_row_is_excluded(tmp_path):
    """Unknown locomotion joins neither pool — the same rule
    rolling_reference applies, so the gate never grades a mode it can't verify.
    """
    path = build_scenario_db("obedient_easy_clean", tmp_path / "fitness.db")
    with db.connect(path) as conn:
        conn.execute(
            "INSERT INTO activities (activity_id, date, activity_type, "
            "duration_seconds, distance_meters) VALUES (999, ?, "
            "'treadmill_running', 1800, 5000)", (GRADED_DATE.isoformat(),))
    conn = cal.open_readonly(path)
    try:
        assert 999 not in cal.running_activity_ids(conn, _WINDOW, GRADED_DATE)
    finally:
        conn.close()


def test_grade_window_builds_real_cards(tmp_path):
    """End-to-end: the gate's histogram must come from the shipping rubric, not
    a reimplementation that could disagree with build_card and never be caught.
    """
    path = build_scenario_db("cap_blown_hard", tmp_path / "fitness.db")
    conn = cal.open_readonly(path)
    try:
        cards = cal.grade_window(conn, [1])
    finally:
        conn.close()
    assert len(cards) == 1
    # The same verdict tests/evals/test_report_card_verdicts.py pins for this
    # scenario — the gate and the evals must see one rubric.
    assert cards[0]["metrics"]["hr"]["cap"] == 140.0
    assert cards[0]["overall"]["grade"] == "C"


# --- the CLI ---------------------------------------------------------------

def test_missing_database_skips_but_require_db_fails(tmp_path, capsys):
    missing = tmp_path / "nope.db"
    assert cal.main(["--db", str(missing)]) == 0
    assert "SKIPPED" in capsys.readouterr().out
    assert cal.main(["--db", str(missing), "--require-db"]) == 2


def test_an_unreadable_file_errors_rather_than_exploding(tmp_path, capsys):
    """A fresh clone or a corrupt file must not hand the user a traceback."""
    junk = tmp_path / "not.db"
    junk.write_bytes(b"this is not a database")
    assert cal.main(["--db", str(junk), "--days", str(_WINDOW)]) == 2
    assert "ERROR" in capsys.readouterr().out


def test_a_window_with_no_runs_skips(tmp_path, capsys):
    path = build_scenario_db("obedient_easy_clean", tmp_path / "fitness.db")
    # A 1-day window anchored on today, decades after the fixture's dates.
    assert cal.main(["--db", str(path), "--days", "1"]) == 0
    assert "no running efforts" in capsys.readouterr().out


def test_a_failure_names_the_constants_to_go_look_at():
    """A gate that says "degenerate" without naming the knob is a riddle. This
    is the pure path, so the assertion is on the report text itself."""
    tally = {k: Counter() for k in cal.GOVERNING_CONSTANTS}
    tally["hr (prescribed cap)"] = _counts(A=7, D=4, F=32)      # the 0.40.0 shape
    text, failed = cal.format_report(
        tally, [], max_fail_share=0.60, max_empty=2, days=90)
    assert failed
    assert "HR_CAP_NOISE_BPM" in text and "HR_CAP_BPM_SCALE" in text
    # ...and it does NOT name constants for the metrics that were fine.
    assert "DISTANCE_FACTORS" not in text


def test_main_reports_and_returns_nonzero_on_a_degenerate_metric(
        tmp_path, capsys, monkeypatch):
    """The whole gate, end to end.

    MIN_SAMPLE is lowered because a fabricated fixture cannot honestly carry a
    season of gradeable runs — its pool members sit at the edge of their own
    60-day windows and mostly abstain. That limitation IS the reason this gate
    is manual: the signal needs real history. `--max-fail-share 0` then makes
    the one genuine breach punitive.
    """
    monkeypatch.setattr(cal, "MIN_SAMPLE", 1)
    path = build_scenario_db("cap_blown_hard", tmp_path / "fitness.db")
    rc_code = cal.main(["--db", str(path), "--days", str(_WINDOW),
                        "--max-fail-share", "0", "--verbose"])
    out = capsys.readouterr().out
    assert rc_code == 1
    assert "FAIL" in out
    assert "HR_CAP_NOISE_BPM" in out        # names the knob
    assert "F-cap fired on" in out          # the informational card-level line
    assert "overall=C" in out               # --verbose listed the graded run


def test_main_passes_on_a_healthy_window(tmp_path, capsys):
    """The converse — the gate must be capable of returning 0, or it is a
    tripwire nobody can ever satisfy."""
    path = build_scenario_db("obedient_easy_clean", tmp_path / "fitness.db")
    assert cal.main(["--db", str(path), "--days", str(_WINDOW),
                     "--max-empty", "5"]) == 0
    assert "OK — every graded metric still uses its bands" in capsys.readouterr().out


def test_the_gate_is_not_wired_into_ci():
    """Deliberate: it needs a populated DB that CI does not have, and a
    fabricated one would only ask the fixture whether it agrees with itself.
    If someone adds it to a workflow, this fails and they have to read why.
    """
    workflows = Path(__file__).resolve().parent.parent / ".github" / "workflows"
    for wf in workflows.glob("*.yml"):
        assert "calibrate_report_card" not in wf.read_text(encoding="utf-8"), wf.name


def test_the_script_documents_itself_as_a_manual_gate():
    assert "MANUAL gate" in cal.__doc__
    assert "READ-ONLY" in cal.__doc__


def test_the_window_is_bounded_at_both_ends(tmp_path):
    """`--days N` means the last N days, not "everything since N days before
    the anchor". Without an upper bound a past anchor grades the whole history,
    and the histogram silently stops describing the window it names.
    """
    path = build_scenario_db("obedient_easy_clean", tmp_path / "fitness.db")
    conn = cal.open_readonly(path)
    try:
        inside = cal.running_activity_ids(conn, _WINDOW, GRADED_DATE)
        # An anchor decades BEFORE the data: every row is in the future here.
        before = cal.running_activity_ids(conn, 5, date(2000, 1, 1))
        # A one-day window on the graded date itself: only that activity.
        just_today = cal.running_activity_ids(conn, 1, GRADED_DATE)
    finally:
        conn.close()
    assert len(inside) == 17
    assert before == []
    assert just_today == [1]
