"""ParseBench integration through its public extension registry.

The dynamically registered provider stores the complete OpenReading response in
ParseBench's raw artifact. Its normalized artifact uses the official
``ParseOutput`` model. ParseBench then applies its own rules, aggregations, and
report generation without any copied metric code.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openreading.evals.official import BenchmarkDependencyError, OfficialComparison, OfficialRun
from openreading.evals.targets import BenchmarkTarget, execute_target, project_parse_response

_SUPPORTED_VERSION = "1.0.2"


def _imports():
    import parse_bench
    from parse_bench.cli import BenchCLI
    from parse_bench.extensions import register_pipeline, register_provider
    from parse_bench.inference.providers.base import Provider
    from parse_bench.schemas.parse_output import ParseOutput
    from parse_bench.schemas.pipeline import PipelineSpec
    from parse_bench.schemas.pipeline_io import InferenceResult, RawInferenceResult

    if parse_bench.__version__ != _SUPPORTED_VERSION:
        raise BenchmarkDependencyError(
            f"ParseBench {parse_bench.__version__} is unsupported. Install parse-bench=={_SUPPORTED_VERSION}."
        )
    return (
        BenchCLI,
        register_pipeline,
        register_provider,
        Provider,
        ParseOutput,
        PipelineSpec,
        InferenceResult,
        RawInferenceResult,
    )


def prepare(*, data_dir: Path, smoke: bool, force: bool) -> int:
    """Use ParseBench's downloader for either its small test set or full set."""

    BenchCLI, *_ = _imports()
    data_dir.mkdir(parents=True, exist_ok=True)
    return int(BenchCLI().download(data_dir=data_dir, force=force, test=smoke))


def _pipeline_name(
    target: BenchmarkTarget, config: str | None, policy: dict[str, Any] | None
) -> str:
    identity = json.dumps(
        {"target": target.reference, "config": config, "policy": policy}, sort_keys=True
    )
    suffix = hashlib.sha256(identity.encode()).hexdigest()[:10]
    safe_name = "".join(char if char.isalnum() else "_" for char in target.name)
    return f"openreading_{target.kind}_{safe_name}_{suffix}"


def _register(target: BenchmarkTarget, *, config: str | None, policy: dict[str, Any] | None) -> str:
    (
        _,
        register_pipeline,
        register_provider,
        Provider,
        ParseOutput,
        PipelineSpec,
        InferenceResult,
        RawInferenceResult,
    ) = _imports()
    pipeline_name = _pipeline_name(target, config, policy)
    provider_name = pipeline_name

    class OpenReadingParseProvider(Provider):
        """Runtime provider bound to one OpenReading target."""

        def run_inference(self, pipeline, request):
            started = datetime.now(UTC)
            response = execute_target(
                request.source_file_path,
                target,
                product="parse",
                config=config,
                policy=policy,
            )
            completed = datetime.now(UTC)
            projection = project_parse_response(
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
            output = ParseOutput.model_validate(raw_result.raw_output["projection"])
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
        register_provider(provider_name)(OpenReadingParseProvider)
        register_pipeline(
            PipelineSpec(
                pipeline_name=pipeline_name,
                provider_name=provider_name,
                product_type="parse",
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
    policy: dict[str, Any] | None,
    jobs: int,
    force: bool,
) -> OfficialRun:
    """Register the target, then delegate inference and scoring to ParseBench."""

    BenchCLI, *_ = _imports()
    pipeline_name = _register(target, config=config, policy=policy)
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
    return OfficialRun("parsebench", target, pipeline_name, output_dir, code)


def compare(*, pipeline_names: tuple[str, ...], output_dir: Path) -> OfficialComparison:
    """Generate ParseBench's own cross-pipeline leaderboard."""

    BenchCLI, *_ = _imports()
    artifact = output_dir / "openreading-leaderboard.html"
    code = int(
        BenchCLI().leaderboard(
            *pipeline_names,
            output_dir=output_dir,
            output_file=artifact,
        )
    )
    return OfficialComparison("parsebench", pipeline_names, artifact, code)
