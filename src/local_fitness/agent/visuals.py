"""Rendering internals shared by the `generate_brief_report` and
`workout_report_card` MCP tools plus `chart`'s png format (agent/tools.py).

Only the first two are `LOCAL_ONLY_TOOLS` — they hand back a filesystem path, which
is useless to a caller on the far side of the networked `/mcp/` transport.
`chart format="png"` (the former `generate_chart` tool, folded in 0.57.0) is in
`ALL_TOOLS`: it returns the PNG as an inline MCP image content block, so a
client never needs the path.

Heavy native imports (matplotlib, weasyprint) are deferred into the two
render functions' bodies rather than this module's top level — this module
is itself only ever imported from inside the two `@tool`-decorated function
bodies (never at `tools.py` module scope), so the always-running web server
process never pays their import cost or inherits a native-binding break.

Both render functions must be called only while holding `RENDER_LOCK`
(acquired at the tool-call site in tools.py) — this serializes chart and PDF
rendering within one process rather than requiring an audit of matplotlib's
`pyplot`-global-state or WeasyPrint's internal thread-safety.
"""
from __future__ import annotations

import asyncio
import base64
import html
import io
import json
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .schemas import Brief

# All colors/fonts/identity come from the brand theme (agent/branding.py):
# the PRESS editorial system by default (warm paper, ink rules, one accent),
# local-overridable via LOCAL_FITNESS_BRAND_FILE. The theme is the single
# source of truth for both the WeasyPrint report CSS and the matplotlib
# chart stylesheet in render_chart_png — the ASCII chart tool keeps its own
# emoji heat ramp (PRESS is the *print* brand).
from . import branding

LOG = logging.getLogger(__name__)

_VERDICT_LABEL = {
    "done": "done",
    "partial": "partial",
    "missed": "missed",
    "compliant": "rest",
    "pending": "scheduled",
}

# Serializes every chart/PDF render within this process. Closes the "is
# WeasyPrint's write_pdf() safe to call concurrently from two threads"
# question by construction, the same way it removes any need to reason about
# whether the underlying MCP Server could ever have two tool calls in flight
# within one stdio session — renders simply never overlap regardless.
RENDER_LOCK = asyncio.Lock()

# --- one-page fitting ------------------------------------------------------
# Both PDFs are single-page documents by contract. They did not used to be:
# measured 2026-07-22, a 3-takeaway brief rendered 2 pages while ~150pt of
# page 1 sat empty (an HTML table row cannot fragment gracefully, so a card
# that won't fit is pushed WHOLE to the next page), and a 6-split report card
# did the same. Neither renderer had any idea how tall its own output was.
#
# The fix is to measure rather than guess: lay the document out, count
# `len(document.pages)`, and step down a density ladder until it fits. Density
# is a handful of scalars threaded into the stylesheet, NOT a second
# stylesheet — one sheet with parameters can't drift from itself.
#
# Ordered roomiest-first. `chart_h_pt` is the load-bearing knob: charts are
# what actually break the page (measured: the same brief fits on one page with
# its charts removed), so capping their HEIGHT is the only lever that reliably
# buys vertical room. Type scale alone does not.
DENSITY_PRESETS: tuple[dict, ...] = (
    {"name": "roomy",   "body_pt": 11.3, "chart_h_pt": 150.0,
     "card_gap_em": 0.75, "hero_em": 5.2, "split_chart_pct": 84.0},
    {"name": "compact", "body_pt": 10.4, "chart_h_pt": 112.0,
     "card_gap_em": 0.55, "hero_em": 4.4, "split_chart_pct": 76.0},
    {"name": "dense",   "body_pt": 9.6,  "chart_h_pt": 82.0,
     "card_gap_em": 0.40, "hero_em": 3.6, "split_chart_pct": 68.0},
)

# The report card's ladder: the three shared rungs plus one the brief must NOT
# have. A card's overflow has TWO drivers and capping the chart only fixes one
# of them; the other is split-row count, which a brief never has. Measured with
# real stored coach reads: at "dense" a 9-split card with a 969-char read still
# laid out to 2 pages, and a 14-split half marathon (activity 22890867603) did
# so at every chart height tested down to 46pt — it was 2 pages on dev too, so
# this rung fixes a spill that predates the chart-cap bug and was never
# diagnosed.
#
# A SEPARATE tuple, not DENSITY_PRESETS plus an entry, and both differences
# from the brief's ladder are deliberate:
#
#  * `dense` caps the chart at 68pt rather than 82. Once `img.split-chart`
#    started honoring the cap at all, 82 was measured to be on the wrong side
#    of the cliff for an ordinary 5-mile card: bisecting activity 23825963527
#    with its HR trace, 80pt still laid out to 2 pages and 78pt was the first
#    value to fit. 68 sits a deliberate 10pt below that boundary.
#  * the 4th rung exists at all.
#
# Neither may leak into the brief. The brief's exhaustion of its ladder is a
# SIGNAL — `generate_brief_report` reads `page_count > 1` to decide whether to
# drop a takeaway — so a roomier cap or an extra rung there silently changes
# which takeaways get printed. Measured while writing this: sharing the tuple
# made a 4-takeaway brief stop reporting overflow, which is a real behavior
# change to a document this work was not asked to touch. The card has no
# droppable content, so for it the ladder is the whole mechanism.
CARD_DENSITY_PRESETS: tuple[dict, ...] = (
    DENSITY_PRESETS[0],
    DENSITY_PRESETS[1],
    {**DENSITY_PRESETS[2], "chart_h_pt": 68.0},
    {"name": "ultra",   "body_pt": 8.6,  "chart_h_pt": 52.0,
     "card_gap_em": 0.28, "hero_em": 3.0, "split_chart_pct": 62.0},
)


#: Where the per-document winning-rung hints live — next to the SQLite DB, in
#: the already-gitignored `data/` dir, exactly like the two coach caches.
#: Deliberately NOT in the DB: it is a disposable performance hint, and losing
#: it costs one slower render, not correctness.
_LADDER_HINT_FILE = "density_ladder_hints.json"


def _ladder_hint_path() -> Path:
    from .. import db

    return db.DEFAULT_DB_PATH.parent / _LADDER_HINT_FILE


def read_ladder_hint(kind: str) -> int:
    """The rung that last won for ``kind``, or 0 when unknown.

    Best-effort by contract: a missing, unreadable or corrupt hint file returns
    0, which is exactly the pre-0.48.0 behaviour. A performance hint must never
    be able to break a render.
    """
    try:
        raw = json.loads(_ladder_hint_path().read_text(encoding="utf-8"))
        value = raw.get(kind)
        return int(value) if isinstance(value, int) and value >= 0 else 0
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


def write_ladder_hint(kind: str, index: int) -> None:
    """Record the winning rung. Never raises."""
    try:
        path = _ladder_hint_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raw = {}
        except (OSError, ValueError):
            raw = {}
        if raw.get(kind) == index:
            return  # no write when nothing moved
        raw[kind] = int(index)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw), encoding="utf-8")
    except (OSError, ValueError, TypeError):
        LOG.debug("density-ladder hint write failed (ignored)", exc_info=True)


def _fit_with_hint(
    build_html: Callable[[dict], str], presets: Sequence[dict], kind: str
) -> tuple[bytes, int]:
    """``fit_one_page`` with the winning rung remembered across renders.

    One rung of headroom (``hint - 1``) so a document that shrank can climb
    back to a roomier layout; see ``fit_one_page``'s ``start_index`` note for
    why that converges rather than ratcheting dense.
    """
    hint = read_ladder_hint(kind)
    pdf, pages, index = fit_one_page(
        build_html, presets, start_index=max(0, hint - 1))
    write_ladder_hint(kind, index)
    return pdf, pages


def fit_one_page(
    build_html: Callable[[dict], str],
    presets: Sequence[dict] = DENSITY_PRESETS,
    *,
    start_index: int = 0,
) -> tuple[bytes, int, int]:
    """Lay `build_html(preset)` out at each density until it fits one page.

    Returns ``(pdf_bytes, page_count, preset_index)``. `page_count` is the
    count for the preset actually used, so a caller that still gets 2 back
    knows the ladder was exhausted and it is content, not type size, that has
    to give — `generate_brief_report` uses exactly that signal to decide
    whether to drop a takeaway.

    Renderer-agnostic on purpose: it takes a callable, not a Brief or a card,
    so the brief report and the workout report card share one implementation
    of "is this one page" rather than each growing their own.

    Costs one layout pass per preset (measured ~65ms each) and writes the PDF
    from the winning `Document` rather than re-rendering it — `render()` then
    `write_pdf()` on the same object is the whole reason this doesn't call
    `HTML.write_pdf()` directly.

    One ``FontConfiguration`` + one image ``cache`` dict are shared across
    the rungs (0.36.0): with neither, every rung got a fresh empty cache and
    a fresh font config, so a 3-rung fit re-decoded ~124 KB of base64 chart
    PNGs and re-parsed/re-registered a ~140 KB @font-face TTF twice for
    nothing — density changes scalars in the stylesheet, never the assets.
    Both are PER CALL, not module-global, deliberately: WeasyPrint writes a
    temp font file per registered face, and a process-lifetime
    FontConfiguration on a long-running server would accumulate them without
    bound; per-call keeps the growth bounded at rungs-per-render and lets GC
    reclaim the temp files with the config.
    """
    import weasyprint
    from weasyprint.text.fonts import FontConfiguration

    if not presets:
        raise ValueError("presets must not be empty")

    # `start_index` skips rungs a previous render already proved too roomy.
    # Measured over the live corpus: the card ladder's roomiest rung won 0 of
    # 15 times and 65% of all layout time went on discarded passes (57% for
    # briefs). Callers pass `last_winner - 1`, keeping one rung of headroom so
    # a document that got SHORTER can still climb back — each render moves the
    # hint at most one rung roomier, so it converges on the true optimum
    # instead of ratcheting dense.
    #
    # The trade, stated plainly: a render can land one rung tighter than
    # strictly necessary until it converges. That is deliberate (0.48.0) and
    # is the whole point — this is NOT a pure win, it buys ~200ms with a
    # bounded, self-correcting density cost.
    #
    # Clamped, never trusted: a stale or corrupt hint must not skip the ladder
    # entirely or index out of range.
    first = min(max(0, start_index), len(presets) - 1)

    font_config = FontConfiguration()
    image_cache: dict = {}
    doc = None
    index = first
    for index, preset in enumerate(presets[first:], start=first):  # noqa: B007 — index is read AFTER the loop (returned rung)
        doc = weasyprint.HTML(
            string=build_html(preset), url_fetcher=_report_url_fetcher()
        ).render(font_config=font_config, cache=image_cache)
        if len(doc.pages) == 1:
            break
    return doc.write_pdf(), len(doc.pages), index


def cards_in_left_rail(n_cards: int, has_plan: bool) -> int:
    """How many signal cards stay in the left rail, the rest going BELOW the
    Training Plan in the right one.

    Before this existed, every card went left and the region under the plan
    rail was dead space — on the 2026-07-22 render the entire right rail of
    page 2 was empty (max word x0=303.5 against a rail starting at x=333)
    while cards spilled off page 1. Filling it roughly doubles the usable
    signal area, which is what lets the density ladder stop at a roomier rung
    and keep the charts legible.

    Measured on the 2026-07-22 render: the plan section runs ~347pt against
    ~212pt for a signal card, so it is worth ~2 cards. Balancing
    ``left == right + 2`` gives ``left = (n + 2) / 2``, floored — floored, not
    rounded up, because the plan is a bit under two full cards and rounding up
    tips the left rail back into being the taller one (at n=3, left-3 measures
    3.0 cards against 1.7, while left-2 measures 2.0 against 2.7).

    With no plan section there is no second rail at all and everything stays
    left.
    """
    if not has_plan or n_cards <= 0:
        return n_cards
    return min(n_cards, (n_cards + 2) // 2)


def value_axis_bounds(values) -> tuple[float, float]:
    """Padded (ylo, yhi) for a chart's value axis, scaled to the DATA band —
    never anchored at zero. A level metric like resting HR (48–57 bpm) on a
    0-based axis renders as a flat sliver with 85% of the canvas empty; the
    variation is the signal, so the axis hugs it. 8% padding each side;
    a flat series pads by max(1, 5% of magnitude) so it doesn't collapse to
    a zero-height axis. Pure — unit-tested directly, sign-agnostic (a
    negative TSB band pads the same way)."""
    lo, hi = min(values), max(values)
    span = hi - lo
    pad = span * 0.08 if span else max(1.0, abs(hi) * 0.05)
    return lo - pad, hi + pad


#: Chart aspect. A wide, short band rather than the old 16:9 (8, 4.5): in a
#: ~289pt-wide rail a 16:9 figure is ~162pt tall, and three of them are most of
#: a page — charts were measurably what pushed the brief onto page 2. A band
#: reads better at this width anyway; the shape of a trend does not need square
#: inches, and the height that buys is what keeps every takeaway on one page.
CHART_FIGSIZE = (8.0, 2.8)
#: Y ticks are pinned so three cards stacked in a rail read as one system
#: instead of three charts that happen to share a palette.
CHART_Y_TICKS = 4
CHART_X_TICKS = 5


def render_chart_png(
    series: Sequence[tuple[str, float]],
    chart_type: str,
    value_fmt: Callable[[float], str],
    window_label: str | None = None,
) -> bytes:
    """Render a styled standalone chart PNG for one metric series.

    `series` is a list of (iso_date, value) pairs, already fetched and
    validated by the caller (agent/tools.py's `_fetch_metric_series`) — this
    function never queries the DB or recomputes anything, only formats what
    it's given. `chart_type` is one of "line" | "bar" | "combo" (v1 scope;
    see the design doc for why calendar/spark are excluded).

    `window_label` (e.g. "last 30 days") is captioned on the chart itself.
    It is not decoration: takeaways choose their own window, so one brief
    routinely carries a 30-day, a 14-day and a 60-day chart, and before this
    label existed nothing on the page said which was which — three charts of
    identical size implying three comparable windows.

    Builds its own `Figure()` object (the thread-safe object-oriented API)
    rather than using `pyplot`'s stateful global current-figure functions —
    required because this is invoked via `asyncio.to_thread`, and nothing
    confirms the underlying MCP Server can never have two tool calls in
    flight within one stdio session. `matplotlib.use("Agg")` is set
    defensively even though the OO API + explicit `FigureCanvasAgg` never
    touches the global backend machinery pyplot would.
    """
    import matplotlib

    matplotlib.use("Agg")
    import numpy as np
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.ticker import FuncFormatter, MaxNLocator

    theme = branding.load_theme()
    paper = theme["colors"]["paper"]
    ink = theme["colors"]["ink"]
    dim = theme["colors"]["dim"]
    accent = theme["colors"]["accent"]

    dates = [d for d, _ in series]
    values = [v for _, v in series]
    labels = [d[5:] for d in dates]  # MM-DD, matching the ASCII chart tool

    fig = Figure(figsize=CHART_FIGSIZE, dpi=150, facecolor=paper)
    FigureCanvasAgg(fig)  # explicit non-global canvas attach, never via pyplot
    ax = fig.add_subplot(111)
    ax.set_facecolor(paper)
    x = np.arange(len(labels))

    ylo, yhi = value_axis_bounds(values)

    if chart_type == "line":
        ax.plot(x, values, color=ink, linewidth=2)
        # Fill down to the padded axis floor, NOT to y=0 — a single-argument
        # fill_between fills to zero and drags autoscale down with it, which
        # rendered a 48–57bpm resting-HR band as a sliver atop a 0–57 axis
        # (Nate, 2026-07-19: autoscale everything; zero-basing is pointless).
        ax.fill_between(x, values, ylo, color=ink, alpha=0.06)
    elif chart_type == "bar":
        ax.bar(x, values, color=ink)
    elif chart_type == "combo":
        # Matches agent/charts.py's render_combo_chart: bars + a
        # least-squares trend line of the SAME single metric — one series,
        # one axis, not a second-metric dual-axis chart (see design doc).
        # The trend line is the chart's ONE accent (PRESS signature law).
        ax.bar(x, values, color=ink, alpha=0.55)
        if len(values) >= 2:
            coeffs = np.polyfit(x, values, 1)
            ax.plot(x, np.poly1d(coeffs)(x), color=accent, linewidth=2)
    else:
        raise ValueError(f"unsupported chart_type '{chart_type}'")

    # Autoscale the value axis to the data band for every chart type — bar
    # rectangles are still drawn from 0 but clip at the padded floor, the
    # standard truncated-bar look.
    ax.set_ylim(ylo, yhi)

    ax.grid(axis="y", color=dim, linewidth=0.6, alpha=0.35)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.spines["left"].set_color(dim)
    ax.spines["bottom"].set_color(dim)
    ax.tick_params(colors=ink, labelsize=8)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _pos: value_fmt(v)))
    # Pinned tick COUNT, not a pinned step: every card in a brief then carries
    # the same number of gridlines regardless of its window length, so the
    # three charts read as one system.
    ax.yaxis.set_major_locator(MaxNLocator(CHART_Y_TICKS))

    # Evenly spaced x labels, horizontal. The old `len // 10` step with a 45°
    # rotation was unreadable once the figure became a band — rotated labels
    # eat the height the band exists to save, and there is no room to spend.
    if len(labels) > CHART_X_TICKS:
        step = max(1, (len(labels) - 1) // (CHART_X_TICKS - 1))
        idx = list(range(0, len(labels), step))[:CHART_X_TICKS]
    else:
        idx = list(range(len(labels)))
    ax.set_xticks(x[idx])
    ax.set_xticklabels([labels[i] for i in idx], fontsize=8)

    if window_label:
        ax.set_title(window_label, loc="left", fontsize=8, color=dim, pad=4)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    return buf.getvalue()


def _data_uri(png_bytes: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")


def _report_url_fetcher():
    """WeasyPrint URLFetcher restricted to the data: scheme.

    Chart images are always embedded as data: URIs (see _data_uri above), so
    nothing legitimate ever needs network access. Brief content (headline/
    summary/details) can be influenced by free-text user notes, so an
    embedded <img src>/<link>/@import reaching this fetcher must be rejected
    rather than trusted — this closes both the network-fetch (SSRF-adjacent)
    and unsanitized-markdown-HTML-passthrough risk in one change, since
    neither vector can reach anything without a working fetcher.

    Uses the `URLFetcher` class (not the module-level `default_url_fetcher`
    function, which emits a DeprecationWarning and is slated for removal).
    """
    import weasyprint

    return weasyprint.URLFetcher(allowed_protocols=["data"])


def _font_face_css(theme: dict) -> tuple[str, str]:
    """(optional @font-face block, mono font-family stack) for the theme.

    When ``fonts.mono_file`` names a real TTF, embed it as a data: URI —
    ``_report_url_fetcher`` rejects every scheme except ``data:``, so a
    file:// reference would be blocked at fetch time — and prepend the
    ``BrandMono`` family to the stack. Missing/unreadable file → system
    stack only, never an error."""
    mono_stack = theme["fonts"]["mono_stack"]
    mono_file = theme["fonts"].get("mono_file")
    if not mono_file:
        return "", mono_stack
    try:
        font_bytes = Path(mono_file).read_bytes()
    except OSError:
        return "", mono_stack
    uri = "data:font/ttf;base64," + base64.b64encode(font_bytes).decode("ascii")
    face = f"@font-face {{ font-family: 'BrandMono'; src: url('{uri}'); }}"
    return face, f"'BrandMono', {mono_stack}"


def _build_css(theme: dict, density: dict | None = None) -> str:
    """The report stylesheet, built from the brand theme at a given density.

    PRESS grammar (the default theme's rules, kept even under color
    overrides): flat paper canvas, ink rules for all structure, NO rounded
    corners / shadows / gradients, sans 800–900 tight-tracked for
    structure, serif italic for commentary, mono for data. Tones and
    verdicts are typographic — the accent appears only on critical/missed
    (and the stamp).

    `density` is one of `DENSITY_PRESETS` (default: the roomiest). It scales
    the handful of dimensions that actually decide page count — body type,
    chart height, inter-card gap — rather than selecting a different sheet,
    so a rule fixed at one density can never be missing at another."""
    d = density or DENSITY_PRESETS[0]
    c = theme["colors"]
    f = theme["fonts"]
    paper, ink, dim, accent, rule = (
        c["paper"], c["ink"], c["dim"], c["accent"], c["rule"])
    font_face, mono = _font_face_css(theme)
    return f"""
{font_face}
@page {{ size: A4; margin: 1.5cm; }}
html {{ background: {paper}; }}
body {{
  font-family: {f["display_stack"]};
  color: {ink};
  font-size: {d["body_pt"]}pt;
  line-height: 1.42;
  margin: 0;
}}

/* Masthead: heavy ink rule, rotated accent stamp, tracked-caps eyebrow in
   the mono data voice, dim byline right. The editorial opening — no pills,
   no banner fills. */
div.masthead {{
  border-top: 8px solid {rule};
  padding-top: 0.55em;
  margin-bottom: 1.1em;
}}
table.masthead-row {{ width: 100%; border-collapse: collapse; }}
table.masthead-row td {{ vertical-align: middle; padding: 0; }}
span.stamp {{
  display: inline-block;
  border: 2.5px solid {accent};
  color: {accent};
  font-weight: 900;
  font-size: 0.8em;
  letter-spacing: 0.02em;
  padding: 0.22em 0.34em;
  transform: rotate(-4deg);
}}
span.eyebrow {{
  font-family: {mono};
  font-size: 0.72em;
  font-weight: 700;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: {ink};
  padding-left: 0.9em;
}}
td.byline {{
  text-align: right;
  font-family: {mono};
  font-size: 0.66em;
  letter-spacing: 0.05em;
  color: {dim};
}}
div.masthead h1 {{
  font-size: 1.7em;
  font-weight: 900;
  letter-spacing: -0.02em;
  margin: 0.35em 0 0 0;
}}

/* Whole-page 2-column layout: signal cards (left) run in parallel with
   the Training Plan (right) instead of stacking sequentially — this is
   what actually buys back the vertical room to fit one page, not just
   tighter type. An HTML table, not flex/grid: both were tried for the
   signal-card pairing earlier and both are broken in WeasyPrint 69.0 for
   this content shape (every flex/grid item lands on its own row/track
   regardless of basis, content length, or surrounding rules — confirmed
   via pdfplumber word bounding boxes, not by eyeballing a render). Table
   layout is the one primitive that reliably holds a real 2-column split
   here, so the whole-page structure reuses it too. */
/* Cell width percentages + their em paddings must sum ≤ 100% of the
   table, or fixed layout paints the right rail past the @page margin
   (measured: up to 14pt of overflow at 56%+44%+1.8em). 54+42=96% leaves
   ~4% ≈ the two 0.9em paddings. */
table.page-layout {{ width: 100%; table-layout: fixed; border-collapse: collapse; }}
/* Descendant selector, NOT `> tr > td`: HTML parsing inserts an implicit
   <tbody>, so the child combinator never matched and these cells silently kept
   `vertical-align: middle` — which is why the Training Plan rail floated in
   the vertical centre of the page with a large void above it while the signals
   rail started at the top. */
/* Only the axis that doesn't conflict. `padding: 0` here would WIN against
   `td.col-plan`'s padding-left (this selector is more specific: one class plus
   two elements), which stripped the right rail's gutter and printed the plan
   text hard against the divider rule. Horizontal padding belongs to the
   column classes alone. */
table.page-layout td {{ vertical-align: top; padding-top: 0; padding-bottom: 0; }}
td.col-signals {{ width: 54%; padding-left: 0; padding-right: 0.9em; }}
td.col-plan {{
  width: 42%;
  padding-left: 0.9em;
  padding-right: 0;
  border-left: 2px solid {rule};
}}
/* No active plan: signals run the full page width, no second rail. */
div.col-signals-full {{ width: 100%; }}

/* Signal cards → ruled editorial sections: structure from ink rules and
   whitespace, never fills or radii. Headline = display voice (900, tight
   tracking); summary = serif-italic standfirst in dim. Tone is
   typographic — only critical earns the accent. */
section.signal-card {{
  border-top: 2px solid {rule};
  padding: 0.55em 0 0.35em 0;
  margin-bottom: {d["card_gap_em"]}em;
  page-break-inside: avoid;
}}
section.signal-card h2 {{
  font-size: 1.06em;
  font-weight: 900;
  letter-spacing: -0.02em;
  margin: 0 0 0.15em 0;
}}
section.tone-critical {{ border-top-color: {accent}; }}
section.tone-critical h2 {{ color: {accent}; }}
section.tone-caution h2 {{ color: {dim}; font-style: italic; }}
p.summary {{
  font-family: {f["serif_stack"]};
  font-style: italic;
  color: {dim};
  margin: 0 0 0.35em 0;
}}
/* Capped by HEIGHT, not just width. `max-width: 100%` alone lets the chart's
   own aspect ratio decide how much of the page it eats, which is why the
   density ladder could not previously buy any vertical room — `width: auto`
   keeps the aspect while `max-height` makes the cap real. */
img.chart {{
  max-width: 100%;
  max-height: {d["chart_h_pt"]}pt;
  width: auto;
  margin: 0.3em 0;
}}
/* Stated, never silent: when the density ladder bottoms out and takeaways had
   to be dropped to hold one page, the page says how many. */
p.omitted-note {{
  font-family: {mono};
  font-size: 0.68em;
  letter-spacing: 0.06em;
  color: {dim};
  border-top: 1px solid {dim};
  padding-top: 0.35em;
  margin: 0.5em 0 0 0;
}}
div.details {{ font-size: 0.93em; }}
div.details p {{ margin: 0; }}
div.details table {{ border-collapse: collapse; margin: 0.4em 0; font-family: {mono}; font-size: 0.9em; }}
div.details th {{
  border-bottom: 2px solid {rule};
  padding: 0.25em 0.5em;
  text-align: left;
  font-size: 0.85em;
  letter-spacing: 0.05em;
  text-transform: uppercase;
}}
div.details td {{
  border-bottom: 1px solid {dim};
  padding: 0.25em 0.5em;
  text-align: left;
}}

/* Training Plan (right rail). */
h2.plan-heading {{
  font-family: {mono};
  font-size: 0.72em;
  font-weight: 700;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: {ink};
  border-bottom: 2px solid {rule};
  padding-bottom: 0.3em;
  margin: 0 0 0.6em 0;
}}
/* Stat strip → PRESS numerals: no fills, no radii — big 900 ink numerals
   over tiny tracked-caps dim labels, ruled above. */
/* Three narrow tiles on top, one wide tile beneath. NOT a 2x2: "This Week"
   carries two numbers and a separator ("29.3 mi / 29.5 mi"), which measured
   ~97pt inside a ~107pt half-rail tile whose neighbour started 10pt later —
   it rendered as `29.3 mi / 29.5 mi 0`, reading as one number. The three
   short values (a percentage, a day count, a slip count) share the top row
   comfortably; the compound value gets the full rail. */
table.stat-strip {{ width: 100%; border-collapse: collapse; margin: 0 0 0.6em 0; table-layout: fixed; }}
td.stat-tile {{ padding: 0.4em 0.3em 0.45em 0; text-align: left; border-bottom: 1px solid {dim}; }}
td.stat-wide {{ padding-right: 0; }}
td.stat-tile .value {{ font-size: 1.25em; font-weight: 900; letter-spacing: -0.02em; color: {ink}; }}
td.stat-tile .label {{
  margin-top: 0.15em;
  font-family: {mono};
  font-size: 0.58em;
  letter-spacing: 0.09em;
  text-transform: uppercase;
  color: {dim};
}}
div.today-callout {{
  border: 2px solid {rule};
  padding: 0.55em 0.7em;
  margin-bottom: 0.7em;
  page-break-inside: avoid;
  overflow-wrap: break-word;
}}
div.today-callout .eyebrow {{
  font-family: {mono};
  font-size: 0.62em;
  font-weight: 700;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: {ink};
  margin: 0 0 0.25em 0;
}}
div.today-callout .rx {{ font-family: {mono}; font-weight: 700; margin: 0 0 0.25em 0; }}
div.today-callout p.coaching-line {{
  font-family: {f["serif_stack"]};
  font-style: italic;
  margin: 0;
  color: {ink};
  font-size: 0.95em;
}}
/* table-layout: fixed — the mono data voice must never paint past the
   44% rail (measured 14pt of VERDICT-column overflow without it); dates
   render MM-DD (the year is noise in a 7-day window). Column widths sum
   to 100%: date/type/planned/actual/verdict. */
table.week-table {{ width: 100%; table-layout: fixed; border-collapse: collapse; font-family: {mono}; font-size: 0.74em; }}
/* Widths are budgeted from the LONGEST string each column can hold at the
   mono size, not split evenly. The old 19% type column could not hold
   "interval": measured 2026-07-22, `interval` and `5.0` came back from
   pdfplumber as the single word `interval5.0` — the columns were touching.
   Longest values: date "07-22" (5ch), type "interval" (8ch), mileage
   "4.0 mi" (6ch), verdict "scheduled" (9ch at 0.9em). Sums to 93%, leaving
   room for the four 0.3em cell gaps. */
table.week-table col.c-date {{ width: 14%; }}
table.week-table col.c-type {{ width: 22%; }}
table.week-table col.c-mi {{ width: 17%; }}
table.week-table col.c-verdict {{ width: 23%; }}
table.week-table th {{
  text-align: left;
  font-size: 0.68em;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: {ink};
  padding: 0 0.3em 0.3em 0;
  border-bottom: 2px solid {rule};
  overflow: hidden;
}}
table.week-table td {{
  padding: 0.3em 0.3em 0.3em 0;
  border-bottom: 1px solid {dim};
  color: {ink};
  overflow: hidden;
}}
table.week-table tr:last-child td {{ border-bottom: none; }}
/* Verdicts are typographic (PRESS-strict): done = ink, partial/scheduled =
   dim italic, rest = dim, MISSED = the one accent, caps. No pills. */
span.verdict {{ font-size: 0.9em; }}
span.verdict-done {{ color: {ink}; font-weight: 700; }}
span.verdict-partial {{ color: {dim}; font-style: italic; }}
span.verdict-missed {{ color: {accent}; font-weight: 900; text-transform: uppercase; letter-spacing: 0.04em; }}
span.verdict-compliant {{ color: {dim}; }}
span.verdict-pending {{ color: {dim}; font-style: italic; }}
"""


def _fmt_mi(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f} mi"


def _render_plan_section_html(plan_section: dict | None) -> str:
    """HTML for the Training Plan section, or "" when there's nothing to
    show. `plan_section` is built fresh from plans.py by tools.py at render
    time (never by this function, which does no DB access) — see the
    design doc's plan_section shape. The caller (generate_brief_report) is
    responsible for passing None when there's no active plan or no plan
    data at all for the trailing-7-day window; this function does not
    re-derive that decision."""
    if plan_section is None:
        return ""

    if plan_section.get("days_to_race") is not None:
        tile2_value = str(plan_section["days_to_race"])
        tile2_label = "Days to Race"
    else:
        tile2_value = html.escape(plan_section["goal_type"])
        tile2_label = "Goal"

    # Walk miles are named, never folded into the run total and never dropped
    # on the floor: the plan carries deliberate walk days, and a reader has to
    # be able to reconcile "run miles" against what the watch actually logged.
    # Kept terse: this label sits under a wide numeral in a ~220pt rail, and
    # the long form ("· plus 9.3 mi walked") wrapped onto a second line that
    # collided with the callout below it.
    walk_mi = plan_section.get("week_walk_mi") or 0
    walk_suffix = f" · +{walk_mi:.1f} walked" if walk_mi else ""

    # Rest-day-free adherence rides the tiny dim LABEL, never the numeral —
    # the same move as walk_suffix above, for the same reason: the 1.25em/900
    # .value slot in a ~third-rail tile is exactly where a compound value
    # collided with its neighbour once already. A label this long wraps to a
    # second line inside its own tile (there is a space to break at), costing
    # height the density ladder absorbs rather than width that would spill.
    # `is not None`, not truthy: 0% of sessions is the number that most needs
    # printing next to a flattering total.
    sessions_pct = plan_section.get("sessions_adherence_pct")
    adherence_suffix = f" · {sessions_pct}% sessions" if sessions_pct is not None else ""

    # 3-up + 1 wide, not 2x2: the three short values fit a third of the rail
    # each, while "This Week" is a compound value that overflowed its half-rail
    # tile and collided with the tile beside it (see the stat-strip CSS note).
    stat_strip = f"""
    <table class="stat-strip">
      <tr>
        <td class="stat-tile">
          <div class="value">{plan_section["adherence_pct"]}%</div>
          <div class="label">Adherence{adherence_suffix}</div>
        </td>
        <td class="stat-tile">
          <div class="value">{tile2_value}</div>
          <div class="label">{tile2_label}</div>
        </td>
        <td class="stat-tile">
          <div class="value">{plan_section["slips"]}</div>
          <div class="label">Slips</div>
        </td>
      </tr>
      <tr>
        <td class="stat-tile stat-wide" colspan="3">
          <div class="value">{_fmt_mi(plan_section["week_actual_mi"])} / {_fmt_mi(plan_section["week_planned_mi"])}</div>
          <div class="label">Run mi · actual / planned{walk_suffix}</div>
        </td>
      </tr>
    </table>
    """

    today = plan_section.get("today")
    today_html = ""
    if today is not None:
        rx = html.escape(today["type"])
        if today.get("distance_mi") is not None:
            rx += f" · {today['distance_mi']:.1f} mi"
        if today.get("pace_min_per_mi"):
            rx += f" @ {html.escape(today['pace_min_per_mi'])}/mi"
        description = (
            f'<p class="summary">{html.escape(today["description"])}</p>'
            if today.get("description") else ""
        )
        today_html = f"""
        <div class="today-callout">
          <p class="eyebrow">Today</p>
          <p class="rx">{rx}</p>
          {description}
          <p class="coaching-line">{html.escape(today["coaching_line"])}</p>
        </div>
        """

    rows = "".join(
        f"""
        <tr>
          <td>{html.escape(day["date"][5:])}</td>
          <td>{html.escape(day["type"])}</td>
          <td>{_fmt_mi(day.get("planned_mi"))}</td>
          <td>{_fmt_mi(day.get("actual_mi"))}</td>
          <td><span class="verdict verdict-{html.escape(day["verdict"])}">{_VERDICT_LABEL.get(day["verdict"], day["verdict"])}</span></td>
        </tr>
        """
        for day in plan_section.get("last_7_days", [])
    )
    table_html = (
        f"""
        <table class="week-table">
          <colgroup>
            <col class="c-date"><col class="c-type"><col class="c-mi">
            <col class="c-mi"><col class="c-verdict">
          </colgroup>
          <thead>
            <tr><th>Date</th><th>Type</th><th>Planned</th><th>Actual</th><th>Verdict</th></tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
        """
        if rows else ""
    )

    return f"""
    <section class="plan-section">
      <h2 class="plan-heading">Training Plan</h2>
      {stat_strip}
      {today_html}
      {table_html}
    </section>
    """


def _build_html(
    brief: Brief,
    charts: dict[str, bytes],
    plan_section: dict | None,
    density: dict | None = None,
    omitted: int = 0,
) -> str:
    """Assemble the full report HTML string. Separated from `render_brief_pdf`
    so layout/structure (e.g. which row gets `colspan="2"`) is testable via
    plain string assertions, without needing to introspect WeasyPrint's PDF
    layout output.

    `density` is a `DENSITY_PRESETS` entry (default: roomiest) — `fit_one_page`
    calls this repeatedly with denser presets until the document is one page.
    `omitted` is how many takeaways the caller dropped to make that happen; a
    non-zero value is printed on the page, never swallowed."""
    import markdown as md_lib

    cards: list[str] = []
    for index, takeaway in enumerate(brief.takeaways):
        headline = html.escape(takeaway.headline)
        summary = html.escape(takeaway.summary)
        # Only `details` goes through the markdown library's own escaping.
        # `headline`/`summary` are plain string-building with no templating
        # engine's autoescape underneath, so they're explicitly html.escape()d
        # above rather than inserted raw.
        details_html = md_lib.markdown(takeaway.details, extensions=["tables"])
        png_bytes = charts.get(str(index))
        chart_img = f'<img class="chart" src="{_data_uri(png_bytes)}" alt="chart">' if png_bytes else ""
        cards.append(f"""
        <section class="signal-card tone-{html.escape(takeaway.tone)}">
          <h2>{headline}</h2>
          <p class="summary">{summary}</p>
          {chart_img}
          <div class="details">{details_html}</div>
        </section>
        """)

    plan_html = _render_plan_section_html(plan_section)
    omitted_html = (
        f'<p class="omitted-note">{omitted} further '
        f'{"signal" if omitted == 1 else "signals"} omitted for space.</p>'
        if omitted > 0 else ""
    )

    # Cards that don't fit the left rail continue BELOW the Training Plan in
    # the right one rather than leaving that region empty — see
    # `cards_in_left_rail`. Reading order stays left-rail-down, then right.
    split = cards_in_left_rail(len(cards), bool(plan_html))
    signals_html = "".join(cards[:split])
    overflow_html = "".join(cards[split:])

    # No plan section → no second rail (no dangling empty column with a
    # divider rule); signals just take the full page width.
    body_html = (
        f'<table class="page-layout"><tr>'
        f'<td class="col-signals">{signals_html}{omitted_html}</td>'
        f'<td class="col-plan">{plan_html}{overflow_html}</td>'
        f'</tr></table>'
        if plan_html
        else f'<div class="col-signals col-signals-full">{signals_html}{omitted_html}</div>'
    )

    theme = branding.load_theme()
    ident = theme["identity"]
    eyebrow = f"{ident['brand_line']} · MORNING BRIEF · {brief.date}"
    return f"""<!doctype html>
<html>
<head><meta charset="utf-8"><style>{_build_css(theme, density)}</style></head>
<body>
  <div class="masthead">
    <table class="masthead-row"><tr>
      <td><span class="stamp">{html.escape(ident["stamp"])}</span><span class="eyebrow">{html.escape(eyebrow)}</span></td>
      <td class="byline">{html.escape(ident["byline"])}</td>
    </tr></table>
    <h1>{html.escape(brief.user_name)}'s Brief</h1>
  </div>
  {body_html}
</body>
</html>"""


def render_brief_pdf(
    brief: Brief,
    charts: dict[str, bytes],
    plan_section: dict | None = None,
    omitted: int = 0,
    presets: Sequence[dict] = DENSITY_PRESETS,
) -> tuple[bytes, int]:
    """Render a saved daily brief into a polished, single-page PDF report.

    `charts` is pre-rendered chart PNG bytes keyed by `str(index)` from
    `enumerate(brief.takeaways)` — NOT by metric name (two takeaways can cite
    the same metric) and NOT by a takeaway id (Takeaway has no id field).
    Takes rendered bytes as a parameter rather than fetching or rendering
    anything itself, so this function does no DB access and is testable with
    plain string-membership assertions.

    `plan_section` (optional, default None) adds a Training Plan section
    built fresh from plans.py by the caller at render time — see the design
    doc's plan_section shape. None omits the section entirely, preserving
    the exact pre-2026-07-09 output shape.

    `omitted` is how many takeaways the caller already dropped before calling;
    it is printed on the page. Returns `(pdf_bytes, page_count)` — a caller
    that gets 2 back has exhausted the density ladder and must drop content
    (see `generate_brief_report`), because nothing this function can do to
    type size will fit what it was handed.
    """
    return _fit_with_hint(
        lambda preset: _build_html(brief, charts, plan_section, preset, omitted),
        presets,
        # Keyed by the ladder actually in use: generate_brief_report passes a
        # TRUNCATED presets tuple when it retries after dropping a takeaway, and
        # a hint learned from a 1-rung ladder must not steer a full-ladder run.
        "brief" if presets is DENSITY_PRESETS else f"brief:{len(presets)}",
    )


# --- workout report card ---------------------------------------------------
# Same PRESS grammar as the brief report: flat paper, ink rules, no rounded
# corners / shadows / gradients, accent reserved for the failing grades.

def _report_card_css(theme: dict, density: dict | None = None) -> str:
    """Report-card-specific styles, appended to the shared `_build_css` sheet.

    The grade block is the whole point of the page, so it is set enormous in
    the display voice — the one place in either report where type does the
    work a colored badge would do elsewhere. Only D and F take the accent;
    everything at C or better stays ink, per the theme's rule that the accent
    means "look at this, it went wrong."

    Every table here is `table-layout: fixed` with an explicit colgroup at the
    markup level, for the reason the 0.24.1 overflow fix established: percentage
    widths plus cell padding must sum under 100% or WeasyPrint paints past the
    @page margin.
    """
    d = density or DENSITY_PRESETS[0]
    c = theme["colors"]
    f = theme["fonts"]
    dim, accent, rule = c["dim"], c["accent"], c["rule"]
    _, mono = _font_face_css(theme)
    return f"""
/* The hero is STACKED, not a two-column table (0.50.0). The old layout put a
   1-2 character letter in a 22% cell beside the coach read; a five-star row is
   ~5.8:1 and about 34% of the text width, so it overflowed straight through the
   HEART RATE paragraph. Stacking it and letting the read run full width is not
   a cosmetic preference — fewer line breaks per paragraph is what pays for the
   star row's height, which is why the density ladder barely moves (measured:
   15 of 16 live cards keep their rung). */
div.star-hero {{ margin: 0 0 0.9em 0; }}
div.star-hero .band {{
  border-bottom: 2px solid {rule};
  padding-bottom: 0.45em;
  margin-bottom: 0.5em;
}}
/* The ink rule under the band replaces the visual mass the giant letter used
   to supply. */
span.hero-score {{
  font-family: {mono};
  font-size: {d["hero_em"] * 0.42 * 0.62}em;
  font-weight: 700;
  letter-spacing: -0.02em;
  margin-left: 0.5em;
  vertical-align: 0.06em;
}}
span.hero-outof {{
  font-family: {mono};
  font-size: {d["hero_em"] * 0.42 * 0.30}em;
  color: {dim};
  letter-spacing: 0.04em;
}}
span.hero-meta {{
  font-family: {mono};
  font-size: 0.78em;
  letter-spacing: 0.06em;
  color: {dim};
  display: block;
  margin-top: 0.35em;
}}
p.hero-scale, p.hero-cap {{
  font-family: {f["serif_stack"]};
  font-style: italic;
  color: {dim};
  font-size: 0.8em;
  margin: 0.15em 0 0 0;
}}
p.reference-line {{
  font-family: {f["serif_stack"]};
  font-style: italic;
  color: {dim};
  margin: 0.2em 0 0 0;
  font-size: 0.95em;
  overflow-wrap: break-word;
}}
h2.card-heading {{
  font-size: 0.78em;
  font-weight: 800;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  border-bottom: 2px solid {rule};
  padding-bottom: 0.28em;
  margin: 1.5em 0 0.6em 0;
}}
table.metric-table, table.split-table {{
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
  font-family: {mono};
  font-size: 0.8em;
}}
table.metric-table th, table.split-table th {{
  text-align: left;
  font-size: 0.86em;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: {dim};
  border-bottom: 1px solid {rule};
  padding: 0.3em 0.35em;
}}
table.metric-table td, table.split-table td {{
  padding: 0.34em 0.35em;
  border-bottom: 1px solid rgba(110,103,92,0.28);
  overflow-wrap: break-word;
}}
td.metric-name {{ font-family: {f["display_stack"]}; font-weight: 800; }}
/* Left-aligned like every other column. The weight/size emphasis the letter
   needed is gone: the stars carry their own visual weight as drawn geometry,
   and bolding the numeral beside them just made the row noisy. */
td.metric-grade {{ text-align: left; white-space: nowrap; }}
svg.stars {{ vertical-align: -0.12em; }}
span.star-num {{
  font-family: {mono};
  font-size: 0.95em;
  color: {dim};
  margin-left: 0.45em;
  letter-spacing: 0.02em;
}}
/* The accent's whole job: the rows that went wrong. Scoped to STAR_CRITICAL
   (the old D/F territory) so a good day still carries zero orange — measured
   on the live 2026-08-02 card, whose four metrics all rate 5.00, the only
   accent left on the page is the masthead stamp. */
span.star-num.crit {{ color: {accent}; }}
span.stars-na {{ color: {dim}; font-style: italic; }}
tr.split-partial td {{ color: {dim}; font-style: italic; }}
p.no-splits, p.drift-line, p.load-context {{
  font-family: {f["serif_stack"]};
  font-style: italic;
  color: {dim};
  font-size: 0.9em;
  margin: 0.5em 0 0 0;
}}
ul.card-notes {{
  font-family: {f["serif_stack"]};
  font-style: italic;
  color: {dim};
  font-size: 0.9em;
  margin: 0.5em 0 0 0;
  padding-left: 1.1em;
}}
/* Deliberately not full-bleed: the per-lap HR bars are supporting evidence for
   the table above them, not the page's subject, and at 100% they out-shouted
   the grades.

   Capped by HEIGHT as well as width, for exactly the reason `img.chart` above
   is — and this rule shipped WITHOUT that cap, so `chart_h_pt` was never read
   by the report-card stylesheet at all and the density ladder could buy no
   vertical room here. Measured on activity 23825963527 (5 mi, 6 splits, HR
   trace): every rung laid out to 2 pages, the chart landing alone on page 2
   under nine inches of white, because a width-only cap lets the figure's own
   aspect ratio decide the page budget. `width: auto` keeps the aspect while
   `max-height` makes the cap real. */
img.split-chart {{
  max-height: {d["chart_h_pt"]}pt;
  width: auto;
  max-width: {d["split_chart_pct"]}%;
  margin-top: 0.7em;
}}
/* Four short paragraphs in the hero's meta cell, one per graded area. Smaller
   than the reference line it replaced, because there are now four of them and
   they have to sit beside the grade letter without pushing the tables down the
   page. Serif italic is the theme's commentary voice — this is the coach
   talking, not data. */
p.coach-read {{
  font-family: {f["serif_stack"]};
  font-style: italic;
  color: {c["ink"]};
  margin: 0.45em 0 0 0;
  font-size: 0.82em;
  line-height: 1.35;
}}
/* The metric label: structure voice, so the eye can jump straight to the
   paragraph it wants without reading all four. */
span.coach-label {{
  font-family: {f["display_stack"]};
  font-style: normal;
  font-weight: 800;
  font-size: 0.82em;
  letter-spacing: 0.06em;
  color: {dim};
  margin-right: 0.5em;
}}
"""


#: The card must state its own claim on every surface. A star row carries an
#: unavoidable review-score connotation, and for this card that MISLEADS:
#: Amazon and Yelp stars answer "how good was it", while this one answers "did
#: you do what the day prescribed". The 0.40.0 compliance/stimulus split exists
#: because conflating those two inverted the rubric outright, so a five-star
#: easy day is BY DESIGN a low-stimulus day and can legitimately be a slow
#: shuffle if slow was what was written.
_STAR_SCALE_NOTE = ("5 stars = you did what the day prescribed. It is a "
                    "compliance score, not a verdict on how good the run was.")

#: At or below this a row draws in the accent. The old D/F territory, and the
#: same boundary `ledger.BAD_CARD_MAX_STARS` and the calibration gate use.
STAR_CRITICAL = 2.0

#: A five-pointed star on a 20x20 box, drawn rather than typed. The brand mono
#: (IBM Plex Mono) carries no star glyph at all — verified against its cmap — so
#: a text star would render in whatever font the host machine happens to have.
#: Pango does fall back per-glyph, so one appears, but at a non-monospace
#: advance in a face that is not the brand's: an appearance that depends on the
#: machine. Geometry does not.
_STAR_PATH = ("M10 1.4 12.6 6.9 18.7 7.7 14.2 11.9 15.4 18 10 15.1 "
              "4.6 18 5.8 11.9 1.3 7.7 7.4 6.9Z")
#: Clip x for each eighth of fill, AREA-LINEARISED. Star ink is not uniform in
#: x — clipping the fill at 75% of the bounding box leaves 89.6% of the ink,
#: which is why the first 4.75 prototype was indistinguishable from a 5.00.
#: These are solved so that a clip at CUT[f] leaves exactly f of the polygon's
#: area. Precomputed on purpose: the renderer carries no geometry solver.
#: Do NOT "simplify" this to 20*f.
_STAR_CUTS = {0.125: 5.90, 0.25: 7.42, 0.375: 8.82, 0.5: 10.0,
              0.625: 11.18, 0.75: 12.58, 0.875: 14.10}
#: Sub-star resolution of the DRAWING. Finer than the quarter-star display
#: quantum so `display_stars`' output always lands on an exact cut.
_STAR_QUANTUM = 8
_STAR_BOX = 20
_STAR_GAP = 4


def star_row(score: float | None, *, uid: str, ink: str, em: float,
             max_stars: int = 5) -> str:
    """One inline ``<svg>`` holding a whole star row.

    ONE element with one id namespace, not five: WeasyPrint resolves
    ``url(#id)`` in DOCUMENT scope, so colliding clipPath ids let one row clip
    another. ``uid`` must be unique per row on the page.

    Inline ``<svg>`` is the mechanism, and the alternatives were tested rather
    than assumed: ``clipPath`` works in WeasyPrint 69 and renders an exact fill
    ramp, while ``<img src="data:image/svg+xml;base64,...">`` renders nothing at
    all — a blank cell.

    Every star is always OUTLINED and only the fill is clipped, so an unearned
    star reads as an ink rule rather than a tint. ``stroke-linejoin`` is a join,
    not a corner radius (PRESS forbids the latter); without it the 36-degree
    points spike at small sizes.
    """
    from . import report_card as rc

    if score is None:
        return f'<span class="stars-na">{html.escape(rc.STAR_NA_TEXT)}</span>'
    shown = rc.display_stars(score)
    width = max_stars * _STAR_BOX + (max_stars - 1) * _STAR_GAP
    defs, parts = [], []
    for i in range(max_stars):
        fill = min(1.0, max(0.0, shown - i))
        fill = round(fill * _STAR_QUANTUM) / _STAR_QUANTUM
        g = [f'<g transform="translate({i * (_STAR_BOX + _STAR_GAP)},0)">',
             f'<path d="{_STAR_PATH}" fill="none" stroke="{ink}" '
             f'stroke-width="1.5" stroke-linejoin="round"/>']
        if fill >= 1:
            g.append(f'<path d="{_STAR_PATH}" fill="{ink}"/>')
        elif fill > 0:
            cid = f"{uid}{i}"
            defs.append(f'<clipPath id="{cid}"><rect x="0" y="0" '
                        f'width="{_STAR_CUTS[fill]}" height="{_STAR_BOX}"/>'
                        f'</clipPath>')
            g.append(f'<path d="{_STAR_PATH}" fill="{ink}" '
                     f'clip-path="url(#{cid})"/>')
        parts.append("".join(g) + "</g>")
    defs_html = f"<defs>{''.join(defs)}</defs>" if defs else ""
    # aria-label because the stars are drawn paths with no text layer — without
    # it a PDF reader gets nothing at all from this cell.
    return (f'<svg class="stars" viewBox="0 0 {width} {_STAR_BOX}" '
            f'height="{em}em" width="{em * width / _STAR_BOX:.3f}em" '
            f'xmlns="http://www.w3.org/2000/svg" role="img" '
            f'aria-label="{score:.2f} out of {max_stars} stars">'
            f'{defs_html}{"".join(parts)}</svg>')


def star_cell(score: float | None, *, uid: str, theme: dict, em: float,
              legacy: str | None = None) -> str:
    """A rating cell: the star row plus its numeral.

    ``legacy`` renders a stored pre-0.50.0 letter unchanged when there is no
    score — a stored card is a historical record of what was actually shown and
    there is no backfill path by design.
    """
    from . import report_card as rc

    if score is None:
        if legacy and legacy != rc.STAR_NA_TEXT:
            return f'<span class="star-num">{html.escape(legacy)}</span>'
        return f'<span class="stars-na">{html.escape(rc.STAR_NA_TEXT)}</span>'
    crit = score <= STAR_CRITICAL
    ink = theme["colors"]["accent"] if crit else theme["colors"]["ink"]
    cls = " crit" if crit else ""
    return (star_row(score, uid=uid, ink=ink, em=em)
            + f'<span class="star-num{cls}">{score:.2f}</span>')


def hr_chart_series(card: dict) -> dict | None:
    """Pick which HR series the chart draws, and describe it.

    Prefers the per-sample trace (a bar per tenth of a mile, positioned on a
    real distance axis) and falls back to per-lap splits when no trace was
    resolved — an activity whose details Garmin never returned, or an offline
    render. Returns None when neither is available.

    Split out from ``render_split_hr_png`` so the choice, the axis positions
    and every label are assertable without rendering a PNG and reading pixels.
    """
    from . import report_card as rc

    trace = card.get("hr_trace") or []
    if trace:
        bin_mi = rc.HR_TRACE_BIN_MI
        # The pace overlay only exists when the trace carried a time channel.
        # Bins without one are None, which the renderer leaves as gaps rather
        # than interpolating a pace nobody ran.
        paced = [(r["start_mi"] + bin_mi / 2, r["pace_sec_per_mi"])
                 for r in trace if r.get("pace_sec_per_mi")]
        return {
            "source": "trace",
            # Bars sit at their true start distance, so the x-axis is miles —
            # not a bar ordinal that only looks like distance.
            "positions": [r["start_mi"] for r in trace],
            "values": [r["avg_hr"] for r in trace],
            "partials": [bool(r["partial"]) for r in trace],
            "width": bin_mi * 0.9,
            "xlabel": "Distance (miles)",
            # The label states the resolution actually binned, read from the
            # binner's own constant so it can never overstate it.
            "title": f"Heart rate and pace every {bin_mi:g} mi",
            "xmax": max(r["end_mi"] for r in trace),
            # Plotted at bin CENTRES: a bar spans its bucket, but a line point
            # is an instant, and hanging it off the bucket's left edge would
            # read half a bin early.
            "pace_x": [x for x, _ in paced],
            "pace_sec_per_mi": [p for _, p in paced],
        }

    rows = card.get("splits", {}).get("rows") or []
    if not rows:
        return None
    unit = card["splits"]["unit"]
    # Per-lap fallback: splits carry pace too, so the overlay survives here.
    paced = [(r["index"] - 0.5, r["avg_pace_sec_per_km"] * rc.MILE_M / 1000.0)
             for r in rows if r.get("avg_pace_sec_per_km")]
    return {
        "source": "splits",
        "positions": [r["index"] - 1 for r in rows],
        "values": [r["avg_hr"] or 0 for r in rows],
        "partials": [bool(r["partial"]) for r in rows],
        "width": 0.9,
        "xlabel": unit,
        "title": f"Heart rate and pace by {unit.lower()}",
        "xmax": len(rows),
        "pace_x": [x for x, _ in paced],
        "pace_sec_per_mi": [p for _, p in paced],
    }


def render_split_hr_png(card: dict) -> bytes:
    """HR bar chart — per tenth-mile from the sample trace when available, else
    per lap. Presentation only: nothing on the card is graded from either
    series. Partial intervals are drawn dim so a 90-meter fragment doesn't read
    as a real data point next to full ones."""
    import matplotlib

    matplotlib.use("Agg")
    import numpy as np
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    theme = branding.load_theme()
    paper, ink, dim, accent = (
        theme["colors"]["paper"], theme["colors"]["ink"],
        theme["colors"]["dim"], theme["colors"]["accent"])

    series = hr_chart_series(card)
    if series is None:
        raise ValueError("no HR series available to chart")
    values = series["values"]
    colors = [dim if p else ink for p in series["partials"]]

    # Short on purpose: at 2.4in the rendered image no longer fit in the space
    # left below the splits table and WeasyPrint orphaned it onto a second page
    # with a hand-sized gap above it. The coach read varies in length, so the
    # slack matters — this is sized to survive a long one.
    fig = Figure(figsize=(8, 1.8), dpi=150, facecolor=paper)
    FigureCanvasAgg(fig)  # explicit non-global canvas attach, never via pyplot
    ax = fig.add_subplot(111)
    ax.set_facecolor(paper)
    ax.bar(series["positions"], values, color=colors,
           width=series["width"], align="edge")
    ylo, yhi = value_axis_bounds([v for v in values if v] or [0, 1])
    ax.set_ylim(max(0, ylo), yhi)
    ax.set_xlim(0, series["xmax"])

    if series["source"] == "trace":
        # A tick per tenth is unreadable at this width; tick whole miles and
        # let the bars carry the resolution.
        ax.set_xticks(np.arange(0, np.floor(series["xmax"]) + 1, 1))
    else:
        ax.set_xticks([p + 0.5 for p in series["positions"]])
        ax.set_xticklabels([str(i + 1) for i in range(len(values))], fontsize=8)

    # The run's own average, so each bar reads as above or below the day's
    # effort instead of against a bare axis. Dim dashed rather than accent: it
    # is a reference, and the accent now belongs to the pace line, which is the
    # series the reader is meant to trace.
    avg_hr = card.get("activity", {}).get("avg_hr")
    if avg_hr:
        # A paper-colored backing box, because the label sits over the bars and
        # the reference line runs near the middle of the data by construction —
        # there is no corner of this chart guaranteed to be empty.
        ax.axhline(avg_hr, color=dim, linewidth=0.8, linestyle="--", zorder=3)
        ax.annotate(
            f"run avg {avg_hr} bpm", xy=(0.998, avg_hr),
            xycoords=("axes fraction", "data"), ha="right", va="bottom",
            fontsize=6.5, color=dim, zorder=5,
            bbox={"facecolor": paper, "edgecolor": "none", "pad": 1.5})

    ax.set_xlabel(series["xlabel"], fontsize=8, color=dim)
    ax.set_ylabel("Heart rate (bpm)", fontsize=8, color=dim)
    ax.set_title(series["title"], fontsize=9, color=ink, loc="left", pad=6)
    ax.tick_params(colors=dim, labelsize=8)
    ax.grid(axis="y", color=dim, linewidth=0.6, alpha=0.35)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(dim)

    _overlay_pace_axis(ax, series, accent, paper)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    return buf.getvalue()


def _fmt_pace_tick(seconds: float, _pos=None) -> str:
    """A pace-axis tick as m:ss. Minutes per mile is base 60, so a decimal tick
    ("9.5") names a different pace than the one it appears to."""
    seconds = max(0, int(round(seconds)))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _overlay_pace_axis(ax, series: dict, accent: str, paper: str) -> None:
    """Draw the pace line on a twinned right-hand axis.

    Two decisions worth stating, because both are easy to get backwards:

    - **The pace axis is inverted, so faster is UP.** Pace is seconds per mile,
      where a smaller number is the better run. On a natural axis a surge would
      dive toward the floor while the HR bars it caused rose beside it, and the
      two series would look like they disagreed. Inverted, effort and pace move
      together and a divergence means something.
    - **Accent, solid, drawn last.** The theme allows exactly one accent, and
      it belongs to the series the reader is meant to trace — which is why the
      HR average was demoted to a dim dashed reference when this arrived.

    Bins with no usable time are simply absent from the series, so the line has
    gaps rather than interpolated paces nobody ran.
    """
    xs, ys = series.get("pace_x") or [], series.get("pace_sec_per_mi") or []
    if len(xs) < 2:
        # One point is not a line, and a lone marker on a second axis costs
        # more explaining than it earns.
        return

    from matplotlib.ticker import FuncFormatter

    ax2 = ax.twinx()
    ax2.plot(xs, ys, color=accent, linewidth=1.3, zorder=4,
             solid_capstyle="round")
    ax2.invert_yaxis()
    ax2.set_ylabel("Pace (min/mi, faster ↑)", fontsize=8, color=accent)
    ax2.tick_params(axis="y", colors=accent, labelsize=7)
    ax2.yaxis.set_major_formatter(FuncFormatter(_fmt_pace_tick))
    ax2.grid(False)  # one set of horizontals; two is noise
    ax2.set_facecolor(paper)
    ax2.patch.set_alpha(0)
    for spine in ("top", "left", "bottom"):
        ax2.spines[spine].set_visible(False)
    ax2.spines["right"].set_color(accent)


def ref_mode_is_running(card: dict) -> bool:
    """Which locomotion pool this card was graded against, for the meta line."""
    return (card.get("reference") or {}).get("mode_label") == "running"


def _render_metric_table_html(card: dict, theme: dict, density: dict) -> str:
    from . import report_card as rc

    # Headers come from report_card.metric_table — the table was written out
    # twice before 0.48.0, which is the same divergence split_table and
    # stimulus_rows exist to prevent. The lead cells still come from there too;
    # only the RATING cell is rendered here, because the PDF draws geometry
    # where the markdown prints glyphs. Both read the same `stars` value and the
    # same `display_stars` quantum, so they cannot disagree about the rating —
    # only about how it is inked.
    headers, body_rows = rc.metric_table(card)
    metrics = card.get("metrics") or {}
    rows = ""
    for (key, _label), cells in zip(rc._METRIC_LABELS, body_rows, strict=True):
        *lead, legacy = cells
        label, rest = lead[0], lead[1:]
        tds = "".join(f"<td>{html.escape(c)}</td>" for c in rest)
        cell = star_cell(
            (metrics.get(key) or {}).get("stars"),
            uid=f"m{key}_", theme=theme, em=1.15, legacy=legacy)
        rows += f"""
        <tr>
          <td class="metric-name">{html.escape(label)}</td>
          {tds}
          <td class="metric-grade">{cell}</td>
        </tr>
        """
    # Widened from 12% to 22% for the rating column, taken from Metric and
    # Delta. A star row plus its numeral is ~9.4em against a letter's ~1.2em.
    # Actual/Expected stay at 19%: they carry strings like "15:29/mi best mile"
    # and already wrap. Three colgroups were swept and the ladder outcome was
    # identical for all of them, so this is a legibility call, not a fit one.
    return f"""
    <table class="metric-table">
      <colgroup>
        <col style="width:23%"><col style="width:19%"><col style="width:19%">
        <col style="width:17%"><col style="width:22%">
      </colgroup>
      <thead><tr>
        {"".join(f"<th>{html.escape(h)}</th>" for h in headers)}
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    """


def _render_stimulus_html(card: dict) -> str:
    """The Stimulus section — deliberately a two-column Signal/Value table with
    NO Grade column, so the page cannot imply a letter that does not exist.

    Returns "" when the activity has no load figure to report, rather than an
    empty section: the density ladder has no content to drop, so every section
    that renders has to earn its vertical space.
    """
    from . import report_card as rc

    if not rc.has_stimulus(card):
        return ""

    # Rows and notes come from report_card so the PDF and the markdown table can
    # never disagree — the same reason render_report_card_pdf takes an
    # already-built card instead of grading anything itself.
    rows = "".join(f"""
        <tr>
          <td class="metric-name">{html.escape(label)}</td>
          <td>{html.escape(value)}</td>
        </tr>
        """ for label, value in rc.stimulus_rows(card))
    notes_html = ('<ul class="card-notes">'
                  + "".join(f"<li>{html.escape(n)}</li>"
                            for n in rc.stimulus_notes(card))
                  + "</ul>")

    return f"""
  <section>
    <h2 class="card-heading">{html.escape(rc.stimulus_heading(card, markdown=False))}</h2>
    <table class="metric-table">
      <colgroup><col style="width:26%"><col style="width:74%"></colgroup>
      <thead><tr><th>Signal</th><th>Value</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    {notes_html}
  </section>
    """


def _render_splits_html(card: dict, split_chart: bytes | None) -> str:
    """The per-mile section, or the honest one-liner when the activity has no
    splits — which is the common case (~88% of the history is backfilled and
    carries none). Never a blank section."""
    splits = card["splits"]
    if not splits["available"]:
        # No per-lap table, but the sample trace may still exist — a backfilled
        # activity has no splits while Garmin still holds its details. Show the
        # chart rather than dropping the only HR detail the card has.
        chart_only = (
            f'<img class="split-chart" src="{_data_uri(split_chart)}" '
            f'alt="heart rate by distance">' if split_chart else ""
        )
        return f"""
        <section>
          <h2 class="card-heading">Heart rate</h2>
          <p class="no-splits">No per-mile splits recorded for this activity.
          Splits are captured only by the daily sync path — backfilled
          activities have none.</p>
          {chart_only}
        </section>
        """
    from . import report_card as rc

    unit = splits["unit"]
    # Headers and cells both come from report_card.split_table so this table and
    # the markdown one cannot disagree about which columns exist — the same
    # reason the stimulus rows are shared. It drops columns that are entirely
    # empty (Elev on every treadmill run; HR when the watch recorded none).
    headers, split_rows = rc.split_table(card)
    body = ""
    # strict=True on purpose: split_table returns exactly one cell-row per
    # split row, and a silent length mismatch would drop laps off the PDF while
    # the markdown card still showed them.
    for r, cells in zip(splits["rows"], split_rows, strict=True):
        tds = "".join(f"<td>{html.escape(c)}</td>" for c in cells)
        body += f"""
        <tr class="{'split-partial' if r['partial'] else ''}">{tds}</tr>
        """
    chart_img = (
        f'<img class="split-chart" src="{_data_uri(split_chart)}" alt="per-lap heart rate">'
        if split_chart else ""
    )
    drift = splits.get("hr_drift_pct")
    drift_html = (
        f'<p class="drift-line">HR drift, back half vs front half: {drift:+.1f}%.</p>'
        if drift is not None else ""
    )
    # Widths follow the surviving column count rather than a hardcoded five:
    # the label column keeps its 26% and the data columns share the rest, so
    # dropping Elev widens what remains instead of leaving a gap.
    rest = (100 - 26) / max(1, len(headers) - 1)
    cols = '<col style="width:26%">' + "".join(
        f'<col style="width:{rest:.0f}%">' for _ in headers[1:])
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    return f"""
    <section>
      <h2 class="card-heading">Per-{html.escape(unit.lower())} breakdown</h2>
      <table class="split-table">
        <colgroup>{cols}</colgroup>
        <thead><tr>{head}</tr></thead>
        <tbody>{body}</tbody>
      </table>
      {chart_img}
      {drift_html}
    </section>
    """


def _build_report_card_html(
    card: dict, split_chart: bytes | None = None, density: dict | None = None
) -> str:
    """Assemble the report-card HTML. Separated from `render_report_card_pdf`
    for the same reason `_build_html` is — layout is testable with plain string
    assertions instead of introspecting WeasyPrint's output.

    Every interpolated value is `html.escape`d: `activity_name` and the plan
    `description` are free text that reached the DB from Garmin and from the
    model respectively, and there is no templating engine's autoescape
    underneath these f-strings.
    """
    from . import report_card as rc
    from . import units as units_mod

    theme = branding.load_theme()
    ident = theme["identity"]
    act = card["activity"]
    name = act.get("activity_name") or act.get("activity_type") or "Workout"
    overall = card["overall"]
    eyebrow = f"{ident['brand_line']} · REPORT CARD · {act.get('date')}"

    # The yardstick no longer gets its own sentence, but the card must still
    # disclose it — the same run grades differently under a plan than under the
    # rolling median. It rides the meta line instead: "· easy (plan)".
    graded_by = "plan" if any(
        (m.get("reference") or "").startswith("plan")
        for m in card["metrics"].values()
    ) else "60d median"
    # The locomotion filter has to be disclosed — it is invisible in the numbers,
    # and a reader checking against Garmin's own app would see a different
    # median. It rides the meta line rather than the notes list because the
    # one-page budget is load-bearing here: as a bullet, this sentence plus the
    # split-graded-pace note pushed a 6-split card onto a second page.
    n_excluded = (card.get("reference") or {}).get("excluded_other_mode") or 0
    if n_excluded:
        other = "walk" if ref_mode_is_running(card) else "run"
        graded_by += f", {n_excluded} {other}s excluded"
    subtitle = (
        f"{rc._fmt_distance(act.get('distance_meters'))} in "
        f"{units_mod.format_duration(act.get('duration_seconds')) or '—'} · "
        f"{rc._fmt_pace(act.get('avg_pace_sec_per_km'))} · "
        f"{card.get('intent')} ({graded_by})"
    )

    notes = [f"{label}: {note}" for label, note in rc.metric_notes(card)]
    # The load spike note lives in the Stimulus section now (0.40.0) — it is a
    # statement about stimulus, not about compliance with the prescription.

    notes_html = (
        '<ul class="card-notes">'
        + "".join(f"<li>{html.escape(n)}</li>" for n in notes)
        + "</ul>"
    ) if notes else ""

    ctx = card.get("context") or {}
    ctx_html = (
        f'<p class="load-context">Fitness (CTL) {ctx["ctl"]:.0f} · '
        f'fatigue (ATL) {ctx["atl"]:.0f} · freshness (TSB) {ctx["tsb"]:+.0f} '
        f'on this date.</p>'
    ) if ctx.get("ctl") is not None else ""

    # Four short paragraphs under the hero band, one per graded area. They run
    # FULL WIDTH now (0.50.0) rather than sitting in a 74% cell beside the
    # score — see the `div.star-hero` comment in the stylesheet for why that is
    # load-bearing rather than cosmetic. The masthead title names the run and
    # carries nothing else. Omitted entirely when absent rather than left as an
    # empty block.
    from .report_card import READ_SECTIONS

    read = card.get("coach_read") or {}
    coach_html = "".join(
        f'<p class="coach-read"><span class="coach-label">'
        f'{html.escape(label)}</span>{html.escape(read[key])}</p>'
        for key, label in READ_SECTIONS if read.get(key)
    )

    hero_em = (density or CARD_DENSITY_PRESETS[0])["hero_em"] * 0.42
    score = overall.get("stars")
    if score is None and overall.get("grade"):
        # A card stored under the letter rubric. Render what it actually said;
        # see `report_card.metric_table` for why there is no backfill.
        hero_stars = ""
        hero_num = (f'<span class="hero-score">{html.escape(overall["grade"])}</span>')
    elif score is None:
        hero_stars = '<span class="stars-na">not rated</span>'
        hero_num = ""
    else:
        ink = (theme["colors"]["accent"] if score <= STAR_CRITICAL
               else theme["colors"]["ink"])
        hero_stars = star_row(score, uid="hero_", ink=ink, em=hero_em)
        hero_num = (f'<span class="hero-score" style="color:{ink}">'
                    f'{score:.2f}</span><span class="hero-outof"> / 5</span>')
    cap_html = ""
    if overall.get("capped"):
        cb = overall["capped_by"]
        # BOTH numbers, so the line reconciles by arithmetic. A bare "capped"
        # states a penalty without its cause, and the cause is the whole point.
        cap_html = (
            f'<p class="hero-cap">Held to {overall["stars"]:.2f} by '
            f'{html.escape(rc._METRIC_PROSE.get(cb["metric"], cb["metric"]))} '
            f'at {cb["stars"]:.2f} — the rows on their own average '
            f'{overall["mean_stars"]:.2f}.</p>')

    return f"""<!doctype html>
<html>
<head><meta charset="utf-8"><style>{_build_css(theme, density)}{_report_card_css(theme, density)}</style></head>
<body>
  <div class="masthead">
    <table class="masthead-row"><tr>
      <td><span class="stamp">{html.escape(ident["stamp"])}</span><span class="eyebrow">{html.escape(eyebrow)}</span></td>
      <td class="byline">{html.escape(ident["byline"])}</td>
    </tr></table>
    <h1>{html.escape(str(name))}</h1>
  </div>

  <div class="star-hero">
    <div class="band">
      {hero_stars}{hero_num}
      <span class="hero-meta">{html.escape(subtitle)}</span>
      <p class="hero-scale">{html.escape(_STAR_SCALE_NOTE)}</p>
      {cap_html}
    </div>
    {coach_html}
  </div>

  <section>
    <h2 class="card-heading">Compliance</h2>
    {_render_metric_table_html(card, theme, density or CARD_DENSITY_PRESETS[0])}
    {notes_html}
    {ctx_html}
  </section>

  {_render_stimulus_html(card)}

  {_render_splits_html(card, split_chart)}
</body>
</html>"""


def render_report_card_pdf(
    card: dict, split_chart: bytes | None = None
) -> tuple[bytes, int]:
    """Render a built report card into a single-page PDF.

    Takes the already-built card dict and pre-rendered chart bytes rather than
    querying or grading anything itself — the same separation `render_brief_pdf`
    keeps, so the table and the PDF can never report different grades.

    A card with 6+ splits plus the four-paragraph coach read used to overflow
    onto a second page (measured on activity 23685126977). It now shares the
    brief's density ladder: the card has no droppable content, so the ladder is
    the whole mechanism rather than a first stage.

    Returns `(pdf_bytes, page_count)` — mirroring `render_brief_pdf`. Unlike the
    brief there is nothing to drop, so a `page_count > 1` cannot be fixed here;
    it is surfaced to the caller (which logs it) rather than being discarded,
    because CLAUDE.md's contract is that a PDF never spills silently.
    """
    return _fit_with_hint(
        lambda preset: _build_report_card_html(card, split_chart, preset),
        CARD_DENSITY_PRESETS,
        "card",
    )
