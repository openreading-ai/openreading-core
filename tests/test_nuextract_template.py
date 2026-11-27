"""NuExtract reads its own typed template, not JSON Schema. The adapter translates a JSON Schema
into that template and passes a native template through unchanged (design D12, Q11)."""

from __future__ import annotations

import pytest

from openreading import schema_bank
from openreading.adapters.nuextract.adapter import (
    NuExtractAdapter,
    is_json_schema,
    json_schema_to_template,
)
from openreading.router.finalize import apply_schema_check
from openreading.types.runtime import RunContext
from tests.test_nuextract import FakeNuExtractClient, _extract_req, _req, _run


def _schema_req(schema, instructions=None):
    es = {"json_schema": schema}
    if instructions:
        es["instructions"] = instructions
    return _req(backend={"id": "nuextract", "operation": "extract"}, extraction_schema=es)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"type": "object", "properties": {"a": {"type": "string"}}}, True),
        ({"type": "object", "properties": {}}, True),
        ({"type": "object"}, False),
        ({"properties": {"a": {}}}, False),
        ({"title": "verbatim-string"}, False),
        ({"invoice": "verbatim-string", "type": "string"}, False),
        (None, False),
    ],
)
def test_a_json_schema_is_a_top_level_object_with_properties(value, expected):
    assert is_json_schema(value) is expected


def test_translation_rules():
    schema = {
        "type": "object",
        "required": ["a"],
        "properties": {
            "name": {"type": "string", "description": "dropped", "x-read-criticality": "critical"},
            "count": {"type": "integer"},
            "amount": {"type": "number"},
            "when": {"type": "string", "format": "date-time"},
            "day": {"type": "string", "format": "date"},
            "flag": {"type": "boolean"},
            "kind": {"type": "string", "enum": ["a", "b"]},
            "kinds": {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}},
            "tags": {"type": "array", "items": {"type": "string"}},
            "sizes": {"type": "array", "items": {"type": "integer"}},
            "anything": {"type": "array"},
            "address": {
                "type": "object",
                "properties": {"city": {"type": "string"}, "zip": {"type": "integer"}},
            },
            "rows": {
                "type": "array",
                "items": {"type": "object", "properties": {"k": {"type": "string"}}},
            },
            "opaque": {"type": "object"},
            "nullable": {"type": ["null", "number"]},
            "ref": {"$ref": "#/$defs/x"},
            "untyped": {},
        },
    }
    assert json_schema_to_template(schema) == {
        "name": "string",
        "count": "integer",
        "amount": "number",
        "when": "date-time",
        "day": "string",
        "flag": ["true", "false"],
        "kind": ["a", "b"],
        "kinds": [["a", "b"]],
        "tags": ["string"],
        "sizes": ["integer"],
        "anything": ["string"],
        "address": {"city": "string", "zip": "integer"},
        "rows": [{"k": "string"}],
        "opaque": "string",
        "nullable": "number",
        "ref": "string",
        "untyped": "string",
    }


@pytest.mark.parametrize("entry", schema_bank.entries(), ids=lambda e: e.ref)
def test_every_bank_entry_translates_to_a_grammar_template(entry):
    template = json_schema_to_template(entry.schema)
    assert list(template) == list(entry.schema["properties"])

    leaves = {"verbatim-string", "string", "integer", "number", "date-time"}

    def in_grammar(node):
        if isinstance(node, str):
            return True  # a leaf type, or one member of an enum list
        if isinstance(node, dict):
            return all(in_grammar(v) for v in node.values())
        if isinstance(node, list) and node and all(isinstance(v, str) for v in node):
            return True  # an enum, or an array of a leaf type
        return isinstance(node, list) and len(node) == 1 and in_grammar(node[0])

    assert all(in_grammar(v) for v in template.values())
    assert all(v in leaves for v in template.values() if isinstance(v, str))
    assert "x-read-criticality" not in str(template)


def test_a_json_schema_reaches_the_platform_as_a_template():
    client = FakeNuExtractClient()
    schema = schema_bank.resolve("generic").schema
    _run(NuExtractAdapter(client=client), _schema_req(schema, "read it"))
    sent = client.created_projects[0]["template"]
    assert sent == json_schema_to_template(schema)
    assert sent["parties"] == [{"name": "string", "role": "string"}]
    assert sent["document_date"] == "string"
    assert client.created_projects[0]["instructions"] == "read it"


def test_a_native_template_passes_through_unchanged():
    from tests.test_nuextract import TEMPLATE

    client = FakeNuExtractClient()
    _run(NuExtractAdapter(client=client), _extract_req())
    assert client.created_projects[0]["template"] is not None
    assert client.created_projects[0]["template"] == TEMPLATE


def test_a_native_template_reports_no_checked_fields():
    resp = _run(NuExtractAdapter(client=FakeNuExtractClient()), _extract_req())
    apply_schema_check(resp, _extract_req())
    assert resp.schema_check is not None
    assert resp.schema_check.fields == []


def test_an_empty_translation_is_still_no_template():
    from openreading.types.errors import TerminalError

    adapter = NuExtractAdapter(client=FakeNuExtractClient())
    with pytest.raises(TerminalError) as exc:
        adapter.submit(_schema_req({"type": "object", "properties": {}}), RunContext())
    assert exc.value.backend_code == "no_template"
