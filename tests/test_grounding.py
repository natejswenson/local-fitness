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


# --- 4a: public re-exports for cross-module reuse (plan_coach.ground_coaching_line) -

def test_parse_number_is_a_public_wrapper_around_parse():
    assert g.parse_number("9.2k") == 9200.0
    assert g.parse_number("not-a-number") is None


def test_numeric_tokens_matches_flag_s_tokenizing_and_skips_time_windows():
    # _NUM_RE's match includes trailing whitespace (its `\s*[kK%]?` tail) when
    # there's no k/% suffix — the raw (unstripped) token is exactly what
    # flag()/ground_coaching_line feed to parse_number, which strips it.
    text = "3 runs in 14 days, RHR 58 this morning"
    assert [t.strip() for t in g.numeric_tokens(text)] == ["3", "58"]


def test_nearest_pool_match_returns_the_closest_entry():
    pool = [(58.0, "rhr"), (11000.0, "steps")]
    assert g.nearest_pool_match(53.0, pool) == (58.0, "rhr")
    assert g.nearest_pool_match(10500.0, pool) == (11000.0, "steps")


@pytest.mark.parametrize("value,verdict", [
    (58.0, "faithful"),   # exact match
    (53.0, "flag"),       # within NEARBY_REL but not EXACT_REL of rhr=58
    (90.0, "ignore"),     # beyond NEARBY_REL of every pool entry
])
def test_classify_against_pool_bands(value, verdict):
    pool = [(58.0, "rhr"), (11000.0, "steps")]
    got_verdict, near_val, near_name = g.classify_against_pool(value, pool)
    assert got_verdict == verdict
    assert near_name == "rhr"
    assert near_val == 58.0


# --- kind-partitioned matching (2026-07-19 facet review) ---------------------
# Cross-unit magnitude collisions pinned production invention_rate at 1.000:
# an HR cap of 140 bpm flagged against a 147% steps-vs-goal value, a 94.7 run
# load flagged against a 101% percentage. Percent tokens and plain magnitudes
# are separate matching kinds now.

def test_plain_prose_number_never_matches_percent_pool_value():
    # The exact production false positive: "keep HR under 140" vs 147% of goal.
    ctx = _ctx(snapshot=[GroundedValue(
        name="avg_frac_of_goal", value=147, unit="pct", display="147% of goal")])
    assert g.flag(_brief("keep HR under 140 today"), ctx) == []


def test_percent_prose_number_never_matches_plain_pool_value():
    # 92% in prose sits near a plain 94.7 load — cross-kind, must not flag.
    ctx = _ctx(snapshot=[GroundedValue(
        name="run_load", value=94.7, unit="none", display="94.7")])
    assert g.flag(_brief("you closed 92% of the gap"), ctx) == []


def test_percent_token_still_flags_against_offset_percent_value():
    # Same-kind near-miss must STILL flag: 78% cited when the real value is 82%.
    ctx = _ctx(snapshot=[GroundedValue(
        name="sleep_score", value=82, unit="pct", display="82%")])
    flags = g.flag(_brief("sleep quality landed at 78% overnight"), ctx)
    assert len(flags) == 1
    assert flags[0].nearest_metric == "sleep_score"
    assert flags[0].token == "78%"
    assert flags[0].delta == -4.0


def test_faithful_percent_citation_is_not_flagged():
    ctx = _ctx(snapshot=[GroundedValue(
        name="sleep_score", value=82, unit="pct", display="82%")])
    assert g.flag(_brief("sleep quality held at 82% overnight"), ctx) == []


# --- Sign blindness fix (2026-07-27 facet review) ----------------------------
# Measured: `flag()` matched on `ax = abs(x)`, so a sign-inverted prose value
# (TSB "+22.4" cited when the real value is -22.4) read as an EXACT faithful
# citation — abs(+22.4) == abs(-22.4) — even though a positive vs. negative
# TSB is the difference between "you're rested, go hard" and "very fatigued,
# back off". These four cases are the exact evidence from that review,
# against a real tsb=-22.4.

def _tsb_ctx() -> BriefContext:
    # Isolated on purpose (no snapshot/steps/sleep noise) — matches how the
    # evidence was measured: a context where tsb is the only citable number,
    # so a "wildly wrong" +40.0 doesn't get compared against some OTHER
    # closer-by-coincidence metric (e.g. rhr=58) instead of tsb itself.
    return BriefContext(
        date="2026-06-26", user_name="Nate", candidates=[],
        training_load=[GroundedValue(name="tsb", value=-22.4, unit="none", display="-22.4")],
    )


def test_correctly_signed_tsb_citation_is_faithful():
    assert g.flag(_brief("Freshness is -22.4 today"), _tsb_ctx()) == []


def test_sign_inverted_exact_magnitude_is_flagged_as_sign_mismatch():
    # abs(+22.4) == abs(-22.4) — the exact case magnitude-only matching missed.
    flags = g.flag(_brief("Freshness is +22.4 today"), _tsb_ctx())
    assert len(flags) == 1
    assert flags[0].kind == "sign"
    assert flags[0].nearest_metric == "tsb"
    assert flags[0].delta == 44.8       # 22.4 - (-22.4), signed


def test_sign_inverted_and_wildly_off_magnitude_still_flags_as_sign_mismatch():
    # rel = (40 - 22.4) / 40 = 0.44 — past the NEARBY_REL magnitude band, but
    # a sign flip is qualitatively wrong regardless of magnitude drift.
    flags = g.flag(_brief("Freshness is +40.0 today"), _tsb_ctx())
    assert len(flags) == 1
    assert flags[0].kind == "sign"
    assert flags[0].nearest_metric == "tsb"
    assert flags[0].delta == 62.4       # 40.0 - (-22.4)


def test_same_sign_magnitude_miss_reports_the_real_signed_gap():
    # A 7% mis-state (real -22.4, cited -24.0): the OLD bug computed
    # delta = prose - abs(nearest) = -24.0 - 22.4 = -46.4 for a 1.6-unit gap.
    flags = g.flag(_brief("Freshness is -24.0 today"), _tsb_ctx())
    assert len(flags) == 1
    assert flags[0].kind == "value"
    assert flags[0].nearest_metric == "tsb"
    assert flags[0].delta == -1.6       # -24.0 - (-22.4), the REAL gap


def test_sign_mismatch_band_excludes_a_genuinely_unrelated_number():
    # rel = (21 - 10) / 21 = 0.524 — past the WIDENED sign-mismatch band too,
    # so this is just an unrelated number, not a flip of anything.
    ctx = _ctx(training_load=[GroundedValue(name="tsb", value=-10.0, unit="none", display="-10.0")])
    assert g.flag(_brief("21 minutes easy today"), ctx) == []


def test_duration_composite_does_not_inject_its_two_halves_as_bare_numbers():
    # "7h 28m" is `_hm()`'s own render for a sleep_seconds display — real
    # coach prose says this constantly. Before this fix, `_display_numbers`
    # scraped the rendered STRING and injected bare 7 and 28 into the pool;
    # now the pool holds the raw seconds value (26,880) instead, and the
    # tokenizer additionally skips both halves of "7h 28m" in PROSE outright.
    ctx = _ctx(snapshot=[
        GroundedValue(name="sleep_baseline", value=26880, unit="sec", display="7h 28m"),
        GroundedValue(name="intensity_minutes_vigorous", value=52, unit="min", display="52"),
    ], training_load=[GroundedValue(name="rhr_baseline", value=52, unit="bpm", display="52 bpm")])
    assert g.flag(_brief("you slept 7h 28m last night"), ctx) == []


def test_exact_duplicate_value_across_units_is_still_faithful_not_flagged():
    # The measured collision: intensity_minutes_vigorous=52 (min) and
    # rhr_baseline=52 (bpm) are the SAME number in different units. Citing
    # either exactly must read as faithful — an exact match always wins over
    # any near-but-unequal match in a different unit.
    ctx = _ctx(snapshot=[GroundedValue(name="intensity_minutes_vigorous", value=52, unit="min", display="52")],
               training_load=[GroundedValue(name="rhr_baseline", value=52, unit="bpm", display="52 bpm")])
    assert g.flag(_brief("52 vigorous minutes logged today"), ctx) == []


# --- _union() additions: workouts_14d / plan_today / anomalies --------------

def test_workouts_14d_citation_is_grounded_not_flagged():
    ctx = _ctx(workouts_14d=[
        {"date": "2026-06-19", "type": "long_run", "distance_mi": 8.0, "avg_hr": 132},
    ])
    assert g.flag(_brief("yesterday's 8mi at HR 132 was solid"), ctx) == []


def test_workouts_14d_off_value_is_flagged_against_the_workout():
    ctx = _ctx(workouts_14d=[
        {"date": "2026-06-19", "type": "long_run", "distance_mi": 8.0, "avg_hr": 132},
    ])
    flags = g.flag(_brief("that was an 8.9mi effort"), ctx)
    assert len(flags) == 1
    assert flags[0].nearest_metric == "workouts_14d.distance_mi"


def test_plan_today_target_distance_is_grounded():
    ctx = _ctx(plan_today={
        "active": True, "adherence_pct": 88,
        "today": {"date": "2026-06-26", "target_distance_m": 9656.064, "type": "long_run"},
    })
    # 9656.06 m == 6.00 mi — the generator would cite the converted figure.
    assert g.flag(_brief("today's plan calls for 6.0 miles"), ctx) == []


def test_plan_today_inactive_contributes_nothing_to_the_pool():
    ctx = _ctx(plan_today={"active": False})
    assert g._plan_today_pool_entries(ctx.plan_today) == []


def test_anomalies_citation_is_grounded():
    ctx = _ctx(anomalies=[
        {"date": "2026-06-24", "metric": "rhr", "value": 61, "baseline": 53, "sd_distance": 1.76},
    ])
    assert g.flag(_brief("RHR spiked to 61, about 1.76 SD above your baseline"), ctx) == []


def test_anomalies_off_value_is_flagged():
    ctx = _ctx(anomalies=[
        {"date": "2026-06-24", "metric": "rhr", "value": 61, "baseline": 53, "sd_distance": 1.76},
    ])
    flags = g.flag(_brief("RHR spiked to 65 this morning"), ctx)
    assert len(flags) == 1
    assert flags[0].nearest_metric == "anomalies.rhr"


# --------------------------------------------------------------------------- #
# Issue #217 (2026-08-10): the live morning brief measured invention_rate
# 1.000 — 7 flags, every one a false positive. Three mechanisms, fixed
# together: (1) "45 steps" matched rhr=50 because a prose unit word didn't
# bind its token to a unit; (2) "3-4mi" tokenizes as -4 (range dash) and
# sign-flagged against distance 4.2; (3) a table's "-1.2%" delta
# sign-flagged against a positive frac_of_goal. The sign check now fires
# only for prose-positive vs pool-negative (the measured true case).
# --------------------------------------------------------------------------- #
def test_issue_217_the_live_brief_carries_no_false_positives():
    ctx = _ctx(
        snapshot=[
            GroundedValue(name="rhr", value=50, unit="bpm", display="50 bpm"),
            GroundedValue(name="rhr_baseline", value=51, unit="bpm", display="51 bpm"),
            GroundedValue(name="steps", value=10045, unit="steps", display="10,045"),
            GroundedValue(name="frac_of_goal", value=1.0, unit="pct", display="100.4%"),
        ],
        training_load=[],
        workouts_14d=[{"distance_mi": 4.2, "avg_hr": 132}],
    )
    b = _brief(
        "Yesterday cleared 10,000 by 45 steps. Walk 3-4mi today.",
        "| RHR | 50 bpm | 51 bpm | -1.2% | ↓ |",
    )
    assert g.flag(b, ctx) == []
    assert g.invention_rate(b, ctx) == 0.0


def test_a_unit_word_binds_its_token_and_a_bare_token_still_flags():
    ctx = _ctx(snapshot=[
        GroundedValue(name="rhr", value=50, unit="bpm", display="50 bpm"),
        GroundedValue(name="steps", value=10045, unit="steps", display="10,045"),
    ], training_load=[])
    # "45 steps" is a steps quantity: it may only match steps-unit entries,
    # where 45 is nowhere near 10,045 -> ignored, not an rhr mis-state.
    assert g.flag(_brief("won by 45 steps"), ctx) == []
    # The same magnitude WITHOUT a unit word keeps the old sensitivity: a
    # bare 45 within the nearby band of rhr=50 is still a value flag.
    flags = g.flag(_brief("sitting at 45 this morning"), ctx)
    assert [f.kind for f in flags] == ["value"]
    assert flags[0].nearest_metric == "rhr"


def test_a_unit_bound_token_with_no_same_unit_pool_entry_is_skipped():
    # "13 mi" when the pool has no mi entries must NOT fall back to the
    # shared bucket (load=13.5 sits inside the nearby band) — the fallback
    # is the misbind.
    ctx = _ctx(snapshot=[
        GroundedValue(name="load", value=13.5, unit="none", display="13.5"),
    ], training_load=[])
    assert g.flag(_brief("ran 13 mi this week"), ctx) == []


def test_a_range_dash_negative_is_not_a_sign_inversion():
    ctx = _ctx(snapshot=[
        GroundedValue(name="distance", value=4.2, unit="mi", display="4.2 mi"),
    ], training_load=[])
    # "3-4mi" tokenizes as 3 then -4; -4 abs-matches 4.2 inside the sign
    # band but distance can't be negative — no inversion, no flag.
    assert g.flag(_brief("prescribed a 3-4mi walk"), ctx) == []


def test_a_negative_delta_is_not_a_sign_inversion_of_a_positive_pct():
    ctx = _ctx(snapshot=[
        GroundedValue(name="frac_of_goal", value=2.0, unit="pct", display="200%"),
    ], training_load=[])
    # Old behavior: -1.2 vs 2.0 -> rel 0.4, opposite signs -> kind="sign".
    # A negative prose percent near an always-positive pct is a computed
    # delta, not an inversion.
    assert g.flag(_brief("down -1.2% on the day"), ctx) == []


def test_prose_positive_vs_pool_negative_still_flags_sign_inversion():
    # The measured TRUE case the sign check exists for is untouched: a
    # negative pool metric (TSB -22) cited positive.
    flags = g.flag(_brief("TSB is +22, you're rested"), _ctx())
    assert [f.kind for f in flags] == ["sign"]
    assert flags[0].nearest_metric == "tsb"


# --------------------------------------------------------------------------- #
# Second live audit (2026-08-10 13:48 brief): two more false-positive
# classes. The generator's markdown renders negatives with the typographic
# MINUS SIGN (U+2212), which the ASCII tokenizer read as a positive ("−7.5"
# sign-flagged against tsb=-7.5), and calendar dates ("Aug 7", "Sept 18")
# matched metric magnitudes. Fixed by offset-safe U+2212 normalization and
# a month-name-before-token veto (the mirror of _WINDOW_AFTER).
# --------------------------------------------------------------------------- #
def test_unicode_minus_reads_as_a_negative_citation():
    # "TSB is −7.5" (U+2212) must parse negative and match tsb=-7.5 as
    # faithful — it was tokenizing as +7.5 and sign-flagging.
    ctx = _ctx(training_load=[
        GroundedValue(name="tsb", value=-7.5, unit="none", display="-7.5"),
    ])
    assert g.flag(_brief("TSB is −7.5 (neutral)"), ctx) == []


def test_unicode_minus_percent_delta_is_not_an_inversion():
    ctx = _ctx(snapshot=[
        GroundedValue(name="frac_of_goal", value=2.0, unit="pct", display="200%"),
    ], training_load=[])
    assert g.flag(_brief("| RHR | −1.2% | ↓ |"), ctx) == []


def test_a_date_after_a_month_name_is_never_a_metric_claim():
    ctx = _ctx(training_load=[
        GroundedValue(name="tsb", value=-7.5, unit="none", display="-7.5"),
    ])
    # "Aug 7" sat at rel 0.067 of |tsb| and sign-flagged three times live.
    assert g.flag(_brief("The Aug 7 long run arrested the slide"), ctx) == []
    # Full and dotted forms too.
    assert g.flag(_brief("Back on August 7 you ran long"), ctx) == []
    assert g.flag(_brief("race day is Sept. 18"), _ctx(snapshot=[
        GroundedValue(name="load", value=17.5, unit="none", display="17.5"),
    ], training_load=[])) == []


def test_a_bare_number_not_after_a_month_keeps_sensitivity():
    # The same magnitude WITHOUT a month word still sign-flags against a
    # negative pool metric — the veto is positional, not a blanket skip.
    ctx = _ctx(training_load=[
        GroundedValue(name="tsb", value=-7.5, unit="none", display="-7.5"),
    ])
    flags = g.flag(_brief("freshness is sitting at 7 today"), ctx)
    assert [f.kind for f in flags] == ["sign"]


def test_numeric_tokens_applies_the_same_normalization_and_date_veto():
    toks = g.numeric_tokens("TSB −22 after the Aug 7 run, 3 easy miles")
    assert [t.strip() for t in toks] == ["-22", "3"]


def test_second_live_audit_brief_carries_no_false_positives():
    # Frozen from the 2026-08-10 13:48 generation — the sentences that
    # produced [tsb:7.5], [tsb:7]x2 (later x3), [workouts_14d.training_load:18].
    ctx = _ctx(
        snapshot=[
            GroundedValue(name="rhr", value=50, unit="bpm", display="50 bpm"),
        ],
        training_load=[
            GroundedValue(name="tsb", value=-7.5, unit="none", display="-7.5"),
        ],
        workouts_14d=[{"distance_mi": 9.0, "avg_hr": 148, "training_load": 16.9}],
    )
    b = _brief(
        "Two consecutive misses. TSB is −7.5 (neutral — moderate fatigue).",
        "The Aug 7 long run is what's keeping the number from sliding.",
        "39 days to race Sept 18 is a problem you can see on paper.",
    )
    assert g.flag(b, ctx) == []
    assert g.invention_rate(b, ctx) == 0.0
