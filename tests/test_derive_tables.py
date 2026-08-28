"""derive.tables — the ONE occupancy-cursor grid + its projections (DESIGN §5). Merged cells get
true grid coordinates; `Table.rows` fills the span origin and leaves covered positions None."""

from __future__ import annotations

from openreading.derive import (
    GridCell,
    cells_to_grid,
    html_table_to_table,
    md_table_to_table,
    table_to_pipe_md,
    table_to_text,
)


def test_cells_to_grid_simple_2x2_explicit_coords():
    cells = [
        GridCell(0, 0, text="A", is_header=True),
        GridCell(0, 1, text="B", is_header=True),
        GridCell(1, 0, text="1"),
        GridCell(1, 1, text="2"),
    ]
    t = cells_to_grid(cells)
    assert t.n_rows == 2 and t.n_cols == 2
    assert t.rows == [["A", "B"], ["1", "2"]]
    assert [(c.row, c.col) for c in t.cells] == [(0, 0), (0, 1), (1, 0), (1, 1)]
    assert t.cells[0].is_header is True and t.cells[2].is_header is False


def test_cells_to_grid_colspan_fills_origin_and_leaves_hole():
    # a header cell spanning 2 columns, then a normal 2-cell row
    cells = [
        GridCell(0, 0, col_span=2, text="Merged Header", is_header=True),
        GridCell(1, 0, text="left"),
        GridCell(1, 1, text="right"),
    ]
    t = cells_to_grid(cells)
    assert t.n_rows == 2 and t.n_cols == 2
    assert t.rows == [["Merged Header", None], ["left", "right"]]  # covered position is None
    hdr = t.cells[0]
    assert (hdr.row, hdr.col, hdr.col_span) == (0, 0, 2)


def test_cells_to_grid_rowspan_cursor_assigns_columns_when_col_none():
    # provider order, no explicit columns (col=None) — the occupancy cursor must skip the position
    # occupied by the rowspan from above (the P9 enumeration-shift bug this fixes).
    cells = [
        GridCell(0, None, row_span=2, text="tall"),  # occupies (0,0) and (1,0)
        GridCell(0, None, text="r0c1"),
        GridCell(1, None, text="r1c1"),  # must land at col 1, not col 0 (occupied by 'tall')
    ]
    t = cells_to_grid(cells)
    assert t.n_rows == 2 and t.n_cols == 2
    assert t.rows == [["tall", "r0c1"], [None, "r1c1"]]  # (1,0) covered by rowspan


def test_table_to_text_rows_as_lines_tab_joined():
    t = cells_to_grid(
        [
            GridCell(0, 0, text="Region", is_header=True),
            GridCell(0, 1, text="Revenue", is_header=True),
            GridCell(1, 0, text="North"),
            GridCell(1, 1, text="4400"),
        ]
    )
    assert table_to_text(t) == "Region\tRevenue\nNorth\t4400"


def test_table_to_pipe_md_escapes_and_has_header_row():
    t = cells_to_grid(
        [
            GridCell(0, 0, text="a|b", is_header=True),  # pipe must be escaped
            GridCell(0, 1, text="c\nd", is_header=True),  # newline -> <br>
            GridCell(1, 0, text="1"),
            GridCell(1, 1, text="2"),
        ]
    )
    md = table_to_pipe_md(t)
    lines = md.splitlines()
    assert lines[0] == r"| a\|b | c<br>d |"
    assert set(lines[1].replace(" ", "")) <= set("|-")  # separator row
    assert lines[2] == "| 1 | 2 |"


def test_table_to_pipe_md_blank_header_when_no_is_header():
    t = cells_to_grid([GridCell(0, 0, text="only"), GridCell(0, 1, text="row")])
    lines = table_to_pipe_md(t).splitlines()
    # GFM requires a header row; with no is_header cell we emit a blank one, data below
    assert lines[0].strip("| ") == "" or lines[0] == "|  |  |"
    assert lines[-1] == "| only | row |"


def test_html_table_to_table_rowspan_colspan_and_headers():
    html = (
        "<table><tr><th>H1</th><th colspan='2'>H23</th></tr>"
        "<tr><td rowspan='2'>tall</td><td>b</td><td>c</td></tr>"
        "<tr><td>e</td><td>f</td></tr></table>"
    )
    t = html_table_to_table(html)
    assert t is not None
    assert t.n_rows == 3 and t.n_cols == 3
    assert t.rows[0] == ["H1", "H23", None]  # colspan hole
    assert t.rows[1] == ["tall", "b", "c"]
    assert t.rows[2] == [None, "e", "f"]  # rowspan hole under 'tall'
    assert t.cells[0].is_header is True


def test_md_table_to_table_separator_marks_header():
    md = "| Name | Age |\n| --- | --- |\n| Alice | 30 |\n| Bob | 25 |"
    t = md_table_to_table(md)
    assert t is not None
    assert t.n_rows == 3 and t.n_cols == 2
    assert t.rows[0] == ["Name", "Age"]
    assert t.rows[2] == ["Bob", "25"]
    assert all(c.is_header for c in t.cells if c.row == 0)
    assert not any(c.is_header for c in t.cells if c.row > 0)


def test_md_table_to_table_none_when_not_a_table():
    assert md_table_to_table("just a plain paragraph, no pipes") is None
