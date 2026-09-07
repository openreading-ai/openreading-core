"""Anthropic Claude adapter — native-PDF document understanding via the Messages API. Two modes:
(a) plain → convert the PDF to markdown/text from the generated text blocks (token-stream);
(b) extraction_schema → tool-use structured extraction (a tool whose input_schema IS the requested
schema, forced tool_choice), parsing tool_use.input → typed_fields.

BYO API key. Self-serve HIPAA BAA (Anthropic Console) + no-train by default → a verified hosted PHI
path, but PDFs are eligible inline on the Messages API only (phi_path_constraints). Native-PDF
limits: 32MB / 600 pages (100 under a <1M-context config).

Normalization (Canon v0.5): the model's markdown is the native `markdown` channel; `text` is its
plain projection (`md_to_text`, C1/C2 — never the markup verbatim); `blocks`/`table_cells` are
derived from that markdown (`md_to_blocks`) as bbox-less typed elements in one synthetic Page(1)
with a `page_attribution_unavailable` warning (§4.3 container rule) — Claude gives no geometry, so
block_bbox/block_confidence stay channel X. A `stop_reason=max_tokens` truncation maps to
ResponseState.PARTIAL (C10). page_location citations feed page_count (or `pdf_page_count` from the
PDF bytes) and, in extract mode, `TypedField.citations`. Model default claude-opus-4-8, which the
caller may override.

Images use image content blocks with their MIME type in both single requests and native batches.
For example, a PNG stays an image/png source instead of being labeled as a PDF document.
Only PDF document blocks enable citations, because image blocks do not accept that parameter.
"""

from __future__ import annotations

import base64
from pathlib import PurePath
from typing import Any, Protocol

from openreading.adapters.base import BackendAdapter
from openreading.derive import md_to_blocks, md_to_text, pdf_page_count
from openreading.router.cost import apply_cost_report
from openreading.types.blocks import Citation, TypedField
from openreading.types.cost import CostBasis, CostReport
from openreading.types.descriptor import (
    AdapterDescriptor,
    BatchIntake,
    Capabilities,
    ComplianceProfile,
    ConfigField,
    Cost,
    CredentialField,
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
    ChannelGrade,
    JobState,
    OutputParadigm,
    ResponseState,
    WaitMode,
)
from openreading.types.errors import MissingCredentialsError, RetryableError, TerminalError
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
from openreading.types.runtime import Health, RawResult, ResolvedCredentials, RunContext

X = ChannelGrade.IMPOSSIBLE
N = ChannelGrade.NATIVE
D = ChannelGrade.DERIVABLE

_DEFAULT_MODEL = "claude-opus-4-8"
# $/1M tokens (input, output) for cost derivation, from Anthropic's published pricing page
# (accessed 2026-06-24).
_MODEL_PRICE: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5": (10.0, 50.0),
}
_RETRYABLE_EXC = {
    "RateLimitError",
    "InternalServerError",
    "APIConnectionError",
    "APITimeoutError",
    "OverloadedError",
}
_MD_PROMPT = "Convert this document to clean, faithful Markdown. Preserve headings, lists, and tables. Output only the Markdown."


class AnthropicClient(Protocol):
    def create(
        self, *, model: str, max_tokens: int, messages: list, tools=None, tool_choice=None
    ) -> dict: ...

    # native Message Batches (§7 / Manifest v0.6) — optional; the offline suite injects a fake.
    def create_batch(self, requests: list[dict]) -> dict: ...  # POST /v1/messages/batches
    def get_batch(self, batch_id: str) -> dict: ...  # GET /v1/messages/batches/{id}
    def get_results(self, batch_id: str) -> list[dict]: ...  # stream the results_url JSONL


class AnthropicProbeClient(Protocol):
    """The liveness-probe port (internal/design/liveness.md §3.2), deliberately separate from
    `AnthropicClient`: it can ONLY list models, so no probe implementation can reach a billed
    `messages.create` even by accident."""

    def list_models(self) -> list[str]: ...


class _RealAnthropicProbeClient:  # pragma: no cover - real API path, proven by the live lane
    def __init__(self, api_key: str | None, timeout_s: float) -> None:
        import anthropic

        self._client = (
            anthropic.Anthropic(api_key=api_key, timeout=timeout_s)
            if api_key
            else anthropic.Anthropic(timeout=timeout_s)
        )

    def list_models(self) -> list[str]:
        # GET /v1/models — a list endpoint, not a generation call: nothing is billed.
        return [m.id for m in self._client.models.list(limit=1).data]


class _RealAnthropicClient:  # pragma: no cover - real API path
    def __init__(self, api_key: str | None) -> None:
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    def create(self, *, model, max_tokens, messages, tools=None, tool_choice=None) -> dict:
        kw: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": messages}
        if tools is not None:
            kw["tools"] = tools
        if tool_choice is not None:
            kw["tool_choice"] = tool_choice
        return self._client.messages.create(**kw).to_dict()

    def create_batch(self, requests: list[dict]) -> dict:
        return self._client.messages.batches.create(requests=requests).to_dict()  # type: ignore[arg-type]

    def get_batch(self, batch_id: str) -> dict:
        return self._client.messages.batches.retrieve(batch_id).to_dict()

    def get_results(self, batch_id: str) -> list[dict]:
        # the SDK streams the results_url JSONL; each entry has custom_id + result{type, message}.
        return [r.to_dict() for r in self._client.messages.batches.results(batch_id)]


def _descriptor() -> AdapterDescriptor:
    return AdapterDescriptor(
        id="anthropic-claude",
        type=BackendType.HOSTED_API,
        # Ledger T4a (AC-8): poll()/submit_many() build their client fresh from ctx on every call
        # (no _active_client cached on self) — verified by R1/R2 conformance.
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
            custom_schema_extraction="verified",
            vlm_based="verified",
            languages=["en", "and many"],
            input_formats=["pdf", "png", "jpg"],
            max_pages_per_request="100 (<1M ctx) / 600 (1M ctx)",
        ),
        cost=Cost(
            native_unit="token",
            basis="estimated",
            usd_per_page_equiv_low=0.01,
            usd_per_page_equiv_high=0.08,
            lossiness="page-def",
        ),
        compliance=ComplianceProfile(
            hipaa_baa="yes",
            soc2="verified",
            gdpr="verified",
            trains_on_customer_data="no",
            data_region_options=["us", "global"],
            data_retention="no training; retention per enterprise terms",
            max_retention_hours=0,
            phi_path_constraints=["messages_api_inline_pdf_only"],
            runs_fully_local=False,
        ),
        runtime=RuntimeProfile(
            offline_capable=False, license="proprietary", version_pin="messages-2023-06-01"
        ),
        output=Output(
            paradigms=[OutputParadigm.TOKEN_STREAM, OutputParadigm.TYPED_FIELDS],
            # v0.3 (§4.3): markdown-derived blocks are mixed layout elements (title/section/
            # paragraph/list/table), bbox-less.
            block_granularity="element",
            channels=OutputChannels(
                markdown=N,
                # text is DERIVED from the model's markdown (md_to_text), not natively emitted —
                # grading it N was the audit-flagged lie (§6.5). Honest floor: D.
                text=D,
                # v0.5 (§4.3): the model's markdown → typed bbox-less blocks via md_to_blocks,
                # wrapped in the container rule. No geometry is fabricated, so block_bbox stays X.
                blocks=D,
                block_bbox=X,
                block_confidence=X,
                typed_fields=D,
                # v0.5: table cells come from md_table_to_table inside md_to_blocks (grid, no bbox).
                table_cells=D,
            ),
        ),
        router=RouterHints(
            normalization_difficulty="medium",
            integration_priority="P1",
            priority_reason="Self-serve-BAA hosted PHI path for custom-schema extraction on hard docs.",
        ),
        credentials_spec=[
            CredentialField(
                key="api_key",
                required=False,
                env=["ANTHROPIC_API_KEY"],
                description="falls back to the anthropic SDK's ambient ANTHROPIC_API_KEY.",
            ),
        ],
        config_spec=[
            ConfigField(
                key="model",
                env=["ANTHROPIC_MODEL"],
                example="claude-opus-4-8",
                description="model override; also settable via request.backend.version.",
            ),
        ],
        signup_url="https://console.anthropic.com",
        accepts_url=False,
        live_gate_env=["ANTHROPIC_API_KEY"],
        # BL-166: checked the official Messages API reference (platform.claude.com/docs/en/api/
        # messages, /api/overview, /api/errors, /api/messages/batches/create) — the documented
        # request headers are exactly x-api-key/Authorization, anthropic-version, content-type
        # (plus opt-in anthropic-beta/anthropic-user-profile-id); no Idempotency-Key header exists.
        # The request body (both create-message and create-batch) has no idempotency_key field
        # either — 409 conflict_error is about resource state conflicts, not retry dedup. There is
        # no vendor-side mechanism to forward a retried submit's key into, for either the sync
        # Messages path or the native Batches path, so an invented header would be silently
        # ignored — honest false rather than a no-op field.
        idempotency_supported=False,
        # BL-164: this adapter's single-document dispatch is INLINE-only (wait_modes below) — the
        # vendor call completes synchronously inside submit(), so by the time a `race`/`pick: best`
        # node's loser-cancellation path could ever run, the job is already terminal (or was never
        # assigned one at all). There is never a live, cancellable vendor job in flight to cancel
        # for this dispatch path; honest false rather than a no-op that implies an effect. (The
        # separate native Message Batches path above is a different, non-`parallel`-reachable
        # execution mode and out of this item's scope.)
        cancel_supported=False,
        # v0.5 (Pulse): the ONE vendor-kind liveness probe. `models.list()` is a documented list
        # endpoint — no message created, no token generated, nothing billed — which is the bar a
        # probe has to clear (internal/design/liveness.md §4). CLAIMED in the same sense the batch
        # block above is: implemented against the SDK contract with an injected-client offline
        # test; the keyed live lane is what proves the wire behaviour.
        liveness=LivenessProbe(
            probe="vendor",
            method="GET /v1/models (SDK models.list — not billed)",
            timeout_s=5.0,
            notes=(
                "Leaves your network and may count against your rate limit; it is never billed. "
                "A healthy answer proves the API is reachable and your key is accepted — it does "
                "not reserve capacity or predict a large document's outcome."
            ),
        ),
        # v0.4 (Manifest v0.6): native Message Batches. CLAIMED — implemented against the documented
        # contract with an injected-client offline test; live verification is gated (no key assumed).
        batch=BatchIntake(
            native="claimed",
            max_items=100_000,
            notes="Message Batches API (POST /v1/messages/batches; poll processing_status; stream "
            "results_url JSONL). Async, most <1h, 50% cheaper; <=100k requests / 256MB per batch.",
        ),
        sources=[
            Source(
                url="https://docs.claude.com",
                accessed="2026-07-22",
                supports="native PDF limits, tool-use extraction, citations, self-serve BAA, no-train",
            ),
            Source(
                url="https://platform.claude.com/docs/en/docs/build-with-claude/batch-processing",
                accessed="2026-07-29",
                supports="Message Batches API: create/poll/results contract, custom_id + result JSONL",
            ),
            Source(
                url="https://platform.claude.com/docs/en/api/messages",
                accessed="2026-08-22",
                supports="BL-166: full Messages request-body field list — no idempotency_key field "
                "or Idempotency-Key header on this endpoint or Batches create",
            ),
        ],
    )


class AnthropicClaudeAdapter(BackendAdapter):
    def __init__(
        self,
        client: AnthropicClient | None = None,
        max_tokens: int = 8192,
        *,
        probe_client: AnthropicProbeClient | None = None,
    ) -> None:
        self.descriptor = _descriptor()
        self._client = client
        self._max_tokens = max_tokens
        # Separate injectable port for the LIVENESS probe (internal/design/liveness.md §3.2): the
        # execution client creates messages (which costs money), the probe only lists models
        # (which does not). Keeping them apart is what makes it structurally impossible for a
        # probe to reach a billed call.
        self._probe_client = probe_client

    def probe_liveness(self, ctx: RunContext, *, timeout_s: float) -> ProbeResult:
        """`GET /v1/models` via the SDK's `models.list()` — a documented, non-billing endpoint. No
        message is created, no token is generated, nothing is charged.

        This is the one `vendor`-kind probe: it leaves the caller's network and may count against
        a rate limit, which is exactly why the descriptor declares the kind and a UI can warn
        before firing it. It is also the only backend here whose free liveness call this repo could
        name from a primary source it already vendors (the `anthropic` SDK is a declared
        dependency). See internal/design/liveness.md §4 for why every other hosted backend
        honestly declares no probe rather than a guessed URL."""
        creds = (ctx.credentials.values if ctx.credentials else {}) or {}
        client = self._probe_client or _RealAnthropicProbeClient(creds.get("api_key"), timeout_s)
        try:
            models = client.list_models()
        except Exception as e:  # noqa: BLE001 - a probe reports, it never throws
            return self._probe_error(e)
        return ProbeResult.live("responding", version=models[0] if models else None)

    @staticmethod
    def _probe_error(e: Exception) -> ProbeResult:
        """Map an SDK exception onto the probe taxonomy WITHOUT importing `anthropic` at module
        scope (the dep is optional and lazily imported everywhere else here). The SDK's exception
        class names are the stable signal — `AuthenticationError`/`PermissionDeniedError` mean the
        key was found and rejected; a connection/timeout error means nothing answered; anything
        else answered in a way that does not prove health.

        The exception's own text is never echoed into an `unauthorized` detail — `check_liveness`
        substitutes the key-free `auth_rejected_hint` sentence instead, because a provider error
        body has been seen carrying the rejected key."""
        name = type(e).__name__
        # Substring, not equality, on every arm: the SDK's exact class names have moved before
        # (APIConnectionError / APITimeoutError already differ from their v1 spellings), and a
        # probe that silently degrades a real "key rejected" into a vague "error" would send the
        # operator looking in the wrong place.
        if "Authentication" in name or "PermissionDenied" in name:
            return ProbeResult.unauthorized()
        if "Connection" in name or "Timeout" in name:
            return ProbeResult.unreachable(f"no response within the probe timeout ({name})")
        return ProbeResult.error(f"probe did not prove health ({name})")

    def health(self) -> Health:
        if self._client is not None:
            return Health(ready=True, detail="injected client")
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return Health(
                ready=False,
                missing_deps=["anthropic (pip install 'openreading[anthropic-claude]')"],
            )
        return Health(ready=True)

    def _get_client(self, ctx: RunContext) -> AnthropicClient:
        if self._client is not None:
            return self._client
        creds = (ctx.credentials.values if ctx.credentials else {}) or {}
        api_key = creds.get("api_key")
        if not api_key:
            # credentials_spec declares `api_key` optional ("falls back to the anthropic SDK's
            # ambient ANTHROPIC_API_KEY") — but that ambient lookup IS just this one env var, which
            # the broker above already resolves identically, so there is never actually a second
            # path. Left unchecked, `anthropic.Anthropic()` constructs fine with no key at all, and
            # only raises a raw `TypeError` ("Could not resolve authentication method...") deep
            # inside the first `messages.create()` call — which this adapter's own error mapping
            # (`_map_error`, below) has no taxonomy slot for beyond a generic `TerminalError`, so it
            # rendered as an unnamed "Backend error" instead of the named MissingCredentialsError
            # panel every correctly-required adapter gets. Fail the same
            # honest way here, before any client call is attempted.
            signup = (
                f" Sign up / configure: {self.descriptor.signup_url}"
                if self.descriptor.signup_url
                else ""
            )
            raise MissingCredentialsError(
                f"missing required credentials/config: ANTHROPIC_API_KEY.{signup}",
                missing=["ANTHROPIC_API_KEY"],
            )
        return _RealAnthropicClient(api_key)  # pragma: no cover

    def _pdf_b64(self, req: OpenReadingRequest) -> str:
        d = req.document
        if d.bytes_base64:
            return d.bytes_base64
        if d.path:
            with open(d.path, "rb") as fh:
                return base64.b64encode(fh.read()).decode()
        raise TerminalError(
            "Claude needs document bytes/path (PDF)", backend_code="unsupported_input"
        )

    def _build_params(self, req: OpenReadingRequest, ctx: RunContext) -> tuple[dict[str, Any], str]:
        """Build the Messages `create` params (model, max_tokens, messages, tools, tool_choice) for a
        request → (params, mode). Shared by the single submit() and the native batch submit_many()
        so both build byte-identical requests."""
        model = req.backend.version or (ctx.runtime or {}).get("model") or _DEFAULT_MODEL
        mode = "extract" if req.extraction_schema else "parse"
        document = req.document
        suffix = PurePath(document.filename or document.path or "").suffix.lower()
        media_type = document.mime_type or {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
        }.get(suffix, "application/pdf")
        is_image = media_type.startswith("image/")
        cite = mode == "parse" and not is_image

        doc_block: dict[str, Any] = {
            "type": "image" if is_image else "document",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": self._pdf_b64(req),
            },
        }
        if cite:
            doc_block["citations"] = {"enabled": True}

        params: dict[str, Any] = {"model": model, "max_tokens": self._max_tokens}
        if mode == "extract":
            schema = (req.extraction_schema.json_schema if req.extraction_schema else None) or {
                "type": "object",
                "properties": {},
                "additionalProperties": True,
            }
            instructions = (
                req.extraction_schema.instructions if req.extraction_schema else None
            ) or ""
            prompt = f"{instructions} Extract the requested fields from the document.".strip()
            params["tools"] = [
                {
                    "name": "extract_fields",
                    "description": "Return the document's extracted fields.",
                    "input_schema": schema,
                }
            ]
            params["tool_choice"] = {"type": "tool", "name": "extract_fields"}
        else:
            prompt = _MD_PROMPT

        params["messages"] = [
            {"role": "user", "content": [doc_block, {"type": "text", "text": prompt}]}
        ]
        return params, mode

    def submit(self, req: OpenReadingRequest, ctx: RunContext) -> Job:
        client = self._get_client(ctx)
        params, mode = self._build_params(req, ctx)
        try:
            raw = client.create(
                model=params["model"],
                max_tokens=params["max_tokens"],
                messages=params["messages"],
                tools=params.get("tools"),
                tool_choice=params.get("tool_choice"),
            )
        except (RetryableError, TerminalError):
            raise
        except Exception as e:  # noqa: BLE001
            raise self._map_error(e) from e

        # Ledger T4b: `normalize()` only ever sees `slim_req`, whose
        # `document.bytes_base64`/`.password`/`.url` are nulled by `slim_request`
        # (the openreading.adapters runbook: `normalize` reads `job.raw.payload` ONLY, never a field the caller
        # might have supplied out-of-band via `slim_req`/`req`) — so the exact, byte-derived page
        # count has to be computed HERE, while `req` still carries the real PDF bytes/path, and
        # carried forward on `job.raw.payload` for `_page_count` to read back out. `None` (no
        # parseable PDF bytes) is a legitimate value — `_page_count` falls back to the citation
        # heuristic in that case, same as before this fix.
        pdf_bytes = self._pdf_bytes(req)
        payload = dict(raw)
        payload["_pdf_page_count"] = pdf_page_count(pdf_bytes) if pdf_bytes is not None else None

        job = self.new_job(
            WaitMode.INLINE, state=JobState.SUCCEEDED, idempotency_key=ctx.idempotency_key
        )
        job.raw = RawResult(
            payload=payload, media_type="application/json", encoding="json", object_class=mode
        )
        return job

    # ---- native Message Batches (§7) — live-lane-gated; offline test injects a fake client -------
    def submit_many(self, reqs: list[OpenReadingRequest], ctx: RunContext) -> Job:
        """Create one Anthropic Message Batch from N requests (custom_id = item-<i>). Returns a POLL
        Job carrying the batch id; poll() advances it, normalize_many() maps the JSONL results."""
        client = self._get_client(ctx)
        batch_reqs = []
        for i, req in enumerate(reqs):
            params, _mode = self._build_params(req, ctx)
            batch_reqs.append({"custom_id": f"item-{i}", "params": params})
        try:
            created = client.create_batch(batch_reqs)
        except (RetryableError, TerminalError):
            raise
        except Exception as e:  # noqa: BLE001
            raise self._map_error(e) from e
        job = self.new_job(
            WaitMode.POLL, state=JobState.RUNNING, idempotency_key=ctx.idempotency_key
        )
        job.backend_job_id = created.get("id")
        job.poll_handle = {"batch_id": created.get("id")}
        job.next_poll_at = 0.0
        return job

    def poll(self, job: Job, ctx: RunContext) -> Job:
        """Advance a native-batch job: GET the batch; `in_progress` → wait; `ended` → fetch the
        results JSONL into job.raw. Single (INLINE) jobs never reach here."""
        h = job.poll_handle or {}
        if "batch_id" not in h:  # not a batch job → base no-op
            return job
        client = self._get_client(ctx)
        try:
            status = client.get_batch(h["batch_id"])
        except Exception as e:  # noqa: BLE001
            raise self._map_error(e) from e
        if str(status.get("processing_status")) != "ended":
            job.next_poll_at = (job.next_poll_at or 0.0) + 5000.0
            return job
        try:
            results = client.get_results(h["batch_id"])
        except Exception as e:  # noqa: BLE001
            raise self._map_error(e) from e
        job.state = JobState.SUCCEEDED
        job.raw = RawResult(
            payload={"results": results},
            media_type="application/json",
            encoding="json",
            object_class="batch",
        )
        return job

    def normalize_many(
        self,
        job: Job,
        reqs: list[OpenReadingRequest],
        credentials: ResolvedCredentials | None = None,
    ):
        """Map the batch JSONL results (keyed by custom_id) back to per-request results, reusing the
        single-document normalize() for each succeeded message and a BatchItemError for the rest.

        Cost (BL-100) is metered per item, right here, not by the caller: the batch-level `job`
        this method receives has no top-level `usage` key (its `raw.payload` is `{"results": [...]}`
        ), so a single report_cost(job) call over the whole batch would silently price every item at
        cost_usd=0.0 — worse than leaving usage absent. Each succeeded item's own synthetic `synth`
        job, built below from that item's own result message, carries the real per-item `usage`
        block report_cost() needs, so apply_cost_report runs against THAT — mirroring
        run_request's `apply_cost_report(adapter, job, adapter.normalize(job, ctx, slim_req))`
        right after `normalize()` (router/cost.py's own invariant), just reached from inside the
        adapter instead of the router, since only the adapter can rebuild each item's raw usage
        from the batch payload.

        `credentials` (BL-108): forwarded into that same apply_cost_report call so a report_cost()
        failure's warning is redacted here exactly like every other apply_cost_report call site —
        the caller (`_run_native`) already has `ctx.credentials` in scope and passes it through.

        BL-113: the succeeded branch's own mapping step below — `self.normalize(synth, ctx,
        slim_req)` (our code re-normalizing a vendor-confirmed-good result) plus the
        `apply_cost_report` call right after it — gets its own try/except, mirroring the
        vendor-reported-failure branch four lines below and `batch/runner.py`'s `_run_item`, which
        does the identical thing one layer up for the platform path. Before this, a bug in that
        mapping step on item k unwound straight past every `out.append()` for items `0..k-1` the
        instant it raised, discarding their already-succeeded results too — not just the offending
        item's — because `out` is a local list with no outer recovery once an exception has unwound
        past it. The caught exception is explicitly redacted before it lands in
        `BatchItemError.message`: `auth_hinted`'s own redaction only fires on an exception that
        escapes its `with` block, and a crash caught right here, inside `normalize_many` itself,
        never does.

        `ctx` (Ledger T4b): `normalize_many` isn't one of the required 8 methods and keeps
        its `credentials`-only `NativeBatchAdapter` Protocol signature, so a minimal
        `RunContext(credentials=credentials)` is built once, locally,
        purely to satisfy `normalize`'s new `(job, ctx, slim_req)` shape — no implementation reads
        `ctx.credentials` today. `slim_request` is imported locally (not at module level) to avoid
        the same `adapters.* -> ledger.header -> adapters.registry -> adapters.*` import cycle the
        `credentials`/`batch` imports two lines below already dodge the same way."""
        from openreading.credentials import redact, secret_values
        from openreading.ledger.header import slim_request
        from openreading.types.batch import BatchItemError

        ctx = RunContext(credentials=credentials)
        results = (job.raw.payload if job.raw else {}).get("results") or []
        by_id = {r.get("custom_id"): r for r in results}
        out: list[Any] = []
        for i, req in enumerate(reqs):
            entry = by_id.get(f"item-{i}")
            result = (entry or {}).get("result") or {}
            rtype = result.get("type")
            if rtype == "succeeded" and result.get("message"):
                mode = "extract" if req.extraction_schema else "parse"
                # Ledger T4b: the native-batch path has the same
                # exact-page-count blind spot as submit(), with the same silent-
                # degradation shape — `req` here is the i-th item's own REAL, unslimmed request
                # (normalize_many's Protocol takes `reqs: list[OpenReadingRequest]` directly, never
                # a slimmed copy, unlike submit()/normalize()'s slim_req split), so the exact count
                # can be computed the same way submit() does for the single-document path, before
                # this synthetic per-item job's payload is built.
                pdf_bytes = self._pdf_bytes(req)
                payload = dict(result["message"])
                payload["_pdf_page_count"] = (
                    pdf_page_count(pdf_bytes) if pdf_bytes is not None else None
                )
                synth = self.new_job(WaitMode.INLINE, state=JobState.SUCCEEDED)
                synth.raw = RawResult(
                    payload=payload,
                    media_type="application/json",
                    encoding="json",
                    object_class=mode,
                )
                try:
                    slim_req = slim_request(req)
                    out.append(
                        apply_cost_report(
                            self, synth, self.normalize(synth, ctx, slim_req), credentials
                        )
                    )
                except Exception as e:  # noqa: BLE001 - per-item isolation (BL-113): a mapping
                    # bug on THIS item must not discard items 0..i-1's already-succeeded results.
                    out.append(
                        BatchItemError(
                            code=type(e).__name__,
                            message=redact(str(e), secret_values(self.descriptor, credentials)),
                        )
                    )
            else:
                err = result.get("error") or {}
                out.append(
                    BatchItemError(
                        code=rtype or "missing_result",
                        message=str(err.get("message") or err or rtype or "no result"),
                    )
                )
        return out

    def _map_error(self, e: Exception):
        name = type(e).__name__
        if name in ("AuthenticationError", "PermissionDeniedError"):
            return TerminalError(str(e), backend_code="auth_rejected")
        # native-PDF ceiling (~100 pages / 32MB): a size/page rejection is terminal, not retryable.
        if "RequestTooLarge" in name or any(
            s in str(e).lower()
            for s in ("too large", "too_large", "exceeds the maximum", "page limit")
        ):
            return TerminalError(
                f"document exceeds Claude's native-PDF limit (~100 pages / 32MB): {e}",
                backend_code="doc_too_large",
            )
        if name in _RETRYABLE_EXC:
            retry_after = None
            resp = getattr(e, "response", None)
            if resp is not None:
                try:
                    retry_after = float(resp.headers.get("retry-after"))
                except (TypeError, ValueError, AttributeError):
                    retry_after = None
            return RetryableError(str(e), backend_code=name, retry_after=retry_after)
        return TerminalError(str(e), backend_code=name)

    # ---- normalize -----------------------------------------------------------
    def normalize(
        self, job: Job, ctx: RunContext, slim_req: OpenReadingRequest
    ) -> NormalizedResponse:
        raw: dict[str, Any] = job.raw.payload if job.raw else {}
        mode = job.raw.object_class if job.raw else "parse"
        outputs = slim_req.outputs or Outputs()
        content = raw.get("content", [])
        model = raw.get("model")

        field_types = self._schema_field_types(slim_req)
        typed_fields: dict[str, TypedField] = {}
        text_parts: list[str] = []
        for cblock in content:
            ctype = cblock.get("type")
            if ctype == "text":
                text_parts.append(cblock.get("text", ""))
            elif ctype == "tool_use":
                # P2: preserve Claude page_location citations (when present) on the derived fields.
                cits = self._block_citations(cblock)
                for k, v in (cblock.get("input") or {}).items():
                    typed_fields[k] = TypedField(value=v, type=field_types.get(k), citations=cits)
        markdown = "".join(text_parts).strip()
        # C1/C2: document.text is a PLAIN projection of the model's markdown (md_to_text) — never
        # the markdown verbatim. The markdown channel keeps the native markdown.
        text = md_to_text(markdown) if markdown else ""

        # blocks (D, §4.3): the model's markdown → typed bbox-less blocks in ONE synthetic page.
        # No geometry is fabricated (block_bbox stays X); page attribution is genuinely unavailable
        # for a whole-document token stream, so a machine-readable warning names that below.
        pages: list[Page] | None = None
        if outputs.blocks and markdown:
            blocks = md_to_blocks(markdown)
            if blocks:
                pages = [Page(page_number=1, blocks=blocks)]

        # C10 status.honest: Claude stops at max_tokens on truncation — never a bare SUCCEEDED.
        truncated = raw.get("stop_reason") == "max_tokens"

        resp = NormalizedResponse(
            status=Status(state=ResponseState.PARTIAL if truncated else ResponseState.SUCCEEDED),
            backend=BackendInfo(
                id="anthropic-claude",
                type=BackendType.HOSTED_API,
                version=model,
                operation=mode,
                output_paradigm=[OutputParadigm.TOKEN_STREAM],
            ),
            document=Document(
                markdown=markdown if (outputs.markdown and markdown) else None,
                text=text if (outputs.text and text) else None,
                page_count=self._page_count(content, raw.get("_pdf_page_count")),
                pages=pages,
            ),
            typed_fields=typed_fields or None,
        )
        if truncated:
            resp.add_warning(
                "output_truncated",
                "Claude stopped at max_tokens; the output is incomplete (stop_reason=max_tokens).",
            )
        # §4.3 container rule: the synthetic single-page container is NOT a real page attribution.
        if pages is not None:
            resp.add_warning(
                "page_attribution_unavailable",
                "Claude returns a whole-document token stream with no page geometry; "
                "markdown-derived blocks live in one synthetic Page(1) (§4.3 container rule).",
                "blocks",
            )
        elif outputs.blocks:
            # C6: blocks requested but this operation produced no token stream (e.g. tool-use
            # extraction returns typed fields, not prose) — name the absence, never silently drop.
            resp.add_warning(
                "blocks_unavailable",
                "no token stream to derive blocks from (e.g. tool-use extraction).",
                "blocks",
            )
        # C6 (deliver-or-warn): tool-use extraction yields typed fields, not prose — name every
        # requested content channel this operation left empty rather than dropping it silently.
        if outputs.markdown and resp.document.markdown is None:
            resp.add_warning(
                "markdown_unavailable",
                "this operation produced no markdown (e.g. tool-use extraction returns fields).",
                "markdown",
            )
        if outputs.text and resp.document.text is None:
            resp.add_warning(
                "text_unavailable",
                "this operation produced no text (e.g. tool-use extraction returns fields).",
                "text",
            )
        resp.channel_provenance = self._provenance(resp, pages, typed_fields)
        u = raw.get("usage", {})
        from openreading.types.response import Usage

        resp.usage = Usage(input_tokens=u.get("input_tokens"), output_tokens=u.get("output_tokens"))
        if outputs.include_backend_raw:
            resp.backend_raw = BackendRaw(
                encoding="json",
                media_type="application/json",
                object_class="anthropic.Message",
                payload=raw,
            )
        return resp

    def _schema_field_types(self, req: OpenReadingRequest) -> dict[str, str]:
        """Declared JSON-Schema `type` per field, for TypedField.type (P2). Only string types are
        carried (a JSON `type` array like ["string","null"] has no single scalar form)."""
        schema = req.extraction_schema.json_schema if req.extraction_schema else None
        props = schema.get("properties") if isinstance(schema, dict) else None
        if not isinstance(props, dict):
            return {}
        out: dict[str, str] = {}
        for name, spec in props.items():
            t = spec.get("type") if isinstance(spec, dict) else None
            if isinstance(t, str):
                out[name] = t
        return out

    def _block_citations(self, cblock: dict) -> list[Citation] | None:
        """Claude page_location citations on a content block → Citation objects (P2). Page numbers
        are 1-indexed; `cited_text` is the quoted source span. `None` when the block has none."""
        cits: list[Citation] = []
        for c in cblock.get("citations") or []:
            if c.get("type") == "page_location":
                cits.append(Citation(page=c.get("start_page_number"), text=c.get("cited_text")))
        return cits or None

    def _provenance(
        self,
        resp: NormalizedResponse,
        pages: list[Page] | None,
        typed_fields: dict[str, TypedField],
    ) -> dict[str, str] | None:
        """Per-response provenance (§3.3/§6.4): the model's markdown and tool-use fields are native;
        text/blocks/table_cells are the platform's derivation from that markdown."""
        prov: dict[str, str] = {}
        if resp.document.markdown is not None:
            prov["markdown"] = "native"
        if resp.document.text is not None:
            prov["text"] = "derived"
        if pages is not None:
            prov["blocks"] = "derived"
            if any(b.table is not None for pg in pages for b in (pg.blocks or [])):
                prov["table_cells"] = "derived"
        if typed_fields:
            prov["typed_fields"] = "native"
        return prov or None

    def _page_count(self, content: list, precomputed_page_count: int | None) -> int | None:
        """Prefer the exact, byte-derived count `submit()` computed once (before the request was
        slimmed) and carried on `job.raw.payload["_pdf_page_count"]`; else fall back to the
        distinct-cited-pages heuristic (which undercounts and is None in extract mode).

        Ledger T4b: this deliberately never reads real document bytes
        itself anymore — it used to call `self._pdf_bytes(req)` on `slim_req`, whose bytes are
        always `None` by design (see `ledger.header.slim_request`), so the exact count silently
        degraded to this heuristic for every base64/path-intake request. The openreading.adapters runbook's rule:
        `normalize()` reads `job.raw.payload` ONLY, never a field the caller might have supplied
        out-of-band via `slim_req`/`req`."""
        if precomputed_page_count is not None:
            return precomputed_page_count
        return self._cited_page_count(content)

    def _pdf_bytes(self, req: OpenReadingRequest) -> bytes | None:
        d = req.document
        try:
            if d.bytes_base64:
                return base64.b64decode(d.bytes_base64)
            if d.path:
                with open(d.path, "rb") as fh:
                    return fh.read()
        except (ValueError, OSError):
            return None
        return None

    def _cited_page_count(self, content: list) -> int | None:
        pages = set()
        for block in content:
            for c in block.get("citations") or []:
                if c.get("type") == "page_location":
                    for p in range(c.get("start_page_number", 0), c.get("end_page_number", 0) + 1):
                        if p:
                            pages.add(p)
        return len(pages) or None

    def report_cost(self, job: Job) -> CostReport:
        raw = job.raw.payload if job.raw else {}
        model = raw.get("model") or _DEFAULT_MODEL
        u = raw.get("usage", {})
        pin, pout = _MODEL_PRICE.get(model, _MODEL_PRICE[_DEFAULT_MODEL])
        cost = (u.get("input_tokens", 0) / 1e6) * pin + (u.get("output_tokens", 0) / 1e6) * pout
        return CostReport(
            native_unit="token",
            native_quantity=float(u.get("input_tokens", 0) + u.get("output_tokens", 0)),
            cost_usd=cost,
            basis=CostBasis.ESTIMATED,
            billing_target="caller_account",
        )
