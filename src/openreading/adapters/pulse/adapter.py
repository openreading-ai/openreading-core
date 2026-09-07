"""Pulse (runpulse.com) adapter — dev-first document extraction (P1). POST /extract returns
markdown + a per-class bounding_boxes dict (Title/Text/Tables/Images/Footer …). Two response
shapes the adapter must handle: (a) inline JSON, and (b) for large docs (>5MB / 70+ pages) a
`{"is_url": true, "url": <single-use, 1h>}` stub whose URL holds the real result — the adapter
resolves it before normalizing. Sync (`async: false`) → INLINE; async → POLL.

BYO API key (`x-api-key`; PULSE_API_KEY). Pulse exposes NO per-element confidence → block
confidence is channel X and never fabricated (a warning says so). Sources:
https://docs.runpulse.com/api-reference/endpoint/extract, runpulse.com/security (accessed 2026-07-21).
"""

from __future__ import annotations

from typing import Any, Protocol

from openreading.adapters._http import error_for_status
from openreading.adapters.base import BackendAdapter
from openreading.derive import (
    GridCell,
    cells_to_grid,
    md_table_to_table,
    md_to_text,
    order_by_position,
    table_to_pipe_md,
    table_to_text,
)
from openreading.types.blocks import Block, Citation, Table, TypedField
from openreading.types.cost import CostReport
from openreading.types.descriptor import (
    AdapterDescriptor,
    Capabilities,
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
    ResponseState,
    WaitMode,
)
from openreading.types.errors import RetryableError, TerminalError
from openreading.types.geometry import to_canonical
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

_BASE_URL = "https://api.runpulse.com"
# bounding_boxes is keyed by element class → normalized BlockType.
_CLASS_MAP = {
    "Title": BlockType.TITLE,
    "Text": BlockType.TEXT,
    "Tables": BlockType.TABLE,
    "Table": BlockType.TABLE,
    "Images": BlockType.FIGURE,
    "Image": BlockType.FIGURE,
    "Figures": BlockType.FIGURE,
    "Footer": BlockType.FOOTER,
    "Header": BlockType.HEADER,
    "SectionHeader": BlockType.SECTION_HEADER,
}
_TERMINAL_STATUS = {"failed", "error", "cancelled"}


def _pick(d: dict, *keys: str) -> Any:
    """First non-None value among `keys` (drift-tolerant key reading)."""
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return None


def _strip_id_prefix(s: Any) -> Any:
    """Strip pulse's markdown_with_ids "<id>-" element prefix (e.g. '0t-', '0a-', '1t-') that
    annotates cell/element content — it must never leak into a content channel (C1). Elements
    expose a clean `original_content`; table cells don't, so this cleans their `text`."""
    import re

    return re.sub(r"^\s*\d+[a-z]-", "", s) if isinstance(s, str) else s


class PulseClient(Protocol):
    def extract(self, document: dict, config: dict) -> dict: ...
    def get_result(self, url: str) -> dict: ...  # resolve an is_url response
    def get_job(self, extraction_id: str) -> dict: ...  # async poll
    def cancel_job(self, extraction_id: str) -> dict: ...


class _HttpxPulseClient:  # pragma: no cover - real network path
    def __init__(self, api_key: str) -> None:
        from openreading.adapters._http import build_httpx_client

        self._http = build_httpx_client(
            base_url=_BASE_URL, headers={"x-api-key": api_key}, timeout=120.0
        )

    def extract(self, document: dict, config: dict) -> dict:
        if "file" in document:
            # local file → MULTIPART upload of the raw bytes. Pulse /extract rejects a base64 blob
            # in JSON (REQ_001 "No file or URL provided"); it wants a binary `file` part. Extraction
            # params ride as form fields — scalars as-is, structured values JSON-encoded.
            import base64
            import json

            raw = base64.b64decode(document["file"])
            files = {"file": (document.get("filename") or "document.pdf", raw, "application/pdf")}
            data = {k: (v if isinstance(v, str) else json.dumps(v)) for k, v in config.items()}
            r = self._http.post("/extract", files=files, data=data)
        else:  # URL intake → JSON body with file_url
            r = self._http.post("/extract", json={**document, **config})
        if r.status_code >= 400:
            raise error_for_status(r.status_code, r.headers, message=r.text)
        return r.json()

    def get_result(self, url: str) -> dict:
        import httpx

        r = httpx.get(url, timeout=120.0)  # single-use presigned URL
        if r.status_code >= 400:
            raise error_for_status(r.status_code, r.headers, message=r.text)
        return r.json()

    def get_job(self, extraction_id: str) -> dict:
        r = self._http.get(f"/extract/{extraction_id}")
        if r.status_code >= 400:
            raise error_for_status(r.status_code, r.headers, message=r.text)
        return r.json()

    def cancel_job(self, extraction_id: str) -> dict:
        # BL-164: Pulse's documented job-management surface (docs.runpulse.com) is a separate
        # /job/{jobId} resource family (GET to poll, DELETE to cancel) from this client's own
        # /extract/{id} polling path above. A 404 here (wrong resource family, or a job already
        # finished) is mapped and RAISED by this method and by PulseAdapter.cancel() — neither
        # swallows it; the caller (engine.py's own `_cancel_loser_job`) is the one layer that
        # treats this whole call as best-effort.
        r = self._http.delete(f"/job/{extraction_id}")
        if r.status_code >= 400:
            raise error_for_status(r.status_code, r.headers, message=r.text)
        return r.json()


def _descriptor() -> AdapterDescriptor:
    return AdapterDescriptor(
        id="pulse",
        type=BackendType.HOSTED_API,
        # Ledger T4a (AC-8): poll()/cancel() build their client fresh from ctx on every call (no
        # _active_client cached on self) — verified by R1/R2 conformance.
        protocol_version=2,
        adapter_impl="http",
        operations=["extract"],
        provisioning=Provisioning(
            byo_mode=["api_key"], auth="api_key"
        ),
        wait_modes=[WaitMode.INLINE, WaitMode.POLL],
        capabilities=Capabilities(
            ocr="verified",
            printed_tables="verified",
            complex_tables="claimed",
            forms_key_value="claimed",
            layout="verified",
            reading_order="claimed",
            figures_charts="claimed",
            splitting="claimed",
            custom_schema_extraction="claimed",
            vlm_based="claimed",
            input_formats=["pdf", "docx", "pptx", "xlsx", "png", "jpg"],
        ),
        runtime=RuntimeProfile(offline_capable=False, license="proprietary", version_pin="api"),
        output=Output(
            paradigms=[OutputParadigm.MARKDOWN, OutputParadigm.ELEMENT_LIST],
            block_granularity="element",  # v0.3 hint (§4.3): Pulse blocks are layout elements
            channels=OutputChannels(
                markdown=N,
                text=D,
                blocks=N,
                block_bbox=N,
                block_confidence=X,  # Pulse exposes no per-element confidence
                typed_fields=D,
                table_cells=D,
            ),
        ),
        router=RouterHints(
            normalization_difficulty="medium",
        ),
        credentials_spec=[
            CredentialField(key="api_key", required=True, env=["PULSE_API_KEY"], example="pk_..."),
        ],
        signup_url="https://www.runpulse.com",
        accepts_url=True,
        live_gate_env=["PULSE_API_KEY"],
        # BL-166: checked docs.runpulse.com/api-reference/endpoint/extract, /introduction, and
        # /async-processing — no documented request-level idempotency key/header for /extract.
        # The only "idempotency" text in Pulse's docs is about webhook DELIVERY (dedupe an
        # incoming webhook via webhook-id), not submit. An invented header the API would silently
        # ignore is worse than this honest declaration.
        idempotency_supported=False,
        # BL-164: `DELETE /job/{jobId}` is real and documented ("attempts to cancel an asynchronous
        # job that is currently pending or processing") — best-effort by the vendor's own contract:
        # already-completed/failed/cancelled jobs are unchanged, a job may finish before
        # cancellation lands, and page usage is still charged regardless. True here means "a real,
        # working mechanism exists and openreading calls it," matching the vendor's own best-effort
        # framing, not a guarantee it always lands before the job finishes.
        cancel_supported=True,
        sources=[
            Source(
                url="https://docs.runpulse.com/api-reference/endpoint/extract",
                accessed="2026-07-21",
                supports="extract endpoint, dual inline/is_url response, per-class bounding_boxes",
            ),
            Source(
                url="https://docs.runpulse.com/api-reference/endpoint/webhook",
                accessed="2026-08-22",
                supports="BL-166: the only 'idempotency' mention anywhere in Pulse's docs is "
                "webhook-delivery dedup (webhook-id), not a submit-time request key",
            ),
            Source(
                url="https://docs.runpulse.com/api-reference/endpoint/delete-job",
                accessed="2026-08-22",
                supports="BL-164: DELETE /job/{jobId} cancels a pending/processing async job — "
                "best-effort, page usage still charged regardless",
            ),
        ],
    )


class PulseAdapter(BackendAdapter):
    def __init__(self, client: PulseClient | None = None) -> None:
        self.descriptor = _descriptor()
        self._client = client

    def health(self) -> Health:
        if self._client is not None:
            return Health(ready=True, detail="injected client")
        try:
            import httpx  # noqa: F401
        except ImportError:
            return Health(ready=False, missing_deps=["httpx (pip install 'openreading[pulse]')"])
        return Health(ready=True)

    def _get_client(self, ctx: RunContext) -> PulseClient:
        if self._client is not None:
            return self._client
        creds = (ctx.credentials.values if ctx.credentials else {}) or {}
        key = creds.get("api_key")
        if not key:
            raise TerminalError(
                "Pulse needs credentials_ref → {api_key}", backend_code="no_credentials"
            )
        return _HttpxPulseClient(key)  # pragma: no cover

    def _document_arg(self, req: OpenReadingRequest) -> dict:
        d = req.document
        if d.url:
            return {"file_url": d.url}
        if d.bytes_base64:
            return {"file": d.bytes_base64, "filename": d.filename or "document.pdf"}
        raise TerminalError("Pulse needs document url/bytes", backend_code="unsupported_input")

    def _resolve(self, client: PulseClient, raw: dict) -> dict:
        """Fold the dual response shape: an is_url stub → fetch the real result."""
        if raw.get("is_url") and raw.get("url"):
            return client.get_result(raw["url"])
        return raw

    def submit(self, req: OpenReadingRequest, ctx: RunContext) -> Job:
        client = self._get_client(ctx)
        is_async = req.async_ is not None and req.async_.mode == "async"
        doc = self._document_arg(req)
        config: dict[str, Any] = {"async": is_async}
        # §10 B.4, VERIFIED live 2026-07-29: forward the extraction schema per the /extract
        # contract — request param `structured_output` carrying `schema` (+ `schemaPrompt`). The
        # response (structured_output.values/confidence/citations) is parsed by _typed_fields; when
        # a requested field yields nothing, normalize() deliver-or-warns (never silently empty).
        if req.extraction_schema and req.extraction_schema.json_schema:
            so: dict[str, Any] = {"schema": req.extraction_schema.json_schema}
            if req.extraction_schema.instructions:
                so["schemaPrompt"] = req.extraction_schema.instructions
            config["structured_output"] = so
        try:
            raw = client.extract(doc, config)
        except (RetryableError, TerminalError):
            raise
        except Exception as e:  # noqa: BLE001
            raise self._map_error(e) from e

        if not is_async:
            raw = self._resolve(client, raw)
            job = self.new_job(
                WaitMode.INLINE, state=JobState.SUCCEEDED, idempotency_key=ctx.idempotency_key
            )
            job.raw = RawResult(
                payload=raw, media_type="application/json", encoding="json", object_class="extract"
            )
            return job

        job = self.new_job(
            WaitMode.POLL, state=JobState.RUNNING, idempotency_key=ctx.idempotency_key
        )
        job.backend_job_id = raw.get("extraction_id") or raw.get("job_id")
        job.poll_handle = {"extraction_id": job.backend_job_id}
        job.next_poll_at = 0.0
        return job

    def poll(self, job: Job, ctx: RunContext) -> Job:
        client = self._get_client(ctx)
        h = job.poll_handle or {}
        try:
            raw = client.get_job(h["extraction_id"])
        except Exception as e:  # noqa: BLE001
            raise self._map_error(e) from e
        status = str(raw.get("status", "")).lower()
        if status in _TERMINAL_STATUS:
            raise TerminalError(f"Pulse extraction {status}", backend_code=status or "failed")
        if status and status not in ("completed", "succeeded", "done") and not raw.get("markdown"):
            job.next_poll_at = (job.next_poll_at or 0.0) + 2000.0
            return job
        job.state = JobState.SUCCEEDED
        job.raw = RawResult(
            payload=self._resolve(client, raw),
            media_type="application/json",
            encoding="json",
            object_class="extract",
        )
        return job

    def cancel(self, job: Job, ctx: RunContext) -> Job:
        # BL-164: real vendor cancel, best-effort — any client error (404/already-finished, auth,
        # 5xx) is mapped and raised; engine.py's own `_cancel_loser_job` already treats this call
        # as best-effort and swallows a TerminalError/RetryableError.
        if job.is_terminal():
            return job
        # Ledger T4a closes BL-164 review (Medium)'s own flagged gap: cancel() now takes
        # ctx and always builds a real client via _get_client(ctx) — a fresh, client-less adapter
        # instance (e.g. one reconstructed by Ledger T3's resume path) no longer silently no-ops.
        client = self._get_client(ctx)
        extraction_id = (job.poll_handle or {}).get("extraction_id") or job.backend_job_id
        if extraction_id:
            try:
                client.cancel_job(extraction_id)
            except Exception as e:  # noqa: BLE001
                raise self._map_error(e) from e
        job.state = JobState.CANCELLED
        return job

    # ---- normalize -----------------------------------------------------------
    def normalize(
        self, job: Job, ctx: RunContext, slim_req: OpenReadingRequest
    ) -> NormalizedResponse:
        raw: dict[str, Any] = job.raw.payload if job.raw else {}
        return self._normalize(raw, slim_req)

    def _normalize(self, raw: dict[str, Any], req: OpenReadingRequest) -> NormalizedResponse:
        outputs = req.outputs or Outputs()
        # Build blocks in provider (class-grouped) order, grouped by page; the true reading order is
        # recovered per page by position (P5) — never left in class-grouped order.
        by_page: dict[int, list[Block]] = {}
        idx = 0
        for cls, entries in (raw.get("bounding_boxes") or {}).items():
            # Skip non-block keys: the id-annotated markdown, and the word-level "Words" breakdown
            # whose per-word entries duplicate the semantic classes at absurd granularity (~700
            # blocks for a 2-page doc). Semantic content lives in Text/Title/Footer/Tables/…
            if not isinstance(entries, list) or cls in ("Words", "markdown_with_ids"):
                continue
            for e in entries:
                if not isinstance(e, dict):
                    continue
                pno = self._page_of(e)
                btype = _CLASS_MAP.get(cls, BlockType.OTHER)
                # Prefer original_content: pulse's `content` is the markdown_with_ids variant, with
                # an "<id>-" element-id prefix ("0a-…") that must NOT leak into the text channel
                # (C1). original_content is the clean text (None for images). Strip the id prefix as
                # a fallback when original_content is absent.
                content = e.get("original_content")
                if content is None and btype not in (BlockType.IMAGE, BlockType.FIGURE):
                    # No original_content: fall back to `content`, stripping pulse's "<id>-" prefix
                    # when present. Images carry only a placeholder id here → left textless.
                    raw_c = e.get("content") or e.get("text")
                    eid = e.get("id")
                    if (
                        isinstance(raw_c, str)
                        and isinstance(eid, str)
                        and raw_c.startswith(f"{eid}-")
                    ):
                        content = raw_c[len(eid) + 1 :]
                    else:
                        content = raw_c
                table = self._table_from_entry(e, content) if btype is BlockType.TABLE else None
                if table is not None:
                    text, markdown = table_to_text(table), table_to_pipe_md(table)
                else:
                    text, markdown = self._project_text(content)
                blk = Block(
                    id=self._block_id(e, cls, idx),
                    type=btype,
                    native_type=cls,
                    text=text,
                    markdown=markdown,
                    table=table,
                    bbox=self._bbox(e),
                    # NEVER set confidence — Pulse exposes none (channel X, see warning below).
                )
                by_page.setdefault(pno, []).append(blk)
                idx += 1

        order = 0
        page_objs: list[Page] = []
        for pno in sorted(by_page):
            blks = by_page[pno]
            ordered = order_by_position(
                blks, [None] * len(blks)
            )  # bbox interleave (no span offsets)
            for b in ordered:
                b.reading_order = order
                order += 1
            page_objs.append(Page(page_number=pno, blocks=ordered))

        # BL-89: doc_markdown/doc_text (not markdown/text) — a second, document-level aggregate
        # assignment, deliberately distinct from the per-element `text`/`markdown` locals bound
        # above in the entries loop; reusing those names here is what made mypy's per-function
        # redefinition check disagree with pyright's per-statement narrowing.
        doc_markdown = raw.get("markdown")
        doc_text = (
            "\n\n".join(b.text for po in page_objs for b in (po.blocks or []) if b.text) or None
        )
        typed = self._typed_fields(raw)
        resp = NormalizedResponse(
            status=Status(state=ResponseState.SUCCEEDED),
            backend=BackendInfo(
                id="pulse",
                type=BackendType.HOSTED_API,
                operation="extract",
                output_paradigm=[OutputParadigm.MARKDOWN, OutputParadigm.ELEMENT_LIST],
            ),
            document=Document(
                markdown=doc_markdown if outputs.markdown else None,
                text=doc_text if outputs.text else None,
                page_count=raw.get("page_count") or (len(page_objs) or None),
                pages=page_objs,
            ),
            typed_fields=typed or None,
        )
        resp.add_warning(
            "confidence_unavailable",
            "Pulse does not expose per-element confidence scores",
            "block_confidence",
        )
        # C6 deliver-or-warn: extraction was requested but Pulse returned no field values (field
        # absent in the document, or a plan tier without structured_output) → typed_fields is
        # honestly absent + named in a warning (never silent). The parse path itself is live-verified
        # (2026-07-29); the code stays `typed_fields_unverified` for consumer stability.
        if not typed and (req.extraction_schema is not None or outputs.typed_fields):
            resp.add_warning(
                "typed_fields_unverified",
                "typed_fields requested but Pulse returned no field values for the schema "
                "(no structured_output.values in the response)",
                "typed_fields",
            )
        for w in raw.get("warnings") or []:
            resp.add_warning("backend_warning", str(w), None)

        provenance: dict[str, str] = {}
        if doc_markdown and outputs.markdown:
            provenance["markdown"] = "native"
        if doc_text and outputs.text:
            provenance["text"] = "derived"  # platform projection of the native blocks/grids
        if page_objs:
            provenance["blocks"] = "native"
        if any(b.table and b.table.cells for po in page_objs for b in (po.blocks or [])):
            provenance["table_cells"] = "derived"
        if typed:
            provenance["typed_fields"] = "native"
        resp.channel_provenance = provenance or None

        if outputs.include_backend_raw:
            resp.backend_raw = BackendRaw(
                encoding="json", media_type="application/json", object_class="extract", payload=raw
            )
        return resp

    @staticmethod
    def _page_of(e: dict) -> int:
        """Drift guard (P6/C9): the documented key is `page_number`; older captures use `page`."""
        v = e.get("page_number")
        if v is None:
            v = e.get("page")
        try:
            return int(v) if v is not None else 1
        except (TypeError, ValueError):
            return 1

    @staticmethod
    def _block_id(e: dict, cls: str, idx: int) -> str:
        ti = e.get("table_info")
        tid = ti.get("id") if isinstance(ti, dict) else None
        return e.get("id") or tid or f"{cls.lower()}{idx}"

    @staticmethod
    def _polygon_rect(bb: Any) -> tuple[float, float, float, float] | None:
        """The documented `bounding_box` is a flat array of normalized numbers (a polygon,
        `[x0,y0,x1,y1,…]`) → the enclosing (x_min, y_min, x_max, y_max)."""
        if (
            not isinstance(bb, list)
            or len(bb) < 4
            or len(bb) % 2 != 0
            or not all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in bb)
        ):
            return None
        xs = [float(bb[i]) for i in range(0, len(bb), 2)]
        ys = [float(bb[i]) for i in range(1, len(bb), 2)]
        return (min(xs), min(ys), max(xs), max(ys))

    def _bbox(self, e: dict):
        page = self._page_of(e)
        # Drift guard: the documented `bounding_box` is a normalized 0-1 polygon; older captures use
        # a `bbox` {left,top,width,height} pixel object with page_width/page_height.
        poly = e.get("bounding_box")
        if poly is not None:
            rect = self._polygon_rect(poly)
            if rect is None:
                return None
            x0, y0, x1, y1 = rect
            return to_canonical(
                [x0, y0, x1, y1],
                origin=NativeOrigin.TOP_LEFT,
                unit=NativeUnit.NORMALIZED,
                page_width=1.0,
                page_height=1.0,
                page=page,
            )
        bb = e.get("bbox")
        if not bb:
            return None
        left, top = float(bb.get("left", 0.0)), float(bb.get("top", 0.0))
        w, hgt = float(bb.get("width", 0.0)), float(bb.get("height", 0.0))
        pw = float(e.get("page_width") or bb.get("page_width") or 0.0)
        ph = float(e.get("page_height") or bb.get("page_height") or 0.0)
        if pw <= 0 or ph <= 0:
            return None
        return to_canonical(
            [left, top, left + w, top + hgt],
            origin=NativeOrigin.TOP_LEFT,
            unit=NativeUnit.PIXEL,
            page_width=pw,
            page_height=ph,
            page=page,
        )

    def _table_from_entry(self, e: dict, content: Any) -> Table | None:
        """Build the canonical grid for a Table element: the documented authoritative rep is the
        `cell_data` array (row/column metadata → cells_to_grid); else a markdown pipe table in
        `content` (md_table_to_table). (P1)"""
        cd = e.get("cell_data")
        if isinstance(cd, list) and cd:
            cells = [self._grid_cell(c) for c in cd if isinstance(c, dict)]
            if cells:
                return cells_to_grid(cells)
        if isinstance(content, str):
            return md_table_to_table(content)
        return None

    @staticmethod
    def _grid_cell(c: dict) -> GridCell:
        """One `cell_data` object → GridCell. The docs quote `cell objects with row/column
        metadata` without pinning exact keys, so common aliases are read defensively."""
        row = _pick(c, "row", "row_index")
        col = _pick(c, "column", "col", "column_index")
        ctext = _strip_id_prefix(_pick(c, "content", "text"))
        return GridCell(
            row=int(row) if row is not None else 0,
            col=int(col) if col is not None else None,
            row_span=int(_pick(c, "row_span", "rowspan") or 1),
            col_span=int(_pick(c, "col_span", "colspan") or 1),
            text=None if ctext is None else str(ctext),
            is_header=bool(_pick(c, "is_header", "header", "is_column_header")),
        )

    @staticmethod
    def _project_text(content: Any) -> tuple[Any, Any]:
        """(text, markdown) for a non-table element: a plain projection of the element's content so
        the text channel is never full of markdown syntax (C1/C2)."""
        if not isinstance(content, str):
            return content, None
        return md_to_text(content), content

    def _typed_fields(self, raw: dict) -> dict[str, TypedField]:
        """Parse Pulse's structured_output (the /extract extraction shape) or the /schema
        endpoint's schema_output. NATIVE JSON values — no str() coercion (§4.6). Also carries the
        per-field `confidence` (float) and `citations` (element-id refs resolved to page+bbox via
        bounding_boxes) the API returns alongside `values`. Empty when absent; normalize() then
        deliver-or-warns (C6). Verified live 2026-07-29 (§10 B.4)."""
        for key in ("structured_output", "schema_output"):
            blob = raw.get(key)
            if not isinstance(blob, dict):
                continue
            values = blob.get("values")
            if not (isinstance(values, dict) and values):
                continue
            conf = blob.get("confidence")
            conf = conf if isinstance(conf, dict) else {}
            cites = blob.get("citations")
            cites = cites if isinstance(cites, dict) else {}
            index = self._citation_index(raw) if cites else {}
            out: dict[str, TypedField] = {}
            for k, v in values.items():
                c = conf.get(k)
                out[k] = TypedField(
                    value=v,
                    confidence=(
                        float(c)
                        if isinstance(c, (int, float))
                        and not isinstance(c, bool)
                        and 0.0 <= c <= 1.0
                        else None
                    ),
                    citations=self._citations_for(cites.get(k), index) or None,
                )
            return out
        return {}

    def _citation_index(self, raw: dict) -> dict[str, tuple[int, list]]:
        """id → (page, polygon) over the extract response's bounding_boxes: element ids
        (`id`+`bounding_box`) AND table-cell ids (`cell_data[].id`+`location.coordinates`), so
        structured_output.citations (element-id refs like 'txt-99' / 'tbl-1-r4c2') resolve to
        real geometry."""
        index: dict[str, tuple[int, list]] = {}
        for entries in (raw.get("bounding_boxes") or {}).values():
            if not isinstance(entries, list):
                continue
            for e in entries:
                if not isinstance(e, dict):
                    continue
                page = self._page_of(e)
                eid, poly = e.get("id"), e.get("bounding_box")
                if isinstance(eid, str) and isinstance(poly, list):
                    index[eid] = (page, poly)
                for c in e.get("cell_data") or []:
                    if not isinstance(c, dict):
                        continue
                    cid = c.get("id")
                    loc = c.get("location")
                    coords = loc.get("coordinates") if isinstance(loc, dict) else None
                    if isinstance(cid, str) and isinstance(coords, list):
                        index[cid] = (page, coords)
        return index

    def _citations_for(self, ids: Any, index: dict[str, tuple[int, list]]) -> list[Citation]:
        """Resolve a pulse citation string ('txt-99, txt-2') to Citations with page+bbox. Ids that
        don't resolve are dropped — never a fabricated / empty-geometry Citation (C7)."""
        if not isinstance(ids, str):
            return []
        out: list[Citation] = []
        for token in ids.split(","):
            ref = index.get(token.strip())
            if ref is None:
                continue
            page, poly = ref
            rect = self._polygon_rect(poly)
            if rect is None:
                continue
            x0, y0, x1, y1 = rect
            out.append(
                Citation(
                    page=page,
                    bbox=to_canonical(
                        [x0, y0, x1, y1],
                        origin=NativeOrigin.TOP_LEFT,
                        unit=NativeUnit.NORMALIZED,
                        page_width=1.0,
                        page_height=1.0,
                        page=page,
                    ),
                )
            )
        return out

    def report_cost(self, job: Job) -> CostReport:
        raw = job.raw.payload if job.raw else {}
        credits = (raw or {}).get("credits_used")
        pages = (raw or {}).get("page_count", 1) or 1
        return CostReport(
            native_unit="credit",
            native_quantity=float(credits) if credits is not None else float(pages),
        )

    def _map_error(self, e: Exception):
        if isinstance(e, (TerminalError, RetryableError)):
            return e
        return TerminalError(str(e), backend_code=type(e).__name__)
