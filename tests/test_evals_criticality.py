"""Criticality-weighted scoring: the `criticality` block `score(..., schema=)` adds.

A schema labels fields with `x-read-criticality`, and the block reports `weighted_prf` and
`critical_pass` beside the unweighted dimensions. These tests pin the rules in the
`openreading.evals.scorers` docstring. The degeneracy tests prove law SX5: with every weight at
1.0 the weighted metric is the unweighted one, bit for bit, so a weighted headline cannot hide an
unweighted regression.
"""

from __future__ import annotations

import pytest

from openreading.evals import field_prf, row_prf, score
from openreading.evals.scorers import (
    CRITICALITY_WEIGHTS_V1,
    CRITICALITY_WEIGHTS_VERSION,
    weighted_prf,
)

ONES = {"critical": 1.0, "expected": 1.0, "optional": 1.0, "ignore": 1.0}

ROWS = [
    {"date": "2026-01-03", "description": "DOORDASH", "amount": -42.18, "category": "food"},
    {"date": "2026-01-05", "description": "PAYROLL ACME", "amount": 2500.0, "category": "pay"},
]


def _resp(fields: dict) -> dict:
    return {"typed_fields": {k: {"value": v} for k, v in fields.items()}}


STATEMENT = {
    "type": "object",
    "properties": {
        "account_holder": {"type": "string", "x-read-criticality": "critical"},
        "closing_balance": {"type": "number", "x-read-criticality": "critical"},
        "bank_name": {"type": "string", "x-read-criticality": "optional"},
        "statement_period": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "x-read-criticality": "critical"},
                "end": {"type": "string"},
            },
        },
        "transactions": {
            "type": "array",
            "x-read-criticality": "critical",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "description": {"type": "string"},
                    "amount": {"type": "number"},
                    "category": {"type": "string", "x-read-criticality": "ignore"},
                },
            },
        },
    },
}


def test_weights_v1_table():
    assert dict(CRITICALITY_WEIGHTS_V1) == {
        "critical": 1.0,
        "expected": 0.3,
        "optional": 0.1,
        "ignore": 0.0,
    }
    assert CRITICALITY_WEIGHTS_VERSION == "v1"


# ------------------------------------------------------------------ when the block appears
def test_no_schema_reports_no_block():
    s = score(_resp({"a": 1}), {"typed_fields": {"a": 1}})
    assert "criticality" not in s


def test_unlabelled_schema_reports_no_block():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    s = score(_resp({"a": 1}), {"typed_fields": {"a": 1}}, schema=schema)
    assert "criticality" not in s


def test_block_shape_and_untouched_overall():
    expected = {"typed_fields": {"account_holder": "Jane", "bank_name": "X"}}
    resp = _resp({"account_holder": "Jane", "bank_name": "Y"})
    plain = score(resp, expected)
    s = score(resp, expected, schema=STATEMENT)
    assert s["overall"] == plain["overall"]
    assert s["dimensions"] == plain["dimensions"]
    block = s["criticality"]
    assert set(block) == {"weighted_prf", "critical_pass", "n_critical", "weights_version"}
    assert set(block["weighted_prf"]) == {"precision", "recall", "f1"}
    assert block["weights_version"] == "v1"
    assert block["critical_pass"] == 1.0
    assert block["n_critical"] == 1
    # correct weight 1.0 of 1.1 returned and 1.1 expected
    assert block["weighted_prf"]["precision"] == pytest.approx(1.0 / 1.1)


# ------------------------------------------------------------------ weighted_prf
def test_wrong_optional_field_costs_less_than_wrong_critical_field():
    expected = {"typed_fields": {"account_holder": "Jane", "bank_name": "X"}}
    wrong_optional = score(
        _resp({"account_holder": "Jane", "bank_name": "Y"}), expected, schema=STATEMENT
    )
    wrong_critical = score(
        _resp({"account_holder": "Joe", "bank_name": "X"}), expected, schema=STATEMENT
    )
    a = wrong_optional["criticality"]["weighted_prf"]["f1"]
    b = wrong_critical["criticality"]["weighted_prf"]["f1"]
    assert a > b
    # Unweighted, both are one wrong field out of two.
    assert wrong_optional["overall"] == wrong_critical["overall"]


def test_undeclared_returned_field_carries_expected_weight():
    expected = {"typed_fields": {"account_holder": "Jane"}}
    s = score(_resp({"account_holder": "Jane", "invented": "x"}), expected, schema=STATEMENT)
    assert s["criticality"]["weighted_prf"]["precision"] == pytest.approx(1.0 / 1.3)


def test_invented_row_lowers_weighted_precision():
    extra = {"date": "2026-01-10", "description": "INVENTED", "amount": -1.0}
    expected = {"typed_rows": {"transactions": ROWS}}
    s = score(_resp({"transactions": ROWS + [extra]}), expected, schema=STATEMENT)
    w = s["criticality"]["weighted_prf"]
    assert w["recall"] == 1.0
    assert w["precision"] == pytest.approx(2 / 3)


def test_ignore_column_inside_items_is_not_compared():
    got = [dict(r, category="WRONG") for r in ROWS]
    expected = {"typed_rows": {"transactions": ROWS}}
    s = score(_resp({"transactions": got}), expected, schema=STATEMENT)
    assert s["criticality"]["weighted_prf"]["f1"] == 1.0
    assert s["criticality"]["critical_pass"] == 1.0
    # The unweighted row dimension still compares every column the case labelled.
    assert s["dimensions"]["row_prf"]["f1"] == 0.0


def test_nested_label_scores_start_and_end_as_separate_items():
    expected = {"typed_fields": {"statement_period": {"start": "2026-01-01", "end": "2026-01-31"}}}
    end_wrong = _resp({"statement_period": {"start": "2026-01-01", "end": "2026-01-30"}})
    s = score(end_wrong, expected, schema=STATEMENT)
    # start is critical (1.0) and right; end inherits the parent's `expected` (0.3) and is wrong.
    w = s["criticality"]["weighted_prf"]
    assert w["precision"] == pytest.approx(1.0 / 1.3)
    assert w["recall"] == pytest.approx(1.0 / 1.3)
    assert s["criticality"]["critical_pass"] == 1.0
    # Unweighted, the whole object is one wrong field.
    assert s["dimensions"]["field_prf"]["f1"] == 0.0


def test_weighted_prf_is_null_when_the_case_scores_no_fields():
    s = score(_resp({"account_holder": "Jane"}), {"text_contains": []}, schema=STATEMENT)
    assert s["criticality"]["weighted_prf"] is None
    assert s["criticality"]["critical_pass"] is None


# ------------------------------------------------------------------ critical_pass
def test_gold_null_on_a_critical_field_fails_a_non_null_reply():
    expected = {"typed_fields": {"closing_balance": None}}
    fail = score(_resp({"closing_balance": 12.0}), expected, schema=STATEMENT)
    assert fail["criticality"]["critical_pass"] == 0.0
    absent = score(_resp({}), expected, schema=STATEMENT)
    assert absent["criticality"]["critical_pass"] == 1.0
    null = score(_resp({"closing_balance": None}), expected, schema=STATEMENT)
    assert null["criticality"]["critical_pass"] == 1.0


def test_critical_field_absent_from_gold_is_skipped():
    expected = {"typed_fields": {"account_holder": "Jane"}}
    s = score(_resp({"account_holder": "Jane", "closing_balance": 99}), expected, schema=STATEMENT)
    assert s["criticality"]["n_critical"] == 1
    assert s["criticality"]["critical_pass"] == 1.0


def test_no_critical_field_reports_null():
    expected = {"typed_fields": {"bank_name": "X"}}
    s = score(_resp({"bank_name": "X"}), expected, schema=STATEMENT)
    assert s["criticality"]["critical_pass"] is None
    assert s["criticality"]["n_critical"] == 0


def test_wrong_critical_scalar_fails():
    expected = {"typed_fields": {"account_holder": "Jane", "closing_balance": 10}}
    s = score(_resp({"account_holder": "Jane", "closing_balance": 11}), expected, schema=STATEMENT)
    assert s["criticality"]["critical_pass"] == 0.0
    assert s["criticality"]["n_critical"] == 2


def test_critical_row_field_needs_every_row_and_no_extra():
    expected = {"typed_rows": {"transactions": ROWS}}
    dropped = score(_resp({"transactions": ROWS[:1]}), expected, schema=STATEMENT)
    assert dropped["criticality"]["critical_pass"] == 0.0
    extra = {"date": "2026-01-10", "description": "INVENTED", "amount": -1.0}
    invented = score(_resp({"transactions": ROWS + [extra]}), expected, schema=STATEMENT)
    assert invented["criticality"]["critical_pass"] == 0.0
    exact = score(_resp({"transactions": list(reversed(ROWS))}), expected, schema=STATEMENT)
    assert exact["criticality"]["critical_pass"] == 1.0


def test_two_row_fields_one_wrong_fail_critical_pass():
    # Pooled counts would hide this: a dropped row in one field and an invented row in the other
    # give rows_exact 2 of 3 expected and 3 got overall, but neither field is right.
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "array", "x-read-criticality": "critical"},
            "b": {"type": "array", "x-read-criticality": "critical"},
        },
    }
    expected = {"typed_rows": {"a": ["x", "y"], "b": ["z"]}}
    s = score(_resp({"a": ["x", "y"], "b": ["w"]}), expected, schema=schema)
    assert s["criticality"]["critical_pass"] == 0.0
    assert s["criticality"]["n_critical"] == 2
    ok = score(_resp({"a": ["y", "x"], "b": ["z"]}), expected, schema=schema)
    assert ok["criticality"]["critical_pass"] == 1.0


def test_a_wrong_non_critical_row_field_does_not_fail_critical_pass():
    # row_prf pools its counts across fields, so the check runs the matcher once per field.
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "array", "x-read-criticality": "critical"},
            "b": {"type": "array", "x-read-criticality": "optional"},
        },
    }
    expected = {"typed_rows": {"a": ["x", "y"], "b": ["z"]}}
    s = score(_resp({"a": ["x", "y"], "b": ["w", "q"]}), expected, schema=schema)
    assert s["dimensions"]["row_prf"]["f1"] < 1.0
    assert s["criticality"]["critical_pass"] == 1.0
    assert s["criticality"]["n_critical"] == 1


def test_critical_column_inside_items_checks_its_row_field():
    schema = {
        "type": "object",
        "properties": {
            "transactions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"amount": {"type": "number", "x-read-criticality": "critical"}},
                },
            }
        },
    }
    rows = [{"amount": 1.0}, {"amount": 2.0}]
    expected = {"typed_rows": {"transactions": rows}}
    s = score(_resp({"transactions": [{"amount": 1.0}, {"amount": 3.0}]}), expected, schema=schema)
    assert s["criticality"]["critical_pass"] == 0.0
    assert s["criticality"]["n_critical"] == 1


def test_nested_critical_path_is_checked_on_its_own():
    expected = {"typed_fields": {"statement_period": {"start": "2026-01-01", "end": "2026-01-31"}}}
    start_wrong = _resp({"statement_period": {"start": "2026-01-02", "end": "2026-01-31"}})
    s = score(start_wrong, expected, schema=STATEMENT)
    assert s["criticality"]["critical_pass"] == 0.0
    assert s["criticality"]["n_critical"] == 1


# ------------------------------------------------------------------ SX5 degeneracy
FIELD_FIXTURES = [
    ({"a": "1", "b": "2", "c": "x"}, {"a": "1", "b": "2", "d": "4"}),
    ({}, {}),
    ({}, {"a": "1"}),
    ({"a": "1"}, {}),
    ({"total": "$1,234.50"}, {"total": 1234.5}),
    (
        {"statement_period": {"end": "2026-01-31", "start": "2026-01-01"}},
        {"statement_period": {"start": "2026-01-01", "end": "2026-01-31"}},
    ),
    ({"x": None, "y": "  Hello  World "}, {"x": None, "y": "hello world", "z": 3}),
    ({"a": "1", "b": "2", "c": "3"}, {"a": "1", "b": "9", "c": "3"}),
]

ROW_FIXTURES = [
    ({"transactions": ROWS}, {"transactions": ROWS}),
    ({"transactions": ROWS[:1]}, {"transactions": ROWS}),
    ({"transactions": ROWS + [{"date": "x"}]}, {"transactions": ROWS}),
    ({}, {"transactions": ROWS}),
    ({"transactions": []}, {"transactions": []}),
    ({"transactions": ROWS}, {"transactions": []}),
    ({"l": ["10", "100"]}, {"l": [10, 100, 1000]}),
]


def _labelled(names) -> dict:
    """A schema labelling every name, so the block is computed, with each code represented."""
    codes = ["critical", "expected", "optional", "ignore"]
    return {
        "type": "object",
        "properties": {
            n: {"x-read-criticality": codes[i % 4]} for i, n in enumerate(sorted(names))
        },
    }


@pytest.mark.parametrize("got,expected", FIELD_FIXTURES)
def test_all_ones_weighted_prf_equals_field_prf_bit_for_bit(got, expected):
    schema = _labelled(set(got) | set(expected))
    w = weighted_prf(_resp(got), {"typed_fields": expected}, schema, weights=ONES)
    u = field_prf(got, expected)
    assert w is not None
    for k in ("precision", "recall", "f1"):
        assert w[k] == u[k], k


@pytest.mark.parametrize("got,expected", ROW_FIXTURES)
def test_all_ones_weighted_prf_equals_row_prf_bit_for_bit(got, expected):
    schema = {"type": "object", "properties": {n: {"type": "array"} for n in expected}}
    w = weighted_prf(_resp(got), {"typed_rows": expected}, schema, weights=ONES)
    u = row_prf(got, expected)
    assert w is not None
    for k in ("precision", "recall", "f1"):
        assert w[k] == u[k], k


def test_all_ones_degeneracy_on_the_row_suite_fixtures():
    from tests import test_evals_rows as suite

    exp = {"transactions": suite.ROWS}
    near = dict(suite.ROWS[0], amount=-1.0)
    for got in (suite.ROWS, suite.ROWS[:2], [near, suite.ROWS[0]], list(reversed(suite.ROWS))):
        schema = {"type": "object", "properties": {"transactions": {"type": "array"}}}
        w = weighted_prf(_resp({"transactions": got}), {"typed_rows": exp}, schema, weights=ONES)
        u = row_prf({"transactions": got}, exp)
        assert w is not None
        assert (w["precision"], w["recall"], w["f1"]) == (u["precision"], u["recall"], u["f1"])


# ------------------------------------------------------------------ the runner carries the block
def _typed_backend(fields: dict):
    from openreading.types.blocks import TypedField
    from tests.fakes import ConfigurableBackend, make_backend

    class _Typed(ConfigurableBackend):
        def normalize(self, job, ctx, req):
            resp = super().normalize(job, ctx, req)
            resp.typed_fields = {k: TypedField(value=v) for k, v in fields.items()}
            return resp

    return _Typed(make_backend("typed-fake", custom_schema=True).descriptor)


def _case(schema: dict | None):
    from openreading.evals import EvalCase

    body: dict = {
        "document": {"bytes_base64": "aGVsbG8=", "mime_type": "application/pdf"},
        "backend": {"id": "typed-fake"},
    }
    if schema is not None:
        body["extraction_schema"] = {"json_schema": schema}
    return EvalCase(
        name="statement",
        request_body=body,
        expected={"typed_fields": {"account_holder": "Jane", "bank_name": "X"}},
    )


def test_run_case_scores_against_the_case_schema():
    from openreading.evals import run_case

    backend = _typed_backend({"account_holder": "Jane", "bank_name": "Y"})
    result = run_case(backend, _case(STATEMENT))
    assert result.error is None
    assert result.criticality is not None
    assert result.criticality["critical_pass"] == 1.0
    assert result.criticality["weights_version"] == "v1"
    assert "criticality" not in result.dimensions


def test_run_case_without_a_schema_carries_no_block():
    from openreading.evals import run_case

    result = run_case(_typed_backend({"account_holder": "Jane"}), _case(None))
    assert result.error is None
    assert result.criticality is None
