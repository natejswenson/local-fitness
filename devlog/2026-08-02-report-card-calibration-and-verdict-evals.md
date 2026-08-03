# 2026-08-02 — the rubric had 141 tests and no evals

## Why

An audit of why the HR-cap defect survived produced one number that made the
rest of it academic: across roughly twelve report-card grading defects, **zero
were caught by the test suite**. Seven came from a human reading a rendered
card, four from a multi-agent code audit, three from the author mid-build. The
last three consecutive findings are all Nate looking at a card and saying that's
wrong.

That is not a coverage problem. `report_card.py` sits at 98% and the module has
141 tests. They assert that the rubric computes what it says it computes — a
deviation float, a band boundary, a display string. **Not one of them could fail
when the rubric's answer was wrong.** `test_hr_cap_deviation_takes_the_worse_of_average_and_time`
checked that `(126, 140, 0.25)` produced `0.20` and never asked what grade
`0.20` was, so it passed green while the axis emitted only A and F.

Two things were missing, and they are different things: nothing measured the
constants against reality, and nothing asserted a verdict.

## P1 — `scripts/calibrate_report_card.py`

CLAUDE.md has carried "calibrate bands against real data, not intuition" since
0.26.0 and nothing executed it. The failure signature is the same every time and
it is measurable: **a band table stops discriminating.** The 0.40.0 time axis
emitted 32 F / 7 A / 4 D over 90 days with B and C empty. The 0.88 easy-HR
ceiling before it demanded a number that appeared in 1 of 13 runs. Both were one
query away from being obvious, against data sitting on disk the whole time.

The script recomputes every graded metric over a trailing window through the
real production path — `load_report_card_inputs` → `build_card`, never a
reimplementation that could quietly disagree with what ships — and fails on
either signature: punitive skew (>60% D/F) or dead bands (≥2 letters unused).

**It catches the bug it was built for.** On `23ee63a` — the commit where a human
investigation looked at this exact card and concluded *"The grade was fine"*:

```
hr (prescribed cap)       1   0   0   0   9     10  FAIL — 90% of runs graded D/F — punitive skew
                       governed by: GRADE_BANDS, HR_CAP_NOISE_BPM, HR_CAP_BPM_SCALE
```

On the corrected rubric, same window, same data:

```
hr (prescribed cap)       3   0   3   1   3     10  ok — 4/5 bands used, F 30%, D/F 40%
```

The F-cap rate over the window falls 21% → 12%.

### The asymmetry, which was this check's own first bug

The first draft failed any letter above a flat 60% share. Run against the
*corrected* code it flagged three of five metrics:

```
distance    33  2  1  5  1   FAIL — 79% of runs graded A
pace        27  9  4  1  1   FAIL — 64% of runs graded A
continuity  29  1  3  2  3   FAIL — 76% of runs graded A
```

All three are healthy. Distance grades 79% A because the distances are being
hit, and all five bands are in use. A gate that fires on that trains everyone to
ignore it — which is precisely how a rubric ends up shipping F on a compliant
run for four days.

So the rule is one-sided on purpose. A rubric measures compliance with a
prescription the athlete is *trying* to follow: heavy compliance is the expected
state, heavy failure is not. Concentration in a passing grade is evidence of
nothing and is printed, not gated. The dead-bands test is the two-sided half,
and it is the weaker claim by design — it says a metric is UNPROVEN over this
window, not broken.

### Not in CI, and that is not laziness

It needs a populated `data/fitness.db`. Fabricating one would only ask the
fixture whether the fixture agrees with itself, which is the exact circularity
that let the 0.40.0 docstring validate the 0.40.0 feature. It is a manual gate:
run it before changing a constant, paste the output into the devlog.
`test_the_gate_is_not_wired_into_ci` fails if anyone adds it to a workflow, so
the decision has to be read before it can be reversed.

Read-only is structural — `mode=ro` through a URI, so SQLite refuses a write
rather than the script remembering not to attempt one.

## P2 — `tests/evals/report_cards.py`

The report card's missing analogue of `scripts/eval_fixtures.py`. Five
fabricated scenarios, each asserting the **overall letter a run deserves**, run
through a real SQLite database so the reference-pool filter is inside what they
grade rather than handed in as a pre-made `reference` dict.

Expected verdicts are BOUNDS in `EXPECTED_VERDICTS`, each with a stated reason,
because "a run that obeyed its prescription must not be marked down" is the
actual contract and an exact letter would fight ordinary recalibration. A
scenario with no entry fails `test_every_scenario_declares_a_verdict` — that is
what stops the suite drifting back into asserting mechanics.

Against `23ee63a`, three fail. The one worth quoting:

```
test_the_three_cap_scenarios_are_strictly_ordered
>   assert _points(straddled["overall"]["grade"]) > _points(blown["overall"]["grade"])
E   AssertionError: assert 2.0 > 2.0
```

Straddling a 140 ceiling by 1.2 bpm and blowing it by 15 bpm both graded **C**.
The ordering assertion names that collapse in one line, and it also catches the
opposite over-correction — a fix that made the cap never bite would fail the
same test from the other end.

## Verification

Every new test was checked by breaking what it guards, not by asserting it
would. Four mutations to `report_card.py`, each reverted:

| Mutation | Caught by |
|---|---|
| `QUALITY_MIN_SPLIT_M` 300 → 99999 (no split is a rep) | `test_rep_pace_is_graded_on_the_reps_not_the_run_average` |
| pool filter ignores measured locomotion | `test_the_reference_pool_excludes_walks...` **and** the `walk_mislabelled` verdict |

...and four to the script itself:

| Mutation | Caught by |
|---|---|
| symmetric share rule (the first draft) | `test_a_healthy_skew_toward_passing_is_not_a_failure` |
| `mode=ro` → `mode=rw` | `test_the_connection_is_physically_read_only` |
| locomotion gate removed | `test_only_running_efforts_are_selected` |
| window upper bound removed | `test_the_window_is_bounded_at_both_ends` |

The last two of those were real defects the tests found during the build, not
planted ones: `sqlite3.connect()` is lazy so a corrupt file opened fine and only
raised on first query, tracebacking past the error handler; and `--days N` had
no upper bound, so any past anchor silently graded the entire history under a
window it named `N`.

## The finding I am not fixing

The gate fails on the *corrected* code, on a metric nobody was looking at:

```
hr (rolling band)        29   1   0   2   0     32  FAIL — dead bands — C, F never used (3/5)
                       governed by: GRADE_BANDS, HR_BANDS
```

Uncapped HR — every run without a prescribed ceiling — grades A 91% of the time
and has never emitted a C or an F across 90 days. It carries 24% of an easy
day's weight and is very close to a constant.

This is the 0.26.0 failure wearing different clothes. That devlog's words were
*"HR and load are 40% of the composite and had quietly become constants: A+ for
clearing a walking bar."* Load was removed from the composite in 0.40.0 for
exactly this. Uncapped HR is now the last metric with the property, and
`HR_BANDS` was last calibrated 2026-07-20 against a 13-run window.

Not touched here, deliberately: this branch is the instrument, and changing a
constant in the same commit that introduces the thing that measures it is how
you get a docstring validating its own feature. Filed for a decision.

`CONTINUITY_TOLERANCE` — flagged in the audit as never having been calibrated —
passes cleanly: 5/5 bands, 13% D/F.
