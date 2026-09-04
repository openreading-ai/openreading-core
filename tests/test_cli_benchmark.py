"""CLI coverage for public benchmark discovery, preparation, and execution."""

from __future__ import annotations

from pathlib import Path

from openreading.cli import main
from openreading.evals.official import OfficialComparison, OfficialRun
from openreading.evals.targets import BenchmarkTarget


def test_benchmark_list_is_offline_and_shows_lanes(capsys, monkeypatch) -> None:
    monkeypatch.setattr(
        "openreading.evals.official.prepare_official_benchmark",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must stay offline")),
    )

    assert main(["benchmark", "list"]) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert "parsebench" in out and "runnable" in out and "commercial" in out
    assert "omnidocbench" in out and "research_only" in out
    assert "govdocs1" in out and "unverified" in out


def test_benchmark_show_separates_data_and_code_terms(capsys) -> None:
    assert main(["benchmark", "show", "parsebench"]) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert "data license: Apache-2.0" in out
    assert "code license: Apache-2.0" in out
    assert "https://huggingface.co/datasets/llamaindex/ParseBench" in out
    assert "pip install 'openreading[parsebench]'" in out


def test_benchmark_show_unknown_profile_exits_2(capsys) -> None:
    assert main(["benchmark", "show", "missing"]) == 2
    assert "[benchmark] unknown benchmark" in capsys.readouterr().err


def test_benchmark_prepare_calls_official_downloader(monkeypatch, tmp_path, capsys) -> None:
    calls = []
    expected = tmp_path / "parsebench" / "smoke"

    def fake_prepare(benchmark_id, **kwargs):
        calls.append((benchmark_id, kwargs))
        return expected

    monkeypatch.setattr("openreading.evals.official.prepare_official_benchmark", fake_prepare)

    assert main(["benchmark", "prepare", "parsebench", "--cache-dir", str(tmp_path)]) == 0
    assert calls == [("parsebench", {"cache_dir": tmp_path, "preset": "smoke", "force": False})]
    assert f"prepared: {expected}" in capsys.readouterr().out


def test_benchmark_prepare_applies_terms_gate_before_download(
    monkeypatch, tmp_path, capsys
) -> None:
    calls = []
    monkeypatch.setattr(
        "openreading.evals.official.prepare_official_benchmark",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert main(["benchmark", "prepare", "omnidocbench", "--cache-dir", str(tmp_path)]) == 2
    assert calls == []
    assert "--allow-research-only" in capsys.readouterr().err


def test_benchmark_run_prepares_then_runs_each_target(monkeypatch, tmp_path, capsys) -> None:
    data_dir = tmp_path / "cache" / "parsebench" / "smoke"
    calls = []
    monkeypatch.setattr(
        "openreading.evals.official.prepare_official_benchmark",
        lambda *args, **kwargs: data_dir,
    )

    def fake_run(benchmark_id, target, **kwargs):
        calls.append((benchmark_id, target, kwargs))
        return OfficialRun(
            benchmark_id, target, f"pipe_{target.name}", Path(kwargs["output_dir"]), 0
        )

    monkeypatch.setattr("openreading.evals.official.run_official_benchmark", fake_run)
    monkeypatch.setattr(
        "openreading.evals.official.build_official_comparison",
        lambda benchmark_id, runs, output_dir: OfficialComparison(
            benchmark_id,
            tuple(run.pipeline_name for run in runs),
            Path(output_dir) / "leaderboard.html",
            0,
        ),
    )

    rc = main(
        [
            "benchmark",
            "run",
            "parsebench",
            "--target",
            "backend:pymupdf",
            "--target",
            "strategy:main",
            "--config",
            "openreading.yaml",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--output-dir",
            str(tmp_path / "out"),
            "--jobs",
            "2",
        ]
    )

    assert rc == 0
    assert [call[1] for call in calls] == [
        BenchmarkTarget.parse("backend:pymupdf"),
        BenchmarkTarget.parse("strategy:main"),
    ]
    assert all(call[2]["data_dir"] == data_dir for call in calls)
    assert all(call[2]["jobs"] == 2 for call in calls)
    out = capsys.readouterr().out
    assert "estimate:" in out and "2 target(s)" in out
    assert "completed: backend:pymupdf" in out
    assert "completed: strategy:main" in out
    assert "comparison: " in out and "leaderboard.html" in out


def test_benchmark_run_requires_a_target(capsys) -> None:
    assert main(["benchmark", "run", "parsebench"]) == 2
    assert "at least one --target" in capsys.readouterr().err


def test_benchmark_estimate_makes_no_backend_calls(capsys) -> None:
    assert (
        main(
            [
                "benchmark",
                "estimate",
                "extractbench",
                "--preset",
                "full",
                "--target",
                "backend:nuextract",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "documents: 370" in out
    assert "pages: 4869" in out
    assert "target calls: 370" in out
