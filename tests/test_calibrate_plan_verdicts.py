"""Tests for the plan-verdict calibration gate.

Same precedent as ``test_calibrate_report_card.py``: a measurement instrument
needs its own tests before its output is allowed to drive a decision. This one
exists specifically because its *absence* is what let #242's first fix ship a
pace cut that made ``done`` unreachable — so a gate that cannot fail, or that
fails on healthy data, would be worse than none.

The load-bearing behaviours:

* **only rated days count** — a quality day with no qualifying run grades
  ``missed`` on volume before the pace cap is consulted, and counting the
  athlete's absence as evidence about the yardstick is the exact error the
  card gate avoids by grading only running efforts;
* **both signatures actually fire**, and
* **healthy data passes** — a gate nobody can satisfy gets switched off.

The live-DB path is exercised against the fabricated ``tests/evals`` plan
scenario databases, so these stay CI-safe: no network, no real data.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import calibrate_plan_verdicts as cal
import pytest


def _rows(*specs) -> list[dict]:
    """Graded rows from ``(verdict, deviation)`` pairs; ``None`` deviation is
    an unrated day (no qualifying run with a rep-sized split)."""
    return [
        {"plan_id": 1, "status": "active", "date": f"2026-07-{i + 1:02d}",
         "type": "tempo", "target_pace": 300.0,
         "rep_pace": None if dev is None else 300.0 * (1 + dev),
         "deviation": dev, "verdict": v}
        for i, (v, dev) in enumerate(specs)
    ]


# --- what the gate counts ---------------------------------------------------

def test_unrated_days_are_excluded_from_the_verdict_share():
    """A day the athlete skipped grades `missed` on volume alone. Counting it
    would let absence read as a punitive yardstick — the reason the card gate
    grades only running efforts."""
    rows = _rows(*([("missed", None)] * 30), *([("done", 0.01)] * 12))
    text, failed = cal.format_report(rows, max_missed_share=0.60)

    assert not failed
    assert "42 quality days across all plans; 12 rated" in text
    # The 30 skipped days are absent from the histogram, not folded into it.
    assert "missed     0" in text
    assert "done      12" in text


def test_punitive_skew_fires_and_names_the_constants():
    rows = _rows(*([("missed", 0.30)] * 11), ("done", 0.01), ("partial", 0.10))
    text, failed = cal.format_report(rows, max_missed_share=0.60)

    assert failed
    assert "PUNITIVE SKEW" in text and "85%" in text
    assert "QUALITY_PACE_DONE_DEVIATION" in text


def test_an_unreachable_done_fires_on_its_own():
    """#242's round-2 finding, made executable. This distribution is not
    punitive — only a third is `missed` — but its top rung is unreachable, and
    a three-valued ladder with two reachable rungs is a two-valued one."""
    rows = _rows(*([("partial", 0.10)] * 13), *([("missed", 0.30)] * 6))
    text, failed = cal.format_report(rows, max_missed_share=0.60)

    assert failed
    assert "UNREACHABLE VERDICT" in text
    assert "PUNITIVE SKEW" not in text


def test_the_measured_pre_retune_distribution_is_what_fails():
    """The exact live shape the retune answered: 0 done / 13 partial / 6
    missed across 19 rated days. Pinned so the gate can never quietly stop
    recognising the case it was written for."""
    rows = _rows(*([("partial", 0.10)] * 13), *([("missed", 0.30)] * 6))
    _text, failed = cal.format_report(rows, max_missed_share=0.60)
    assert failed


def test_a_healthy_distribution_passes():
    """The converse — the gate must be capable of returning clean, or it is a
    tripwire nobody can satisfy. This is the post-retune live shape."""
    rows = _rows(*([("done", 0.01)] * 2), *([("partial", 0.10)] * 11),
                 *([("missed", 0.30)] * 6))
    text, failed = cal.format_report(rows, max_missed_share=0.60)

    assert not failed
    assert "PUNITIVE SKEW" not in text and "UNREACHABLE VERDICT" not in text


def test_concentration_in_done_is_never_gated():
    """The asymmetry inherited from the card gate: an athlete who hits every
    session must read as hitting every session. #242 is the standing reminder
    that what this project actually ships is a verdict that is too generous —
    and that is caught day by day in the evals, not in aggregate here."""
    rows = _rows(*([("done", 0.0)] * 25))
    _text, failed = cal.format_report(rows, max_missed_share=0.60)
    assert not failed


def test_the_gate_abstains_on_thin_data():
    """Under the sample floor a histogram is noise. Abstain rather than fail —
    a fresh clone with two graded days must not be told its rubric is broken."""
    rows = _rows(*([("missed", 0.30)] * 5))
    text, failed = cal.format_report(rows, max_missed_share=0.60)

    assert not failed
    assert "ABSTAINED" in text
    assert cal.MIN_SAMPLE > 5


def test_no_rated_days_is_not_a_failure():
    rows = _rows(*([("missed", None)] * 8))
    text, failed = cal.format_report(rows, max_missed_share=0.60)
    assert not failed
    assert "no rated days" in text


# --- the live path, against fabricated scenario databases -------------------

def _scenario_db(scenario: str, tmp_path: Path) -> Path:
    from plan_verdicts import build_scenario_db
    return build_scenario_db(scenario, tmp_path / scenario / "fitness.db")


@pytest.mark.parametrize("scenario, expected", [
    ("tempo_hit", "done"),
    ("tempo_jogged", "missed"),
])
def test_grade_quality_days_reads_the_production_verdict(scenario, expected, tmp_path):
    """It must regrade through `classify_workout`, not reimplement it — a
    reimplementation is free to disagree with what ships and nothing would
    say which was wrong. Driven against the eval fixtures, whose expected
    verdicts are declared independently in `EXPECTED_VERDICTS`."""
    path = _scenario_db(scenario, tmp_path)
    conn = cal.open_readonly(path)
    try:
        rows = cal.grade_quality_days(conn)
    finally:
        conn.close()

    assert len(rows) == 1
    assert rows[0]["verdict"] == expected
    assert rows[0]["type"] == "tempo"
    # The pace cap had an opinion, so the day is rated.
    assert rows[0]["deviation"] is not None


def test_grade_quality_days_marks_a_splitless_day_unrated(tmp_path):
    """The backfilled tail. The cap abstains, so the day carries a verdict but
    no deviation and must not enter the histogram."""
    path = _scenario_db("tempo_no_splits", tmp_path)
    conn = cal.open_readonly(path)
    try:
        rows = cal.grade_quality_days(conn)
    finally:
        conn.close()

    assert len(rows) == 1
    assert rows[0]["deviation"] is None


def test_the_connection_refuses_writes(tmp_path):
    """`mode=ro` is enforced by SQLite, not by this script remembering to only
    SELECT. Nate's live data is not ours."""
    path = _scenario_db("tempo_hit", tmp_path)
    conn = cal.open_readonly(path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM plan_workouts")
    finally:
        conn.close()


def test_a_missing_database_skips_rather_than_failing(tmp_path, capsys):
    assert cal.main(["--db", str(tmp_path / "nope.db")]) == 0
    assert "SKIPPED" in capsys.readouterr().out


def test_require_db_turns_the_skip_into_a_failure(tmp_path):
    assert cal.main(["--db", str(tmp_path / "nope.db"), "--require-db"]) == 2


def test_a_file_that_is_not_a_database_is_an_error(tmp_path, capsys):
    bogus = tmp_path / "fitness.db"
    bogus.write_text("not a database")
    assert cal.main(["--db", str(bogus)]) == 2
    assert "ERROR" in capsys.readouterr().out


def test_main_runs_end_to_end_on_a_scenario_db(tmp_path, capsys):
    """One quality day is under MIN_SAMPLE, so this abstains — which is the
    correct outcome and proves `main` reaches `format_report` with real rows."""
    path = _scenario_db("tempo_hit", tmp_path)
    assert cal.main(["--db", str(path), "--verbose"]) == 0
    out = capsys.readouterr().out
    assert "quality-day verdict calibration" in out
    assert "done" in out


# --- the contract this script makes about itself ---------------------------

def test_the_plan_gate_is_not_wired_into_ci():
    """Deliberate, and the same reason as its sibling: it needs a populated DB
    that CI does not have, and a fabricated one would only ask the fixture
    whether it agrees with itself. If someone adds it to a workflow, this
    fails and they have to read why."""
    workflows = Path(__file__).resolve().parent.parent / ".github" / "workflows"
    for wf in workflows.glob("*.yml"):
        assert "calibrate_plan_verdicts" not in wf.read_text(encoding="utf-8"), wf.name


def test_the_script_documents_itself_as_a_manual_gate():
    assert "MANUAL gate" in cal.__doc__
    assert "READ-ONLY" in cal.__doc__


def test_the_missed_bar_matches_the_card_gates():
    """Both gates must call the same distribution punitive, or the two
    surfaces disagree about what a broken rubric looks like."""
    import calibrate_report_card as card_cal
    assert cal.DEFAULT_MAX_MISSED_SHARE == card_cal.DEFAULT_MAX_FAIL_SHARE
