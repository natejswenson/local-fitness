"""Tests for the shared markdown-table renderer + the collapsed-row repair."""
from __future__ import annotations

from local_fitness.agent.render import fix_table_row_breaks, render_table


def test_render_table_basic():
    out = render_table(["Metric", "Value", "Read"], [["RHR", "52 bpm", "→ baseline"]])
    lines = out.split("\n")
    assert lines[0] == "| Metric | Value | Read |"
    assert lines[1] == "| --- | --- | --- |"
    assert lines[2] == "| RHR | 52 bpm | → baseline |"


def test_render_table_empty_rows_is_valid():
    out = render_table(["A", "B"], [])
    assert out == "| A | B |\n| --- | --- |"


def test_render_table_escapes_pipes_in_cells():
    out = render_table(["X"], [["a|b"]])
    # The pipe is escaped so markdown won't read it as a column delimiter.
    assert out.split("\n")[-1] == r"| a\|b |"


def test_fix_repairs_the_observed_defect():
    # The exact corruption captured from a low-effort brief: the separator row
    # glued to the first data row by a literal `n`.
    broken = (
        "Recovery read:\n"
        "| Metric | Value | vs Baseline | Trend |\n"
        "|--------|-------|-------------|-------|n| RHR | 52 bpm | +0% | → |\n"
        "| Sleep | 8h 31m | at baseline | ↑ |"
    )
    fixed = fix_table_row_breaks(broken)
    assert "|n|" not in fixed
    # The separator row and the first data row are now on their own lines.
    assert "|-------|\n| RHR | 52 bpm" in fixed
    # Row count: header, separator, RHR, Sleep = 4 table rows.
    assert sum(1 for ln in fixed.split("\n") if ln.startswith("|")) == 4


def test_fix_is_idempotent():
    broken = "| A | B |\n| --- | --- |n| 1 | 2 |"
    once = fix_table_row_breaks(broken)
    assert fix_table_row_breaks(once) == once


def test_fix_noop_on_prose_without_table():
    # No separator row → never touch the text, even if it contains `|n|`.
    prose = "the variable |n| is a placeholder in pseudo-code"
    assert fix_table_row_breaks(prose) == prose


def test_fix_noop_on_clean_table():
    clean = "| A | B |\n| --- | --- |\n| 1 | 2 |"
    assert fix_table_row_breaks(clean) == clean
