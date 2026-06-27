"""Deterministic tests for the baseline-capture tool (no model calls).

Covers the pure reducer (``aggregate_scenario``), the document builder, and the
CLI guards (dry-run, hard cap, unknown-scenario, mock). The live ``--run`` path
is pure I/O glue over the LLM composer and is intentionally NOT unit-tested
(a test there would only assert a mock replays itself).
"""
from __future__ import annotations

import json

import ab_brief
import capture_baseline as cb


def _tk(headline, summary="why", tone="neutral", metric=None, details="d"):
    return {"headline": headline, "summary": summary, "tone": tone,
            "metric": metric, "details": details}


def _brief(takeaways):
    return {"date": "2026-06-26", "user_name": "Nate", "takeaways": takeaways}


def _three_with_steps(plan=False):
    plan_line = "training plan calls for an easy 5k" if plan else "easy run today"
    return _brief([
        _tk("Today's workout", plan_line, metric={"metric": "tsb", "days": 30}),
        _tk("Steps check", "11k over your 10k step goal", metric={"metric": "steps", "days": 14}),
        _tk("Recovery", "RHR sitting below baseline", metric={"metric": "rhr", "days": 14}),
    ])


# --- aggregate_scenario ---------------------------------------------------

def test_aggregate_counts_valid_invalid_and_flakes():
    results = [
        _three_with_steps(),          # valid, 3 takeaways, has steps
        _brief([]),                   # schema-invalid (min_length=1)
        {"error": "could not parse brief JSON"},
    ]
    rec = cb.aggregate_scenario(results, plan_active=False)
    assert rec["runs"] == 3
    assert rec["schema_valid"] == 1
    assert rec["schema_invalid"] == 1
    assert rec["flakes"] == ["could not parse brief JSON"]
    # Both non-error briefs are fingerprinted; the error one is not.
    assert len(rec["fingerprints"]) == 2
    # The empty brief (0 takeaways) makes the set structurally inconsistent.
    assert rec["consistency"]["consistent"] is False


def test_aggregate_truncates_flake_to_reason_line():
    # The composer's parse error embeds the full raw response after a blank
    # line; only the first reason line is recorded.
    verbose = "Could not parse brief JSON: bad delimiter\n\n{\"takeaways\": [{\"headline\": \"...prose...\"}]}"
    rec = cb.aggregate_scenario([{"error": verbose}], plan_active=False)
    assert rec["flakes"] == ["Could not parse brief JSON: bad delimiter"]
    assert all("prose" not in f for f in rec["flakes"])


def test_aggregate_all_errors_is_not_consistent():
    rec = cb.aggregate_scenario([{"error": "a"}, {"error": "b"}], plan_active=False)
    assert rec["schema_valid"] == 0
    assert rec["fingerprints"] == []
    assert rec["flakes"] == ["a", "b"]
    assert rec["consistency"]["consistent"] is False
    assert "no successful generations" in rec["consistency"]["divergences"]


def test_aggregate_clean_run_is_consistent():
    rec = cb.aggregate_scenario([_three_with_steps(), _three_with_steps()],
                                plan_active=False)
    assert rec["schema_valid"] == 2
    assert rec["flakes"] == []
    assert rec["consistency"]["consistent"] is True
    assert rec["consistency"]["divergences"] == []


def test_aggregate_flags_missing_steps_takeaway():
    no_steps = _brief([
        _tk("Workout", "easy run"),
        _tk("Recovery", "RHR low"),
        _tk("Conditioning", "CTL climbing"),
    ])
    rec = cb.aggregate_scenario([no_steps, no_steps], plan_active=False)
    assert rec["consistency"]["consistent"] is False
    assert any("steps takeaway missing" in d for d in rec["consistency"]["divergences"])


def test_aggregate_flags_plan_leak_when_no_plan_active():
    leaked = _three_with_steps(plan=True)  # mentions "training plan"
    rec = cb.aggregate_scenario([leaked, leaked], plan_active=False)
    assert rec["consistency"]["consistent"] is False
    assert any("plan content present" in d for d in rec["consistency"]["divergences"])


def test_aggregate_flags_plan_not_folded_when_plan_active():
    no_plan = _three_with_steps(plan=False)  # no plan keywords
    rec = cb.aggregate_scenario([no_plan, no_plan], plan_active=True)
    assert rec["plan_active"] is True
    assert rec["consistency"]["consistent"] is False
    assert any("NOT folded" in d for d in rec["consistency"]["divergences"])


def test_schema_valid_helper():
    assert cb._schema_valid(_three_with_steps()) is True
    assert cb._schema_valid(_brief([])) is False


# --- build_baseline / estimate -------------------------------------------

def test_build_baseline_shape():
    doc = cb.build_baseline({"green_light": {"runs": 2}}, model="claude-sonnet-4-6",
                            runs=2, captured_at="2026-06-26T08:00:00")
    assert doc["version"] == 1
    assert doc["model"] == "claude-sonnet-4-6"
    assert doc["runs_per_scenario"] == 2
    assert doc["captured_at"] == "2026-06-26T08:00:00"
    assert "green_light" in doc["scenarios"]
    assert "grounding.flag" in doc["note"]  # documents the Phase-4 backfill


def test_estimate_scales_with_generations():
    e = cb.estimate(["a", "b"], 3)
    assert e["generations"] == 6
    assert e["est_seconds"] == 6 * ab_brief.EST_SECONDS_PER_BRIEF
    assert e["est_output_tokens"] == 6 * ab_brief.EST_OUTPUT_TOKENS_PER_BRIEF


# --- CLI guards -----------------------------------------------------------

def test_dry_run_returns_zero_and_writes_nothing(tmp_path, capsys):
    out = tmp_path / "baseline.json"
    rc = cb.main(["--out", str(out)])
    assert rc == 0
    assert not out.exists()  # dry-run must not write
    printed = capsys.readouterr().out
    assert "generations" in printed and "--run" in printed


def test_run_refused_over_cap(tmp_path, capsys):
    out = tmp_path / "baseline.json"
    # 6 scenarios x 5 runs = 30 > MAX_GENERATIONS(16) → refuse before any call.
    rc = cb.main(["--run", "--runs", "5", "--out", str(out)])
    assert rc == 2
    assert not out.exists()
    assert "REFUSED" in capsys.readouterr().err


def test_unknown_scenario_arg_refused(capsys):
    rc = cb.main(["--scenarios", "bogus"])
    assert rc == 2
    assert "unknown scenario" in capsys.readouterr().err


def test_mock_path_writes_and_aggregates(tmp_path):
    canned = tmp_path / "canned.json"
    canned.write_text(json.dumps({
        "green_light": [_three_with_steps()],
        "taper_plan": [_three_with_steps(plan=True)],
    }))
    out = tmp_path / "baseline.json"
    rc = cb.main(["--mock", str(canned), "--out", str(out)])
    assert rc == 0
    doc = json.loads(out.read_text())
    assert doc["model"] == "mock"
    gl = doc["scenarios"]["green_light"]
    assert gl["schema_valid"] == 1 and gl["plan_active"] is False
    tp = doc["scenarios"]["taper_plan"]
    assert tp["plan_active"] is True and tp["consistency"]["consistent"] is True
