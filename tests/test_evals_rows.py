"""Row-level scoring of repeated groups in typed_fields: `scorers.row_prf` and the `row_prf`
dimension `score()` adds when a case declares `expected.typed_rows`.

A bank statement's transactions, an RFQ's line items and a contract's clause list come back as
one typed field holding a list of objects. `field_prf` compares that list as one string, so a
single dropped row scores the whole field wrong and a reordered list scores it wrong too. These
tests pin the row semantics instead: order-free matching, exact-row precision and recall,
cell accuracy over the columns the case labeled, and numeric values compared as numbers.
"""

from __future__ import annotations

from openreading.evals import row_prf, score

ROWS = [
    {"date": "2026-01-03", "description": "DOORDASH", "amount": -42.18},
    {"date": "2026-01-05", "description": "PAYROLL ACME", "amount": 2500.0},
    {"date": "2026-01-09", "description": "CHECK #1043", "amount": -310.0},
]


def _resp(rows):
    return {"typed_fields": {"transactions": {"value": rows}}}


def test_identical_rows_score_one():
    s = row_prf({"transactions": ROWS}, {"transactions": ROWS})
    assert s["f1"] == 1.0 and s["precision"] == 1.0 and s["recall"] == 1.0
    assert s["cell_accuracy"] == 1.0
    assert s["rows_expected"] == 3.0 and s["rows_got"] == 3.0 and s["rows_exact"] == 3.0


def test_order_does_not_matter():
    s = row_prf({"transactions": list(reversed(ROWS))}, {"transactions": ROWS})
    assert s["f1"] == 1.0


def test_dropped_row_lowers_recall_only():
    s = row_prf({"transactions": ROWS[:2]}, {"transactions": ROWS})
    assert s["precision"] == 1.0
    assert abs(s["recall"] - 2 / 3) < 1e-9


def test_invented_row_lowers_precision_only():
    extra = {"date": "2026-01-10", "description": "INVENTED", "amount": -1.0}
    s = row_prf({"transactions": ROWS + [extra]}, {"transactions": ROWS})
    assert s["recall"] == 1.0
    assert abs(s["precision"] - 3 / 4) < 1e-9


def test_one_wrong_cell_fails_the_row_but_keeps_cell_credit():
    got = [dict(ROWS[0], amount=42.18), ROWS[1], ROWS[2]]  # sign error on one row
    s = row_prf({"transactions": got}, {"transactions": ROWS})
    assert s["rows_exact"] == 2.0
    assert abs(s["cell_accuracy"] - 8 / 9) < 1e-9


def test_numbers_compare_as_numbers():
    got = [
        {"date": "2026-01-03", "description": "doordash", "amount": "-$42.18"},
        {"date": "2026-01-05", "description": "PAYROLL  ACME", "amount": "$2,500.00"},
        {"date": "2026-01-09", "description": "CHECK #1043", "amount": "(310.00)"},
    ]
    s = row_prf({"transactions": got}, {"transactions": ROWS})
    assert s["f1"] == 1.0


def test_columns_the_case_did_not_label_are_ignored():
    got = [dict(r, category="food") for r in ROWS]
    assert row_prf({"transactions": got}, {"transactions": ROWS})["f1"] == 1.0


def test_missing_or_non_list_field_recalls_nothing():
    assert row_prf({}, {"transactions": ROWS})["recall"] == 0.0
    assert row_prf({"transactions": "3 rows"}, {"transactions": ROWS})["recall"] == 0.0


def test_empty_expected_list_is_satisfied_only_by_an_empty_list():
    assert row_prf({"transactions": []}, {"transactions": []})["f1"] == 1.0
    assert row_prf({"transactions": ROWS}, {"transactions": []})["precision"] == 0.0


def test_rows_pool_across_fields():
    lines = [{"line": "1", "part_number": "BRKT-1042", "quantities": [10, 100]}]
    got = {
        "transactions": ROWS,
        "line_items": [{"line": "1", "part_number": "BRKT-1042", "quantities": [10, 100]}],
    }
    s = row_prf(got, {"transactions": ROWS, "line_items": lines})
    assert s["rows_expected"] == 4.0 and s["f1"] == 1.0


def test_list_cells_compare_element_wise():
    exp = [{"part_number": "BRKT-1042", "quantities": [10, 100, 1000]}]
    ok = [{"part_number": "BRKT-1042", "quantities": ["10", "100", "1,000"]}]
    bad = [{"part_number": "BRKT-1042", "quantities": [10, 1000]}]
    assert row_prf({"l": ok}, {"l": exp})["f1"] == 1.0
    assert row_prf({"l": bad}, {"l": exp})["f1"] == 0.0


def test_matching_prefers_the_best_pair():
    # A greedy first-come match would pair got[0] with ROWS[0] at 2/3 and strand the exact one.
    near = dict(ROWS[0], amount=-1.0)
    s = row_prf({"transactions": [near, ROWS[0]]}, {"transactions": [ROWS[0]]})
    assert s["rows_exact"] == 1.0


def test_score_adds_row_prf_only_when_declared():
    with_rows = score(_resp(ROWS), {"typed_rows": {"transactions": ROWS}})
    assert with_rows["dimensions"]["row_prf"]["f1"] == 1.0
    assert with_rows["overall"] == 1.0
    without = score(_resp(ROWS), {"text_contains": []})
    assert "row_prf" not in without["dimensions"]


def test_row_f1_is_the_headline_in_overall():
    s = score(_resp(ROWS[:2]), {"typed_rows": {"transactions": ROWS}})
    assert abs(s["overall"] - s["dimensions"]["row_prf"]["f1"]) < 1e-12


def test_row_fields_are_not_also_scored_as_fields():
    # A response that returns the whole schema carries the row field too. It is scored once, as
    # rows, so it must not lower field precision as an unlabeled extra key.
    resp = {
        "typed_fields": {"closing_balance": {"value": 12001.15}, "transactions": {"value": ROWS}}
    }
    s = score(
        resp, {"typed_fields": {"closing_balance": 12001.15}, "typed_rows": {"transactions": ROWS}}
    )
    assert s["dimensions"]["field_prf"]["precision"] == 1.0
    assert s["overall"] == 1.0


def test_field_prf_compares_objects_structurally():
    from openreading.evals import field_prf

    period = {"start": "2026-01-01", "end": "2026-01-31"}
    reordered = {"end": "2026-01-31", "start": "2026-01-01"}
    assert field_prf({"statement_period": reordered}, {"statement_period": period})["f1"] == 1.0
    wrong = {"start": "2026-01-01", "end": "2026-01-30"}
    assert field_prf({"statement_period": wrong}, {"statement_period": period})["f1"] == 0.0


def test_field_prf_scalars_keep_exact_string_semantics():
    from openreading.evals import field_prf

    # Unchanged on purpose: existing scalar scores do not move.
    assert field_prf({"total": "$1,234.50"}, {"total": 1234.5})["f1"] == 0.0
