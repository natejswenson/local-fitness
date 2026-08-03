"""Tests for agent/visuals.py — the matplotlib/WeasyPrint rendering internals
shared by the `generate_brief_report` and `generate_chart` MCP tools.

These test the pure rendering functions directly (no DB, no MCP tool
wrapping) — data is handed in exactly as agent/tools.py would supply it.
"""
from __future__ import annotations

import html
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


def _render(*args, **kwargs) -> bytes:
    """`render_brief_pdf` returns (pdf_bytes, page_count) since the one-page
    guarantee landed. Most tests here assert on content, not on the count, so
    they take the bytes; the page-count contract has its own tests below."""
    return visuals.render_brief_pdf(*args, **kwargs)[0]


def test_render_brief_pdf_magic_bytes_and_page_count():
    brief = _brief([Takeaway(headline="h", summary="s", tone="neutral", details="d")])
    pdf = _render(brief, {})
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
    pdf = _render(brief, {})
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
    pdf = _render(brief, {})
    text = _pdf_text(pdf)
    assert "a < b & c > d" in text


def test_render_brief_pdf_markdown_table_renders_as_real_table():
    table_md = "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    brief = _brief([Takeaway(headline="h", summary="s", tone="neutral", details=table_md)])
    pdf = _render(brief, {})
    text = _pdf_text(pdf)
    # A correctly-enabled table extension renders cell text without the
    # raw pipe/dash markup; a disabled extension would leave literal
    # "| a | b |" in the output instead.
    assert "|" not in text
    # Headers render uppercased (PRESS table-header treatment) — assert
    # case-insensitively; the cells stay verbatim.
    assert "A" in text and "B" in text and "1" in text and "2" in text


def test_render_brief_pdf_blocks_external_image_network_fetch():
    brief = _brief([Takeaway(
        headline="h", summary="s", tone="neutral",
        details='details with an <img src="http://example.com/evil.png"> embed',
    )])
    # Must not raise (the fetcher rejects the URL internally, it doesn't
    # propagate as an unhandled connection error) and must not leak the
    # blocked host into the rendered output.
    pdf = _render(brief, {})
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
    pdf = _render(brief, {"0": png_a, "2": png_b})

    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        images_per_page = [len(p.images) for p in doc.pages]
    total_images = sum(images_per_page)
    # Exactly two embedded charts (indices 0 and 2); index 1 has none.
    assert total_images == 2


def test_render_brief_pdf_takeaway_without_chart_renders_without_image():
    brief = _brief([Takeaway(headline="h", summary="s", tone="neutral", details="d")])
    pdf = _render(brief, {})
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
def test_build_html_cards_stack_single_column_not_paired(n):
    # 2026-07-09 layout: cards no longer pair 2-up into table rows — the
    # 2-column split moved to the whole-page level (signals rail vs. plan
    # rail), so every card renders as its own top-level <section>, and
    # there is no per-pair <tr>/colspan wrapping at all regardless of N.
    brief = _brief(_takeaways(n))
    html_out = visuals._build_html(brief, {}, None)
    assert html_out.count('class="signal-card') == n
    # Exactly ONE <tr> — the masthead's identity row. No per-pair card
    # wrapping rows exist (the original regression this test pins).
    assert html_out.count("<tr>") == 1
    assert html_out.index("<tr>") < html_out.index('class="signal-card')
    assert "colspan" not in html_out


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


def test_render_brief_pdf_cards_stack_in_a_single_column():
    # No plan section → signal cards run the full page width, one per row.
    words = ["Alphahead", "Betahead", "Gammahead"]
    brief = _brief([
        Takeaway(headline=w, summary=f"s{i}", tone="neutral", details=f"d{i}")
        for i, w in enumerate(words)
    ])
    pdf = _render(brief, {})
    pos = _heading_positions(pdf, words)

    x0s = {pos[w][0] for w in words}
    assert len(x0s) == 1, f"expected one shared column x0, got {x0s}"
    tops = [pos[w][1] for w in words]
    assert tops == sorted(tops) and len(set(tops)) == len(tops)


def test_render_brief_pdf_signals_and_plan_render_as_two_page_columns():
    # With a plan section present, the WHOLE PAGE is 2 columns: the signal
    # cards rail (left) and the Training Plan rail (right) run in parallel
    # rather than stacking sequentially — this is what actually buys back
    # the vertical room to fit a full brief on one page.
    brief = _brief([Takeaway(headline="Alphahead", summary="s", tone="neutral", details="d")])
    pdf = _render(brief, {}, plan_section=_PLAN_SECTION)
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        words = {w["text"]: (w["x0"], w["top"]) for w in doc.pages[0].extract_words()}
    assert "Alphahead" in words
    assert "TRAINING" in words  # "TRAINING PLAN" heading, uppercased via CSS but literal in text
    # The plan rail sits well to the right of the signals rail — not merely
    # a few points over the way sub-pixel padding differences would.
    assert words["TRAINING"][0] - words["Alphahead"][0] > 100


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
    # No sessions number in this section -> the label is bare, exactly as before.
    assert '<div class="label">Adherence</div>' in html_out


def test_render_plan_section_html_sessions_adherence_rides_the_label():
    """The compound value goes on the tiny dim label, never in the 1.25em/900
    numeral slot that collided with its neighbour once already."""
    section = dict(_PLAN_SECTION, sessions_adherence_pct=62, rest_days_counted=2)
    html_out = visuals._render_plan_section_html(section)
    assert '<div class="value">75%</div>' in html_out          # numeral untouched
    assert '<div class="label">Adherence · 62% sessions</div>' in html_out


def test_render_plan_section_html_zero_sessions_adherence_is_still_printed():
    """`is not None`, not truthy — 0% is the number that most needs to sit
    beside a flattering total."""
    section = dict(_PLAN_SECTION, sessions_adherence_pct=0)
    assert ('<div class="label">Adherence · 0% sessions</div>'
            in visuals._render_plan_section_html(section))


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
    pdf = _render(brief, {}, _PLAN_SECTION)
    text = _pdf_text(pdf)
    assert "TRAINING PLAN" in text
    assert "easy · 4.0 mi @ 9:30/mi" in text
    assert "keep HR under 140" in text
    assert "Go make good on yesterday's shortfall." in text


def test_render_brief_pdf_plan_section_table_rows_and_verdicts():
    brief = _brief([Takeaway(headline="h", summary="s", tone="neutral", details="d")])
    pdf = _render(brief, {}, _PLAN_SECTION)
    text = _pdf_text(pdf)
    assert "07-08 easy 4.0 mi 3.0 mi partial" in text  # 2.96mi displays as 3.0 (1dp)
    assert "07-07 rest — — rest" in text  # compliant -> "rest" label, no mileage
    assert "07-06 tempo 3.0 mi 3.0 mi done" in text
    # missed shows actual 0.0 (not "—") and renders CAPS in the accent —
    # the one loud verdict under PRESS-strict.
    assert "07-03 long 6.0 mi 0.0 mi MISSED" in text
    assert "07-09 easy 4.0 mi — scheduled" in text  # pending -> no actual shown


def test_render_brief_pdf_plan_section_without_today_omits_callout():
    section = dict(_PLAN_SECTION, today=None)
    brief = _brief([Takeaway(headline="h", summary="s", tone="neutral", details="d")])
    pdf = _render(brief, {}, section)
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


# --- value_axis_bounds: autoscale, never zero-anchored (2026-07-19) ----------

def test_value_axis_bounds_hugs_the_data_band():
    # The motivating case: RHR 48..57 must NOT produce a 0-based axis.
    ylo, yhi = visuals.value_axis_bounds([52.0, 57.0, 48.0, 50.0])
    span = 57.0 - 48.0
    assert ylo == pytest.approx(48.0 - span * 0.08)
    assert yhi == pytest.approx(57.0 + span * 0.08)
    assert ylo > 40  # nowhere near zero


def test_value_axis_bounds_flat_series_gets_nonzero_height():
    ylo, yhi = visuals.value_axis_bounds([50.0, 50.0, 50.0])
    assert ylo == pytest.approx(50.0 - 2.5)  # 5% of magnitude
    assert yhi == pytest.approx(50.0 + 2.5)


def test_value_axis_bounds_flat_near_zero_uses_min_pad():
    ylo, yhi = visuals.value_axis_bounds([0.0, 0.0])
    assert (ylo, yhi) == (-1.0, 1.0)


def test_value_axis_bounds_negative_band_pads_sign_agnostically():
    # TSB-shaped series: all negative, padding extends both directions.
    ylo, yhi = visuals.value_axis_bounds([-20.0, -5.0, -12.0])
    span = 15.0
    assert ylo == pytest.approx(-20.0 - span * 0.08)
    assert yhi == pytest.approx(-5.0 + span * 0.08)


def test_render_chart_png_line_axis_not_zero_anchored():
    # Integration: render the RHR-band shape and confirm the figure's y-limits
    # came from value_axis_bounds, not matplotlib's fill-to-zero autoscale.
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.figure import Figure
    captured = {}
    orig = Figure.add_subplot

    def spy(self, *a, **k):
        ax = orig(self, *a, **k)
        captured["ax"] = ax
        return ax

    Figure.add_subplot = spy
    try:
        series = [(f"2026-07-{d:02d}", 48.0 + (d % 10)) for d in range(1, 15)]
        png = visuals.render_chart_png(series, "line", _fmt)
    finally:
        Figure.add_subplot = orig
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    ylo, yhi = captured["ax"].get_ylim()
    exp_lo, exp_hi = visuals.value_axis_bounds([48.0 + (d % 10) for d in range(1, 15)])
    assert ylo == pytest.approx(exp_lo)
    assert yhi == pytest.approx(exp_hi)
    assert ylo > 40


# --- PRESS theme (2026-07-19: brand-driven PDF styling) ----------------------

def test_build_css_carries_press_tokens_and_no_legacy_blue(monkeypatch):
    # Pin to the repo-default theme: cli.py's load_dotenv() (imported by other
    # test modules) loads the developer's .env process-wide, which may set
    # LOCAL_FITNESS_BRAND_FILE to a personal brand file.
    monkeypatch.delenv("LOCAL_FITNESS_BRAND_FILE", raising=False)
    from local_fitness.agent import branding
    css = visuals._build_css(branding.load_theme())
    assert "#F5F0E6" in css and "#181510" in css and "#E8501F" in css
    assert "#2a78d6" not in css  # the retired budget-project blue
    # PRESS "never do": no rounded corners anywhere in the report.
    assert "border-radius" not in css


def test_build_css_press_strict_verdict_treatments(monkeypatch):
    monkeypatch.delenv("LOCAL_FITNESS_BRAND_FILE", raising=False)
    from local_fitness.agent import branding
    css = visuals._build_css(branding.load_theme())
    # missed = the one accent, caps; done = ink weight; partial = dim italic.
    missed = css.split("span.verdict-missed")[1].split("}")[0]
    assert "#E8501F" in missed and "uppercase" in missed
    done = css.split("span.verdict-done")[1].split("}")[0]
    assert "#181510" in done
    partial = css.split("span.verdict-partial")[1].split("}")[0]
    assert "#6E675C" in partial and "italic" in partial


def test_build_css_custom_theme_accent_flows_through():
    from local_fitness.agent import branding
    theme = branding._deep_merge(branding.DEFAULT_THEME, {"colors": {"accent": "#0055FF"}})
    css = visuals._build_css(theme)
    assert "#0055FF" in css and "#E8501F" not in css


def test_build_html_masthead_carries_identity(monkeypatch):
    monkeypatch.delenv("LOCAL_FITNESS_BRAND_FILE", raising=False)
    brief = _brief([Takeaway(headline="h", summary="s", tone="neutral", details="d")])
    out = visuals._build_html(brief, {}, None)
    assert '<span class="stamp">NS</span>' in out
    assert "LOCAL FITNESS · MORNING BRIEF · 2026-07-08" in out
    assert "linkedin.com/in/natejswenson" in out
    assert "date-pill" not in out  # old header banner is gone


def test_font_face_emitted_only_for_real_mono_file(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCAL_FITNESS_BRAND_FILE", raising=False)
    from local_fitness.agent import branding
    theme = branding.load_theme()
    face, stack = visuals._font_face_css(theme)
    assert face == "" and "BrandMono" not in stack

    font = tmp_path / "font.ttf"
    font.write_bytes(b"\x00\x01\x00\x00fakefont")
    theme["fonts"]["mono_file"] = str(font)
    face, stack = visuals._font_face_css(theme)
    assert "@font-face" in face and "data:font/ttf;base64," in face
    assert stack.startswith("'BrandMono'")

    theme["fonts"]["mono_file"] = str(tmp_path / "gone.ttf")
    face, stack = visuals._font_face_css(theme)
    assert face == "" and "BrandMono" not in stack


def test_render_chart_png_uses_theme_paper_background(monkeypatch):
    monkeypatch.delenv("LOCAL_FITNESS_BRAND_FILE", raising=False)
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.figure import Figure
    captured = {}
    orig = Figure.add_subplot

    def spy(self, *a, **k):
        ax = orig(self, *a, **k)
        captured["ax"] = ax
        return ax

    Figure.add_subplot = spy
    try:
        png = visuals.render_chart_png(_SERIES, "line", _fmt)
    finally:
        Figure.add_subplot = orig
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    import matplotlib.colors as mcolors
    assert mcolors.to_hex(captured["ax"].get_facecolor()).upper() == "#F5F0E6"


def test_render_brief_pdf_no_word_paints_past_the_printable_area():
    # Regression net for the 2026-07-19 overflow: the mono week table's
    # VERDICT column painted up to 14pt past the right content edge (and
    # the coaching line 4pt) — table-layout: fixed + MM-DD dates + wrap
    # hints keep every word inside @page margins. Measured, not eyeballed.
    long_line = ("You hit yesterday's session clean. Today: easy 3.0 mi @ "
                 "10:28/mi. Recovery 3mi. Keep HR under 140. 61 days to your 10k.")
    plan = dict(_PLAN_SECTION)
    plan["today"] = dict(plan["today"], coaching_line=long_line)
    brief = _brief([Takeaway(headline="h", summary="s", tone="neutral", details="d")])
    pdf = _render(brief, {}, plan)
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        page = doc.pages[0]
        margin_pt = 1.5 * 28.35  # @page margin: 1.5cm
        right_edge = page.width - margin_pt
        offenders = [
            (w["text"], round(w["x1"] - right_edge, 1))
            for w in page.extract_words() if w["x1"] > right_edge + 0.5
        ]
    assert offenders == []
    # And the week table renders MM-DD, not the full ISO date.
    text = _pdf_text(pdf)
    assert "07-08 easy" in text
    assert "2026-07-08 easy" not in text


def test_render_report_card_pdf_no_word_paints_past_the_printable_area():
    """Same regression net as the brief's, for the report card. The coach read
    is model-generated variable-length prose dropped into the hero's meta cell,
    which is exactly the shape that produced the 0.24.1 overflow — a fixed cell
    fed text whose length nobody controls. Measured, not eyeballed."""
    from local_fitness.agent import report_card as rc
    from local_fitness.agent import workout_coach

    long_read = (
        "You turned a 10:28 easy run into an 8:55 closer and every split says "
        "so: 10:10, then 9:24, then 8:55, accelerating the whole way like the "
        "prescription was a suggestion rather than an instruction you agreed "
        "to follow. Distance and training load landed on spec. Heart rate "
        "stayed inside the easy ceiling. Pace missed by a full minute per "
        "mile and kept getting worse, which is the whole failure mode."
    )
    card = rc.build_card(
        {"activity_id": 1, "date": "2026-07-19",
         "activity_name": "A Deliberately Long Activity Name For Overflow",
         "activity_type": "running", "distance_meters": 4925,
         "duration_seconds": 1736, "avg_pace_sec_per_km": 352,
         "avg_hr": 136, "training_load": 51},
        [{"activity_id": 1, "split_index": 0, "distance_meters": 1609.344,
          "duration_seconds": 570, "avg_hr": 125, "avg_pace_sec_per_km": 354,
          "elevation_gain_meters": 24}],
        {"type": "easy", "target_distance_m": 4828,
         "target_pace_sec_per_km": 390, "seq": 1},
        {"mode": "rolling_60d", "n": 12, "pool": "running",
         "median_distance_m": 5000.0, "median_pace_sec_per_km": 360.0,
         "median_hr": 146.0, "median_load": 53.0},
        {"ctl": 57.0, "atl": 87.0, "tsb": -30.0},
    )
    # Every section carries the long paragraph — the worst case the hero cell
    # can be asked to hold.
    card["coach_read"] = {key: long_read for key, _ in workout_coach.READ_SECTIONS}
    pdf, _pages = visuals.render_report_card_pdf(card, None)
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        margin_pt = 1.5 * 28.35  # @page margin: 1.5cm
        offenders = []
        for page in doc.pages:
            right_edge = page.width - margin_pt
            offenders += [
                (w["text"], round(w["x1"] - right_edge, 1))
                for w in page.extract_words() if w["x1"] > right_edge + 0.5
            ]
    assert offenders == []


def _report_card_with_splits(n_splits: int, long_read: bool = True) -> dict:
    """A graded card carrying `n_splits` mile splits (and, by default, the
    four-paragraph worst-case coach read) — the input shape that outgrows the
    density ladder. Split HR climbs across the run so the chart is non-degenerate."""
    from local_fitness.agent import report_card as rc
    from local_fitness.agent import workout_coach

    splits = [
        {"activity_id": 1, "split_index": i, "distance_meters": 1609.344,
         "duration_seconds": 540 + i * 5, "avg_hr": 130 + i,
         "avg_pace_sec_per_km": 335 + i * 3, "elevation_gain_meters": 10 + i}
        for i in range(n_splits)
    ]
    card = rc.build_card(
        {"activity_id": 1, "date": "2026-07-19", "activity_name": "Long Run",
         "activity_type": "running", "distance_meters": 1609.344 * n_splits,
         "duration_seconds": 540 * n_splits, "avg_pace_sec_per_km": 348,
         "avg_hr": 142, "training_load": 90},
        splits,
        {"type": "long", "target_distance_m": 1609.344 * n_splits,
         "target_pace_sec_per_km": 360, "seq": 1},
        {"mode": "rolling_60d", "n": 12, "pool": "running",
         "median_distance_m": 1609.344 * n_splits, "median_pace_sec_per_km": 360.0,
         "median_hr": 146.0, "median_load": 88.0},
        {"ctl": 57.0, "atl": 87.0, "tsb": -30.0},
    )
    if long_read:
        read = (
            "You held pace through the whole distance and every split says so, "
            "cruising within a few seconds of target from the first mile to the "
            "last without the late fade a long run usually shows. That is the "
            "aerobic base doing its job on a hard prescription."
        )
        card["coach_read"] = {key: read for key, _ in workout_coach.READ_SECTIONS}
    return card


def test_report_card_pdf_prints_the_stimulus_section_without_a_grade_column():
    """0.40.0: the PDF grows a Stimulus section. It must carry the numbers and
    the not-graded explanation, and must NOT print a Grade column beside them —
    the whole failure being fixed is a reader inferring a letter from a low
    load."""
    card = _report_card_with_splits(2, long_read=False)
    pdf, pages = visuals.render_report_card_pdf(card, None)
    assert pages == 1
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        text = "\n".join(p.extract_text() or "" for p in doc.pages)
    # Section headings and table headers are uppercased by CSS text-transform,
    # so compare case-insensitively rather than pinning the presentation. Line
    # wrapping is collapsed for the same reason — a phrase broken across two
    # rendered lines is still on the page.
    upper = " ".join(text.split()).upper()
    assert "STIMULUS" in upper
    assert "COMPLIANCE" in upper          # the graded table was renamed
    assert "NOT GRADED" in upper
    assert "BANK LESS OF IT" in upper
    # The compliance table still has its Grade column; the stimulus table has
    # Signal/Value only.
    assert "GRADE" in upper
    assert "SIGNAL" in upper and "VALUE" in upper


def test_pdf_and_markdown_stimulus_sections_cannot_diverge():
    """Both renderers read the same rows/notes helpers. Pin that, because they
    HAD diverged (the PDF dropped the section with no level while the markdown
    kept it) and a card whose table and PDF disagree is the exact failure
    render_report_card_pdf's already-built-card contract exists to prevent."""
    from local_fitness.agent import report_card as rc

    card = _report_card_with_splits(2, long_read=False)
    md = rc.render_markdown(card)
    html_doc = visuals._build_report_card_html(card, None, visuals.DENSITY_PRESETS[0])
    for _label, value in rc.stimulus_rows(card):
        assert value in md
        assert value in html_doc or html.escape(value) in html_doc
    for note in rc.stimulus_notes(card):
        assert note in md
        assert note in html_doc or html.escape(note) in html_doc


def test_render_report_card_pdf_returns_page_count():
    """render_report_card_pdf mirrors render_brief_pdf's `(bytes, page_count)`
    contract — a small card fits one page and says so.

    This pair used to be the WHOLE page-count net for the card, and between
    them they blessed the bug: this one only ever exercised a 2-split card with
    no chart, and its sibling below asserts `pages > 1`. Nothing asserted that
    an ordinary card fits — see test_report_card_is_always_exactly_one_page,
    which is the real guarantee.
    """
    pdf, pages = visuals.render_report_card_pdf(
        _report_card_with_splits(2), None)
    assert pages == 1
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        assert len(doc.pages) == 1


@pytest.mark.parametrize("n_splits", [3, 5, 6, 8, 9, 11, 14])
def test_report_card_is_always_exactly_one_page(n_splits):
    """The report card's half of the one-page contract, and the regression net
    for the bug that made it false.

    `img.split-chart` was capped by WIDTH only, so `chart_h_pt` — the knob the
    density ladder exists to turn — was never read by the report-card
    stylesheet at all and no rung could buy vertical room. Measured on the live
    DB at the time: 3 of 15 stored cards rendered 2 pages, with the HR chart
    landing alone on page 2 under nine inches of white. `img.chart` had carried
    the height cap since 2026-07-22 and the lesson was simply never applied
    here.

    Parametrized across the real range: 3 splits is a short recovery run, 14 is
    a half marathon (activity 22890867603, which was 2 pages on dev for a
    SECOND reason — row count, not chart height — and is why
    CARD_DENSITY_PRESETS has a 4th rung the brief does not).

    Renders the real chart because the chart is the thing that breaks the page;
    a version of this test that passed `None` would pass against the bug.
    """
    card = _report_card_with_splits(n_splits)
    split_chart = visuals.render_split_hr_png(card)
    pdf, pages = visuals.render_report_card_pdf(card, split_chart)
    assert pages == 1
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        assert len(doc.pages) == 1


def test_split_chart_is_capped_by_height_not_only_width():
    """The specific CSS property whose absence caused the two-page card.

    A width-only cap lets the figure's own aspect ratio decide the page budget,
    which is exactly what `img.chart` documents and what this rule did not do.
    Asserting the declaration (rather than only the page count above) is what
    makes a revert fail loudly instead of drifting back in behind a fixture
    that happens to still fit.
    """
    from local_fitness.agent import branding

    css = visuals._report_card_css(
        branding.load_theme(), visuals.CARD_DENSITY_PRESETS[2])
    rule = css.split("img.split-chart")[1].split("}")[0]
    assert "max-height" in rule
    assert "width: auto" in rule
    # And it must be the ladder's knob, not a constant — otherwise the rungs
    # below "roomy" buy nothing.
    assert f"{visuals.CARD_DENSITY_PRESETS[2]['chart_h_pt']}pt" in rule


def test_the_card_ladder_never_changes_the_brief_ladder():
    """CARD_DENSITY_PRESETS is deliberately a separate tuple.

    The brief reads `page_count > 1` to decide whether to DROP a takeaway, so a
    roomier chart cap or an extra rung on its ladder silently changes which
    takeaways get printed. Measured while writing 0.41.0: sharing one tuple
    made a 4-takeaway brief stop reporting overflow.
    """
    assert len(visuals.DENSITY_PRESETS) == 3
    assert len(visuals.CARD_DENSITY_PRESETS) == 4
    assert visuals.CARD_DENSITY_PRESETS[:2] == visuals.DENSITY_PRESETS[:2]
    # The brief's bottom rung keeps its own cap; the card's is tighter.
    assert visuals.DENSITY_PRESETS[2]["chart_h_pt"] == 82.0
    assert visuals.CARD_DENSITY_PRESETS[2]["chart_h_pt"] == 68.0


def test_report_card_reports_overflow_rather_than_silently_spilling():
    """A long run's worth of mile splits plus the four-paragraph coach read
    exhausts the density ladder. The card has nothing to drop, so the contract
    is that render_report_card_pdf SAYS so (page_count > 1) instead of quietly
    emitting two pages — the signal workout_report_card logs. Measured through
    the real chart+layout pipeline, not eyeballed."""
    card = _report_card_with_splits(20)
    split_chart = visuals.render_split_hr_png(card)
    _pdf, pages = visuals.render_report_card_pdf(card, split_chart)
    assert pages > 1


# --- the one-page guarantee ------------------------------------------------
# Both PDFs are single-page documents by contract (2026-07-22). Before that a
# 3-takeaway brief and a 6-split report card each rendered 2 pages. These are
# the regression net for that contract, and they measure page count rather
# than trusting the CSS to still be tuned right.

def _chart_png() -> bytes:
    """A chart at the real figure geometry, so page-count tests exercise the
    height a live chart actually costs."""
    return visuals.render_chart_png(_SERIES, "line", _fmt, "last 14 days")


def _realistic_takeaways(n: int) -> list[Takeaway]:
    return [
        Takeaway(
            headline=f"Signal {i} headline that runs to a realistic length",
            summary="A standfirst of the length the generator actually emits, "
                    "roughly twenty-five words so the card height is honest "
                    "rather than optimistic about what fits.",
            tone="neutral",
            metric=TakeawayMetric(metric="rhr", days=14),
            details="Four or five sentences of deep-dive prose, which is what "
                    "the model writes in practice. It cites a number, explains "
                    "what the number means, and then says what to do about it "
                    "today. That is the realistic worst case for card height.",
        )
        for i in range(n)
    ]


@pytest.mark.parametrize("n_takeaways", [1, 2, 3])
@pytest.mark.parametrize("with_plan", [True, False])
def test_brief_fits_one_page_at_realistic_takeaway_counts(n_takeaways, with_plan):
    """Up to 3 charted takeaways — the shape briefs actually come in — the
    density ladder alone holds one page with no content dropped."""
    png = _chart_png()
    pdf, pages = visuals.render_brief_pdf(
        _brief(_realistic_takeaways(n_takeaways)),
        {str(i): png for i in range(n_takeaways)},
        _PLAN_SECTION if with_plan else None,
    )
    assert pages == 1
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        assert len(doc.pages) == 1


@pytest.mark.parametrize("n_takeaways", [1, 2, 3])
def test_brief_fits_one_page_with_the_sessions_adherence_sublabel(n_takeaways):
    """The longer adherence label wraps to a second line inside its tile.
    Measured, not assumed: that extra height must still land on one page."""
    png = _chart_png()
    pdf, pages = visuals.render_brief_pdf(
        _brief(_realistic_takeaways(n_takeaways)),
        {str(i): png for i in range(n_takeaways)},
        dict(_PLAN_SECTION, sessions_adherence_pct=62, rest_days_counted=2),
    )
    assert pages == 1
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        assert len(doc.pages) == 1


@pytest.mark.parametrize("n_takeaways", [4, 5])
def test_render_reports_overflow_rather_than_silently_spilling(n_takeaways):
    """At the schema's upper end the ladder can be exhausted. The contract is
    that `render_brief_pdf` SAYS so (page_count > 1) instead of quietly
    emitting two pages — that signal is what drives truncation in
    `generate_brief_report`, which owns the actual one-page guarantee."""
    png = _chart_png()
    _pdf, pages = visuals.render_brief_pdf(
        _brief(_realistic_takeaways(n_takeaways)),
        {str(i): png for i in range(n_takeaways)},
        None,
    )
    assert pages > 1


def test_fit_one_page_steps_down_until_it_fits():
    """The ladder must actually walk: a document that only fits at a denser
    rung has to come back on that rung, not on the first one tried."""
    seen: list[str] = []

    def build(preset):
        seen.append(preset["name"])
        # Height scales with the preset's body size, so only the dense rung fits.
        rows = "".join("<p>line</p>" for _ in range(120))
        return (f"<html><head><style>@page {{ size: A4; margin: 1.5cm; }}"
                f"body {{ font-size: {preset['body_pt']}pt; }}"
                f"p {{ margin: 0; }}</style></head><body>{rows}</body></html>")

    _pdf, pages, index = visuals.fit_one_page(build)
    assert seen == ["roomy", "compact", "dense"]
    assert index == 2
    assert pages > 1  # exhausted the ladder; caller must drop content


def test_fit_one_page_stops_at_the_first_rung_that_fits():
    seen: list[str] = []

    def build(preset):
        seen.append(preset["name"])
        return "<html><body><p>short</p></body></html>"

    _pdf, pages, index = visuals.fit_one_page(build)
    assert seen == ["roomy"]
    assert (pages, index) == (1, 0)


def test_fit_one_page_rejects_an_empty_ladder():
    with pytest.raises(ValueError, match="presets must not be empty"):
        visuals.fit_one_page(lambda _p: "<html><body>x</body></html>", [])


def test_omitted_takeaways_are_stated_on_the_page():
    pdf, _pages = visuals.render_brief_pdf(
        _brief([Takeaway(headline="h", summary="s", tone="neutral", details="d")]),
        {}, _PLAN_SECTION, omitted=2,
    )
    assert "2 further signals omitted for space." in _pdf_text(pdf)


def test_omitted_note_is_singular_for_one():
    pdf, _pages = visuals.render_brief_pdf(
        _brief([Takeaway(headline="h", summary="s", tone="neutral", details="d")]),
        {}, _PLAN_SECTION, omitted=1,
    )
    assert "1 further signal omitted for space." in _pdf_text(pdf)


def test_no_omitted_note_when_nothing_was_dropped():
    pdf, _pages = visuals.render_brief_pdf(
        _brief([Takeaway(headline="h", summary="s", tone="neutral", details="d")]),
        {}, _PLAN_SECTION,
    )
    assert "omitted for space" not in _pdf_text(pdf)


@pytest.mark.parametrize(
    "n_cards,has_plan,expected",
    [
        (0, True, 0), (1, True, 1), (2, True, 2), (3, True, 2),
        (4, True, 3), (5, True, 3),
        (1, False, 1), (3, False, 3), (5, False, 5),
    ],
)
def test_cards_in_left_rail_balances_against_the_plan_block(n_cards, has_plan, expected):
    assert visuals.cards_in_left_rail(n_cards, has_plan) == expected


def test_overflow_cards_render_below_the_plan_not_dropped():
    """A card pushed into the right rail must still be ON the page — the split
    moves cards, it never loses them."""
    takeaways = [
        Takeaway(headline=f"Headline number {i}", summary="s", tone="neutral",
                 details="d")
        for i in range(4)
    ]
    pdf, _pages = visuals.render_brief_pdf(_brief(takeaways), {}, _PLAN_SECTION)
    text = _pdf_text(pdf)
    for i in range(4):
        assert f"Headline number {i}" in text


# --- table geometry: the two measured collisions ---------------------------

def _words(pdf: bytes) -> list[dict]:
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        return [w for p in doc.pages for w in p.extract_words()]


def test_week_table_type_never_touches_the_planned_column():
    """Regression for 2026-07-22: `interval` and `5.0` extracted as the single
    word `interval5.0` — the Type and Planned columns had no gap at all."""
    plan = dict(_PLAN_SECTION)
    plan["last_7_days"] = [
        {"date": "2026-07-09", "type": "interval", "planned_mi": 5.0,
         "actual_mi": 9.18, "verdict": "done"},
    ]
    pdf, _pages = visuals.render_brief_pdf(
        _brief([Takeaway(headline="h", summary="s", tone="neutral", details="d")]),
        {}, plan,
    )
    texts = [w["text"] for w in _words(pdf)]
    assert "interval" in texts, texts
    assert not any(t.startswith("interval") and len(t) > len("interval") for t in texts)


def test_stat_tile_values_never_cross_into_the_next_tile():
    """Regression for 2026-07-22: the compound "This Week" value overran its
    half-rail tile and read as one number with the Slips tile beside it."""
    plan = dict(_PLAN_SECTION)
    # A deliberately wide compound value — three-digit mileage both sides.
    plan["week_actual_mi"] = 122.3
    plan["week_planned_mi"] = 129.5
    pdf, _pages = visuals.render_brief_pdf(
        _brief([Takeaway(headline="h", summary="s", tone="neutral", details="d")]),
        {}, plan,
    )
    words = _words(pdf)
    slips = next(w for w in words if w["text"] == "SLIPS")
    # The wide value lives on its own full-width row BELOW the three short
    # tiles, so nothing from it may sit on the Slips label's baseline band.
    band = [w for w in words if abs(w["top"] - slips["top"]) < 2]
    assert "122.3" not in [w["text"] for w in band]


def test_walk_miles_are_named_when_present():
    plan = dict(_PLAN_SECTION, week_walk_mi=9.3)
    pdf, _pages = visuals.render_brief_pdf(
        _brief([Takeaway(headline="h", summary="s", tone="neutral", details="d")]),
        {}, plan,
    )
    assert "+9.3 walked" in _pdf_text(pdf).lower()


def test_walk_suffix_absent_when_no_walking():
    pdf, _pages = visuals.render_brief_pdf(
        _brief([Takeaway(headline="h", summary="s", tone="neutral", details="d")]),
        {}, dict(_PLAN_SECTION, week_walk_mi=0.0),
    )
    assert "walked" not in _pdf_text(pdf).lower()


# --- chart consistency -----------------------------------------------------

def test_chart_window_label_is_rendered():
    png = visuals.render_chart_png(_SERIES, "line", _fmt, "last 30 days")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    # Same series, different label -> different pixels, so the caption is
    # genuinely drawn rather than silently dropped.
    assert png != visuals.render_chart_png(_SERIES, "line", _fmt, "last 14 days")


def test_chart_without_a_label_still_renders():
    assert visuals.render_chart_png(_SERIES, "line", _fmt)[:8] == b"\x89PNG\r\n\x1a\n"


def test_chart_is_a_band_not_a_square():
    """The height cap only buys page room if the figure is wide and short."""
    w, h = visuals.CHART_FIGSIZE
    assert w / h > 2.5


def test_fit_one_page_decodes_each_asset_once_across_rungs(monkeypatch):
    """0.36.0 (S10): the shared image cache must stop per-rung re-decodes —
    a 3-rung fit used to re-fetch/re-decode every data: chart and the
    @font-face TTF at each rung. The fetcher is only consulted on a cache
    miss, so a multi-rung walk fetching the same data: URL once IS the
    behavior under test."""
    fetches: list[str] = []
    real_factory = visuals._report_url_fetcher

    def counting_factory():
        real = real_factory()

        def fetcher(url):
            fetches.append(url[:40])
            return real(url)

        return fetcher

    monkeypatch.setattr(visuals, "_report_url_fetcher", counting_factory)
    # A real 1x1 PNG as a data: URI, embedded at every rung; tall body so the
    # ladder walks all three rungs.
    png_b64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
               "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")

    def build(preset):
        rows = "".join("<p>line</p>" for _ in range(120))
        return (f"<html><head><style>@page {{ size: A4; margin: 1.5cm; }}"
                f"body {{ font-size: {preset['body_pt']}pt; }}"
                f"p {{ margin: 0; }}</style></head><body>"
                f'<img src="data:image/png;base64,{png_b64}">{rows}</body></html>')

    _pdf, pages, index = visuals.fit_one_page(build)
    assert index == 2  # the ladder genuinely walked all three rungs
    assert len(fetches) == 1  # ...but the image was fetched/decoded ONCE


# --- density-ladder hint (0.48.0) -------------------------------------------
# Remembering the winning rung skips layout passes a previous render already
# proved too roomy. MEASURED across all 15 live cards with their real coach
# reads: winners {0:1, 1:13, 2:1}, 342 ms saved of 3551 ms = 9.6% (23 ms/card).
# NOT the ~65% a first estimate suggested — that figure was "time spent on
# discarded layouts", which is only recoverable by dropping the one-rung
# headroom, and without headroom a document that got shorter could never climb
# back to a roomier layout.


def test_ladder_hint_defaults_to_zero_when_absent(tmp_path, monkeypatch):
    from local_fitness import db

    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "fitness.db")
    assert visuals.read_ladder_hint("card") == 0


def test_ladder_hint_round_trips_per_kind(tmp_path, monkeypatch):
    from local_fitness import db

    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "fitness.db")
    visuals.write_ladder_hint("card", 2)
    visuals.write_ladder_hint("brief", 1)
    assert visuals.read_ladder_hint("card") == 2
    assert visuals.read_ladder_hint("brief") == 1
    # The two documents have different ladders and must not share a hint.
    visuals.write_ladder_hint("card", 3)
    assert visuals.read_ladder_hint("brief") == 1


@pytest.mark.parametrize("corrupt", ["not json at all", "[]", '{"card": "two"}',
                                     '{"card": -4}', '{"card": null}'])
def test_a_corrupt_hint_degrades_to_zero(tmp_path, monkeypatch, corrupt):
    """A disposable performance hint must never be able to break a render."""
    from local_fitness import db

    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "fitness.db")
    (tmp_path / visuals._LADDER_HINT_FILE).write_text(corrupt, encoding="utf-8")
    assert visuals.read_ladder_hint("card") == 0


def test_write_hint_never_raises_on_an_unwritable_path(tmp_path, monkeypatch):
    import pathlib

    from local_fitness import db

    monkeypatch.setattr(
        db, "DEFAULT_DB_PATH", pathlib.Path("/proc/nonexistent/fitness.db"))
    visuals.write_ladder_hint("card", 1)          # must not raise
    assert visuals.read_ladder_hint("card") == 0


def test_start_index_is_clamped_into_the_ladder():
    """A stale hint pointing past the end must not skip the ladder entirely or
    IndexError — it clamps to the last rung and still returns a real render."""
    calls = []

    def build(preset):
        calls.append(preset["name"])
        return "<html><body><p>tiny</p></body></html>"

    _pdf, pages, index = visuals.fit_one_page(
        build, visuals.DENSITY_PRESETS, start_index=99)
    assert pages == 1
    assert index == len(visuals.DENSITY_PRESETS) - 1
    assert calls == [visuals.DENSITY_PRESETS[-1]["name"]]


def test_start_index_skips_the_rungs_before_it():
    """The actual mechanism: rungs below start_index are never laid out."""
    calls = []

    def build(preset):
        calls.append(preset["name"])
        return "<html><body><p>tiny</p></body></html>"

    _pdf, _pages, index = visuals.fit_one_page(
        build, visuals.DENSITY_PRESETS, start_index=1)
    assert index == 1
    assert calls == [visuals.DENSITY_PRESETS[1]["name"]]
    assert visuals.DENSITY_PRESETS[0]["name"] not in calls


def test_negative_start_index_is_treated_as_zero():
    calls = []

    def build(preset):
        calls.append(preset["name"])
        return "<html><body><p>tiny</p></body></html>"

    _pdf, _pages, index = visuals.fit_one_page(
        build, visuals.DENSITY_PRESETS, start_index=-3)
    assert index == 0
    assert calls == [visuals.DENSITY_PRESETS[0]["name"]]
