"""Chunkr adapter — task-based RAG-grade parse + schema extraction (P0). Chunkr has NO sync path:
POST /tasks/parse | /tasks/extract creates a task (task_id), then you poll GET /tasks/{id} until
status=Succeeded (or receive a webhook). Parse returns output.chunks[].segments[] — 17 segment
types with per-segment bbox{left,top,width,height} (in the page's pixel space), confidence, and a
per-type content rendition (HTML for tables/forms, Markdown for text/lists, LaTeX for formulas, a
markdown description for pictures). Extract returns three parallel trees: results (schema-shaped),
citations, and metrics (per-leaf confidence rating) — flattened into typed_fields here.

BYO API key (Authorization header; CHUNKR_API_KEY). Also self-hostable (AGPL, Docker) with the
identical API — point CHUNKR_BASE_URL at your container. Credit-based pricing. The descriptor
estimates $0.008 to $0.03 per page-equivalent, and `report_cost` projects $0.01 per page until a
live run refines it.
Compliance: BAA is enterprise-tier (hipaa_baa=tier_gated, so a require_baa policy drops Chunkr
unless the operator confirms it via `baa_tier_confirmed`); the no-train guarantee is scoped to
Scale-tier+ so it is modelled as opt_out (drops under a no-train policy unless the operator
confirms it). Sources: https://docs.chunkr.ai/api-references/tasks/{create-parse,create-extract}-task
and .../features/parse/outputs (accessed 2026-07-21).
"""

from __future__ import annotations

import re
from typing import Any, Protocol

from openreading.adapters._http import error_for_status
from openreading.adapters.base import BackendAdapter
from openreading.derive import (
    html_table_to_table,
    html_to_text,
    md_to_text,
    table_to_pipe_md,
    table_to_text,
)
from openreading.types.blocks import Block, Chunk, Citation, Table, TypedField
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

_DEFAULT_BASE_URL = "https://api.chunkr.ai"

# Chunkr segment_type → normalized BlockType (parse/outputs.md).
_SEGMENT_MAP = {
    "Title": BlockType.TITLE,
    "SectionHeader": BlockType.SECTION_HEADER,
    "Section Header": BlockType.SECTION_HEADER,
    "PageHeader": BlockType.HEADER,
    "PageFooter": BlockType.FOOTER,
    "Page Header": BlockType.HEADER,
    "Page Footer": BlockType.FOOTER,
    "Text": BlockType.TEXT,
    "Paragraph": BlockType.TEXT,
    "ListItem": BlockType.LIST_ITEM,
    "List Item": BlockType.LIST_ITEM,
    "Caption": BlockType.CAPTION,
    "Table": BlockType.TABLE,
    "Picture": BlockType.FIGURE,
    "Figure": BlockType.FIGURE,
    "Formula": BlockType.FORMULA,
    "FormRegion": BlockType.KEY_VALUE,
    "Form Region": BlockType.KEY_VALUE,
    "Footnote": BlockType.TEXT,
    "PageNumber": BlockType.PAGE_NUMBER,
    "Page Number": BlockType.PAGE_NUMBER,
}
_TERMINAL_STATUS = {"Failed", "Cancelled"}
# metrics confidence ratings → a coarse [0,1] channel (chunkr rates High/Medium/Low, not a float).
_RATING = {"High": 0.9, "Medium": 0.6, "Low": 0.3}
_HTML_TAG = re.compile(r"<[a-zA-Z/][^>]*>")
# citation-leaf marker keys in the parallel citations tree (distinguish a leaf from a nested node).
_CITATION_KEYS = ("page_number", "bboxes", "citation_id", "page")


def _looks_html(s: str) -> bool:
    return bool(_HTML_TAG.search(s))


def _rating_to_conf(rating: Any) -> float | str | None:
    """metrics rating → confidence. Known labels map to the documented [0,1] constants; an unknown
    label is PRESERVED as its native string (TypedField.confidence permits a qualitative string)
    rather than silently quantized to None (Appendix A P2)."""
    if rating is None or rating == "":
        return None
    return _RATING.get(rating, rating)


def _citation_from(node: dict) -> Citation:
    # chunkr citation bboxes are pixel-space with no page dims carried in the citation itself, so a
    # canonical [0,1] bbox can't be honestly built here — capture page + text, never fabricate.
    return Citation(
        page=node.get("page_number") or node.get("page"),
        text=node.get("content") or node.get("text"),
    )


def _collect_citations(node: Any) -> list[Citation]:
    """Walk a field's citations subtree (parallel to the results tree, depth-N) and collect every
    leaf citation, so a NESTED leaf's citation is reachable — not only a top-level field's (P0)."""
    out: list[Citation] = []
    if isinstance(node, dict):
        if any(k in node for k in _CITATION_KEYS):
            out.append(_citation_from(node))
        else:
            for v in node.values():
                out.extend(_collect_citations(v))
    elif isinstance(node, list):
        for item in node:
            out.extend(_collect_citations(item))
    return out


class ChunkrClient(Protocol):
    def create_parse_task(self, document: dict, config: dict) -> dict: ...
    def create_extract_task(self, document: dict, schema: dict, config: dict) -> dict: ...
    def get_task(self, task_id: str) -> dict: ...
    def cancel_task(self, task_id: str) -> dict: ...


class _HttpxChunkrClient:  # pragma: no cover - real network path
    def __init__(self, api_key: str, base_url: str) -> None:
        from openreading.adapters._http import build_httpx_client

        self._http = build_httpx_client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": api_key},
            timeout=120.0,
        )

    def _create(self, path: str, body: dict) -> dict:
        r = self._http.post(path, json=body)
        if r.status_code >= 400:
            raise error_for_status(r.status_code, r.headers, message=r.text)
        return r.json()

    def create_parse_task(self, document: dict, config: dict) -> dict:
        return self._create("/api/v1/tasks/parse", {**document, **config})

    def create_extract_task(self, document: dict, schema: dict, config: dict) -> dict:
        return self._create("/api/v1/tasks/extract", {**document, "schema": schema, **config})

    def get_task(self, task_id: str) -> dict:
        r = self._http.get(f"/api/v1/tasks/{task_id}")
        if r.status_code >= 400:
            raise error_for_status(r.status_code, r.headers, message=r.text)
        return r.json()

    def cancel_task(self, task_id: str) -> dict:
        # BL-164: Chunkr's own precondition is status == "Starting" (still queued) — once a task
        # moves to "Processing" this answers 400 (Chunkr's own docs cite it as "cannot be
        # cancelled in its current state"), which the adapter's cancel() treats as an expected,
        # non-exceptional outcome rather than a real error.
        r = self._http.get(f"/api/v1/tasks/{task_id}/cancel")
        if r.status_code >= 400:
            raise error_for_status(r.status_code, r.headers, message=r.text)
        return r.json()


def _descriptor() -> AdapterDescriptor:
    return AdapterDescriptor(
        id="chunkr",
        type=BackendType.HOSTED_API,
        # Ledger T4a (AC-8): poll()/cancel()/resolve_webhook() build their client fresh from ctx
        # on every call (no _active_client cached on self) — verified by R1/R2 conformance.
        protocol_version=2,
        adapter_impl="http",
        operations=["parse", "extract"],
        provisioning=Provisioning(
            byo_mode=["api_key", "container"], auth="api_key", billing_target="caller_account"
        ),
        wait_modes=[WaitMode.POLL, WaitMode.WEBHOOK],
        capabilities=Capabilities(
            ocr="verified",
            printed_tables="verified",
            complex_tables="claimed",
            forms_key_value="verified",
            layout="verified",
            reading_order="claimed",
            figures_charts="claimed",
            custom_schema_extraction="verified",
            vlm_based="verified",
            input_formats=["pdf", "docx", "pptx", "xlsx", "png", "jpg", "tiff", "webp", "html"],
            max_pages_per_request="2000 (soft)",
        ),
        cost=Cost(
            native_unit="credit",
            basis="estimated",
            usd_per_page_equiv_low=0.008,
            usd_per_page_equiv_high=0.03,
            lossiness="credit",
        ),
        compliance=ComplianceProfile(
            hipaa_baa="tier_gated",  # BAA is an Enterprise-tier feature
            soc2="claimed",  # SOC 2 Type I & II audits in progress (cert-complete UNVERIFIED)
            gdpr=False,  # DPA offered at Enterprise; no explicit statement (UNVERIFIED → not claimed)
            trains_on_customer_data="opt_out",  # no-train guaranteed Scale-tier+ only → confirm to use
            train_opt_out_precondition="scale_tier_or_above_no_train_guarantee",
            data_region_options=["us"],
            data_retention="per-task expires_in → permanent delete; TLS + AES-256 at rest",
            runs_fully_local=False,
        ),
        runtime=RuntimeProfile(
            offline_capable=False,
            license="proprietary (AGPL-3.0 self-host available)",
            version_pin="api v1",
        ),
        output=Output(
            paradigms=[OutputParadigm.ELEMENT_LIST, OutputParadigm.TYPED_FIELDS],
            block_granularity="element",  # v0.3 hint (§4.3): chunkr segments are layout elements
            channels=OutputChannels(
                markdown=N,
                text=D,
                blocks=N,
                block_bbox=N,
                block_confidence=N,
                typed_fields=N,
                table_cells=D,  # HTML table content → canonical grid via derive.html_table_to_table
            ),
        ),
        router=RouterHints(
            normalization_difficulty="medium",
            integration_priority="P0",
            priority_reason="One adapter covers a hosted RAG-grade backend AND the offline/compliance "
            "tier (identical API self-hosted, AGPL).",
        ),
        credentials_spec=[
            CredentialField(key="api_key", required=True, env=["CHUNKR_API_KEY"], example="ch_..."),
        ],
        config_spec=[
            ConfigField(
                key="base_url",
                env=["CHUNKR_BASE_URL"],
                example="http://localhost:8000",
                description="override for a self-hosted (AGPL) Chunkr container; default api.chunkr.ai.",
            ),
        ],
        signup_url="https://chunkr.ai",
        accepts_url=True,
        live_gate_env=["CHUNKR_API_KEY"],
        # BL-166: checked docs.chunkr.ai's create-parse-task and create-extract-task references (the
        # CreateParseForm/CreateExtractForm body schemas and the only documented security scheme,
        # Authorization), plus the task-system overview/task-handling/limits pages and the llms.txt
        # index — no Idempotency-Key header, no client-token body field, and no deduplication/safe-
        # retry guidance anywhere. A retried submit creates a second billable task. Honest false
        # rather than forwarding ctx.idempotency_key into a header the vendor would silently ignore.
        idempotency_supported=False,
        # BL-164: `GET /tasks/{task_id}/cancel` is real and documented, so a losing race branch's
        # task can genuinely be stopped — but ONLY while it's still queued (status == "Starting");
        # once it moves to "Processing" the vendor answers 400 and the task runs to completion
        # regardless. True here means "a real, working mechanism exists," not "always effective" —
        # the same honesty bar idempotency_supported above applies, just for the cases it does
        # apply to. See ChunkrAdapter.cancel()'s own comment for how the 400 case is handled.
        cancel_supported=True,
        sources=[
            Source(
                url="https://docs.chunkr.ai/api-references/tasks/create-parse-task",
                accessed="2026-07-21",
                supports="task API, ParseTaskResponse segment shape, bbox/confidence, self-host",
            ),
            Source(
                url="https://docs.chunkr.ai/api-references/tasks/create-extract-task",
                accessed="2026-08-22",
                supports="BL-166: CreateParseForm/CreateExtractForm body + header schemas — no "
                "Idempotency-Key or client-token field on either task-creation endpoint",
            ),
            Source(
                url="https://docs.chunkr.ai/api-references/tasks/cancel-task",
                accessed="2026-08-22",
                supports="BL-164: GET /tasks/{task_id}/cancel — real, but requires status == "
                "'Starting'; 400 once a task has moved to 'Processing'",
            ),
        ],
    )


class ChunkrAdapter(BackendAdapter):
    def __init__(self, client: ChunkrClient | None = None) -> None:
        self.descriptor = _descriptor()
        self._client = client

    def health(self) -> Health:
        if self._client is not None:
            return Health(ready=True, detail="injected client")
        try:
            import httpx  # noqa: F401
        except ImportError:
            return Health(ready=False, missing_deps=["httpx (pip install 'openreading[chunkr]')"])
        return Health(ready=True)

    def _get_client(self, ctx: RunContext) -> ChunkrClient:
        if self._client is not None:
            return self._client
        creds = (ctx.credentials.values if ctx.credentials else {}) or {}
        key = creds.get("api_key")
        if not key:
            raise TerminalError(
                "Chunkr needs credentials_ref → {api_key}", backend_code="no_credentials"
            )
        base_url = (ctx.runtime or {}).get("base_url") or _DEFAULT_BASE_URL
        return _HttpxChunkrClient(key, base_url)  # pragma: no cover

    def _document_arg(self, req: OpenReadingRequest) -> dict:
        d = req.document
        if d.url:
            return {"file_url": d.url}
        if d.file_id:
            return {"task_id": d.file_id}  # reuse a prior parse task
        if d.bytes_base64:
            return {"file": d.bytes_base64}
        raise TerminalError(
            "Chunkr needs document url/file_id/bytes", backend_code="unsupported_input"
        )

    def submit(self, req: OpenReadingRequest, ctx: RunContext) -> Job:
        client = self._get_client(ctx)
        op = req.backend.operation or ("extract" if req.extraction_schema else "parse")
        doc = self._document_arg(req)
        use_webhook = req.async_ is not None and req.async_.webhook_url is not None
        # forward the caller's callback URL to the vendor — without this, a WaitMode.WEBHOOK job
        # is never told to call back and hangs at "running" forever (BL-67).
        config: dict[str, Any] = {}
        if req.async_ is not None and req.async_.webhook_url is not None:
            config["webhook_url"] = req.async_.webhook_url
        try:
            if op == "extract":
                schema = req.extraction_schema.json_schema if req.extraction_schema else {}
                created = client.create_extract_task(doc, schema or {}, config)
            else:
                created = client.create_parse_task(doc, config)
        except (RetryableError, TerminalError):
            raise
        except Exception as e:  # noqa: BLE001
            raise self._map_error(e) from e

        task_id = created.get("task_id")
        job = self.new_job(
            WaitMode.WEBHOOK if use_webhook else WaitMode.POLL,
            state=JobState.RUNNING,
            idempotency_key=ctx.idempotency_key,
        )
        job.backend_job_id = task_id
        job.webhook_token = task_id if use_webhook else None
        job.poll_handle = {"op": op, "task_id": task_id}
        job.next_poll_at = 0.0
        # a task can already be Succeeded on creation (small docs) — fold that in immediately.
        if created.get("status") == "Succeeded" and created.get("output"):
            self._finish(job, created)
        return job

    def poll(self, job: Job, ctx: RunContext) -> Job:
        client = self._get_client(ctx)
        h = job.poll_handle or {}
        try:
            raw = client.get_task(h["task_id"])
        except Exception as e:  # noqa: BLE001
            raise self._map_error(e) from e
        status = raw.get("status")
        if status in _TERMINAL_STATUS:
            raise TerminalError(f"Chunkr task {status}", backend_code=status.lower())
        if status != "Succeeded":  # Starting / Processing
            job.next_poll_at = (job.next_poll_at or 0.0) + 2000.0
            return job
        self._finish(job, raw)
        return job

    def cancel(self, job: Job, ctx: RunContext) -> Job:
        # BL-164: real vendor cancel, best-effort. Chunkr's own precondition is status ==
        # "Starting" (still queued) — once a task has moved to "Processing" the vendor answers 400,
        # which is an EXPECTED outcome for most real races (the loser has usually started
        # processing by the time a winner is picked), not a failure to surface. Any other error
        # (auth, 5xx, unknown task) is mapped and raised — engine.py's own `_cancel_loser_job`
        # already treats this call as best-effort and swallows a TerminalError/RetryableError.
        if job.is_terminal():
            return job
        # Ledger T4a closes BL-164 review (Medium)'s own flagged gap: cancel() now takes
        # ctx and always builds a real client via _get_client(ctx) — a fresh, client-less adapter
        # instance (e.g. one reconstructed by Ledger T3's resume path) no longer silently no-ops.
        client = self._get_client(ctx)
        task_id = (job.poll_handle or {}).get("task_id") or job.backend_job_id
        if task_id:
            try:
                client.cancel_task(task_id)
            except Exception as e:  # noqa: BLE001
                mapped = self._map_error(e)
                already_processing = (
                    isinstance(mapped, TerminalError) and mapped.backend_code == "http_400"
                )
                if not already_processing:
                    raise mapped from e
        job.state = JobState.CANCELLED
        return job

    def resolve_webhook(self, event: dict, job: Job, ctx: RunContext) -> Job:
        # BL-50: chunkr declares no webhook_secret and has no signature mechanism at all — every
        # event this method sees carries no signature of its own. Authentication happens one level
        # up, at the dispatcher: the server appends a per-job callback token to the URL it
        # registers with the vendor and refuses an event that cannot present it (M5, see the
        # openreading.server docstring's webhook section). This method only does id-matching +
        # refetch-on-empty-body, and must stay safe on its own terms for a caller that reaches it
        # another way — hence the id guard below.
        if job.is_terminal():
            return job
        eid = event.get("task_id")
        # BL-70: the identical missing-guard defect that existed in the dispatcher (server/app.py)
        # also exists here, independently — eid is None for a create-task response that omitted
        # its id field (submit() then leaves backend_job_id == webhook_token == None), and an
        # unguarded `eid in (None, None)` is True by construction. Guards this method directly so
        # it stays safe even if reached a way that bypasses the dispatcher's own lookup.
        if eid is None or eid not in (job.webhook_token, job.backend_job_id):
            return job
        payload = event.get("data") or event
        if not payload.get("output") and job.backend_job_id:
            client = self._get_client(ctx)
            payload = client.get_task(job.backend_job_id)  # pragma: no cover
        self._finish(job, payload)
        return job

    def _finish(self, job: Job, raw: dict) -> None:
        job.state = JobState.SUCCEEDED
        job.raw = RawResult(
            payload=raw,
            media_type="application/json",
            encoding="json",
            object_class=(job.poll_handle or {}).get("op", "parse"),
        )

    # ---- normalize -----------------------------------------------------------
    def normalize(
        self, job: Job, ctx: RunContext, slim_req: OpenReadingRequest
    ) -> NormalizedResponse:
        raw: dict[str, Any] = job.raw.payload if job.raw else {}
        op = job.raw.object_class if job.raw else "parse"
        outputs = slim_req.outputs or Outputs()
        resp = (
            self._normalize_extract(raw) if op == "extract" else self._normalize_parse(raw, outputs)
        )
        if outputs.include_backend_raw:
            resp.backend_raw = BackendRaw(
                encoding="json", media_type="application/json", object_class=op, payload=raw
            )
        return resp

    def _bbox(self, seg: dict):
        bb = seg.get("bbox")
        if not bb:
            return None
        left, top = float(bb.get("left", 0.0)), float(bb.get("top", 0.0))
        w, hgt = float(bb.get("width", 0.0)), float(bb.get("height", 0.0))
        pw = float(seg.get("page_width") or bb.get("page_width") or 0.0)
        ph = float(seg.get("page_height") or bb.get("page_height") or 0.0)
        if pw <= 0 or ph <= 0:
            return None  # can't normalize without page dims (never fabricate)
        return to_canonical(
            [left, top, left + w, top + hgt],
            origin=NativeOrigin.TOP_LEFT,
            unit=NativeUnit.PIXEL,
            page_width=pw,
            page_height=ph,
            page=int(seg.get("page_number", 1)),
        )

    def _table_from_segment(self, content: Any) -> Table | None:
        """Derive the canonical grid for a Table segment (table_cells=D). Chunkr renders tables as
        HTML in `content`; the shared html_table_to_table (th/td, rowspan/colspan) lifts it to the
        one canonical grid. (A native `ss_cells` array would justify N, but its documented shape is
        unconfirmed against a live payload — deferred; the always-present HTML content satisfies D.)
        """
        if isinstance(content, str) and _looks_html(content):
            return html_table_to_table(content)
        return None

    def _project_content(self, native_text: Any, content: Any) -> tuple[Any, Any, Any]:
        """(text, markdown, html) for a NON-table segment. Prefer chunkr's native per-segment OCR
        `text`; when a segment omits it, project the per-type `content` (HTML for forms, Markdown
        for text/lists) to plain via the shared derive layer, so document.text is never markup
        (C1/P0)."""
        if native_text:
            text: Any = native_text
        elif isinstance(content, str):
            text = html_to_text(content) if _looks_html(content) else md_to_text(content)
        else:
            text = None
        markdown = content if isinstance(content, str) else None
        return text, markdown, None

    def _normalize_parse(self, raw: dict, outputs: Outputs) -> NormalizedResponse:
        output = raw.get("output", {}) or {}
        pages_blocks: dict[int, list[Block]] = {}
        chunks_out: list[Chunk] = []
        order = 0
        for ci, ch in enumerate(output.get("chunks", []) or []):
            block_ids: list[str] = []
            for seg in ch.get("segments", []) or []:
                pno = int(seg.get("page_number", 1))
                bid = seg.get("segment_id") or f"c{ci}s{len(block_ids)}"
                content = seg.get("content")
                btype = _SEGMENT_MAP.get(seg.get("segment_type"), BlockType.OTHER)
                table = self._table_from_segment(content) if btype is BlockType.TABLE else None
                if table is not None:
                    text, markdown, html = (
                        table_to_text(table),  # tab-joined rows (C1/C2), never HTML
                        table_to_pipe_md(table),
                        content if isinstance(content, str) else None,  # keep raw HTML (P2)
                    )
                else:
                    text, markdown, html = self._project_content(seg.get("text"), content)
                blk = Block(
                    id=bid,
                    type=btype,
                    native_type=seg.get("segment_type"),
                    text=text,
                    markdown=markdown,
                    html=html,
                    table=table,
                    bbox=self._bbox(seg),
                    confidence=seg.get("confidence"),
                    reading_order=order,
                )
                pages_blocks.setdefault(pno, []).append(blk)
                block_ids.append(bid)
                order += 1
            chunks_out.append(
                Chunk(
                    id=ch.get("chunk_id") or f"chunk{ci}",
                    markdown=ch.get("content"),
                    block_ids=block_ids,
                )
            )

        # seed pages from output.pages so a page with no segments is KEPT (was silently dropped),
        # carrying its native dimensions; blocks attach where segments exist (P2/C9).
        provider_pages = {
            int(p["page_number"]): p
            for p in (output.get("pages") or [])
            if isinstance(p, dict) and p.get("page_number") is not None
        }
        page_objs: list[Page] = []
        for p in sorted(set(pages_blocks) | set(provider_pages)):
            dims = provider_pages.get(p) or {}
            page_objs.append(
                Page(
                    page_number=p,
                    width=float(dims["page_width"]) if dims.get("page_width") else None,
                    height=float(dims["page_height"]) if dims.get("page_height") else None,
                    dpi=float(dims["dpi"]) if dims.get("dpi") else None,
                    blocks=pages_blocks.get(p) or None,
                )
            )
        # BL-89: doc_markdown/doc_text (not markdown/text) — this is a second, document-level
        # aggregate assignment, deliberately distinct from the per-segment `text`/`markdown` locals
        # bound above in the segment loop; reusing those names here is what made mypy's
        # per-function redefinition check disagree with pyright's per-statement narrowing.
        doc_markdown = "\n\n".join(c.markdown for c in chunks_out if c.markdown) or None
        doc_text = (
            "\n\n".join(b.text for pb in page_objs for b in (pb.blocks or []) if b.text) or None
        )
        resp = NormalizedResponse(
            status=Status(state=ResponseState.SUCCEEDED),
            backend=BackendInfo(
                id="chunkr",
                type=BackendType.HOSTED_API,
                operation="parse",
                output_paradigm=[OutputParadigm.ELEMENT_LIST],
            ),
            document=Document(
                markdown=doc_markdown if outputs.markdown else None,
                text=doc_text if outputs.text else None,
                page_count=(raw.get("output_usage") or {}).get("page_count")
                or len(page_objs)
                or None,
                pages=page_objs,
            ),
            chunks=chunks_out or None,
        )
        # v0.3 provenance (§3.3/§6.4): markdown/blocks are native segment renditions; text is the
        # platform's plain projection; table_cells are derived from the segment HTML.
        resp.channel_provenance = {
            "markdown": "native",
            "text": "derived",
            "blocks": "native",
            "table_cells": "derived",
        }
        return resp

    def _normalize_extract(self, raw: dict) -> NormalizedResponse:
        output = raw.get("output", {}) or {}
        results = output.get("results", {})
        if not isinstance(results, dict):
            results = {}
        metrics = output.get("metrics", {}) or {}
        citations = output.get("citations", {}) or {}
        typed: dict[str, TypedField] = {}
        for key, value in results.items():
            m = metrics.get(key)
            rating = m.get("confidence") if isinstance(m, dict) else None
            cits = _collect_citations(citations.get(key))
            typed[key] = TypedField(
                value=value,  # native JSON, recursively preserved to depth N — no str() coercion (P0)
                confidence=_rating_to_conf(rating),
                citations=cits or None,
            )
        resp = NormalizedResponse(
            status=Status(state=ResponseState.SUCCEEDED),
            backend=BackendInfo(
                id="chunkr",
                type=BackendType.HOSTED_API,
                operation="extract",
                output_paradigm=[OutputParadigm.TYPED_FIELDS],
            ),
            document=Document(),
            typed_fields=typed or None,
        )
        resp.channel_provenance = {"typed_fields": "native"}
        return resp

    def report_cost(self, job: Job) -> CostReport:
        usage = (job.raw.payload or {}).get("output_usage", {}) if job.raw else {}
        pages = usage.get("page_count", 1) or 1
        return CostReport(
            native_unit="credit",
            native_quantity=float(pages),
            cost_usd=0.01 * float(pages),
            basis=CostBasis.ESTIMATED,
            billing_target="caller_account",
        )

    def _map_error(self, e: Exception):
        if isinstance(e, (TerminalError, RetryableError)):
            return e
        return TerminalError(str(e), backend_code=type(e).__name__)
