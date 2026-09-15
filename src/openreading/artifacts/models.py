"""Strict constructors for retained documents and bounded agent tool payloads.

The three vendored v0.3 schemas own these contracts. Artifact identity excludes names
and creation time, so identical bytes and engine settings reuse evidence identifiers.
Character offsets count Unicode code points, relative to the original normalized block.
Geometry identifies the enclosing block, never an inferred character highlight.
Page origin none means no retained text; unknown means extraction origin was not measured.
Only pages can have origin none. A nonempty passage cannot claim absence of text.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openreading.artifacts.constants import (
    MAX_CURSOR_CHARS,
    MAX_EXCERPT_CHARS,
    MAX_PASSAGE_CHARS,
    MAX_QUERY_CHARS,
)
from openreading.types.geometry import BBox

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ArtifactId = Annotated[str, Field(pattern=r"^or1_[0-9a-f]{64}$")]
EvidenceId = Annotated[str, Field(pattern=r"^p[0-9]{4,}-b[0-9]{4,}-s[0-9]{4,}$")]
WarningCode = Literal["source_changed", "parser_warnings_present", "no_matches"]
TextOrigin = Literal["native", "ocr", "mixed", "unknown"]
PageOrigin = Literal["native", "ocr", "mixed", "unknown", "none"]
ErrorCode = Literal[
    "memory_limit",
    "worker_monitor_failed",
    "os_permission_denied",
    "configuration_required",
    "engine_identity_unavailable",
    "access_denied",
    "input_not_found",
    "unsupported_format",
    "password_required",
    "input_too_large",
    "extraction_too_large",
    "no_readable_text",
    "busy",
    "timeout",
    "cancelled",
    "storage_limit",
    "parse_failed",
    "artifact_not_found",
    "artifact_corrupt",
    "artifact_version_unsupported",
    "evidence_not_found",
    "invalid_cursor",
    "response_too_large",
]


def json_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    def wire(self) -> dict:
        result = self.model_dump(mode="json", exclude_none=True)
        if "next_cursor" in type(self).model_fields:
            result["next_cursor"] = self.model_dump()["next_cursor"]
        return result


class EngineIdentity(WireModel):
    core_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")] | None = None
    core_version: str
    backend_id: str = Field(default="pymupdf", min_length=1)
    backend_version: str
    extraction_settings: dict[str, object]


class FileRecord(WireModel):
    length: int = Field(ge=0)
    sha256: Digest


class ArtifactManifest(WireModel):
    format: Literal["local-document.v0.3"] = "local-document.v0.3"
    artifact_id: ArtifactId
    document_sha256: Digest
    display_name: str
    source_relative_path: str
    input_grant_sha256: Digest
    page_count: int = Field(ge=1)
    passage_count: int = Field(ge=1)
    engine: EngineIdentity
    evidence_format: Literal["passages.v0.3"] = "passages.v0.3"
    created_at: str
    page_origins: dict[str, PageOrigin] = Field(default_factory=dict)
    files: dict[str, FileRecord]
    warnings: list[WarningCode] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_files(self) -> ArtifactManifest:
        if self.page_origins and (
            len(self.page_origins) != self.page_count
            or any(
                not key.isascii()
                or not key.isdigit()
                or str(int(key)) != key
                or not 1 <= int(key) <= self.page_count
                for key in self.page_origins
            )
        ):
            raise ValueError("Text origins must cover exactly the physical pages")
        if set(self.files) != {"source.pdf", "response.json", "passages.jsonl"}:
            raise ValueError("Unexpected artifact files")
        if len(self.display_name.encode("utf-8")) > 255:
            raise ValueError("Display name exceeds byte limit")
        return self


def artifact_id(document_sha256: str, engine: EngineIdentity) -> str:
    identity = {
        "format": "local-document.v0.3",
        "document_sha256": document_sha256,
        "engine": engine.wire(),
        "evidence_format": "passages.v0.3",
    }
    return "or1_" + hashlib.sha256(json_bytes(identity)).hexdigest()


class Passage(WireModel):
    evidence_id: EvidenceId
    page: int = Field(ge=1)
    block_index: int = Field(ge=0)
    segment_index: int = Field(ge=0)
    text_origin: TextOrigin | None = None
    source_kind: Literal["block_text", "page_text"]
    text_start: int = Field(ge=0)
    text_end: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=MAX_PASSAGE_CHARS)
    bbox: BBox | None = None
    source_block_id: str | None = None

    @model_validator(mode="after")
    def validate_span(self) -> Passage:
        if self.text_end - self.text_start != len(self.text):
            raise ValueError("Span length differs from text")
        expected = f"p{self.page:04d}-b{self.block_index:04d}-s{self.segment_index:04d}"
        if self.evidence_id != expected:
            raise ValueError("Evidence identifier differs from position")
        if self.bbox is not None and self.bbox.page != self.page:
            raise ValueError("Geometry belongs to another page")
        if self.source_kind == "page_text" and self.bbox is not None:
            raise ValueError("Page fallback cannot infer geometry")
        return self


class ImportReceipt(WireModel):
    schema_version: Literal["0.3"] = "0.3"
    artifact_id: ArtifactId
    display_name: str
    document_sha256: Digest
    page_count: int = Field(ge=1)
    passage_count: int = Field(ge=1)
    reused: bool
    warnings: list[WarningCode]
    next_action: Literal["search"] = "search"


class SearchHit(WireModel):
    text_origin: TextOrigin | None = None
    evidence_id: EvidenceId
    page: int = Field(ge=1)
    matched_terms: list[str]
    excerpt_start: int = Field(ge=0)
    excerpt_end: int = Field(ge=1)
    excerpt: str = Field(min_length=1, max_length=MAX_EXCERPT_CHARS)


class SearchResult(WireModel):
    schema_version: Literal["0.3"] = "0.3"
    artifact_id: ArtifactId
    query: str = Field(max_length=MAX_QUERY_CHARS)
    hits: list[SearchHit]
    next_cursor: str | None = Field(default=None, max_length=MAX_CURSOR_CHARS)
    warnings: list[WarningCode] = Field(default_factory=list)


class ReadResult(WireModel):
    schema_version: Literal["0.3"] = "0.3"
    artifact_id: ArtifactId
    display_name: str
    passages: list[Passage]
    next_cursor: str | None = Field(default=None, max_length=MAX_CURSOR_CHARS)
    warnings: list[WarningCode] = Field(default_factory=list)


class ToolError(WireModel):
    code: ErrorCode
    message: str = Field(max_length=256)
    retryable: bool


class ErrorEnvelope(WireModel):
    schema_version: Literal["0.3"] = "0.3"
    error: ToolError
