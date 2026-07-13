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

# The same dropped-backslash corruption at the END of a table: the final row's
# closing pipe followed by a bare `n` at end-of-line (`| ↓ |n\n`) — a `\n`
# whose backslash was lost after the table's last cell, not between rows.
_TRAILING_N_ROW_RE = re.compile(r"^(\|.*\|)n[ \t]*$", re.MULTILINE)

# A line that is part of a table block: starts and ends with a pipe.
_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")


def fix_table_row_breaks(text: str) -> str:
    """Repair markdown-table corruption and enforce renderable table blocks.

    No-op unless ``text`` actually contains a markdown table (a separator row),
    so prose is never touched. Three repairs, all idempotent:

    - ``|n|`` inside a table is restored to a real row break (a ``\\n`` whose
      backslash the model dropped).
    - A bare ``n`` dangling after a row's closing pipe at end-of-line
      (``| ... |n``) — the same dropped-backslash artifact at the table's
      edge — is stripped.
    - Table blocks are separated from surrounding prose by blank lines.
      Strict CommonMark/GFM renderers only parse a table that starts its own
      block; ``**Header:**\\n| Metric | ...`` renders as one flattened
      paragraph of pipes otherwise (seen live in the 2026-07-13 brief).
    """
    if not text or "|" not in text:
        return text
    if not _SEPARATOR_RE.search(text):
        return text
    text = _COLLAPSED_ROW_RE.sub("|\n|", text)
    text = _TRAILING_N_ROW_RE.sub(r"\1", text)

    # Blank-line isolation: walk lines, inserting a blank line at every
    # prose→table and table→prose boundary.
    lines = text.split("\n")
    out: list[str] = []
    prev_kind = "blank"  # blank | table | prose
    for line in lines:
        if not line.strip():
            kind = "blank"
        elif _TABLE_LINE_RE.match(line):
            kind = "table"
        else:
            kind = "prose"
        if (kind == "table" and prev_kind == "prose") or (
            kind == "prose" and prev_kind == "table"
        ):
            out.append("")
        out.append(line)
        prev_kind = kind
    return "\n".join(out)
