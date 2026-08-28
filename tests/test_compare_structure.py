"""Structure deltas for the superpowered `--format diffs`: how subjects PACKAGE the same content.
`table_deltas` pairs tables by index across subjects and reports each one's grid shape + which
subjects flattened it (the pulse `(1,17)` vs reducto `(4,5)` signal). See deltas.py for the
content (packaging-immune) axis; this is the structure axis (DESIGN §9)."""

from __future__ import annotations

from openreading.comparison.ingest import load_subjects
from openreading.comparison.structure import table_deltas
from tests.fakes import make_envelope


def _tbl(rows: list[list[str]], text: str = "grid") -> dict:
    return {"type": "table", "text": text, "table": {"rows": rows}}


def test_table_deltas_pairs_by_index_and_reports_shapes_and_flat():
    # a flattens the table onto ONE row of 6 cells; b reconstructs a 3x2 grid.
    a = make_envelope("a", pages=[[_tbl([["Date", "Amount", "05/01", "100", "05/02", "200"]])]])
    b = make_envelope("b", pages=[[_tbl([["Date", "Amount"], ["05/01", "100"], ["05/02", "200"]])]])
    td = table_deltas(load_subjects([a, b]))
    t0 = td["tables"][0]
    assert t0["shapes"]["a"] == (1, 6)
    assert t0["shapes"]["b"] == (3, 2)
    assert "a" in t0["flat"] and "b" not in t0["flat"]  # a is the flattened one


def test_table_deltas_label_prefers_descriptive_first_cell():
    a = make_envelope("a", pages=[[_tbl([["Account Summary - 7421664389", "05/01", "x"]])]])
    b = make_envelope("b", pages=[[_tbl([["05/01"], ["x"]])]])
    td = table_deltas(load_subjects([a, b]))
    assert "Account Summary" in td["tables"][0]["label"]


def test_table_deltas_reports_page_number():
    a = make_envelope("a", pages=[[], [_tbl([["x", "y", "z"]])]])  # table on page 2
    b = make_envelope("b", pages=[[], [_tbl([["x"], ["y"], ["z"]])]])
    td = table_deltas(load_subjects([a, b]))
    assert td["tables"][0]["page"] == 2


def test_table_deltas_handles_unequal_table_counts():
    a = make_envelope("a", pages=[[_tbl([["x", "y"]]), _tbl([["z", "w"]])]])  # 2 tables
    b = make_envelope("b", pages=[[_tbl([["x"], ["y"]])]])  # 1 table
    td = table_deltas(load_subjects([a, b]))
    assert td["counts"] == {"a": 2, "b": 1}
    assert set(td["tables"][1]["shapes"]) == {"a"}  # 2nd table only a has it


def test_table_deltas_empty_when_no_tables():
    a = make_envelope("a", text="no tables here")
    b = make_envelope("b", text="none here either")
    td = table_deltas(load_subjects([a, b]))
    assert td["tables"] == []


def test_render_diffs_shows_content_verdict_tables_and_granularity():
    from openreading.comparison.render import render_diffs
    from openreading.comparison.report import build_report

    # same cells, different reconstruction: a flattens to (1,6), b builds (3,2).
    a = make_envelope(
        "a",
        text="Date Amount 05/01 100 05/02 200",
        pages=[[_tbl([["Date", "Amount", "05/01", "100", "05/02", "200"]])]],
    )
    b = make_envelope(
        "b",
        text="Date\nAmount\n05/01\n100\n05/02\n200",
        pages=[[_tbl([["Date", "Amount"], ["05/01", "100"], ["05/02", "200"]])]],
    )
    subjects = load_subjects([a, b])
    out = render_diffs(subjects, build_report(subjects))
    assert "CONTENT" in out and "EQUIVALENT" in out  # same tokens → content equivalent
    assert "TABLES" in out and "1×6" in out and "3×2" in out  # shapes shown
    assert "flat" in out.lower()  # a's flattening is called out
    assert "GRANULARITY" in out
