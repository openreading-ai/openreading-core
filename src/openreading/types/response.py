"""NormalizedResponse — the pydantic mirror of response.v0.3.json.

`to_schema_dict()` produces a dict that validates against the vendored response schema
(enforced by tests). Enums serialize to their string values; None fields are dropped so
we never emit `"text": null` against a `type: string` slot.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from openreading.types.blocks import Block, Chunk, TypedField
from openreading.types.enums import BackendType, OutputParadigm, PageUnit, ResponseState


class ResponseError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str | None = None
    message: str | None = None
    backend_code: str | None = None


class Status(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: ResponseState
    error: ResponseError | None = None


class BackendInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: BackendType
    operation: str | None = None
    version: str | None = None
    output_paradigm: list[OutputParadigm] | None = None


class DocType(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)  # C7: [0,1]
    description: str | None = None


class Page(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_number: int = Field(ge=1)
    width: float | None = None
    height: float | None = None
    unit: PageUnit | None = None
    dpi: float | None = None
    rotation: float | None = None
    markdown: str | None = None
    text: str | None = None
    blocks: list[Block] | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)  # C7: [0,1]
    image_base64: str | None = None
    image_url: str | None = None
    source_backend: str | None = None  # v0.2 — page-granularity escalation provenance


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid")

    markdown: str | None = None
    text: str | None = None
    page_count: int | None = None
    language: list[str] | None = None
    doc_type: DocType | None = None
    pages: list[Page] | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)  # v0.3 — document-level (C7)


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pages_processed: int | None = None
    credits: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    duration_ms: int | None = None


class JobHandle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    created_at: str | None = None
    finished_at: str | None = None
    poll_url: str | None = None
    studio_url: str | None = None


class Warning(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str | None = None
    message: str | None = None
    field: str | None = None


class BackendRaw(BaseModel):
    model_config = ConfigDict(extra="forbid")

    encoding: str | None = None
    media_type: str | None = None
    payload: Any = None
    object_class: str | None = None
    truncated: bool | None = None


class NormalizedResponse(BaseModel):
    """Pydantic mirror of OpenReadingResponse. Build it in adapters, then `to_schema_dict()`."""

    # extra="ignore" on the ENVELOPE (not the nested payload models) makes an old package
    # forward-tolerant of a newer producer's additive top-level fields (§8 "unknown-field
    # tolerance is contract"; mechanized by the §7 forward-tolerance gate). "ignore" (not
    # "allow") drops unknowns on re-serialization so to_schema_dict() output never carries an
    # unvalidated field. Nested models keep extra="forbid" so adapter-construction typos in the
    # structural payload still raise.
    model_config = ConfigDict(extra="ignore")

    schema_version: str = "0.3"
    status: Status
    backend: BackendInfo
    document: Document
    typed_fields: dict[str, TypedField] | None = None
    chunks: list[Chunk] | None = None
    usage: Usage | None = None
    job: JobHandle | None = None
    warnings: list[Warning] | None = None
    backend_raw: BackendRaw | None = None
    orchestration: dict[str, Any] | None = None  # v0.2 — strategy execution trace (strategy runs)
    channel_provenance: dict[str, str] | None = None  # v0.3 — per-response native/derived (§3.3)
    schema_url: str | None = None  # v0.3 — $id of the declared schema version (OTel pattern)

    def to_schema_dict(self) -> dict[str, Any]:
        """Schema-valid JSON dict: enums→values, None dropped, empty containers kept only
        where they carry meaning (e.g. an explicitly-empty page.blocks list)."""
        return self.model_dump(mode="json", exclude_none=True)

    def add_warning(self, code: str, message: str, field: str | None = None) -> None:
        w = Warning(code=code, message=message, field=field)
        if self.warnings is None:
            self.warnings = []
        self.warnings.append(w)
