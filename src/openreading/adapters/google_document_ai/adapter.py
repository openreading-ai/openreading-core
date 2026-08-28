"""Google Document AI adapter. The Document proto is INDEX-ANCHORED: a single top-level `text`
string, and every element carries a layout.textAnchor.textSegments[{startIndex,endIndex}] that
slices into it (indexes are strings; index 0 / coord 0 are omitted). Geometry is
boundingPoly.normalizedVertices (top-left, 0-1). Confidence is already 0-1 (NOT /100).

Paragraphs → reading-order blocks, tables → cells, formFields + entities (extractor processors)
→ typed_fields. BYO GCP (project + service-account/ADC). Self-serve HIPAA BAA (Document AI is on
the GCP covered-products list); does not train on customer data. INLINE :process (sync, ≤15pp);
:batchProcess via GCS is a future POLL path.
"""

from __future__ import annotations

from typing import Any, Protocol

from openreading.adapters.base import BackendAdapter
from openreading.derive import (
    GridCell,
    aggregate_confidence,
    cells_to_grid,
    escape_md,
    order_by_position,
    table_to_pipe_md,
    table_to_text,
    utf8_slice,
)
from openreading.types.blocks import Block, Citation, TypedField
from openreading.types.cost import CostBasis, CostReport
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
from openreading.types.errors import RetryableError, TerminalError
from openreading.types.geometry import bbox_from_polygon
from openreading.types.job import Job
from openreading.types.request import OpenReadingRequest, Outputs
from openreading.types.response import (
    BackendInfo,
    BackendRaw,
    Document,
    NormalizedResponse,
    Page,
    Status,
)
from openreading.types.runtime import Health, RawResult, RunContext

X = ChannelGrade.IMPOSSIBLE
N = ChannelGrade.NATIVE
D = ChannelGrade.DERIVABLE

_UNIT_MAP = {
    "POINTS": (PageUnit.PDF_POINT, NativeUnit.PDF_POINT),
    "points": (PageUnit.PDF_POINT, NativeUnit.PDF_POINT),
    "INCH": (PageUnit.INCH, NativeUnit.INCH),
    "inch": (PageUnit.INCH, NativeUnit.INCH),
    "PIXEL": (PageUnit.PIXEL, NativeUnit.PIXEL),
    "pixel": (PageUnit.PIXEL, NativeUnit.PIXEL),
}
_RETRYABLE = {"RESOURCE_EXHAUSTED", "UNAVAILABLE", "DEADLINE_EXCEEDED", "INTERNAL", "ABORTED"}


class DocAIClient(Protocol):
    def process(self, processor_name: str, content: bytes, mime_type: str) -> dict: ...


def _regional_endpoint(location: str | None) -> str | None:
    """The DocAI API endpoint for a processor's region. 'us' (and unset) use the default global
    endpoint (None); every other region MUST be pinned to <location>-documentai.googleapis.com or
    the call resolves to the wrong region and fails. (routing_and_compliance region handling.)"""
    if not location or location == "us":
        return None
    return f"{location}-documentai.googleapis.com"


class _RealDocAIClient:  # pragma: no cover - real GCP path
    def __init__(self, config: dict) -> None:
        self._config = config

    def process(self, processor_name: str, content: bytes, mime_type: str) -> dict:
        from google.cloud import documentai  # type: ignore

        endpoint = _regional_endpoint(self._config.get("location"))
        if endpoint:
            from google.api_core.client_options import ClientOptions  # type: ignore

            client = documentai.DocumentProcessorServiceClient(
                client_options=ClientOptions(api_endpoint=endpoint)
            )
        else:
            client = documentai.DocumentProcessorServiceClient()
        result = client.process_document(
            request={
                "name": processor_name,
                "raw_document": {"content": content, "mime_type": mime_type},
            }
        )
        from google.cloud.documentai_v1 import Document as _Doc  # type: ignore

        return {"document": _Doc.to_dict(result.document)}


def _descriptor() -> AdapterDescriptor:
    return AdapterDescriptor(
        id="google-document-ai",
        type=BackendType.HOSTED_API,
        # Ledger T4b (AC-8): INLINE-only, so R1's resume scenario is structurally inapplicable
        # (no non-terminal state to resume) and R2 already holds trivially (no client is ever
        # cached on self) — verified against the real R1/R2 conformance kit, not assumed.
        protocol_version=2,
        adapter_impl="http",
        operations=["OCR", "FormParser", "LayoutParser", "CustomExtractor"],
        provisioning=Provisioning(
            byo_mode=["cloud_credential"], auth="gcp_adc", billing_target="caller_account"
        ),
        wait_modes=[WaitMode.INLINE],
        capabilities=Capabilities(
            ocr="verified",
            handwriting="verified",
            printed_tables="verified",
            complex_tables="verified",
            forms_key_value="verified",
            layout="verified",
            reading_order="verified",
            classification="claimed",
            splitting="claimed",
            custom_schema_extraction="verified",
            languages=["en", "and 200+ (OCR)"],
            input_formats=["pdf", "tiff", "gif", "png", "jpg", "bmp", "webp"],
            max_pages_per_request="15 sync / 500 batch",
        ),
        cost=Cost(
            native_unit="page",
            basis="estimated",
            usd_per_page_equiv_low=0.0006,
            usd_per_page_equiv_high=0.03,
            lossiness="none",
        ),
        compliance=ComplianceProfile(
            hipaa_baa="yes",
            soc2="verified",
            gdpr="verified",
            pci="verified",
            trains_on_customer_data="no",
            data_region_options=["us", "eu", "europe-west2", "europe-west3", "asia-south1"],
            data_retention="in-memory sync processing (~0 retention)",
            max_retention_hours=24,
            runs_fully_local=False,
        ),
        runtime=RuntimeProfile(
            offline_capable=False, license="proprietary", version_pin="documentai v1"
        ),
        output=Output(
            paradigms=[OutputParadigm.BLOCK_TREE, OutputParadigm.TYPED_FIELDS],
            block_granularity="paragraph",  # v0.3 hint (§4.3): DocAI blocks are paragraphs
            channels=OutputChannels(
                markdown=D,  # v0.5: real derivation (blocks + pipe tables), not a text copy
                text=N,
                blocks=N,
                block_bbox=N,
                block_confidence=N,
                typed_fields=N,
                table_cells=N,
            ),
        ),
        router=RouterHints(
            normalization_difficulty="high",
            integration_priority="P0",
            priority_reason="Best-in-class hosted PHI path: self-serve BAA + verified no-train + handwriting.",
        ),
        # Document AI authenticates by ADC (ambient); credentials_path (BL-155) is graded secret
        # even though it is only a path, not a bearer token — its resolved value is where the
        # service-account key FILE lives on disk (directory layout, sometimes a username or a
        # secrets-mount convention), which is reconnaissance-grade disclosure this codebase's
        # redaction mechanism (credentials.secret_values / readiness.auth_hinted) should scrub out
        # of failure messages the same as any other credential. It stays required=False: ADC can
        # also resolve from gcloud's own ambient chain with no env var set at all.
        credentials_spec=[
            CredentialField(
                key="credentials_path",
                required=False,
                env=["GOOGLE_APPLICATION_CREDENTIALS"],
                description="service-account JSON for ADC; read ambiently by the google SDK.",
            ),
        ],
        config_spec=[
            ConfigField(
                key="project_id",
                required=True,
                env=["GCP_PROJECT_ID", "GOOGLE_CLOUD_PROJECT"],
            ),
            ConfigField(key="processor_id", required=True, env=["GCP_PROCESSOR_ID"]),
            ConfigField(key="location", env=["GCP_LOCATION"], example="us"),
        ],
        signup_url="https://cloud.google.com/document-ai",
        accepts_url=False,
        live_gate_env=["GOOGLE_APPLICATION_CREDENTIALS", "GCP_PROJECT_ID", "GCP_PROCESSOR_ID"],
        # BL-166: checked the official REST reference for ProcessRequest (the sync `:process`
        # method this adapter's submit() calls) — its body is skipHumanReview/fieldMask/
        # processOptions/labels/imagelessMode/inlineDocument/rawDocument/gcsDocument. No
        # requestId field, and no mention of "idempoten*" anywhere on the page. BatchProcessRequest
        # (the LRO method, unused here) has no requestId either, so this isn't even a case of the
        # sync method lacking a mechanism the async one has. An invented header the API ignores
        # would be worse than this honest declaration.
        idempotency_supported=False,
        # BL-164: this adapter's dispatch is INLINE-only (the sync `:process` call this submit()
        # uses completes before returning) — there is never a live, cancellable vendor job in
        # flight by the time a `race`/`pick: best` node's loser-cancellation path could run. Honest
        # false rather than a no-op that implies an effect. (`BatchProcessRequest`, the LRO/async
        # method, is unused by this adapter and out of this item's scope.)
        cancel_supported=False,
        sources=[
            Source(
                url="https://cloud.google.com/document-ai/docs",
                accessed="2026-07-21",
                supports="Document proto index-anchored shape, HIPAA covered product, no-train",
            ),
            Source(
                url="https://cloud.google.com/document-ai/docs/reference/rest/v1/projects.locations.processors/process",
                accessed="2026-08-22",
                supports="BL-166: ProcessRequest body (checked batchProcess too, for contrast) — "
                "no requestId field (AIP-155) on either method",
            ),
        ],
    )


class GoogleDocumentAIAdapter(BackendAdapter):
    def __init__(self, client: DocAIClient | None = None) -> None:
        self.descriptor = _descriptor()
        self._client = client
        self._page_dim: dict[int, dict] = {}  # page-index → dimension (set per normalize())

    def health(self) -> Health:
        if self._client is not None:
            return Health(ready=True, detail="injected client")
        try:
            import google.cloud.documentai  # type: ignore  # noqa: F401
        except ImportError:
            return Health(
                ready=False,
                missing_deps=[
                    "google-cloud-documentai (pip install 'openreading[google-document-ai]')"
                ],
            )
        return Health(ready=True)

    def _get_client(self, ctx: RunContext) -> DocAIClient:
        if self._client is not None:
            return self._client
        return _RealDocAIClient(ctx.runtime or {})  # pragma: no cover

    def _processor_name(self, ctx: RunContext) -> str:
        runtime = ctx.runtime or {}
        project = runtime.get("project_id")
        location = runtime.get("location") or "us"
        processor = runtime.get("processor_id")
        if not project or not processor:
            raise TerminalError(
                "Document AI needs config → {project_id, processor_id, location}",
                backend_code="no_credentials",
            )
        return f"projects/{project}/locations/{location}/processors/{processor}"

    def submit(self, req: OpenReadingRequest, ctx: RunContext) -> Job:
        client = self._get_client(ctx)
        name = self._processor_name(ctx)
        d = req.document
        if d.bytes_base64:
            import base64

            content = base64.b64decode(d.bytes_base64)
        elif d.path:
            with open(d.path, "rb") as fh:
                content = fh.read()
        else:
            raise TerminalError(
                "Document AI sync needs document bytes/path", backend_code="unsupported_input"
            )
        try:
            raw = client.process(name, content, d.mime_type or "application/pdf")
        except (RetryableError, TerminalError):
            raise
        except Exception as e:  # noqa: BLE001
            raise self._map_error(e) from e
        job = self.new_job(
            WaitMode.INLINE, state=JobState.SUCCEEDED, idempotency_key=ctx.idempotency_key
        )
        job.raw = RawResult(
            payload=raw,
            media_type="application/json",
            encoding="json",
            object_class="documentai.Document",
        )
        return job

    def _map_error(self, e: Exception):
        code = getattr(e, "code", None)
        name = getattr(code, "name", None) or type(e).__name__
        if name in ("PERMISSION_DENIED", "UNAUTHENTICATED", "PermissionDenied", "Unauthenticated"):
            return TerminalError(str(e), backend_code="auth_rejected")
        if name in _RETRYABLE:
            return RetryableError(str(e), backend_code=name)
        return TerminalError(str(e), backend_code=name)

    # ---- normalize (index-anchored) ------------------------------------------
    def normalize(
        self, job: Job, ctx: RunContext, slim_req: OpenReadingRequest
    ) -> NormalizedResponse:
        raw: dict[str, Any] = job.raw.payload if job.raw else {}
        doc = raw.get("document", raw)
        text = doc.get("text", "")
        outputs = slim_req.outputs or Outputs()

        doc_pages = doc.get("pages", [])
        # page-index → dimension, for citation geometry that references a page by 0-based index
        self._page_dim = {i: pg.get("dimension", {}) for i, pg in enumerate(doc_pages)}
        page_objs: list[Page] = []
        for pg in doc_pages:
            pno = int(pg.get("pageNumber", len(page_objs) + 1))
            dim = pg.get("dimension", {})
            unit_enum = _UNIT_MAP.get(
                dim.get("unit", "POINTS"), (PageUnit.PDF_POINT, NativeUnit.PDF_POINT)
            )[0]
            # Build (block, span-offset) pairs so mid-page tables interleave with paragraphs in
            # true reading order (P1/P5) — the provider groups all tables AFTER all paragraphs.
            keyed: list[tuple[Block, int | None]] = []
            for para in pg.get("paragraphs", []):
                layout = para.get("layout", {})
                keyed.append(
                    (
                        Block(
                            type=BlockType.TEXT,
                            native_type="paragraph",
                            text=self._anchor_text(text, layout) or None,
                            bbox=self._poly_bbox(layout, pno, dim),
                            confidence=self._conf(layout.get("confidence")),
                        ),
                        self._anchor_start(layout),
                    )
                )
            for tbl in pg.get("tables", []):
                keyed.append((self._table_block(tbl, text, pno, dim), self._table_start(tbl)))
            blocks = order_by_position([b for b, _ in keyed], [k for _, k in keyed])
            for i, b in enumerate(blocks):
                b.reading_order = i
            page_objs.append(
                Page(
                    page_number=pno,
                    width=dim.get("width"),
                    height=dim.get("height"),
                    unit=unit_enum,
                    blocks=blocks,
                )
            )

        typed_fields = self._entities_to_fields(text, doc.get("entities", []))
        typed_fields.update(self._formfields(text, doc_pages))

        markdown = self._doc_markdown(page_objs)
        resp = NormalizedResponse(
            status=Status(state=ResponseState.SUCCEEDED),
            backend=BackendInfo(
                id="google-document-ai",
                type=BackendType.HOSTED_API,
                output_paradigm=[OutputParadigm.BLOCK_TREE],
            ),
            document=Document(
                text=text if outputs.text else None,  # native, complete (includes table cells)
                markdown=(markdown or None) if outputs.markdown else None,
                page_count=len(page_objs),
                pages=page_objs,
            ),
            typed_fields=typed_fields or None,
        )
        # v0.3 provenance (§3.3/§6.4): text/blocks/cells/fields are native; markdown is the
        # platform's projection (blocks assembled + pipe tables), never a copy of `text`.
        resp.channel_provenance = {
            "text": "native",
            "markdown": "derived",
            "blocks": "native",
            "block_bbox": "native",
            "table_cells": "native",
            "typed_fields": "native",
        }
        if outputs.include_backend_raw:
            resp.backend_raw = BackendRaw(
                encoding="json",
                media_type="application/json",
                object_class="documentai.Document",
                payload=raw,
            )
        return resp

    def _doc_markdown(self, pages: list[Page]) -> str:
        """Assemble a real markdown from the ordered blocks (§4.2/C3): tables render as GFM pipe
        tables via `table_to_pipe_md`; literal paragraph text is `escape_md`-escaped so content can
        never be reinterpreted as markup. Headings are NOT invented (the standard proto has none)."""
        parts: list[str] = []
        for pg in pages:
            for b in pg.blocks or []:
                if b.type is BlockType.TABLE and b.table is not None:
                    parts.append(table_to_pipe_md(b.table))
                elif b.text:
                    parts.append(escape_md(b.text))
        return "\n\n".join(p for p in parts if p)

    def _anchor_text(self, text: str, layout: dict) -> str:
        # textAnchor indices are UTF-8 BYTE offsets (P0): slicing by code points garbles every
        # document after its first multibyte char. utf8_slice slices the encoded bytes.
        ta = (layout or {}).get("textAnchor", {})
        parts = []
        for seg in ta.get("textSegments", []) or []:
            start = int(seg.get("startIndex", 0) or 0)
            end = int(seg.get("endIndex", 0) or 0)
            parts.append(utf8_slice(text, start, end))
        return "".join(parts).strip()

    def _anchor_start(self, layout: dict) -> int | None:
        """The startIndex (byte offset) of a layout's first text segment — the reading-order key."""
        segs = ((layout or {}).get("textAnchor", {}) or {}).get("textSegments", []) or []
        if not segs:
            return None
        return int(segs[0].get("startIndex", 0) or 0)

    def _table_start(self, tbl: dict) -> int | None:
        """A table's reading-order key: its own layout anchor, else its first cell's start."""
        own = self._anchor_start(tbl.get("layout", {}))
        if own:
            return own
        for row in (tbl.get("headerRows", []) or []) + (tbl.get("bodyRows", []) or []):
            for cell in row.get("cells", []) or []:
                start = self._anchor_start(cell.get("layout", {}))
                if start is not None:
                    return start
        return None

    def _poly_bbox(self, layout: dict, page: int, dim: dict | None = None):
        poly = (layout or {}).get("boundingPoly", {})
        nv = poly.get("normalizedVertices")
        if nv:
            pts = [[v.get("x", 0.0), v.get("y", 0.0)] for v in nv]
            return bbox_from_polygon(
                pts,
                origin=NativeOrigin.TOP_LEFT,
                unit=NativeUnit.NORMALIZED,
                page_width=1.0,
                page_height=1.0,
                page=page,
            )
        # P2: normalizedVertices absent → derive from pixel `vertices` + page dimensions (image
        # inputs report pixel dimensions in the same space as the vertices), instead of None.
        verts = poly.get("vertices")
        pw, ph = (dim or {}).get("width"), (dim or {}).get("height")
        if verts and pw and ph:
            pts = [[v.get("x", 0.0), v.get("y", 0.0)] for v in verts]
            return bbox_from_polygon(
                pts,
                origin=NativeOrigin.TOP_LEFT,
                unit=NativeUnit.PIXEL,
                page_width=float(pw),
                page_height=float(ph),
                page=page,
            )
        return None

    def _conf(self, v) -> float | None:
        # DocAI confidence is already 0-1 (NOT 0-100)
        return float(v) if v is not None and 0.0 <= float(v) <= 1.0 else None

    def _cell_bbox_dict(self, layout: dict, page: int, dim: dict | None) -> dict | None:
        bb = self._poly_bbox(layout, page, dim)
        return bb.model_dump(mode="json", exclude_none=True) if bb is not None else None

    def _table_block(self, tbl: dict, text: str, page: int, dim: dict | None = None) -> Block:
        # Build the grid through the shared occupancy cursor (P9): enumeration indices are wrong
        # under merged cells. col=None lets `cells_to_grid` assign the true column past spans.
        header = tbl.get("headerRows", []) or []
        body = tbl.get("bodyRows", []) or []
        grid_cells: list[GridCell] = []
        for ri, row in enumerate(header + body):
            for cell in row.get("cells", []) or []:
                grid_cells.append(
                    GridCell(
                        row=ri,
                        col=None,
                        row_span=cell.get("rowSpan", 1) or 1,
                        col_span=cell.get("colSpan", 1) or 1,
                        text=self._anchor_text(text, cell.get("layout", {})) or None,
                        is_header=(ri < len(header)),
                        bbox=self._cell_bbox_dict(cell.get("layout", {}), page, dim),
                    )
                )
        table = cells_to_grid(grid_cells)
        return Block(
            type=BlockType.TABLE,
            native_type="table",
            text=table_to_text(table) or None,  # C2: table content reaches the block/text channel
            bbox=self._poly_bbox(tbl.get("layout", {}), page, dim),
            table=table,
        )

    def _entity_value(self, text: str, ent: dict):
        """The natural-JSON value of an entity: a nested {sub_type: value} object when the entity
        has `properties` (recursed), else its scalar mention/normalized text (§4.6)."""
        props = ent.get("properties") or []
        if props:
            return self._group_values(text, props)
        nv = ent.get("normalizedValue") or {}
        return (
            ent.get("mentionText")
            or self._anchor_text(text, ent.get("layout", {}))
            or (nv.get("text") or None)
        )

    def _group_values(self, text: str, entities: list) -> dict:
        """Nested properties → natural-JSON dict; a repeated sub-type collects into a list (§4.6)."""
        grouped: dict[str, Any] = {}
        for ent in entities:
            key = ent.get("type")
            if not key:
                continue
            val = self._entity_value(text, ent)
            if key in grouped:
                if isinstance(grouped[key], list):
                    grouped[key].append(val)
                else:
                    grouped[key] = [grouped[key], val]
            else:
                grouped[key] = val
        return grouped

    def _page_anchor_citations(self, text: str, ent: dict) -> list[Citation]:
        """pageAnchor.pageRefs → TypedField.citations (§4.6): page index is 0-based into
        document.pages; geometry rides boundingPoly (normalized or pixel vertices)."""
        cits: list[Citation] = []
        for ref in (ent.get("pageAnchor") or {}).get("pageRefs", []) or []:
            page_no = int(ref.get("page", 0) or 0) + 1
            dim = self._page_dim.get(page_no - 1)
            cits.append(
                Citation(
                    page=page_no,
                    bbox=self._poly_bbox(ref, page_no, dim),  # ref carries boundingPoly directly
                    text=ent.get("mentionText") or None,
                    confidence=self._conf(ref.get("confidence")),
                )
            )
        return cits

    def _entities_to_fields(self, text: str, entities: list) -> dict[str, TypedField]:
        # Group by type so a repeated entity type collects into a LIST value instead of
        # last-writer-wins (P0/§4.6); nested `properties` and pageAnchor citations are preserved.
        by_key: dict[str, list[dict]] = {}
        for ent in entities:
            key = ent.get("type")
            if key:
                by_key.setdefault(key, []).append(ent)
        tf: dict[str, TypedField] = {}
        for key, ents in by_key.items():
            if len(ents) == 1:
                ent = ents[0]
                nv = ent.get("normalizedValue") or {}
                tf[key] = TypedField(
                    value=self._entity_value(text, ent),
                    type=key,
                    normalized_value=(nv.get("text") or nv or None) if nv else None,
                    confidence=self._conf(ent.get("confidence")),
                    citations=self._page_anchor_citations(text, ent) or None,
                )
            else:
                cits: list[Citation] = []
                for ent in ents:
                    cits.extend(self._page_anchor_citations(text, ent))
                tf[key] = TypedField(
                    value=[self._entity_value(text, ent) for ent in ents],
                    type=key,
                    confidence=aggregate_confidence(
                        [self._conf(ent.get("confidence")) for ent in ents]
                    ),
                    citations=cits or None,
                )
        return tf

    def _formfields(self, text: str, pages: list) -> dict[str, TypedField]:
        tf: dict[str, TypedField] = {}
        for pg in pages:
            for ff in pg.get("formFields", []) or []:
                key = self._anchor_text(
                    text, ff.get("fieldName", {}).get("layout", ff.get("fieldName", {}))
                )
                val = self._anchor_text(
                    text, ff.get("fieldValue", {}).get("layout", ff.get("fieldValue", {}))
                )
                if key:
                    tf[key.rstrip(":")] = TypedField(
                        value=val,
                        confidence=self._conf((ff.get("fieldValue", {}) or {}).get("confidence")),
                    )
        return tf

    def report_cost(self, job: Job) -> CostReport:
        raw = job.raw.payload if job.raw else {}
        pages = len((raw.get("document", raw) or {}).get("pages", [])) or 1
        return CostReport(
            native_unit="page",
            native_quantity=float(pages),
            cost_usd=0.0015 * pages,
            basis=CostBasis.ESTIMATED,
            billing_target="caller_account",
        )
