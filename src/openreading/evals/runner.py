"""Eval runner: drive an adapter over a dataset and score each normalized response. Works for any
adapter (via the router driver, so INLINE and POLL both work); scoring is uniform because every
adapter returns the same NormalizedResponse schema — the whole point of OpenReading.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from openreading.config import apply as apply_policy
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
    """One scored case. `error` set means the backend failed and `overall` is a placeholder 0.0.
    `overall` is None when the case's `expected` named no recognized dimension, which reads as
    unscored and never as zero."""

    name: str
    overall: float | None  # None = unscored: case.expected named no recognized dimension (BL-86)
    dimensions: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class DatasetReport:
    """One backend's results over one dataset. `mean_overall` excludes errored and unscored
    cases, so neither drags the mean toward zero."""

    backend_id: str
    results: list[CaseResult]

    @property
    def mean_overall(self) -> float:
        # exclude unscored cases (overall is None, BL-79's honest "not labeled yet" marker) from
        # both the numerator and denominator, the same move already applied to calibrate.py's
        # Observation.scorer_overall — an unscored case must never silently read as a 0 or drag
        # the mean, only a genuinely-scored one contributes (BL-86).
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
    policy=None,
    router_config: RouterConfig | None = None,
) -> CaseResult:
    """Run one case against `adapter` and score the normalized response.

    The compliance gate runs before `submit()` (BL-121), because nothing else on this path checks
    `req.compliance` against the adapter's descriptor. Any exception, `ComplianceRefused`
    included, comes back as `CaseResult(error=...)` with `overall=0.0`. A case whose `expected`
    names no recognized dimension scores `overall=None`, which means unscored."""
    try:
        req = OpenReadingRequest.model_validate(case.request_body)
        # The operator's `policy:` block gates a measurement exactly as it gates a run (law PF6).
        # Passing only the three attestations, as this path used to, applies the keys that WIDEN
        # the eligible set while dropping the five requirements they qualify — the one combination
        # that is always wrong. `config.apply` folds both halves together, so a leaderboard cannot
        # rank a backend the same file would refuse to run.
        req, router_config = apply_policy(req, policy, router_config or RouterConfig())
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
    policy=None,
    router_config: RouterConfig | None = None,
) -> DatasetReport:
    """Load every `*/case.json` under `dataset_dir` for this adapter and score each one. Returns
    one `DatasetReport` whose `mean_overall` excludes errored and unscored cases."""
    cases = load_dataset(dataset_dir, backend_id=adapter.descriptor.id)
    return DatasetReport(
        backend_id=adapter.descriptor.id,
        results=[
            run_case(adapter, c, ctx, policy=policy, router_config=router_config) for c in cases
        ],
    )
