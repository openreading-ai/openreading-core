"""ExtractBench integration through its public provider registry.

Each publisher case supplies its JSON Schema through ``schema_override``. The
OpenReading target receives that schema unchanged and returns typed fields.
Values and citations are projected into ExtractBench's official output model,
while the raw artifact retains the complete normalized OpenReading response.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from openreading.evals.official import BenchmarkDependencyError, OfficialComparison, OfficialRun
from openreading.evals.targets import (
    BenchmarkTarget,
    execute_target,
    project_extract_response,
)
from openreading.evals.targets import (
    pipeline_name as build_pipeline_name,
)

_SUPPORTED_VERSION = "0.1.0"
_SUPPORTED_REVISION = "0880af24f579236bff24291bc7f15e18c2fa51e3"


def _imports():
    import extract_bench
    from extract_bench.cli import BenchCLI
    from extract_bench.inference.pipelines import register_pipeline
    from extract_bench.inference.providers.base import Provider
    from extract_bench.inference.providers.registry import register_provider
    from extract_bench.schemas.extract_output import ExtractOutput
    from extract_bench.schemas.pipeline import PipelineSpec
    from extract_bench.schemas.pipeline_io import InferenceResult, RawInferenceResult
    from extract_bench.schemas.product import ProductType

    if extract_bench.__version__ != _SUPPORTED_VERSION:
        raise BenchmarkDependencyError(
            f"ExtractBench {extract_bench.__version__} is unsupported. Install the publisher "
            f"revision {_SUPPORTED_REVISION} through openreading[extractbench]."
        )
    return (
        BenchCLI,
        register_pipeline,
        register_provider,
        Provider,
        ExtractOutput,
        PipelineSpec,
        InferenceResult,
        RawInferenceResult,
        ProductType,
    )


def prepare(*, data_dir: Path, smoke: bool, force: bool) -> int:
    """Use ExtractBench's downloader for either its six-case test set or full set."""

    BenchCLI, *_ = _imports()
    data_dir.mkdir(parents=True, exist_ok=True)
    return int(BenchCLI().download(data_dir=data_dir, force=force, test=smoke))


def _register(target: BenchmarkTarget, *, config: str | None) -> str:
    (
        _,
        register_pipeline,
        register_provider,
        Provider,
        ExtractOutput,
        PipelineSpec,
        InferenceResult,
        RawInferenceResult,
        ProductType,
    ) = _imports()
    pipeline_name = build_pipeline_name("extractbench", target, config=config)
    provider_name = pipeline_name

    class OpenReadingExtractProvider(Provider):
        """Runtime provider bound to one OpenReading extraction target."""

        def run_inference(self, pipeline, request):
            if request.schema_override is None:
                raise ValueError("ExtractBench did not supply an extraction schema")
            started = datetime.now(UTC)
            response = execute_target(
                request.source_file_path,
                target,
                product="extract",
                config=config,
                extraction_schema=request.schema_override,
            )
            completed = datetime.now(UTC)
            projection = project_extract_response(
                response,
                example_id=request.example_id,
                pipeline_name=pipeline.pipeline_name,
            )
            return RawInferenceResult(
                request=request,
                pipeline=pipeline,
                pipeline_name=pipeline.pipeline_name,
                product_type=request.product_type,
                raw_output={"openreading_response": response, "projection": projection},
                started_at=started,
                completed_at=completed,
                latency_in_ms=max(0, int((completed - started).total_seconds() * 1000)),
            )

        def normalize(self, raw_result):
            output = ExtractOutput.model_validate(raw_result.raw_output["projection"])
            return InferenceResult(
                request=raw_result.request,
                pipeline_name=raw_result.pipeline_name,
                product_type=raw_result.product_type,
                raw_output=raw_result.raw_output,
                output=output,
                started_at=raw_result.started_at,
                completed_at=raw_result.completed_at,
                latency_in_ms=raw_result.latency_in_ms,
            )

    try:
        register_provider(provider_name)(OpenReadingExtractProvider)
        register_pipeline(
            PipelineSpec(
                pipeline_name=pipeline_name,
                provider_name=provider_name,
                product_type=ProductType.EXTRACT,
                config={},
            )
        )
    except ValueError as exc:
        if "already registered" not in str(exc):
            raise
    return pipeline_name


def run(
    *,
    target: BenchmarkTarget,
    data_dir: Path,
    output_dir: Path,
    smoke: bool,
    config: str | None,
    jobs: int,
    force: bool,
) -> OfficialRun:
    """Register the target, then delegate inference and scoring to ExtractBench."""

    BenchCLI, *_ = _imports()
    pipeline_name = _register(target, config=config)
    output_dir.mkdir(parents=True, exist_ok=True)
    code = int(
        BenchCLI().run(
            pipeline=pipeline_name,
            input_dir=data_dir,
            output_dir=output_dir,
            max_concurrent=jobs,
            force=force,
            open_report=False,
            test=smoke,
        )
    )
    return OfficialRun("extractbench", target, pipeline_name, output_dir, code)


def compare(*, pipeline_names: tuple[str, ...], output_dir: Path) -> OfficialComparison:
    """Generate ExtractBench's own cross-pipeline leaderboard."""

    BenchCLI, *_ = _imports()
    artifact = output_dir / "openreading-leaderboard.html"
    code = int(
        BenchCLI().leaderboard(
            *pipeline_names,
            output_dir=output_dir,
            output_file=artifact,
        )
    )
    return OfficialComparison("extractbench", pipeline_names, artifact, code)
