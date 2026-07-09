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
from typing import TYPE_CHECKING, Callable, Sequence

if TYPE_CHECKING:
    from .schemas import Brief

# Reused verbatim from the sibling `budget` project's validated PDF-report
# theme — brand consistency across the two "glossy" reporting surfaces takes
# priority over consistency with the plain-text ASCII chart tool's own heat
# ramp. Single source of truth for both the WeasyPrint report CSS below and
# the matplotlib chart stylesheet in render_chart_png.
PRIMARY = "#2a78d6"
GOOD = "#0ca30c"
WARNING = "#fab219"
CRITICAL = "#d03b3b"
NEUTRAL = "#e1e0d9"

_TONE_COLOR = {
    "positive": GOOD,
    "caution": WARNING,
    "critical": CRITICAL,
    "neutral": PRIMARY,
}

# Serializes every chart/PDF render within this process. Closes the "is
# WeasyPrint's write_pdf() safe to call concurrently from two threads"
# question by construction, the same way it removes any need to reason about
# whether the underlying MCP Server could ever have two tool calls in flight
# within one stdio session — renders simply never overlap regardless.
RENDER_LOCK = asyncio.Lock()


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

    dates = [d for d, _ in series]
    values = [v for _, v in series]
    labels = [d[5:] for d in dates]  # MM-DD, matching the ASCII chart tool

    fig = Figure(figsize=(8, 4.5), dpi=150, facecolor="white")
    FigureCanvasAgg(fig)  # explicit non-global canvas attach, never via pyplot
    ax = fig.add_subplot(111)
    ax.set_facecolor("white")
    x = np.arange(len(labels))

    if chart_type == "line":
        ax.plot(x, values, color=PRIMARY, linewidth=2)
        ax.fill_between(x, values, color=PRIMARY, alpha=0.08)
    elif chart_type == "bar":
        ax.bar(x, values, color=PRIMARY)
    elif chart_type == "combo":
        # Matches agent/charts.py's render_combo_chart: bars + a
        # least-squares trend line of the SAME single metric — one series,
        # one axis, not a second-metric dual-axis chart (see design doc).
        ax.bar(x, values, color=PRIMARY, alpha=0.55)
        if len(values) >= 2:
            coeffs = np.polyfit(x, values, 1)
            ax.plot(x, np.poly1d(coeffs)(x), color=CRITICAL, linewidth=2)
    else:
        raise ValueError(f"unsupported chart_type '{chart_type}'")

    ax.grid(axis="y", color=NEUTRAL, linewidth=0.6, alpha=0.7)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.spines["left"].set_color(NEUTRAL)
    ax.spines["bottom"].set_color(NEUTRAL)
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


_CSS = f"""
body {{
  font-family: -apple-system, "Helvetica Neue", Arial, sans-serif;
  color: #1a1a1a;
  margin: 2em;
}}
h1 {{ font-size: 1.4em; margin-bottom: 0.2em; }}
h1 .date {{ color: #666; font-weight: normal; font-size: 0.7em; }}
section.takeaway {{
  border-left: 4px solid {PRIMARY};
  padding: 0.6em 1em;
  margin: 1em 0;
  page-break-inside: avoid;
}}
section.tone-positive {{ border-left-color: {GOOD}; }}
section.tone-caution {{ border-left-color: {WARNING}; }}
section.tone-critical {{ border-left-color: {CRITICAL}; }}
section.tone-neutral {{ border-left-color: {PRIMARY}; }}
h2 {{ font-size: 1.05em; margin: 0 0 0.2em 0; }}
p.summary {{ color: #444; margin: 0 0 0.6em 0; }}
img.chart {{ max-width: 100%; margin: 0.4em 0; }}
div.details {{ font-size: 0.92em; }}
div.details table {{ border-collapse: collapse; margin: 0.6em 0; }}
div.details th, div.details td {{
  border: 1px solid {NEUTRAL};
  padding: 0.3em 0.6em;
  text-align: left;
}}
"""


def render_brief_pdf(brief: "Brief", charts: dict[str, bytes]) -> bytes:
    """Render a saved daily brief into a polished PDF report.

    `charts` is pre-rendered chart PNG bytes keyed by `str(index)` from
    `enumerate(brief.takeaways)` — NOT by metric name (two takeaways can cite
    the same metric) and NOT by a takeaway id (Takeaway has no id field).
    Takes rendered bytes as a parameter rather than fetching or rendering
    anything itself, so this function does no DB access and is testable with
    plain string-membership assertions.
    """
    import markdown as md_lib
    import weasyprint

    sections: list[str] = []
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
        sections.append(f"""
        <section class="takeaway tone-{html.escape(takeaway.tone)}">
          <h2>{headline}</h2>
          <p class="summary">{summary}</p>
          {chart_img}
          <div class="details">{details_html}</div>
        </section>
        """)

    doc = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><style>{_CSS}</style></head>
<body>
  <h1>{html.escape(brief.user_name)}'s Brief <span class="date">{html.escape(brief.date)}</span></h1>
  {"".join(sections)}
</body>
</html>"""

    return weasyprint.HTML(string=doc, url_fetcher=_report_url_fetcher()).write_pdf()
