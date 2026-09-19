"""Define general parse acceptance and fixed job-operation failures for MCP clients.

Batch accepts an ordered requests array of the same restricted shared requests, including an empty array.
Parse accepts the shared request shape restricted to grant-relative paths and operator-owned runtime configuration.
For example, backend.id strategy:local selects an authorized strategy without accepting caller credentials or document URLs.
Resume accepts a prior job_id and an optional zero-based batch item_index, never arbitrary ledger paths.
Version 0.3 adds resume admission while retaining the existing job status wire shape.
The retained execution-job contract supplies status and lookup shapes without changing historical lifecycle records.
Successful acceptance starts detached work, while successful publication preserves the separate provider response state.
"""

from copy import deepcopy
from typing import Literal

from pydantic import Field

from openreading.artifacts.models import ErrorEnvelope
from openreading.schemas import execution_job_schema, request_schema
from openreading.types.execution_job import ExecutionFaultCode, JobWire

ExecutionToolCode = Literal[
    ExecutionFaultCode,
    "job_not_found",
    "job_state_invalid",
    "job_start_failed",
    "resume_unavailable",
    "response_too_large",
]


class ExecutionToolFault(JobWire):
    code: ExecutionToolCode


class ExecutionToolFailure(JobWire):
    schema_version: Literal["0.1"] = "0.1"
    error: ExecutionToolFault


class ResumeRequest(JobWire):
    job_id: str = Field(pattern=r"^ej1_[0-9a-f]{32}$")
    item_index: int | None = Field(default=None, ge=0, strict=True)


def execution_tool_contract() -> dict:
    """Restrict acquisition and runtime authority without rewriting the shared request contract."""
    request = deepcopy(request_schema())
    for key in ("$schema", "$id"):
        request.pop(key, None)
    request["title"] = "ParseRequest"
    properties = request["properties"]
    for name in ("async", "idempotency_key"):
        properties.pop(name, None)
    document = properties["document"]
    document.pop("oneOf")
    document["required"] = ["path"]
    document["properties"] = {
        name: value
        for name, value in document["properties"].items()
        if name in {"path", "filename", "mime_type"}
    }
    document["properties"]["path"] = {"type": "string", "minLength": 1}
    document["description"] = "A relative source reference beneath the operator's input grant."
    for name in ("credentials_ref", "runtime"):
        properties["backend"]["properties"].pop(name, None)
    schema = deepcopy(execution_job_schema())
    failure = ExecutionToolFailure.model_json_schema()
    schema["$defs"].update(failure.pop("$defs"))
    envelope = ErrorEnvelope.model_json_schema()
    schema["$defs"].update(envelope.pop("$defs"))
    schema["$defs"].update(
        ParseRequest=request,
        ResumeRequest=ResumeRequest.model_json_schema(),
        BatchRequest={
            "type": "object",
            "additionalProperties": False,
            "required": ["requests"],
            "properties": {
                "requests": {"type": "array", "items": {"$ref": "#/$defs/ParseRequest"}}
            },
        },
        ExecutionToolFailure=failure,
        ErrorEnvelope=envelope,
    )
    schema["anyOf"].extend(
        [
            {"$ref": "#/$defs/ParseRequest"},
            {"$ref": "#/$defs/BatchRequest"},
            {"$ref": "#/$defs/ResumeRequest"},
            {"$ref": "#/$defs/ExecutionToolFailure"},
            {"$ref": "#/$defs/ErrorEnvelope"},
        ]
    )
    schema["$id"] = "https://openreading.ai/schemas/execution-tool.v0.3.json"
    schema["title"] = "OpenReading Execution Tool v0.3"
    return schema
