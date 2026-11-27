"""The typed-fields check in `openreading.derive.fields`: semantics, warnings and laws SX3, SX6.

Pure tests over `check_typed_fields`, `check_warnings`, `strip_annotations` and the request-model
criticality guard. The router paths that attach the check live in `tests/test_finalize_paths.py`.
"""

from __future__ import annotations

import random

import pytest
from pydantic import ValidationError

from openreading.derive.fields import (
    FORMAT_CHECKER,
    MAX_ERRORS,
    check_typed_fields,
    check_warnings,
    has_annotations,
    schema_hash,
    strip_annotations,
    unknown_criticality,
)
from openreading.router.finalize import apply_schema_check
from openreading.types.blocks import TypedField
from openreading.types.request import OpenReadingRequest
from openreading.types.response import (
    BackendInfo,
    Document,
    NormalizedResponse,
    Status,
)

# Four synthetic schemas shaped like the lab's domain schemas (bank statement, RFQ, title block,
# clinical agreement). They are written here, not imported, so core depends on nothing outside it.
BANK = {
    "type": "object",
    "required": ["account_number_last4", "closing_balance"],
    "properties": {
        "account_number_last4": {
            "type": "string",
            "pattern": "^[0-9]{4}$",
            "x-read-criticality": "critical",
        },
        "opening_balance": {"type": "number", "x-read-criticality": "critical"},
        "closing_balance": {"type": "number", "x-read-criticality": "critical"},
        "bank_name": {"type": "string", "x-read-criticality": "optional"},
        "statement_period": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "format": "date", "x-read-criticality": "critical"},
                "end": {"type": "string", "format": "date", "x-read-criticality": "critical"},
            },
        },
        "transactions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["amount"],
                "properties": {
                    "date": {"type": "string", "format": "date"},
                    "amount": {"type": "number"},
                    "category": {"type": "string", "maxLength": 3, "x-read-criticality": "ignore"},
                },
            },
        },
    },
}
RFQ = {
    "type": "object",
    "properties": {
        "rfq_number": {"type": "string", "x-read-criticality": "critical"},
        "response_due": {"type": "string", "format": "date-time", "x-read-criticality": "critical"},
        "buyer_email": {"type": "string", "format": "email", "x-read-notes": "contact"},
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"part": {"type": "string"}, "qty": {"type": "integer"}},
            },
        },
    },
}
TITLE_BLOCK = {
    "type": "object",
    "properties": {
        "drawing_number": {"type": "string", "x-read-criticality": "critical"},
        "revision": {"type": "string", "x-read-criticality": "expected"},
        "scale": {"type": "string", "x-read-criticality": "optional"},
        "x-read-legacy": {"type": "string"},
    },
}
CLINICAL = {
    "type": "object",
    "$defs": {"party": {"type": "object", "properties": {"name": {"type": "string"}}}},
    "properties": {
        "sponsor": {"$ref": "#/$defs/party", "x-read-criticality": "critical"},
        "effective_date": {"type": "string", "format": "date"},
        "indemnification": {"type": "string", "x-read-criticality": "expected"},
    },
}
LAB_STYLE_SCHEMAS = [BANK, RFQ, TITLE_BLOCK, CLINICAL]


def _tf(**values) -> dict[str, TypedField]:
    return {k: TypedField(value=v) for k, v in values.items()}


def _status(check) -> dict[str, str]:
    return {f.path: f.status for f in check.fields}


def _good_bank(**over):
    values = {
        "account_number_last4": "1000",
        "opening_balance": 10.0,
        "closing_balance": 20.0,
        "bank_name": "First National",
        "statement_period": {"start": "2026-01-01", "end": "2026-01-31"},
        "transactions": [{"date": "2026-01-03", "amount": 5.0, "category": "fee"}],
    }
    values.update(over)
    return _tf(**values)


# --------------------------------------------------------------------------- strip and hash
@pytest.mark.parametrize("schema", LAB_STYLE_SCHEMAS)
def test_strip_removes_every_x_read_keyword_and_keeps_property_names(schema):
    stripped = strip_annotations(schema)
    assert not has_annotations(stripped)
    assert has_annotations(schema)
    # A property NAMED x-read-legacy is data, not a keyword, so it survives.
    if schema is TITLE_BLOCK:
        assert "x-read-legacy" in stripped["properties"]
    assert stripped is not schema and has_annotations(schema)  # the original is untouched


def test_schema_hash_hashes_the_annotated_original():
    relabelled = strip_annotations(BANK)
    assert schema_hash(BANK).startswith("sha256:") and len(schema_hash(BANK)) == 71
    assert schema_hash(BANK) != schema_hash(relabelled)
    assert schema_hash({"b": 1, "a": 2}) == schema_hash({"a": 2, "b": 1})


# --------------------------------------------------------------------------- fields and status
def test_a_clean_reply_is_valid_and_critical_ok():
    check = check_typed_fields(BANK, _good_bank())
    assert check.valid and check.critical_ok
    assert check.errors == [] and check.extra == []
    assert check.schema_ref is None
    assert set(_status(check).values()) == {"present"}


def test_nested_labels_get_entries_and_an_unlabelled_parent_gets_expected():
    check = check_typed_fields(BANK, _good_bank())
    crit = {f.path: f.criticality for f in check.fields}
    assert crit["statement_period"] == "expected"
    assert crit["statement_period.start"] == "critical"
    assert crit["statement_period.end"] == "critical"
    assert crit["transactions.category"] == "ignore"
    assert "transactions.amount" not in crit  # unlabelled nested properties get no entry


def test_missing_versus_null_versus_blank_string():
    fields = _good_bank()
    del fields["opening_balance"]
    fields["bank_name"] = TypedField(value=None)
    fields["account_number_last4"] = TypedField(value="   ")
    status = _status(check_typed_fields(BANK, fields))
    assert status["opening_balance"] == "missing"
    assert status["bank_name"] == "missing"
    assert status["account_number_last4"] == "missing"  # blank beats its pattern error


def test_a_null_required_field_reports_missing_at_the_parent_plus_name():
    check = check_typed_fields(BANK, _good_bank(closing_balance=None))
    assert _status(check)["closing_balance"] == "missing"
    assert {"path": "closing_balance", "message": "'closing_balance' is a required property"} in [
        e.model_dump() for e in check.errors
    ]
    assert not check.valid and not check.critical_ok


def test_a_nested_required_error_carries_the_row_index_and_name():
    check = check_typed_fields(BANK, _good_bank(transactions=[{"amount": 1}, {"date": "x"}]))
    paths = [e.path for e in check.errors]
    assert "transactions.1.amount" in paths
    assert _status(check)["transactions"] == "invalid"


def test_an_empty_list_counts_as_present():
    assert _status(check_typed_fields(BANK, _good_bank(transactions=[])))["transactions"] == (
        "present"
    )


def test_a_nested_critical_path_is_checked():
    check = check_typed_fields(BANK, _good_bank(statement_period={"start": "2026-02-30"}))
    status = _status(check)
    assert status["statement_period.start"] == "invalid"  # not a real date
    assert status["statement_period.end"] == "missing"
    assert status["statement_period"] == "invalid"
    assert not check.critical_ok


def test_an_error_under_an_ignore_column_stays_in_errors_and_leaves_its_parent_present():
    check = check_typed_fields(
        BANK, _good_bank(transactions=[{"amount": 1, "category": "toolong"}])
    )
    assert [e.path for e in check.errors] == ["transactions.0.category"]
    status = _status(check)
    assert status["transactions"] == "present"
    assert status["transactions.category"] == "invalid"
    assert check.critical_ok and not check.valid


def test_extra_keys_are_listed_and_kept():
    fields = _good_bank(zeta=1, alpha=2)
    check = check_typed_fields(BANK, fields)
    assert check.extra == ["alpha", "zeta"]
    assert "zeta" in fields


def test_errors_are_capped_at_fifty():
    rows = [{"date": "nope"} for _ in range(80)]  # each row: missing amount + bad date
    check = check_typed_fields(BANK, _good_bank(transactions=rows))
    assert len(check.errors) == MAX_ERRORS


def test_no_typed_fields_reports_every_field_missing():
    check = check_typed_fields(BANK, None)
    assert set(_status(check).values()) == {"missing"}
    assert not check.critical_ok


def test_a_schema_without_top_level_properties_reports_no_fields():
    check = check_typed_fields({"invoice_number": "string"}, _tf(invoice_number="A-1"))
    assert check.fields == []


@pytest.mark.parametrize(
    "schema",
    [{"type": "unknown-type"}, {"$ref": "#"}, {"$ref": "https://schema.example.test/x"}],
)
def test_an_unusable_schema_reports_invalid_with_one_root_error(schema):
    check = check_typed_fields(schema, _tf(total=42))
    assert not check.valid
    assert [e.path for e in check.errors] == [""]


# --------------------------------------------------------------------------- formats
def test_date_and_date_time_are_asserted_and_email_is_unchecked():
    check = check_typed_fields(
        RFQ, _tf(rfq_number="R-1", response_due="2026-13-01T00:00:00Z", buyer_email="not-mail")
    )
    assert _status(check)["response_due"] == "invalid"
    assert _status(check)["buyer_email"] == "present"
    assert check.formats_unchecked == ["email"]
    good = check_typed_fields(RFQ, _tf(rfq_number="R-1", response_due="2026-12-01T09:30:00+02:00"))
    assert good.valid


@pytest.mark.parametrize(
    "fmt,value,ok",
    [
        ("date", "2026-01-31", True),
        ("date", "20260131", False),
        ("date", "2026-02-30", False),
        ("date-time", "2026-01-31T23:59:60Z", True),
        ("date-time", "2026-01-31T10:00:00", False),
        ("date-time", "2026-01-31 10:00:00Z", False),
        ("date-time", "2026-01-31t10:00:00.5z", True),
    ],
)
def test_the_pinned_format_checker(fmt, value, ok):
    assert FORMAT_CHECKER.conforms(value, fmt) is ok
    assert FORMAT_CHECKER.conforms(12, fmt)  # formats apply to strings only


# --------------------------------------------------------------------------- warnings
def _warn(schema, fields):
    return check_warnings(schema, check_typed_fields(schema, fields))


def test_each_field_warning_carries_the_path():
    warnings = _warn(BANK, _good_bank(opening_balance=None, account_number_last4="12"))
    by_code = {(code, field) for code, _, field in warnings}
    assert ("critical_field_missing", "opening_balance") in by_code
    assert ("critical_field_invalid", "account_number_last4") in by_code
    assert all(msg.endswith(".") for _, msg, _ in warnings)


def test_intent_all_critical_fires_on_two_critical_labels_and_not_on_one():
    two = {
        "properties": {
            "a": {"x-read-criticality": "critical"},
            "b": {"x-read-criticality": "critical"},
            "c": {},
        }
    }
    one = {"properties": {"a": {"x-read-criticality": "critical"}, "c": {}}}
    mixed = {
        "properties": {
            "a": {"x-read-criticality": "critical"},
            "b": {"x-read-criticality": "optional"},
        }
    }
    full = _tf(a=1, b=1, c=1)
    assert [c for c, _, _ in _warn(two, full)] == ["intent_all_critical"]
    assert _warn(one, full) == []
    assert _warn(mixed, full) == []


def test_a_schema_without_labels_is_all_expected_and_critical_ok():
    schema = strip_annotations(BANK)
    check = check_typed_fields(schema, None)
    assert {f.criticality for f in check.fields} == {"expected"}
    assert check.critical_ok
    assert check_warnings(schema, check) == []


# --------------------------------------------------------------------------- apply_schema_check
def _resp(fields) -> NormalizedResponse:
    return NormalizedResponse(
        status=Status(state="succeeded"),
        backend=BackendInfo(id="spy", type="hosted_api"),
        document=Document(),
        typed_fields=fields,
    )


def _labelled_req(schema) -> OpenReadingRequest:
    return OpenReadingRequest.model_validate(
        {
            "document": {"path": "/x.pdf"},
            "backend": {"id": "spy"},
            "extraction_schema": {"json_schema": schema},
        }
    )


def test_apply_schema_check_is_idempotent_and_replaces_its_warnings():
    resp = _resp(_good_bank(opening_balance=None))
    resp.add_warning("output_truncated", "kept", None)
    req = _labelled_req(BANK)
    once = apply_schema_check(resp, req).to_schema_dict()
    twice = apply_schema_check(resp, req).to_schema_dict()
    assert once == twice
    assert [w["code"] for w in twice["warnings"]] == ["output_truncated", "critical_field_missing"]
    assert twice["schema_check"]["schema_ref"] is None  # a required nullable key survives


def test_apply_schema_check_fills_type_and_never_touches_values():
    resp = _resp(_tf(opening_balance=10, bank_name="x"))
    resp.typed_fields["bank_name"].type = "text"
    apply_schema_check(resp, _labelled_req(BANK))
    assert resp.typed_fields["opening_balance"].type == "number"
    assert resp.typed_fields["bank_name"].type == "text"  # an adapter's own type wins


def test_without_extraction_schema_nothing_is_added():
    resp = _resp(_tf(a=1))
    req = OpenReadingRequest.model_validate(
        {
            "document": {"path": "/x.pdf"},
            "backend": {"id": "spy"},
            "outputs": {"typed_fields": True},
        }
    )
    out = apply_schema_check(resp, req).to_schema_dict()
    assert "schema_check" not in out and "warnings" not in out
    assert out["typed_fields"]["a"] == {"value": 1}


# --------------------------------------------------------------------------- SX3
def _random_value(rng: random.Random, depth: int = 0):
    kind = rng.randrange(7 if depth < 2 else 4)
    if kind == 0:
        return None
    if kind == 1:
        return rng.choice(["", " ", "x", "2026-01-01"])
    if kind == 2:
        return rng.choice([0, 1.5, -3, True])
    if kind == 3:
        return rng.choice([None, "y"])
    if kind in (4, 5):
        return {k: _random_value(rng, depth + 1) for k in rng.sample(["start", "end", "z"], 2)}
    return [_random_value(rng, depth + 1) for _ in range(rng.randrange(3))]


@pytest.mark.parametrize("seed", range(40))
def test_sx3_the_check_never_edits_values(seed):
    rng = random.Random(seed)
    names = list(BANK["properties"]) + ["undeclared"]
    fields = {
        n: TypedField(value=_random_value(rng), normalized_value=_random_value(rng))
        for n in rng.sample(names, rng.randrange(1, len(names)))
    }
    before = {n: (f.value, f.normalized_value) for n, f in fields.items()}
    resp = _resp(fields)
    apply_schema_check(resp, _labelled_req(BANK))
    after = {n: (f.value, f.normalized_value) for n, f in resp.typed_fields.items()}
    assert after == before


# --------------------------------------------------------------------------- SX6 (request model)
def test_an_unknown_criticality_code_is_a_validation_error():
    schema = {"properties": {"a": {"x-read-criticality": "crtical"}}}
    assert unknown_criticality(schema) == ["'crtical'"]
    with pytest.raises(ValidationError, match="crtical"):
        _labelled_req(schema)


def test_a_known_code_at_any_depth_is_accepted():
    _labelled_req(BANK)
    assert unknown_criticality(BANK) == []
    nested = {"properties": {"a": {"items": {"properties": {"b": {"x-read-criticality": 3}}}}}}
    assert unknown_criticality(nested) == ["3"]
