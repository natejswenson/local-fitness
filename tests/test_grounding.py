"""Tests for agent/grounding.py — the advisory invention-rate signal.

The bar (design 'Requires tests'): flag a deliberately-corrupted metric value,
do NOT flag a correctly-converted miles/pace/duration token, and never gate /
mutate / raise. These assert the real flagged token + delta, not stand-ins.
"""
from __future__ import annotations

import pytest

from local_fitness.agent import grounding as g
from local_fitness.agent.schemas import (
    Brief,
    BriefContext,
    CandidateTakeaway,
    GroundedValue,
    Takeaway,
)


def _ctx(**over) -> BriefContext:
    base = dict(
        date="2026-06-26", user_name="Nate", candidates=[],
        snapshot=[
            GroundedValue(name="rhr", value=58, unit="bpm", display="58 bpm"),
            GroundedValue(name="steps", value=11000, unit="steps", display="11,000"),
            GroundedValue(name="sleep_seconds", value=27000, unit="sec", display="7h 30m"),
        ],
        training_load=[GroundedValue(name="tsb", value=-22, unit="none", display="-22")],
        step_goal=10000,
    )
    base.update(over)
    return BriefContext(**base)


def _brief(*summaries: str) -> Brief:
    return Brief(date="2026-06-26", user_name="Nate",
                 takeaways=[Takeaway(headline="h", summary=s, details="d") for s in summaries])


def test_faithful_citations_are_not_flagged():
    b = _brief("RHR 58, slept 7h 30m, TSB -22, 11,000 steps over the 10,000 goal")
    assert g.flag(b, _ctx()) == []
    assert g.invention_rate(b, _ctx()) == 0.0


def test_corrupted_metric_value_is_flagged_with_delta():
    b = _brief("RHR is sitting at 53 this morning")   # real is 58 → ~8.6% off
    flags = g.flag(b, _ctx())
    assert len(flags) == 1
    assert flags[0].nearest_metric == "rhr"
    assert flags[0].token == "53"
    assert flags[0].delta == -5.0
    assert g.invention_rate(b, _ctx()) == 1.0


def test_time_window_numbers_are_not_flagged():
    # "14 days" / "7-day" are windows, not metric claims — even though 14 sits
    # near a metric magnitude, the trailing time-unit word suppresses the flag.
    ctx = _ctx(snapshot=[GroundedValue(name="imm", value=15, unit="min", display="15")])
    assert g.flag(_brief("3 runs in 14 days, down from the prior 14-day block"), ctx) == []
    assert g.flag(_brief("your 7-day average held over 4 weeks"), ctx) == []


def test_wild_number_is_ignored_as_a_different_quantity():
    # 90 is far from every known metric → reads as a prescription, not a
    # mis-stated metric → not flagged (contradiction-only).
    assert g.flag(_brief("go walk for 90 minutes"), _ctx()) == []


def test_prescription_and_goal_numbers_are_not_flagged():
    # "45 min" (prescription) and "10,000" (goal, a context scalar) must pass.
    assert g.flag(_brief("easy 45 min run; your 10,000 step goal still stands"), _ctx()) == []


def test_correctly_converted_units_are_not_flagged():
    # A miles distance whose display is already the converted value: the prose
    # citing that converted number matches exactly → no flag.
    ctx = _ctx(snapshot=[GroundedValue(name="distance", value=6.2, unit="mi", display="6.2")])
    assert g.flag(_brief("you ran 6.2 miles yesterday"), ctx) == []


def test_abs_floor_suppresses_tiny_diffs_on_small_values():
    # TSB display -22; prose -22.3 differs by 0.3 (< ABS_FLOOR) → not a flag.
    assert g.flag(_brief("freshness is -22.3 today"), _ctx()) == []


def test_candidate_metrics_are_part_of_the_grounded_pool():
    ctx = _ctx(candidates=[CandidateTakeaway(
        category="conditioning", fired_triggers=["ctl_shifted"],
        metrics=[GroundedValue(name="ctl", value=14.2, unit="none", display="14.2")],
        suggested_tone="positive", evidence="ctl up")])
    # Citing the candidate's CTL faithfully → no flag.
    assert g.flag(_brief("your fitness (CTL) is 14.2"), ctx) == []
    # A close-but-off CTL → flagged against the candidate metric.
    flags = g.flag(_brief("CTL is 13.0 now"), ctx)
    assert len(flags) == 1 and flags[0].nearest_metric == "ctl"


def test_days_to_race_is_a_grounded_scalar():
    ctx = _ctx(days_to_race=10)
    # "10 days out" cites the context's days-to-race → not a flag.
    assert g.flag(_brief("your race is 10 days out"), ctx) == []


def test_empty_context_pool_yields_no_flags():
    empty = BriefContext(date="d", user_name="N", candidates=[])
    assert g.flag(_brief("RHR 58, steps 9000"), empty) == []


def test_invention_rate_is_fraction_of_takeaways_with_a_flag():
    # 3 takeaways, exactly one carries a corrupted metric → 1/3.
    b = _brief("RHR 58 steady", "RHR drifted to 53", "11,000 steps, nice")
    assert g.invention_rate(b, _ctx()) == pytest.approx(0.333, abs=0.001)


def test_invention_rate_empty_brief_is_zero():
    assert g.invention_rate(Brief(date="d", user_name="N",
                                  takeaways=[Takeaway(headline="h", summary="no numbers here",
                                                      details="d")]), _ctx()) == 0.0


def test_flag_never_mutates_the_brief():
    b = _brief("RHR 53 this morning")
    before = b.model_dump()
    g.flag(b, _ctx())
    assert b.model_dump() == before     # advisory: read-only over the brief


@pytest.mark.parametrize("token,expected", [
    ("11,000", 11000.0), ("9.2k", 9200.0), ("120%", 120.0),
    ("-22", -22.0), ("+3.2", 3.2), ("58", 58.0),
    ("", None), ("abc", None), ("k", None),
])
def test_parse_numeric_tokens(token, expected):
    assert g._parse(token) == expected
