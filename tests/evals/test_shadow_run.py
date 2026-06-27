"""Deterministic tests for the V2 shadow-run parity tool (no model calls).

Covers the pure comparison (``parity_report``), the median helper, and the CLI
guards. The live ``--run`` path is I/O glue over the LLM composer and is not
unit-tested (it would only assert a mock replays itself).
"""
from __future__ import annotations

import json

import shadow_run as sr


def _fp(n=3, steps=True, plan=False):
    return {"n_takeaways": n, "has_steps": steps, "mentions_plan": plan,
            "metrics": [], "tones": []}


def _rec(fps, schema_invalid=0, flakes=(), invention_rate=None):
    rec = {"fingerprints": list(fps), "schema_invalid": schema_invalid,
           "flakes": list(flakes)}
    if invention_rate is not None:
        rec["invention_rate"] = invention_rate
    return rec


def _baseline(**scen_fps):
    return {"scenarios": {n: {"fingerprints": fps} for n, fps in scen_fps.items()}}


# --- _median_count --------------------------------------------------------

def test_median_count_edges():
    assert sr._median_count([]) is None
    assert sr._median_count([_fp(n=4)]) == 4
    assert sr._median_count([_fp(n=3), _fp(n=5)]) == 5   # upper-middle of even set


# --- parity_report --------------------------------------------------------

def test_parity_holds_when_structure_matches():
    base = _baseline(green_light=[_fp(), _fp()])
    shadow = {"green_light": _rec([_fp(), _fp()])}
    report = sr.parity_report(base, shadow)
    assert report["overall_parity"] is True
    assert report["scenarios"]["green_light"]["parity"] is True


def test_schema_invalid_breaks_parity():
    base = _baseline(green_light=[_fp()])
    shadow = {"green_light": _rec([_fp()], schema_invalid=1)}
    report = sr.parity_report(base, shadow)
    assert report["scenarios"]["green_light"]["checks"]["schema_valid"] is False
    assert report["overall_parity"] is False


def test_missing_steps_mandate_breaks_parity():
    base = _baseline(green_light=[_fp()])
    shadow = {"green_light": _rec([_fp(steps=False)])}
    report = sr.parity_report(base, shadow)
    assert report["scenarios"]["green_light"]["checks"]["steps_mandate"] is False
    assert report["overall_parity"] is False


def test_count_out_of_range_breaks_parity():
    base = _baseline(green_light=[_fp(n=3)])
    shadow = {"green_light": _rec([_fp(n=6)])}   # 6 > max of 5
    report = sr.parity_report(base, shadow)
    assert report["scenarios"]["green_light"]["checks"]["count_in_range"] is False


def test_count_far_from_baseline_breaks_parity():
    base = _baseline(green_light=[_fp(n=3)])
    shadow = {"green_light": _rec([_fp(n=5)])}   # |5-3| = 2 > 1
    report = sr.parity_report(base, shadow)
    assert report["scenarios"]["green_light"]["checks"]["count_near_baseline"] is False
    assert report["overall_parity"] is False


def test_plan_scenario_requires_plan_folding():
    # taper_plan is plan-active: a brief that drops the plan FAILS parity (hard).
    base = _baseline(taper_plan=[_fp(plan=True)])
    shadow = {"taper_plan": _rec([_fp(plan=False)])}
    report = sr.parity_report(base, shadow)
    assert report["scenarios"]["taper_plan"]["checks"]["plan_parity"] is False
    assert report["overall_parity"] is False


def test_nonplan_plan_leak_is_advisory_not_a_failure():
    # A non-plan scenario showing mentions_plan=True is keyword noise → warning,
    # parity still holds (the baseline itself recorded that flakiness).
    base = _baseline(sliding_fitness=[_fp(plan=False)])
    shadow = {"sliding_fitness": _rec([_fp(plan=True)])}
    report = sr.parity_report(base, shadow)
    rec = report["scenarios"]["sliding_fitness"]
    assert rec["checks"]["plan_parity"] is True
    assert rec["warnings"] and "advisory" in rec["warnings"][0]
    assert rec["parity"] is True


def test_scenario_absent_from_baseline_cannot_prove_parity():
    base = _baseline()  # empty baseline
    shadow = {"green_light": _rec([_fp()])}
    report = sr.parity_report(base, shadow)
    assert report["scenarios"]["green_light"]["checks"]["count_near_baseline"] is False
    assert report["overall_parity"] is False


def test_invention_rate_not_computed_without_a_rate():
    # Structural-only records (no invention_rate) → the gate reports not-computed
    # and never adds an invention check.
    report = sr.parity_report(_baseline(green_light=[_fp()]),
                              {"green_light": _rec([_fp()])})
    assert "not computed" in report["invention_rate_gate"]
    assert "invention_rate" not in report["scenarios"]["green_light"]["checks"]


def test_invention_rate_is_advisory_not_a_parity_gate():
    # Invention is ADVISORY: within budget → no warning; over budget → a warning,
    # but structural parity is UNAFFECTED either way.
    base = _baseline(green_light=[_fp(), _fp()])
    low = sr.parity_report(base, {"green_light": _rec([_fp(), _fp()], invention_rate=0.0)})
    assert low["overall_parity"] is True
    assert not low["scenarios"]["green_light"]["warnings"]
    assert "advisory" in low["invention_rate_gate"]

    high = sr.parity_report(base, {"green_light": _rec([_fp(), _fp()], invention_rate=0.8)})
    assert high["overall_parity"] is True       # NOT broken by high invention
    assert "invention_rate" not in high["scenarios"]["green_light"]["checks"]
    assert any("invention_rate 0.8" in w for w in high["scenarios"]["green_light"]["warnings"])


# --- CLI guards -----------------------------------------------------------

def _write_baseline(tmp_path):
    p = tmp_path / "baseline.json"
    p.write_text(json.dumps(_baseline(green_light=[_fp(), _fp()])))
    return p


def test_dry_run_returns_zero_and_quotes_spend(tmp_path, capsys):
    base = _write_baseline(tmp_path)
    rc = sr.main(["--baseline", str(base)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "generations" in out and "V2 flag forced ON" in out


def test_run_refused_over_cap(tmp_path, capsys):
    base = _write_baseline(tmp_path)
    rc = sr.main(["--run", "--runs", "5", "--baseline", str(base)])
    assert rc == 2
    assert "REFUSED" in capsys.readouterr().err


def test_missing_baseline_refused(tmp_path, capsys):
    rc = sr.main(["--baseline", str(tmp_path / "nope.json")])
    assert rc == 2
    assert "baseline not found" in capsys.readouterr().err


def test_unknown_scenario_refused(tmp_path, capsys):
    base = _write_baseline(tmp_path)
    rc = sr.main(["--scenarios", "bogus", "--baseline", str(base)])
    assert rc == 2
    assert "unknown scenario" in capsys.readouterr().err


def test_mock_parity_pass_writes_report(tmp_path):
    base = tmp_path / "baseline.json"
    base.write_text(json.dumps(_baseline(green_light=[_fp(), _fp()])))
    canned = tmp_path / "shadow.json"
    canned.write_text(json.dumps({"green_light": [
        {"date": "2026-06-26", "user_name": "N", "takeaways": [
            {"headline": "Workout", "summary": "easy run", "tone": "positive", "details": "d"},
            {"headline": "Steps check", "summary": "over 10k steps", "tone": "positive", "details": "d"},
            {"headline": "Recovery", "summary": "rhr low", "tone": "positive", "details": "d"},
        ]}]}))
    out = tmp_path / "report.json"
    rc = sr.main(["--mock", str(canned), "--baseline", str(base), "--out", str(out)])
    assert rc == 0
    report = json.loads(out.read_text())
    assert report["overall_parity"] is True
    assert report["scenarios"]["green_light"]["parity"] is True


def test_mock_parity_failure_returns_one(tmp_path):
    base = tmp_path / "baseline.json"
    base.write_text(json.dumps(_baseline(green_light=[_fp(n=3)])))
    canned = tmp_path / "shadow.json"
    # A brief with no steps takeaway → steps_mandate fails → parity fails.
    canned.write_text(json.dumps({"green_light": [
        {"date": "2026-06-26", "user_name": "N", "takeaways": [
            {"headline": "Workout", "summary": "easy run", "tone": "positive", "details": "d"},
            {"headline": "Recovery", "summary": "rhr low", "tone": "neutral", "details": "d"},
            {"headline": "Conditioning", "summary": "ctl up", "tone": "positive", "details": "d"},
        ]}]}))
    rc = sr.main(["--mock", str(canned), "--baseline", str(base)])
    assert rc == 1
