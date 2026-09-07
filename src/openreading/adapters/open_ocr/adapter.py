"""open-ocr.com adapter — an OCR *aggregator* as a backend (P1). One POST /v1/ocr fans out to
~20 engines (openocr/tesseract, easyocr, vision LLMs …) selected via the `engine` config; the
platform meters per page. It also returns the actual amount debited (`cost_debited`) on every
response, which stays in `job.raw` and never reaches `usage`: core carries no money at all.
`mode: sync` (default)
completes inside submit() → INLINE; `mode: async` returns a request_id to poll
(GET /v1/ocr/{id}) or a webhook delivery → POLL/WEBHOOK.

Output is a plain OCR text blob (+ page count + one overall confidence) — NO element blocks,
bboxes, or per-block confidence, ever: those channels are graded X, warned when requested,
never fabricated. `markdown` is D and DELIVERED as the plain text: a structure-free backend
has no headings/tables to synthesize, so `markdown == text` is the honest projection (§4.2 —
it must be POPULATED, not silently absent); provenance marks it derived. The API's own
`output_format: markdown` variant is NOT used — it swaps the JSON envelope for a text/markdown
body and would drop the usage/latency metadata. The single overall `confidence` scalar surfaces
on `document.confidence` [0,1] (never smeared onto fabricated blocks); `applied_settings.language`
→ `document.language`.

BYO API key (Bearer sk-ocr-…; OPENOCR_API_KEY). `OPENOCR_ENGINE` picks the engine (default
openocr/tesseract — cheapest, always on). `features.ocr_languages` passes through as `lang`
(the platform maps codes per engine); `pages.max_pages` → `page_count` (1–100). The platform's
native Idempotency-Key header is fed from ctx.idempotency_key so network retries never
double-charge. Errors arrive as {"error": {"type", "message"}} — the type string is preserved
as backend_code (402 insufficient_credits is terminal; 429/5xx retryable with Retry-After).
Sources: open-ocr.com/docs/api + /docs/endpoints (accessed 2026-07-27).
"""

from __future__ import annotations

import contextlib
from typing import Any, Protocol

from openreading.adapters._http import error_for_status
from openreading.adapters.base import BackendAdapter
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
    ChannelGrade,
    JobState,
    OutputParadigm,
    ResponseState,
    WaitMode,
)
from openreading.types.errors import RetryableError, TerminalError
from openreading.types.job import Job
from openreading.types.request import OpenReadingRequest, Outputs
from openreading.types.response import (
    BackendInfo,
    BackendRaw,
    Document,
    NormalizedResponse,
    Status,
    Usage,
)
from openreading.types.runtime import Health, RawResult, RunContext

X = ChannelGrade.IMPOSSIBLE
N = ChannelGrade.NATIVE
D = ChannelGrade.DERIVABLE

_BASE_URL = "https://api.open-ocr.com"
_DEFAULT_ENGINE = "openocr/tesseract"
_TERMINAL_STATUS = {"failed", "error", "cancelled", "canceled"}


class OpenOCRClient(Protocol):
    def create_ocr(self, body: dict, idempotency_key: str | None) -> dict: ...
    def get_request(self, request_id: str) -> dict: ...


class _HttpxOpenOCRClient:  # pragma: no cover - real network path
    def __init__(self, api_key: str) -> None:
        from openreading.adapters._http import build_httpx_client

        self._http = build_httpx_client(
            base_url=_BASE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=120.0,
        )

    def _raise(self, r):
        # error envelope: {"error": {"type"|"code", "message"}} — keep the platform's own type
        # string as backend_code (e.g. insufficient_credits, engine_unavailable).
        detail, code = "", None
        with contextlib.suppress(Exception):
            err = r.json().get("error") or {}
            detail = err.get("message") or ""
            code = err.get("type") or err.get("code")
        raise error_for_status(
            r.status_code, r.headers, backend_code=code, message=detail or r.text
        )

    def create_ocr(self, body: dict, idempotency_key: str | None) -> dict:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        r = self._http.post("/v1/ocr", json=body, headers=headers)
        if r.status_code >= 400:
            self._raise(r)
        return r.json()

    def get_request(self, request_id: str) -> dict:
        r = self._http.get(f"/v1/ocr/{request_id}")
        if r.status_code >= 400:
            self._raise(r)
        return r.json()


def _descriptor() -> AdapterDescriptor:
    return AdapterDescriptor(
        id="open-ocr",
        type=BackendType.HOSTED_API,
        # Ledger T4a (AC-8): poll()/resolve_webhook() build their client fresh from ctx on every
        # call (no _active_client cached on self) — verified by R1/R2 conformance.
        protocol_version=2,
        adapter_impl="http",
        operations=["parse"],
        provisioning=Provisioning(
            byo_mode=["api_key"], auth="api_key"
        ),
        wait_modes=[WaitMode.INLINE, WaitMode.POLL, WaitMode.WEBHOOK],
        capabilities=Capabilities(
            ocr="claimed",
            handwriting="claimed",  # engine-dependent (EasyOCR / vision-LLM engines)
            vlm_based="claimed",  # vision-LLM engines selectable via OPENOCR_ENGINE
            languages=["en", "fr", "ja", "zh", "ko", "ar"],
            input_formats=["pdf", "png", "jpg", "gif", "webp", "tiff", "bmp"],
            max_pages_per_request="engine-dependent: 200 (tesseract) / 5-20 (vision LLMs)",
            max_file_size="10MB request body",
        ),
        runtime=RuntimeProfile(offline_capable=False, license="proprietary", version_pin="v1"),
        output=Output(
            paradigms=[OutputParadigm.TOKEN_STREAM],
            channels=OutputChannels(
                markdown=D,  # derivable from the text blob (tesseract precedent)
                text=N,
                blocks=X,  # a flat text blob — no element structure, ever
                block_bbox=X,
                block_confidence=X,  # one overall confidence only; lives in backend_raw
                typed_fields=X,
                table_cells=X,
            ),
        ),
        router=RouterHints(
            normalization_difficulty="low",
        ),
        credentials_spec=[
            CredentialField(
                key="api_key", required=True, env=["OPENOCR_API_KEY"], example="sk-ocr-..."
            ),
        ],
        config_spec=[
            ConfigField(
                key="engine",
                env=["OPENOCR_ENGINE"],
                example="openocr/tesseract",
                description="engine id to route to (GET /v1/engines lists ~20); "
                "default openocr/tesseract.",
            ),
        ],
        signup_url="https://open-ocr.com",
        accepts_url=True,
        live_gate_env=["OPENOCR_API_KEY"],
        # BL-164: checked open-ocr.com's full documented endpoint set directly (docs/api,
        # docs/endpoints, and the homepage) — POST /v1/ocr, POST /v1/ocr/batch, GET /v1/ocr/:id
        # (poll), GET /v1/engines(+/health/stats/latency/uptime), GET /v1/usage, GET /v1/health,
        # GET /v1/status. Only POST and GET methods appear anywhere; no DELETE, no cancel/stop/abort
        # endpoint of any kind for an in-flight async job. Honest false — a losing race branch's
        # async OCR job keeps running (and, for the async mode, may still fire its webhook) at the
        # vendor regardless of what openreading does locally.
        cancel_supported=False,
        sources=[
            Source(
                url="https://open-ocr.com/docs/api",
                accessed="2026-07-27",
                supports="auth, /v1/ocr request/response fields, error envelope + codes, "
                "Idempotency-Key, rate limits, file constraints",
            ),
            Source(
                url="https://open-ocr.com/docs/endpoints",
                accessed="2026-07-27",
                supports="sync/async/webhook flow, poll endpoint, engines list + pricing, "
                "language mapping",
            ),
            Source(
                url="https://open-ocr.com/docs/endpoints",
                accessed="2026-08-22",
                supports="BL-164: re-checked the full endpoint set — no cancel/stop/DELETE "
                "operation exists for an in-flight job",
            ),
        ],
    )


class OpenOCRAdapter(BackendAdapter):
    def __init__(self, client: OpenOCRClient | None = None) -> None:
        self.descriptor = _descriptor()
        self._client = client

    def health(self) -> Health:
        if self._client is not None:
            return Health(ready=True, detail="injected client")
        try:
            import httpx  # noqa: F401
        except ImportError:
            return Health(ready=False, missing_deps=["httpx (pip install 'openreading[open-ocr]')"])
        return Health(ready=True)

    def _get_client(self, ctx: RunContext) -> OpenOCRClient:
        if self._client is not None:
            return self._client
        creds = (ctx.credentials.values if ctx.credentials else {}) or {}
        key = creds.get("api_key")
        if not key:
            raise TerminalError(
                "open-ocr needs credentials_ref → {api_key}", backend_code="no_credentials"
            )
        return _HttpxOpenOCRClient(key)  # pragma: no cover

    def _input_arg(self, req: OpenReadingRequest) -> dict:
        d = req.document
        if d.url:
            return {"type": "url", "url": d.url}
        if d.bytes_base64:
            arg = {"type": "base64", "data_base64": d.bytes_base64}
            if d.mime_type:
                arg["mime_type"] = d.mime_type
            return arg
        raise TerminalError("open-ocr needs document url/bytes", backend_code="unsupported_input")

    def _body(self, req: OpenReadingRequest, ctx: RunContext) -> tuple[dict, bool]:
        engine = (ctx.runtime or {}).get("engine") or _DEFAULT_ENGINE
        body: dict[str, Any] = {"engine": engine, "input": self._input_arg(req)}
        langs = req.features.ocr_languages if req.features else None
        if langs:
            body["lang"] = langs
        max_pages = req.pages.max_pages if req.pages else None
        if max_pages:
            body["page_count"] = max_pages
        is_async = req.async_ is not None and req.async_.mode == "async"
        if is_async:
            body["mode"] = "async"
            if req.async_ is not None and req.async_.webhook_url:
                body["webhook_url"] = req.async_.webhook_url
        return body, is_async

    def submit(self, req: OpenReadingRequest, ctx: RunContext) -> Job:
        client = self._get_client(ctx)
        body, is_async = self._body(req, ctx)
        try:
            raw = client.create_ocr(body, ctx.idempotency_key)
        except (RetryableError, TerminalError):
            raise
        except Exception as e:  # noqa: BLE001
            raise self._map_error(e) from e

        status = str(raw.get("status", "")).lower()
        if status in _TERMINAL_STATUS:
            msg = (raw.get("error") or {}).get("message") or "open-ocr request failed"
            raise TerminalError(msg, backend_code=status)
        if status == "succeeded":  # sync mode: done inside submit
            job = self.new_job(
                WaitMode.INLINE, state=JobState.SUCCEEDED, idempotency_key=ctx.idempotency_key
            )
            job.backend_job_id = raw.get("request_id")
            job.raw = RawResult(
                payload=raw, media_type="application/json", encoding="json", object_class="ocr"
            )
            return job

        # processing → async path
        use_webhook = is_async and req.async_ is not None and req.async_.webhook_url is not None
        job = self.new_job(
            WaitMode.WEBHOOK if use_webhook else WaitMode.POLL,
            state=JobState.RUNNING,
            idempotency_key=ctx.idempotency_key,
        )
        job.backend_job_id = raw.get("request_id")
        job.webhook_token = raw.get("request_id") if use_webhook else None
        job.poll_handle = {"request_id": raw.get("request_id")}
        job.next_poll_at = 0.0
        return job

    def poll(self, job: Job, ctx: RunContext) -> Job:
        client = self._get_client(ctx)
        h = job.poll_handle or {}
        try:
            raw = client.get_request(h["request_id"])
        except Exception as e:  # noqa: BLE001
            raise self._map_error(e) from e
        status = str(raw.get("status", "")).lower()
        if status in _TERMINAL_STATUS:
            msg = (raw.get("error") or {}).get("message") or f"open-ocr request {status}"
            raise TerminalError(msg, backend_code=status)
        if status != "succeeded":  # processing
            job.next_poll_at = (job.next_poll_at or 0.0) + 2000.0
            return job
        self._finish(job, raw)
        return job

    def resolve_webhook(self, event: dict, job: Job, ctx: RunContext) -> Job:
        # BL-50: open-ocr declares no webhook_secret and has no signature mechanism at all — every
        # event this method sees carries no signature of its own. Authentication happens one level
        # up, at the dispatcher: the server appends a per-job callback token to the URL it
        # registers with the vendor and refuses an event that cannot present it (M5, see the
        # openreading.server docstring's webhook section). This method only does id-matching +
        # refetch-on-empty-body, and must stay safe on its own terms for a caller that reaches it
        # another way — hence the id guard below.
        if job.is_terminal():
            return job
        eid = event.get("request_id")
        # BL-70: the identical missing-guard defect that existed in the dispatcher (server/app.py)
        # also exists here, independently — eid is None for a create-request response that omitted
        # its id field (submit() then leaves backend_job_id == webhook_token == None), and an
        # unguarded `eid in (None, None)` is True by construction. Guards this method directly so
        # it stays safe even if reached a way that bypasses the dispatcher's own lookup.
        if eid is None or eid not in (job.webhook_token, job.backend_job_id):
            return job
        payload = event.get("data") or event
        if not payload.get("extracted_text") and job.backend_job_id:
            client = self._get_client(ctx)
            payload = client.get_request(job.backend_job_id)
        self._finish(job, payload)
        return job

    def _finish(self, job: Job, raw: dict) -> None:
        job.state = JobState.SUCCEEDED
        job.raw = RawResult(
            payload=raw, media_type="application/json", encoding="json", object_class="ocr"
        )

    # ---- normalize -----------------------------------------------------------
    def normalize(
        self, job: Job, ctx: RunContext, slim_req: OpenReadingRequest
    ) -> NormalizedResponse:
        raw: dict[str, Any] = job.raw.payload if job.raw else {}
        outputs = slim_req.outputs or Outputs()
        text = raw.get("extracted_text")
        # markdown=D delivered: structure-free backend → markdown == the plain text (§4.2).
        # Populated (never silently None), so the D channel is honestly delivered; provenance
        # records it as our projection (derived) of the native text.
        markdown = text
        resp = NormalizedResponse(
            status=Status(state=ResponseState.SUCCEEDED),
            backend=BackendInfo(
                id="open-ocr",
                type=BackendType.HOSTED_API,
                operation="parse",
                # The aggregated engine that actually ran — the ONLY surface where the
                # ~20-engine variance is visible; compare keys on backend.id + version (§9).
                version=raw.get("engine"),
                output_paradigm=[OutputParadigm.TOKEN_STREAM],
            ),
            document=Document(
                markdown=markdown if outputs.markdown else None,
                text=text if outputs.text else None,
                page_count=raw.get("pages"),
                language=self._language(raw),
                confidence=self._confidence(raw),  # overall scalar → document-level (§4.5)
            ),
        )
        if outputs.blocks:  # channel X — warn whenever requested, never fabricate
            resp.add_warning(
                "blocks_unavailable",
                "open-ocr returns a plain OCR text blob — no element blocks, bboxes, or "
                "per-block confidence (the single overall confidence is on document.confidence)",
                "blocks",
            )
        # v0.3 provenance (§3.3/§6.4): text is the backend's native plain OCR blob; markdown is
        # our structure-free projection of it (equals text, §4.2).
        resp.channel_provenance = {"text": "native", "markdown": "derived"}
        resp.usage = self._usage(raw)
        if outputs.include_backend_raw:
            resp.backend_raw = BackendRaw(
                encoding="json", media_type="application/json", object_class="ocr", payload=raw
            )
        return resp

    def _usage(self, raw: dict) -> Usage | None:
        """Pages and latency. `cost_debited` stays in `job.raw` and off the response, with every
        other backend's money."""
        if not raw:
            return None
        usage = Usage(
            pages_processed=raw.get("pages"),
            duration_ms=raw.get("provider_latency_ms"),
        )
        has_data = (usage.pages_processed, usage.duration_ms)
        return usage if any(v is not None for v in has_data) else None

    @staticmethod
    def _confidence(raw: dict) -> float | None:
        """The single overall confidence scalar → document.confidence [0,1] (§4.5). Never
        smeared onto fabricated blocks; passed through only when it's a real float."""
        c = raw.get("confidence")
        if isinstance(c, bool) or not isinstance(c, (int, float)):
            return None
        return float(c)

    @staticmethod
    def _language(raw: dict) -> list[str] | None:
        """applied_settings.language (the engine's resolved language) → Document.language."""
        applied = raw.get("applied_settings") or {}
        lang = applied.get("language")
        if isinstance(lang, str):
            return [lang] if lang else None
        if isinstance(lang, list):
            vals = [str(x) for x in lang if x]
            return vals or None
        return None

    def report_cost(self, job: Job) -> CostReport:
        """The pages OpenOCR reported.

        The response also carries `cost_debited`, a real amount OpenOCR says it took off the
        caller's balance, and this used to forward it as `cost_usd`. There is no money on a
        response any more: the figure was indistinguishable, once on
        `usage`, from the fourteen other backends' derived guesses, and the caller's own OpenOCR
        balance is the authority on it either way. `job.raw` still carries it verbatim.
        """
        raw = (job.raw.payload or {}) if job.raw else {}
        pages = raw.get("pages", 1) or 1
        return CostReport(native_unit="page", native_quantity=float(pages))

    def _map_error(self, e: Exception):
        if isinstance(e, (TerminalError, RetryableError)):
            return e
        return TerminalError(str(e), backend_code=type(e).__name__)
