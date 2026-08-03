# 2026-08-02 — the report card was two pages, and its A+ looked like a trophy

0.41.0. A UX pass on `workout_report_card`, prompted by the card simply not
reading well. Four changes; the first is a straight bug.

## The durable lesson: a fix documented in one place is not a fix

`visuals.py` has carried this comment since 2026-07-22, on `img.chart`:

```
/* Capped by HEIGHT, not just width. `max-width: 100%` alone lets the chart's
   own aspect ratio decide how much of the page it eats, which is why the
   density ladder could not previously buy any vertical room — `width: auto`
   keeps the aspect while `max-height` makes the cap real. */
```

490 lines below it, `img.split-chart` — the report card's chart — was:

```css
img.split-chart { width: 68.0%; margin-top: 0.7em; }
```

Width only. The exact anti-pattern, in the same file, under a comment
explaining why it doesn't work. The consequence: **`chart_h_pt` was never read
by the report-card stylesheet at all**, so the density ladder was decorative
for the card — stepping from "roomy" to "dense" changed type size while the
chart kept whatever height its aspect ratio wanted.

Measured over the live DB, **3 of 15 stored cards rendered 2 pages**, with the
HR chart landing alone on page 2 under nine inches of white. Nobody noticed
because the tests didn't ask. There were two page-count tests for the card and
between them they blessed the bug: one rendered a 2-split card *with no chart*
and asserted `pages == 1`; the other asserted `pages > 1`. Nothing asserted
that an ordinary card fits.

The generalisable version: when you fix a rendering bug, grep for every other
element of the same kind. A lesson written into a comment protects the line it
sits on and nothing else.

## The card needed its own ladder

Two things had to change that the brief must not inherit:

- `dense`'s chart cap, 82pt → 68pt. Bisected on activity 23825963527 with its
  real HR trace: 80pt still laid out to 2 pages, 78pt was the first value to
  fit. 68 leaves 10pt of margin rather than sitting on the cliff.
- a 4th `ultra` rung, because a card's overflow has **two** drivers and the
  chart cap only fixes one. The other is split-row count. A 14-split half
  marathon (activity 22890867603) was 2 pages at every chart height tested down
  to 46pt — it was 2 pages on dev too, a spill that predates the chart bug and
  was never diagnosed.

Both live in a separate `CARD_DENSITY_PRESETS`. Sharing the tuple was the first
attempt and it was wrong: the brief reads `page_count > 1` to decide whether to
**drop a takeaway**, so a roomier cap or an extra rung there silently changes
what gets printed on the brief. The test suite caught it — a 4-takeaway brief
stopped reporting overflow — which is the second lesson: an overflow signal
that drives content decisions is not a layout detail you can tune globally.

Result: 15/15 stored cards fit one page, including the half marathon.

## The A+ was earned; the display was hiding why

The card's grade distribution looks like a participation trophy. Across the 15
stored cards, **A+ is 29 of the 32 A-band grades — 91%**. The cause is not a
loose modifier. It's direction gating: **9 of 15 pace deviations are exactly
`0.0000`**, which mechanically lands in the bottom third of the A band.

That's correct grading. An easy day is only penalized for running too *fast*,
so running slower is compliance. But the card printed:

```
| Pace | 9:44/mi | 9:39/mi | 5s/mi slower | A+ |
```

A stated target, a stated 5s/mi miss, and an A+. Every number correct, and the
row reads as a broken grade. `9:39` is not a point target on an easy day — it's
a floor — and the Expected column never said so.

Now it reads `≥ 9:39/mi`, reusing the `≤`/`≥` idiom `_fmt_hr_band` already
established for HR. Quality pace gets `≤`, rolling-reference distance gets `≥`.
Two-sided expectations (a plan distance, a steady-day pace) keep a bare number,
because they really are point targets.

The safeguard against drift: `pace_bound_kind` is a claim *about*
`pace_deviation`, so its test derives the truth from `pace_deviation` itself —
feed in a run 10% slower and 10% faster and check which side scores zero —
rather than restating the branches in the assertion. If the gating ever changes
and the display doesn't, the test fails.

## One Delta grammar

Four rows, four dialects: `on target` / `5s/mi slower` / `53% over` / `even`.
The percentages were the worst part — a percentage of a distance, a percentage
of a ratio, and a percentage of a percentage. Three different quantities
wearing one symbol, which is the "never compare two quantities" contract
failing one level down in the units.

The rule now: **a delta is stated in the unit its own row is measured in.**
Distance `0.35 mi long`, HR `8 bpm over`, continuity `0.15x over`. Pace was
already right. One test asserts no delta contains a `%` at all.

Worth noting one test whose *name* had been true for a while and whose
assertion hadn't: `test_an_average_breach_keeps_the_row_in_bpm` asserted
`== "+6%"`.

## Dead columns

13 of 15 cards had an entirely empty `Elev` column; 362 of 428
`activity_splits` rows (85%) have no elevation, because a treadmill run never
does. That's ~15% of the split table's width spent on em-dashes, on a page that
was already overflowing.

Columns now render only if some row has data. `Avg HR` and `vs run` drop
together — dropping one would leave a column of dashes headed by a comparison
against a number that isn't shown. `vs run` is kept whenever it *has* data; it
is what makes a hot mile visible at a glance, and "the reader can subtract"
isn't a reason to make them.

Both renderers now take headers and cells from one `report_card.split_table`.
They had this table written out twice, and a column dropped in one and kept in
the other is exactly the divergence `render_report_card_pdf`'s
already-built-card contract exists to prevent — the same reason `stimulus_rows`
is shared.

## Not done here

- The redundant HR note (the cell and the bullet state the same 58% twice) and
  the HR-cap grading itself were owned by parallel work.
- The band legend, the overall-grade headline, and routing HR drift into the HR
  section of the coach prompt are deferred — the legend in particular needs the
  page space this change just recovered, and that should be re-measured first.
