"""Batch retention preserves complete envelopes while validating each normalized response."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from referencing import Registry, Resource

from openreading import schemas
from openreading.artifacts.models import json_bytes
from openreading.artifacts.result_models import (
    AttributedResultProvenance,
    ResultContent,
    ResultError,
    ResultRecord,
)
from openreading.mcp_server.results import deliver_result
from openreading.types.execution_job import ExecutionJob
from tests.test_execution_job_contract import receipt, status
from tests.test_mcp_results import reconstruct
from tests.test_retained_results import provenance, response, results  # noqa: F401


def batch():
    payload = response("partial", "café界" * 1200)
    payload["typed_fields"] = {"backend_raw": {"value": {"nullable": None}}}
    return {
        "schema_version": "0.2",
        "status": {"state": "partial"},
        "request": {"strategy": None},
        "items": [
            {
                "source": {"filename": "first.pdf", "format": "pdf", "path": None},
                "state": "succeeded",
                "response": payload,
                "error": None,
                "transport": "platform",
            },
            {
                "source": {"filename": "second.pdf", "format": "pdf"},
                "state": "failed",
                "response": None,
                "error": {"code": "failed", "message": "Retained failure"},
                "transport": None,
            },
        ],
        "summary": {"total": 2, "succeeded": 1, "failed": 1, "duration_ms": None},
        "warnings": [{"code": "retained_warning"}],
    }


def validator(schema):
    registry = Registry().with_resources(
        (value["$id"], Resource.from_contents(value))
        for value in [
            schemas.response_schema(),
            schemas.batch_result_schema(),
            schemas.comparison_report_schema(),
        ]
    )
    return Draft202012Validator(schema, registry=registry)


def test_batch_retention_preserves_payload_and_all_delivery_modes(results):  # noqa: F811
    payload = batch()
    before = copy.deepcopy(payload)
    retained = results.publish("batch_result", payload, provenance())
    loaded = results.load(retained.result_id)
    assert payload == before == loaded.payload
    assert retained.kind == "batch_result"
    record = json.loads(results.path(retained.result_id).read_bytes())
    assert record["format"] == "retained-result.v0.3"
    assert results.record(loaded).wire() == record
    validator(schemas.retained_result_schema()).validate(record)
    assert retained.result_id == "orr1_" + hashlib.sha256(json_bytes(record)).hexdigest()
    for mode, budget in [("auto", 100_000), ("file", 4096), ("fragments", 4096)]:
        cursor, pages = None, []
        while True:
            delivered = deliver_result(
                results,
                retained.result_id,
                mode=mode,
                budget=budget,
                root=None,
                request_id=1,
                cursor=cursor,
            )
            page = json.loads(delivered.content[0].text)
            validator(schemas.result_tool_schema()).validate(page)
            pages.append(page)
            cursor = page.get("next_cursor")
            if cursor is None:
                break
        actual = (
            pages[0]["content"]
            if mode == "auto"
            else json.loads(Path(pages[0]["local_path"]).read_bytes())
            if mode == "file"
            else reconstruct(pages)
        )
        assert actual == loaded.wire()


@pytest.mark.parametrize("damage", ["envelope", "nested", "raw"])
def test_batch_rejects_invalid_envelope_response_and_direct_raw(results, damage):  # noqa: F811
    payload = batch()
    if damage == "envelope":
        payload["status"]["state"] = "invented"
    elif damage == "nested":
        payload["items"][0]["response"]["status"]["state"] = "invented"
    else:
        payload["items"][0]["response"]["backend_raw"] = {"payload": "secret"}
    with pytest.raises(ResultError, match="invalid_result"):
        results.publish("batch_result", payload, provenance())
    assert not list(results.root.iterdir())
    record = {
        "format": "retained-result.v0.3",
        "input_grant_sha256": results.store.grant,
        "content": {
            "kind": "batch_result",
            "payload": payload,
            "provenance": provenance().model_dump(mode="json"),
        },
    }
    assert not validator(schemas.retained_result_schema()).is_valid(record)


def test_batch_refuses_comparison_provenance(results):  # noqa: F811
    for origin in [
        provenance(subjects={"synthetic": "orr1_" + "a" * 64}),
        AttributedResultProvenance(**provenance().model_dump(mode="json"), subject_sources={}),
    ]:
        with pytest.raises(ResultError, match="invalid_result"):
            results.publish("batch_result", batch(), origin)


@pytest.mark.parametrize("version", ["0.1", "0.2"])
def test_historical_formats_cannot_claim_batch_payloads(version):
    record = {
        "format": "retained-result.v" + version,
        "input_grant_sha256": "a" * 64,
        "content": {
            "kind": "batch_result",
            "payload": batch(),
            "provenance": provenance().model_dump(mode="json"),
        },
    }
    with pytest.raises(ValidationError):
        ResultRecord.model_validate(record)
    assert not validator(schemas.retained_result_schema()).is_valid(record)


@pytest.mark.parametrize("outcome", ["succeeded", "partial", "failed"])
def test_execution_job_v02_accepts_batch_receipts_and_preserves_outcome(outcome):
    value = status(
        schema_version="0.2",
        state="succeeded",
        stage="complete",
        receipt={**receipt(), "kind": "batch_result"},
        response_state=outcome,
    )
    assert ExecutionJob.model_validate(value).wire() == value
    Draft202012Validator(schemas.execution_job_schema()).validate(value)
    value["schema_version"] = "0.1"
    with pytest.raises(ValidationError):
        ExecutionJob.model_validate(value)
    assert not Draft202012Validator(schemas.execution_job_schema()).is_valid(value)


def test_new_job_default_and_old_job_roundtrip():
    value = status()
    assert ExecutionJob.model_validate(value).wire() == value
    del value["schema_version"]
    assert ExecutionJob.model_validate(value).schema_version == "0.2"


def test_batch_content_retains_nulls_without_model_normalization():
    value = ResultContent(kind="batch_result", payload=batch(), provenance=provenance())
    assert value.wire()["payload"] == batch()
