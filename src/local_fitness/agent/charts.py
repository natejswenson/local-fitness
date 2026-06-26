"""Deterministic terminal-chart rendering for the ``chart`` MCP tool.

Three pure renderers, no DB access — unit-testable in isolation (mirrors how
``render.py`` keeps table rendering pure). A 2026-06-25 prototype against the
real terminal established the one constraint these encode: **ANSI color escapes
are stripped on the way to the display** (tool text → markdown render), so the
only color that survives is emoji/Unicode *glyphs*. That forces a split:

- ``render_bar_chart`` — horizontal bars built from colored square emoji. Color
  survives, but emoji are double-width and cannot be overlaid, so no trend line.
- ``render_combo_chart`` — a 2D canvas of vertical bars with a regression trend
  line overlaid. Monochrome (thin box-drawing glyphs align; emoji wouldn't), but
  it carries a y-axis and handles negative series (TSB / freshness).
- ``render_sparkline`` — a one-line block-glyph mini chart for dense windows.
- ``render_calendar`` — a week-stacked heat-grid (one colored square per day).
  The default for the tool: it stays compact for any window (a 90-day range is
  ~13 rows), so it renders fully instead of getting truncated the way a
  one-row-per-day bar chart does once the window grows past a couple weeks.

Callers pass a ``value_fmt`` callable so unit formatting (seconds→hours, etc.)
stays in the tool layer; the renderers only deal with floats.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, timedelta

__all__ = [
    "render_bar_chart", "render_combo_chart", "render_sparkline", "render_calendar",
    "render_line",
]

# Low→high "heat" ramp. Neutral magnitude, NOT good/bad — a metric where high is
# good (sleep) and one where high is bad (RHR) both read as "more = warmer".
_HEAT = ("🟦", "🟩", "🟨", "🟧", "🟥")
_BLOCKS = "▁▂▃▄▅▆▇█"
# Full-width (Unicode "Wide") space: invisible, but the same display width as an
# emoji square, so a 2D emoji canvas stays aligned without a visible background.
_GAP = "　"

_NO_DATA = "(no data in window)"


def _heat(t: float) -> str:
    """Map t in [0,1] to one of the five heat squares."""
    idx = min(len(_HEAT) - 1, max(0, int(t * len(_HEAT))))
    return _HEAT[idx]


def _norm(values: Sequence[float]) -> tuple[float, float, float]:
    """Return (lo, hi, span) with span never zero (flat series → span 1)."""
    lo, hi = min(values), max(values)
    return lo, hi, (hi - lo) or 1.0


def _trend(values: Sequence[float]) -> list[float]:
    """Least-squares fit, returned as one fitted y per x. Flat for n<2."""
    n = len(values)
    if n < 2:
        return list(values)
    xs = range(n)
    x_mean = (n - 1) / 2
    y_mean = sum(values) / n
    denom = sum((x - x_mean) ** 2 for x in xs) or 1e-9
    slope = sum((x - x_mean) * (values[x] - y_mean) for x in xs) / denom
    return [y_mean + slope * (x - x_mean) for x in xs]


def _slope(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    fit = _trend(values)
    return (fit[-1] - fit[0]) / (n - 1)


def render_bar_chart(
    labels: Sequence[str],
    values: Sequence[float],
    *,
    value_fmt: Callable[[float], str] = lambda v: f"{v:g}",
    width: int = 20,
    title: str | None = None,
) -> str:
    """Horizontal emoji-color bars, one row per point.

    Bar length is zero-based when every value is ≥ 0 (length ∝ v / max, so a
    zero reads as an empty bar — honest for steps / intensity minutes); for a
    series that dips negative it falls back to min-based scaling across the
    window. Color is always the point's *relative* magnitude in the window.
    """
    if not values:
        return f"{title}\n{_NO_DATA}" if title else _NO_DATA
    lo, hi, span = _norm(values)
    flat = hi == lo  # constant series has no range to scale across
    zero_based = lo >= 0
    denom = hi if (zero_based and hi > 0) else span
    label_w = max((len(s) for s in labels), default=0)
    lines = [title] if title else []
    for lab, v in zip(labels, values):
        # A flat (all-equal) series fills every bar regardless of sign — matches
        # how render_combo_chart plants a flat series at full height. Without this
        # a flat *negative* series ((v-lo)/span == 0) would render empty bars
        # (the TSB / freshness shape) while a flat positive one renders full ones.
        # Exception: an all-zero non-negative window (a rest-day stretch) honors
        # the zero-based "zero = empty bar" contract and renders empty.
        if flat:
            frac = 0.0 if (zero_based and hi == 0) else 1.0
        else:
            frac = (v / denom) if zero_based else ((v - lo) / span)
        n = max(0, round(frac * width))
        rel = (v - lo) / span
        bar = _heat(rel) * n
        lines.append(f"{lab:<{label_w}} {bar} {value_fmt(v)}")
    return "\n".join(lines)


def render_combo_chart(
    labels: Sequence[str],
    values: Sequence[float],
    *,
    value_fmt: Callable[[float], str] = lambda v: f"{v:g}",
    height: int = 9,
    title: str | None = None,
) -> str:
    """2D vertical bars (``█``) with a least-squares trend line (``•``) overlaid.

    Monochrome by necessity (see module docstring). The y-axis is labeled with
    real values, so the bars are scaled across the data range — negative series
    (TSB) render correctly because the axis, not zero, anchors the scale. The
    trend marker wins any cell it shares with a bar so the line stays visible.
    """
    if not values:
        return f"{title}\n{_NO_DATA}" if title else _NO_DATA
    lo, hi = min(values), max(values)
    n = len(values)
    height = max(2, height)
    flat = hi == lo  # a constant series has no range to scale across

    def row_of(v: float) -> int:
        # Flat series: there is no real range, so plant every bar at full height.
        # This keeps █ visible (the bar fills rows 0..height-2; the trend marker
        # only claims the top row) instead of collapsing to a single overwritten
        # cell at row 0.
        if flat:
            return height - 1
        return max(0, min(height - 1, round((v - lo) / (hi - lo) * (height - 1))))

    grid = [[" "] * n for _ in range(height)]
    for x, v in enumerate(values):
        for y in range(row_of(v) + 1):
            grid[y][x] = "█"
    for x, tv in enumerate(_trend(values)):
        grid[row_of(tv)][x] = "•"

    # y-axis labels keyed by row → real value. A flat series has no range to
    # label, so we print the single constant value once (mid-axis) rather than
    # fabricating a lo / lo+½ / lo+1 spread that the data never spans. A real
    # range labels top / middle / bottom.
    if flat:
        axis_vals = {height // 2: lo}
    else:
        axis_vals = {y: lo + (hi - lo) * y / (height - 1) for y in (height - 1, height // 2, 0)}
    axis_labels = {y: value_fmt(v) for y, v in axis_vals.items()}
    # Width the axis column off every label actually printed (not just lo / hi),
    # so a fractional midpoint can't shove the ┤ column out of line.
    axis_w = max((len(s) for s in axis_labels.values()), default=0)
    lines = [title] if title else []
    for y in range(height - 1, -1, -1):
        label = f"{axis_labels[y]:>{axis_w}}" if y in axis_labels else " " * axis_w
        lines.append(f"{label} ┤{''.join(grid[y])}")
    lines.append(f"{' ' * axis_w} └{'─' * n}")
    # Report the trend as its fitted endpoints over the window, formatted with the
    # same value_fmt as the axis. This keeps the footer unit-consistent (rhr reads
    # "55 → 53", sleep "7.5h → 7.6h") instead of printing a raw-unit per-step slope
    # against a formatted axis.
    fit = _trend(values)
    slope = _slope(values)
    arrow = "rising" if slope > 0 else "falling" if slope < 0 else "flat"
    # The on-canvas trend marker is clamped to the drawn rows, so clamp the
    # reported endpoints to the data range too — otherwise an extrapolating
    # least-squares line prints values the axis never shows and the • never
    # reaches. Direction still comes from the unclamped slope above.
    start = min(hi, max(lo, fit[0]))
    end = min(hi, max(lo, fit[-1]))
    lines.append(f"{' ' * axis_w}  trend {value_fmt(start)} → {value_fmt(end)} · {arrow}")
    return "\n".join(lines)


def render_sparkline(values: Sequence[float]) -> str:
    """One-line block-glyph sparkline. Empty series → the no-data marker."""
    if not values:
        return _NO_DATA
    lo, _, span = _norm(values)
    return "".join(_BLOCKS[min(7, round((v - lo) / span * 7))] for v in values)


def render_calendar(
    dates: Sequence[str],
    values: Sequence[float],
    *,
    value_fmt: Callable[[float], str] = lambda v: f"{v:g}",
    title: str | None = None,
    cumulative: bool = False,
) -> str:
    """Week-stacked calendar heat-grid: one colored square per day, weeks stacked
    top→bottom, Mon→Sun left→right, color by the day's magnitude in the window.

    This is the compact answer to "show me N days of a metric": a 60-day window
    is ~9 rows and a 90-day window ~13, so the whole timeframe renders at once
    instead of scrolling off / getting truncated the way one-row-per-day bars do.

    ``dates`` are ISO ``YYYY-MM-DD`` strings aligned with ``values`` (the series
    the DB returned, which skips null days). Days inside the window with no value
    render as ⬜. The right-hand weekly aggregate is a sum when ``cumulative``
    (steps / intensity minutes) and the mean of present days otherwise (rhr, tsb…).
    """
    if not values:
        return f"{title}\n{_NO_DATA}" if title else _NO_DATA
    by_date = {date.fromisoformat(d): v for d, v in zip(dates, values)}
    lo, hi, span = _norm(values)
    start, end = min(by_date), max(by_date)

    # Every grid cell is a single emoji (heat / ⬜ / ⬛) so columns line up — an
    # emoji renders wider than an ASCII char in most terminals, so mixing the two
    # (a "· " pad, or an "M T W…" weekday header) breaks alignment. We drop the
    # ASCII weekday header entirely and spell the Mon→Sun convention in the legend.
    lines = [title] if title else []
    agg_kind = "sum" if cumulative else "avg"
    lines.append(
        f"🟦 {value_fmt(lo)} (low) → 🟥 {value_fmt(hi)} (high)   "
        f"⬜ no data · ⬛ outside · rows = weeks (Mon→Sun) · right = wk {agg_kind}"
    )
    week_start = start - timedelta(days=start.weekday())  # Monday on/before start
    while week_start <= end:
        cells, present = [], []
        for i in range(7):
            d = week_start + timedelta(days=i)
            if d < start or d > end:
                cells.append("⬛")            # outside the window (emoji-width pad)
            elif d in by_date:
                v = by_date[d]
                cells.append(_heat((v - lo) / span))
                present.append(v)
            else:
                cells.append("⬜")            # in-window day with no data
        agg = ""
        if present:
            wk = sum(present) if cumulative else sum(present) / len(present)
            agg = value_fmt(wk)
        lines.append(f"{week_start.strftime('%b %d'):<8}{''.join(cells)}   {agg}")
        week_start += timedelta(days=7)
    return "\n".join(lines)


def _weekly_means(dates: Sequence[str], values: Sequence[float]):
    """Collapse a daily series to one mean per ISO week (Mon-anchored), preserving
    order. Same weekly notion as render_calendar's right-hand column, so the line
    and calendar styles tell a consistent story. Labels are each week's Monday."""
    order, buckets = [], {}
    for d, v in zip(dates, values):
        mon = date.fromisoformat(d) - timedelta(days=date.fromisoformat(d).weekday())
        if mon not in buckets:
            buckets[mon] = []
            order.append(mon)
        buckets[mon].append(v)
    labels = [m.strftime("%b %d") for m in order]
    vals = [sum(buckets[m]) / len(buckets[m]) for m in order]
    return labels, vals


def render_line(
    dates: Sequence[str],
    values: Sequence[float],
    *,
    value_fmt: Callable[[float], str] = lambda v: f"{v:g}",
    title: str | None = None,
    height: int = 9,
    weekly_after: int = 35,
) -> str:
    """A *colored* line chart: a connected value-path drawn in heat-colored emoji
    squares on an invisible full-width-space canvas (so it reads as a line, not a
    grid), with a y-axis and a baseline. Color and height both encode the value.

    A thin one-cell hairline isn't possible here — color needs double-width emoji,
    which can't be a 1-cell glyph — so the line is one emoji thick. Windows longer
    than ``weekly_after`` days collapse to one point per ISO week so the whole span
    stays visible (a daily 90-point line would be ~180 cells wide and wrap); short
    windows plot one point per day. ``dates`` are ISO ``YYYY-MM-DD`` strings."""
    if not values:
        return f"{title}\n{_NO_DATA}" if title else _NO_DATA
    if len(values) > weekly_after:
        labels, values = _weekly_means(dates, values)
    else:
        labels = [d[5:] for d in dates]
    lo, hi, span = _norm(values)
    H = max(3, height)
    W = len(values)

    def row_of(v: float) -> int:
        return round((v - lo) / span * (H - 1))

    grid = [[_GAP] * W for _ in range(H)]
    prev = None
    for x, v in enumerate(values):
        r = row_of(v)
        color = _heat((v - lo) / span)
        if prev is not None:                       # fill the riser between points
            for rr in range(min(prev, r), max(prev, r) + 1):
                grid[rr][x] = color
        grid[r][x] = color
        prev = r

    axis_w = max(len(value_fmt(lo)), len(value_fmt(hi)))
    lines = [title] if title else []
    lines.append(f"🟦 {value_fmt(lo)} (low) → 🟥 {value_fmt(hi)} (high)")
    for y in range(H - 1, -1, -1):
        if y in (H - 1, H // 2, 0):
            label = f"{value_fmt(lo + span * y / (H - 1)):>{axis_w}}"
        else:
            label = " " * axis_w
        lines.append(f"{label} │{''.join(grid[y])}")
    lines.append(f"{' ' * axis_w} └{'──' * W}")
    lines.append(f"{' ' * axis_w}  {labels[0]} → {labels[-1]}")
    return "\n".join(lines)
