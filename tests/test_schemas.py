"""The vendored JSON Schemas are the contract. These tests keep them (and the pydantic
mirrors) honest: the schemas are valid, real instances validate, and the response anyOf
(the honest common denominator) actually rejects an empty document."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema.exceptions import ValidationError

from openreading import schemas
from openreading.types import BackendType, BlockType, ResponseState

SCHEMA_DIR = Path(schemas.__file__).parent


def test_all_schema_files_are_valid_json_schema():
    from jsonschema.validators import validator_for

    files = sorted(SCHEMA_DIR.glob("*.json"))
    assert files, "no vendored schema files found"
    for path in files:  # sweep EVERY vendored version, not a stale v0.1-only subset (§6.1)
        doc = json.loads(path.read_text())
        validator_for(doc).check_schema(doc)  # raises if invalid


def test_minimal_response_validates_each_anyof_branch():
    base = {
        "schema_version": "0.3",
        "status": {"state": "succeeded"},
        "backend": {"id": "pymupdf", "type": "oss_library"},
    }
    # branch: document.text
    schemas.validate_response({**base, "document": {"text": "hello"}})
    # branch: document.markdown
    schemas.validate_response({**base, "document": {"markdown": "# hello"}})
    # branch: document.pages
    schemas.validate_response({**base, "document": {"pages": [{"page_number": 1, "blocks": []}]}})
    # branch: top-level typed_fields (document may then be empty)
    schemas.validate_response({**base, "document": {}, "typed_fields": {"total": {"value": 42}}})


def test_response_anyof_rejects_empty_document():
    empty = {
        "schema_version": "0.3",
        "status": {"state": "succeeded"},
        "backend": {"id": "sensible", "type": "hosted_api"},
        "document": {"page_count": 1},  # none of markdown/text/pages, and no typed_fields
    }
    with pytest.raises(ValidationError):
        schemas.validate_response(empty)


def test_response_rejects_out_of_range_bbox():
    bad = {
        "schema_version": "0.3",
        "status": {"state": "succeeded"},
        "backend": {"id": "aws-textract", "type": "hosted_api"},
        "document": {
            "pages": [
                {
                    "page_number": 1,
                    "blocks": [
                        {"type": "text", "bbox": {"x": 1.5, "y": 0, "w": 0.1, "h": 0.1, "page": 1}}
                    ],
                }
            ]
        },
    }
    with pytest.raises(ValidationError):
        schemas.validate_response(bad)


def test_request_schema_forbids_unknown_nested_fields():
    """M12: the pydantic request models are `extra="forbid"` at every level
    (openreading.types.request), but request.v0.1.json set `additionalProperties: false` at the
    top level only. A misspelled NESTED field such as `document.mim_type` (for `mime_type`)
    therefore passed schema validation and failed only later, at the pydantic layer — the vendored
    schema stopped being the source of truth exactly where nesting began. request.v0.3.json closes
    every nested object node (see the structural walk in test_schema_evolution.py), so the same
    typo is now rejected here, at the wire boundary, before any backend or pydantic model sees it."""
    body = {
        "document": {"bytes_base64": "aGk=", "mim_type": "application/pdf"},
        "backend": {"id": None},
    }
    with pytest.raises(ValidationError):
        schemas.validate_request(body)


def test_validator_is_cached_per_schema_object():
    # BL-167: the constructed validator is cached per (family, version), not rebuilt (and
    # re-run through cls.check_schema against the 2020-12 metaschema) on every call. Calls
    # response_schema() twice — not once, reusing the same reference — so this pins the
    # underlying invariant the cache depends on (_load returning the identical object per
    # filename), rather than trivially passing regardless of whether that invariant holds
    # (BL-167 review).
    v1 = schemas._validator(schemas.response_schema())
    v2 = schemas._validator(schemas.response_schema())
    assert v1 is v2


def test_enums_match_schema_vocabulary():
    resp = schemas.response_schema()
    block_enum = set(resp["$defs"]["Block"]["properties"]["type"]["enum"])
    assert {bt.value for bt in BlockType} == block_enum
    state_enum = set(resp["properties"]["status"]["properties"]["state"]["enum"])
    assert {s.value for s in ResponseState} == state_enum
    btype_enum = set(resp["properties"]["backend"]["properties"]["type"]["enum"])
    assert {b.value for b in BackendType} == btype_enum
