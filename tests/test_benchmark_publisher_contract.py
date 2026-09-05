"""The benchmark bridge checked against the contracts on both sides of it.

Every other benchmark test mocks a boundary. `test_benchmark_targets` mocks `openreading.api.run`,
so a request the real API refuses still passes, and `test_benchmark_official` mocks the publisher
modules, so a projection the real model rejects still passes. Two shipped defects lived in exactly
that gap: a bare JSON Schema handed to `extraction_schema` (which `OpenReadingRequest` forbids, so
every ExtractBench case died before a backend ran) and lowercase layout labels (which ParseBench's
fallback mapper refuses, so every page's layout evidence was dropped).

This module validates against the real things instead. The OpenReading side runs offline with no
key. The publisher side skips when the optional package is absent, which is every Python below
3.12, and the label check is the one assertion that reads the installed enum rather than a copy.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openreading.evals.targets import (
    _PARSEBENCH_DEFAULT_LABEL,
    _PARSEBENCH_LAYOUT_LABELS,
    BenchmarkTarget,
    execute_target,
    pipeline_name,
    project_extract_response,
    project_parse_response,
)
from openreading.testing.sample_pdf import build_sample_pdf

_SCHEMA = {
    "type": "object",
    "properties": {"total": {"type": "number"}, "vendor": {"type": "string"}},
    "required": ["total"],
}


@pytest.fixture(scope="module")
def sample_pdf(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("benchmark") / "sample.pdf"
    path.write_bytes(build_sample_pdf())
    return path


# --- the OpenReading side: the request an ExtractBench case actually builds -----------------


def test_extract_target_builds_a_valid_openreading_request(monkeypatch) -> None:
    """`build_request` is `extra="forbid"` all the way down, so a bare schema is a hard error."""
    from openreading import api

    captured: dict[str, object] = {}

    def capture(source, **kwargs):
        # The real builder, so the shape of every kwarg is checked rather than recorded.
        captured["request"] = api.build_request(source, kwargs.pop("backend", "auto"), **kwargs)
        return {"status": {"state": "succeeded"}, "document": {}}

    monkeypatch.setattr("openreading.api.run", capture)
    execute_target(
        b"%PDF-1.4",
        BenchmarkTarget.parse("backend:pymupdf"),
        product="extract",
        extraction_schema=_SCHEMA,
    )

    request = captured["request"]
    assert request.extraction_schema is not None
    assert request.extraction_schema.json_schema == _SCHEMA
    # ExtractBench scores word and page grounding F1. Neither is measurable unless per-field
    # geometry was asked for in the request that produced the fields.
    assert request.extraction_schema.citations is True
    assert request.outputs is not None and request.outputs.typed_fields is True


def test_parse_target_leaves_the_backend_sub_operation_unset(monkeypatch) -> None:
    """`backend.operation` is Textract's `AnalyzeDocument`, not the benchmark's product name."""
    from openreading import api

    captured: dict[str, object] = {}

    def capture(source, **kwargs):
        captured["request"] = api.build_request(source, kwargs.pop("backend", "auto"), **kwargs)
        return {"status": {"state": "succeeded"}, "document": {}}

    monkeypatch.setattr("openreading.api.run", capture)
    execute_target(b"%PDF-1.4", BenchmarkTarget.parse("backend:aws-textract"), product="parse")

    assert captured["request"].backend.operation is None


# --- the publisher side: the models the projections are validated against -------------------


def test_every_layout_label_is_a_canonical_ontology_value() -> None:
    ontology = pytest.importorskip("parse_bench.schemas.layout_ontology")

    values = {label.value for label in ontology.CanonicalLabel}
    assert set(_PARSEBENCH_LAYOUT_LABELS.values()) <= values
    assert _PARSEBENCH_DEFAULT_LABEL in values


def test_parse_projection_validates_against_the_official_model(sample_pdf: Path) -> None:
    parse_output = pytest.importorskip("parse_bench.schemas.parse_output")

    import openreading

    response = openreading.run(str(sample_pdf), backend="pymupdf")
    projection = project_parse_response(response, example_id="c1", pipeline_name="p1")
    output = parse_output.ParseOutput.model_validate(projection)

    assert output.markdown
    assert [page.page_index for page in output.pages] == list(range(len(output.pages)))
    assert output.layout_pages[0].items
    # pymupdf reports no per-block confidence, so both optional publisher fields stay unset
    # rather than carrying a 0.0 nobody measured.
    first = output.layout_pages[0].items[0]
    assert first.score is None
    assert first.layout_segments[0].confidence is None


def test_extract_projection_validates_against_the_official_model() -> None:
    extract_output = pytest.importorskip("extract_bench.schemas.extract_output")

    response = {
        "typed_fields": {
            "total": {
                "value": 19.5,
                "confidence": 0.91,
                "citations": [
                    {
                        "page": 2,
                        "text": "$19.50",
                        "bbox": {"x": 0.5, "y": 0.6, "w": 0.2, "h": 0.1, "page": 2},
                    }
                ],
            }
        }
    }

    output = extract_output.ExtractOutput.model_validate(
        project_extract_response(response, example_id="c2", pipeline_name="p2")
    )

    assert output.extracted_data == {"total": 19.5}
    assert output.field_citations[0].bbox == [0.5, 0.6, 0.2, 0.1]
    assert output.field_citations[0].page == 2


# --- both sides at once: the provider the publisher will actually call -----------------------


def test_registered_parsebench_provider_runs_and_normalizes_offline(sample_pdf: Path) -> None:
    """The whole bridge, minus the download: register, infer through pymupdf, normalize."""
    pytest.importorskip("parse_bench")
    from parse_bench.inference.pipelines import get_pipeline
    from parse_bench.inference.providers.registry import create_provider
    from parse_bench.schemas.pipeline_io import InferenceRequest

    from openreading.evals import parsebench

    target = BenchmarkTarget.parse("backend:pymupdf")
    try:
        pipeline_name = parsebench._register(target, config=None, policy=None)
    except parsebench.BenchmarkDependencyError as exc:  # a publisher version this pin rejects
        pytest.skip(str(exc))

    spec = get_pipeline(pipeline_name)
    provider = create_provider(spec)
    request = InferenceRequest(
        example_id="ex1", source_file_path=str(sample_pdf), product_type="parse"
    )

    result = provider.normalize(provider.run_inference(spec, request))

    assert result.output.markdown
    assert result.latency_in_ms >= 0
    # The raw artifact keeps the whole normalized envelope, which is what makes cost, warnings
    # and escalation readable beside the publisher's own quality numbers.
    assert result.raw_output["openreading_response"]["status"]["state"] == "succeeded"


def test_pipeline_name_is_stable_and_separates_configurations() -> None:
    target = BenchmarkTarget.parse("backend:pymupdf")
    base = pipeline_name("parsebench", target, config=None, policy=None)

    assert base == pipeline_name("parsebench", target, config=None, policy=None)
    assert base != pipeline_name("parsebench", target, config="openreading.yaml", policy=None)
    assert base != pipeline_name("parsebench", target, config=None, policy={"require_local": True})
    assert base != pipeline_name(
        "parsebench", BenchmarkTarget.parse("strategy:pymupdf"), config=None, policy=None
    )
    # The publisher keys its artifact directory on this name and both commands default to
    # ./benchmark-results, so the two products must not land in one directory.
    assert base != pipeline_name("extractbench", target, config=None, policy=None)


def test_registered_extractbench_provider_reaches_the_adapter(sample_pdf: Path) -> None:
    """A pure parser refusing extraction is the request arriving intact, one layer further on.

    The refusal comes from the pymupdf adapter, which only ever sees a request that passed
    `OpenReadingRequest` validation. A malformed `extraction_schema` fails earlier and louder,
    with a pydantic `extra_forbidden` on the request, so the error's identity is the assertion.
    """
    pytest.importorskip("extract_bench")
    from extract_bench.inference.pipelines import get_pipeline
    from extract_bench.inference.providers.registry import create_provider
    from extract_bench.schemas.pipeline_io import InferenceRequest

    from openreading.evals import extractbench
    from openreading.types.errors import UnsupportedFeatureError

    target = BenchmarkTarget.parse("backend:pymupdf")
    try:
        pipeline_name = extractbench._register(target, config=None, policy=None)
    except extractbench.BenchmarkDependencyError as exc:
        pytest.skip(str(exc))

    spec = get_pipeline(pipeline_name)
    provider = create_provider(spec)
    request = InferenceRequest(
        example_id="ex1",
        source_file_path=str(sample_pdf),
        product_type="extract",
        schema_override=_SCHEMA,
    )

    with pytest.raises(UnsupportedFeatureError):
        provider.run_inference(spec, request)


def test_extractbench_provider_refuses_a_case_with_no_schema(sample_pdf: Path) -> None:
    pytest.importorskip("extract_bench")
    from extract_bench.inference.pipelines import get_pipeline
    from extract_bench.inference.providers.registry import create_provider
    from extract_bench.schemas.pipeline_io import InferenceRequest

    from openreading.evals import extractbench

    target = BenchmarkTarget.parse("backend:pymupdf")
    try:
        pipeline_name = extractbench._register(target, config=None, policy=None)
    except extractbench.BenchmarkDependencyError as exc:
        pytest.skip(str(exc))

    spec = get_pipeline(pipeline_name)
    provider = create_provider(spec)
    request = InferenceRequest(
        example_id="ex2", source_file_path=str(sample_pdf), product_type="extract"
    )

    with pytest.raises(ValueError, match="extraction schema"):
        provider.run_inference(spec, request)


def test_extractbench_provider_normalizes_a_projection(sample_pdf: Path) -> None:
    """`normalize` is pure over the raw artifact, so it runs without an extraction-capable key."""
    pytest.importorskip("extract_bench")
    from datetime import UTC, datetime

    from extract_bench.inference.pipelines import get_pipeline
    from extract_bench.inference.providers.registry import create_provider
    from extract_bench.schemas.pipeline_io import InferenceRequest, RawInferenceResult

    from openreading.evals import extractbench

    target = BenchmarkTarget.parse("backend:pymupdf")
    try:
        pipeline_name = extractbench._register(target, config=None, policy=None)
    except extractbench.BenchmarkDependencyError as exc:
        pytest.skip(str(exc))

    spec = get_pipeline(pipeline_name)
    provider = create_provider(spec)
    request = InferenceRequest(
        example_id="ex3",
        source_file_path=str(sample_pdf),
        product_type="extract",
        schema_override=_SCHEMA,
    )
    response = {"typed_fields": {"total": {"value": 19.5}}, "status": {"state": "succeeded"}}
    moment = datetime.now(UTC)
    raw = RawInferenceResult(
        request=request,
        pipeline=spec,
        pipeline_name=pipeline_name,
        product_type=request.product_type,
        raw_output={
            "openreading_response": response,
            "projection": project_extract_response(
                response, example_id="ex3", pipeline_name=pipeline_name
            ),
        },
        started_at=moment,
        completed_at=moment,
        latency_in_ms=0,
    )

    result = provider.normalize(raw)

    assert result.output.extracted_data == {"total": 19.5}
    assert result.raw_output["openreading_response"] is response


def test_publisher_loader_reads_a_subset_this_repo_produced(tmp_path) -> None:
    """The other half of the small-run promise: the publisher must read what `--limit` writes.

    `test_benchmark_subset` proves the subset has the shape the publisher's format documents.
    This proves the publisher's own loader agrees, which is the assertion that would catch a
    corpus written in a shape only this repo believes in.
    """
    loader = pytest.importorskip("parse_bench.test_cases.loader")

    from openreading.evals.subset import materialize_subset, plan_subset
    from tests.conftest import jsonl_corpus

    plan = plan_subset(jsonl_corpus(tmp_path / "full"), limit=2)
    subset = materialize_subset(plan, tmp_path / "subset")

    cases = loader.load_test_cases(subset)

    assert [case.test_id for case in cases] == ["chart/doc0", "layout/doc0"]
    # Every rule asserted against a chosen document survives, so a limited run scores the same
    # way a full one does over those documents.
    assert all(len(case.test_rules) == 2 for case in cases)
    assert all(case.file_path.is_file() for case in cases)
    assert [case.expected_markdown for case in cases] == ["# chart 0", "# layout 0"]


def test_the_whole_bridge_runs_offline_over_a_limited_corpus(tmp_path) -> None:
    """Inference, scoring and reports, driven by the publisher's own runner. No key, no network.

    This is the test that would have caught the shipped feature not working at all. Everything
    else here checks one seam; this drives the real `BenchCLI.run` over a corpus `--limit`
    produced, with a local backend, and asserts the publisher wrote the artifacts a reader is
    told to go read.
    """
    pytest.importorskip("parse_bench")
    from parse_bench.cli import BenchCLI

    from openreading.evals import parsebench
    from openreading.evals.subset import materialize_subset, plan_subset
    from tests.conftest import jsonl_corpus

    target = BenchmarkTarget.parse("backend:pymupdf")
    try:
        pipeline_name = parsebench._register(target, config=None, policy=None)
    except parsebench.BenchmarkDependencyError as exc:
        pytest.skip(str(exc))

    subset = materialize_subset(
        plan_subset(jsonl_corpus(tmp_path / "full"), limit=2), tmp_path / "subset"
    )
    output = tmp_path / "out"

    code = BenchCLI().run(
        pipeline=pipeline_name,
        input_dir=subset,
        output_dir=output,
        max_concurrent=1,
        force=True,
        open_report=False,
        test=False,
    )

    assert code == 0
    summary = json.loads((output / pipeline_name / "_summary.json").read_text())
    # Two documents in, two inferred, none failed. A backend that never ran would show here as a
    # failure count rather than as a quietly empty report.
    assert (summary["total"], summary["successful"], summary["failed"]) == (2, 2, 0)
    raw = sorted(p.name for p in (output / pipeline_name).rglob("*.raw.json"))
    assert raw == ["doc0.raw.json", "doc0.raw.json"]
