"""Google Gemini Developer API document adapter via the GA Interactions surface.

The adapter is an INLINE, stateless (``store=false``) hosted flow. It accepts either inline
``bytes_base64`` or a Gemini Files resource name in ``file_id``; it does not download arbitrary
URLs or read local paths. The request combines one ``document`` input with one text instruction
and posts it to ``POST /v1beta/interactions`` using the caller's ``GEMINI_API_KEY``. The default
model is ``gemini-3.6-flash``; the credential/config broker maps ``GEMINI_MODEL`` to
``ctx.runtime["model"]``. The ordinary per-request ``request.backend.version`` override wins over
that environment-derived default.

Parse mode asks for faithful Markdown. Markdown is therefore native model output; plain text,
layout-element blocks, and table cells are deterministic projections through ``openreading.derive``.
They live in one explicitly synthetic page because Interactions returns no reliable page
attribution. Geometry and confidence remain impossible and are always warned when blocks are
requested. Extract mode supplies the caller's JSON Schema as Interactions ``response_format`` and
turns the returned JSON object into native typed fields without coercing strings, numbers,
booleans, arrays, or nested objects.

Pricing is deliberately ``unknown``: usage preserves the API's reported input/output token counts,
but this integration does not freeze a model-price table whose identifiers and rates can change.
No dollar value is invented. Compliance also fails closed. Google documents materially different
data-use posture between free and paid service tiers, while the adapter cannot observe the tier;
``trains_on_customer_data`` is therefore ``unverified`` and unsupported public compliance claims
remain false. This implementation claims neither vendor cancellation nor idempotency, native
batching, liveness probing, geometry, or confidence. Capability grades remain ``claimed`` until a
maintainer runs the keyed live test; no live account was available while this module was authored.

Primary sources, accessed 2026-09-02:

* https://ai.google.dev/gemini-api/docs/document-processing — inline/File API document inputs,
  document limits, and document-understanding examples.
* https://ai.google.dev/gemini-api/docs/interactions-overview — Interactions lifecycle,
  stateless ``store=false``, response steps, and usage.
* https://ai.google.dev/api/interactions-api — REST request and response field contract.
* https://ai.google.dev/gemini-api/docs/structured-output — JSON-schema ``response_format``.
* https://ai.google.dev/gemini-api/docs/pricing — current tier-dependent pricing and data-use
  distinctions; intentionally not copied into a static price table here.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Protocol

from openreading.adapters.base import BackendAdapter
from openreading.derive import md_to_blocks, md_to_text, pdf_page_count
from openreading.types.blocks import TypedField
from openreading.types.cost import CostReport
from openreading.types.descriptor import (
    AdapterDescriptor,
    Capabilities,
    ComplianceProfile,
    ConfigField,
    Cost,
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
    ChannelGrade,
    CostBasis,
    JobState,
    OutputParadigm,
    ResponseState,
    WaitMode,
)
from openreading.types.errors import MissingCredentialsError, RetryableError, TerminalError
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

_DEFAULT_MODEL = "gemini-3.6-flash"
_DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"
_PARSE_PROMPT = (
    "Convert this document to clean, faithful Markdown. Preserve headings, lists, and tables. "
    "Output only the Markdown."
)


class GeminiClient(Protocol):
    """Narrow injectable port shared by the real HTTP client and offline fakes."""

    def interact(
        self,
        *,
        model: str,
        input: list[dict[str, Any]],
        store: bool,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


class _HttpxGeminiClient:  # pragma: no cover - request shape is covered through respx
    def __init__(self, api_key: str) -> None:
        from openreading.adapters._http import build_httpx_client

        self._http = build_httpx_client(
            base_url=_DEFAULT_BASE_URL,
            headers={"x-goog-api-key": api_key},
            timeout=300.0,
        )

    def interact(
        self,
        *,
        model: str,
        input: list[dict[str, Any]],
        store: bool,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"model": model, "input": input, "store": store}
        if response_format is not None:
            body["response_format"] = response_format
        response = self._http.post("/v1beta/interactions", json=body)
        if response.status_code >= 400:
            from openreading.adapters._http import error_for_status

            # Authentication responses are not trusted content: providers and proxies have
            # echoed rejected credentials in their bodies. The execution boundary supplies the
            # actionable, key-free environment hint for this taxonomy code.
            message = "" if response.status_code in (401, 403) else response.text
            raise error_for_status(response.status_code, response.headers, message=message)
        try:
            payload = response.json()
        except ValueError as exc:
            raise TerminalError(
                "Gemini returned a non-JSON Interactions response",
                backend_code="malformed_response",
            ) from exc
        if not isinstance(payload, dict):
            raise TerminalError(
                "Gemini returned a non-object Interactions response",
                backend_code="malformed_response",
            )
        return payload


def _descriptor() -> AdapterDescriptor:
    return AdapterDescriptor(
        id="google-gemini",
        type=BackendType.HOSTED_API,
        protocol_version=2,
        adapter_impl="http",
        operations=["parse", "extract"],
        provisioning=Provisioning(
            byo_mode=["api_key"], auth="api_key", billing_target="caller_account"
        ),
        wait_modes=[WaitMode.INLINE],
        capabilities=Capabilities(
            ocr="claimed",
            handwriting="claimed",
            printed_tables="claimed",
            complex_tables="claimed",
            forms_key_value="claimed",
            reading_order="claimed",
            figures_charts="claimed",
            custom_schema_extraction="claimed",
            vlm_based="claimed",
            input_formats=["pdf"],
            max_pages_per_request=1000,
            max_file_size="50 MB",
        ),
        cost=Cost(native_unit="token", basis="unknown", lossiness="page-def"),
        compliance=ComplianceProfile(
            hipaa_baa="no",
            soc2=False,
            gdpr=False,
            trains_on_customer_data="unverified",
            runs_fully_local=False,
        ),
        runtime=RuntimeProfile(
            offline_capable=False,
            license="proprietary",
            version_pin="v1beta/interactions",
            hardware="managed",
        ),
        output=Output(
            paradigms=[OutputParadigm.TOKEN_STREAM, OutputParadigm.TYPED_FIELDS],
            block_granularity="element",
            channels=OutputChannels(
                markdown=N,
                text=D,
                blocks=D,
                block_bbox=X,
                block_confidence=X,
                typed_fields=N,
                table_cells=D,
            ),
        ),
        router=RouterHints(
            normalization_difficulty="medium",
            integration_priority="P1",
            priority_reason="General document understanding and schema-constrained extraction.",
        ),
        credentials_spec=[
            CredentialField(
                key="api_key",
                required=True,
                env=["GEMINI_API_KEY"],
                description="Google Gemini Developer API key.",
            )
        ],
        config_spec=[
            ConfigField(
                key="model",
                env=["GEMINI_MODEL"],
                example=_DEFAULT_MODEL,
                description="Interactions model; defaults to gemini-3.6-flash.",
            )
        ],
        signup_url="https://aistudio.google.com/apikey",
        accepts_url=False,
        live_gate_env=["GEMINI_API_KEY"],
        # The Interactions API reference has no request-id/idempotency field on create.
        idempotency_supported=False,
        # That reference limits cancellation to background interactions; this adapter is INLINE.
        cancel_supported=False,
        sources=[
            Source(
                url="https://ai.google.dev/gemini-api/docs/document-processing",
                accessed="2026-09-02",
                supports="inline/File API document inputs and documented limits",
            ),
            Source(
                url="https://ai.google.dev/gemini-api/docs/interactions-overview",
                accessed="2026-09-02",
                supports="Interactions flow, response steps, usage, and stateless store=false",
            ),
            Source(
                url="https://ai.google.dev/api/interactions-api",
                accessed="2026-09-02",
                supports="REST request and response fields",
            ),
            Source(
                url="https://ai.google.dev/gemini-api/docs/structured-output",
                accessed="2026-09-02",
                supports="JSON-schema response_format",
            ),
            Source(
                url="https://ai.google.dev/gemini-api/docs/pricing",
                accessed="2026-09-02",
                supports="tier-dependent pricing and data-use posture; no static price encoded",
            ),
        ],
    )


class GoogleGeminiAdapter(BackendAdapter):
    """Protocol-v2 adapter for the stateless Gemini Interactions document flow."""

    def __init__(self, client: GeminiClient | None = None) -> None:
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
                missing_deps=["httpx (pip install 'openreading[google-gemini]')"],
            )
        return Health(ready=True)

    def _get_client(self, ctx: RunContext) -> GeminiClient:
        if self._client is not None:
            return self._client
        values = (ctx.credentials.values if ctx.credentials else {}) or {}
        api_key = values.get("api_key")
        if not api_key:
            raise MissingCredentialsError(
                "missing required credentials/config: GEMINI_API_KEY. "
                f"Sign up / configure: {self.descriptor.signup_url}",
                missing=["GEMINI_API_KEY"],
            )
        # A real client is constructed per method call; only constructor injection may live on self.
        return _HttpxGeminiClient(str(api_key))

    def _document_input(self, req: OpenReadingRequest) -> dict[str, Any]:
        document = req.document
        mime_type = document.mime_type or "application/pdf"
        if document.bytes_base64 is not None:
            try:
                base64.b64decode(document.bytes_base64, validate=True)
            except ValueError as exc:
                raise TerminalError(
                    "Gemini inline input must contain valid base64 document bytes",
                    backend_code="unsupported_input",
                ) from exc
            return {
                "type": "document",
                "data": document.bytes_base64,
                "mime_type": mime_type,
            }
        if document.file_id:
            return {"type": "document", "uri": document.file_id, "mime_type": mime_type}
        raise TerminalError(
            "Gemini needs bytes_base64 or a Gemini Files resource in file_id",
            backend_code="unsupported_input",
        )

    def _build_request(
        self, req: OpenReadingRequest, ctx: RunContext
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None, str]:
        runtime = ctx.runtime or {}
        model = str(req.backend.version or runtime.get("model") or _DEFAULT_MODEL)
        document = self._document_input(req)
        if req.extraction_schema is None:
            return model, [document, {"type": "text", "text": _PARSE_PROMPT}], None, "parse"

        schema = req.extraction_schema.json_schema or {
            "type": "object",
            "properties": {},
            "additionalProperties": True,
        }
        instructions = req.extraction_schema.instructions or "Extract the requested fields."
        response_format = {"type": "text", "mime_type": "application/json", "schema": schema}
        return (
            model,
            [document, {"type": "text", "text": instructions}],
            response_format,
            "extract",
        )

    def submit(self, req: OpenReadingRequest, ctx: RunContext) -> Job:
        self.assert_supports(req)
        client = self._get_client(ctx)
        model, inputs, response_format, mode = self._build_request(req, ctx)
        try:
            raw = client.interact(
                model=model,
                input=inputs,
                store=False,
                response_format=response_format,
            )
        except (RetryableError, TerminalError):
            raise
        except Exception as exc:  # noqa: BLE001 - client exceptions enter the adapter taxonomy
            raise TerminalError(str(exc), backend_code=type(exc).__name__) from exc

        if not isinstance(raw, dict):
            raise TerminalError(
                "Gemini returned a non-object Interactions response",
                backend_code="malformed_response",
            )
        payload = dict(raw)
        payload["_pdf_page_count"] = self._page_count(req)
        job = self.new_job(
            WaitMode.INLINE,
            state=JobState.SUCCEEDED,
            idempotency_key=ctx.idempotency_key,
        )
        job.raw = RawResult(
            payload=payload,
            media_type="application/json",
            encoding="json",
            object_class=mode,
        )
        return job

    @staticmethod
    def _page_count(req: OpenReadingRequest) -> int | None:
        if req.document.bytes_base64 is None:
            return None
        try:
            return pdf_page_count(base64.b64decode(req.document.bytes_base64, validate=True))
        except ValueError:
            return None

    @staticmethod
    def _output_text(raw: dict[str, Any]) -> str:
        for step in reversed(raw.get("steps") or []):
            if not isinstance(step, dict) or step.get("type") != "model_output":
                continue
            parts = [
                block.get("text", "")
                for block in (step.get("content") or [])
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            return "".join(parts).strip()
        return ""

    @staticmethod
    def _schema_types(req: OpenReadingRequest) -> dict[str, str]:
        schema = req.extraction_schema.json_schema if req.extraction_schema else None
        properties = schema.get("properties") if isinstance(schema, dict) else None
        if not isinstance(properties, dict):
            return {}
        return {
            name: field["type"]
            for name, field in properties.items()
            if isinstance(field, dict) and isinstance(field.get("type"), str)
        }

    def normalize(
        self, job: Job, ctx: RunContext, slim_req: OpenReadingRequest
    ) -> NormalizedResponse:
        del ctx
        raw: dict[str, Any] = job.raw.payload if job.raw else {}
        mode = job.raw.object_class if job.raw else "parse"
        outputs = slim_req.outputs or Outputs()
        output_text = self._output_text(raw)

        markdown: str | None = None
        text: str | None = None
        pages: list[Page] | None = None
        typed_fields: dict[str, TypedField] | None = None
        response_error: ResponseError | None = None
        if mode == "extract":
            try:
                values = json.loads(output_text)
            except (TypeError, json.JSONDecodeError):
                values = None
                response_error = ResponseError(
                    code="malformed_response",
                    message="Gemini structured output was not valid JSON.",
                    backend_code="malformed_response",
                )
            if values is not None and not isinstance(values, dict):
                response_error = ResponseError(
                    code="malformed_response",
                    message="Gemini structured output was not a JSON object.",
                    backend_code="malformed_response",
                )
            if isinstance(values, dict):
                field_types = self._schema_types(slim_req)
                typed_fields = {
                    name: TypedField(value=value, type=field_types.get(name))
                    for name, value in values.items()
                }
        else:
            markdown = output_text or None
            plain = md_to_text(output_text) if output_text else ""
            text = plain or None
            if (outputs.blocks or outputs.tables == "cells") and output_text:
                blocks = md_to_blocks(output_text)
                if blocks:
                    pages = [Page(page_number=1, blocks=blocks)]

        interaction_completed = raw.get("status") == "completed"
        completed = interaction_completed and response_error is None
        response = NormalizedResponse(
            status=Status(
                state=ResponseState.SUCCEEDED if completed else ResponseState.PARTIAL,
                error=response_error,
            ),
            backend=BackendInfo(
                id="google-gemini",
                type=BackendType.HOSTED_API,
                version=raw.get("model"),
                operation=str(mode),
                output_paradigm=(
                    [OutputParadigm.TYPED_FIELDS]
                    if mode == "extract"
                    else [OutputParadigm.TOKEN_STREAM]
                ),
            ),
            document=Document(
                markdown=markdown if outputs.markdown else None,
                text=text if outputs.text else None,
                page_count=raw.get("_pdf_page_count"),
                pages=pages,
            ),
            typed_fields=typed_fields,
            usage=Usage(
                input_tokens=(raw.get("usage") or {}).get("total_input_tokens"),
                output_tokens=(raw.get("usage") or {}).get("total_output_tokens"),
            ),
        )

        if not interaction_completed:
            response.add_warning(
                "interaction_incomplete",
                "Gemini did not report a completed interaction; output may be incomplete.",
            )
        if pages is not None:
            response.add_warning(
                "page_attribution_unavailable",
                "Gemini returns whole-document Markdown without page attribution; derived blocks "
                "live in one synthetic Page(1).",
                "blocks",
            )
        elif outputs.blocks:
            response.add_warning(
                "blocks_unavailable",
                "this Gemini operation produced no Markdown from which to derive blocks.",
                "blocks",
            )
        if outputs.blocks:
            response.add_warning(
                "block_bbox_unavailable",
                "Gemini Interactions document output does not provide reliable block geometry.",
                "block_bbox",
            )
            response.add_warning(
                "block_confidence_unavailable",
                "Gemini Interactions document output does not provide calibrated block confidence.",
                "block_confidence",
            )
        if outputs.markdown and response.document.markdown is None:
            response.add_warning(
                "markdown_unavailable",
                "this operation did not produce Markdown.",
                "markdown",
            )
        if outputs.text and response.document.text is None:
            response.add_warning("text_unavailable", "this operation did not produce text.", "text")
        if outputs.typed_fields and not typed_fields:
            response.add_warning(
                "typed_fields_unavailable",
                "this operation did not produce structured fields.",
                "typed_fields",
            )
        if outputs.tables == "cells" and not self._has_table_cells(pages):
            response.add_warning(
                "table_cells_unavailable",
                "no Markdown table was available to derive table cells.",
                "table_cells",
            )

        provenance: dict[str, str] = {}
        if response.document.markdown is not None:
            provenance["markdown"] = "native"
        if response.document.text is not None:
            provenance["text"] = "derived"
        if pages is not None:
            provenance["blocks"] = "derived"
            if self._has_table_cells(pages):
                provenance["table_cells"] = "derived"
        if typed_fields:
            provenance["typed_fields"] = "native"
        response.channel_provenance = provenance or None

        if outputs.include_backend_raw:
            response.backend_raw = BackendRaw(
                encoding="json",
                media_type="application/json",
                object_class="google.ai.Interaction",
                payload=raw,
            )
        return response

    @staticmethod
    def _has_table_cells(pages: list[Page] | None) -> bool:
        return bool(
            pages
            and any(
                block.table is not None and bool(block.table.cells)
                for page in pages
                for block in (page.blocks or [])
            )
        )

    def report_cost(self, job: Job) -> CostReport:
        raw = job.raw.payload if job.raw else {}
        usage = raw.get("usage") or {}
        quantity = float(
            (usage.get("total_input_tokens") or 0) + (usage.get("total_output_tokens") or 0)
        )
        return CostReport(
            native_unit="token",
            native_quantity=quantity,
            cost_usd=None,
            basis=CostBasis.UNKNOWN,
            billing_target="caller_account",
        )
