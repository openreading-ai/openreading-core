"""Lazy official benchmark dispatcher tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from openreading.evals.official import (
    BenchmarkDependencyError,
    BenchmarkProfileError,
    OfficialComparison,
    OfficialRun,
    build_official_comparison,
    prepare_official_benchmark,
    run_official_benchmark,
)
from openreading.evals.targets import BenchmarkTarget


def test_prepare_rejects_catalog_only_profile_before_import(tmp_path) -> None:
    with pytest.raises(BenchmarkProfileError, match="cataloged"):
        prepare_official_benchmark("doclaynet", cache_dir=tmp_path)


def test_prepare_dispatches_to_lazy_parsebench_module(monkeypatch, tmp_path) -> None:
    calls = []
    monkeypatch.setattr(
        "openreading.evals.parsebench.prepare",
        lambda **kwargs: calls.append(kwargs) or 0,
    )

    prepared = prepare_official_benchmark(
        "parsebench", cache_dir=tmp_path, preset="smoke", force=True
    )

    assert prepared == tmp_path / "parsebench" / "smoke"
    assert calls == [{"data_dir": prepared, "smoke": True, "force": True}]


def test_prepare_turns_missing_package_into_install_hint(monkeypatch, tmp_path) -> None:
    def missing(**kwargs):
        raise ModuleNotFoundError("No module named 'extract_bench'", name="extract_bench")

    monkeypatch.setattr("openreading.evals.extractbench.prepare", missing)

    with pytest.raises(BenchmarkDependencyError, match=r"openreading\[extractbench\]"):
        prepare_official_benchmark("extractbench", cache_dir=tmp_path)


def test_run_dispatches_target_and_returns_publisher_artifact(monkeypatch, tmp_path) -> None:
    target = BenchmarkTarget.parse("strategy:main")
    calls = []

    def fake_run(**kwargs):
        calls.append(kwargs)
        return OfficialRun(
            benchmark_id="extractbench",
            target=target,
            pipeline_name="openreading_strategy_main_abc",
            output_dir=Path(kwargs["output_dir"]),
            exit_code=0,
        )

    monkeypatch.setattr("openreading.evals.extractbench.run", fake_run)
    result = run_official_benchmark(
        "extractbench",
        target,
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "results",
        preset="full",
        config="openreading.yaml",
        jobs=3,
        force=True,
    )

    assert result.exit_code == 0
    assert result.target == target
    assert calls == [
        {
            "target": target,
            "data_dir": tmp_path / "data",
            "output_dir": tmp_path / "results",
            "smoke": False,
            "config": "openreading.yaml",
            "jobs": 3,
            "force": True,
        }
    ]


def test_comparison_uses_publisher_leaderboard(monkeypatch, tmp_path) -> None:
    runs = [
        OfficialRun(
            "parsebench",
            BenchmarkTarget.parse("backend:pymupdf"),
            "pipe_a",
            tmp_path,
            0,
        ),
        OfficialRun(
            "parsebench",
            BenchmarkTarget.parse("strategy:main"),
            "pipe_b",
            tmp_path,
            0,
        ),
    ]
    calls = []

    def fake_compare(**kwargs):
        calls.append(kwargs)
        return OfficialComparison(
            "parsebench", ("pipe_a", "pipe_b"), tmp_path / "leaderboard.html", 0
        )

    monkeypatch.setattr("openreading.evals.parsebench.compare", fake_compare)

    result = build_official_comparison("parsebench", runs, output_dir=tmp_path)

    assert result.exit_code == 0
    assert result.artifact == tmp_path / "leaderboard.html"
    assert calls == [{"pipeline_names": ("pipe_a", "pipe_b"), "output_dir": tmp_path}]


def test_comparison_needs_two_successful_runs(tmp_path) -> None:
    run = OfficialRun(
        "extractbench",
        BenchmarkTarget.parse("backend:nuextract"),
        "pipe_a",
        tmp_path,
        0,
    )
    with pytest.raises(ValueError, match="two successful"):
        build_official_comparison("extractbench", [run], output_dir=tmp_path)


@pytest.mark.parametrize("preset", ["tiny", "all", ""])
def test_prepare_rejects_unknown_preset(preset: str, tmp_path) -> None:
    with pytest.raises(ValueError, match="preset"):
        prepare_official_benchmark("parsebench", cache_dir=tmp_path, preset=preset)
