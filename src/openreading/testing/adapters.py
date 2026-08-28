"""Reference adapters that PROVE the conformance kit (Phase 0.8) and serve as templates.

- NullAdapter: the degenerate backend — emits only document.text, declares every other
  channel X, and warns when a requested channel is X. Exercises the "never fabricate,
  always warn" path.
- FixtureAdapter: the record/replay pattern every hosted adapter follows — an INLINE job
  carrying a stored raw payload, normalized by a supplied factory. Used to drive real
  geometry (bbox in [0,1] + bbox_native) through the kit.
"""

from __future__ import annotations

from collections.abc import Callable

from openreading.adapters.base import BackendAdapter
from openreading.types.cost import CostReport, infra_only
from openreading.types.descriptor import (
    AdapterDescriptor,
    Capabilities,
    ComplianceProfile,
    Cost,
    Output,
    OutputChannels,
    Provisioning,
    RuntimeProfile,
)
from openreading.types.enums import (
    BackendType,
    ChannelGrade,
    CostBasis,
    JobState,
    ResponseState,
    WaitMode,
)
from openreading.types.job import Job
from openreading.types.request import OpenReadingRequest, Outputs
from openreading.types.response import (
    BackendInfo,
    Document,
    NormalizedResponse,
    Status,
)
from openreading.types.runtime import Health, RawResult, RunContext

X = ChannelGrade.IMPOSSIBLE
N = ChannelGrade.NATIVE


class NullAdapter(BackendAdapter):
    """Produces only document.text; every other channel is X and warned when requested."""

    def __init__(self, backend_id: str = "null") -> None:
        self.descriptor = AdapterDescriptor(
            id=backend_id,
            type=BackendType.OSS_LIBRARY,
            protocol_version=2,
            provisioning=Provisioning(byo_mode=["pip"], auth="none", billing_target="caller_infra"),
            wait_modes=[WaitMode.INLINE],
            capabilities=Capabilities(),
            cost=Cost(native_unit="cpu_second", basis="infra_only"),
            compliance=ComplianceProfile(
                hipaa_baa="na_local", trains_on_customer_data="na_local", runs_fully_local=True
            ),
            runtime=RuntimeProfile(
                offline_capable=True, license="Apache-2.0", sandbox="in_process"
            ),
            adapter_impl="in_process",
            output=Output(channels=OutputChannels(text=N)),  # everything else defaults to X
        )

    def health(self) -> Health:
        return Health(ready=True)

    def submit(self, req: OpenReadingRequest, ctx: RunContext) -> Job:
        job = self.new_job(WaitMode.INLINE, state=JobState.SUCCEEDED)
        job.raw = RawResult(payload="", media_type="text/plain", encoding="text")
        return job

    def normalize(
        self, job: Job, ctx: RunContext, slim_req: OpenReadingRequest
    ) -> NormalizedResponse:
        resp = NormalizedResponse(
            status=Status(state=ResponseState.SUCCEEDED),
            backend=BackendInfo(id=self.descriptor.id, type=self.descriptor.type),
            # non-empty: text is graded N, so under the Phase-C all-strict default it must be
            # delivered (C6) — a null adapter still produces *some* text.
            document=Document(text="null adapter document text"),
        )
        outputs = slim_req.outputs or Outputs()
        # warn for every requested-but-X channel (never a fake value)
        if outputs.markdown:
            resp.add_warning(
                "channel_unsupported", "markdown not produced by null backend", "markdown"
            )
        if outputs.blocks:
            resp.add_warning("channel_unsupported", "blocks not produced by null backend", "blocks")
        if outputs.typed_fields:
            resp.add_warning(
                "channel_unsupported", "typed_fields not produced by null backend", "typed_fields"
            )
        return resp

    def report_cost(self, job: Job) -> CostReport:
        return infra_only("cpu_second", 0.0)


class FixtureAdapter(BackendAdapter):
    """INLINE adapter that carries a stored raw payload and normalizes it via `factory`. The
    record/replay template for hosted adapters; also used to push real geometry through the kit."""

    def __init__(
        self,
        descriptor: AdapterDescriptor,
        factory: Callable[[OpenReadingRequest, RawResult | None], NormalizedResponse],
        raw: RawResult | None = None,
    ) -> None:
        self.descriptor = descriptor
        self._factory = factory
        self._raw = raw

    def health(self) -> Health:
        return Health(ready=True, version=self.descriptor.runtime.version_pin)

    def submit(self, req: OpenReadingRequest, ctx: RunContext) -> Job:
        job = self.new_job(WaitMode.INLINE, state=JobState.SUCCEEDED)
        job.raw = self._raw or RawResult(payload=None)
        return job

    def normalize(
        self, job: Job, ctx: RunContext, slim_req: OpenReadingRequest
    ) -> NormalizedResponse:
        return self._factory(slim_req, job.raw)

    def report_cost(self, job: Job) -> CostReport:
        target = self.descriptor.provisioning.billing_target
        if target == "caller_infra":
            return infra_only(self.descriptor.cost.native_unit, 1.0)
        return CostReport(
            native_unit=self.descriptor.cost.native_unit,
            native_quantity=1.0,
            cost_usd=self.descriptor.cost.usd_per_page_equiv_low,
            basis=CostBasis.ESTIMATED,
            billing_target="caller_account",
        )
