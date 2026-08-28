"""derive — edge/defensive branches (empty grids, malformed input, reconstruction paths)."""

from __future__ import annotations

from openreading.derive import (
    cells_to_grid,
    html_table_to_table,
    html_to_text,
    md_table_to_table,
    md_to_blocks,
    md_to_text,
    table_to_pipe_md,
    table_to_text,
)
from openreading.types.blocks import Table, TableCell


def test_cells_to_grid_empty_input():
    t = cells_to_grid([])
    assert t.n_rows == 0 and t.n_cols == 0 and t.cells == [] and t.rows == []


def test_table_projections_reconstruct_rows_from_cells_when_rows_none():
    t = Table(
        n_rows=1,
        n_cols=2,
        cells=[TableCell(row=0, col=0, text="a"), TableCell(row=0, col=1, text="b")],
        rows=None,
    )
    assert table_to_text(t) == "a\tb"
    assert table_to_pipe_md(t).splitlines()[-1] == "| a | b |"


def test_table_to_pipe_md_empty_table_is_empty_string():
    assert table_to_pipe_md(Table(n_rows=0, n_cols=0, cells=[], rows=[])) == ""


def test_html_table_invalid_span_treated_as_one_and_no_table_is_none():
    t = html_table_to_table("<table><tr><td rowspan='oops'>a</td><td>b</td></tr></table>")
    assert t is not None and t.rows == [["a", "b"]]
    assert html_table_to_table("<div>no table here</div>") is None


def test_md_table_without_separator_row_is_none():
    assert md_table_to_table("| a | b |\n| c | d |") is None  # no --- separator


def test_md_to_text_pipe_line_without_separator_is_prose():
    assert md_to_text("a | b without a separator row") == "a | b without a separator row"


def test_html_to_text_br_is_newline():
    assert html_to_text("line1<br>line2") == "line1\nline2"


def test_md_to_blocks_unclosed_fence_still_emits_code():
    blocks = md_to_blocks("```\nx = 1\ny = 2")
    code = [b for b in blocks if b.type.value == "code"]
    assert code and code[0].text == "x = 1\ny = 2"


def test_md_to_blocks_pipe_lines_without_separator_become_text():
    blocks = md_to_blocks("a | b\nc | d")
    assert blocks and all(b.type.value == "text" for b in blocks)
