"""OpenReadingRequest: the pydantic mirror of request.v0.3.json.

`async` is a Python keyword, so the field is `async_` with alias `"async"`; build requests
with `OpenReadingRequest(..., **{"async": {...}})` or set `.async_`. `to_schema_dict()`
emits the aliased, schema-valid form.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openreading.types.enums import BackendType


class DocumentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bytes_base64: str | None = None
    url: str | None = None
    path: str | None = None
    file_id: str | None = None
    mime_type: str | None = None
    filename: str | None = None
    password: str | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> DocumentInput:
        sources = [self.bytes_base64, self.url, self.path, self.file_id]
        set_count = sum(s is not None for s in sources)
        if set_count != 1:
            raise ValueError(
                "document requires exactly one of bytes_base64 | url | path | file_id "
                f"(got {set_count})"
            )
        return self


class BackendRuntime(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["in_process", "subprocess", "container", "remote_endpoint"] | None = None
    image: str | None = None
    endpoint: str | None = None
    device: Literal["cpu", "cuda", "mps", "auto"] | None = None
    system_deps_ok: bool | None = None


class BackendSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # `None` means the caller named no backend, so selection falls to `policy.backends` and then
    # to the documented default. It replaces the `"auto"` sentinel, which asked the engine to
    # infer from vendor claims it could not verify.
    id: str | None = None
    type: BackendType | None = None
    operation: str | None = None
    version: str | None = None
    credentials_ref: str | None = None
    runtime: BackendRuntime | None = None


class Chunking(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: Literal["none", "by_page", "by_title", "by_section", "by_similarity", "fixed"] = (
        "none"
    )
    max_characters: int | None = Field(default=None, ge=1)
    overlap: int | None = Field(default=None, ge=0)


class Outputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    markdown: bool = True
    text: bool = True
    blocks: bool = True
    typed_fields: bool = False
    tables: Literal["none", "cells", "markdown", "html"] = "markdown"
    chunking: Chunking | None = None
    include_backend_raw: bool = True


class ExtractionSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    json_schema: dict[str, Any] | None = None
    instructions: str | None = None
    citations: bool = False


class Features(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ocr: Literal["auto", "force", "off"] = "auto"
    ocr_languages: list[str] | None = None
    layout: bool = True
    reading_order: bool = True
    tables: bool = True
    forms_key_value: bool = False
    figures_images: bool = False
    signatures: bool = False
    classification: bool = False
    handwriting: bool = False


class PageRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: int = Field(ge=1)
    end: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _end_not_before_start(self) -> PageRange:
        # Cross-field numeric comparison is inexpressible in the vendored JSON Schema draft, so
        # this lives only here; the schema stays the wire contract for shapes, pydantic for this.
        if self.end is not None and self.end < self.start:
            raise ValueError(f"pages range end {self.end} is before start {self.start}")
        return self


class Pages(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ranges: list[PageRange] | None = None
    max_pages: int | None = Field(default=None, ge=1)


class Routing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_type_hint: (
        Literal[
            "bank_statement",
            "paystub",
            "w2",
            "1003_loan_app",
            "1040_tax",
            "invoice",
            "id_document",
            "medical_form",
            "clinical_pdf",
            "generic",
        ]
        | None
    ) = None
    fallback: list[str] | None = None


class AsyncSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["auto", "sync", "async"] = "auto"
    webhook_url: str | None = None


class OpenReadingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # "0.2": the newest request schema file's const (schemas.REQUEST_SCHEMA_FILE); C12 pins this
    # default to match it. Every nested model here was already extra="forbid" — v0.2 only taught
    # the wire schema the same rule, so this default bump carries no behavior change of its own.
    schema_version: str = "0.3"
    document: DocumentInput
    backend: BackendSpec
    outputs: Outputs | None = None
    extraction_schema: ExtractionSchema | None = None
    features: Features | None = None
    pages: Pages | None = None
    routing: Routing | None = None
    async_: AsyncSpec | None = Field(default=None, alias="async")
    idempotency_key: str | None = None

    def to_schema_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True, by_alias=True)
