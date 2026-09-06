"""CLI coverage for public benchmark discovery, preparation, and execution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openreading.cli import main
from openreading.evals.official import OfficialComparison, OfficialRun
from openreading.evals.targets import BenchmarkTarget
from openreading.testing.sample_pdf import build_sample_pdf


@pytest.fixture
def prepared_corpus(tmp_path) -> Path:
    """A ParseBench-shaped corpus, so `run` has real documents to size and price."""
    root = tmp_path / "cache" / "parsebench" / "smoke"
    pdf = build_sample_pdf()
    for category in ("chart", "table"):
        rows = []
        for index in range(2):
            relative = f"pdfs/{category}/doc{index}.pdf"
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_bytes(pdf)
            rows.append(
                json.dumps({"pdf": relative, "category": category, "type": "present", "rule": {}})
            )
        (root / f"{category}.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return root


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


def test_benchmark_run_prepares_then_runs_each_target(
    monkeypatch, tmp_path, capsys, prepared_corpus
) -> None:
    calls = []
    monkeypatch.setattr(
        "openreading.evals.official.prepare_official_benchmark",
        lambda *args, **kwargs: prepared_corpus,
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
            "--limit",
            "0",
            "--yes",
        ]
    )

    assert rc == 0
    assert [call[1] for call in calls] == [
        BenchmarkTarget.parse("backend:pymupdf"),
        BenchmarkTarget.parse("strategy:main"),
    ]
    assert all(call[2]["data_dir"] == prepared_corpus for call in calls)
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
    # Pages, not documents. Every hosted backend bills per page, and ExtractBench is 370
    # documents but 4,869 pages, so a document count understates the bill about thirteen times.
    assert "pages (the billing unit): 4869" in out
    assert "backend:nuextract: not priced" in out


def test_benchmark_estimate_refuses_a_cataloged_profile(capsys) -> None:
    # Published scale with nothing to spend it on reads as a run you could start. GovDocs1 is a
    # million documents, so the number is the whole message if it prints at all.
    assert main(["benchmark", "estimate", "govdocs1", "--allow-unverified-terms"]) == 2
    err = capsys.readouterr().err
    assert "cataloged for discovery but has no runnable profile" in err


def test_benchmark_run_rejects_bad_jobs_before_downloading(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "openreading.evals.official.prepare_official_benchmark",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not download")),
    )

    rc = main(["benchmark", "run", "parsebench", "--target", "backend:pymupdf", "--jobs", "0"])

    assert rc == 2
    assert "--jobs must be at least 1" in capsys.readouterr().err


def test_benchmark_run_drops_a_repeated_target(
    monkeypatch, tmp_path, capsys, prepared_corpus
) -> None:
    monkeypatch.setattr(
        "openreading.evals.official.prepare_official_benchmark",
        lambda *args, **kwargs: prepared_corpus,
    )
    seen = []

    def fake_run(benchmark_id, target, **kwargs):
        seen.append(target)
        return OfficialRun(benchmark_id, target, "pipe", Path(kwargs["output_dir"]), 0)

    monkeypatch.setattr("openreading.evals.official.run_official_benchmark", fake_run)

    rc = main(
        [
            "benchmark",
            "run",
            "parsebench",
            "--target",
            "backend:pymupdf",
            "--target",
            "backend:pymupdf",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )

    # One run, and therefore no two-target leaderboard showing one pipeline against itself.
    assert rc == 0
    assert seen == [BenchmarkTarget.parse("backend:pymupdf")]
    err = capsys.readouterr().err
    assert "ignoring repeated target backend:pymupdf" in err


def _stub_run(monkeypatch, seen: list, corpus) -> None:
    monkeypatch.setattr(
        "openreading.evals.official.prepare_official_benchmark",
        lambda *args, **kwargs: corpus,
    )

    def fake_run(benchmark_id, target, **kwargs):
        seen.append((target, Path(kwargs["data_dir"])))
        return OfficialRun(benchmark_id, target, "pipe", Path(kwargs["output_dir"]), 0)

    monkeypatch.setattr("openreading.evals.official.run_official_benchmark", fake_run)


def test_benchmark_run_defaults_to_two_documents(
    monkeypatch, tmp_path, capsys, prepared_corpus
) -> None:
    seen: list = []
    _stub_run(monkeypatch, seen, prepared_corpus)

    rc = main(
        [
            "benchmark",
            "run",
            "parsebench",
            "--target",
            "backend:pymupdf",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )

    assert rc == 0
    # A subset directory, not the prepared corpus: the run touched 2 of the 4 prepared documents.
    corpus_used = seen[0][1]
    assert corpus_used != prepared_corpus
    assert sorted(p.name for p in corpus_used.glob("*.jsonl")) == ["chart.jsonl", "table.jsonl"]
    assert len(list(corpus_used.rglob("*.pdf"))) == 2
    out = capsys.readouterr().out
    assert "2 document(s) of 4 prepared (2 not run)" in out
    assert "document: chart/doc0" in out and "document: table/doc0" in out


def test_limit_zero_runs_the_prepared_corpus_in_place(
    monkeypatch, tmp_path, prepared_corpus
) -> None:
    seen: list = []
    _stub_run(monkeypatch, seen, prepared_corpus)

    rc = main(
        [
            "benchmark",
            "run",
            "parsebench",
            "--target",
            "backend:pymupdf",
            "--limit",
            "0",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )

    # Copying gigabytes to change nothing is its own surprise, so a complete run uses the cache.
    assert rc == 0
    assert seen[0][1] == prepared_corpus


def test_doc_selects_one_named_document(monkeypatch, tmp_path, capsys, prepared_corpus) -> None:
    seen: list = []
    _stub_run(monkeypatch, seen, prepared_corpus)

    rc = main(
        [
            "benchmark",
            "run",
            "parsebench",
            "--target",
            "backend:pymupdf",
            "--doc",
            "table/doc1",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )

    assert rc == 0
    assert len(list(seen[0][1].rglob("*.pdf"))) == 1
    assert "document: table/doc1" in capsys.readouterr().out


def test_an_unknown_doc_name_stops_before_running(
    monkeypatch, tmp_path, capsys, prepared_corpus
) -> None:
    seen: list = []
    _stub_run(monkeypatch, seen, prepared_corpus)

    rc = main(
        [
            "benchmark",
            "run",
            "parsebench",
            "--target",
            "backend:pymupdf",
            "--doc",
            "nope",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )

    assert rc == 2
    assert seen == []
    assert "no benchmark document named" in capsys.readouterr().err


def test_an_unpriced_target_refuses_without_yes(
    monkeypatch, tmp_path, capsys, prepared_corpus
) -> None:
    seen: list = []
    _stub_run(monkeypatch, seen, prepared_corpus)

    # pytest gives the process no terminal, which is the CI shape: refuse and name the flag
    # rather than hang on stdin. A strategy target is unpriced because escalation depth is unknown.
    rc = main(
        [
            "benchmark",
            "run",
            "parsebench",
            "--target",
            "strategy:main",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )

    assert rc == 2
    assert seen == []
    err = capsys.readouterr().err
    assert "pass --yes" in err
    assert "stopped before spending" in err


def test_run_prints_the_comparison_and_writes_a_manifest(
    monkeypatch, tmp_path, capsys, prepared_corpus
) -> None:
    """The answer belongs in the terminal, not only in an HTML file someone has to open."""
    out = tmp_path / "out"
    monkeypatch.setattr(
        "openreading.evals.official.prepare_official_benchmark",
        lambda *args, **kwargs: prepared_corpus,
    )

    def fake_run(benchmark_id, target, **kwargs):
        pipeline = f"pipe_{target.name}"
        directory = Path(kwargs["output_dir"]) / pipeline / "text"
        directory.mkdir(parents=True, exist_ok=True)
        (directory.parent / "_metadata.json").write_text(
            json.dumps({"summary": {"total": 2, "successful": 2, "failed": 0}})
        )
        (directory / "_evaluation_report.json").write_text(
            json.dumps(
                {
                    "total_examples": 2,
                    "aggregate_metrics": {
                        "avg_rule_pass_rate": 0.9 if target.name == "pymupdf" else 0.4
                    },
                }
            )
        )
        return OfficialRun(benchmark_id, target, pipeline, Path(kwargs["output_dir"]), 0)

    monkeypatch.setattr("openreading.evals.official.run_official_benchmark", fake_run)
    monkeypatch.setattr(
        "openreading.evals.official.build_official_comparison",
        lambda benchmark_id, runs, output_dir: OfficialComparison(
            benchmark_id, (), Path(output_dir) / "leaderboard.html", 0
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
            "backend:tesseract",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--output-dir",
            str(out),
        ]
    )

    assert rc == 0
    printed = capsys.readouterr().out
    assert "backend:pymupdf" in printed and "0.900" in printed
    # Better target first, by the publisher's own number.
    assert printed.index("backend:pymupdf") < printed.index("backend:tesseract")
    manifest = json.loads((out / "openreading-run.json").read_text())
    assert {t["reference"] for t in manifest["targets"]} == {
        "backend:pymupdf",
        "backend:tesseract",
    }


def test_report_reads_a_finished_run_without_rerunning_it(tmp_path, capsys) -> None:
    directory = tmp_path / "pipe" / "text"
    directory.mkdir(parents=True)
    (directory.parent / "_metadata.json").write_text(
        json.dumps({"summary": {"total": 1, "successful": 1, "failed": 0}})
    )
    (directory / "_evaluation_report.json").write_text(
        json.dumps({"total_examples": 1, "aggregate_metrics": {"avg_rule_pass_rate": 0.75}})
    )

    assert main(["benchmark", "report", "--output-dir", str(tmp_path), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["targets"][0]["overall"] == 0.75


def test_report_on_an_empty_directory_exits_2(tmp_path, capsys) -> None:
    assert main(["benchmark", "report", "--output-dir", str(tmp_path)]) == 2
    assert "no publisher run" in capsys.readouterr().err


def test_benchmark_target_naming_an_unknown_backend_is_usage(capsys):
    """`--target backend:NAME` validated its SYNTAX and never its identity, so a typo priced a
    run that could not exist and exited 0. Every other place a backend id is typed on this CLI
    refuses an unknown one by name and lists the known ids."""
    rc = main(["benchmark", "estimate", "parsebench", "--target", "backend:nosuchbackend"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "nosuchbackend" in err
    assert "pymupdf" in err  # the known ids, so the typo is fixable from the message


def test_benchmark_target_naming_a_real_backend_still_estimates(capsys):
    rc = main(["benchmark", "estimate", "parsebench", "--target", "backend:pymupdf"])
    assert rc == 0
    assert "1 target(s)" in capsys.readouterr().out
