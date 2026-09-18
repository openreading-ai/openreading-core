"""Bind complete results to explicit producer provenance without inventing citations.

retained-result.v0.1 owns storage, and result-tool.v0.1 owns retrieval payloads.
A result contains either a response.v0.3 envelope or a comparison-report.v0.2 report.
Request and configuration fingerprints identify producer inputs without retaining credentials.
Source hashes and adapter versions are supplied by the trusted producer, not measured here.
Unknown adapter versions remain null. Storage cannot establish that a producer reported them accurately.
Comparison subject labels map to retained identifiers under the same input grant.
For example, synthetic and synthetic#2 can identify two separate runs of the same adapter.
Report paths identify report values, never physical source pages or new citation evidence.
"""

from __future__ import annotations

from typing import Annotated, Literal, cast

from pydantic import ConfigDict, Field, JsonValue, model_validator

from openreading.artifacts.constants import MAX_CURSOR_CHARS
from openreading.artifacts.document import TextFragment, ValueFragment
from openreading.artifacts.models import Digest, ErrorEnvelope, WireModel
from openreading.schemas import validate_comparison_report, validate_response

ResultId = Annotated[str, Field(pattern=r"^orr1_[0-9a-f]{64}$")]
InputResultId = Annotated[str, Field(pattern=r"^(or1|orr1)_[0-9a-f]{64}$")]
ResultKind = Literal["normalized_response", "comparison_report"]


class ResultProvenance(WireModel):
    """Explicit producer assertions, with configuration values represented only by fingerprints."""

    request_sha256: Digest
    config_sha256: Digest
    core_version: str = Field(min_length=1)
    core_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")] | None
    adapters: dict[str, str | None]
    source_sha256: list[Digest]
    subjects: dict[str, InputResultId]


class ResultContent(WireModel):
    """Complete normalized values with their producer identity, without backend_raw."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"kind": {"const": kind}}},
                    "then": {"properties": {"payload": {"$ref": reference}}},
                }
                for kind, reference in (
                    ("normalized_response", "https://openreading.ai/schemas/response/v0.3.json"),
                    (
                        "comparison_report",
                        "https://openreading.ai/schemas/comparison-report.v0.2.json",
                    ),
                )
            ]
        },
    )
    kind: ResultKind
    provenance: ResultProvenance
    payload: dict[str, JsonValue]

    @model_validator(mode="after")
    def validate_payload(self) -> ResultContent:
        if self.kind == "normalized_response":
            validate_response(self.payload)
            if "backend_raw" in self.payload or self.provenance.subjects:
                raise ValueError("Normalized results exclude raw output and comparison subjects")
        else:
            validate_comparison_report(self.payload)
            subjects = self.payload["subjects"]
            assert isinstance(subjects, list)
            labels = [cast(str, s["label"]) for s in subjects if isinstance(s, dict)]
            if len(set(labels)) != len(labels) or set(labels) != set(self.provenance.subjects):
                raise ValueError("Every report subject requires one retained input identifier")
        return self

    def wire(self) -> dict:
        # Null values in payloads and unknown adapter versions are retained facts.
        return self.model_dump(mode="json")


class ResultRecord(WireModel):
    format: Literal["retained-result.v0.1"] = "retained-result.v0.1"
    input_grant_sha256: Digest
    content: ResultContent

    def wire(self) -> dict:
        return self.model_dump(mode="json")


class ResultReceipt(WireModel):
    schema_version: Literal["0.1"] = "0.1"
    result_id: ResultId
    kind: ResultKind
    content_bytes: int = Field(ge=1)
    content_sha256: Digest


class ResultRequest(WireModel):
    result_id: ResultId
    delivery: Literal["auto", "file", "fragments"] = "auto"
    cursor: str | None = Field(default=None, max_length=MAX_CURSOR_CHARS)


class CompleteResult(ResultReceipt):
    delivery: Literal["tool_result"] = "tool_result"
    content: ResultContent

    def wire(self) -> dict:
        return self.model_dump(mode="json")


class FileResult(ResultReceipt):
    delivery: Literal["local_file"] = "local_file"
    local_path: str
    next_action: str


class FragmentResult(ResultReceipt):
    delivery: Literal["fragments"] = "fragments"
    fragment_start: int = Field(ge=0)
    fragment_count: int = Field(ge=1)
    fragments: list[ValueFragment | TextFragment] = Field(min_length=1)
    next_cursor: str | None = Field(default=None, max_length=MAX_CURSOR_CHARS)

    def wire(self) -> dict:
        return self.model_dump(mode="json")


ResultErrorCode = Literal["result_not_found", "result_corrupt", "invalid_result"]
_MESSAGES: dict[ResultErrorCode, str] = {
    "result_not_found": "This result is unavailable under the current input grant.",
    "result_corrupt": "The retained result failed integrity validation. Restore it before retrieval.",
    "invalid_result": "The supplied result does not match its normalized contract or provenance.",
}


class ResultFault(WireModel):
    code: ResultErrorCode
    message: str
    retryable: Literal[False] = False


class ResultFailure(WireModel):
    schema_version: Literal["0.1"] = "0.1"
    error: ResultFault


class ResultError(Exception):
    def __init__(self, code: ResultErrorCode):
        self.code: ResultErrorCode = code
        super().__init__(code)

    def wire(self) -> dict:
        return ResultFailure(error=ResultFault(code=self.code, message=_MESSAGES[self.code])).wire()


ResultPayload = Annotated[
    ResultRequest | CompleteResult | FileResult | FragmentResult | ResultFailure | ErrorEnvelope,
    Field(title="OpenReading Result Tool v0.1"),
]
