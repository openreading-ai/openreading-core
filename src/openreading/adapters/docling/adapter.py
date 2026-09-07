"""Docling adapter — the first ContainerAdapter. Talks to a self-hosted docling-serve container
over HTTP; because the container runs in the caller's own infra, data never leaves the environment
(it runs on hardware you control, so no document leaves it). MIT-licensed.

The DoclingDocument is a tree: `body.children` are JSON-pointer refs (`#/texts/0`, `#/tables/0`,
`#/groups/1`) that we walk to linearize reading order. Each item's provenance carries a bbox with
an explicit `coord_origin` — Docling uses BOTTOMLEFT (y-up), so we convert with to_canonical
origin=BOTTOM_LEFT using pages[page_no].size, which lands the same canonical box as a top-left
source (verified: title l/t/r/b bottom-left → y=64.14/792, the tight glyph box). Per-item
confidence is UNVERIFIED (only a page/overall report exists) → block_confidence=X, never faked;
docling's overall ConfidenceScores instead route to document/page confidence (never smeared onto
blocks).

v0.5 (Phase B.5): text is now NATIVE — we request `to_formats+['text']` so docling emits plain
text itself (table content included, formula LaTeX excluded), rather than concatenating block
text; typed_fields is DERIVED from docling's key_value_items/form_items graphs; tables are built
through the shared occupancy grid (derive.cells_to_grid); captions are recursed and emitted.
"""

from __future__ import annotations

from typing import Any, Protocol

from openreading.adapters._http import error_for_status
from openreading.adapters.base import BackendAdapter
from openreading.derive import GridCell, aggregate_confidence, cells_to_grid, table_to_text
from openreading.liveness import probe_http
from openreading.types.blocks import Block, Citation, TypedField
from openreading.types.cost import CostReport, infra_only
from openreading.types.descriptor import (
    AdapterDescriptor,
    Capabilities,
    ConfigField,
    LivenessProbe,
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
from openreading.types.geometry import to_canonical
from openreading.types.job import Job
from openreading.types.liveness import ProbeResult
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

_LABEL_MAP = {
    "title": BlockType.TITLE,
    "section_header": BlockType.SECTION_HEADER,
    "page_header": BlockType.HEADER,
    "page_footer": BlockType.FOOTER,
    "page_number": BlockType.PAGE_NUMBER,
    "list_item": BlockType.LIST_ITEM,
    "text": BlockType.TEXT,
    "paragraph": BlockType.TEXT,
    "caption": BlockType.CAPTION,
    "footnote": BlockType.TEXT,
    "formula": BlockType.FORMULA,
    "code": BlockType.CODE,
    "checkbox_selected": BlockType.SELECTION_MARK,
    "checkbox_unselected": BlockType.SELECTION_MARK,
}


def _is_key_cell(cell: dict) -> bool:
    """A docling GraphCell labelled KEY (vs VALUE) — used to orient key→value links."""
    return str(cell.get("label", "")).lower().startswith("key")


def _overall_confidence(scores: Any) -> float | None:
    """Aggregate a docling ConfidenceScores block (ocr/parse/layout/table sub-scores, each a
    [0,1] float, NaN/None when unset) to a single quality floor: the min of the present scores
    (aggregate_confidence's documented choice). None when nothing usable is present — never
    fabricated, and never smeared onto blocks (block_confidence stays X)."""
    if not isinstance(scores, dict):
        return None
    vals: list[float] = []
    for k in ("ocr_score", "parse_score", "layout_score", "table_score"):
        v = scores.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and 0.0 <= v <= 1.0:
            vals.append(float(v))
    return aggregate_confidence(vals)


class DoclingClient(Protocol):
    def convert(self, document: dict, options: dict) -> dict: ...


class _HttpxDoclingClient:  # pragma: no cover - real container path
    def __init__(self, base_url: str) -> None:
        from openreading.adapters._http import build_httpx_client

        self._http = build_httpx_client(base_url=base_url.rstrip("/"), timeout=300.0)

    def convert(self, document: dict, options: dict) -> dict:
        r = self._http.post("/v1/convert/source", json={"sources": [document], "options": options})
        if r.status_code >= 400:
            raise error_for_status(r.status_code, r.headers, message=r.text)
        return r.json()


def _descriptor() -> AdapterDescriptor:
    return AdapterDescriptor(
        id="docling",
        type=BackendType.OSS_LIBRARY,
        # Ledger T4b (AC-8): INLINE-only, so R1's resume scenario is structurally inapplicable
        # (no non-terminal state to resume) and R2 already holds trivially (no client is ever
        # cached on self) — verified against the real R1/R2 conformance kit, not assumed.
        protocol_version=2,
        adapter_impl="container",
        provisioning=Provisioning(
            byo_mode=["container"], auth="none"
        ),
        wait_modes=[WaitMode.INLINE],
        capabilities=Capabilities(
            ocr="claimed",
            handwriting=False,
            printed_tables="verified",
            complex_tables="verified",
            forms_key_value="claimed",
            layout="verified",
            reading_order="verified",
            figures_charts="claimed",
            multi_column="verified",
            input_formats=["pdf", "docx", "pptx", "xlsx", "html", "png", "jpg"],
        ),
        runtime=RuntimeProfile(
            offline_capable=True,
            license="MIT",
            system_deps=["docling-serve container (self-hosted)"],
            version_pin="docling-serve",
            hardware="cpu",
            sandbox="container",
        ),
        output=Output(
            paradigms=[OutputParadigm.BLOCK_TREE],
            block_granularity="element",  # v0.3 hint (§4.3): docling blocks are layout elements
            channels=OutputChannels(
                markdown=N,
                text=N,  # v0.5: native plain text via to_formats+['text'] (was D)
                blocks=N,
                block_bbox=N,
                block_confidence=X,
                typed_fields=D,  # v0.5: derived from key_value_items/form_items graphs (was X)
                table_cells=N,
            ),
        ),
        router=RouterHints(
            normalization_difficulty="medium",
        ),
        config_spec=[
            ConfigField(
                key="endpoint",
                required=True,
                env=["DOCLING_SERVE_URL"],
                example="http://localhost:5001",
                description="URL of a self-hosted docling-serve container (no data leaves your infra).",
            ),
        ],
        accepts_url=True,
        live_gate_env=["DOCLING_SERVE_URL"],
        liveness=LivenessProbe(
            probe="endpoint",
            method="GET {DOCLING_SERVE_URL}/health",
            timeout_s=5.0,
            notes=(
                "The container runs in your own infrastructure, so this never leaves your network "
                "and can never cost anything. A healthy answer proves the container is up and "
                "serving; it does not predict how a large document will convert."
            ),
        ),
        sources=[
            Source(
                url="https://docling-project.github.io/docling/",
                accessed="2026-07-21",
                supports="DoclingDocument tree, provenance bbox coord_origin, export renditions",
            )
        ],
    )


class DoclingAdapter(BackendAdapter):
    def __init__(
        self, client: DoclingClient | None = None, *, probe_client: Any | None = None
    ) -> None:
        self.descriptor = _descriptor()
        self._client = client
        # Separate injectable port for the LIVENESS probe (internal/design/liveness.md §3.2): the
        # execution client speaks `convert`, the probe speaks a plain HTTP GET, and keeping them
        # apart means an offline test can drive the probe without a fake that pretends to convert.
        self._probe_client = probe_client

    def health(self) -> Health:
        if self._client is not None:
            return Health(ready=True, detail="injected client")
        try:
            import httpx  # noqa: F401
        except ImportError:
            return Health(ready=False, missing_deps=["httpx (pip install 'openreading[docling]')"])
        # This method is offline by construction and CANNOT know whether the container is up —
        # `probe_liveness` below is the one that can. Until it is run, "ready" here means
        # configured, never reachable (the exact gap internal/design/liveness.md §1 exists to close).
        return Health(ready=True, detail="requires a running docling-serve container")

    def probe_liveness(self, ctx: RunContext, *, timeout_s: float) -> ProbeResult:
        """docling-serve's own health endpoint. The container runs in the caller's infrastructure,
        so this is `endpoint` kind: real network I/O that never leaves their network and can never
        cost anything."""
        endpoint = self._endpoint(ctx)
        if not endpoint:  # pragma: no cover - check_liveness gates on readiness before calling
            return ProbeResult.unreachable("no endpoint configured (DOCLING_SERVE_URL)")
        return probe_http(
            f"{endpoint.rstrip('/')}/health",
            timeout_s=timeout_s,
            env_hint="DOCLING_SERVE_URL",
            client=self._probe_client,
        )

    def _endpoint(self, ctx: RunContext) -> str | None:
        runtime = ctx.runtime or {}
        creds = (ctx.credentials.values if ctx.credentials else {}) or {}
        return runtime.get("endpoint") or creds.get("endpoint")

    def _get_client(self, ctx: RunContext) -> DoclingClient:
        if self._client is not None:
            return self._client
        runtime = ctx.runtime or {}
        endpoint = runtime.get("endpoint") or (
            (ctx.credentials.values if ctx.credentials else {}) or {}
        ).get("endpoint")
        if not endpoint:
            raise TerminalError(
                "Docling needs runtime.endpoint (docling-serve URL)", backend_code="no_endpoint"
            )
        return _HttpxDoclingClient(endpoint)  # pragma: no cover

    def submit(self, req: OpenReadingRequest, ctx: RunContext) -> Job:
        self.assert_supports(req)  # raises UnsupportedFeatureError for extraction_schema
        client = self._get_client(ctx)
        d = req.document
        if d.url:
            document = {"kind": "http", "url": d.url}
        elif d.bytes_base64:
            document = {
                "kind": "file",
                "base64_string": d.bytes_base64,
                "filename": d.filename or "doc.pdf",
            }
        else:
            raise TerminalError(
                "Docling needs document url/bytes", backend_code="unsupported_input"
            )
        options = {
            # 'text' → docling emits a native plain-text rendition (text channel = N): table
            # content included, formula LaTeX rendered out — fixes both P0 text leaks at the source.
            "to_formats": ["md", "json", "text"],
            "do_ocr": (req.features.ocr != "off") if req.features else True,
        }
        try:
            raw = client.convert(document, options)
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
            object_class="docling_core.types.doc.DoclingDocument",
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
        outputs = slim_req.outputs or Outputs()
        doc_wrap = raw.get("document", raw)
        ddoc = doc_wrap.get("json_content") or doc_wrap.get("json") or doc_wrap
        md_content = doc_wrap.get("md_content") or doc_wrap.get("markdown")
        text_content = doc_wrap.get("text_content")
        conf_report = raw.get("confidence") or doc_wrap.get("confidence") or {}
        conf_pages = conf_report.get("pages", {}) if isinstance(conf_report, dict) else {}

        pages_meta = ddoc.get("pages", {}) or {}
        ordered = self._linearize(ddoc)
        pages_blocks: dict[int, list[Block]] = {}
        for kind, item in ordered:
            page_no, block = self._to_block(kind, item, pages_meta)
            if block is not None:
                pages_blocks.setdefault(page_no, []).append(block)

        page_objs = []
        for pno in sorted(pages_blocks):
            size = (pages_meta.get(str(pno)) or pages_meta.get(pno) or {}).get("size", {})
            bl = pages_blocks[pno]
            for i, b in enumerate(bl):
                b.reading_order = i
            page_objs.append(
                Page(
                    page_number=pno,
                    width=size.get("width"),
                    height=size.get("height"),
                    unit=PageUnit.PDF_POINT,
                    blocks=bl,
                    confidence=_overall_confidence(conf_pages.get(str(pno)) or conf_pages.get(pno)),
                )
            )

        # text channel (§4.1): docling's native plain rendition (N) when present, else a plain
        # projection of the block spine (D) — table content joined, formula LaTeX excluded.
        text_provenance: str | None = None
        doc_text: str | None = None
        if outputs.text:
            if isinstance(text_content, str) and text_content.strip():
                doc_text, text_provenance = text_content, "native"
            else:
                doc_text, text_provenance = (self._derived_text(page_objs) or None), "derived"

        resp = NormalizedResponse(
            status=Status(state=ResponseState.SUCCEEDED),
            backend=BackendInfo(
                id="docling",
                type=BackendType.OSS_LIBRARY,
                output_paradigm=[OutputParadigm.BLOCK_TREE],
            ),
            document=Document(
                markdown=md_content if outputs.markdown else None,
                text=doc_text,
                page_count=len(pages_meta) or len(page_objs),
                pages=page_objs,
                confidence=_overall_confidence(conf_report),
            ),
        )

        # typed_fields (§4.6): derived from docling's key_value_items/form_items graphs (X→D).
        typed_fields = None
        if outputs.typed_fields:
            typed_fields = self._typed_fields(ddoc, pages_meta) or None
            if typed_fields:
                resp.typed_fields = typed_fields
            else:  # requested but this document has no KV/form regions — C6 deliver-or-warn
                resp.add_warning(
                    "typed_fields_empty",
                    "Docling found no key-value or form regions in this document",
                    "typed_fields",
                )

        resp.channel_provenance = self._provenance(
            page_objs, md_content if outputs.markdown else None, text_provenance, typed_fields
        )

        if outputs.include_backend_raw:
            resp.backend_raw = BackendRaw(
                encoding="json_serialized_object",
                media_type="application/vnd.docling+json",
                object_class="docling_core.types.doc.DoclingDocument",
                payload=raw,
            )
        return resp

    def _derived_text(self, page_objs: list[Page]) -> str:
        """Plain-text fallback when docling returns no native `text_content`: the block spine
        (table cells already projected to tab-joined rows) with formula LaTeX omitted (no honest
        LaTeX→plain rendering is vendored, so it never leaks into the plain channel)."""
        parts: list[str] = []
        for pb in page_objs:
            for b in pb.blocks or []:
                if b.type is BlockType.FORMULA:
                    continue
                if b.text:
                    parts.append(b.text)
        return "\n\n".join(parts)

    def _provenance(
        self,
        page_objs: list[Page],
        markdown: str | None,
        text_provenance: str | None,
        typed_fields: dict[str, TypedField] | None,
    ) -> dict[str, str]:
        """Per-response native/derived signal (§3.3/§6.4). markdown/blocks/geometry/cells are
        docling-native; text is native or the derived fallback; typed_fields is derived."""
        prov: dict[str, str] = {}
        if markdown:
            prov["markdown"] = "native"
        if text_provenance:
            prov["text"] = text_provenance
        blocks = [b for pb in page_objs for b in pb.blocks or []]
        if blocks:
            prov["blocks"] = "native"
            if any(b.bbox for b in blocks):
                prov["block_bbox"] = "native"
            if any(b.table for b in blocks):
                prov["table_cells"] = "native"
        if typed_fields:
            prov["typed_fields"] = "derived"
        return prov

    def _typed_fields(self, ddoc: dict, pages_meta: dict) -> dict[str, TypedField]:
        """key_value_items / form_items → typed_fields. Each region carries a graph of key/value
        cells joined by links; a link's key cell names the field and the value cell fills it, with
        the value cell's provenance bbox recorded as a Citation (§4.6)."""
        tf: dict[str, TypedField] = {}
        regions = [*(ddoc.get("key_value_items") or []), *(ddoc.get("form_items") or [])]
        for region in regions:
            graph = region.get("graph") or {}
            cells = {c.get("cell_id"): c for c in (graph.get("cells") or [])}
            for link in graph.get("links") or []:
                a = cells.get(link.get("source_cell_id"))
                b = cells.get(link.get("target_cell_id"))
                if not isinstance(a, dict) or not isinstance(b, dict):
                    continue
                key_cell, val_cell = (a, b) if _is_key_cell(a) else (b, a)
                name = (key_cell.get("text") or "").strip()
                if not name:
                    continue
                tf[name] = TypedField(
                    value=val_cell.get("text"),
                    citations=self._cell_citation(val_cell, pages_meta) or None,
                )
        return tf

    def _cell_citation(self, cell: dict, pages_meta: dict) -> list[Citation]:
        prov = cell.get("prov")
        if isinstance(prov, list):
            prov = prov[0] if prov else None
        if not isinstance(prov, dict):
            return []
        page_no, bbox = self._canonical_bbox(prov, pages_meta)
        return [Citation(page=page_no, bbox=bbox, text=cell.get("text"))]

    def _linearize(self, ddoc: dict) -> list[tuple[str, dict]]:
        """DFS over body.children (JSON-pointer refs) to reading order; recurse into groups."""
        out: list[tuple[str, dict]] = []
        seen: set[str] = set()

        def resolve(ref: str) -> tuple[str, dict | None]:
            parts = ref.lstrip("#/").split("/")
            if len(parts) != 2:
                return "", None
            coll, idx = parts[0], parts[1]
            arr = ddoc.get(coll)
            if not isinstance(arr, list):
                return coll, None
            try:
                return coll, arr[int(idx)]
            except (ValueError, IndexError):
                return coll, None

        def emit_captions(item: dict) -> None:
            # captions live in an item's `captions` ref list (not body.children); resolve + emit
            # them right after their figure/table so they aren't silently dropped (P1).
            for cap in item.get("captions", []) or []:
                cref = cap.get("$ref") or cap.get("cref")
                if not cref or cref in seen:
                    continue
                seen.add(cref)
                ccoll, citem = resolve(cref)
                if citem is not None:
                    out.append((ccoll, citem))

        def walk(node: dict):
            for ch in node.get("children", []) or []:
                ref = ch.get("$ref") or ch.get("cref")
                if not ref or ref in seen:
                    continue
                seen.add(ref)
                coll, item = resolve(ref)
                if item is None:
                    continue
                if coll == "groups":
                    walk(item)
                else:
                    out.append((coll, item))
                    emit_captions(item)

        walk(ddoc.get("body", {}))
        # fallback: if body empty, emit texts/tables/pictures in array order
        if not out:
            for coll in ("texts", "tables", "pictures"):
                for item in ddoc.get(coll, []) or []:
                    out.append((coll, item))
        return out

    def _canonical_bbox(self, prov: dict, pages_meta: dict):
        """One docling ProvenanceItem (page_no + coord_origin bbox) → (page_no, canonical BBox)."""
        page_no = prov.get("page_no", 1)
        bb = prov.get("bbox")
        if not bb:
            return page_no, None
        origin = (
            NativeOrigin.BOTTOM_LEFT
            if str(bb.get("coord_origin", "BOTTOMLEFT")).upper().startswith("BOTTOM")
            else NativeOrigin.TOP_LEFT
        )
        size = (pages_meta.get(str(page_no)) or pages_meta.get(page_no) or {}).get("size", {})
        pw, ph = size.get("width", 1.0), size.get("height", 1.0)
        box = to_canonical(
            [bb["l"], bb["t"], bb["r"], bb["b"]],
            origin=origin,
            unit=NativeUnit.PDF_POINT,
            page_width=pw,
            page_height=ph,
            page=page_no,
        )
        return page_no, box

    def _prov_bbox(self, item: dict, pages_meta: dict):
        return self._canonical_bbox((item.get("prov") or [{}])[0], pages_meta)

    def _to_block(self, kind: str, item: dict, pages_meta: dict) -> tuple[int, Block | None]:
        page_no, bbox = self._prov_bbox(item, pages_meta)
        if kind == "tables":
            return page_no, self._table_block(item, bbox)
        if kind == "pictures":
            return page_no, Block(type=BlockType.IMAGE, native_type="picture", bbox=bbox)
        if kind != "texts":
            return page_no, None  # KV/form/other refs are not reading-order blocks
        label = item.get("label", "text")
        return page_no, Block(
            type=_LABEL_MAP.get(label, BlockType.TEXT),
            native_type=label,
            text=item.get("text"),
            bbox=bbox,
        )

    def _table_block(self, item: dict, bbox) -> Block:
        """Build the canonical grid through the shared occupancy cursor (derive.cells_to_grid);
        is_header honours BOTH column_header and row_header. Block.text is the plain grid
        projection so table content is in the text channel (C2) and the block spine (C11)."""
        data = item.get("data", {}) or {}
        cells: list[GridCell] = []
        for c in data.get("table_cells", []) or []:
            r0 = c.get("start_row_offset_idx", 0)
            c0 = c.get("start_col_offset_idx", 0)
            cells.append(
                GridCell(
                    row=r0,
                    col=c0,
                    row_span=max(1, c.get("end_row_offset_idx", r0 + 1) - r0),
                    col_span=max(1, c.get("end_col_offset_idx", c0 + 1) - c0),
                    text=c.get("text") or None,
                    is_header=bool(c.get("column_header") or c.get("row_header")),
                )
            )
        table = cells_to_grid(cells)
        return Block(
            type=BlockType.TABLE,
            native_type="table",
            bbox=bbox,
            text=table_to_text(table) or None,
            table=table,
        )

    def report_cost(self, job: Job) -> CostReport:
        raw = job.raw.payload if job.raw else {}
        ddoc = (raw.get("document", raw) or {}).get("json_content", {}) or {}
        pages = len(ddoc.get("pages", {})) or 1
        return infra_only("page", float(pages))
