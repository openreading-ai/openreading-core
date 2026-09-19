"""Bind complete results to explicit producer provenance without inventing citations.

retained-result.v0.4 owns storage, and result-tool.v0.4 owns retrieval payloads.
The reader preserves v0.1 through v0.3 records without changing their canonical bytes.
A result contains a response.v0.3, comparison-report.v0.2, batch-result.v0.2, or corpus-report.v0.1 payload.
Batch retention validates each nested response and rejects its direct backend_raw field.
Typed fields named backend_raw remain intact, including their nested null values.
Request and configuration fingerprints identify producer inputs without retaining credentials.
Source hashes and adapter versions are supplied by the trusted producer, not measured here.
Unknown adapter versions remain null. Normalized-response hashes remain producer assertions.
Attributed comparison hashes are checked against retained inputs during publication.
Comparison subject labels map to retained identifiers under the same input grant.
For example, synthetic and synthetic#2 can identify two separate runs of the same adapter.
Attributed comparisons use v0.2 records and map each label to hashes plus a verification basis.
Legacy aggregates remain unattributed; reading an old record never invents per-subject verification.
Scored comparisons and corpus reports use v0.4 records; older readers cannot consume those new shapes.
Scored provenance retains exact caller-supplied expected values without independently certifying their truth.
Corpus subjects reference retained batches; nested comparison reports validate independently.
The result-tool v0.4 schema accepts every content shape; unchanged receipt fields retain version 0.1.
Report paths identify report values, never physical source pages or new citation evidence.
"""

from __future__ import annotations

from typing import Annotated, Literal, cast

from pydantic import ConfigDict, Field, JsonValue, model_validator
from pydantic.config import JsonDict

from openreading.artifacts.constants import MAX_CURSOR_CHARS
from openreading.artifacts.document import TextFragment, ValueFragment
from openreading.artifacts.models import Digest, ErrorEnvelope, WireModel
from openreading.schemas import (
    validate_batch_result,
    validate_comparison_report,
    validate_corpus_report,
    validate_response,
)

ResultId = Annotated[str, Field(pattern=r"^orr1_[0-9a-f]{64}$")]
InputResultId = Annotated[str, Field(pattern=r"^(or1|orr1)_[0-9a-f]{64}$")]
ResultKind = Literal["normalized_response", "comparison_report", "batch_result", "corpus_report"]


class ResultProvenance(WireModel):
    """Explicit producer assertions, with configuration values represented only by fingerprints."""

    request_sha256: Digest
    config_sha256: Digest
    core_version: str = Field(min_length=1)
    core_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")] | None
    adapters: dict[str, str | None]
    source_sha256: list[Digest]
    subjects: dict[str, InputResultId]


class SubjectSource(WireModel):
    """Source hashes attributed to one subject, with the producer's verification basis."""

    source_sha256: list[Digest]
    verification: Literal["verified_source_bytes", "producer_asserted"] = Field(
        description=(
            "verified_source_bytes means the retained source copy was rehashed against its "
            "manifest digest when the comparison producer loaded it. It does not check whether "
            "the user's original file still matches or establish that subjects share a document. "
            "producer_asserted preserves the normalized-response producer's hash claims without "
            "independent source-byte verification. Reading a retained comparison does not rehash "
            "its subjects' source copies again."
        )
    )


class AttributedResultProvenance(ResultProvenance):
    """Comparison attribution supplements the legacy aggregate without upgrading its claims."""

    subject_sources: dict[str, SubjectSource]


class ScoredResultProvenance(AttributedResultProvenance):
    """Caller-supplied expected values retained exactly, without an independent truth claim."""

    truth: dict[str, JsonValue] = Field(
        description=(
            "Exact expected values supplied by the caller to the shared scorer. "
            "The record digest binds these values; no independent truth verification is claimed."
        )
    )


class ResultContent(WireModel):
    """Complete normalized values with their producer identity, without backend_raw."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra=cast(
            JsonDict,
            {
                "allOf": [
                    {
                        "if": {"properties": {"kind": {"const": kind}}},
                        "then": {"properties": {"payload": {"$ref": reference}}},
                    }
                    for kind, reference in (
                        (
                            "normalized_response",
                            "https://openreading.ai/schemas/response/v0.3.json",
                        ),
                        (
                            "comparison_report",
                            "https://openreading.ai/schemas/comparison-report.v0.2.json",
                        ),
                        ("batch_result", "https://openreading.ai/schemas/batch-result.v0.2.json"),
                        ("corpus_report", "https://openreading.ai/schemas/corpus-report.v0.1.json"),
                    )
                ]
                + [
                    {
                        "if": {"properties": {"kind": {"const": "corpus_report"}}},
                        "then": {
                            "properties": {
                                "provenance": {
                                    "required": ["subject_sources"],
                                    "not": {"required": ["truth"]},
                                },
                                "payload": {
                                    "properties": {
                                        "documents": {
                                            "items": {
                                                "properties": {
                                                    "report": {
                                                        "anyOf": [
                                                            {"type": "null"},
                                                            {
                                                                "$ref": "https://openreading.ai/schemas/comparison-report.v0.2.json"
                                                            },
                                                        ]
                                                    }
                                                }
                                            }
                                        }
                                    }
                                },
                            }
                        },
                    },
                    {
                        "if": {"properties": {"kind": {"const": "batch_result"}}},
                        "then": {
                            "properties": {
                                "provenance": {
                                    "properties": {"subjects": {"maxProperties": 0}},
                                    "not": {"required": ["subject_sources"]},
                                },
                                "payload": {
                                    "properties": {
                                        "items": {
                                            "items": {
                                                "properties": {
                                                    "response": {
                                                        "anyOf": [
                                                            {"type": "null"},
                                                            {
                                                                "$ref": "https://openreading.ai/schemas/response/v0.3.json",
                                                                "not": {
                                                                    "required": ["backend_raw"]
                                                                },
                                                            },
                                                        ]
                                                    }
                                                }
                                            }
                                        }
                                    }
                                },
                            }
                        },
                    },
                ]
            },
        ),
    )
    kind: ResultKind
    provenance: ScoredResultProvenance | AttributedResultProvenance | ResultProvenance
    payload: dict[str, JsonValue]

    @model_validator(mode="after")
    def validate_payload(self) -> ResultContent:
        if self.kind == "normalized_response":
            validate_response(self.payload)
            if (
                "backend_raw" in self.payload
                or self.provenance.subjects
                or isinstance(self.provenance, AttributedResultProvenance)
            ):
                raise ValueError("Normalized results exclude raw output and comparison subjects")
        elif self.kind == "batch_result":
            validate_batch_result(self.payload)
            if self.provenance.subjects or isinstance(self.provenance, AttributedResultProvenance):
                raise ValueError("Batch results exclude comparison subjects")
            items = self.payload["items"]
            assert isinstance(items, list)
            for item in items:
                assert isinstance(item, dict)
                response = item.get("response")
                if response is not None:
                    assert isinstance(response, dict)
                    validate_response(response)
                    if "backend_raw" in response:
                        raise ValueError("Batch responses exclude raw output")
        else:
            if self.kind == "corpus_report":
                validate_corpus_report(self.payload)
                documents = self.payload["documents"]
                assert isinstance(documents, list)
                for document in documents:
                    assert isinstance(document, dict)
                    report = document.get("report")
                    if report is not None:
                        assert isinstance(report, dict)
                        validate_comparison_report(report)
                if (
                    not isinstance(self.provenance, AttributedResultProvenance)
                    or isinstance(self.provenance, ScoredResultProvenance)
                    or any(ref.startswith("or1_") for ref in self.provenance.subjects.values())
                ):
                    raise ValueError(
                        "Corpus subjects require attributed retained batches without truth"
                    )
            else:
                validate_comparison_report(self.payload)
                if isinstance(self.provenance, ScoredResultProvenance):
                    truth = self.payload.get("truth")
                    if not isinstance(truth, dict) or truth.get("dimensions") != sorted(
                        self.provenance.truth
                    ):
                        raise ValueError(
                            "Scored reports require dimensions matching supplied expected values"
                        )
            subjects = self.payload["subjects"]
            assert isinstance(subjects, list)
            labels = [cast(str, s["label"]) for s in subjects if isinstance(s, dict)]
            if len(set(labels)) != len(labels) or set(labels) != set(self.provenance.subjects):
                raise ValueError("Every report subject requires one retained input identifier")
            if isinstance(self.provenance, AttributedResultProvenance):
                sources = self.provenance.subject_sources
                if set(sources) != set(labels):
                    raise ValueError("Source attribution must cover every comparison subject")
                hashes = []
                for label in labels:
                    source = sources[label]
                    local = self.provenance.subjects[label].startswith("or1_")
                    if local != (source.verification == "verified_source_bytes") or (
                        local and len(source.source_sha256) != 1
                    ):
                        raise ValueError("Source verification must match the retained input kind")
                    hashes.extend(source.source_sha256)
                if hashes != self.provenance.source_sha256:
                    raise ValueError("Aggregate hashes must preserve report subject order")
        return self

    def record_format(
        self,
    ) -> Literal[
        "retained-result.v0.1",
        "retained-result.v0.2",
        "retained-result.v0.3",
        "retained-result.v0.4",
    ]:
        """Preserve historical record identities while assigning new shapes their own version."""
        if self.kind == "corpus_report" or isinstance(self.provenance, ScoredResultProvenance):
            return "retained-result.v0.4"
        if self.kind == "batch_result":
            return "retained-result.v0.3"
        if isinstance(self.provenance, AttributedResultProvenance):
            return "retained-result.v0.2"
        return "retained-result.v0.1"

    def wire(self) -> dict:
        # Null values in payloads and unknown adapter versions are retained facts.
        return self.model_dump(mode="json")


class ResultRecord(WireModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"format": {"const": version}}},
                    "then": {"properties": {"content": constraints}},
                }
                for version, constraints in {
                    "retained-result.v0.1": {
                        "properties": {
                            "kind": {"enum": ["normalized_response", "comparison_report"]},
                            "provenance": {"not": {"required": ["subject_sources"]}},
                        }
                    },
                    "retained-result.v0.2": {
                        "properties": {
                            "kind": {"const": "comparison_report"},
                            "provenance": {
                                "required": ["subject_sources"],
                                "not": {"required": ["truth"]},
                            },
                        }
                    },
                    "retained-result.v0.3": {"properties": {"kind": {"const": "batch_result"}}},
                    "retained-result.v0.4": {
                        "oneOf": [
                            {
                                "properties": {
                                    "kind": {"const": "comparison_report"},
                                    "provenance": {"required": ["truth", "subject_sources"]},
                                }
                            },
                            {
                                "properties": {
                                    "kind": {"const": "corpus_report"},
                                    "provenance": {
                                        "required": ["subject_sources"],
                                        "not": {"required": ["truth"]},
                                    },
                                }
                            },
                        ]
                    },
                }.items()
            ]
        },
    )
    format: Literal[
        "retained-result.v0.1",
        "retained-result.v0.2",
        "retained-result.v0.3",
        "retained-result.v0.4",
    ] = "retained-result.v0.4"
    input_grant_sha256: Digest
    content: ResultContent

    @model_validator(mode="after")
    def validate_format(self) -> ResultRecord:
        if self.format != self.content.record_format():
            raise ValueError("Record format must match its content kind and provenance")
        return self

    def wire(self) -> dict:
        return self.model_dump(mode="json")


class ResultReceipt(WireModel):
    schema_version: Literal["0.1"] = "0.1"
    result_id: ResultId
    kind: Literal["normalized_response", "comparison_report", "batch_result"]
    content_bytes: int = Field(ge=1)
    content_sha256: Digest


class RetrievalReceipt(ResultReceipt):
    """Retrieval also serves corpus reports without widening execution-job receipts."""

    kind: ResultKind


class ResultRequest(WireModel):
    result_id: ResultId
    delivery: Literal["auto", "file", "fragments"] = "auto"
    cursor: str | None = Field(default=None, max_length=MAX_CURSOR_CHARS)


class CompleteResult(RetrievalReceipt):
    delivery: Literal["tool_result"] = "tool_result"
    content: ResultContent

    def wire(self) -> dict:
        return self.model_dump(mode="json")


class FileResult(RetrievalReceipt):
    delivery: Literal["local_file"] = "local_file"
    local_path: str
    next_action: str


class FragmentResult(RetrievalReceipt):
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
    Field(title="OpenReading Result Tool v0.4"),
]
