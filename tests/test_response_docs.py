"""Execute the response guide's consumer against optional-field and status boundaries.

The example lives in the schemas README because readers copy it from there. These tests execute
that exact function, so an unsafe subscript, invented default, or skipped validation fails here.
The web repository owns and tests its tutorial copy without becoming a dependency of core.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from jsonschema import ValidationError

from openreading import run
from openreading.cli.app import main
from openreading.schemas import validate_response

GUIDE = Path(__file__).resolve().parents[1] / "src/openreading/schemas/README.md"


def _consumer():
    match = re.search(r"<!-- response-consumer -->\s*```python\n(.*?)```", GUIDE.read_text(), re.S)
    assert match, "the response guide needs its executable consumer"
    namespace = {}
    exec(compile(match[1], str(GUIDE), "exec"), namespace)
    return namespace["read_response"]


def _response(document, **extra):
    return {
        "schema_version": "0.3",
        "status": {"state": "succeeded"},
        "backend": {"id": "example", "type": "oss_library"},
        "document": document,
        **extra,
    }


@pytest.mark.parametrize("topic", ["response", "envelope", "json"])
def test_response_help_and_aliases_are_available(topic, capsys):
    assert main(["help", topic]) == 0
    output = capsys.readouterr()
    assert output.out.startswith("Understanding the response JSON\n")
    assert not output.err


def test_response_consumer_keeps_absent_text_distinct_from_empty_text():
    read = _consumer()
    assert read(_response({"text": ""}))["text"] == ""
    result = read(_response({"markdown": "# Title"}))
    assert result["text"] is None
    assert result["markdown"] == "# Title"
    assert result["warnings"] == []
    assert result["tables"] == []


def test_response_consumer_supports_fields_without_text_or_pages():
    fields = {"balance": {"value": 0}, "paid": {"value": False}, "missing": {"value": None}}
    result = _consumer()(_response({}, typed_fields=fields))
    assert result["fields"] == fields
    assert result["text"] is None


def test_response_consumer_keeps_table_structure_and_zero_confidence():
    table = {"rows": [["Total", None]], "cells": [{"row": 0, "col": 0, "confidence": 0}]}
    document = {"pages": [{"page_number": 3, "blocks": [{"type": "table", "table": table}]}]}
    assert _consumer()(_response(document))["tables"] == [table]


def test_response_consumer_does_not_invent_missing_tables_or_page_blocks():
    document = {"pages": [{"page_number": 1}, {"page_number": 2, "blocks": [{"type": "table"}]}]}
    assert _consumer()(_response(document))["tables"] == []


def test_response_consumer_preserves_partial_and_degraded_outcomes():
    response = _response(
        {"text": "Keep this"},
        status={"state": "partial"},
        warnings=[{"code": "future_warning", "message": "Inspect this result"}],
        orchestration={"outcome": "degraded"},
    )
    result = _consumer()(response)
    assert result["state"] == "partial"
    assert result["quality_outcome"] == "degraded"
    assert result["warnings"] == response["warnings"]


@pytest.mark.parametrize("state", ["failed", "processing"])
def test_response_consumer_refuses_non_consumable_states(state):
    with pytest.raises(ValueError, match=state):
        _consumer()(_response({"text": ""}, status={"state": state}))


def test_response_consumer_validates_before_reading_content():
    with pytest.raises(ValidationError):
        _consumer()(_response({"text": None}))


def test_annotated_response_is_valid_json_under_the_actual_contract():
    match = re.search(
        r"<!-- response-example: bank -->\s*```json\n(.*?)```", GUIDE.read_text(), re.S
    )
    assert match, "the guide needs one cohesive response example"
    response = json.loads(match[1])
    validate_response(response)
    assert response["document"]["pages"][0]["blocks"][0]["text"] == "First National Bank"


def test_annotated_response_matches_the_bundled_document():
    match = re.search(
        r"<!-- response-example: bank -->\s*```json\n(.*?)```", GUIDE.read_text(), re.S
    )
    example = json.loads(match[1])
    actual = run(GUIDE.parents[3] / "examples/john_smith_1000_2026_01.pdf", backend="pymupdf")
    for key in ("schema_version", "status", "backend", "usage", "warnings", "channel_provenance"):
        assert example[key] == actual[key]
    document = actual["document"]
    assert example["document"]["text"] == document["text"].splitlines()[0]
    assert example["document"]["markdown"] == document["markdown"].splitlines()[0]
    assert example["document"]["page_count"] == document["page_count"]
    page = document["pages"][0]
    shown_page = example["document"]["pages"][0]
    for key in ("page_number", "width", "height", "unit"):
        assert shown_page[key] == page[key]
    block = page["blocks"][0]
    shown_block = shown_page["blocks"][0]
    for key in ("type", "native_type", "text", "reading_order"):
        assert shown_block[key] == block[key]
    for key in ("x", "y", "w", "h"):
        assert shown_block["bbox"][key] == round(block["bbox"][key], 4)
    assert shown_block["bbox"]["page"] == block["bbox"]["page"]
