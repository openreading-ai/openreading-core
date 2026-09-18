"""Describe grant-scoped general execution jobs independently of local artifact imports.

A job succeeds when it retains a complete normalized response and returns its result receipt.
The separate response_state preserves the provider outcome, including partial, failed or still-processing extraction.
For example, a retained failed response has state succeeded and response_state failed.
Only succeeded jobs carry receipts; failed and cancelled jobs carry fixed lifecycle error codes.
No source path, document text, provider diagnostic or credential appears in public status.

Stages report observed work rather than estimated progress. Cancellation requests can race publication.
A committed result wins that race, so cancel_requested does not override a succeeded terminal state.
Lists contain at most fifty summaries; unavailable means a retained job could not be validated.
These contracts support the supervisor and general MCP tools, with tool failures defined in openreading.types.execution_tool.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from openreading.artifacts.result_models import ResultReceipt

ExecutionJobId = Annotated[str, Field(pattern=r"^ej1_[0-9a-f]{32}$")]
ExecutionState = Literal["queued", "running", "succeeded", "failed", "cancelled"]
ResponseOutcome = Literal["succeeded", "partial", "failed", "processing"]
ExecutionStage = Literal["queued", "executing", "publishing", "complete", "stopped"]
ExecutionFaultCode = Literal[
    "execution_failed",
    "source_changed",
    "cancelled",
    "timeout",
    "os_permission_denied",
    "interrupted",
    "access_denied",
    "input_not_found",
    "storage_limit",
    "scope_denied",
    "invalid_request",
    "invalid_configuration",
]


class JobWire(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    def wire(self) -> dict:
        return self.model_dump(mode="json")


class ExecutionFault(JobWire):
    code: ExecutionFaultCode


def _invariants() -> list[dict]:
    return [
        {
            "if": {"properties": {"state": {"const": state}}},
            "then": {
                "properties": {
                    "stage": {"enum": stages},
                    "receipt": {"type": "object" if state == "succeeded" else "null"},
                    "response_state": {"type": "string" if state == "succeeded" else "null"},
                    "error": {"type": "object" if state in {"failed", "cancelled"} else "null"},
                    **({"cancel_requested": {"const": True}} if state == "cancelled" else {}),
                }
            },
        }
        for state, stages in {
            "queued": ["queued"],
            "running": ["executing", "publishing"],
            "succeeded": ["complete"],
            "failed": ["stopped"],
            "cancelled": ["stopped"],
        }.items()
    ] + [
        {
            "if": {"properties": {"state": {"const": "succeeded"}}},
            "then": {
                "properties": {
                    "receipt": {"properties": {"kind": {"const": "normalized_response"}}}
                }
            },
        },
        {
            "if": {"properties": {"state": {"const": "cancelled"}}},
            "then": {"properties": {"error": {"properties": {"code": {"const": "cancelled"}}}}},
            "else": {
                "properties": {
                    "error": {
                        "not": {"type": "object", "properties": {"code": {"const": "cancelled"}}}
                    }
                }
            },
        },
    ]


def _job_schema(schema: dict) -> None:
    schema["allOf"] = _invariants()
    schema["required"] = [
        "schema_version",
        "job_id",
        "state",
        "stage",
        "elapsed_seconds",
        "cancel_requested",
        "receipt",
        "response_state",
        "error",
    ]


class ExecutionJob(JobWire):
    model_config = ConfigDict(extra="forbid", strict=True, json_schema_extra=_job_schema)

    schema_version: Literal["0.1"] = "0.1"
    job_id: ExecutionJobId
    state: ExecutionState
    stage: ExecutionStage
    elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)
    cancel_requested: bool = False
    receipt: ResultReceipt | None = None
    response_state: ResponseOutcome | None = None
    error: ExecutionFault | None = None

    @model_validator(mode="after")
    def lifecycle(self):
        stages = {
            "queued": {"queued"},
            "running": {"executing", "publishing"},
            "succeeded": {"complete"},
            "failed": {"stopped"},
            "cancelled": {"stopped"},
        }
        if self.stage not in stages[self.state]:
            raise ValueError("Stage must describe the job lifecycle state.")
        success = self.state == "succeeded"
        if success != (self.receipt is not None) or success != (self.response_state is not None):
            raise ValueError("Only published results carry receipts and response outcomes.")
        if self.receipt is not None and self.receipt.kind != "normalized_response":
            raise ValueError("Execution jobs retain normalized responses.")
        if (self.state in {"failed", "cancelled"}) != (self.error is not None):
            raise ValueError("Stopped jobs require a lifecycle error.")
        if (self.state == "cancelled") != (
            self.error is not None and self.error.code == "cancelled"
        ):
            raise ValueError("Cancellation status requires its cancellation error.")
        if self.state == "cancelled" and not self.cancel_requested:
            raise ValueError("Cancelled jobs retain the observed cancellation request.")
        return self


class ExecutionJobSummary(JobWire):
    job_id: ExecutionJobId
    state: ExecutionState | Literal["unavailable"]
    elapsed_seconds: float | None = Field(ge=0, allow_inf_nan=False)


class ExecutionJobList(JobWire):
    schema_version: Literal["0.1"] = "0.1"
    jobs: list[ExecutionJobSummary] = Field(max_length=50)
    next_cursor: str | None = Field(default=None, max_length=512)


class ExecutionJobGet(JobWire):
    job_id: ExecutionJobId
    wait_seconds: float = Field(default=0, ge=0, le=20, allow_inf_nan=False)


class ExecutionJobCancel(JobWire):
    job_id: ExecutionJobId


class ExecutionJobListRequest(JobWire):
    limit: int = Field(default=20, ge=1, le=50)
    cursor: str | None = Field(default=None, max_length=512)


def execution_job_contract() -> dict:
    """Generate the self-contained public lifecycle contract and its closed lookup requests."""
    schema = TypeAdapter(
        ExecutionJob
        | ExecutionJobList
        | ExecutionJobGet
        | ExecutionJobCancel
        | ExecutionJobListRequest
    ).json_schema()
    schema.update(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://openreading.ai/schemas/execution-job.v0.1.json",
            "title": "OpenReading Execution Job v0.1",
        }
    )
    return schema
