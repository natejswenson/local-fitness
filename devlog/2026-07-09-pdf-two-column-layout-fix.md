# Fixing the brief PDF's 2-column layout that never actually rendered

**2026-07-09**

Nate flagged the daily brief PDF looked worse since v0.20.0 — specifically
that the 2-column signal-card grid, shipped in the earlier redesign, wasn't
there anymore. Diffing `visuals.py` between v0.20.0 and v0.21.0 showed zero
changes to the file — so this wasn't a regression between those two tags.
It was a bug that shipped with the original 2-column design and had never
actually rendered correctly, on any version, until now.

## Root cause

The signal-card grid used `display: flex; flex-wrap: wrap` with each card
at `flex: 1 1 calc(50% - 0.5em)`. That reads correctly, and would work in
a browser. In WeasyPrint 69.0 (this project's PDF renderer), it doesn't:
every card renders on its own row regardless of `flex-basis` (percentage
or `calc()`), content length, or which other CSS rules are present in the
sheet. A follow-up attempt with `display: grid; grid-template-columns: 1fr
1fr` also failed once real (wrapping, variable-length) content and the
odd-card-count `colspan`-equivalent were involved — items landed in extra
implicit narrow columns instead of wrapping to a new row.

The fix: an HTML `<table>`, with cards paired two-up into `<tr>` rows and
the odd trailing card given `colspan="2"`. Table layout is one of the
oldest, most reliably-supported parts of any CSS engine (this file already
leaned on it for `div.details table`/`table.week-table`), and it renders
correctly — verified directly against real, wrapping takeaway content, not
placeholder text.

## Why this shipped broken and stayed broken

Every existing test for this layout (`test_build_html_span_full_count_
matches_parity`, etc.) asserted on the HTML **string** — checking for the
presence of a `span-full` CSS class. That's a legitimate thing to test, but
it can't catch a layout engine silently ignoring the CSS that class points
to. The PDF was never actually opened and measured.

Diagnosing this took a long back-and-forth of *visually* reading rendered
PDF thumbnails and repeatedly misjudging "is this two columns or one" —
several dead-end conclusions along the way turned out to be misreads of
the image, not real signal. What actually resolved it was switching to
`pdfplumber` (already a project dependency) to extract objective word
bounding boxes (`x0`, `top`) and compare them programmatically instead of
eyeballing a render.

## What's different now

- `visuals.py`: `div.signals` (flex/grid) → `table.signals`, cards paired
  2-up per `<tr>`, odd trailing card gets `colspan="2"` instead of a
  `.span-full` CSS class.
- Two new tests in `test_visuals.py` render a real PDF via the actual
  `render_brief_pdf` path and assert genuine rendered geometry: paired
  cards share a row (`top`) and sit at two distinct `x0` columns, and the
  odd trailing card's row extends past the second column's start x
  position. Confirmed these fail against the pre-fix code and pass
  against the fix, cleanly isolating this as a real regression test rather
  than a restatement of the implementation.
- The two old string-based `span-full` tests were updated to check for the
  new `colspan="2"` mechanism instead — the semantic they were protecting
  (odd count → one full-width trailing card) is unchanged.

## Verified

Full suite: 891 passed, 5 skipped, 93.38% coverage (gate 85%). `ruff`
clean. Rendered the actual production `render_brief_pdf` path with 4 and 5
realistic takeaways (matching real brief content, including the exact
cards from the day this was reported) and confirmed correct 2-column
placement and correct odd-card full-width spanning via `pdfplumber`
bounding boxes, not a visual read.
