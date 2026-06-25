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

Callers pass a ``value_fmt`` callable so unit formatting (seconds→hours, etc.)
stays in the tool layer; the renderers only deal with floats.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence

__all__ = ["render_bar_chart", "render_combo_chart", "render_sparkline"]

# Low→high "heat" ramp. Neutral magnitude, NOT good/bad — a metric where high is
# good (sleep) and one where high is bad (RHR) both read as "more = warmer".
_HEAT = ("🟦", "🟩", "🟨", "🟧", "🟥")
_BLOCKS = "▁▂▃▄▅▆▇█"

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
    zero_based = lo >= 0
    denom = hi if (zero_based and hi > 0) else span
    label_w = max((len(s) for s in labels), default=0)
    lines = [title] if title else []
    for lab, v in zip(labels, values):
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
    unit: str = "",
) -> str:
    """2D vertical bars (``█``) with a least-squares trend line (``•``) overlaid.

    Monochrome by necessity (see module docstring). The y-axis is labeled with
    real values, so the bars are scaled across the data range — negative series
    (TSB) render correctly because the axis, not zero, anchors the scale. The
    trend marker wins any cell it shares with a bar so the line stays visible.
    """
    if not values:
        return f"{title}\n{_NO_DATA}" if title else _NO_DATA
    lo, hi, span = _norm(values)
    n = len(values)
    height = max(2, height)

    def row_of(v: float) -> int:
        return max(0, min(height - 1, round((v - lo) / span * (height - 1))))

    grid = [[" "] * n for _ in range(height)]
    for x, v in enumerate(values):
        for y in range(row_of(v) + 1):
            grid[y][x] = "█"
    for x, tv in enumerate(_trend(values)):
        grid[row_of(tv)][x] = "•"

    # y-axis labels: top, middle, bottom carry real values; the rest align blank.
    axis_w = max(len(value_fmt(lo)), len(value_fmt(hi)))
    lines = [title] if title else []
    for y in range(height - 1, -1, -1):
        if y in (height - 1, height // 2, 0):
            val = lo + span * y / (height - 1)
            label = f"{value_fmt(val):>{axis_w}}"
        else:
            label = " " * axis_w
        lines.append(f"{label} ┤{''.join(grid[y])}")
    lines.append(f"{' ' * axis_w} └{'─' * n}")
    slope = _slope(values)
    arrow = "rising" if slope > 0 else "falling" if slope < 0 else "flat"
    lines.append(f"{' ' * axis_w}  trend {slope:+.2f}{unit}/step · {arrow}")
    return "\n".join(lines)


def render_sparkline(values: Sequence[float]) -> str:
    """One-line block-glyph sparkline. Empty series → the no-data marker."""
    if not values:
        return _NO_DATA
    lo, _, span = _norm(values)
    return "".join(_BLOCKS[min(7, round((v - lo) / span * 7))] for v in values)
