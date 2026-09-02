"""BackendAdapter — the one interface that wraps all four backend types (adapter_interface.md
§2). The router calls only these eight methods + reads the static AdapterDescriptor; it never
branches on backend type. INLINE backends complete inside submit(); POLL/WEBHOOK backends
return a RUNNING Job the driver advances.

`BackendAdapter` is the ABC real adapters subclass (it supplies INLINE-friendly defaults for
poll/resolve_webhook/cancel so an in-process library only implements submit/normalize/etc.).
`AdapterProtocol` is the structural type the router/conformance-kit use.

Two OPTIONAL capabilities sit beside the required 8, each as its own `@runtime_checkable`
Protocol that the platform feature-detects and gates on a descriptor declaration — never as extra
members of `AdapterProtocol`, which would break `isinstance` for every adapter that doesn't
implement them: `NativeBatchAdapter` (many documents in one submission) and `LivenessProbeAdapter`
(is this backend actually answering — internal/design/liveness.md).
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

from openreading.types.batch import BatchItemError
from openreading.types.cost import CostReport
from openreading.types.descriptor import AdapterDescriptor
from openreading.types.enums import JobState, WaitMode
from openreading.types.errors import UnsupportedFeatureError
from openreading.types.job import Job
from openreading.types.liveness import ProbeResult
from openreading.types.request import OpenReadingRequest
from openreading.types.response import NormalizedResponse
from openreading.types.runtime import Health, ResolvedCredentials, RunContext


def _cap_supported(value: object) -> bool:
    """A descriptor capability grade counts as 'supported' unless it is an explicit negative."""
    return value not in (False, None, "", "false")


@runtime_checkable
class AdapterProtocol(Protocol):
    descriptor: AdapterDescriptor

    def capabilities(self) -> dict: ...
    def health(self) -> Health: ...
    def submit(self, req: OpenReadingRequest, ctx: RunContext) -> Job: ...
    def poll(self, job: Job, ctx: RunContext) -> Job: ...
    def resolve_webhook(self, event: dict, job: Job, ctx: RunContext) -> Job: ...
    def cancel(self, job: Job, ctx: RunContext) -> Job: ...
    def normalize(
        self, job: Job, ctx: RunContext, slim_req: OpenReadingRequest
    ) -> NormalizedResponse: ...
    def report_cost(self, job: Job) -> CostReport: ...


@runtime_checkable
class NativeBatchAdapter(Protocol):
    """Optional native-batch capability (Manifest v0.6 §7). An adapter whose provider offers a
    real multi-document submission implements these two methods; the batch runner feature-detects
    them (isinstance against this runtime_checkable Protocol) and dispatches to the native path when
    the descriptor's `batch.native` is truthy. NOT part of the required 8-method surface — absent by
    default, exactly like poll/resolve_webhook are no-ops for INLINE adapters.

    `normalize_many`'s `credentials` (BL-108): optional so this stays backward compatible —
    `@runtime_checkable` `Protocol`s check method presence, not exact signature — but a caller
    (`_run_native`) that has `ResolvedCredentials` in scope should pass them through so an
    implementation that calls `apply_cost_report` internally (as `anthropic-claude`'s does, to
    meter each batch item off its own synthetic per-item job) can redact a `report_cost()`
    failure's warning exactly like every other `apply_cost_report` call site already does."""

    def submit_many(self, reqs: list[OpenReadingRequest], ctx: RunContext) -> Job: ...
    def normalize_many(
        self,
        job: Job,
        reqs: list[OpenReadingRequest],
        credentials: ResolvedCredentials | None = None,
    ) -> list[NormalizedResponse | BatchItemError]: ...


@runtime_checkable
class LivenessProbeAdapter(Protocol):
    """Optional liveness-probe capability (internal/design/liveness.md §3.1). An adapter whose backend
    can actually be *contacted* to prove it is up implements this one method; the platform
    (`openreading.liveness.check_liveness`) feature-detects it (isinstance against this
    runtime_checkable Protocol) and calls it only when the descriptor's `liveness.probe` is not
    `"none"`. NOT part of the required 8-method surface — the exact shape `NativeBatchAdapter`
    above already establishes for an optional capability, and deliberately NOT a 9th member of
    `AdapterProtocol`: that Protocol is runtime_checkable, so a 9th member would make every
    third-party adapter that doesn't implement it fail `isinstance`.

    `probe_liveness` is the ONE method in this codebase explicitly permitted to do network I/O on a
    non-execution path — and nothing reachable from the offline gate ever calls it (§8). Its
    contract:

    * It MUST honor `timeout_s` and MUST NOT hang the caller.
    * It takes NO `OpenReadingRequest` — by construction there is no document, page, or caller
      content to forward, so a probe can never become a data path (§7).
    * It MUST NOT be a billed request. A vendor with no free liveness call declares
      `liveness.probe = "none"` and gets the honest inferred status instead; spending the caller's
      money on a diagnostic they did not ask to pay for is worse than not answering.
    * It MUST NOT return a secret, or a provider's raw response body, in any field. The platform
      redacts defensively on the way out, but the adapter is the first line.
    * It returns a `ProbeResult` — only what it OBSERVED. It never decides `not_configured` vs
      `configured_unverified`; the platform owns that ladder so it exists once instead of in every
      adapter.
    """

    def probe_liveness(self, ctx: RunContext, *, timeout_s: float) -> ProbeResult: ...


class BackendAdapter(ABC):
    """Base class with the 8-method surface. Subclasses MUST set `descriptor` and implement
    `health`, `submit`, `normalize`, `report_cost`; POLL/WEBHOOK backends also override
    `poll`/`resolve_webhook`.

    It also supplies an honest default for the optional 9th method (`probe_liveness`), so every
    existing adapter — the 15 built-ins and any third-party subclass — keeps working untouched and
    simply reports "no probe"."""

    descriptor: AdapterDescriptor

    # ---- static declaration --------------------------------------------------
    def capabilities(self) -> dict:
        """Runtime capability view; defaults to the static descriptor (may probe-refine)."""
        return self.descriptor.capabilities.model_dump(mode="json")

    # ---- lifecycle -----------------------------------------------------------
    @abstractmethod
    def health(self) -> Health:
        """Cheap readiness probe. Hosted: creds + reachability. Library: import + version +
        system-dep presence. Container/model: /health + warm/cold + VRAM free."""
        ...

    def probe_liveness(self, ctx: RunContext, *, timeout_s: float) -> ProbeResult:
        """Optional 9th method — the LIVENESS MEASUREMENT (internal/design/liveness.md §3). Default:
        "I do not implement a probe", which is what keeps this addition backward compatible.

        Unlike `health()` — which asks "do my Python deps import here" and is offline by
        construction, so `make verify` can call it freely — this asks "did the backend actually
        answer", and is therefore allowed to touch the network and is NEVER called from the
        offline gate. Override it only when the answer is obtainable for free; see the
        `LivenessProbeAdapter` Protocol above for the full contract, and declare
        `descriptor.liveness` when you do, or the platform will never call it."""
        return ProbeResult.unsupported()

    # ---- execution -----------------------------------------------------------
    @abstractmethod
    def submit(self, req: OpenReadingRequest, ctx: RunContext) -> Job:
        """Begin work. INLINE → SUCCEEDED Job carrying .raw. POLL/WEBHOOK → RUNNING Job with
        poll_handle/webhook_token. Raises TerminalError for bad input at submit time."""
        ...

    def poll(self, job: Job, ctx: RunContext) -> Job:
        """Advance one step. INLINE default: already terminal, return unchanged. POLL backends
        override to call the backend status endpoint and set state/.raw/.error/.next_poll_at.

        `ctx` (Ledger T4a): every overriding POLL adapter calls `self._get_client(ctx)` at its own
        top rather than assuming a client bound at submit() time survived on this instance — the
        one that lets a fresh, freshly-constructed instance (a different process, a resumed job)
        resume driving a job with no instance-affinity requirement (AC-5)."""
        return job

    def resolve_webhook(self, event: dict, job: Job, ctx: RunContext) -> Job:
        """WEBHOOK backends override to verify the signature and map the event to a terminal Job.
        Default (non-webhook adapters): idempotent no-op returning the job unchanged.

        `ctx` (Ledger T4a): same rationale as `poll`'s own — an overriding adapter builds its
        client from `ctx` rather than relying on instance-bound state."""
        return job

    def cancel(self, job: Job, ctx: RunContext) -> Job:
        """Best-effort local cancel. Most hosted async ops have no server cancel → mark
        CANCELLED and stop polling. Subprocess/container adapters override to kill the process.

        `ctx` (Ledger T4a): an overriding adapter that issues a real vendor cancel call builds its
        client from `ctx` rather than relying on instance-bound state — closing BL-164's own gap
        (a fresh, client-less adapter instance silently no-opping cancel() instead of reaching the
        vendor)."""
        if not job.is_terminal():
            job.state = JobState.CANCELLED
        return job

    # ---- transform -----------------------------------------------------------
    @abstractmethod
    def normalize(
        self, job: Job, ctx: RunContext, slim_req: OpenReadingRequest
    ) -> NormalizedResponse:
        """Map job.raw → NormalizedResponse. Fill only what the backend produced; record every
        requested-but-unavailable channel as a response.warnings[] entry; never fabricate
        bbox/confidence. Attach backend_raw per the §5 serialization policy.

        `ctx` (Ledger T4b): added for signature uniformity with submit/poll/resolve_webhook/cancel
        — every adapter method that touches the walk now receives one. No current implementation
        needs `ctx.credentials`; it is here for the completed, uniform shape alone.

        `slim_req` (Ledger T4b, AC-9-adjacent): the caller's own `OpenReadingRequest` with
        `document.bytes_base64`/`document.password`/`document.url`/`async.webhook_url` nulled
        (`ledger.header.slim_request`), computed once at the call site. `normalize` never reads a
        secret-class field today, so this closes a latent exposure surface for any FUTURE
        implementation that might otherwise be handed the full request."""
        ...

    @abstractmethod
    def report_cost(self, job: Job) -> CostReport:
        """Project job.raw usage counters + the pricing model into a CostReport."""
        ...

    # ---- helpers -------------------------------------------------------------
    def assert_supports(self, req: OpenReadingRequest) -> None:
        """Pre-flight for a DIRECTLY-named backend: if the request's primary structured-extraction
        ask cannot be met by this backend, raise UnsupportedFeatureError rather than silently
        dropping it (guardrail 1 — never silently deny a channel the caller asked for). The router
        pre-filters these at stage 2 (capability filter), so this only fires on the direct-named
        path the router doesn't cover (e.g. `openreading parse --backend pymupdf --extract`).

        This is the fourth error-taxonomy member: unsupported-feature is surfaced (→ the caller/CLI
        turns it into an advisory, the router falls back) instead of fabricating an empty result.
        Optional-but-unavailable channels are handled the other way — normalize() records them as
        warnings[] and returns what the backend CAN produce."""
        caps = self.descriptor.capabilities
        if req.extraction_schema is not None and not _cap_supported(caps.custom_schema_extraction):
            raise UnsupportedFeatureError(
                f"{self.descriptor.id} cannot perform schema-driven field extraction; "
                f"route to an extraction-capable backend (e.g. google-document-ai, reducto)",
                feature="custom_schema_extraction",
            )

    def new_job(self, wait_mode: WaitMode, **kwargs) -> Job:
        return Job(
            id=kwargs.pop("id", None) or f"omjob_{uuid.uuid4().hex}",
            backend_id=self.descriptor.id,
            wait_mode=wait_mode,
            **kwargs,
        )
