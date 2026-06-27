"""Tests for the structured-output schemas in agent/schemas.py."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from local_fitness.agent import schemas


def test_grounded_value_roundtrips_and_validates_unit():
    g = schemas.GroundedValue(name="rhr", value=56.0, unit="bpm", display="56 bpm")
    assert (g.name, g.value, g.unit, g.display) == ("rhr", 56.0, "bpm", "56 bpm")


def test_grounded_value_rejects_unknown_unit():
    with pytest.raises(ValidationError):
        schemas.GroundedValue(name="x", value=1.0, unit="furlongs", display="1")


def test_candidate_takeaway_carries_typed_metrics():
    c = schemas.CandidateTakeaway(
        category="workout",
        fired_triggers=["tsb_positive"],
        metrics=[schemas.GroundedValue(name="tsb", value=3.2, unit="none", display="+3.2")],
        suggested_tone="positive",
        chart_metric=schemas.TakeawayMetric(metric="tsb", days=30),
        evidence="TSB +3.2 — fresh, push",
    )
    assert c.metrics[0].name == "tsb"
    assert c.chart_metric.metric == "tsb"
    assert c.suggested_tone == "positive"


def test_candidate_takeaway_chart_metric_optional():
    c = schemas.CandidateTakeaway(
        category="recovery", fired_triggers=[], metrics=[],
        suggested_tone="neutral", evidence="nothing fired",
    )
    assert c.chart_metric is None


def test_brief_context_defaults_are_empty_not_shared():
    a = schemas.BriefContext(date="2026-06-26", user_name="Nate", candidates=[])
    b = schemas.BriefContext(date="2026-06-27", user_name="Nate", candidates=[])
    a.snapshot.append(schemas.GroundedValue(name="rhr", value=50, unit="bpm", display="50 bpm"))
    assert a.snapshot and b.snapshot == []  # default_factory → no shared mutable
    assert a.plan_today is None and a.days_to_race is None and a.step_goal is None


def test_brief_context_full_payload():
    ctx = schemas.BriefContext(
        date="2026-06-26", user_name="Nate",
        candidates=[schemas.CandidateTakeaway(
            category="steps", fired_triggers=["under_goal"], metrics=[],
            suggested_tone="critical", evidence="4.2k vs 10k")],
        snapshot=[schemas.GroundedValue(name="steps", value=4200, unit="steps", display="4,200")],
        training_load=[schemas.GroundedValue(name="tsb", value=-22, unit="none", display="-22")],
        plan_today={"type": "easy", "distance_mi": 3.1},
        step_goal=10000, days_to_race=10,
    )
    assert ctx.candidates[0].category == "steps"
    assert ctx.plan_today["type"] == "easy"
    assert ctx.step_goal == 10000 and ctx.days_to_race == 10


def test_takeaway_minimal_valid():
    t = schemas.Takeaway(headline="Run easy today", summary="RHR is up a touch", details="...")
    assert t.tone == "neutral"
    assert t.metric is None


def test_tone_whitespace_collapsed():
    # Models occasionally stream `"crit\nical"` — the validator must heal it.
    t = schemas.Takeaway(headline="h", summary="s", details="d", tone="crit\nical")
    assert t.tone == "critical"


def test_prose_fields_edge_trimmed_but_internal_preserved():
    t = schemas.Takeaway(
        headline="  Lead  ",
        summary="why",
        details="line one\nline two  ",
    )
    assert t.headline == "Lead"
    assert "\n" in t.details  # internal newline preserved in prose
    assert not t.details.endswith(" ")


def test_metric_whitespace_collapsed():
    m = schemas.TakeawayMetric(metric="r\nhr")
    assert m.metric == "rhr"
    assert m.days == 14


def test_metric_days_bounds():
    with pytest.raises(ValidationError):
        schemas.TakeawayMetric(metric="rhr", days=3)
    with pytest.raises(ValidationError):
        schemas.TakeawayMetric(metric="rhr", days=1000)


def test_bad_tone_rejected():
    with pytest.raises(ValidationError):
        schemas.Takeaway(headline="h", summary="s", details="d", tone="ecstatic")


def test_bad_metric_rejected():
    with pytest.raises(ValidationError):
        schemas.TakeawayMetric(metric="heart_rate_variability")


def test_brief_requires_at_least_one_takeaway():
    with pytest.raises(ValidationError):
        schemas.Brief(date="2026-06-06", user_name="Dana", takeaways=[])


def test_brief_caps_at_five_takeaways():
    one = schemas.Takeaway(headline="h", summary="s", details="d")
    with pytest.raises(ValidationError):
        schemas.Brief(date="2026-06-06", user_name="Dana", takeaways=[one] * 6)


def test_brief_generated_at_optional():
    one = schemas.Takeaway(headline="h", summary="s", details="d")
    b = schemas.Brief(date="2026-06-06", user_name="Dana", takeaways=[one])
    assert b.generated_at is None
