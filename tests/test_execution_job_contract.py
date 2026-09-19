"""General job status distinguishes retained publication from provider response success."""

import importlib.util

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError


def models():
    from openreading.types import execution_job

    return execution_job


def receipt():
    return {
        "schema_version": "0.1",
        "result_id": "orr1_" + "a" * 64,
        "kind": "normalized_response",
        "content_bytes": 10,
        "content_sha256": "b" * 64,
    }


def status(**changes):
    return {
        "schema_version": "0.1",
        "job_id": "ej1_" + "a" * 32,
        "state": "queued",
        "stage": "queued",
        "elapsed_seconds": 0.0,
        "cancel_requested": False,
        "receipt": None,
        "response_state": None,
        "error": None,
        **changes,
    }


def test_execution_job_contract_exists():
    assert importlib.util.find_spec("openreading.types.execution_job") is not None


@pytest.mark.parametrize("response", ["succeeded", "partial", "failed", "processing"])
def test_retained_failed_response_is_successful_job_publication(response):
    value = status(state="succeeded", stage="complete", receipt=receipt(), response_state=response)
    assert models().ExecutionJob.model_validate(value).wire() == value
    from openreading.schemas import execution_job_schema

    Draft202012Validator(execution_job_schema()).validate(value)


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "running"},
        {"stage": "complete"},
        {"receipt": receipt()},
        {"response_state": "failed"},
        {"state": "failed", "stage": "stopped"},
        {"state": "succeeded", "stage": "complete", "receipt": receipt()},
        {"state": "cancelled", "stage": "stopped", "error": {"code": "cancelled"}},
        {"state": "failed", "stage": "stopped", "error": {"code": "cancelled"}},
        {"state": "running", "stage": "executing", "error": {"code": "execution_failed"}},
        {"elapsed_seconds": -1.0},
        {"job_id": "j1_" + "a" * 32},
        {"error": {"code": "arbitrary provider secret"}},
        {"source_path": "private"},
        {
            "state": "succeeded",
            "stage": "complete",
            "receipt": {**receipt(), "kind": "comparison_report"},
            "response_state": "succeeded",
        },
    ],
)
def test_invalid_states_refused_by_model_and_vendored_contract(changes):
    from openreading.schemas import execution_job_schema

    value = status(**changes)
    with pytest.raises(ValidationError):
        models().ExecutionJob.model_validate(value)
    assert not Draft202012Validator(execution_job_schema()).is_valid(value)


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"state": "running", "stage": "executing"},
        {"state": "running", "stage": "publishing", "cancel_requested": True},
        {"state": "failed", "stage": "stopped", "error": {"code": "interrupted"}},
        {
            "state": "cancelled",
            "stage": "stopped",
            "cancel_requested": True,
            "error": {"code": "cancelled"},
        },
    ],
)
def test_lifecycle_shapes_roundtrip(changes):
    from openreading.schemas import execution_job_schema

    value = status(**changes)
    assert models().ExecutionJob.model_validate(value).wire() == value
    Draft202012Validator(execution_job_schema()).validate(value)


def test_schema_matches_generated_contract_and_is_closed():
    from openreading.schemas import execution_job_schema

    expected = models().execution_job_contract()
    assert execution_job_schema() == expected
    assert expected["$defs"]["ExecutionJob"]["additionalProperties"] is False
    assert expected["$defs"]["ExecutionJobList"]["properties"]["jobs"]["maxItems"] == 50
    for wait in [True, -1, 21, float("inf")]:
        with pytest.raises(ValidationError):
            models().ExecutionJobGet(job_id="ej1_" + "a" * 32, wait_seconds=wait)
    for limit in [True, 0, 51]:
        with pytest.raises(ValidationError):
            models().ExecutionJobListRequest(limit=limit)


@pytest.mark.parametrize("field", ["receipt", "response_state", "error", "cancel_requested"])
def test_wire_status_requires_explicit_absence(field):
    from openreading.schemas import execution_job_schema

    value = status()
    del value[field]
    assert not Draft202012Validator(execution_job_schema()).is_valid(value)
