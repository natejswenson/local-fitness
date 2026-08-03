# 2026-08-02 — three passing numbers and an F

## Why

Nate ran his easy 5 and the card came back a **C**. He pushed back on the one
row that carried it:

> "pretty sure it is wrong, got an f for heart rate and it was prety much on
> target"

He was reading the row correctly. The row was wrong.

```
| Metric | Actual | Expected | Delta | Grade |
|---|---|---|---|---|
| Avg HR | 139 bpm | ≤ 140 bpm | -1% | F |
```

139 against a 140 ceiling, one percent under, F. Nothing in that row can
produce that letter. Either the grade is broken or the row is.

## The measurement

The grade was fine. `hr_cap_deviation` measures two independent ways to breach
a prescribed cap and takes the worse:

| Axis | This run | Deviation |
|---|---|---|
| Average over cap | 139 vs 140 | **0.0** — clean, that's an A |
| Time over cap, past the 5% grace | 58% − 5% | **0.53** |

```
>>> rc.hr_cap_deviation(139.0, 140.0, None)    # average axis alone
0.0
>>> rc.hr_cap_deviation(139.0, 140.0, 0.58)    # with the time axis
0.53
```

Miles 2, 4 and 5 all sat above 140. 58% of the session ran over a ceiling the
plan stated in writing, which is disobedience, and the function's own docstring
already maps it — *"~5% is noise, 20% over is a C, a third of the run over is an
F."* 53% is well past a third.

So the letter was earned on the time axis, and every number printed beside it
described the average axis — the one that *passed*. The quantity that produced
the F appeared only in a footnote under the table.

## The actual bug

`hr_cap_deviation` takes `max(over_avg, over_time)` and then throws away which
one won. Two axes in different units — bpm over a ceiling, fraction of a run
over it — collapse to one number, and the row defaults to displaying bpm.

The codebase had already solved this, one metric over. The pace branch:

> `actual` stays the number the grade was measured against, so the Delta column
> can never compare two different quantities. When that isn't the run average,
> `actual_display` says which number it is.

Quality-day pace grades on the fastest rep, not the run average, and it says so.
HR grades on time-over-cap and didn't. Same rule, one metric missed it.

There's a stronger version of this already written into the module, about the
overall grade:

> A card that prints "Overall: A" above a row reading F is not reporting a
> grade, it is averaging away the finding.

This was that failure one level down. A row that prints `-1%` beside an F is not
reporting a metric, it is hiding the finding inside a footnote.

## The fix

`hr_cap_axis()` names the governing breach — `"time"`, `"average"`, or `None`
when nothing was breached. It shares `_hr_cap_axes()` with `hr_cap_deviation()`,
so the letter and the row explaining it are computed from one formula and cannot
drift. When time governs, actual/expected/delta all move to that axis:

```
| Avg HR | 58% above cap (avg 139 bpm) | ≤ 5% above cap | 53% over | F |
```

The average stays in the cell as a parenthetical rather than being dropped — it
is the reason the row *looks* clean, so it's the thing the reader needs
reconciled, not hidden. When the average is the axis that breached, the row
stays in bpm exactly as it was.

No grade moved. Deviations, letters and GPAs are unchanged; `actual`/`expected`
stay numeric bpm so stored cards, the note line and the coach read are all
untouched. Markdown and PDF share the three display helpers, so both surfaces
got it.

## What the tests pin

The end-to-end one asserts the rendered row byte-for-byte, and against the
pre-fix code it fails with exactly the card Nate was looking at:

```
- | Avg HR | 58% above cap (avg 139 bpm) | ≤ 5% above cap | 53% over | F |
+ | Avg HR | 139 bpm | ≤ 140 bpm | -1% | F |
```

Two-sided, because a one-sided version would pass if the row moved to the time
axis unconditionally: an average-governed breach must *keep* the bpm display,
and a compliant run must name no axis at all and still read "in range".

One test premise was wrong on the first pass, which is worth recording. The
"average breach" fixture used five splits at 147–149 against a 140 cap — and
that is time-governed, not average-governed, because every second above the cap
is also weight on the mean. The average can only be the sole live axis when the
splits carry no HR at all (the documented degrade path). The code was right and
the fixture was wrong, which is the good direction for that to happen in.

## The part that isn't fixed

The `/eval` skill couldn't grade this run. It resolves a contract from
`skills/<name>/skills/<name>/SKILL.md` and refused outright — `local-fitness` is
an MCP server, not a marketplace skill, so there are no citable clauses to grade
against and no way to convert the finding into a permanent eval case there. The
regression lives in this repo's own suite instead, which is the right home for
it, but "an MCP server has no gradeable contract" is a real gap.
