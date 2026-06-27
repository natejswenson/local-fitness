"""Tests for the A/B brief harness comparison logic (no model calls)."""
from __future__ import annotations

import ab_brief


def _brief(takeaways):
    return {"date": "2026-06-15", "user_name": "Nate", "takeaways": takeaways}


def _tk(headline="x", summary="y", details="z", tone="neutral", metric=None):
    t = {"headline": headline, "summary": summary, "details": details, "tone": tone}
    if metric:
        t["metric"] = {"metric": metric, "days": 30}
    return t


def test_extract_features_basic():
    b = _brief([
        _tk(headline="Today: 5mi easy", details="hold goal pace", tone="positive", metric="ctl"),
        _tk(headline="Steps update", summary="11k steps yesterday", tone="neutral"),
    ])
    f = ab_brief.extract_features(b)
    assert f["n_takeaways"] == 2
    assert f["has_steps"] is True
    assert f["mentions_plan"] is True  # "goal pace"
    assert f["metrics"] == ["ctl"]


def test_compare_consistent():
    feats = {
        "sonnet#1": ab_brief.extract_features(_brief([
            _tk(headline="Workout: 5mi", details="prescribed plan session"),
            _tk(summary="steps were 10k"), _tk(), _tk(),
        ])),
        "opus#1": ab_brief.extract_features(_brief([
            _tk(headline="Run 5mi", details="today's session per the plan"),
            _tk(summary="step count solid"), _tk(),
        ])),
    }
    res = ab_brief.compare(feats, plan_active=True)
    assert res["consistent"], res["divergences"]


def test_compare_flags_missing_steps():
    feats = {
        "sonnet#1": ab_brief.extract_features(_brief([_tk(summary="ran 5mi"), _tk(), _tk()])),
    }
    res = ab_brief.compare(feats, plan_active=False)
    assert not res["consistent"]
    assert any("steps" in d for d in res["divergences"])


def test_compare_flags_plan_leak_when_inactive():
    feats = {
        "sonnet#1": ab_brief.extract_features(_brief([
            _tk(details="adherence to your training plan"), _tk(summary="steps 9k"), _tk(),
        ])),
    }
    res = ab_brief.compare(feats, plan_active=False)
    assert not res["consistent"]
    assert any("NO active plan" in d for d in res["divergences"])


def test_compare_flags_plan_not_folded_when_active():
    feats = {
        "sonnet#1": ab_brief.extract_features(_brief([
            _tk(headline="Easy run"), _tk(summary="steps 9k"), _tk(),
        ])),
    }
    res = ab_brief.compare(feats, plan_active=True)
    assert not res["consistent"]
    assert any("NOT folded" in d for d in res["divergences"])


def test_compare_flags_count_variance():
    feats = {
        "a": ab_brief.extract_features(_brief([_tk(summary="steps"), _tk(), _tk()])),       # 3
        "b": ab_brief.extract_features(_brief([_tk(summary="steps"), _tk(), _tk(), _tk(), _tk()])),  # 5
    }
    res = ab_brief.compare(feats, plan_active=False)
    assert not res["consistent"]
    assert any("varies" in d for d in res["divergences"])


def test_estimate_and_cap():
    est = ab_brief.estimate(["a", "b"], runs=2)
    assert est["generations"] == 4
    # the CLI refuses generations over the cap
    rc = ab_brief.main(["--run", "--models", "a,b,c,d,e", "--runs", "2"])  # 10 > cap 8
    assert rc == 2


def test_dry_run_is_default_and_free(capsys):
    rc = ab_brief.main([])  # no --run, no --mock
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run" not in out.lower() or "Re-run with --run" in out
    assert "generations" in out


# --- Phase 0: --run is robust — failures are recorded, not crashes; save=False --

def test_generate_features_records_failure_gracefully(monkeypatch):
    import asyncio

    from local_fitness.agent import briefing
    from local_fitness.agent.schemas import Brief

    seen_save = []

    async def fake_generate(model, save=False):
        seen_save.append(save)
        if model == "bad":
            raise ValueError("Could not parse brief JSON")
        return Brief.model_validate(
            {"date": "2026-06-15", "user_name": "Nate",
             "takeaways": [{"headline": "h", "summary": "steps were low",
                            "tone": "neutral", "details": "d"}]}
        )

    monkeypatch.setattr(briefing, "_generate", fake_generate)
    feats = asyncio.run(ab_brief._generate_features(["good", "bad"], runs=1))

    assert "error" in feats["bad#1"]                 # failure recorded, not raised
    assert feats["good#1"]["n_takeaways"] == 1       # the good generation still parsed
    assert seen_save == [False, False]               # eval path is always save=False

    res = ab_brief.compare(feats, plan_active=False)
    assert res["failures"] and not res["consistent"]  # flake surfaced + flagged
    ab_brief._report(feats, res)                       # must not crash on an error entry


def test_compare_all_failures_is_not_consistent():
    feats = {"a#1": {"error": "boom"}, "b#1": {"error": "boom"}}
    res = ab_brief.compare(feats, plan_active=False)
    assert not res["consistent"] and len(res["failures"]) == 2
