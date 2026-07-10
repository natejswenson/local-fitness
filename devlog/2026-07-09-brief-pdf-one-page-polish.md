# Squeezing the brief PDF onto one page

**2026-07-09**

Right after fixing the 2-column signal-card grid (see the prior entry the
same day), Nate's next ask was simpler to state and harder to do: the brief
PDF needed to fit on one page, and it needed to look "more condensed and
more polished and fun." A worst-case brief — 5 takeaways (the documented
max, `prompts.py`) plus a full Training Plan section — was running well
past one A4 page.

## The real fix wasn't smaller type

The instinct is to shrink font sizes until it fits. That works, up to a
point, and then the page just looks cramped and the "fun" part never
happens. The actual lever was structural: the signal cards and the
Training Plan section were stacking **sequentially** — read all the
takeaways, then read the whole plan section below them. Running them in
**parallel**, side by side as two real page columns, buys back roughly
half the vertical space without touching a single font size.

That meant extending the table-based layout fix from earlier the same day
to the whole page, not just the card grid. `table.page-layout` splits the
page into a 56%-width signals rail and a 44%-width plan rail (a colspan-1
row, not the flex/grid CSS that had already failed once that day — see
the prior entry for why table layout is the one primitive WeasyPrint 69.0
actually holds for this). When there's no active plan, the signals rail
just takes the full page width — no dangling empty second column.

## Polished and fun, concretely

"Fun" isn't a CSS property, but it decomposes into a few concrete moves:

- Signal cards got a tone-tinted background wash (a translucent version of
  the card's accent color) instead of a plain white card with a colored
  border — the page reads as colorful tiles, not a ruled list.
- The header gained a colored rule under it and a pill-shaped date badge,
  instead of a bare `<h1>` — the kind of small detail that makes a report
  look designed rather than dumped.
- The Training Plan's stat strip (adherence, days to race, this week's
  mileage, slip count) moved from a 1x4 row to a 2x2 tile grid, because
  the right rail is now only ~44% of page width — 4 tiles across would
  have wrapped "11.0 mi / 16.0 mi" badly.

## Verifying "fits on one page" honestly

This project's prior PDF layout bug (same day, see the earlier entry)
shipped because nothing actually opened the rendered PDF and measured it — every
test asserted on HTML strings, and WeasyPrint quietly ignored the CSS
those strings pointed at. Same discipline here: page count is checked with
`pdfplumber` (`len(pdf.pages) == 1`) against the real, realistic 5-takeaway
worst case, not eyeballed. Font size and card spacing went through three
tuning passes (9.3pt/1.32 line-height → 10.8pt/1.4 → 11.3pt/1.42), each
re-verified against the same page-count check before moving to the next.

Column placement itself is verified the same way as the earlier fix:
`pdfplumber.extract_words()` bounding boxes, comparing `x0`/`top` of known
headline tokens, not a visual read of a rendered thumbnail.

## A test-writing gotcha along the way

One existing test asserted an exact phrase from the Training Plan's
fallback coaching line using `pdfplumber`'s plain `extract_text()`. Two
separate things broke it under the new layout:

1. `extract_text()` reads left-to-right across the **full page width** per
   visual row — it has no concept of columns, so with two real columns
   side by side it interleaves words from both. Fix: `page.crop(...)` to
   the right half before extracting, isolating the plan column.
2. Even cropped, the assertion still failed — the narrower right rail
   caused the target phrase to word-wrap mid-sentence (`"9:23/"` on one
   line, `"mi."` on the next), and `extract_text()` renders that as a
   literal newline with no space. A naive `"\n"` → `" "` replace would
   have inserted a spurious space into `"9:23/mi."`. The fix that's
   actually correct: strip **all** whitespace from both the extracted
   text and the expected phrase before comparing, so neither a real space
   nor a wrap-induced newline can cause a false negative (or mask a real
   mismatch).

## Verified

Full suite: 890 passed, 5 skipped, 93.37% coverage (gate 85%). `ruff`
clean. Rendered the actual `render_brief_pdf` path with a realistic
5-takeaway brief plus a full plan section and confirmed single-page output
via `pdfplumber` page count, with correct 2-column rail placement via word
bounding boxes.
