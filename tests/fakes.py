"""Shared test fakes: minimal backend adapters plus the factories that build their descriptors.

Reach for `InlineFake`, `PollFake`, `WebhookFake` or `NeverFinishesFake` to exercise one wait mode
through the driver. Reach for `make_backend` when a router or compliance test needs a descriptor
with particular compliance, capability, cost or priority values. Strategy-engine tests use
`ScriptedBackend`, `PollFaultBackend` and `scripted_registry`. Compare tests use `make_envelope`.

Tests import this module as `tests.fakes`, which works because pyproject sets `pythonpath = ["."]`
and `tests/__init__.py` exists."""

from __future__ import annotations

from typing import Any

from openreading.adapters.base import BackendAdapter
from openreading.types import (
    AdapterDescriptor,
    BackendInfo,
    BackendType,
    Capabilities,
    ComplianceProfile,
    Cost,
    CostReport,
    Document,
    Health,
    Job,
    JobState,
    NormalizedResponse,
    Provisioning,
    RawResult,
    ResponseState,
    RouterHints,
    RuntimeProfile,
    Status,
    WaitMode,
    infra_only,
)
from openreading.types.descriptor import CredentialField
from openreading.types.errors import RetryableError, TerminalError
from openreading.types.request import OpenReadingRequest
from openreading.types.response import Page, Usage
from openreading.types.runtime import RunContext


def make_envelope(
    backend_id: str = "backend",
    *,
    state: str = "succeeded",
    backend_type: str = "hosted_api",
    output_paradigm: list[str] | None = None,
    markdown: str | None = None,
    text: str | None = None,
    fields: dict[str, Any] | None = None,
    pages: list[Any] | None = None,
    warnings: list[str] | None = None,
    duration_ms: float | None = None,
    cost_usd: float | None = None,
    cost_basis: str | None = None,
    pages_processed: int | None = None,
    validate: bool = True,
) -> dict[str, Any]:
    """H1 — build a schema-valid response envelope directly (no adapters, no I/O) for compare
    tests. `fields` maps name→value or name→{value,confidence,…}; `pages` items are page dicts or
    bare block-lists. Validates against the response schema unless `validate=False`."""
    doc: dict[str, Any] = {}
    if markdown is not None:
        doc["markdown"] = markdown
    if text is not None:
        doc["text"] = text
    norm_pages: list[dict[str, Any]] = []
    for i, pg in enumerate(pages or [], start=1):
        if isinstance(pg, dict):
            page = dict(pg)
            page.setdefault("page_number", i)
        else:  # a bare list of blocks
            page = {"page_number": i, "blocks": list(pg)}
        norm_pages.append(page)
    if norm_pages:
        doc["pages"] = norm_pages
        doc["page_count"] = len(norm_pages)
    # The response schema requires a common-denominator channel; an otherwise-empty envelope
    # (the "empty output" case) still needs one, so seed an empty text surface.
    if not any(k in doc for k in ("markdown", "text", "pages")) and not fields:
        doc["text"] = ""

    resp: dict[str, Any] = {
        "schema_version": "0.3",
        "status": {"state": state},
        "backend": {"id": backend_id, "type": backend_type},
        "document": doc,
    }
    if output_paradigm is not None:
        resp["backend"]["output_paradigm"] = output_paradigm

    typed_fields = {
        name: (v if isinstance(v, dict) else {"value": v}) for name, v in (fields or {}).items()
    }
    if typed_fields:
        resp["typed_fields"] = typed_fields

    usage = {
        k: v
        for k, v in (
            ("duration_ms", duration_ms),
            ("cost_usd", cost_usd),
            ("cost_basis", cost_basis),
            ("pages_processed", pages_processed),
        )
        if v is not None
    }
    if usage:
        resp["usage"] = usage
    if warnings:
        resp["warnings"] = [{"code": c, "message": c} for c in warnings]

    if validate:
        from openreading.schemas import validate_response

        validate_response(resp)
    return resp


def make_descriptor(backend_id: str, wait_modes: list[WaitMode]) -> AdapterDescriptor:
    """A minimal hosted_api descriptor with the given wait modes and fixed values elsewhere."""
    return AdapterDescriptor(
        id=backend_id,
        type=BackendType.HOSTED_API,
        protocol_version=1,
        provisioning=Provisioning(auth="api_key", billing_target="caller_account"),
        wait_modes=wait_modes,
        capabilities=Capabilities(ocr="claimed"),
        cost=Cost(native_unit="page", basis="estimated"),
        compliance=ComplianceProfile(hipaa_baa="no"),
        runtime=RuntimeProfile(offline_capable=False),
    )


class ConfigurableBackend(BackendAdapter):
    """An INLINE adapter whose descriptor (compliance/capabilities/cost/priority) is fully
    parameterized — used to reproduce the `internal/design/routing_and_compliance.md` worked
    examples."""

    def __init__(self, descriptor: AdapterDescriptor) -> None:
        self.descriptor = descriptor

    def health(self) -> Health:
        return Health(ready=True)

    def submit(self, req: OpenReadingRequest, ctx: RunContext) -> Job:
        job = self.new_job(WaitMode.INLINE, state=JobState.SUCCEEDED)
        job.raw = RawResult(payload=f"{self.descriptor.id} result")
        return job

    def normalize(self, job: Job, ctx: RunContext, req: OpenReadingRequest) -> NormalizedResponse:
        return NormalizedResponse(
            status=Status(state=ResponseState.SUCCEEDED),
            backend=BackendInfo(id=self.descriptor.id, type=self.descriptor.type),
            document=Document(text=str(job.raw.payload if job.raw else "")),
        )

    def report_cost(self, job: Job) -> CostReport:
        return infra_only("page", 1.0)


def make_backend(
    backend_id: str,
    *,
    btype: BackendType = BackendType.HOSTED_API,
    local: bool = False,
    hipaa_baa: str = "no",
    trains: str = "no",
    regions: list[str] | None = None,
    max_retention_hours: int | None = None,
    soc2: bool = False,
    gdpr: bool = False,
    handwriting: bool = False,
    forms: bool = False,
    tables: bool = False,
    custom_schema: bool = False,
    input_formats: list[str] | None = None,
    priority: str = "P1",
    cost_low: float | None = None,
    cost_high: float | None = None,
    page_ranges: bool = False,
    webhook: bool = False,
) -> ConfigurableBackend:
    """A ConfigurableBackend whose descriptor carries the given compliance, capability and cost."""
    compliance_extra: dict = {}
    if max_retention_hours is not None:
        compliance_extra["max_retention_hours"] = max_retention_hours
    desc = AdapterDescriptor(
        id=backend_id,
        type=btype,
        protocol_version=1,
        provisioning=Provisioning(
            auth="none" if local else "api_key",
            billing_target="caller_infra" if local else "caller_account",
        ),
        wait_modes=[WaitMode.WEBHOOK, WaitMode.INLINE] if webhook else [WaitMode.INLINE],
        capabilities=Capabilities(
            ocr="verified",
            handwriting="verified" if handwriting else False,
            forms_key_value="verified" if forms else False,
            printed_tables="verified" if tables else False,
            custom_schema_extraction="verified" if custom_schema else False,
            input_formats=input_formats or [],
            # extra-allowed capability (granularity:page, §2.7); dict-spread keeps pyright quiet
            **{"page_range_selection": page_ranges},
        ),
        cost=Cost(
            native_unit="cpu_second" if local else "page",
            basis="infra_only" if local else "estimated",
            usd_per_page_equiv_low=cost_low,
            usd_per_page_equiv_high=cost_high,
        ),
        compliance=ComplianceProfile(
            hipaa_baa=hipaa_baa,
            soc2=soc2,
            gdpr=gdpr,
            trains_on_customer_data=trains,
            data_region_options=regions or (["*"] if local else []),
            runs_fully_local=local,
            **compliance_extra,
        ),
        runtime=RuntimeProfile(offline_capable=local),
        router=RouterHints(integration_priority=priority),
    )
    return ConfigurableBackend(desc)


class _NormalizeMixin:
    def normalize(  # type: ignore[override]
        self, job: Job, ctx: RunContext, req: OpenReadingRequest
    ) -> NormalizedResponse:
        text = (job.raw.payload if job.raw else None) or ""
        return NormalizedResponse(
            status=Status(state=ResponseState.SUCCEEDED),
            backend=BackendInfo(id=self.descriptor.id, type=BackendType.HOSTED_API),
            document=Document(text=str(text)),
        )

    def report_cost(self, job: Job) -> CostReport:  # type: ignore[override]
        return infra_only("page", 1.0)

    def health(self) -> Health:  # type: ignore[override]
        return Health(ready=True)


class InlineFake(_NormalizeMixin, BackendAdapter):
    """Succeeds inside submit(), the shortest path through the driver."""

    def __init__(self) -> None:
        self.descriptor = make_descriptor("inline-fake", [WaitMode.INLINE])

    def submit(self, req: OpenReadingRequest, ctx: RunContext) -> Job:
        job = self.new_job(WaitMode.INLINE, state=JobState.SUCCEEDED)
        job.raw = RawResult(payload="inline result")
        return job


class PollFake(_NormalizeMixin, BackendAdapter):
    """SUCCEEDS after `polls_needed` polls; optionally raises RetryableError `flaky` times first.

    `backend_code` (BL-88): defaults to None (plain "transient" failures, as always). Pass
    `"deadline_exceeded"` to model a GENUINE adapter-raised exhaustion that happens to carry the
    exact sentinel string `driver.py`'s own per-call deadline check uses — proving `get_job`'s
    discriminator tells the two apart by exception TYPE (`_DriveSliceExpired`), not by re-deriving
    a `backend_code` string comparison that this fixture is built specifically to collide with.

    `poll_calls` (BL-92): counts every real `poll()` invocation, so a test can assert a GET made
    (or didn't make) genuine contact with the backend — the whole point of BL-92's regression test,
    where a "running" result alone can't tell a harmless not-done-yet from a call that never
    actually reached the vendor at all.
    """

    def __init__(
        self, polls_needed: int = 2, flaky: int = 0, backend_code: str | None = None
    ) -> None:
        self.descriptor = make_descriptor("poll-fake", [WaitMode.POLL])
        self._polls_needed = polls_needed
        self._flaky = flaky
        self._backend_code = backend_code
        self.poll_calls = 0

    def submit(self, req: OpenReadingRequest, ctx: RunContext) -> Job:
        return self.new_job(
            WaitMode.POLL,
            state=JobState.RUNNING,
            backend_job_id="job-123",
            poll_handle={"remaining": self._polls_needed, "flaky": self._flaky},
            next_poll_at=0.0,
        )

    def poll(self, job: Job, ctx: RunContext) -> Job:
        self.poll_calls += 1
        h = job.poll_handle or {}
        if h.get("flaky", 0) > 0:
            h["flaky"] -= 1
            raise RetryableError("transient", retry_after=0.01, backend_code=self._backend_code)
        h["remaining"] = h.get("remaining", 0) - 1
        if h["remaining"] <= 0:
            job.state = JobState.SUCCEEDED
            job.raw = RawResult(payload="poll result")
        else:
            job.next_poll_at = (job.next_poll_at or 0.0) + 10.0
        return job


class PollFaultBackend(_NormalizeMixin, BackendAdapter):
    """A POLL backend whose status call faults AFTER the job was accepted (harness H2 fault
    injection): the attempt fails while its backend job is still RUNNING — and still billing — at
    the vendor. Records every `cancel()` it is asked to perform, so the loser-cancellation path
    (`internal/design/execution.md` §3.3) can be asserted end-to-end. Descriptor shape matches
    the strategy-engine fakes (`make_backend`), so it can stand in for a hosted branch in a
    `parallel` node."""

    def __init__(
        self, backend_id: str, *, cost_low: float | None = 0.01, cancel_sleep_s: float = 0.0
    ) -> None:
        self.descriptor = make_backend(backend_id, cost_low=cost_low).descriptor.model_copy(
            update={"wait_modes": [WaitMode.POLL]}
        )
        self.submitted: list[Job] = []
        self.cancelled: list[Job] = []
        # BL-164: a real vendor cancel() is a blocking HTTP call. `cancel_sleep_s` simulates that
        # round trip without a network, so a test can prove `engine.py` dispatches it off the
        # event loop rather than blocking it. `cancel_started` records entry before the sleep and
        # `cancelled` only after it returns, so a test whose deadline is shorter than the sleep
        # can still prove the attempt was dispatched.
        self._cancel_sleep_s = cancel_sleep_s
        self.cancel_started: list[Job] = []
        # (entered, returned) monotonic bounds of each cancel(), so a test can prove CONCURRENT
        # dispatch by overlap instead of by a wall-clock budget: two cancels that overlap were in
        # flight together, and two dispatched serially cannot overlap however slow the machine is.
        self.cancel_windows: list[tuple[float, float]] = []

    def submit(self, req: OpenReadingRequest, ctx: RunContext) -> Job:
        job = self.new_job(WaitMode.POLL, state=JobState.RUNNING, backend_job_id="job-1")
        self.submitted.append(job)
        return job

    def poll(self, job: Job, ctx: RunContext) -> Job:
        raise TerminalError("status endpoint returned 500", backend_code="server")

    def cancel(self, job: Job, ctx: RunContext) -> Job:
        import time

        self.cancel_started.append(job)
        entered = time.monotonic()
        if self._cancel_sleep_s:
            time.sleep(self._cancel_sleep_s)  # simulates a real synchronous vendor HTTP call
        self.cancel_windows.append((entered, time.monotonic()))
        self.cancelled.append(job)
        return super().cancel(job, ctx)


class NeverFinishesFake(_NormalizeMixin, BackendAdapter):
    """Stays RUNNING forever. Each poll pushes next_poll_at out by 1000 s, for deadline tests."""

    def __init__(self) -> None:
        self.descriptor = make_descriptor("never-fake", [WaitMode.POLL])

    def submit(self, req: OpenReadingRequest, ctx: RunContext) -> Job:
        return self.new_job(WaitMode.POLL, state=JobState.RUNNING, next_poll_at=0.0)

    def poll(self, job: Job, ctx: RunContext) -> Job:
        job.next_poll_at = (job.next_poll_at or 0.0) + 1000.0
        return job


class WebhookFake(_NormalizeMixin, BackendAdapter):
    """Finishes only when resolve_webhook() sees its own token with status 'done'."""

    def __init__(self) -> None:
        self.descriptor = make_descriptor("webhook-fake", [WaitMode.WEBHOOK, WaitMode.POLL])

    def submit(self, req: OpenReadingRequest, ctx: RunContext) -> Job:
        return self.new_job(
            WaitMode.WEBHOOK, state=JobState.RUNNING, webhook_token="tok-abc", next_poll_at=0.0
        )

    def resolve_webhook(self, event: dict, job: Job, ctx: RunContext) -> Job:
        if event.get("token") == job.webhook_token and event.get("status") == "done":
            job.state = JobState.SUCCEEDED
            job.raw = RawResult(payload=event.get("result", "webhook result"))
        return job


class ScriptedBackend(BackendAdapter):
    """A per-run-instance fake driven by one scripted outcome (harness H2). Either returns a
    NormalizedResponse built from the given content, or raises the given exception from submit().
    INLINE, so the driver returns immediately. Records every RunContext it saw (fresh-instance /
    credential-leak assertions)."""

    def __init__(
        self,
        backend_id: str,
        *,
        local: bool = False,
        cost_low: float | None = None,
        trains: str = "no",
        hipaa_baa: str = "no",
        error: Exception | None = None,
        normalize_error: Exception | None = None,
        text: str = "The quick brown fox jumps over the lazy dog, and it does so every day.",
        confidence: float | None = None,
        typed_fields: dict | None = None,
        cost_usd: float | None = None,
        pages: list | None = None,
        required_env: list[str] | None = None,
        latency_ms: int = 0,
        page_ranges: bool = False,
        webhook: bool = False,
        report_cost_error: Exception | None = None,
    ) -> None:
        self.descriptor = make_backend(
            backend_id,
            local=local,
            cost_low=cost_low,
            trains=trains,
            hipaa_baa=hipaa_baa,
            page_ranges=page_ranges,
            webhook=webhook,
        ).descriptor
        self.test_latency_ms = latency_ms  # virtual latency for deterministic race tests
        if required_env:
            self.descriptor = self.descriptor.model_copy(
                update={
                    "credentials_spec": [
                        CredentialField(key=e.lower(), required=True, secret=True, env=[e])
                        for e in required_env
                    ]
                }
            )
        self._error = error
        # BL-99: a plain, non-AdapterError exception (KeyError/ValueError/...) raised from
        # normalize(). A distinct knob from `error`, which is raised from submit() and is always
        # an AdapterError in every existing caller, including BL-85's and BL-93's tests. Reaching
        # normalize() at all needs a healthy submit(), because the job must be accepted and
        # driven to completion first.
        self._normalize_error = normalize_error
        self._text = text
        self._confidence = confidence
        self._typed_fields = typed_fields
        self._cost_usd = cost_usd
        self._pages = pages
        self._report_cost_error = report_cost_error
        self.contexts: list[RunContext] = []
        self.requests: list[OpenReadingRequest] = []

    def health(self) -> Health:
        return Health(ready=True)

    def submit(self, req: OpenReadingRequest, ctx: RunContext) -> Job:
        self.contexts.append(ctx)
        self.requests.append(req)
        if self._error is not None:
            raise self._error
        return self.new_job(WaitMode.INLINE, state=JobState.SUCCEEDED)

    def normalize(self, job: Job, ctx: RunContext, req: OpenReadingRequest) -> NormalizedResponse:
        # ctx deliberately not appended to self.contexts (Ledger T4b) — that list's own contract
        # (see the class docstring + every len(backend.contexts) assertion across the suite) is
        # "one entry per submit()", not "per adapter-method call"; normalize()'s ctx has no
        # observer here.
        if self._normalize_error is not None:
            raise self._normalize_error
        pages = self._pages
        if pages is None and self._confidence is not None:
            pages = [Page(page_number=1, text=self._text, confidence=self._confidence)]
        # honor a page-range request (granularity:page escalation): return only requested pages
        if pages is not None and req.pages and req.pages.ranges:
            wanted = {n for r in req.pages.ranges for n in range(r.start, (r.end or r.start) + 1)}
            pages = [p for p in pages if p.page_number in wanted]
        usage = Usage(cost_usd=self._cost_usd) if self._cost_usd is not None else None
        return NormalizedResponse(
            status=Status(state=ResponseState.SUCCEEDED),
            backend=BackendInfo(id=self.descriptor.id, type=self.descriptor.type),
            document=Document(text=self._text, pages=pages),
            typed_fields=self._typed_fields,
            usage=usage,
        )

    def report_cost(self, job: Job) -> CostReport:
        # BL-134: an adapter whose meter raises AFTER normalize() already set usage.cost_usd
        # (router/cost.py's own "an adapter meters a channel itself" pattern) — apply_cost_report's
        # own except clause degrades gracefully but never runs merge_cost_report, so cost_basis
        # stays unset. Lets a test reproduce a real, positive cost with no resolved basis.
        if self._report_cost_error is not None:
            raise self._report_cost_error
        return infra_only("page", 1.0)


def scripted_registry(*backends: ScriptedBackend):
    """A Registry populated with the given ScriptedBackends (for engine tests)."""
    from openreading.router.registry import Registry

    reg = Registry()
    for b in backends:
        reg.register(b)
    return reg
