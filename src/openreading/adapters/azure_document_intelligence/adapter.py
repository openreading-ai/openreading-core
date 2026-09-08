"""Azure AI Document Intelligence adapter — LRO (long-running operation) POLL over httpx.
POST .../documentModels/{modelId}:analyze → 202 + Operation-Location → GET analyzeResults/{id}
until status=succeeded, honoring the retry-after header. Everything in the AnalyzeResult is
span-indexed into one `content` string. The adapter maps paragraphs[] to reading-order blocks,
tables[] to cells, styles[].isHandwritten (by span overlap) to text_type, and prebuilt
documents[].fields to typed_fields. Markdown output mode (outputContentFormat=markdown) gives
native markdown.

BYO endpoint and subscription key. The vendor also documents Entra ID (OAuth2 bearer) auth,
which this adapter does not implement. HIPAA BAA is included by default via the Microsoft DPA.
Geometry is per-page unit (inch for PDF, pixel for image); polygons are flat [x1,y1,x2,y2,...].
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

from openreading.adapters._http import error_for_status, retry_after_seconds
from openreading.adapters.base import BackendAdapter
from openreading.derive import (
    GridCell,
    aggregate_confidence,
    cells_to_grid,
    md_to_text,
    order_by_position,
)
from openreading.types.blocks import Block, Citation, TypedField
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
    TextType,
    WaitMode,
)
from openreading.types.errors import RetryableError, TerminalError
from openreading.types.geometry import bbox_from_polygon
from openreading.types.job import Job
from openreading.types.request import OpenReadingRequest, Outputs
from openreading.types.response import (
    BackendInfo,
    BackendRaw,
    DocType,
    Document,
    NormalizedResponse,
    Page,
    Status,
)
from openreading.types.runtime import Health, RawResult, RunContext

X = ChannelGrade.IMPOSSIBLE
N = ChannelGrade.NATIVE
D = ChannelGrade.DERIVABLE

_API_VERSION = "2024-11-30"
_ROLE_MAP = {
    "title": BlockType.TITLE,
    "sectionHeading": BlockType.SECTION_HEADER,
    "pageHeader": BlockType.HEADER,
    "pageFooter": BlockType.FOOTER,
    "pageNumber": BlockType.PAGE_NUMBER,
    "footnote": BlockType.TEXT,
}
_UNIT_MAP = {"inch": (PageUnit.INCH, NativeUnit.INCH), "pixel": (PageUnit.PIXEL, NativeUnit.PIXEL)}
_DEFAULT_PORT_FOR_SCHEME = {"http": 80, "https": 443}


def _origin(url: str) -> tuple[str | None, str | None, int | None]:
    """(scheme, host, port) — normalized so a bare `https://host` and an explicit
    `https://host:443` compare equal. BL-162: comparing `.hostname` alone let
    a same-host attacker-chosen port, or an HTTPS→HTTP scheme downgrade, through unnoticed. Always
    returns a 3-tuple, even for an unparseable url (never None) — a malformed configured endpoint
    still activates the poll() comparison rather than silently disabling it."""
    p = urlparse(url)
    scheme = p.scheme.lower() or None
    host = p.hostname.lower() if p.hostname else None
    try:
        # BL-162: `p.port` raises ValueError on a non-numeric port (e.g.
        # "https://host:not-a-number/") instead of degrading — a malformed url must still yield a
        # tuple, per this function's own contract, not crash the caller. -1 is not a valid port,
        # so a malformed url on either side of the comparison can never accidentally match.
        numeric_port = p.port
    except ValueError:
        return (scheme, host, -1)
    # BL-162: `p.port or default` treats an explicit port 0 as absent
    # (falsy-zero), silently promoting it to the scheme default. `is not None` doesn't.
    port = numeric_port if numeric_port is not None else _DEFAULT_PORT_FOR_SCHEME.get(scheme or "")
    return (scheme, host, port)


@dataclass
class PollResp:
    status_code: int
    body: dict
    retry_after: float | None = None


class AzureDIClient(Protocol):
    def analyze(
        self, model_id: str, body: dict, content_format: str
    ) -> str: ...  # operation-location
    def get(self, operation_location: str) -> PollResp: ...


class _HttpxAzureClient:
    """Default client (real Azure). Built lazily from BYO endpoint + key."""

    def __init__(self, endpoint: str, key: str) -> None:  # pragma: no cover - real AWS/Azure path
        from openreading.adapters._http import build_httpx_client

        self._endpoint = endpoint.rstrip("/")
        self._http = build_httpx_client(headers={"Ocp-Apim-Subscription-Key": key})

    def analyze(self, model_id: str, body: dict, content_format: str) -> str:  # pragma: no cover
        url = f"{self._endpoint}/documentintelligence/documentModels/{model_id}:analyze"
        r = self._http.post(
            url,
            params={"api-version": _API_VERSION, "outputContentFormat": content_format},
            json=body,
        )
        if r.status_code not in (200, 202):
            raise error_for_status(r.status_code, r.headers, message=r.text)
        return r.headers["operation-location"]

    def get(self, operation_location: str) -> PollResp:  # pragma: no cover
        r = self._http.get(operation_location)
        return PollResp(
            r.status_code, r.json() if r.content else {}, retry_after_seconds(r.headers)
        )


def _descriptor() -> AdapterDescriptor:
    return AdapterDescriptor(
        id="azure-document-intelligence",
        type=BackendType.HOSTED_API,
        # Ledger T4a (AC-8): poll() builds its client fresh from ctx on every call (no
        # _active_client cached on self) — verified by R1/R2 conformance.
        protocol_version=2,
        adapter_impl="http",
        operations=["prebuilt-read", "prebuilt-layout", "prebuilt-invoice", "custom"],
        provisioning=Provisioning(byo_mode=["api_key"], auth="api_key"),
        wait_modes=[WaitMode.POLL],
        capabilities=Capabilities(
            ocr="verified",
            handwriting="verified",
            printed_tables="verified",
            complex_tables="verified",
            forms_key_value="verified",
            layout="verified",
            reading_order="verified",
            signatures=False,
            classification="claimed",
            custom_schema_extraction="claimed",
            languages=["en", "de", "fr", "es", "it", "nl", "pt"],
            input_formats=["pdf", "png", "jpg", "tiff", "bmp", "docx", "xlsx", "pptx", "html"],
            max_pages_per_request="2000",
        ),
        runtime=RuntimeProfile(
            offline_capable=False, license="proprietary", version_pin="api-version 2024-11-30"
        ),
        output=Output(
            paradigms=[OutputParadigm.ELEMENT_LIST, OutputParadigm.TYPED_FIELDS],
            block_granularity="paragraph",  # v0.3 hint (§4.3): Azure text blocks are paragraphs
            channels=OutputChannels(
                markdown=N,
                text=N,
                blocks=N,
                block_bbox=N,
                block_confidence=D,
                typed_fields=N,
                table_cells=N,
            ),
        ),
        router=RouterHints(
            normalization_difficulty="medium",
        ),
        credentials_spec=[
            CredentialField(key="key", required=True, env=["AZURE_DOCUMENT_INTELLIGENCE_KEY"]),
        ],
        config_spec=[
            ConfigField(
                key="endpoint",
                required=True,
                env=["AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"],
                description="the resource's Document Intelligence endpoint.",
                example="https://<resource>.cognitiveservices.azure.com",
            ),
        ],
        signup_url="https://azure.microsoft.com/products/ai-services/ai-document-intelligence",
        accepts_url=True,
        live_gate_env=[
            "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT",
            "AZURE_DOCUMENT_INTELLIGENCE_KEY",
        ],
        # BL-166: checked the official `documentModels/{modelId}:analyze` REST reference
        # (learn.microsoft.com/rest/api/aiservices/document-models/analyze-document) — its Security
        # section documents exactly two request-auth schemes (Ocp-Apim-Subscription-Key, OAuth2Auth)
        # and no other request header at all; no Idempotency-Key, Repeatability-Request-ID, or
        # x-ms-client-request-id is accepted by this operation. The service FAQ's own throttling
        # guidance ("implement retry logic … consider adding a delay") never mentions a dedup token
        # either. There is no vendor-side mechanism to forward, so an invented header would be
        # silently ignored — honest false rather than a no-op field.
        idempotency_supported=False,
        # BL-164: checked the full Document Models operation-group reference (API version
        # 2024-11-30, the current GA/v4.0 surface) — every operation listed (Analyze Batch/
        # Document/FromStream, model CRUD, Delete Analyze (Batch) Result, list/get) and none named
        # Cancel/Stop. The only DELETE verb, "Delete Analyze Result", is a post-completion
        # data-lifecycle call (garbage-collect a result before the 24h retention window), not a
        # cancel — its own docs describe marking a completed result for deletion, never aborting an
        # in-progress one. Honest false — a losing race branch's Analyze operation keeps running at
        # the vendor regardless of what openreading does locally.
        cancel_supported=False,
        sources=[
            Source(
                url="https://learn.microsoft.com/azure/ai-services/document-intelligence/",
                accessed="2026-07-21",
                supports="AnalyzeResult shape, LRO, pricing, HIPAA BAA",
            ),
            Source(
                url="https://learn.microsoft.com/en-us/rest/api/aiservices/document-models/analyze-document",
                accessed="2026-08-22",
                supports="BL-166: :analyze operation's full header/param list (Security section) — "
                "no Idempotency-Key or Repeatability-Request-ID documented",
            ),
            Source(
                url="https://learn.microsoft.com/en-us/rest/api/aiservices/document-models?view=rest-aiservices-v4.0%20(2024-11-30)",
                accessed="2026-08-22",
                supports="BL-164: full Document Models operation list (v4.0/2024-11-30) — no "
                "Cancel/Stop operation exists; the only DELETE is a post-completion result cleanup",
            ),
        ],
    )


class AzureDocumentIntelligenceAdapter(BackendAdapter):
    def __init__(self, client: AzureDIClient | None = None) -> None:
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
                missing_deps=["httpx (pip install 'openreading[azure-document-intelligence]')"],
            )
        return Health(ready=True)

    def _get_client(self, ctx: RunContext) -> AzureDIClient:
        if self._client is not None:
            return self._client
        creds = (ctx.credentials.values if ctx.credentials else {}) or {}
        endpoint, key = (ctx.runtime or {}).get("endpoint"), creds.get("key")
        if not endpoint or not key:
            raise TerminalError(
                "Azure DI needs an endpoint (config) + key (credential)",
                backend_code="no_credentials",
            )
        return _HttpxAzureClient(endpoint, key)  # pragma: no cover

    def submit(self, req: OpenReadingRequest, ctx: RunContext) -> Job:
        client = self._get_client(ctx)
        model_id = req.backend.operation or "prebuilt-layout"
        d = req.document
        if d.bytes_base64:
            body = {"base64Source": d.bytes_base64}
        elif d.url:
            body = {"urlSource": d.url}
        elif d.path:
            with open(d.path, "rb") as fh:
                body = {"base64Source": base64.b64encode(fh.read()).decode()}
        else:
            raise TerminalError(
                "Azure DI needs document bytes/url/path", backend_code="unsupported_input"
            )
        content_format = "markdown" if (req.outputs is None or req.outputs.markdown) else "text"
        try:
            op_loc = client.analyze(model_id, body, content_format)
        except (RetryableError, TerminalError):
            raise
        except Exception as e:  # noqa: BLE001
            raise self._map_error(e) from e
        job = self.new_job(
            WaitMode.POLL, state=JobState.RUNNING, idempotency_key=ctx.idempotency_key
        )
        job.poll_handle = {"operation_location": op_loc, "model_id": model_id}
        job.next_poll_at = 0.0
        return job

    def poll(self, job: Job, ctx: RunContext) -> Job:
        client = self._get_client(ctx)
        h = job.poll_handle or {}
        op_loc = h["operation_location"]
        # BL-162 part 3 (Ledger T4a fix): the operator-configured endpoint's origin is derived
        # fresh from ctx.runtime here — the same value _get_client(ctx) above reads to build the
        # client — rather than trusted from instance state set only in submit(). A fresh/resumed
        # instance (T4a's whole point: poll() may run on a different instance than the one that
        # called submit()) has no submit()-time state to trust; deriving it fresh from ctx on every
        # call keeps the guard active identically for same-instance and fresh-instance polls. None
        # only when NO ctx.runtime endpoint is resolved at all (only reachable with an injected
        # test client, since _get_client above already refuses a real run with no endpoint) — the
        # check below is a no-op in that case, matching every existing injected-client test's
        # `RunContext()`. A truthy-but-malformed endpoint still yields a (possibly-None-filled)
        # tuple, not None, so the comparison stays active rather than silently disabling itself.
        endpoint = (ctx.runtime or {}).get("endpoint")
        expected_origin = _origin(endpoint) if endpoint else None
        if expected_origin is not None:
            actual_origin = _origin(op_loc)
            if actual_origin != expected_origin:
                raise TerminalError(
                    f"operation-location origin {actual_origin!r} does not match the configured "
                    f"endpoint origin {expected_origin!r}; refusing to fetch",
                    backend_code="operation_location_origin_mismatch",
                )
        resp = client.get(op_loc)
        if resp.status_code == 429:
            raise RetryableError("throttled", backend_code="429", retry_after=resp.retry_after)
        if resp.status_code >= 400:
            raise error_for_status(resp.status_code, message=str(resp.body))
        status = resp.body.get("status")
        if status in ("notStarted", "running"):
            job.next_poll_at = (job.next_poll_at or 0.0) + (resp.retry_after or 2.0) * 1000.0
            return job
        if status == "failed":
            err = resp.body.get("error", {})
            raise TerminalError(
                err.get("message", "analyze failed"), backend_code=err.get("code", "failed")
            )
        job.state = JobState.SUCCEEDED
        job.raw = RawResult(
            payload=resp.body,
            media_type="application/json",
            encoding="json",
            object_class=h["model_id"],
        )
        return job

    def _map_error(self, e: Exception):
        if isinstance(e, (TerminalError, RetryableError)):
            return e
        return TerminalError(str(e), backend_code=type(e).__name__)

    # ---- normalize -----------------------------------------------------------
    def normalize(
        self, job: Job, ctx: RunContext, slim_req: OpenReadingRequest
    ) -> NormalizedResponse:
        raw: dict[str, Any] = job.raw.payload if job.raw else {}
        model_id = (job.raw.object_class if job.raw else None) or "prebuilt-layout"
        ar = raw.get("analyzeResult", {})
        outputs = slim_req.outputs or Outputs()

        page_meta = {p["pageNumber"]: p for p in ar.get("pages", [])}
        hw_spans = [
            s for st in ar.get("styles", []) if st.get("isHandwritten") for s in st.get("spans", [])
        ]
        # word confidences (indexed by their content span) → per-element block_confidence (D, §4.5)
        words = [w for pg in ar.get("pages", []) for w in pg.get("words", []) if w.get("span")]

        # Each block carries its provider span offset (the key for reading-order interleave, §4.3);
        # paragraphs, tables and selection marks all live in one per-page list so a mid-page table
        # is no longer dumped after every paragraph (P5).
        pages_items: dict[int, list[tuple[Block, int | None]]] = {pn: [] for pn in page_meta}

        for para in ar.get("paragraphs", []):
            region = (para.get("boundingRegions") or [{}])[0]
            pno = region.get("pageNumber", 1)
            role = para.get("role")
            spans = para.get("spans", [])
            block = Block(
                type=_ROLE_MAP.get(role, BlockType.TEXT),
                native_type=role or "paragraph",
                text=para.get("content"),
                bbox=self._region_bbox(region, page_meta),
                text_type=TextType.HANDWRITING if _spans_overlap(spans, hw_spans) else None,
                confidence=_word_confidence(spans, words),
            )
            pages_items.setdefault(pno, []).append((block, _span_offset(spans)))

        for tbl in ar.get("tables", []):
            block, pno = self._table_block(tbl, page_meta)
            pages_items.setdefault(pno, []).append((block, _span_offset(tbl.get("spans", []))))

        for pno, pg in page_meta.items():
            for sm in pg.get("selectionMarks", []) or []:
                block = Block(
                    type=BlockType.SELECTION_MARK,
                    native_type=f"selectionMark:{sm.get('state')}",
                    confidence=sm.get("confidence"),
                    bbox=self._poly_bbox(sm.get("polygon"), pno, page_meta),
                )
                span = sm.get("span")
                key = span.get("offset") if isinstance(span, dict) else None
                pages_items.setdefault(pno, []).append((block, key))

        page_objs = []
        for pno in sorted(pages_items):
            pg = page_meta.get(pno, {})
            unit_enum = _UNIT_MAP.get(pg.get("unit", "inch"), (PageUnit.INCH, NativeUnit.INCH))[0]
            items = pages_items[pno]
            ordered = order_by_position([b for b, _ in items], [k for _, k in items])
            for i, b in enumerate(ordered):
                b.reading_order = i
            page_objs.append(
                Page(
                    page_number=pno,
                    width=pg.get("width"),
                    height=pg.get("height"),
                    unit=unit_enum,
                    rotation=pg.get("angle"),
                    blocks=ordered,
                )
            )

        # text is a plain projection of the native markdown (default mode) or Azure's own plain
        # `content` (text mode) — never a byte-identical copy of markdown (P0/C1/C2).
        content = ar.get("content", "")
        content_format = (
            "markdown" if (slim_req.outputs is None or slim_req.outputs.markdown) else "text"
        )
        if content_format == "markdown":
            md_out, text_out = (content or None), (md_to_text(content) if content else None)
        else:
            md_out, text_out = None, (content or None)

        typed_fields = self._documents_to_fields(ar.get("documents", []), page_meta)
        for name, field in self._kv_pairs_to_fields(ar, page_meta).items():
            typed_fields.setdefault(name, field)  # native documents[] fields win over derived KV

        doc_type = None
        docs = ar.get("documents", [])
        if docs:
            doc_type = DocType(label=docs[0].get("docType"), confidence=docs[0].get("confidence"))

        resp = NormalizedResponse(
            status=Status(state=ResponseState.SUCCEEDED),
            backend=BackendInfo(
                id="azure-document-intelligence",
                type=BackendType.HOSTED_API,
                operation=model_id,
                version=ar.get("apiVersion"),
                output_paradigm=[OutputParadigm.ELEMENT_LIST],
            ),
            document=Document(
                markdown=md_out if outputs.markdown else None,
                text=text_out if outputs.text else None,
                page_count=len(page_meta),
                pages=page_objs,
                doc_type=doc_type,
            ),
            typed_fields=typed_fields or None,
        )
        # v0.3 provenance (§3.3/§6.4): mode-dependent — markdown mode projects text from the native
        # markdown (text=derived); text mode returns Azure's native plain text (text=native).
        resp.channel_provenance = {
            "blocks": "native",
            "block_bbox": "native",
            "block_confidence": "derived",
            "typed_fields": "native",
            "table_cells": "native",
            **(
                {"markdown": "native", "text": "derived"}
                if content_format == "markdown"
                else {"text": "native"}
            ),
        }
        if outputs.include_backend_raw:
            resp.backend_raw = BackendRaw(
                encoding="json", media_type="application/json", object_class=model_id, payload=raw
            )
        return resp

    def _region_bbox(self, region: dict, page_meta: dict):
        return self._poly_bbox(region.get("polygon"), region.get("pageNumber", 1), page_meta)

    def _poly_bbox(self, polygon, pno: int, page_meta: dict):
        if not polygon:
            return None
        pg = page_meta.get(pno, {})
        unit = _UNIT_MAP.get(pg.get("unit", "inch"), (PageUnit.INCH, NativeUnit.INCH))[1]
        pts = [[polygon[i], polygon[i + 1]] for i in range(0, len(polygon) - 1, 2)]
        return bbox_from_polygon(
            pts,
            origin=NativeOrigin.TOP_LEFT,
            unit=unit,
            page_width=pg.get("width", 1.0),
            page_height=pg.get("height", 1.0),
            page=pno,
        )

    def _table_block(self, tbl: dict, page_meta: dict) -> tuple[Block, int]:
        cells = tbl.get("cells", [])
        first_region = (cells[0].get("boundingRegions") or [{}])[0] if cells else {}
        pno = first_region.get("pageNumber", 1)
        # Route every table through the ONE occupancy cursor (§4.7): Azure gives explicit grid
        # indices + spans, so `Table.rows` leaves covered positions None and is_header comes from
        # the rowHeader/columnHeader/stubHead kinds (never fabricated as "row 0").
        grid_cells: list[GridCell] = []
        for c in cells:
            region = (c.get("boundingRegions") or [{}])[0]
            cbbox = self._poly_bbox(region.get("polygon"), region.get("pageNumber", pno), page_meta)
            grid_cells.append(
                GridCell(
                    row=c.get("rowIndex", 0),
                    col=c.get("columnIndex", 0),
                    row_span=c.get("rowSpan", 1),
                    col_span=c.get("columnSpan", 1),
                    text=c.get("content") or None,
                    is_header=c.get("kind") in _HEADER_KINDS,
                    bbox=cbbox.model_dump(mode="json", exclude_none=True) if cbbox else None,
                )
            )
        block = Block(
            type=BlockType.TABLE,
            native_type="table",
            bbox=self._region_bbox(first_region, page_meta),
            table=cells_to_grid(grid_cells),
        )
        return block, pno

    def _field_citations(self, f: dict, page_meta: dict) -> list[Citation]:
        """A field's boundingRegions (page + geometry) → TypedField.citations (§4.6)."""
        cits: list[Citation] = []
        for r in f.get("boundingRegions") or []:
            cits.append(
                Citation(
                    page=r.get("pageNumber"),
                    bbox=self._region_bbox(r, page_meta),
                    text=f.get("content"),
                )
            )
        return cits

    def _documents_to_fields(self, documents: list, page_meta: dict) -> dict[str, TypedField]:
        tf: dict[str, TypedField] = {}
        for doc in documents:
            for name, f in (doc.get("fields") or {}).items():
                tf[name] = TypedField(
                    value=_field_value(f),
                    type=f.get("type"),
                    normalized_value=(
                        f.get("valueCurrency") or f.get("valueAddress") or f.get("valueDate")
                    ),
                    confidence=f.get("confidence"),
                    citations=self._field_citations(f, page_meta) or None,
                )
        return tf

    def _kv_pairs_to_fields(self, ar: dict, page_meta: dict) -> dict[str, TypedField]:
        """keyValuePairs (layout `keyValuePairs` feature) → typed_fields, citation from the value
        geometry. Previously unread (P2)."""
        tf: dict[str, TypedField] = {}
        for kv in ar.get("keyValuePairs") or []:
            key = (kv.get("key") or {}).get("content")
            if not key:
                continue
            val = kv.get("value") or {}
            cits = [
                Citation(
                    page=r.get("pageNumber"),
                    bbox=self._region_bbox(r, page_meta),
                    text=val.get("content"),
                )
                for r in (val.get("boundingRegions") or [])
            ]
            tf[key] = TypedField(
                value=val.get("content"), confidence=kv.get("confidence"), citations=cits or None
            )
        return tf

    def report_cost(self, job: Job) -> CostReport:
        """The pages Azure returned in `analyzeResult`, unpriced."""
        raw = job.raw.payload if job.raw else {}
        pages = len((raw.get("analyzeResult") or {}).get("pages", [])) or 1
        return CostReport(native_unit="page", native_quantity=float(pages))


def _spans_overlap(spans_a: list, spans_b: list) -> bool:
    for a in spans_a:
        a0, a1 = a.get("offset", 0), a.get("offset", 0) + a.get("length", 0)
        for b in spans_b:
            b0, b1 = b.get("offset", 0), b.get("offset", 0) + b.get("length", 0)
            if a0 < b1 and b0 < a1:
                return True
    return False


# Azure cell `kind`s that denote a header (§4.7). Everything else (content/description) is a body.
_HEADER_KINDS = frozenset({"columnHeader", "rowHeader", "stubHead"})


def _span_offset(spans: list) -> int | None:
    """The first content-span offset of an element — the reading-order key for order_by_position."""
    return spans[0].get("offset") if spans else None


def _word_confidence(spans: list, words: list) -> float | None:
    """block_confidence (D) — min-aggregate the confidences of the words whose content spans overlap
    this element's spans (the span-overlap path §4.5 promises). None when no word overlaps."""
    confs = [w.get("confidence") for w in words if _spans_overlap(spans, [w["span"]])]
    return aggregate_confidence(confs)


def _field_value(f: dict):
    """A prebuilt document field's natural JSON value — recursing into valueArray/valueObject so
    invoice line items stay nested structures instead of being flattened to strings (§4.6)."""
    if f.get("valueArray") is not None:
        return [_field_value(item) for item in f["valueArray"]]
    if f.get("valueObject") is not None:
        return {k: _field_value(v) for k, v in f["valueObject"].items()}
    for k in (
        "valueString",
        "valueNumber",
        "valueInteger",
        "valueDate",
        "valueTime",
        "valueBoolean",
        "valuePhoneNumber",
        "valueCountryRegion",
        "valueSelectionMark",
    ):
        if f.get(k) is not None:
            return f[k]
    if f.get("valueCurrency") is not None:
        return f["valueCurrency"]
    if f.get("valueAddress") is not None:
        return f["valueAddress"]
    return f.get("content")
