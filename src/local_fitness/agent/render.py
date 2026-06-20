"""Deterministic markdown-table rendering + repair for agent output.

Two responsibilities, one source of truth:

1. ``render_table`` — build a clean, width-disciplined markdown table from
   headers + rows. Shared by the coach/brief snapshot rendering
   (``web/mcp_server._render_status``) and any future table output so tables
   look identical everywhere and are correct by construction.

2. ``fix_table_row_breaks`` — repair the one model failure mode the brief A/B
   surfaced (2026-06-20): at lower reasoning effort the composer occasionally
   drops the backslash on a ``\\n`` row break, emitting a literal ``n`` between
   the separator row and the first data row (``|---|---|n| RHR | ... |``),
   which collapses the table into one unrenderable line. The model authors the
   ``details`` prose freely, so we cannot render those tables in code wholesale;
   instead we repair this specific, unambiguous corruption at the save gate so
   every brief renders cleanly regardless of how the model was sampled.
"""
from __future__ import annotations

import re

__all__ = ["render_table", "fix_table_row_breaks"]


def render_table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a clean GitHub-flavored markdown table.

    Cells are coerced to ``str`` and pipes inside cell content are escaped so a
    stray ``|`` can't break the column structure. An empty ``rows`` yields just
    the header + separator (a valid, if empty, table).
    """
    def _cell(v: object) -> str:
        return str(v).replace("|", r"\|").strip()

    head = "| " + " | ".join(_cell(h) for h in headers) + " |"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    lines = [head, sep]
    for r in rows:
        lines.append("| " + " | ".join(_cell(c) for c in r) + " |")
    return "\n".join(lines)


# A markdown separator row: pipes around runs of dashes (with optional
# alignment colons / spaces). Its presence is how we know `text` contains a
# real table before attempting any repair — keeps the repair from touching
# ordinary prose.
_SEPARATOR_RE = re.compile(r"\|\s*:?-{2,}:?\s*\|")

# The corruption: a pipe, a bare `n` (no surrounding spaces — never a legitimate
# cell in this app's tables, which always carry values like "52 bpm"), a pipe.
# This is a `\n` row break whose backslash the model dropped.
_COLLAPSED_ROW_RE = re.compile(r"\|n\|")


def fix_table_row_breaks(text: str) -> str:
    """Repair literal-``n`` row-break corruption inside markdown tables.

    No-op unless ``text`` actually contains a markdown table (a separator row),
    so prose is never touched. Within a table, ``|n|`` is restored to a real
    row break (``|`` + newline + ``|``). Idempotent.
    """
    if not text or "|" not in text:
        return text
    if not _SEPARATOR_RE.search(text):
        return text
    return _COLLAPSED_ROW_RE.sub("|\n|", text)
