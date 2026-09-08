"""NuExtract (NuMind) adapter — schema-first structured extraction + NuMarkdown parse (P0).
The platform is job-based with NO sync path: POST creates a job (jobId), poll
GET /api/jobs/{id}/status until status=completed, then fetch the result from the
operation-specific jobs endpoint. `extract` drives structured extraction: a temporary project
carries the caller's template (POST /api/structured-extraction), the raw document bytes are
POSTed to .../{projectId}/jobs, and the completed job's template-shaped `result` dict is
flattened into typed_fields with native JSON values preserved (the response schema allows
nested values). `parse` drives content extraction (NuMarkdown): document bytes → RAG-ready
markdown (`result` string). markdown is native; `text` is a fence-safe plain projection and
`blocks`/`table_cells` are DERIVED from the NuMarkdown via the shared derive library — the blocks
are bbox-less, so they live in one synthetic Page(1) with a `page_attribution_unavailable`
warning (§4.3 container rule) and per-block geometry/confidence stay X (never fabricated).

The template in `extraction_schema.json_schema` is passed VERBATIM as the NuExtract typed
template — {"field": "verbatim-string"|"string"|"integer"|"number"|"date-time"|[enum...]|
[[multi-enum...]]}, nestable — it is NuExtract's OWN format, not JSON Schema. Temperature is
pinned to 0 (the platform's project default 0.6 degrades extraction; NuMind recommends ~0).
Token usage (inputTokens/outputTokens/totalTokens) is reported per job and forwarded verbatim.
No rate is applied to it: core carries no prices at all.

BYO API key (Bearer; NUEXTRACT_API_KEY, with the vendor SDK's NUMIND_API_KEY honored).
NUEXTRACT_BASE_URL points at an on-prem/enterprise platform deployment. NOTE: the open-weight
NuExtract 2.0 models served via vLLM/SGLang speak the OpenAI chat API — a DIFFERENT wire
protocol; they share the template format but need a separate adapter (see qwen-vl for the
pattern). Sources: github.com/numindai/nuextract-platform-sdk (OpenAPI-generated SDK; endpoint
+ response shapes), huggingface.co/numind/NuExtract-2.0-8B (accessed 2026-07-27).
"""

from __future__ import annotations

import base64
import contextlib
from typing import Any, Protocol

from openreading.adapters._http import error_for_status
from openreading.adapters.base import BackendAdapter
from openreading.derive import md_to_blocks, md_to_text
from openreading.types.blocks import TypedField
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
    Page,
    ResponseError,
    Status,
    Usage,
)
from openreading.types.runtime import Health, RawResult, RunContext

X = ChannelGrade.IMPOSSIBLE
N = ChannelGrade.NATIVE
D = ChannelGrade.DERIVABLE

_DEFAULT_BASE_URL = "https://nuextract.ai"
# Extraction wants determinism; the platform's project default (0.6) is tuned for diversity and
# degrades extraction accuracy (NuExtract model card: "temperature at or very close to 0").
_TEMPERATURE = 0.0
_TERMINAL_STATUS = {"failed", "error", "cancelled", "canceled", "timeout", "timed_out"}


class NuExtractClient(Protocol):
    def create_project(self, name: str, template: dict, instructions: str) -> dict: ...
    def create_structured_job(
        self, project_id: str, input_bytes: bytes, *, filename: str = "document.pdf"
    ) -> dict: ...
    def create_content_job(self, input_bytes: bytes, *, filename: str = "document.pdf") -> dict: ...
    def get_job_status(self, job_id: str) -> dict: ...
    def get_structured_result(self, job_id: str) -> dict: ...
    def get_content_result(self, job_id: str) -> dict: ...
    def delete_project(self, project_id: str) -> None: ...
    def cancel_job(self, job_id: str) -> dict: ...


# multipart part content-type by filename extension (the platform's documented upload is
# `-F "file=@…"`; the extension/part type is what carries the format).
_MIME_BY_EXT = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "odt": "application/vnd.oasis.opendocument.text",
    "txt": "text/plain",
}


def _part_mime(filename: str) -> str:
    return _MIME_BY_EXT.get(filename.rsplit(".", 1)[-1].lower(), "application/octet-stream")


class _HttpxNuExtractClient:  # pragma: no cover - real network path
    def __init__(self, api_key: str, base_url: str) -> None:
        from openreading.adapters._http import build_httpx_client

        self._http = build_httpx_client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=120.0,
        )

    def _check(self, r):
        if r.status_code >= 400:
            raise error_for_status(r.status_code, r.headers, message=r.text)
        return r

    def create_project(self, name: str, template: dict, instructions: str) -> dict:
        body = {"name": name, "description": "", "template": template, "instructions": instructions}
        return self._check(self._http.post("/api/structured-extraction", json=body)).json()

    def create_structured_job(
        self, project_id: str, input_bytes: bytes, *, filename: str = "document.pdf"
    ) -> dict:
        # MULTIPART file upload (`-F "file=@…"` per the platform docs). A raw octet-stream body
        # is rejected with an empty HTTP 500 (confirmed live 2026-07-28).
        return self._check(
            self._http.post(
                f"/api/structured-extraction/{project_id}/jobs",
                params={"temperature": _TEMPERATURE},
                files={"file": (filename, input_bytes, _part_mime(filename))},
            )
        ).json()

    def create_content_job(self, input_bytes: bytes, *, filename: str = "document.pdf") -> dict:
        return self._check(
            self._http.post(
                "/api/content-extraction/jobs",
                params={"temperature": _TEMPERATURE},
                files={"file": (filename, input_bytes, _part_mime(filename))},
            )
        ).json()

    def get_job_status(self, job_id: str) -> dict:
        return self._check(self._http.get(f"/api/jobs/{job_id}/status")).json()

    def cancel_job(self, job_id: str) -> dict:
        # BL-164: same generic /api/jobs/{job_id}/... namespace get_job_status above already uses
        # (not the op-specific /api/structured-extraction|content-extraction/... namespace) —
        # confirmed job_id is shared across job kinds by that adapter-internal precedent, not just
        # the vendor SDK docs' own cross-namespace inference. Idempotent per the vendor's own docs:
        # a no-op 200 on an already-completed job.
        return self._check(self._http.post(f"/api/jobs/{job_id}/cancel")).json()

    def get_structured_result(self, job_id: str) -> dict:
        return self._check(self._http.get(f"/api/structured-extraction/jobs/{job_id}")).json()

    def get_content_result(self, job_id: str) -> dict:
        return self._check(self._http.get(f"/api/content-extraction/jobs/{job_id}")).json()

    def delete_project(self, project_id: str) -> None:
        self._check(self._http.delete(f"/api/structured-extraction/{project_id}"))


def _descriptor() -> AdapterDescriptor:
    return AdapterDescriptor(
        id="nuextract",
        type=BackendType.HOSTED_API,
        # Ledger T4a (AC-8): poll()/cancel() build their client fresh from ctx on every call (no
        # _active_client cached on self) — verified by R1/R2 conformance.
        protocol_version=2,
        adapter_impl="http",
        operations=["extract", "parse"],
        provisioning=Provisioning(byo_mode=["api_key"], auth="api_key"),
        wait_modes=[WaitMode.POLL],
        capabilities=Capabilities(
            ocr="claimed",  # VLM reads rasterized document pages (no separate OCR pass)
            printed_tables="claimed",
            reading_order="claimed",
            custom_schema_extraction="claimed",
            vlm_based="claimed",
            input_formats=["pdf", "png", "jpg", "pptx", "odt", "txt"],
        ),
        runtime=RuntimeProfile(
            offline_capable=False,
            license="proprietary (open-weight NuExtract 2.0 [MIT 2B/8B] self-hostable via vLLM — "
            "different wire protocol, separate adapter)",
            version_pin="api",
        ),
        output=Output(
            # blocks are derived from the NuMarkdown (paragraph-grained typed elements), not a
            # native element list, so the backend's paradigm stays TYPED_FIELDS + MARKDOWN.
            paradigms=[OutputParadigm.TYPED_FIELDS, OutputParadigm.MARKDOWN],
            block_granularity="paragraph",  # v0.3 hint (§4.3): md_to_blocks is paragraph-grained
            channels=OutputChannels(
                markdown=N,  # NuMarkdown content extraction
                text=D,  # fence-safe plain projection of the markdown (derive.md_to_text)
                blocks=D,  # v0.5: derived from the NuMarkdown (derive.md_to_blocks), bbox-less
                block_bbox=X,  # markdown-derived blocks have no geometry — never fabricated
                block_confidence=X,
                typed_fields=N,  # the whole point
                table_cells=D,  # markdown tables → the canonical grid on Block.table
            ),
        ),
        router=RouterHints(
            normalization_difficulty="low",
        ),
        credentials_spec=[
            CredentialField(
                key="api_key",
                required=True,
                env=["NUEXTRACT_API_KEY", "NUMIND_API_KEY"],
                description="platform API key (Bearer); NUMIND_API_KEY is the vendor SDK's name",
            ),
        ],
        config_spec=[
            ConfigField(
                key="base_url",
                env=["NUEXTRACT_BASE_URL"],
                example="https://nuextract.ai",
                description="override for an on-prem/enterprise NuExtract platform deployment; "
                "default nuextract.ai.",
            ),
        ],
        signup_url="https://nuextract.ai",
        live_gate_env=["NUEXTRACT_API_KEY"],
        # BL-166: checked the platform's own OpenAPI spec (the SDK's generation source,
        # https://nuextract.ai/docs/docs.yaml, fetched 2026-08-22) end to end for both job-creation
        # endpoints (POST .../structured-extraction/{id}/jobs, POST .../content-extraction/jobs) —
        # the only documented request header is `x-organization-id`; there is no Idempotency-Key
        # header, no client-token body/query field, and no dedup/safe-retry guidance anywhere for
        # job submission. The spec's ONLY use of "idempotent" describes POST /api/jobs/{id}/cancel
        # ("Cancellation is idempotent"), a different operation (see cancel_supported). A retried
        # submit creates a second billable job. Honest false rather than forwarding
        # ctx.idempotency_key into a header the vendor would silently ignore.
        idempotency_supported=False,
        # BL-164: `POST /api/jobs/{jobId}/cancel` — real, documented ("Request cancellation of a
        # job... idempotent: calling this on an already-completed job is a no-op and returns 200"),
        # already noted in passing by BL-166's own research above. Same generic /api/jobs/{id}/...
        # namespace this adapter's own get_job_status already calls (not the op-specific
        # structured/content-extraction namespace) — confirmed by that adapter-internal precedent
        # that job_id is shared across job kinds, not just the vendor SDK docs' cross-namespace
        # inference.
        cancel_supported=True,
        sources=[
            Source(
                url="https://github.com/numindai/nuextract-platform-sdk",
                accessed="2026-07-27",
                supports="job API endpoints, StructuredExtraction/ContentExtraction response "
                "shapes, token usage fields, temp-project flow",
            ),
            Source(
                url="https://huggingface.co/numind/NuExtract-2.0-8B",
                accessed="2026-07-27",
                supports="typed-template format, temperature≈0 guidance, open-weight licensing",
            ),
            Source(
                url="https://nuextract.ai/docs/docs.yaml",
                accessed="2026-08-22",
                supports="BL-166: full OpenAPI spec (the SDK's own generation source) — no "
                "Idempotency-Key header/field on job-creation endpoints; only /jobs/{id}/cancel "
                "is documented idempotent",
            ),
            Source(
                url="https://github.com/numindai/nuextract-platform-sdk/blob/main/docs/JobsApi.md",
                accessed="2026-08-22",
                supports="BL-164: POST /api/jobs/{jobId}/cancel — idempotent, propagates to child "
                "jobs, 400 if called on a child job (cancel the parent instead)",
            ),
        ],
    )


class NuExtractAdapter(BackendAdapter):
    def __init__(self, client: NuExtractClient | None = None) -> None:
        self.descriptor = _descriptor()
        self._client = client

    def health(self) -> Health:
        if self._client is not None:
            return Health(ready=True, detail="injected client")
        try:
            import httpx  # noqa: F401
        except ImportError:
            return Health(
                ready=False, missing_deps=["httpx (pip install 'openreading[nuextract]')"]
            )
        return Health(ready=True)

    def _get_client(self, ctx: RunContext) -> NuExtractClient:
        if self._client is not None:
            return self._client
        creds = (ctx.credentials.values if ctx.credentials else {}) or {}
        key = creds.get("api_key")
        if not key:
            raise TerminalError(
                "NuExtract needs credentials_ref → {api_key}", backend_code="no_credentials"
            )
        base_url = (ctx.runtime or {}).get("base_url") or _DEFAULT_BASE_URL
        return _HttpxNuExtractClient(key, base_url)  # pragma: no cover

    def _input_bytes(self, req: OpenReadingRequest) -> bytes:
        d = req.document
        if d.bytes_base64:
            return base64.b64decode(d.bytes_base64)
        raise TerminalError(
            "NuExtract needs document bytes_base64 (the platform has no URL intake)",
            backend_code="unsupported_input",
        )

    def submit(self, req: OpenReadingRequest, ctx: RunContext) -> Job:
        client = self._get_client(ctx)
        op = req.backend.operation or ("extract" if req.extraction_schema else "parse")
        data = self._input_bytes(req)
        filename = req.document.filename or "document.pdf"
        project_id: str | None = None
        try:
            if op == "extract":
                template = req.extraction_schema.json_schema if req.extraction_schema else None
                if not template:
                    raise TerminalError(
                        "NuExtract extract needs extraction_schema.json_schema "
                        "(a NuExtract typed template)",
                        backend_code="no_template",
                    )
                instructions = (
                    req.extraction_schema.instructions if req.extraction_schema else None
                ) or ""
                project = client.create_project("openreading", template, instructions)
                project_id = project.get("id")
                created = client.create_structured_job(project_id or "", data, filename=filename)
            else:
                created = client.create_content_job(data, filename=filename)
        except (RetryableError, TerminalError):
            raise
        except Exception as e:  # noqa: BLE001
            raise self._map_error(e) from e

        job = self.new_job(
            WaitMode.POLL, state=JobState.RUNNING, idempotency_key=ctx.idempotency_key
        )
        job.backend_job_id = created.get("jobId")
        job.poll_handle = {"op": op, "job_id": job.backend_job_id, "project_id": project_id}
        job.next_poll_at = 0.0
        return job

    def poll(self, job: Job, ctx: RunContext) -> Job:
        client = self._get_client(ctx)
        h = job.poll_handle or {}
        try:
            status_raw = client.get_job_status(h["job_id"])
        except Exception as e:  # noqa: BLE001
            raise self._map_error(e) from e
        status = str(status_raw.get("status", "")).lower()
        if status in _TERMINAL_STATUS:
            self._cleanup(client, h.get("project_id"))
            detail = self._failure_detail(client, h)
            raise TerminalError(
                f"NuExtract job {status}" + (f": {detail}" if detail else ""),
                backend_code=status,
            )
        if status != "completed":  # pending / queued / running
            job.next_poll_at = (job.next_poll_at or 0.0) + 2000.0
            return job
        try:
            raw = (
                client.get_structured_result(h["job_id"])
                if h.get("op") == "extract"
                else client.get_content_result(h["job_id"])
            )
        except Exception as e:  # noqa: BLE001
            raise self._map_error(e) from e
        self._finish(job, raw)
        self._cleanup(client, h.get("project_id"))
        return job

    def cancel(self, job: Job, ctx: RunContext) -> Job:
        # BL-164: real vendor cancel, best-effort — any client error (already-finished, auth,
        # 5xx) is mapped and raised; engine.py's own `_cancel_loser_job` already treats this call
        # as best-effort and swallows a TerminalError/RetryableError. Also cleans up the temporary
        # extraction-schema project a cancelled `extract` op created (same best-effort cleanup
        # poll() runs on every other terminal path), so a cancelled race loser doesn't leak one.
        if job.is_terminal():
            return job
        # Ledger T4a closes BL-164 review (Medium)'s own flagged gap: cancel() now takes
        # ctx and always builds a real client via _get_client(ctx) — a fresh, client-less adapter
        # instance (e.g. one reconstructed by Ledger T3's resume path) no longer silently no-ops
        # (which previously skipped the cleanup below too).
        client = self._get_client(ctx)
        h = job.poll_handle or {}
        job_id = h.get("job_id") or job.backend_job_id
        if job_id:
            try:
                client.cancel_job(job_id)
            except Exception as e:  # noqa: BLE001
                raise self._map_error(e) from e
            finally:
                self._cleanup(client, h.get("project_id"))
        job.state = JobState.CANCELLED
        return job

    def _failure_detail(self, client: NuExtractClient, h: dict) -> str | None:
        """Best-effort WHY for a failed job. Confirmed live: the status body carries no error
        detail — the reason (e.g. HTTP 403 "QuotaExceeded … upgrade your plan") surfaces only on
        the op-specific result endpoint, either as an HTTP error or an `error` object."""
        try:
            raw = (
                client.get_structured_result(h["job_id"])
                if h.get("op") == "extract"
                else client.get_content_result(h["job_id"])
            )
        except Exception as e:  # noqa: BLE001 - the error IS the detail we came for
            return str(e) or None
        err = raw.get("error") if isinstance(raw, dict) else None
        if isinstance(err, dict):
            return err.get("message") or err.get("errorCode")
        return None

    def _finish(self, job: Job, raw: dict) -> None:
        job.state = JobState.SUCCEEDED
        job.raw = RawResult(
            payload=raw,
            media_type="application/json",
            encoding="json",
            object_class=(job.poll_handle or {}).get("op", "parse"),
        )

    def _cleanup(self, client: NuExtractClient, project_id: str | None) -> None:
        """Best-effort delete of the per-request temporary template project (mirrors the vendor
        SDK's own create→run→delete pattern). The result is already in hand — never fail on it."""
        if not project_id:
            return
        with contextlib.suppress(Exception):
            client.delete_project(project_id)

    # ---- normalize -----------------------------------------------------------
    def normalize(
        self, job: Job, ctx: RunContext, slim_req: OpenReadingRequest
    ) -> NormalizedResponse:
        raw: dict[str, Any] = job.raw.payload if job.raw else {}
        op = (job.raw.object_class if job.raw else None) or "parse"
        outputs = slim_req.outputs or Outputs()
        resp = (
            self._normalize_extract(raw) if op == "extract" else self._normalize_parse(raw, outputs)
        )
        if op == "extract":
            # The extract op returns typed fields only; the requested content channels are not
            # produced by this operation. Name each in a machine-readable warning so a requested
            # N/D channel is never silently empty (C6 deliver-or-warn).
            for channel, requested in (
                ("markdown", outputs.markdown),
                ("text", outputs.text),
                ("blocks", outputs.blocks),
            ):
                if requested:
                    resp.add_warning(
                        "channel_not_produced_by_operation",
                        f"the extract operation returns typed fields only — the {channel} "
                        "channel is not produced (run the parse operation for document content)",
                        channel,
                    )
        resp.usage = self._usage(raw)
        if outputs.include_backend_raw:
            resp.backend_raw = BackendRaw(
                encoding="json", media_type="application/json", object_class=op, payload=raw
            )
        return resp

    def _normalize_extract(self, raw: dict) -> NormalizedResponse:
        result = raw.get("result") or {}
        # native JSON values preserved — the template shape (nested objects/arrays) IS the value.
        typed = {k: TypedField(value=v) for k, v in result.items()}
        err = raw.get("error")
        status = (
            Status(
                state=ResponseState.PARTIAL,  # the model answered but the output failed validation
                error=ResponseError(
                    code="backend_validation",
                    message=err.get("message"),
                    backend_code=err.get("errorCode"),
                ),
            )
            if err
            else Status(state=ResponseState.SUCCEEDED)
        )
        resp = NormalizedResponse(
            status=status,
            backend=BackendInfo(
                id="nuextract",
                type=BackendType.HOSTED_API,
                operation="extract",
                output_paradigm=[OutputParadigm.TYPED_FIELDS],
            ),
            document=Document(
                page_count=self._page_count(raw),  # documentInfo.partCount
                confidence=self._doc_confidence(raw),  # outputTokenProbability ([0,1])
            ),
            typed_fields=typed or None,
        )
        if typed:
            resp.channel_provenance = {"typed_fields": "native"}
        return resp

    def _normalize_parse(self, raw: dict, outputs: Outputs) -> NormalizedResponse:
        md = raw.get("result")
        text = md_to_text(md) if (outputs.text and md) else None
        pages: list[Page] | None = None
        provenance: dict[str, str] = {}
        if md and outputs.markdown:
            provenance["markdown"] = "native"
        if text is not None:
            provenance["text"] = "derived"  # fence-safe plain projection of the native markdown
        if md and outputs.blocks:
            # §4.3 container rule: whole-document markdown stream has no page attribution, so the
            # bbox-less derived blocks live in ONE synthetic Page(1) + a machine-readable warning.
            blocks = md_to_blocks(md)
            if blocks:
                pages = [Page(page_number=1, blocks=blocks)]
                provenance["blocks"] = "derived"
                if any(b.table for b in blocks):
                    provenance["table_cells"] = "derived"  # the grid lives on Block.table
        resp = NormalizedResponse(
            status=Status(state=ResponseState.SUCCEEDED),
            backend=BackendInfo(
                id="nuextract",
                type=BackendType.HOSTED_API,
                operation="parse",
                output_paradigm=[OutputParadigm.MARKDOWN],
            ),
            document=Document(
                markdown=md if outputs.markdown else None,
                text=text,
                pages=pages,
                confidence=self._doc_confidence(raw),  # outputTokenProbability ([0,1])
            ),
        )
        if pages is not None:
            resp.add_warning(
                "page_attribution_unavailable",
                "NuMarkdown is a whole-document stream with no page attribution — the derived "
                "blocks are placed in one synthetic page (page_number=1), not a real page count",
                "blocks",
            )
        resp.channel_provenance = provenance or None
        return resp

    def _page_count(self, raw: dict) -> int | None:
        """documentInfo.partCount — the number of image parts (pages) the platform processed."""
        pc = (raw.get("documentInfo") or {}).get("partCount")
        return pc if isinstance(pc, int) and not isinstance(pc, bool) else None

    def _doc_confidence(self, raw: dict) -> float | None:
        """outputTokenProbability (geometric mean of output-token probabilities, already in [0,1])
        is NuExtract's only confidence signal → document.confidence. Never smeared onto blocks."""
        p = raw.get("outputTokenProbability")
        if isinstance(p, (int, float)) and not isinstance(p, bool) and 0.0 <= p <= 1.0:
            return float(p)
        return None

    def _usage(self, raw: dict) -> Usage | None:
        if not raw:
            return None
        parts = (raw.get("documentInfo") or {}).get("partCount")
        usage = Usage(
            pages_processed=parts,
            input_tokens=raw.get("inputTokens"),
            output_tokens=raw.get("outputTokens"),
        )
        has_data = (usage.pages_processed, usage.input_tokens, usage.output_tokens)
        return usage if any(v is not None for v in has_data) else None

    def report_cost(self, job: Job) -> CostReport:
        raw = (job.raw.payload or {}) if job.raw else {}
        tokens = raw.get("totalTokens")
        return CostReport(
            native_unit="token",
            native_quantity=float(tokens) if tokens is not None else 0.0,
        )

    def _map_error(self, e: Exception):
        if isinstance(e, (TerminalError, RetryableError)):
            return e
        return TerminalError(str(e), backend_code=type(e).__name__)
