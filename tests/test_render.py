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


def test_fix_strips_trailing_n_after_last_row():
    # The 2026-07-13 live defect: the dropped-backslash `\n` artifact landing
    # AFTER the table's final closing pipe (`| ↓ |n`), not between rows.
    broken = (
        "**Snapshot:**\n"
        "| Metric | Today |\n"
        "|---|---|\n"
        "| Stress | 30 | ↓ |n\n"
        "Plan calls for an easy run."
    )
    fixed = fix_table_row_breaks(broken)
    assert "|n" not in fixed
    assert "| Stress | 30 | ↓ |" in fixed.split("\n")


def test_fix_inserts_blank_lines_around_table_block():
    # Strict CommonMark/GFM renderers flatten a table glued to prose into one
    # paragraph of pipes — the exact failure seen in the 2026-07-13 brief.
    broken = (
        "**Snapshot — today vs baseline:**\n"
        "| Metric | Today |\n"
        "|---|---|\n"
        "| RHR | 55 bpm |\n"
        "Plan calls for an easy run."
    )
    fixed = fix_table_row_breaks(broken)
    lines = fixed.split("\n")
    assert lines[0] == "**Snapshot — today vs baseline:**"
    assert lines[1] == ""  # blank line before the table block
    assert lines[2] == "| Metric | Today |"
    assert lines[5] == ""  # blank line after the table block
    assert lines[6] == "Plan calls for an easy run."


def test_fix_blank_line_isolation_is_idempotent_and_preserves_clean_spacing():
    already_clean = (
        "**Snapshot:**\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\nProse after."
    )
    assert fix_table_row_breaks(already_clean) == already_clean
    glued = "**Snapshot:**\n| A | B |\n|---|---|\n| 1 | 2 |\nProse after."
    once = fix_table_row_breaks(glued)
    assert fix_table_row_breaks(once) == once


def test_fix_combined_live_shape_end_to_end():
    # All three defects together, mirroring the stored 2026-07-13 takeaway
    # (values fabricated): glued header, trailing |n artifact, glued prose.
    broken = (
        "**Snapshot — today vs baseline:**\n"
        "| Metric | Today | Baseline | Δ | Trend |\n"
        "|---|---|---|---|---|\n"
        "| RHR | 55 bpm | 51 bpm | +7.8% | ↑ |\n"
        "| Avg Stress | 25 | 31.0 | -19.4% | ↓ |n\n"
        "Plan calls for Easy 4mi."
    )
    fixed = fix_table_row_breaks(broken)
    lines = fixed.split("\n")
    assert "" in lines  # blank lines exist
    table_lines = [ln for ln in lines if ln.startswith("|")]
    assert len(table_lines) == 4
    assert table_lines[-1].endswith("| ↓ |")
    # prose is separated from the table by a blank line on both sides
    i_first = lines.index(table_lines[0])
    i_last = lines.index(table_lines[-1])
    assert lines[i_first - 1] == ""
    assert lines[i_last + 1] == ""
