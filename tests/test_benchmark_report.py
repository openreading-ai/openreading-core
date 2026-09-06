"""Reading a finished publisher run back into a terminal table.

The numbers here are never computed, only joined. What these lock down is the join: which target
produced which pipeline, which publisher metric each category reported, and that a category the
publisher could not score prints as unscored rather than as zero.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openreading.evals.report import (
    ALL_DOCUMENTS,
    MANIFEST,
    ReportError,
    read_run,
    render,
    to_json,
    write_manifest,
)


def _publisher_run(root: Path, pipeline: str, *, categories: dict[str, dict]) -> None:
    """A finished publisher run, in the shape ParseBench leaves on disk."""
    directory = root / pipeline
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "_metadata.json").write_text(
        json.dumps(
            {
                "pipeline": {"pipeline_name": pipeline, "product_type": "parse"},
                "summary": {"total": 2, "successful": 2, "failed": 0, "avg_latency_ms": 43.5},
            }
        ),
        encoding="utf-8",
    )
    for name, aggregate in categories.items():
        (directory / name).mkdir(parents=True, exist_ok=True)
        (directory / name / "_evaluation_report.json").write_text(
            json.dumps({"total_examples": 1, "aggregate_metrics": aggregate}), encoding="utf-8"
        )


def test_manifest_joins_a_pipeline_back_to_its_target(tmp_path) -> None:
    _publisher_run(tmp_path, "openreading_backend_pymupdf_abc", categories={})
    write_manifest(
        tmp_path,
        benchmark_id="parsebench",
        preset="smoke",
        documents=("table/doc0",),
        targets=(("backend:pymupdf", "openreading_backend_pymupdf_abc"),),
    )

    reports = read_run(tmp_path)

    # The publisher's own metadata never records "backend:pymupdf", and the pipeline name is a
    # hashed rendering that cannot be reversed, so without the manifest the row is unattributable.
    assert reports[0].reference == "backend:pymupdf"
    assert json.loads((tmp_path / MANIFEST).read_text())["documents"] == ["table/doc0"]


def test_a_run_without_a_manifest_still_reports(tmp_path) -> None:
    _publisher_run(tmp_path, "some_other_pipeline", categories={})

    reports = read_run(tmp_path)

    # Hiding a completed publisher run because our sidecar is missing would lose real results.
    assert reports[0].reference == "some_other_pipeline"


def test_each_category_names_the_metric_it_reported(tmp_path) -> None:
    _publisher_run(
        tmp_path,
        "pipe",
        categories={
            "text_content": {
                "avg_rule_pass_rate": 0.874,
                "total_rule_pass_rate_passed": 80.0,
                "total_rule_pass_rate_evaluated": 95.0,
            },
            # ParseBench's table category reports no rule pass rate at all.
            "table": {"avg_grits_trm_composite": 0.5},
        },
    )

    categories = {c.name: c for c in read_run(tmp_path)[0].categories}

    assert categories["text_content"].metric == "rule_pass_rate"
    assert categories["table"].metric == "grits_trm_composite"
    # A single hardcoded column header would show two different measurements under one name.
    assert "grits_trm_composite" in render(read_run(tmp_path))


def test_an_unscorable_category_is_unscored_not_zero(tmp_path) -> None:
    _publisher_run(tmp_path, "pipe", categories={"mystery": {"some_other_metric": 1.0}})

    category = read_run(tmp_path)[0].categories[0]

    assert category.score is None
    assert category.metric == "none reported"
    # Zero is a measurement. Absent is not, and the two must not print the same.
    assert "—" in render(read_run(tmp_path))


def test_targets_rank_by_the_publishers_numbers(tmp_path) -> None:
    _publisher_run(tmp_path, "worse", categories={"text": {"avg_rule_pass_rate": 0.684}})
    _publisher_run(tmp_path, "better", categories={"text": {"avg_rule_pass_rate": 0.874}})
    write_manifest(
        tmp_path,
        benchmark_id="parsebench",
        preset="smoke",
        documents=(),
        targets=(("backend:good", "better"), ("backend:bad", "worse")),
    )

    rendered = render(read_run(tmp_path))

    assert rendered.index("backend:good") < rendered.index("backend:bad")


def test_json_carries_every_number_the_table_shows(tmp_path) -> None:
    _publisher_run(
        tmp_path,
        "pipe",
        categories={
            "text": {
                "avg_rule_pass_rate": 0.5,
                "total_rule_pass_rate_passed": 5.0,
                "total_rule_pass_rate_evaluated": 10.0,
            }
        },
    )

    payload = to_json(read_run(tmp_path))["targets"][0]

    assert payload["overall"] == pytest.approx(0.5)
    assert payload["succeeded"] == 2
    assert payload["categories"][0]["evaluated"] == 10.0


def test_reading_a_directory_with_no_run_is_an_error(tmp_path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(ReportError, match="no publisher run"):
        read_run(tmp_path / "empty")

    with pytest.raises(ReportError, match="not a directory"):
        read_run(tmp_path / "missing")


def test_a_single_category_run_reports_its_root_level_report(tmp_path) -> None:
    """A one-category corpus writes its report at the pipeline root, not under a category.

    Measured against ParseBench 1.0.2. The per-category glob cannot see it, so a run over a
    corpus of your own documents reported no categories at all before this.
    """
    directory = tmp_path / "pipe"
    directory.mkdir(parents=True)
    (directory / "_metadata.json").write_text(
        json.dumps({"summary": {"total": 1, "successful": 1, "failed": 0}})
    )
    (directory / "_evaluation_report.json").write_text(
        json.dumps(
            {
                "total_examples": 1,
                "aggregate_metrics": {
                    "avg_rule_pass_rate": 1.0,
                    "total_rule_pass_rate_passed": 2.0,
                    "total_rule_pass_rate_evaluated": 2.0,
                },
            }
        )
    )

    categories = read_run(tmp_path)[0].categories

    assert [c.name for c in categories] == [ALL_DOCUMENTS]
    assert categories[0].score == 1.0
    assert categories[0].evaluated == 2.0


def test_a_root_report_never_double_counts_a_categorised_run(tmp_path) -> None:
    """A multi-category run writes no root report, but guard the sum anyway."""
    _publisher_run(tmp_path, "pipe", categories={"text": {"avg_rule_pass_rate": 0.5}})
    (tmp_path / "pipe" / "_evaluation_report.json").write_text(
        json.dumps({"total_examples": 9, "aggregate_metrics": {"avg_rule_pass_rate": 0.9}})
    )

    categories = read_run(tmp_path)[0].categories

    # The per-category rows win; the root is a fallback for when there are none.
    assert [c.name for c in categories] == ["text"]
