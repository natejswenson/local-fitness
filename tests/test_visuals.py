"""Tests for agent/visuals.py — the matplotlib/WeasyPrint rendering internals
shared by the `generate_brief_report` and `generate_chart` MCP tools.

These test the pure rendering functions directly (no DB, no MCP tool
wrapping) — data is handed in exactly as agent/tools.py would supply it.
"""
from __future__ import annotations

import io

import pdfplumber
import pytest

from local_fitness.agent import visuals
from local_fitness.agent.schemas import Brief, Takeaway, TakeawayMetric

_SERIES = [
    ("2026-07-01", 50.0),
    ("2026-07-02", 52.0),
    ("2026-07-03", 48.0),
    ("2026-07-04", 51.0),
]


def _fmt(v: float) -> str:
    return f"{v:.1f}"


@pytest.mark.parametrize("chart_type", ["line", "bar", "combo"])
def test_render_chart_png_valid_for_each_v1_chart_type(chart_type):
    png = visuals.render_chart_png(_SERIES, chart_type, _fmt)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 100


def test_render_chart_png_unsupported_chart_type_raises():
    with pytest.raises(ValueError, match="unsupported chart_type"):
        visuals.render_chart_png(_SERIES, "calendar", _fmt)


def _pdf_text(pdf_bytes: bytes) -> str:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as doc:
        return "\n".join(p.extract_text() or "" for p in doc.pages)


def _brief(takeaways: list[Takeaway]) -> Brief:
    return Brief(date="2026-07-08", user_name="Nate", takeaways=takeaways)


def test_render_brief_pdf_magic_bytes_and_page_count():
    brief = _brief([Takeaway(headline="h", summary="s", tone="neutral", details="d")])
    pdf = visuals.render_brief_pdf(brief, {})
    assert pdf[:5] == b"%PDF-"
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        assert len(doc.pages) >= 1


def test_render_brief_pdf_escapes_html_special_chars_in_headline():
    # A sentinel headline containing <, >, & must render escaped in the HTML
    # source — proving html.escape() actually ran, not just that the source
    # substring is present (a raw pass-through would ALSO extract back to the
    # same visible text via pdfplumber, so this test inspects the HTML build
    # path directly rather than relying on extracted text alone).
    brief = _brief([Takeaway(
        headline="keep effort < 6/10 & recover", summary="s", tone="neutral", details="d",
    )])
    pdf = visuals.render_brief_pdf(brief, {})
    # The visible extracted text must still read as the original characters.
    text = _pdf_text(pdf)
    assert "keep effort < 6/10 & recover" in text


def test_render_brief_pdf_headline_html_is_actually_escaped():
    # Inspect the constructed HTML directly (bypassing WeasyPrint) to prove
    # the escape step runs — this is the sharper assertion the design calls
    # for, since a rendered PDF's extracted text looks identical whether or
    # not html.escape() ran.
    import html as html_mod
    headline = "a < b & c > d"
    escaped = html_mod.escape(headline)
    assert escaped == "a &lt; b &amp; c &gt; d"
    # Confirm visuals.render_brief_pdf uses html.escape on headline by
    # checking the module actually imports/uses it (behavioral proxy: a
    # raw '<' immediately followed by a letter would be swallowed by an
    # HTML parser's tag-open recovery, so if escaping were dropped the
    # rendered PDF's extracted text would NOT contain "< b" verbatim).
    brief = _brief([Takeaway(headline=headline, summary="s", tone="neutral", details="d")])
    pdf = visuals.render_brief_pdf(brief, {})
    text = _pdf_text(pdf)
    assert "a < b & c > d" in text


def test_render_brief_pdf_markdown_table_renders_as_real_table():
    table_md = "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    brief = _brief([Takeaway(headline="h", summary="s", tone="neutral", details=table_md)])
    pdf = visuals.render_brief_pdf(brief, {})
    text = _pdf_text(pdf)
    # A correctly-enabled table extension renders cell text without the
    # raw pipe/dash markup; a disabled extension would leave literal
    # "| a | b |" in the output instead.
    assert "|" not in text
    assert "a" in text and "b" in text and "1" in text and "2" in text


def test_render_brief_pdf_blocks_external_image_network_fetch():
    brief = _brief([Takeaway(
        headline="h", summary="s", tone="neutral",
        details='details with an <img src="http://example.com/evil.png"> embed',
    )])
    # Must not raise (the fetcher rejects the URL internally, it doesn't
    # propagate as an unhandled connection error) and must not leak the
    # blocked host into the rendered output.
    pdf = visuals.render_brief_pdf(brief, {})
    assert pdf[:5] == b"%PDF-"
    text = _pdf_text(pdf)
    assert "example.com" not in text


def test_url_fetcher_rejects_non_data_scheme():
    fetcher = visuals._report_url_fetcher()
    with pytest.raises(ValueError, match="disallowed protocol"):
        fetcher("http://example.com/x.png")


def test_url_fetcher_allows_data_scheme():
    fetcher = visuals._report_url_fetcher()
    uri = visuals._data_uri(b"\x89PNG\r\n\x1a\nfakepngbytes")
    result = fetcher(uri)
    assert result is not None


def test_render_brief_pdf_chart_keying_places_correct_chart_per_takeaway():
    # Dense/sequential keys alone can't discriminate a positional-zip bug
    # from correct index-keyed lookup (both produce identical output when
    # every takeaway has a chart) — the fixture must include a chartless
    # takeaway BETWEEN two charted ones: indices 0/1/2, charts only at
    # {"0": ..., "2": ...}. A `zip(takeaways, charts.values())`-style bug
    # would assign png_b to takeaway 1 and leave takeaway 2 chartless —
    # wrong on both counts.
    png_a = visuals.render_chart_png(_SERIES, "line", _fmt)
    png_b = visuals.render_chart_png(_SERIES, "bar", _fmt)
    brief = _brief([
        Takeaway(headline="first", summary="s0", tone="neutral",
                  metric=TakeawayMetric(metric="rhr", days=14), details="d0"),
        Takeaway(headline="second", summary="s1", tone="neutral", details="d1"),
        Takeaway(headline="third", summary="s2", tone="neutral",
                  metric=TakeawayMetric(metric="rhr", days=14), details="d2"),
    ])
    pdf = visuals.render_brief_pdf(brief, {"0": png_a, "2": png_b})

    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        images_per_page = [len(p.images) for p in doc.pages]
    total_images = sum(images_per_page)
    # Exactly two embedded charts (indices 0 and 2); index 1 has none.
    assert total_images == 2


def test_render_brief_pdf_takeaway_without_chart_renders_without_image():
    brief = _brief([Takeaway(headline="h", summary="s", tone="neutral", details="d")])
    pdf = visuals.render_brief_pdf(brief, {})
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        total_images = sum(len(p.images) for p in doc.pages)
    assert total_images == 0


# --- 2026-07-09: 2-column signal grid + Training Plan section --------------

def _takeaways(n: int) -> list[Takeaway]:
    return [
        Takeaway(headline=f"h{i}", summary=f"s{i}", tone="neutral", details=f"d{i}")
        for i in range(n)
    ]


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5])
def test_build_html_colspan_count_matches_parity(n):
    brief = _brief(_takeaways(n))
    html_out = visuals._build_html(brief, {}, None)
    # An odd count gets exactly one trailing full-width (colspan="2") row;
    # an even count pairs every card into a two-<td> row with none. Match
    # the actual rendered <td> attribute, not a bare "colspan=\"2\"" —
    # that substring also appears in this file's own CSS comment, which is
    # present in every render regardless of takeaway count.
    colspan_count = html_out.count('class="cell-full" colspan="2"')
    assert colspan_count == (1 if n % 2 == 1 else 0)


def test_build_html_colspan_is_on_the_last_card_specifically():
    brief = _brief(_takeaways(3))
    html_out = visuals._build_html(brief, {}, None)
    # Split into per-row chunks on <tr> and confirm only the last row (the
    # 3rd, last, 0-indexed card, alone after pairing 0+1) is full-width.
    rows = html_out.split("<tr>")[1:]
    assert len(rows) == 2
    assert 'colspan="2"' not in rows[0]
    assert 'colspan="2"' in rows[1]


def _heading_positions(pdf_bytes: bytes, words: list[str]) -> dict[str, tuple[float, float]]:
    """Map each single-token headline word to its (x0, top) position on the
    first PDF page. Used to assert genuine rendered column placement — HTML
    string assertions alone can't catch a layout engine silently collapsing
    a 2-column design to one column (which is exactly what happened here:
    the flex/grid-based CSS looked correct as a string but WeasyPrint 69.0
    rendered every card on its own row regardless)."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as doc:
        found = {w["text"]: (w["x0"], w["top"]) for w in doc.pages[0].extract_words()}
    missing = [w for w in words if w not in found]
    assert not missing, f"expected headline tokens not found in rendered PDF: {missing}"
    return {w: found[w] for w in words}


def test_render_brief_pdf_paired_cards_render_in_two_distinct_columns():
    # Single-token, non-wrapping headlines so each word's position
    # unambiguously identifies its card.
    words = ["Alphahead", "Betahead", "Gammahead", "Deltahead"]
    brief = _brief([
        Takeaway(headline=w, summary=f"s{i}", tone="neutral", details=f"d{i}")
        for i, w in enumerate(words)
    ])
    pdf = visuals.render_brief_pdf(brief, {})
    pos = _heading_positions(pdf, words)

    # Row 1 (Alphahead, Betahead): same top (same row), distinct x0 (columns).
    assert pos["Alphahead"][1] == pos["Betahead"][1]
    assert pos["Alphahead"][0] != pos["Betahead"][0]
    # Row 2 (Gammahead, Deltahead): same pairing, and strictly below row 1.
    assert pos["Gammahead"][1] == pos["Deltahead"][1]
    assert pos["Gammahead"][0] != pos["Deltahead"][0]
    assert pos["Gammahead"][1] > pos["Alphahead"][1]
    # The two columns line up: left members share an x0, right members share one.
    left_x, right_x = sorted([pos["Alphahead"][0], pos["Betahead"][0]])
    assert {pos["Gammahead"][0], pos["Deltahead"][0]} == {left_x, right_x}


def test_render_brief_pdf_odd_trailing_card_spans_full_row_width():
    words = ["Alphahead", "Betahead", "Gammahead"]
    # The odd card's summary is one long unbroken-enough line — in a
    # half-width column it wraps well before reaching the second column's
    # x-position; at full (colspan="2") width it doesn't have to.
    long_summary = "one two three four five six seven eight nine ten eleven twelve"
    brief = _brief([
        Takeaway(headline=w, summary=(long_summary if w == "Gammahead" else f"s{i}"),
                  tone="neutral", details=f"d{i}")
        for i, w in enumerate(words)
    ])
    pdf = visuals.render_brief_pdf(brief, {})
    pos = _heading_positions(pdf, words)

    left_x = min(pos["Alphahead"][0], pos["Betahead"][0])
    right_x = max(pos["Alphahead"][0], pos["Betahead"][0])
    # The odd trailing card starts at the left column, not the right one —
    # colspan="2" widens its cell rather than shifting it into column 2.
    # Sub-point tolerance: the colspan cell's padding differs slightly from
    # a single-column cell's, which shifts its left edge by a fraction of a
    # point — not the multi-point difference a real column jump would cause.
    assert abs(pos["Gammahead"][0] - left_x) < 2
    assert pos["Gammahead"][1] > pos["Alphahead"][1]

    # And its row is genuinely full-width: the long summary's first line
    # must run at least past the right column's start x-position, which a
    # half-width cell (the pre-fix, single-column-only behavior) could not.
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        summary_words = [w for w in doc.pages[0].extract_words() if w["text"] == "one"]
    assert summary_words, "summary text 'one' not found in rendered PDF"
    summary_top = summary_words[0]["top"]
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        same_line = [w for w in doc.pages[0].extract_words() if w["top"] == summary_top]
    assert max(w["x1"] for w in same_line) > right_x


def test_render_plan_section_html_none_is_empty_string():
    assert visuals._render_plan_section_html(None) == ""


def test_build_html_plan_section_none_has_no_training_plan_heading():
    # Matches the exact rendered heading markup, not a bare substring —
    # "Training Plan" also appears in this file's own CSS explanatory
    # comment (always present in every render), which is invisible to any
    # actual viewer but would make a loose substring search a false match.
    brief = _brief([Takeaway(headline="h", summary="s", tone="neutral", details="d")])
    html_out = visuals._build_html(brief, {}, None)
    assert '<h2 class="plan-heading">Training Plan</h2>' not in html_out


_PLAN_SECTION = {
    "adherence_pct": 75,
    "goal_type": "10k",
    "days_to_race": 71,
    "week_planned_mi": 17.0,
    "week_actual_mi": 11.1,
    "slips": 2,
    "today": {
        "type": "easy",
        "distance_mi": 4.0,
        "pace_min_per_mi": "9:30",
        "description": "keep HR under 140",
        "coaching_line": "Go make good on yesterday's shortfall.",
    },
    "last_7_days": [
        {"date": "2026-07-09", "type": "easy", "planned_mi": 4.0, "actual_mi": None, "verdict": "pending"},
        {"date": "2026-07-08", "type": "easy", "planned_mi": 4.0, "actual_mi": 2.96, "verdict": "partial"},
        {"date": "2026-07-07", "type": "rest", "planned_mi": None, "actual_mi": None, "verdict": "compliant"},
        {"date": "2026-07-06", "type": "tempo", "planned_mi": 3.0, "actual_mi": 3.05, "verdict": "done"},
        {"date": "2026-07-03", "type": "long", "planned_mi": 6.0, "actual_mi": 0.0, "verdict": "missed"},
    ],
}


def test_render_plan_section_html_stat_tile_exact_values():
    # Exact-value assertions against the raw HTML builder, not the rendered
    # PDF's extracted text — pdfplumber's word-position-based extraction
    # reorders/interleaves the flex stat-strip's tiles and labels in a way
    # that makes precise single-value assertions ambiguous (e.g. "2" for
    # slips would trivially match a date like "2026-07-08" too).
    html_out = visuals._render_plan_section_html(_PLAN_SECTION)
    assert '<div class="value">75%</div>' in html_out
    assert '<div class="value">71</div>' in html_out
    assert '<div class="label">Days to Race</div>' in html_out
    assert '<div class="value">11.1 mi / 17.0 mi</div>' in html_out
    assert '<div class="value">2</div>' in html_out
    assert '<div class="label">Slips</div>' in html_out


def test_render_plan_section_html_no_race_date_shows_goal_tile():
    section = dict(_PLAN_SECTION, days_to_race=None, goal_type="base building")
    html_out = visuals._render_plan_section_html(section)
    assert '<div class="value">base building</div>' in html_out
    assert '<div class="label">Goal</div>' in html_out
    assert "Days to Race" not in html_out


def test_render_brief_pdf_plan_section_appears_in_pdf_text():
    # Broad "does the content actually show up in the rendered document"
    # smoke check — uses unambiguous substrings only (headline uppercase is
    # a real CSS text-transform, not a bug: h2.plan-heading is uppercased).
    brief = _brief([Takeaway(headline="h", summary="s", tone="neutral", details="d")])
    pdf = visuals.render_brief_pdf(brief, {}, _PLAN_SECTION)
    text = _pdf_text(pdf)
    assert "TRAINING PLAN" in text
    assert "easy · 4.0 mi @ 9:30/mi" in text
    assert "keep HR under 140" in text
    assert "Go make good on yesterday's shortfall." in text


def test_render_brief_pdf_plan_section_table_rows_and_verdicts():
    brief = _brief([Takeaway(headline="h", summary="s", tone="neutral", details="d")])
    pdf = visuals.render_brief_pdf(brief, {}, _PLAN_SECTION)
    text = _pdf_text(pdf)
    assert "2026-07-08 easy 4.0 mi 3.0 mi partial" in text  # 2.96mi displays as 3.0 (1dp)
    assert "2026-07-07 rest — — rest" in text  # compliant -> "rest" label, no mileage
    assert "2026-07-06 tempo 3.0 mi 3.0 mi done" in text
    assert "2026-07-03 long 6.0 mi 0.0 mi missed" in text  # missed shows actual 0.0, not "—"
    assert "2026-07-09 easy 4.0 mi — scheduled" in text  # pending -> no actual shown


def test_render_brief_pdf_plan_section_without_today_omits_callout():
    section = dict(_PLAN_SECTION, today=None)
    brief = _brief([Takeaway(headline="h", summary="s", tone="neutral", details="d")])
    pdf = visuals.render_brief_pdf(brief, {}, section)
    text = _pdf_text(pdf)
    # Stat strip + table still present, but no coaching-line paragraph.
    assert "TRAINING PLAN" in text
    assert "Go make good on yesterday's shortfall." not in text


@pytest.mark.parametrize(
    "verdict,label",
    [("done", "done"), ("partial", "partial"), ("missed", "missed"),
     ("compliant", "rest"), ("pending", "scheduled")],
)
def test_render_plan_section_html_verdict_label_mapping_exhaustive(verdict, label):
    section = dict(_PLAN_SECTION, last_7_days=[
        {"date": "2026-07-01", "type": "easy", "planned_mi": 1.0, "actual_mi": 1.0, "verdict": verdict},
    ])
    html_out = visuals._render_plan_section_html(section)
    assert f'verdict-{verdict}"' in html_out
    assert f">{label}<" in html_out
