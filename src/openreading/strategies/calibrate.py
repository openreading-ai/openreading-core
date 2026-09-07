"""`openreading calibrate` — derive gate thresholds from a labeled sample (signals.md §5).

Raw thresholds are meaningless to users; the usable knob is an **escalation rate**, the fraction
of a sample that would fall through to the next rung. Principle (signals.md §5, spec §4.4): *users
pick rates; tools derive thresholds.* This module used to report a `cost_per_doc` per operating
point and accept a `--max-cost-per-doc` budget filter. Both multiplied the escalation rate by a
price read off `descriptor.cost`, a number this package wrote down about someone else's rate card
and could not verify, and the escalation rate it multiplied was the only measured half
. The rate is what calibration now reports, and it is the figure a
caller multiplies by their own invoice. This module runs the strategy's rung-1 backend over a labeled sample, scores each
result with the EXISTING eval scorers (`openreading.evals.scorers` — no parallel scoring path),
computes the engine's own signal probe per document, sweeps each gated threshold over its domain,
and reports candidate operating points (threshold → predicted escalation rate, scorer agreement)
plus the recommended point as a ready-to-paste `escalate_if:` block. **It
proposes; it never rewrites the user's config** — the file the user commits is the authority.

Two layers: `sweep_predicate` is a PURE function over pre-computed observations (unit-testable
without running any backend); `calibrate_strategy` is the integration that produces those
observations from the dataset. v0.3 sweeps each gated predicate independently over its domain (a
full threshold-vector cartesian product is a documented follow-up, D-v3-21)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# gate operator -> (signal field on the probe snapshot, fire direction). Only the numeric,
# sweepable predicates appear here; boolean/regex gates are not threshold-calibrated.
_PREDICATE_SIGNAL: dict[str, tuple[str, str]] = {
    "confidence_below": ("doc_confidence", "below"),
    "page_confidence_below": ("doc_confidence", "below"),
    "chars_per_page_below": ("chars_per_page", "below"),
    "table_sanity_below": ("table_sanity", "below"),
    "empty_pages_over": ("empty_pages_fraction", "over"),
    "garble_score_over": ("garble_score", "over"),
}


def calibratable_predicates(gate: dict[str, Any] | None) -> list[str]:
    """The subset of a gate's predicate keys that `calibrate` can sweep (numeric thresholds)."""
    if not isinstance(gate, dict):
        return []
    return [k for k in gate if k in _PREDICATE_SIGNAL]


def _domain(predicate: str) -> list[float]:
    """The threshold grid swept for a predicate — evenly spaced over its natural domain."""
    field_name, _ = _PREDICATE_SIGNAL[predicate]
    if field_name in ("doc_confidence", "table_sanity", "empty_pages_fraction", "garble_score"):
        return [round(i / 20, 2) for i in range(21)]  # [0.00 .. 1.00] step 0.05
    # chars_per_page — 0 .. 3000 chars, step 100
    return [float(i) for i in range(0, 3001, 100)]


def _fires(value: float | None, threshold: float, direction: str) -> bool:
    """Whether the predicate fires — an unavailable signal never fires (missing-signal law, §6)."""
    if value is None:
        return False
    return value < threshold if direction == "below" else value > threshold


@dataclass
class Observation:
    """One document after the rung-1 run: its probe signals + the eval scorer's overall. Despite
    the name, not every document is actually labeled: `scorer_overall` is `None` when its
    `expected` names none of the scorer's five dimensions (an ordinary "not labeled yet" shape,
    never a claim of perfection — BL-79); `sweep_predicate` excludes those observations from
    `scorer_agreement` entirely rather than reading the absent label as agreement."""

    name: str
    signals: dict[str, float | None]  # probe field -> value
    scorer_overall: float | None  # from openreading.evals.scorers.score; None = unscored (BL-79)


@dataclass
class OperatingPoint:
    threshold: float
    escalation_rate: float  # fraction of the sample that would escalate at this threshold
    scorer_agreement: float  # fraction where the gate decision matches the scorer's verdict

    def as_dict(self) -> dict[str, Any]:
        return {
            "threshold": self.threshold,
            "escalation_rate": round(self.escalation_rate, 4),
            "scorer_agreement": round(self.scorer_agreement, 4),
        }


@dataclass
class PredicateSweep:
    predicate: str
    points: list[OperatingPoint]
    recommended: OperatingPoint | None


@dataclass
class CalibrationReport:
    strategy: str
    n_docs: int
    n_scored: int  # of n_docs, how many carried a recognized `expected` dimension (BL-87);
    # scorer_agreement is grounded in this subset only — see sweep_predicate's own docstring
    rung1_backend: str
    rung2_backend: str | None
    sweeps: list[PredicateSweep] = field(default_factory=list)
    target_escalation: float | None = None

    def recommended_block(self) -> dict[str, Any]:
        """The ready-to-paste `escalate_if:` block (a RECOMMENDATION — the user commits the file)."""
        gate: dict[str, Any] = {}
        for s in self.sweeps:
            if s.recommended is not None:
                gate[s.predicate] = s.recommended.threshold
        return {"escalate_if": gate} if gate else {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "n_docs": self.n_docs,
            "n_scored": self.n_scored,
            "rung1_backend": self.rung1_backend,
            "rung2_backend": self.rung2_backend,
            "target_escalation": self.target_escalation,
            "sweeps": [
                {
                    "predicate": s.predicate,
                    "recommended": s.recommended.as_dict() if s.recommended else None,
                    "points": [p.as_dict() for p in s.points],
                }
                for s in self.sweeps
            ],
            "recommended": self.recommended_block(),
        }


def _recommend(
    points: list[OperatingPoint],
    target_escalation: float | None,
) -> OperatingPoint | None:
    """Pick the operating point that best meets the user's target (signals.md §5: users pick rates,
    tools derive thresholds). With a target, get closest to it (ties → higher scorer agreement →
    lower threshold); with no target, maximize scorer agreement, then prefer the lower escalation
    rate, then the higher threshold. Deterministic."""
    if not points:
        return None
    survivors = points
    if target_escalation is not None:
        return min(
            survivors,
            key=lambda p: (
                abs(p.escalation_rate - target_escalation),
                -p.scorer_agreement,
                p.threshold,
            ),
        )
    return max(survivors, key=lambda p: (p.scorer_agreement, -p.escalation_rate, -p.threshold))


def sweep_predicate(
    observations: list[Observation],
    predicate: str,
    *,
    target_escalation: float | None = None,
    quality_bar: float = 0.8,
) -> PredicateSweep:
    """PURE sweep of one gated predicate over its domain — no backend involved. `quality_bar` labels
    a document as "should escalate" when the eval scorer's overall is below it; scorer_agreement is
    how often the gate's fire decision matches that label (this grounds the threshold in measured
    quality — the FrugalGPT structure, signals.md §5). An observation with `scorer_overall is None`
    (no recognized `expected` dimension — an ordinary "not labeled yet" shape, not a claim of
    perfection) carries no quality label at all, so it is excluded from `scorer_agreement`'s
    numerator AND denominator entirely, rather than silently reading as "agrees with every
    threshold" (BL-79). It still counts toward `escalation_rate`, which is signal-only and needs
    no label."""
    signal_field, direction = _PREDICATE_SIGNAL[predicate]
    n = len(observations)
    needs = [o.scorer_overall < quality_bar for o in observations if o.scorer_overall is not None]
    n_scored = len(needs)
    points: list[OperatingPoint] = []
    for t in _domain(predicate):
        fires = [_fires(o.signals.get(signal_field), t, direction) for o in observations]
        esc = sum(fires) / n if n else 0.0
        scored_fires = [
            f for f, o in zip(fires, observations, strict=True) if o.scorer_overall is not None
        ]
        agree = (
            sum(1 for f, nd in zip(scored_fires, needs, strict=True) if f == nd) / n_scored
            if n_scored
            else 0.0
        )
        points.append(OperatingPoint(t, esc, agree))
    return PredicateSweep(predicate, points, _recommend(points, target_escalation))


def calibrate_strategy(
    dataset_dir: str,
    config: Any,
    strategy_name: str,
    registry: Any,
    *,
    target_escalation: float | None = None,
    quality_bar: float = 0.8,
    router_config: Any = None,
) -> CalibrationReport:
    """Run the strategy's rung-1 backend over the labeled sample, score + probe each result, and
    sweep every gated numeric predicate on rung 1. Fully offline for local backends. Never mutates
    `config`.

    Compliance (BL-112): the strategy file's own `policy:` block is folded into effective
    compliance and RouterConfig exactly the way `compile_strategy` does for every other
    strategy-engaged surface (`config.apply`/`merge_router_config`, reused not
    reimplemented), and the rung-1 backend is gated PER CASE, before `adapter.submit()`, via
    `Router.check_eligible` — never gated once for the whole sample, since each case is loaded
    from its own independent file and can carry its own `compliance` block."""
    import base64

    from openreading.config import apply as apply_policy
    from openreading.config import merge_router_config
    from openreading.credentials import build_run_context
    from openreading.evals import scorers
    from openreading.evals.dataset import load_dataset
    from openreading.ledger.header import slim_request
    from openreading.readiness import auth_hinted
    from openreading.router.clock import RealClock
    from openreading.router.driver import run_to_completion
    from openreading.router.router import Router, RouterConfig
    from openreading.strategies.normalize import normalize_strategy
    from openreading.strategies.signals import probe
    from openreading.types.errors import AdapterError, TerminalError
    from openreading.types.request import OpenReadingRequest

    tree = normalize_strategy(strategy_name, config)
    steps = tree.get("steps") if isinstance(tree, dict) else None
    if not steps:
        raise ValueError(f"strategy {strategy_name!r} is not a cascade — nothing to calibrate")
    rung1 = steps[0]
    gate = rung1.get("escalate_if") if isinstance(rung1, dict) else None
    predicates = calibratable_predicates(gate)
    if isinstance(rung1, dict) and "backend" not in rung1:
        # A `compare:` or `race:` step compiles to a node keyed `parallel`, which runs several
        # backends and names none. Calibration sweeps ONE rung-1 backend's scores, so there is
        # nothing here to sweep. Say so the way the not-a-cascade case does, rather than reading
        # a key that is not there and handing the reader a KeyError.
        raise ValueError(
            f"strategy {strategy_name!r} starts with a parallel step, which runs several"
            " backends at once. Calibration sweeps one rung-1 backend, so start the strategy"
            " with a single backend, or calibrate a cascade that does"
        )
    rung1_backend = rung1["backend"] if isinstance(rung1, dict) else str(rung1)
    rung2_backend = (
        (steps[1].get("backend") if isinstance(steps[1], dict) else str(steps[1]))
        if len(steps) > 1
        else None
    )

    a1 = registry.get(rung1_backend)
    if a1 is None:
        raise ValueError(f"rung-1 backend {rung1_backend!r} is not registered")
    # (BL-112) fold the strategy file's own `policy:` block into the RouterConfig once — it is a
    # deployment-wide setting that does not vary per case, the same fold compile_strategy performs
    # before every route(). The compliance side is folded PER CASE below (not here), because
    # load_dataset can yield a distinct `compliance` block per case once evals/dataset.py's
    # load_case forwards case.json's own `compliance` key.
    config_policy = getattr(config, "policy", None)
    merged_router_config = merge_router_config(router_config or RouterConfig(), config_policy)
    router = Router(registry, merged_router_config)

    observations: list[Observation] = []
    cases = load_dataset(dataset_dir, backend_id=rung1_backend)
    for i, case in enumerate(cases):
        req = OpenReadingRequest.model_validate(case.request_body)
        # The shared fold, not a local rebuild of it. Assembling the compliance half by hand here
        # meant the file's own policy reached every other surface and not this one, and any
        # key added later would have reached this one last. `merged_router_config` above already
        # carries the attestations, so this call is idempotent over them.
        req, _ = apply_policy(req, config_policy, merged_router_config)
        clock = RealClock()
        # Per-case isolation (BL-107): this block used to have no fault handling at all — a
        # RetryableError (rate-limit exhaustion, or the hard-coded 60s per-case deadline tripping,
        # router/driver.py's _DriveSliceExpired), an UnsupportedFeatureError, or a plain crash out
        # of normalize() (the exact class BL-99 hardened five other call sites against but never
        # reached here) all propagated straight out, discarding every Observation computed before
        # it — including real, already-paid-for backend calls. Unlike evals/runner.py's run_case,
        # whose per-case score is meaningful even in isolation, a threshold sweep IS the aggregate
        # over the whole sample, so silently continuing over a materially incomplete one would be
        # misleading, not merely degraded — this fails fast instead. Every failure names which case
        # and how many cases were already scored, so the already-paid-for calls are never silently
        # unaccounted for; an AdapterError (RetryableError/TerminalError/UnsupportedFeatureError/
        # ScopeRefused) keeps its own type — cmd_calibrate maps each to a clean, coded exit —
        # gaining only the case context submit()/normalize() themselves can't know about.
        try:
            # Gate BEFORE submit() — matching compile_strategy's own behavior for the identical
            # policy + backend pair (AGENTS.md: compliance is never relaxed by fallback). Raises
            # ScopeRefused, which cmd_calibrate already knows how to turn into a clean exit.
            # check_eligible's only other possible exception (KeyError, unregistered backend) is
            # already unreachable here — rung1_backend was already resolved via registry.get()
            # above, before the loop.
            router.check_eligible(req, rung1_backend)
            rc = build_run_context(req, a1.descriptor)
            with auth_hinted(a1.descriptor, rc.credentials):
                job = run_to_completion(
                    a1, a1.submit(req, rc), ctx=rc, deadline_ms=clock.now_ms() + 60_000, clock=clock
                )
                resp = a1.normalize(job, rc, slim_request(req))
        except AdapterError as e:
            e.message = (
                f"case {case.name!r} ({i + 1}/{len(cases)}) failed after {len(observations)} "
                f"case(s) already scored: {e.message}"
            )
            e.args = (e.message,)
            raise
        except Exception as e:
            raise TerminalError(
                f"case {case.name!r} ({i + 1}/{len(cases)}) failed after {len(observations)} "
                f"case(s) already scored: {type(e).__name__}: {e}"
            ) from e
        doc_bytes = (
            base64.b64decode(req.document.bytes_base64) if req.document.bytes_base64 else None
        )
        snap = probe(resp, doc_bytes=doc_bytes, mime_type=req.document.mime_type)
        s = scorers.score(resp.to_schema_dict(), case.expected)  # REUSE the eval scorers
        overall = s["overall"]  # None when case.expected names no recognized dimension (BL-79)
        observations.append(
            Observation(
                name=case.name,
                signals={
                    "doc_confidence": snap.doc_confidence,
                    "chars_per_page": snap.chars_per_page,
                    "table_sanity": snap.table_sanity,
                    "empty_pages_fraction": snap.empty_pages_fraction,
                    "garble_score": snap.garble_score,
                },
                scorer_overall=None if overall is None else float(overall),
            )
        )

    sweeps = [
        sweep_predicate(
            observations,
            p,
            target_escalation=target_escalation,
            quality_bar=quality_bar,
        )
        for p in predicates
    ]
    return CalibrationReport(
        strategy=strategy_name,
        n_docs=len(observations),
        n_scored=sum(1 for o in observations if o.scorer_overall is not None),
        rung1_backend=rung1_backend,
        rung2_backend=rung2_backend,
        sweeps=sweeps,
        target_escalation=target_escalation,
    )
