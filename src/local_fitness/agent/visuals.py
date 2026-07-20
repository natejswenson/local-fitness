"""Rendering internals shared by the `generate_brief_report` and
`generate_chart` MCP tools (agent/tools.py, LOCAL_ONLY_TOOLS).

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
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Sequence

if TYPE_CHECKING:
    from .schemas import Brief

# All colors/fonts/identity come from the brand theme (agent/branding.py):
# the PRESS editorial system by default (warm paper, ink rules, one accent),
# local-overridable via LOCAL_FITNESS_BRAND_FILE. The theme is the single
# source of truth for both the WeasyPrint report CSS and the matplotlib
# chart stylesheet in render_chart_png — the ASCII chart tool keeps its own
# emoji heat ramp (PRESS is the *print* brand).
from . import branding

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


def render_chart_png(
    series: Sequence[tuple[str, float]],
    chart_type: str,
    value_fmt: Callable[[float], str],
) -> bytes:
    """Render a styled standalone chart PNG for one metric series.

    `series` is a list of (iso_date, value) pairs, already fetched and
    validated by the caller (agent/tools.py's `_fetch_metric_series`) — this
    function never queries the DB or recomputes anything, only formats what
    it's given. `chart_type` is one of "line" | "bar" | "combo" (v1 scope;
    see the design doc for why calendar/spark are excluded).

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
    from matplotlib.ticker import FuncFormatter

    theme = branding.load_theme()
    paper = theme["colors"]["paper"]
    ink = theme["colors"]["ink"]
    dim = theme["colors"]["dim"]
    accent = theme["colors"]["accent"]

    dates = [d for d, _ in series]
    values = [v for _, v in series]
    labels = [d[5:] for d in dates]  # MM-DD, matching the ASCII chart tool

    fig = Figure(figsize=(8, 4.5), dpi=150, facecolor=paper)
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
    ax.tick_params(colors=ink)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _pos: value_fmt(v)))

    # Thin x labels for long windows so they stay readable, same idea as the
    # ASCII tool's weekly-averaging for long line charts.
    step = max(1, len(labels) // 10) if len(labels) > 14 else 1
    ax.set_xticks(x[::step])
    ax.set_xticklabels(labels[::step], rotation=45, ha="right", fontsize=8)

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


def _build_css(theme: dict) -> str:
    """The report stylesheet, built from the brand theme.

    PRESS grammar (the default theme's rules, kept even under color
    overrides): flat paper canvas, ink rules for all structure, NO rounded
    corners / shadows / gradients, sans 800–900 tight-tracked for
    structure, serif italic for commentary, mono for data. Tones and
    verdicts are typographic — the accent appears only on critical/missed
    (and the stamp)."""
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
  font-size: 11.3pt;
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
table.page-layout {{ width: 100%; table-layout: fixed; border-collapse: collapse; }}
table.page-layout > tr > td {{ vertical-align: top; padding: 0; }}
td.col-signals {{ width: 56%; padding-right: 0.9em; }}
td.col-plan {{ width: 44%; padding-left: 0.9em; border-left: 2px solid {rule}; }}
/* No active plan: signals run the full page width, no second rail. */
div.col-signals-full {{ width: 100%; }}

/* Signal cards → ruled editorial sections: structure from ink rules and
   whitespace, never fills or radii. Headline = display voice (900, tight
   tracking); summary = serif-italic standfirst in dim. Tone is
   typographic — only critical earns the accent. */
section.signal-card {{
  border-top: 2px solid {rule};
  padding: 0.55em 0 0.35em 0;
  margin-bottom: 0.75em;
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
img.chart {{ max-width: 100%; margin: 0.3em 0; }}
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
table.stat-strip {{ width: 100%; border-collapse: collapse; margin: 0 0 0.6em 0; table-layout: fixed; }}
td.stat-tile {{ padding: 0.4em 0.3em 0.45em 0; text-align: left; border-bottom: 1px solid {dim}; }}
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
table.week-table {{ width: 100%; border-collapse: collapse; font-family: {mono}; font-size: 0.8em; }}
table.week-table th {{
  text-align: left;
  font-size: 0.68em;
  letter-spacing: 0.09em;
  text-transform: uppercase;
  color: {ink};
  padding: 0 0.4em 0.3em 0;
  border-bottom: 2px solid {rule};
}}
table.week-table td {{
  padding: 0.3em 0.4em 0.3em 0;
  border-bottom: 1px solid {dim};
  color: {ink};
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

    # 2x2, not a 4-across strip: the right rail is roughly 44% of page
    # width now (2-column page layout), too narrow for 4 tiles side by
    # side to hold "11.0 mi / 16.0 mi" without wrapping badly.
    stat_strip = f"""
    <table class="stat-strip">
      <tr>
        <td class="stat-tile">
          <div class="value">{plan_section["adherence_pct"]}%</div>
          <div class="label">Adherence</div>
        </td>
        <td class="stat-tile">
          <div class="value">{tile2_value}</div>
          <div class="label">{tile2_label}</div>
        </td>
      </tr>
      <tr>
        <td class="stat-tile">
          <div class="value">{_fmt_mi(plan_section["week_actual_mi"])} / {_fmt_mi(plan_section["week_planned_mi"])}</div>
          <div class="label">This Week</div>
        </td>
        <td class="stat-tile">
          <div class="value">{plan_section["slips"]}</div>
          <div class="label">Slips</div>
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
          <td>{html.escape(day["date"])}</td>
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


def _build_html(brief: "Brief", charts: dict[str, bytes], plan_section: dict | None) -> str:
    """Assemble the full report HTML string. Separated from `render_brief_pdf`
    so layout/structure (e.g. which row gets `colspan="2"`) is testable via
    plain string assertions, without needing to introspect WeasyPrint's PDF
    layout output."""
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

    # Stacked single-column, not paired 2-up: the 2-column split now
    # happens at the whole-page level (signals rail vs. plan rail — see
    # the `table.page-layout` CSS comment), so each card gets the left
    # rail's full width rather than half of it again.
    signals_html = "".join(cards)
    plan_html = _render_plan_section_html(plan_section)

    # No plan section → no second rail (no dangling empty column with a
    # divider rule); signals just take the full page width.
    body_html = (
        f'<table class="page-layout"><tr>'
        f'<td class="col-signals">{signals_html}</td>'
        f'<td class="col-plan">{plan_html}</td>'
        f'</tr></table>'
        if plan_html
        else f'<div class="col-signals col-signals-full">{signals_html}</div>'
    )

    theme = branding.load_theme()
    ident = theme["identity"]
    eyebrow = f"{ident['brand_line']} · MORNING BRIEF · {brief.date}"
    return f"""<!doctype html>
<html>
<head><meta charset="utf-8"><style>{_build_css(theme)}</style></head>
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
    brief: "Brief", charts: dict[str, bytes], plan_section: dict | None = None
) -> bytes:
    """Render a saved daily brief into a polished PDF report.

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
    """
    import weasyprint

    doc = _build_html(brief, charts, plan_section)
    return weasyprint.HTML(string=doc, url_fetcher=_report_url_fetcher()).write_pdf()
