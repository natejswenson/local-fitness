# 2026-07-22 — One-page PDFs, and a mileage number that was counting walks

## Why

Nate asked for the generated report to be better on accuracy, consistency,
graphs and layout, and to **always fit on one page**. Measuring first turned
"the layout feels loose" into a list of specific defects.

## What was actually wrong (measured, not eyeballed)

- The 2026-07-22 brief was **2 pages** — while ~150pt of page 1 sat empty.
  An HTML table row can't fragment gracefully, so a signal card that didn't fit
  was pushed *whole* to page 2, and page 2's right rail was completely blank
  (max word `x0`=303.5 against a rail starting at x=333) with its divider rule
  still drawn.
- The report card for activity 23685126977 was **2 pages** too — the overflow
  CLAUDE.md had written off as accepted.
- Charts were the binding constraint. Page counts for the same brief:
  1 page at 3 takeaways with charts removed; 1/2/2/3/3 pages for 1–5
  takeaways with them. The schema allows 5, so overflow was structural.
- Two collisions, via pdfplumber word boxes: `interval` and `5.0` came back as
  the **single word** `interval5.0`, and the stat strip's "This Week" value ran
  ~97pt inside a ~107pt tile whose neighbour started 10pt later, rendering as
  `29.3 mi / 29.5 mi 0`.
- `_fetch_metric_series` anchored to `date.today()` with **no upper bound**, so
  re-rendering an old brief charted data the brief never saw.
- The plan rollup counted walking as run mileage: 07-21 showed
  `Planned 5.0 mi / Actual 9.2 mi` for an interval day whose run was 5.95 mi
  and whose other 3.23 mi was a 29:15/mi walking-pad session.

## What changed

**Measure, don't tune.** `visuals.fit_one_page(build_html, presets)` lays the
document out, counts `len(document.pages)`, and steps down a three-rung density
ladder until it fits. It takes a callable, so the brief and the report card
share one implementation. `chart_h_pt` is the knob that matters, which is why
`img.chart` is now capped by height with `width: auto` — a `max-width`-only cap
lets the figure's aspect decide the page budget and makes the ladder a no-op.

**Shrink first, then truncate.** Nate's call. When the densest rung still
overflows, `generate_brief_report` drops the lowest-priority takeaways and
prints `N further signals omitted for space`. No silent spill, no silent hide.

**Fill the dead space.** `cards_in_left_rail` balances the two rails, counting
the plan block as ~2 cards (measured ~347pt vs ~212pt for a card). Overflow
cards continue below the Training Plan instead of leaving that region empty.

## Three bugs found while verifying, not while planning

1. **The pace gate shipped as a no-op.** `plans.load_activities_by_date` never
   selected `avg_pace_sec_per_km`, so `_ran` fell back to the label every time
   — the exact bug it was written to fix. Only visible because the regenerated
   PDF still read `29.3 mi`. There is now a test that pins the column.
2. **`table.page-layout > tr > td` never matched anything.** HTML parsing
   inserts an implicit `<tbody>`, so the child combinator silently failed and
   the cells kept `vertical-align: middle` — which is why the Training Plan
   rail floated in the vertical centre of the page under a large void. It had
   been that way since the 2-column layout landed.
3. **Fixing (2) broke the gutter.** `table.page-layout td { padding: 0 }` is
   *more* specific than `td.col-plan { padding-left }`, so once the selector
   started matching it stripped the right rail's padding and printed the plan
   text against the divider. Horizontal padding now belongs to the column
   classes alone.

Each was caught by rendering the real PDF and looking at it. None would have
been caught by the test suite as it stood.

## Consequences worth knowing

- **Adherence moved 94% → 88%** on 2026-07-22. That is the walk gating working:
  the old number counted walking-pad miles toward prescribed runs. The strip now
  reads `20.0 mi / 29.5 mi · +9.3 walked`, and run + walk reconcile by
  construction.
- Duration mattered more than distance here. The 07-21 walk ran 1:34:30 — on its
  own long enough to satisfy *any* interval or tempo target it was compared to.
- Easy days still count walking on purpose. The plan now carries two walk days a
  week, and they have to stay gradeable.
- A saved brief's prose is frozen at generation time, so an OLD brief re-rendered
  today can quote an adherence number the live plan rail now computes
  differently. New briefs agree; historical ones are a snapshot.

## Verification

`uv run pytest` (1418 passed, 94.63% coverage), `ruff`, perf benchmarks re-run
locally with no regression on `_build_plan_section` (688µs median) — **not**
rebaselined. Both PDFs regenerated for real and rasterised with `pdftoppm`:
1 page each, no collisions, both rails filled.
