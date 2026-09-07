"""Mistral OCR hosted adapter (protocol v2, synchronous ``INLINE`` execution).

The adapter sends one request to ``POST https://api.mistral.ai/v1/ocr`` with Bearer credentials
from ``MISTRAL_API_KEY``. ``MISTRAL_OCR_MODEL`` is optional configuration (resolved into
``ctx.runtime['model']``); its default is ``mistral-ocr-latest``. Public PDF, DOCX, and PPTX URLs
use a ``document_url`` chunk, image URLs use ``image_url``, and caller-provided bytes use the same
chunk types with a MIME-qualified ``data:...;base64,...`` URL. Local paths and OpenReading
``file_id`` values are refused because this adapter has no upload/staging lifecycle.

The request opts into native blocks, block confidence, and HTML tables. Page Markdown and blocks
are native; block bounds are converted from the provider's top-left pixels only when real page
dimensions are present, and the native average block confidence is retained only in [0,1]. Plain
text and table-cell grids are deterministic projections. The provider's page ``index`` is
zero-based — the same base as the request ``pages`` list, which the endpoint reference documents
as starting from 0 — so ``index + 1`` is the source page number; a missing or unusable index falls
back to the page's position in the response.

Schema extraction forwards the request JSON Schema as ``document_annotation_format`` and the
optional instructions as ``document_annotation_prompt``; a request whose ``extraction_schema``
carries no ``json_schema`` sends neither and is plain OCR (``parse``) end to end, including in
``report_cost``. ``document_annotation`` may be a JSON object or a JSON-encoded object. Malformed
annotation output produces a PARTIAL response with ``status.error`` and a warning; it is never
converted to a fabricated field value.

Cost is ESTIMATED, never claimed as the caller's billed amount: ``usage_info.pages_processed`` is
multiplied by the public list prices accessed below ($4/1000 OCR pages, $5/1000 Document AI pages
when annotations are requested). There is no documented request idempotency or vendor cancellation
mechanism on this synchronous endpoint, so both declarations are false. No native batch or
liveness probe is declared. Hosted compliance claims are deliberately fail-closed here:
HIPAA/SOC2/GDPR are not credited and training posture is unverified until a vendor-owned public
source establishes the exact API-account contract.

Capability grades are ``claimed`` because this implementation has not been run against a real
Mistral account as of 2026-09-02. The offline fixture is documentation-shaped evidence only. A
maintainer must run the keyed live test before promoting any grade to ``verified``.

Primary sources (accessed 2026-09-02):

* https://docs.mistral.ai/api/endpoint/ocr — endpoint, auth, request and response fields.
* https://docs.mistral.ai/studio/document-processing/basic_ocr — URL/base64 input, pages,
  Markdown, tables, blocks, dimensions, and confidence.
* https://docs.mistral.ai/studio/document-processing/annotations — JSON Schema document
  annotations and prompts.
* https://mistral.ai/pricing/api/ — public per-page OCR and Document AI annotation prices.
"""

from __future__ import annotations

import base64
import contextlib
import json
from typing import Any, Protocol

from openreading.adapters._http import error_for_status
from openreading.adapters.base import BackendAdapter
from openreading.derive import html_table_to_table, md_to_text, table_to_text
from openreading.types.blocks import Block, Table, TypedField
from openreading.types.cost import CostReport
from openreading.types.descriptor import (
    AdapterDescriptor,
    Capabilities,
    ConfigField,
    CredentialField,
    Output,
    OutputChannels,
    Provisioning,
    RouterHints,
    RuntimeProfile,
    Source,
)
from openreading.types.enums import (
    BackendType,
    BlockType,
    ChannelGrade,
    JobState,
    NativeOrigin,
    NativeUnit,
    OutputParadigm,
    PageUnit,
    ResponseState,
    WaitMode,
)
from openreading.types.errors import MissingCredentialsError, RetryableError, TerminalError
from openreading.types.geometry import to_canonical
from openreading.types.job import Job
from openreading.types.request import OpenReadingRequest, Outputs
from openreading.types.response import (
    BackendInfo,
    BackendRaw,
    Document,
    NormalizedResponse,
    Page,
    ResponseError,
    Status,
    Usage,
)
from openreading.types.runtime import Health, RawResult, RunContext

X = ChannelGrade.IMPOSSIBLE
N = ChannelGrade.NATIVE
D = ChannelGrade.DERIVABLE

_BASE_URL = "https://api.mistral.ai"
_DEFAULT_MODEL = "mistral-ocr-latest"

# The image formats the OCR endpoint documents; the data: URL MIME is derived from the extension
# when the caller gives none, because a PNG labelled image/jpeg is rejected or mis-decoded.
_IMAGE_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".avif": "image/avif",
}

_BLOCK_TYPES = {
    "title": BlockType.TITLE,
    "header": BlockType.HEADER,
    "footer": BlockType.FOOTER,
    "text": BlockType.TEXT,
    "list": BlockType.LIST,
    "table": BlockType.TABLE,
    "image": BlockType.IMAGE,
    "caption": BlockType.CAPTION,
    "equation": BlockType.FORMULA,
    "code": BlockType.CODE,
    "signature": BlockType.SIGNATURE,
}


def _bare_name(name: str | None) -> str:
    """Lower-cased filename or URL path with any query string dropped, for extension checks."""
    return (name or "").lower().split("?", 1)[0]


class MistralOCRClient(Protocol):
    """Minimal client seam used by offline fakes and the real HTTP transport."""

    def ocr(self, body: dict) -> dict: ...


class _HttpxMistralOCRClient:
    def __init__(self, api_key: str) -> None:
        from openreading.adapters._http import build_httpx_client

        self._http = build_httpx_client(
            base_url=_BASE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=120.0,
        )

    def ocr(self, body: dict) -> dict:
        response = self._http.post("/v1/ocr", json=body)
        if response.status_code >= 400:
            # Do not surface an authentication response body. Providers and intermediaries have
            # echoed rejected keys; the execution boundary adds the safe env-var hint instead.
            auth_rejected = response.status_code in (401, 403)
            detail = ""
            code = None
            if not auth_rejected:
                with contextlib.suppress(Exception):
                    payload = response.json()
                    if isinstance(payload, dict):
                        message = payload.get("message") or payload.get("detail")
                        if isinstance(message, str):
                            detail = message
                        code_value = payload.get("code") or payload.get("type")
                        if isinstance(code_value, str):
                            code = code_value
            raise error_for_status(
                response.status_code,
                response.headers,
                backend_code=code,
                message="" if auth_rejected else (detail or response.text),
            )
        try:
            result = response.json()
        except ValueError as exc:
            raise TerminalError(
                "Mistral OCR returned a non-JSON response", backend_code="malformed_response"
            ) from exc
        if not isinstance(result, dict):
            raise TerminalError(
                "Mistral OCR returned a non-object response", backend_code="malformed_response"
            )
        return result


def _descriptor() -> AdapterDescriptor:
    return AdapterDescriptor(
        id="mistral-ocr",
        type=BackendType.HOSTED_API,
        protocol_version=2,
        adapter_impl="http",
        operations=["parse", "extract"],
        provisioning=Provisioning(
            byo_mode=["api_key"], auth="api_key"
        ),
        wait_modes=[WaitMode.INLINE],
        capabilities=Capabilities(
            ocr="claimed",
            handwriting="claimed",
            printed_tables="claimed",
            complex_tables="claimed",
            forms_key_value="claimed",
            layout="claimed",
            reading_order="claimed",
            multi_column="claimed",
            figures_charts="claimed",
            custom_schema_extraction="claimed",
            vlm_based="claimed",
            page_range_selection=True,
            input_formats=["pdf", "docx", "pptx", "png", "jpg", "jpeg", "avif"],
        ),
        runtime=RuntimeProfile(offline_capable=False, license="proprietary", version_pin="api"),
        output=Output(
            paradigms=[
                OutputParadigm.MARKDOWN,
                OutputParadigm.ELEMENT_LIST,
                OutputParadigm.TYPED_FIELDS,
            ],
            block_granularity="element",
            channels=OutputChannels(
                markdown=N,
                text=D,
                blocks=N,
                block_bbox=N,
                block_confidence=N,
                typed_fields=N,
                table_cells=D,
            ),
        ),
        router=RouterHints(normalization_difficulty="low"),
        credentials_spec=[
            CredentialField(key="api_key", required=True, env=["MISTRAL_API_KEY"], example="...")
        ],
        config_spec=[
            ConfigField(
                key="model",
                env=["MISTRAL_OCR_MODEL"],
                example=_DEFAULT_MODEL,
                description="OCR model override; default mistral-ocr-latest.",
            )
        ],
        signup_url="https://console.mistral.ai/api-keys",
        accepts_url=True,
        live_gate_env=["MISTRAL_API_KEY"],
        # The OCR endpoint reference has no request idempotency key or header.
        idempotency_supported=False,
        # The same reference exposes a synchronous POST and no cancellable OCR job resource.
        cancel_supported=False,
        sources=[
            Source(
                url="https://docs.mistral.ai/api/endpoint/ocr",
                accessed="2026-09-02",
                supports="POST /v1/ocr, Bearer auth, request fields, pages and usage response",
            ),
            Source(
                url="https://docs.mistral.ai/studio/document-processing/basic_ocr",
                accessed="2026-09-02",
                supports="URL/base64 input, Markdown, blocks, dimensions, confidence and tables",
            ),
            Source(
                url="https://docs.mistral.ai/studio/document-processing/annotations",
                accessed="2026-09-02",
                supports="JSON Schema document_annotation_format and annotation prompt",
            ),
            Source(
                url="https://mistral.ai/pricing/api/",
                accessed="2026-09-02",
                supports="$4/1000 OCR pages and $5/1000 annotated Document AI pages",
            ),
        ],
    )


class MistralOCRAdapter(BackendAdapter):
    """Translate OpenReading requests and Mistral OCR responses without persistent client state."""

    def __init__(self, client: MistralOCRClient | None = None) -> None:
        self.descriptor = _descriptor()
        self._client = client

    def health(self) -> Health:
        if self._client is not None:
            return Health(ready=True, detail="injected client")
        try:
            import httpx  # noqa: F401
        except ImportError:
            return Health(
                ready=False,
                missing_deps=["httpx (pip install 'openreading[mistral-ocr]')"],
            )
        return Health(ready=True)

    def _get_client(self, ctx: RunContext) -> MistralOCRClient:
        if self._client is not None:
            return self._client
        credentials = (ctx.credentials.values if ctx.credentials else {}) or {}
        api_key = credentials.get("api_key")
        if not api_key:
            raise MissingCredentialsError(
                "missing required credentials/config: MISTRAL_API_KEY. "
                f"Sign up / configure: {self.descriptor.signup_url}",
                missing=["MISTRAL_API_KEY"],
            )
        return _HttpxMistralOCRClient(str(api_key))  # pragma: no cover

    @staticmethod
    def _is_image(mime_type: str | None, name: str | None) -> bool:
        if mime_type:
            return mime_type.lower().startswith("image/")
        return any(_bare_name(name).endswith(suffix) for suffix in _IMAGE_MIME_TYPES)

    def _document_arg(self, req: OpenReadingRequest) -> dict[str, str]:
        document = req.document
        name = document.filename or document.url
        is_image = self._is_image(document.mime_type, name)
        chunk_type = "image_url" if is_image else "document_url"
        if document.url:
            return {"type": chunk_type, chunk_type: document.url}
        if document.bytes_base64:
            try:
                base64.b64decode(document.bytes_base64, validate=True)
            except ValueError as exc:
                raise TerminalError(
                    "Mistral OCR inline input must contain valid base64 document bytes",
                    backend_code="unsupported_input",
                ) from exc
            # `openreading.derive.mime` resolved this before the request was built. A private
            # guesser here was a sixth extension table with its own PDF default, which is how an
            # unidentified input reached the vendor labelled as a document.
            mime_type = document.mime_type
            if not mime_type:
                raise TerminalError(
                    "Mistral OCR needs a media type and core could not identify this document; "
                    "pass document.mime_type explicitly",
                    backend_code="unsupported_input",
                )
            return {
                "type": chunk_type,
                chunk_type: f"data:{mime_type};base64,{document.bytes_base64}",
            }
        raise TerminalError(
            "Mistral OCR needs document url/bytes", backend_code="unsupported_input"
        )

    @staticmethod
    def _selected_pages(req: OpenReadingRequest) -> list[int] | None:
        page_spec = req.pages
        if page_spec is None:
            return None
        selected: list[int] = []
        if page_spec.ranges:
            for page_range in page_spec.ranges:
                end = page_range.end or page_range.start
                if end - page_range.start > 10_000:
                    raise TerminalError(
                        "Mistral OCR page range is too large", backend_code="page_range_too_large"
                    )
                selected.extend(range(page_range.start - 1, end))
        elif page_spec.max_pages is not None:
            if page_spec.max_pages > 10_000:
                raise TerminalError(
                    "Mistral OCR max_pages is too large", backend_code="page_range_too_large"
                )
            selected.extend(range(page_spec.max_pages))
        if page_spec.max_pages is not None:
            selected = selected[: page_spec.max_pages]
        return list(dict.fromkeys(selected)) or None

    def _body(self, req: OpenReadingRequest, ctx: RunContext) -> dict[str, Any]:
        model = req.backend.version or (ctx.runtime or {}).get("model") or _DEFAULT_MODEL
        body: dict[str, Any] = {
            "model": str(model),
            "document": self._document_arg(req),
            "include_blocks": True,
            "confidence_scores_granularity": "block",
            "table_format": "html",
        }
        pages = self._selected_pages(req)
        if pages is not None:
            body["pages"] = pages
        extraction = req.extraction_schema
        if extraction is not None and extraction.json_schema:
            body["document_annotation_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "openreading_extraction",
                    "strict": True,
                    "schema": extraction.json_schema,
                },
            }
            if extraction.instructions:
                body["document_annotation_prompt"] = extraction.instructions
        return body

    def submit(self, req: OpenReadingRequest, ctx: RunContext) -> Job:
        self.assert_supports(req)
        client = self._get_client(ctx)
        body = self._body(req, ctx)
        try:
            raw = client.ocr(body)
        except (RetryableError, TerminalError):
            raise
        except Exception as exc:  # noqa: BLE001
            raise TerminalError(str(exc), backend_code=type(exc).__name__) from exc
        if not isinstance(raw, dict):
            raise TerminalError(
                "Mistral OCR returned a non-object response", backend_code="malformed_response"
            )
        job = self.new_job(
            WaitMode.INLINE,
            state=JobState.SUCCEEDED,
            idempotency_key=ctx.idempotency_key,
        )
        # "extract" only when an annotation was actually sent: an instructions-only schema buys
        # plain OCR, and the annotated per-page price would otherwise be charged for it.
        job.raw = RawResult(
            payload=raw,
            media_type="application/json",
            encoding="json",
            object_class="extract" if "document_annotation_format" in body else "parse",
        )
        return job

    @staticmethod
    def _page_number(page: dict[str, Any], fallback: int) -> int:
        # Zero-based like the request `pages` list. Anything that is not a non-negative int is
        # not evidence of a page position, so it yields the positional fallback rather than a
        # coerced guess.
        value = page.get("index")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return fallback
        return value + 1

    @staticmethod
    def _dimensions(page: dict[str, Any]) -> tuple[float | None, float | None, float | None]:
        dimensions = page.get("dimensions")
        if not isinstance(dimensions, dict):
            return None, None, None

        def number(key: str) -> float | None:
            value = dimensions.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                return float(value)
            return None

        return number("width"), number("height"), number("dpi")

    @staticmethod
    def _confidence(block: dict[str, Any]) -> float | None:
        scores = block.get("confidence_scores")
        value: Any = (
            scores.get("average_content_confidence_score") if isinstance(scores, dict) else None
        )
        # Tolerate older/captured response aliases without treating them as documented evidence.
        if value is None and isinstance(scores, dict):
            value = scores.get("average")
        if value is None:
            value = block.get("confidence")
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and 0.0 <= float(value) <= 1.0
        ):
            return float(value)
        return None

    @staticmethod
    def _bbox(
        block: dict[str, Any],
        *,
        page_number: int,
        page_width: float | None,
        page_height: float | None,
        dpi: float | None,
    ):
        if page_width is None or page_height is None:
            return None
        keys = ("top_left_x", "top_left_y", "bottom_right_x", "bottom_right_y")
        values = [block.get(key) for key in keys]
        numeric_values: list[float] = []
        for value in values:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return None
            numeric_values.append(float(value))
        return to_canonical(
            numeric_values,
            origin=NativeOrigin.TOP_LEFT,
            unit=NativeUnit.PIXEL,
            page_width=page_width,
            page_height=page_height,
            page=page_number,
            dpi=dpi,
        )

    @staticmethod
    def _table_from_content(content: Any) -> Table | None:
        return html_table_to_table(content) if isinstance(content, str) else None

    def _block(
        self,
        raw: dict[str, Any],
        *,
        page_number: int,
        page_width: float | None,
        page_height: float | None,
        dpi: float | None,
        fallback_id: str,
    ) -> Block:
        native_type = str(raw.get("type") or "other")
        raw_content = raw.get("content")
        content = raw_content if isinstance(raw_content, str) else None
        block_type = _BLOCK_TYPES.get(native_type.lower(), BlockType.OTHER)
        table = self._table_from_content(content) if block_type is BlockType.TABLE else None
        if table is not None:
            text = table_to_text(table)
            html = content
            markdown = None
        else:
            text = md_to_text(content) if content else None
            html = None
            markdown = content
        return Block(
            id=str(raw.get("id") or fallback_id),
            type=block_type,
            native_type=native_type,
            text=text,
            markdown=markdown,
            html=html,
            table=table,
            bbox=self._bbox(
                raw,
                page_number=page_number,
                page_width=page_width,
                page_height=page_height,
                dpi=dpi,
            ),
            confidence=self._confidence(raw),
        )

    def _table_blocks(self, raw_tables: Any, *, page_number: int, start_index: int) -> list[Block]:
        if not isinstance(raw_tables, list):
            return []
        blocks: list[Block] = []
        for offset, raw_table in enumerate(raw_tables):
            if not isinstance(raw_table, dict):
                continue
            content = raw_table.get("html") or raw_table.get("content")
            table = self._table_from_content(content)
            if table is None:
                continue
            blocks.append(
                Block(
                    id=str(raw_table.get("id") or f"p{page_number}-table-{offset}"),
                    type=BlockType.TABLE,
                    native_type="table",
                    text=table_to_text(table),
                    html=content,
                    table=table,
                    reading_order=start_index + len(blocks),
                )
            )
        return blocks

    @staticmethod
    def _typed_fields(raw: dict[str, Any]) -> tuple[dict[str, TypedField], bool]:
        annotation = raw.get("document_annotation")
        if annotation is None:
            return {}, False
        if isinstance(annotation, str):
            try:
                annotation = json.loads(annotation)
            except (json.JSONDecodeError, TypeError):
                return {}, True
        if not isinstance(annotation, dict):
            return {}, True
        return {str(key): TypedField(value=value) for key, value in annotation.items()}, False

    def normalize(
        self, job: Job, ctx: RunContext, slim_req: OpenReadingRequest
    ) -> NormalizedResponse:
        raw: dict[str, Any] = (
            job.raw.payload if job.raw and isinstance(job.raw.payload, dict) else {}
        )
        outputs = slim_req.outputs or Outputs()
        pages: list[Page] = []
        reading_order = 0
        for fallback, raw_page in enumerate(raw.get("pages") or [], start=1):
            if not isinstance(raw_page, dict):
                continue
            page_number = self._page_number(raw_page, fallback)
            width, height, dpi = self._dimensions(raw_page)
            page_markdown = raw_page.get("markdown")
            page_markdown = page_markdown if isinstance(page_markdown, str) else None
            page_text = md_to_text(page_markdown) if page_markdown else None
            blocks: list[Block] = []
            raw_blocks = raw_page.get("blocks")
            if isinstance(raw_blocks, list):
                for block_index, raw_block in enumerate(raw_blocks):
                    if not isinstance(raw_block, dict):
                        continue
                    block = self._block(
                        raw_block,
                        page_number=page_number,
                        page_width=width,
                        page_height=height,
                        dpi=dpi,
                        fallback_id=f"p{page_number}-block-{block_index}",
                    )
                    block.reading_order = reading_order
                    reading_order += 1
                    blocks.append(block)
            if not any(block.type is BlockType.TABLE for block in blocks):
                derived_tables = self._table_blocks(
                    raw_page.get("tables"), page_number=page_number, start_index=reading_order
                )
                reading_order += len(derived_tables)
                blocks.extend(derived_tables)
            if outputs.tables == "none":
                for block in blocks:
                    block.table = None
            pages.append(
                Page(
                    page_number=page_number,
                    width=width,
                    height=height,
                    unit=PageUnit.PIXEL if width is not None and height is not None else None,
                    dpi=dpi,
                    markdown=page_markdown if outputs.markdown else None,
                    text=page_text if outputs.text else None,
                    blocks=blocks if outputs.blocks else None,
                )
            )

        document_markdown = (
            "\n\n".join(
                page.markdown for page in pages if isinstance(page.markdown, str) and page.markdown
            )
            or None
        )
        document_text = (
            "\n\n".join(page.text for page in pages if isinstance(page.text, str) and page.text)
            or None
        )
        typed_fields, annotation_malformed = self._typed_fields(raw)
        usage_raw = raw.get("usage_info")
        usage_raw = usage_raw if isinstance(usage_raw, dict) else {}
        pages_processed = usage_raw.get("pages_processed")
        if (
            not isinstance(pages_processed, int)
            or isinstance(pages_processed, bool)
            or pages_processed < 0
        ):
            pages_processed = len(pages) or None
        operation = "extract" if job.raw and job.raw.object_class == "extract" else "parse"
        response = NormalizedResponse(
            status=Status(
                state=ResponseState.PARTIAL if annotation_malformed else ResponseState.SUCCEEDED,
                error=(
                    ResponseError(
                        code="malformed_response",
                        message="Mistral OCR document_annotation was not a JSON object.",
                        backend_code="malformed_response",
                    )
                    if annotation_malformed
                    else None
                ),
            ),
            backend=BackendInfo(
                id="mistral-ocr",
                type=BackendType.HOSTED_API,
                operation=operation,
                version=str(raw.get("model") or _DEFAULT_MODEL),
                output_paradigm=[
                    OutputParadigm.MARKDOWN,
                    OutputParadigm.ELEMENT_LIST,
                    OutputParadigm.TYPED_FIELDS,
                ],
            ),
            document=Document(
                markdown=document_markdown if outputs.markdown else None,
                text=document_text if outputs.text else None,
                page_count=pages_processed,
                pages=pages or None,
            ),
            typed_fields=typed_fields or None,
            usage=Usage(pages_processed=pages_processed) if pages_processed is not None else None,
        )
        if annotation_malformed:
            response.add_warning(
                "typed_fields_malformed",
                "Mistral OCR returned document_annotation that was not a JSON object",
                "typed_fields",
            )
        elif not typed_fields and (slim_req.extraction_schema is not None or outputs.typed_fields):
            response.add_warning(
                "typed_fields_unavailable",
                "typed_fields requested but Mistral OCR returned no document_annotation object",
                "typed_fields",
            )
        if outputs.markdown and not document_markdown:
            response.add_warning(
                "markdown_unavailable", "Mistral OCR returned no page Markdown", "markdown"
            )
        if outputs.text and not document_text:
            response.add_warning(
                "text_unavailable", "No plain text could be derived from page Markdown", "text"
            )
        if outputs.blocks and not any(page.blocks for page in pages):
            response.add_warning("blocks_unavailable", "Mistral OCR returned no blocks", "blocks")
        if outputs.blocks and any(
            block.bbox is None for page in pages for block in page.blocks or []
        ):
            response.add_warning(
                "block_bbox_unavailable",
                "Mistral OCR omitted usable page dimensions or bounds for one or more blocks",
                "block_bbox",
            )
        if outputs.blocks and any(
            block.confidence is None for page in pages for block in page.blocks or []
        ):
            response.add_warning(
                "block_confidence_unavailable",
                "Mistral OCR omitted a valid content confidence for one or more blocks",
                "block_confidence",
            )
        if outputs.tables == "cells" and not any(
            block.table and block.table.cells for page in pages for block in (page.blocks or [])
        ):
            response.add_warning(
                "table_cells_unavailable",
                "Mistral OCR returned no HTML table cells",
                "table_cells",
            )

        provenance: dict[str, str] = {}
        if document_markdown and outputs.markdown:
            provenance["markdown"] = "native"
        if document_text and outputs.text:
            provenance["text"] = "derived"
        all_blocks = [block for page in pages for block in (page.blocks or [])]
        if all_blocks:
            provenance["blocks"] = "native"
        if any(block.bbox is not None for block in all_blocks):
            provenance["block_bbox"] = "native"
        if any(block.confidence is not None for block in all_blocks):
            provenance["block_confidence"] = "native"
        if typed_fields:
            provenance["typed_fields"] = "native"
        if any(block.table and block.table.cells for block in all_blocks):
            provenance["table_cells"] = "derived"
        response.channel_provenance = provenance or None

        if outputs.include_backend_raw:
            response.backend_raw = BackendRaw(
                encoding="json",
                media_type="application/json",
                object_class=operation,
                payload=raw,
            )
        return response

    def report_cost(self, job: Job) -> CostReport:
        raw = job.raw.payload if job.raw and isinstance(job.raw.payload, dict) else {}
        usage = raw.get("usage_info")
        usage = usage if isinstance(usage, dict) else {}
        value = usage.get("pages_processed")
        pages: float | None = None
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            pages = float(value)
        elif isinstance(raw.get("pages"), list):
            pages = float(len(raw["pages"]))
        return CostReport(native_unit="page", native_quantity=pages or 0.0)
