"""Read a finished benchmark run and say who won, in the terminal.

The publishers write HTML dashboards and a tree of JSON. Both are thorough and neither answers the
question the run was started to answer, which is whether one target beat another. Before this
module the answer lived in a browser or in a script the reader had to write, and a benchmark whose
result needs a script is a benchmark nobody checks twice.

Nothing here scores anything. Every number is read back from the publisher's own
``_evaluation_report.json`` and ``_metadata.json``, so the terminal table and the publisher's
dashboard cannot disagree. This module only joins those numbers to the target that produced them.

That join needs one fact the publisher does not record. Its metadata names the pipeline
(``openreading_backend_pymupdf_4d8801dee2``) and never the target (``backend:pymupdf``), and the
pipeline name is a lossy, hashed rendering that cannot be reversed. So ``benchmark run`` writes
``openreading-run.json`` beside the publisher's artifacts, mapping one to the other. Reading a
directory without that manifest still works and falls back to pipeline names.

The headline metric is named in its own column rather than assumed. ParseBench reports a rule pass
rate and ExtractBench reports value F1, so a single hardcoded column header would silently show
two different measurements under one name.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MANIFEST = "openreading-run.json"

# The category name used when the publisher reported no per-category breakdown, which is what
# a single-category corpus produces. Named rather than left blank so a reader can tell "one
# undivided run" from "a category the publisher happened to call nothing".
ALL_DOCUMENTS = "all documents"

# Checked in order. The first one a publisher's aggregate block carries becomes that row's
# headline, and its name is printed, so no row ever shows one metric under another's heading.
_HEADLINE_METRICS = (
    "avg_rule_pass_rate",  # ParseBench text, layout and chart categories
    "avg_grits_trm_composite",  # ParseBench table category, which reports no rule pass rate
    "avg_unified_value_f1",  # ExtractBench values
    "avg_value_f1",
    "avg_f1",
    "avg_accuracy",
)


class ReportError(ValueError):
    """Raised when a directory holds no publisher run this module can read."""


@dataclass(frozen=True)
class CategoryScore:
    """One publisher evaluation category, as that publisher scored it."""

    name: str
    metric: str
    score: float | None
    passed: float | None
    evaluated: float | None
    examples: int


@dataclass(frozen=True)
class TargetReport:
    """One target's run: what it parsed, and how the publisher graded it."""

    reference: str
    pipeline_name: str
    documents: int
    succeeded: int
    failed: int
    avg_latency_ms: float | None
    categories: tuple[CategoryScore, ...]

    @property
    def overall(self) -> float | None:
        """Mean of the categories that produced a score, or None when none did.

        Averaging the publisher's own per-category numbers is a summary, not a rescoring. It is
        unweighted because the categories hold different numbers of documents and this is a
        reading aid, not a ranking anyone should defend on its own.
        """

        scored = [c.score for c in self.categories if c.score is not None]
        return sum(scored) / len(scored) if scored else None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _score(report: Path, name: str) -> CategoryScore:
    payload = _read_json(report)
    aggregate = payload.get("aggregate_metrics") or {}
    metric = next((key for key in _HEADLINE_METRICS if key in aggregate), "")
    raw = aggregate.get(metric) if metric else None
    return CategoryScore(
        name=name,
        metric=metric.removeprefix("avg_") if metric else "none reported",
        score=float(raw) if isinstance(raw, (int, float)) else None,
        passed=aggregate.get("total_rule_pass_rate_passed"),
        evaluated=aggregate.get("total_rule_pass_rate_evaluated"),
        examples=int(payload.get("total_examples") or 0),
    )


def _categories(pipeline_dir: Path) -> tuple[CategoryScore, ...]:
    scores = [
        _score(report, report.parent.name)
        for report in sorted(pipeline_dir.glob("*/_evaluation_report.json"))
    ]
    # A corpus with ONE category writes its report at the pipeline root rather than under a
    # category directory, which the glob above cannot see. Measured against ParseBench 1.0.2: a
    # multi-category run writes only per-category reports, so reading the root as well adds a row
    # exactly when there would otherwise be none, and never double counts.
    root_report = pipeline_dir / "_evaluation_report.json"
    if not scores and root_report.is_file():
        scores.append(_score(root_report, ALL_DOCUMENTS))
    return tuple(scores)


def read_run(output_dir: str | Path) -> tuple[TargetReport, ...]:
    """Read every target's publisher artifacts under one output directory."""

    root = Path(output_dir)
    if not root.is_dir():
        raise ReportError(f"{root} is not a directory; run `openreading benchmark run` first")
    manifest = _read_json(root / MANIFEST)
    references = {
        entry.get("pipeline_name"): entry.get("reference")
        for entry in manifest.get("targets") or []
    }
    reports: list[TargetReport] = []
    for pipeline_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        metadata = _read_json(pipeline_dir / "_metadata.json")
        summary = metadata.get("summary") or {}
        if not summary and not (pipeline_dir / "_summary.json").exists():
            continue
        summary = summary or _read_json(pipeline_dir / "_summary.json")
        latency = summary.get("avg_latency_ms")
        reports.append(
            TargetReport(
                # A directory with no manifest entry still reports, under the publisher's own
                # pipeline name. Refusing to show a result because our sidecar is missing would
                # hide a run the publisher completed.
                reference=references.get(pipeline_dir.name) or pipeline_dir.name,
                pipeline_name=pipeline_dir.name,
                documents=int(summary.get("total") or 0),
                succeeded=int(summary.get("successful") or 0),
                failed=int(summary.get("failed") or 0),
                avg_latency_ms=float(latency) if isinstance(latency, (int, float)) else None,
                categories=_categories(pipeline_dir),
            )
        )
    if not reports:
        raise ReportError(f"no publisher run found under {root}")
    return tuple(reports)


def to_json(reports: tuple[TargetReport, ...]) -> dict[str, Any]:
    """The same numbers as a plain object, for a script that wants to branch on them."""

    return {
        "targets": [
            {
                "reference": report.reference,
                "pipeline_name": report.pipeline_name,
                "documents": report.documents,
                "succeeded": report.succeeded,
                "failed": report.failed,
                "avg_latency_ms": report.avg_latency_ms,
                "overall": report.overall,
                "categories": [
                    {
                        "name": category.name,
                        "metric": category.metric,
                        "score": category.score,
                        "passed": category.passed,
                        "evaluated": category.evaluated,
                        "examples": category.examples,
                    }
                    for category in report.categories
                ],
            }
            for report in reports
        ]
    }


def render(reports: tuple[TargetReport, ...]) -> str:
    """The comparison table, ranked by the publisher's own numbers."""

    ranked = sorted(reports, key=lambda r: (r.overall is None, -(r.overall or 0.0), r.reference))
    lines = [f"{'target / category':<34}{'score':>7}{'parsed':>9}{'errors':>8}  metric"]
    for report in ranked:
        # An unscored row prints as an em dash rather than a number, the same way `leaderboard`
        # does, so a category that never ran never looks like one that ran and scored zero.
        overall = f"{report.overall:.3f}" if report.overall is not None else "—"
        parsed = f"{report.succeeded}/{report.documents}"
        lines.append(f"{report.reference:<34}{overall:>7}{parsed:>9}{report.failed:>8}")
        for category in report.categories:
            score = f"{category.score:.3f}" if category.score is not None else "—"
            rules = (
                f" ({category.passed:.0f}/{category.evaluated:.0f} rules)"
                if category.evaluated
                else ""
            )
            lines.append(f"  {category.name:<32}{score:>7}{'':>17}  {category.metric}{rules}")
    lines.append("")
    lines.append("scored by the publisher, read back here. A category scoring 0.000 may be a")
    lines.append("channel the backend never claimed rather than a failure. Read its report.")
    return "\n".join(lines)


def write_manifest(
    output_dir: str | Path,
    *,
    benchmark_id: str,
    preset: str,
    documents: tuple[str, ...],
    targets: tuple[tuple[str, str], ...],
) -> Path:
    """Record which target produced which publisher pipeline, next to the artifacts.

    The publisher's metadata names the pipeline and never the target, and the pipeline name is a
    hashed rendering that cannot be reversed, so without this a later reader cannot say which row
    was which backend.
    """

    path = Path(output_dir) / MANIFEST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "benchmark": benchmark_id,
                "preset": preset,
                "documents": list(documents),
                "targets": [
                    {"reference": reference, "pipeline_name": pipeline}
                    for reference, pipeline in targets
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path
