"""Leaderboard (BL-160): rank many backends over many documents.

`evals/` scores one adapter over many documents, and `comparison/` compares many backends on
one document. Neither covers the cell this module fills
(internal/product/specs/eval-leaderboard.product-spec.md, "Problem").

`run_leaderboard` runs the SAME dataset (the evals.dataset case.json shape) across N named,
registered backends through the UNCHANGED `evals.runner.run_dataset`/`run_case` path — same
per-case compliance gate, same five-dimension scorer, no second scoring or gating code path (AC-1)
— and ranks backends by measured mean score on that one dataset. Everything below is aggregation
over `DatasetReport`s `run_dataset` already produces; nothing here re-implements or re-decides
compliance or scoring.

Deliberately narrow: never touches `Router._score` / `_QUALITY_BY_PRIORITY` / any adapter's
`integration_priority` (AC-9); never persists a run store or trend history across invocations (one
call, one report, stateless, matching `compare`'s own posture). It never collects or ships a real
labeled corpus — the harness works on whatever dataset directory it is handed.
"""

from __future__ import annotations

from typing import Any

from openreading.evals.runner import DatasetReport, run_dataset
from openreading.router.compliance import RouterConfig
from openreading.router.registry import Registry
from openreading.types.leaderboard import (
    BenchmarkReport,
    LeaderboardBackend,
    LeaderboardCase,
    LeaderboardDataset,
)


def _headline(value: Any) -> float:
    """The SAME headline-value rule `scorers.score()` already applies when it collapses a case's
    per-dimension dict into `overall`: a dict-shaped dimension (field_prf) contributes its f1;
    every other recognized dimension is already a plain float. Reused verbatim, not
    reimplemented, so a per-dimension mean here can never silently diverge from what `overall`
    itself means for that same case."""
    return value["f1"] if isinstance(value, dict) else value


def _dimension_means(report: DatasetReport) -> dict[str, float]:
    """Per-dimension mean over the cases that actually exercised that dimension, excluding errored
    cases the exact way DatasetReport.mean_overall already excludes them — never a single blended
    cross-dimension number when different cases in the dataset exercise different `expected`
    dimensions."""
    buckets: dict[str, list[float]] = {}
    for r in report.results:
        if r.error is not None:
            continue
        for key, value in r.dimensions.items():
            buckets.setdefault(key, []).append(_headline(value))
    return {key: sum(values) / len(values) for key, values in buckets.items()}


def _n_scored(report: DatasetReport) -> int:
    return sum(1 for r in report.results if r.error is None and r.overall is not None)


def _case_score(report: DatasetReport, index: int) -> float | None:
    """This backend's overall for the case at `index` — None for BOTH an errored case and a
    genuinely-unscored one (never CaseResult's own internal 0.0 error placeholder, which would
    misread as a real, comparably-bad score rather than 'this backend failed here')."""
    r = report.results[index]
    return None if r.error is not None else r.overall


def run_leaderboard(
    dataset_dir: str,
    backend_ids: list[str],
    registry: Registry,
    *,
    policy=None,
    router_config: RouterConfig | None = None,
) -> BenchmarkReport:
    """Run every case in `dataset_dir` against every named backend through the unchanged
    `evals.runner.run_dataset` path (itself just `run_case` per case — AC-1/AC-2's compliance gate
    and AC-3's honest-unscored convention carry over unmodified), and rank backends by
    `DatasetReport.mean_overall` on this one dataset.

    Raises `ValueError` for fewer than two backends or a backend id `registry` doesn't know
    (mirroring `strategies.calibrate.calibrate_strategy`'s identical `rung1_backend` check); a
    per-case backend fault (missing credentials, a real submit/poll/normalize failure, a
    compliance refusal) never raises here — it is scored, per `run_case`'s own existing contract.
    """
    # Lazy (function-local), not module-level: comparison.report and strategies.calibrate each sit
    # behind an import chain that reaches back into evals.* at THEIR OWN module-load time (e.g.
    # comparison.blocks imports evals.scorers, which loads the evals package __init__, which now
    # exports run_leaderboard) — a module-level import here would deadlock that cycle. Every other
    # cross-package pull in this file already happens to be cycle-safe at module level; these two
    # are the only ones that aren't, so only these two are deferred.
    from openreading.comparison.report import _NON_DETERMINISTIC

    if len(backend_ids) < 2:
        raise ValueError(f"leaderboard needs at least two backends (got {len(backend_ids)})")

    adapters = {}
    reports: dict[str, DatasetReport] = {}
    for bid in backend_ids:
        adapter = registry.get(bid)
        if adapter is None:
            raise ValueError(f"backend {bid!r} is not registered")
        adapters[bid] = adapter
        reports[bid] = run_dataset(adapter, dataset_dir, policy=policy, router_config=router_config)

    # Dataset identity (AC-5) from the first backend's own case list: evals.dataset.load_case
    # bakes `backend_id` into request_body.backend.id, but name/expected/input never vary by
    # backend, and load_dataset's sorted */case.json glob is independent of which backend_id it
    # was called with — so every backend's run_dataset call loads the identical case sequence.
    first = reports[backend_ids[0]]
    case_names = [r.name for r in first.results]
    for bid, backend_report in reports.items():
        names = [r.name for r in backend_report.results]
        if names != case_names:
            raise ValueError(
                f"backend {bid!r} scored a different case sequence than {backend_ids[0]!r} "
                f"({names} != {case_names}) — every named backend must run the same dataset_dir"
            )

    dataset = LeaderboardDataset(
        path=str(dataset_dir), case_count=len(case_names), case_names=case_names
    )

    # Rank BEFORE constructing LeaderboardBackend (rank is schema/pydantic-required >=1 — there is
    # no valid placeholder to construct-then-patch). Best-first; ties break on backend_id so a
    # rerun is byte-identical (AC-8) rather than dependent on Python's sort stability over whatever
    # order backend_ids happened to list them in.
    ranked_ids = sorted(backend_ids, key=lambda bid: (-reports[bid].mean_overall, bid))

    backends = [
        LeaderboardBackend(
            backend_id=bid,
            rank=rank,
            mean_score=reports[bid].mean_overall,
            n_cases=len(reports[bid].results),
            n_scored=_n_scored(reports[bid]),
            errors=reports[bid].errors,
            non_deterministic=bid in _NON_DETERMINISTIC,
            dimensions=_dimension_means(reports[bid]),
        )
        for rank, bid in enumerate(ranked_ids, start=1)
    ]

    cases = []
    for i, name in enumerate(case_names):
        scores = {bid: _case_score(reports[bid], i) for bid in backend_ids}
        real = {bid: s for bid, s in scores.items() if s is not None}
        # highest score wins; a tie breaks alphabetically on backend_id — deterministic either way.
        winner = min(real, key=lambda bid: (-real[bid], bid)) if real else None
        cases.append(LeaderboardCase(name=name, winner=winner, scores=scores))

    report = BenchmarkReport(dataset=dataset, backends=backends, cases=cases)
    from openreading import schemas

    schemas.validate_leaderboard_report(report.to_schema_dict())
    return report
