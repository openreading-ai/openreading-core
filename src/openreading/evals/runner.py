"""Eval runner: drive an adapter over a dataset and score each normalized response. Works for any
adapter (via the router driver, so INLINE and POLL both work); scoring is uniform because every
adapter returns the same NormalizedResponse schema — the whole point of OpenReading.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from openreading.credentials import build_run_context
from openreading.evals import scorers
from openreading.evals.dataset import EvalCase, load_dataset
from openreading.ledger.header import slim_request
from openreading.readiness import auth_hinted
from openreading.router import compliance as comp
from openreading.router.clock import RealClock
from openreading.router.compliance import RouterConfig
from openreading.router.driver import run_to_completion
from openreading.types.errors import ComplianceRefused
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RunContext


@dataclass
class CaseResult:
    name: str
    overall: float | None  # None = unscored: case.expected named no recognized dimension (BL-86)
    dimensions: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class DatasetReport:
    backend_id: str
    results: list[CaseResult]

    @property
    def mean_overall(self) -> float:
        # exclude unscored cases (overall is None, BL-79's honest "not labeled yet" marker) from
        # both the numerator and denominator, the same move already applied to calibrate.py's
        # Observation.scorer_overall — an unscored case must never silently read as a 0 or drag
        # the mean, only a genuinely-scored one contributes (BL-86/Jin).
        scored = [r.overall for r in self.results if r.error is None and r.overall is not None]
        return sum(scored) / len(scored) if scored else 0.0

    @property
    def errors(self) -> int:
        return sum(1 for r in self.results if r.error is not None)

    def summary(self) -> str:
        lines = [
            f"backend={self.backend_id}  mean_overall={self.mean_overall:.3f}  "
            f"cases={len(self.results)}  errors={self.errors}"
        ]
        for r in self.results:
            if r.error:
                lines.append(f"  {r.name}: ERROR {r.error}")
            else:
                dims = " ".join(
                    f"{k}={v['f1']:.2f}" if isinstance(v, dict) else f"{k}={v:.2f}"
                    for k, v in r.dimensions.items()
                )
                overall_str = "unscored" if r.overall is None else f"{r.overall:.3f}"
                lines.append(f"  {r.name}: overall={overall_str}  {dims}")
        return "\n".join(lines)


def run_case(
    adapter,
    case: EvalCase,
    ctx: RunContext | None = None,
    *,
    deadline_ms: float = 60_000,
    router_config: RouterConfig | None = None,
) -> CaseResult:
    try:
        req = OpenReadingRequest.model_validate(case.request_body)
        run_ctx = ctx or build_run_context(req, adapter.descriptor)
        clock = RealClock()
        # Compliance gate (BL-121), mirroring calibrate_strategy's identical BL-112 fix: run_case
        # drives adapter.submit() directly, with no Router in front of it to apply the stage-1
        # hard-filter, so req.compliance vs. this adapter's descriptor is never checked otherwise.
        # Gate BEFORE submit() (AGENTS.md: compliance is never relaxed by fallback); the raised
        # ComplianceRefused is an AdapterError, so the except clause below turns it into a scored,
        # honest CaseResult(error=...) exactly like any other adapter failure — no new control-flow.
        dr = comp.evaluate(req.compliance, adapter.descriptor, router_config)
        if dr is not None:
            raise ComplianceRefused(dr.detail, constraint=dr.code)
        with auth_hinted(adapter.descriptor, run_ctx.credentials):
            job = adapter.submit(req, run_ctx)
            job = run_to_completion(
                adapter, job, ctx=run_ctx, deadline_ms=clock.now_ms() + deadline_ms, clock=clock
            )
            resp = adapter.normalize(job, run_ctx, slim_request(req))
        s = scorers.score(resp.to_schema_dict(), case.expected)
        return CaseResult(name=case.name, overall=s["overall"], dimensions=s["dimensions"])
    except Exception as e:  # noqa: BLE001 - a failing case is a scored outcome, not a crash
        return CaseResult(name=case.name, overall=0.0, error=f"{type(e).__name__}: {e}")


def run_dataset(
    adapter,
    dataset_dir,
    ctx: RunContext | None = None,
    *,
    router_config: RouterConfig | None = None,
) -> DatasetReport:
    cases = load_dataset(dataset_dir, backend_id=adapter.descriptor.id)
    return DatasetReport(
        backend_id=adapter.descriptor.id,
        results=[run_case(adapter, c, ctx, router_config=router_config) for c in cases],
    )
