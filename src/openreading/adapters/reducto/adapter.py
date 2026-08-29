"""Reducto adapter — the first WEBHOOK adapter (also INLINE sync + POLL fallback). Reducto's
docs push you off polling (CODE 2000 excessive-polling), so async prefers webhooks with poll as
the degrade path. Parse returns chunks[] of typed blocks (Text/Table/Title/Signature/...) with
normalized bbox; /extract returns typed_fields with per-field citations.

BYO API key (Bearer). HIPAA/ZDR are tier-gated (Growth+), so hipaa_baa=tier_gated — a require_baa
request drops Reducto unless the deployment lists it in `baa_tier_confirmed` (fail closed).
Credit-based cost (~1 credit/page, ~$0.015). resolve_webhook verifies the Svix signature and is
idempotent by job id (duplicate deliveries are no-ops).
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Protocol

from openreading.adapters._http import error_for_status
from openreading.adapters.base import BackendAdapter
from openreading.derive import (
    GridCell,
    cells_to_grid,
    html_table_to_table,
    html_to_text,
    md_table_to_table,
    md_to_text,
    table_to_pipe_md,
    table_to_text,
)
from openreading.types.blocks import Block, Chunk, Citation, TypedField
from openreading.types.cost import CostBasis, CostReport
from openreading.types.descriptor import (
    AdapterDescriptor,
    Capabilities,
    ComplianceProfile,
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

_BLOCK_MAP = {
    "Text": BlockType.TEXT,
    "Title": BlockType.TITLE,
    "Section Header": BlockType.SECTION_HEADER,
    "Header": BlockType.HEADER,
    "Footer": BlockType.FOOTER,
    "Page Number": BlockType.PAGE_NUMBER,
    "List Item": BlockType.LIST_ITEM,
    "Table": BlockType.TABLE,
    "Figure": BlockType.FIGURE,
    "Key Value": BlockType.KEY_VALUE,
    "Checkbox": BlockType.SELECTION_MARK,
    "Signature": BlockType.SIGNATURE,
    "Comment": BlockType.OTHER,
}
# Reducto rate-limit / shedding codes (integration_notes) that are safe to retry.
_RETRYABLE_CODES = {"1000", "2000"}
# Reducto block `confidence` is a string enum in the response schema, not a float.
_CONFIDENCE = {"high": 0.9, "medium": 0.6, "low": 0.3}
_HTML_TAG = re.compile(r"<[a-zA-Z/][^>]*>")


def _confidence(v: Any) -> float | None:
    if isinstance(v, bool):  # guard: bool is an int subclass
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        return _CONFIDENCE.get(v.strip().lower())
    return None


def _block_confidence(b: dict) -> float | None:
    """Prefer the numeric granular_confidence.parse_confidence the live API returns per block;
    fall back to the coarse high/medium/low string enum. (result.ocr.words — the audit's assumed
    source — is null for native-text PDFs, so granular_confidence is the available signal.)"""
    gc = b.get("granular_confidence")
    if isinstance(gc, dict):
        pc = gc.get("parse_confidence")
        if isinstance(pc, (int, float)) and not isinstance(pc, bool):
            return float(pc)
    return _confidence(b.get("confidence"))


def _jsonbbox_rows(data: Any) -> list | None:
    """Rows-of-cells from a table_output_format=jsonbbox payload. The live shape is a list of
    tables, each a list of rows, each a list of {text, bbox} cells: `[[[cell, …], …]]`."""
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    if isinstance(first, list) and first and isinstance(first[0], list):
        return first  # data = [table]; rows = data[0]
    if isinstance(first, list) and first and isinstance(first[0], dict):
        return data  # data = [row, row, …]
    return None


class ReductoClient(Protocol):
    def parse(self, document: dict, options: dict, is_async: bool) -> dict: ...
    def extract(self, document: dict, schema: dict, is_async: bool) -> dict: ...
    def get_job(self, job_id: str) -> dict: ...
    def verify_webhook(self, headers: dict, body: dict) -> bool: ...
    def cancel_job(self, job_id: str) -> dict: ...


class _HttpxReductoClient:  # pragma: no cover - real network path
    def __init__(self, api_key: str, webhook_secret: str | None = None) -> None:
        from openreading.adapters._http import build_httpx_client

        self._http = build_httpx_client(
            base_url="https://platform.reducto.ai", headers={"Authorization": f"Bearer {api_key}"}
        )
        self._webhook_secret = webhook_secret

    def _document_url(self, document: dict) -> str:
        """Resolve a document arg to a Reducto `document_url`. A local file (base64) has no
        URL, so it must be uploaded first (POST /upload) → a `reducto://…` handle; a URL or an
        existing handle passes straight through. Without this, base64 intake sent
        `document_url: null` and Reducto 422'd."""
        if document.get("url"):
            return document["url"]
        if document.get("file_id"):
            return document["file_id"]
        if document.get("base64"):
            return self._upload(document["base64"], document.get("filename") or "document.pdf")
        raise TerminalError(
            "Reducto needs document url/file_id/bytes", backend_code="unsupported_input"
        )

    def _upload(self, b64: str, filename: str) -> str:
        r = self._http.post(
            "/upload",
            files={"file": (filename, base64.b64decode(b64), "application/octet-stream")},
        )
        if r.status_code >= 400:
            raise error_for_status(r.status_code, r.headers, message=r.text)
        return r.json()["file_id"]

    def _inline_result(self, raw: dict) -> dict:
        """Large docs return `result` as an external URL (`{type:"url", url}`, ~1h TTL) instead of
        inline chunks. Fetch it so downstream normalization always sees `result.chunks`; without
        this a big document silently normalizes to an empty parse."""
        import httpx

        result = raw.get("result")
        if isinstance(result, dict) and result.get("type") == "url" and result.get("url"):
            r = httpx.get(result["url"], timeout=120.0)
            if r.status_code >= 400:
                raise error_for_status(r.status_code, r.headers, message=r.text)
            fetched = r.json()
            if isinstance(fetched, list):  # a bare chunks list
                raw["result"] = {"type": "full", "chunks": fetched}
            elif isinstance(fetched, dict):  # a full result object (possibly wrapped)
                raw["result"] = fetched.get("result", fetched)
        return raw

    def parse(self, document: dict, options: dict, is_async: bool) -> dict:
        path = "/parse_async" if is_async else "/parse"
        r = self._http.post(path, json={"document_url": self._document_url(document), **options})
        if r.status_code >= 400:
            raise error_for_status(r.status_code, r.headers, message=r.text)
        return self._inline_result(r.json())

    def extract(self, document: dict, schema: dict, is_async: bool) -> dict:
        r = self._http.post(
            "/extract", json={"document_url": self._document_url(document), "schema": schema}
        )
        if r.status_code >= 400:
            raise error_for_status(r.status_code, r.headers, message=r.text)
        return self._inline_result(r.json())

    def get_job(self, job_id: str) -> dict:
        r = self._http.get(f"/job/{job_id}")
        if r.status_code >= 400:
            raise error_for_status(r.status_code, r.headers, message=r.text)
        return self._inline_result(r.json())

    def cancel_job(self, job_id: str) -> dict:
        # BL-164: distinct from DELETE /job/{job_id} (which deletes a completed job's stored
        # artifacts, not an in-flight one) — POST /cancel/{job_id} is the real "stop running jobs"
        # operation.
        r = self._http.post(f"/cancel/{job_id}")
        if r.status_code >= 400:
            raise error_for_status(r.status_code, r.headers, message=r.text)
        return r.json()

    def verify_webhook(self, headers: dict, body: dict) -> bool:
        if not self._webhook_secret:
            return False  # BL-50: no secret configured → fail closed, not fail open
        try:
            from svix.webhooks import Webhook, WebhookVerificationError  # type: ignore
        except ImportError as e:  # pragma: no cover - environment guard
            raise TerminalError(
                "Reducto webhook verification needs svix (pip install 'openreading[reducto]')",
                backend_code="import_error",
            ) from e

        # BL-82: previously left uncaught here — svix's own WebhookVerificationError is a type
        # outside this codebase's _ADAPTER_ERRORS (server/app.py), so a bad signature surfaced as an
        # unhandled 500 instead of this codebase's own structured error, unlike every other
        # adapter-error path (e.g. poll()'s own `except Exception as e: raise self._map_error(e)`
        # two methods above).
        try:
            Webhook(self._webhook_secret).verify(body.get("_raw", b""), headers)
        except WebhookVerificationError as e:
            raise TerminalError(
                "Reducto webhook signature invalid", backend_code="bad_signature"
            ) from e
        return True


def _descriptor() -> AdapterDescriptor:
    return AdapterDescriptor(
        id="reducto",
        type=BackendType.HOSTED_API,
        # Ledger T4a (AC-8): poll()/cancel()/resolve_webhook() build their client fresh from ctx
        # on every call (no _active_client cached on self) — verified by R1/R2 conformance.
        protocol_version=2,
        adapter_impl="http",
        operations=["parse", "extract", "split", "classify", "edit", "pipeline"],
        provisioning=Provisioning(
            byo_mode=["api_key"], auth="api_key", billing_target="caller_account"
        ),
        wait_modes=[WaitMode.INLINE, WaitMode.WEBHOOK, WaitMode.POLL],
        capabilities=Capabilities(
            ocr="verified",
            handwriting="claimed",
            printed_tables="verified",
            complex_tables="verified",
            forms_key_value="verified",
            layout="verified",
            reading_order="verified",
            figures_charts="claimed",
            signatures="verified",
            custom_schema_extraction="verified",
            vlm_based="claimed",
            input_formats=["pdf", "png", "jpg", "docx", "xlsx", "pptx"],
            max_pages_per_request="unbounded (async)",
        ),
        cost=Cost(
            native_unit="credit",
            basis="billed",
            usd_per_page_equiv_low=0.015,
            usd_per_page_equiv_high=0.06,
            lossiness="credit",
        ),
        compliance=ComplianceProfile(
            hipaa_baa="tier_gated",
            soc2="verified",
            gdpr="verified",
            trains_on_customer_data="no",
            data_region_options=["us", "eu"],
            max_retention_hours=0,
            zdr_flag="zdr_tier_gated",
            runs_fully_local=False,
        ),
        runtime=RuntimeProfile(
            offline_capable=False, license="proprietary", version_pin="reducto-api"
        ),
        output=Output(
            paradigms=[OutputParadigm.ELEMENT_LIST, OutputParadigm.TYPED_FIELDS],
            block_granularity="element",  # v0.3 hint (§4.3): reducto blocks are layout elements
            channels=OutputChannels(
                markdown=N,
                text=D,
                blocks=N,
                block_bbox=N,
                block_confidence=D,
                typed_fields=N,
                table_cells=N,  # v0.5: native cells via advanced_options.table_output_format=jsonbbox
            ),
        ),
        router=RouterHints(
            normalization_difficulty="medium",
            integration_priority="P0",
            priority_reason="Accuracy ceiling for the hard residual; first webhook adapter.",
        ),
        credentials_spec=[
            CredentialField(
                key="api_key", required=True, env=["REDUCTO_API_KEY"], example="sk_..."
            ),
            CredentialField(
                key="webhook_secret",
                required=False,
                env=["REDUCTO_WEBHOOK_SECRET"],
                description="Svix signing secret for webhook signature verification.",
            ),
        ],
        signup_url="https://platform.reducto.ai",
        accepts_url=True,
        live_gate_env=["REDUCTO_API_KEY"],
        # BL-166: Reducto's own idempotency mentions are about webhook DELIVERY (retrying a missed
        # webhook callback), not submit — there is no documented request-level idempotency token
        # for parse/extract. An invented header the API ignores would be worse than this honest
        # declaration.
        idempotency_supported=False,
        # BL-164: `POST /cancel/{job_id}` is real and documented ("Stop running jobs") — a distinct
        # operation from `DELETE /job/{job_id}` (deletes a completed job's stored artifacts, not an
        # in-flight one). Same bearer auth, same job_id already held from polling — a genuine
        # working mechanism, not a no-op.
        cancel_supported=True,
        sources=[
            Source(
                url="https://docs.reducto.ai/",
                accessed="2026-07-21",
                supports="parse chunks/blocks shape, webhooks, credit pricing, tier-gated BAA",
            ),
            # BL-166's finding is deliberately uncited here. Its only write-up is in the company
            # repo, and `sources[]` ships to every caller through `GET /v1/backends`, where a
            # private path is a pointer nobody outside can follow. The reasoning behind
            # `idempotency_supported=False` is in the comment on that field instead.
            Source(
                url="https://docs.reducto.ai/api-reference/cancel-job.md",
                accessed="2026-08-22",
                supports="BL-164: POST /cancel/{job_id} stops a running job — distinct from "
                "DELETE /job/{job_id}, which only deletes a completed job's stored artifacts",
            ),
        ],
    )


class ReductoAdapter(BackendAdapter):
    def __init__(self, client: ReductoClient | None = None) -> None:
        self.descriptor = _descriptor()
        self._client = client

    def health(self) -> Health:
        if self._client is not None:
            return Health(ready=True, detail="injected client")
        try:
            import httpx  # noqa: F401
        except ImportError:
            return Health(ready=False, missing_deps=["httpx (pip install 'openreading[reducto]')"])
        return Health(ready=True)

    def _get_client(self, ctx: RunContext) -> ReductoClient:
        if self._client is not None:
            return self._client
        creds = (ctx.credentials.values if ctx.credentials else {}) or {}
        key = creds.get("api_key")
        if not key:
            raise TerminalError(
                "Reducto needs credentials_ref → {api_key}", backend_code="no_credentials"
            )
        return _HttpxReductoClient(key, creds.get("webhook_secret"))  # pragma: no cover

    def _get_webhook_client(self, ctx: RunContext) -> ReductoClient | None:
        """resolve_webhook's own, more lenient client construction (Ledger T4a). Unlike
        `_get_client` — which `submit`/`poll`/`cancel` all need a real `api_key` for — verifying an
        inbound webhook and mapping its body only needs the `webhook_secret`: a deployment that
        only ever RECEIVES webhooks on this process (submits happen elsewhere, or through a
        different credential) legitimately configures `REDUCTO_WEBHOOK_SECRET` with no
        `REDUCTO_API_KEY` at all — server/app.py's own webhook() dispatcher already enforces and
        verifies the signature against that same secret before ever calling this method, so the
        adapter-side check here is a backstop, not the primary gate. Returns None only when there
        is truly nothing to build a client from (no injected `_client`, no api_key, no
        webhook_secret) — the caller already degrades gracefully to the event's own raw payload in
        that case, exactly as it did before this method existed."""
        if self._client is not None:
            return self._client
        creds = (ctx.credentials.values if ctx.credentials else {}) or {}
        key = creds.get("api_key")
        secret = creds.get("webhook_secret")
        if not key and not secret:
            return None
        return _HttpxReductoClient(key or "", secret)  # pragma: no cover

    def _document_arg(self, req: OpenReadingRequest) -> dict:
        d = req.document
        if d.url:
            return {"url": d.url}
        if d.file_id:
            return {"file_id": d.file_id}
        if d.bytes_base64:
            return {"base64": d.bytes_base64, "filename": d.filename or "document.pdf"}
        raise TerminalError(
            "Reducto needs document url/file_id/bytes", backend_code="unsupported_input"
        )

    def submit(self, req: OpenReadingRequest, ctx: RunContext) -> Job:
        client = self._get_client(ctx)
        op = req.backend.operation or "parse"
        doc = self._document_arg(req)
        # Reducto has no async Extract endpoint (parse has /parse_async; extract is sync-only) — so
        # an async Extract request runs synchronously rather than producing a job with no job_id.
        is_async = (req.async_ is not None and req.async_.mode == "async") and op != "extract"
        use_webhook = is_async and req.async_ is not None and req.async_.webhook_url is not None
        try:
            if op == "extract":
                schema = req.extraction_schema.json_schema if req.extraction_schema else {}
                raw = client.extract(doc, schema or {}, is_async)
            else:
                # jsonbbox → native table cells WITH per-cell geometry (table_cells=N, block_bbox)
                options: dict[str, Any] = {"advanced_options": {"table_output_format": "jsonbbox"}}
                if is_async and req.async_ is not None and req.async_.webhook_url is not None:
                    # forward the caller's callback URL to the vendor — without this, a
                    # WaitMode.WEBHOOK job is never told to call back and hangs at "running"
                    # forever (BL-67).
                    options["webhook_url"] = req.async_.webhook_url
                raw = client.parse(doc, options, is_async)
        except (RetryableError, TerminalError):
            raise
        except Exception as e:  # noqa: BLE001
            raise self._map_error(e) from e

        if not is_async:  # INLINE: full result returned synchronously
            job = self.new_job(
                WaitMode.INLINE, state=JobState.SUCCEEDED, idempotency_key=ctx.idempotency_key
            )
            job.raw = RawResult(
                payload=raw, media_type="application/json", encoding="json", object_class=op
            )
            return job

        # async: prefer WEBHOOK when a callback URL is provided, else POLL
        job_id = raw.get("job_id")
        job = self.new_job(
            WaitMode.WEBHOOK if use_webhook else WaitMode.POLL,
            state=JobState.RUNNING,
            idempotency_key=ctx.idempotency_key,
        )
        job.backend_job_id = job_id
        job.webhook_token = job_id if use_webhook else None
        job.poll_handle = {"op": op, "job_id": job_id}
        job.next_poll_at = 0.0
        return job

    def poll(self, job: Job, ctx: RunContext) -> Job:
        client = self._get_client(ctx)
        h = job.poll_handle or {}
        try:
            raw = client.get_job(h["job_id"])
        except Exception as e:  # noqa: BLE001
            raise self._map_error(e) from e
        status = raw.get("status", "completed" if "result" in raw else "pending")
        if status in ("pending", "processing", "in_progress"):
            job.next_poll_at = (job.next_poll_at or 0.0) + 3000.0  # slow poll; webhook is preferred
            return job
        if status in ("failed", "error"):
            raise TerminalError("Reducto job failed", backend_code="job_failed")
        job.state = JobState.SUCCEEDED
        job.raw = RawResult(
            payload=raw, media_type="application/json", encoding="json", object_class=h["op"]
        )
        return job

    def cancel(self, job: Job, ctx: RunContext) -> Job:
        # BL-164: real vendor cancel, best-effort — any client error (already-finished, auth,
        # 5xx) is mapped and raised; engine.py's own `_cancel_loser_job` already treats this call
        # as best-effort and swallows a TerminalError/RetryableError.
        if job.is_terminal():
            return job
        # Ledger T4a closes BL-164 review (Medium)'s own flagged gap: cancel() now takes
        # ctx and always builds a real client via _get_client(ctx) — a fresh adapter instance with
        # no bound client (a resumed-from-persistence job, before this adapter has ever called
        # submit()) no longer silently marks CANCELLED with zero vendor contact.
        client = self._get_client(ctx)
        job_id = (job.poll_handle or {}).get("job_id") or job.backend_job_id
        if job_id:
            try:
                client.cancel_job(job_id)
            except Exception as e:  # noqa: BLE001
                raise self._map_error(e) from e
        job.state = JobState.CANCELLED
        return job

    def resolve_webhook(self, event: dict, job: Job, ctx: RunContext) -> Job:
        # idempotent: duplicate deliveries after terminal are no-ops
        if job.is_terminal():
            return job
        eid = event.get("job_id")
        # BL-70: eid is None for a create-task response that omitted its id field (submit() then
        # leaves backend_job_id == webhook_token == None); an unguarded `eid in (None, None)` is
        # True by construction. Added here for defense-in-depth/uniformity with chunkr's and
        # open-ocr's own resolve_webhook, in case this method is ever reached by a path that
        # doesn't already gate on signature verification.
        if eid is None or eid not in (job.webhook_token, job.backend_job_id):
            return job  # not for this job
        # Ledger T4a: _get_webhook_client(ctx) — not the shared _get_client(ctx) submit/poll/
        # cancel use — so a deployment with only REDUCTO_WEBHOOK_SECRET configured (no API key)
        # still gets its signature genuinely checked, instead of a hard TerminalError demanding
        # credentials this method doesn't need. Still closes the original gap: a client is built
        # from ctx whenever ANYTHING is configured, not left permanently unbuilt on principle.
        client = self._get_webhook_client(ctx)
        if client is not None and not client.verify_webhook(event.get("headers", {}), event):
            raise TerminalError("Reducto webhook signature invalid", backend_code="bad_signature")
        result = event.get("data") or event.get("result")
        if result is None and client is not None and job.backend_job_id:  # only a handle → fetch
            result = client.get_job(job.backend_job_id)  # pragma: no cover
        job.state = JobState.SUCCEEDED
        job.raw = RawResult(
            payload=result,
            media_type="application/json",
            encoding="json",
            object_class=(job.poll_handle or {}).get("op", "parse"),
        )
        return job

    def _map_error(self, e: Exception):
        code = getattr(e, "backend_code", None) or getattr(e, "code", None) or type(e).__name__
        if isinstance(e, (RetryableError, TerminalError)):
            return e
        if str(code) in _RETRYABLE_CODES or "429" in str(code):
            return RetryableError(str(e), backend_code=str(code))
        return TerminalError(str(e), backend_code=str(code))

    # ---- normalize -----------------------------------------------------------
    def normalize(
        self, job: Job, ctx: RunContext, slim_req: OpenReadingRequest
    ) -> NormalizedResponse:
        raw: dict[str, Any] = job.raw.payload if job.raw else {}
        op = job.raw.object_class if job.raw else "parse"
        outputs = slim_req.outputs or Outputs()
        if op == "extract":
            resp = self._normalize_extract(raw)
        else:
            resp = self._normalize_parse(raw, outputs)
        if outputs.include_backend_raw:
            resp.backend_raw = BackendRaw(
                encoding="json", media_type="application/json", object_class=op, payload=raw
            )
        return resp

    def _page_of(self, bb: dict | None) -> int:
        # original_page is the source-document page; page may be a per-split renumber (P6/C9).
        bb = bb or {}
        return int(bb.get("original_page") or bb.get("page") or 1)

    def _bbox(self, bb: dict | None):
        if not bb:
            return None
        left, top = bb.get("left", 0.0), bb.get("top", 0.0)
        w, hgt = bb.get("width", 0.0), bb.get("height", 0.0)
        return to_canonical(
            [left, top, left + w, top + hgt],
            origin=NativeOrigin.TOP_LEFT,
            unit=NativeUnit.NORMALIZED,
            page_width=1.0,
            page_height=1.0,
            page=self._page_of(bb),
        )

    def _cell_bbox(self, bb: dict | None, page: int) -> dict | None:
        """jsonbbox cell geometry ({x,y,width,height}, normalized) → canonical bbox dict."""
        if not bb:
            return None
        x, y = bb.get("x", 0.0), bb.get("y", 0.0)
        w, h = bb.get("width", 0.0), bb.get("height", 0.0)
        canon = to_canonical(
            [x, y, x + w, y + h],
            origin=NativeOrigin.TOP_LEFT,
            unit=NativeUnit.NORMALIZED,
            page_width=1.0,
            page_height=1.0,
            page=page,
        )
        return canon.model_dump(mode="json", exclude_none=True)

    def _table_from_content(self, content: Any, page: int) -> tuple[Any, bool]:
        """(Table, native?) from a Table block's content. jsonbbox JSON → native cells with
        geometry; else HTML/pipe-markdown → derived cells (no geometry). (None, False) if neither."""
        if isinstance(content, str) and content.lstrip().startswith("["):
            try:
                rows = _jsonbbox_rows(json.loads(content))
            except (ValueError, TypeError):
                rows = None
            if rows is not None:
                cells = []
                for ri, row in enumerate(rows):
                    for label_ci, cell in enumerate(row):
                        if isinstance(cell, dict):
                            cells.append(
                                GridCell(
                                    ri,
                                    label_ci,
                                    text=cell.get("text"),
                                    bbox=self._cell_bbox(cell.get("bbox"), page),
                                )
                            )
                        elif cell is not None:
                            cells.append(GridCell(ri, label_ci, text=str(cell)))
                return cells_to_grid(cells), True
        if isinstance(content, str) and _HTML_TAG.search(content):
            table = html_table_to_table(content)
            if table is not None:
                return table, False
        if isinstance(content, str):
            table = md_table_to_table(content)
            if table is not None:
                return table, False
        return None, False

    def _project_text(self, content: Any) -> tuple[Any, Any, Any]:
        """(text, markdown, html) for a non-table block: plain projection via the shared derive
        layer so document.text is never full of markup (C1/C2)."""
        if not isinstance(content, str):
            return content, content, None
        if _HTML_TAG.search(content):
            return html_to_text(content), content, content
        return md_to_text(content), content, None

    def _normalize_parse(self, raw: dict, outputs: Outputs) -> NormalizedResponse:
        result = raw.get("result", {})
        chunks_out: list[Chunk] = []
        pages_blocks: dict[int, list[Block]] = {}
        order = 0
        for ci, ch in enumerate(result.get("chunks", [])):
            block_ids: list[str] = []
            for b in ch.get("blocks", []):
                bbox = self._bbox(b.get("bbox"))
                pno = self._page_of(b.get("bbox"))
                bid = f"c{ci}b{len(block_ids)}"
                content = b.get("content")
                btype = _BLOCK_MAP.get(b.get("type"), BlockType.OTHER)
                table = None
                if btype is BlockType.TABLE:
                    table, _native = self._table_from_content(content, pno)
                if table is not None:
                    is_html = isinstance(content, str) and bool(_HTML_TAG.search(content))
                    text, markdown, html = (
                        table_to_text(table),
                        table_to_pipe_md(table),
                        (content if is_html else None),
                    )
                else:
                    text, markdown, html = self._project_text(content)
                blk = Block(
                    id=bid,
                    type=btype,
                    native_type=b.get("type"),
                    text=text,
                    markdown=markdown,
                    html=html,
                    table=table,
                    confidence=_block_confidence(b),
                    bbox=bbox,
                    reading_order=order,
                )
                pages_blocks.setdefault(pno, []).append(blk)
                block_ids.append(bid)
                order += 1
            cc = ch.get("content")
            ctext, _cmd, _chtml = self._project_text(cc)
            chunks_out.append(Chunk(id=f"chunk{ci}", text=ctext, markdown=cc, block_ids=block_ids))

        page_objs = [
            Page(page_number=pno, blocks=pages_blocks[pno]) for pno in sorted(pages_blocks)
        ]
        text = "\n\n".join(b.text for pb in page_objs for b in (pb.blocks or []) if b.text)
        markdown = "\n\n".join(
            b.markdown for pb in page_objs for b in (pb.blocks or []) if b.markdown
        )
        usage = raw.get("usage", {})
        resp = NormalizedResponse(
            status=Status(state=ResponseState.SUCCEEDED),
            backend=BackendInfo(
                id="reducto",
                type=BackendType.HOSTED_API,
                operation="parse",
                output_paradigm=[OutputParadigm.ELEMENT_LIST],
            ),
            document=Document(
                markdown=markdown if outputs.markdown else None,
                text=text if outputs.text else None,
                page_count=usage.get("num_pages"),
                pages=page_objs,
            ),
            chunks=chunks_out or None,
        )
        # v0.3 provenance (§3.3/§6.4): markdown/blocks/table_cells are native; text is the
        # platform's plain projection of the native markdown/grid.
        resp.channel_provenance = {
            "markdown": "native",
            "text": "derived",
            "blocks": "native",
            "table_cells": "native",
        }
        return resp

    def _normalize_extract(self, raw: dict) -> NormalizedResponse:
        result = raw.get("result", {})
        if not isinstance(result, dict):
            # a non-dict result is an unexpected shape, not "zero fields" — fail loudly (P0), never
            # return an empty typed_fields as if extraction succeeded with nothing.
            raise TerminalError(
                f"Reducto extract returned a non-dict result ({type(result).__name__})",
                backend_code="unexpected_result",
            )
        data = result.get("data", result)
        if not isinstance(data, dict):
            data = {}
        citations_map = result.get("citations", {})
        tf: dict[str, TypedField] = {}
        for name, value in data.items():
            if name == "citations":
                continue
            cits = []
            for c in citations_map.get(name, []) or []:
                cits.append(
                    Citation(
                        page=c.get("page"),
                        text=c.get("content"),
                        bbox=self._bbox(c.get("bbox")),
                        confidence=_confidence(c.get("confidence")),
                    )
                )
            tf[name] = TypedField(value=value, citations=cits or None)
        return NormalizedResponse(
            status=Status(state=ResponseState.SUCCEEDED),
            backend=BackendInfo(
                id="reducto",
                type=BackendType.HOSTED_API,
                operation="extract",
                output_paradigm=[OutputParadigm.TYPED_FIELDS],
            ),
            document=Document(page_count=raw.get("usage", {}).get("num_pages")),
            typed_fields=tf or None,
        )

    def report_cost(self, job: Job) -> CostReport:
        usage = (job.raw.payload or {}).get("usage", {}) if job.raw else {}
        credits = usage.get("credits", 0.0)
        pages = usage.get("num_pages", 1) or 1
        return CostReport(
            native_unit="credit",
            native_quantity=float(credits),
            cost_usd=float(credits) * 0.015 if credits else 0.015 * pages,
            basis=CostBasis.BILLED,
            billing_target="caller_account",
            breakdown=usage.get("page_billing_breakdown"),
        )
