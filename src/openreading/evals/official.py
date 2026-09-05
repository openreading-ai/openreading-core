"""Lazy boundary to publisher-owned benchmark packages.

ParseBench and ExtractBench own dataset acquisition, case loading, metric
normalization, scoring, and their detailed reports. OpenReading registers one
provider inside those harnesses. That provider calls the public OpenReading API
and projects its normalized response into the publisher's output model.

The packages stay optional because their dependency stacks are substantial and
require Python 3.12 or newer. Missing or incompatible packages fail before data
preparation or inference with an install command. Publisher artifacts remain
the authoritative quality reports.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openreading.evals.benchmarks import get_benchmark
from openreading.evals.targets import BenchmarkTarget


class BenchmarkProfileError(ValueError):
    """Raised when discovery exists but no runnable profile is available."""


class BenchmarkDependencyError(RuntimeError):
    """Raised when an official package is missing or incompatible."""


@dataclass(frozen=True)
class OfficialRun:
    """Location and status of one target's publisher-owned benchmark run."""

    benchmark_id: str
    target: BenchmarkTarget
    pipeline_name: str
    output_dir: Path
    exit_code: int


@dataclass(frozen=True)
class OfficialComparison:
    """Publisher-generated leaderboard over two or more successful targets."""

    benchmark_id: str
    pipeline_names: tuple[str, ...]
    artifact: Path
    exit_code: int


def _preset_is_smoke(preset: str) -> bool:
    if preset not in {"smoke", "full"}:
        raise ValueError(f"unknown benchmark preset {preset!r}; choose smoke or full")
    return preset == "smoke"


def _runnable(benchmark_id: str):
    descriptor = get_benchmark(benchmark_id)
    if descriptor.status != "runnable":
        raise BenchmarkProfileError(
            f"{descriptor.id} is cataloged for discovery but has no runnable profile"
        )
    return descriptor


def _dependency_error(benchmark_id: str, exc: ModuleNotFoundError) -> BenchmarkDependencyError:
    descriptor = get_benchmark(benchmark_id)
    extra = descriptor.install_extra or descriptor.id
    return BenchmarkDependencyError(
        f"{descriptor.title} support is not installed. Use Python 3.12 or newer and run "
        f"`pip install 'openreading[{extra}]'`. Missing module: {exc.name or 'unknown'}"
    )


def prepare_official_benchmark(
    benchmark_id: str,
    *,
    cache_dir: str | Path,
    preset: str = "smoke",
    force: bool = False,
) -> Path:
    """Download one publisher dataset into a caller-controlled cache directory."""

    descriptor = _runnable(benchmark_id)
    smoke = _preset_is_smoke(preset)
    data_dir = Path(cache_dir) / descriptor.id / preset
    try:
        if descriptor.id == "parsebench":
            from openreading.evals import parsebench

            code = parsebench.prepare(data_dir=data_dir, smoke=smoke, force=force)
        elif descriptor.id == "extractbench":
            from openreading.evals import extractbench

            code = extractbench.prepare(data_dir=data_dir, smoke=smoke, force=force)
        else:  # pragma: no cover - guarded by the static runnable descriptors
            raise BenchmarkProfileError(f"no profile implementation for {descriptor.id}")
    except ModuleNotFoundError as exc:
        raise _dependency_error(descriptor.id, exc) from exc
    if code != 0:
        raise BenchmarkProfileError(
            f"{descriptor.id} publisher preparation failed with exit code {code}"
        )
    return data_dir


def run_official_benchmark(
    benchmark_id: str,
    target: BenchmarkTarget,
    *,
    data_dir: str | Path,
    output_dir: str | Path,
    preset: str = "smoke",
    config: str | None = None,
    policy: dict[str, Any] | None = None,
    jobs: int = 1,
    force: bool = False,
) -> OfficialRun:
    """Run one OpenReading target through the selected official harness."""

    descriptor = _runnable(benchmark_id)
    smoke = _preset_is_smoke(preset)
    if jobs < 1:
        raise ValueError("benchmark jobs must be at least 1")
    kwargs = {
        "target": target,
        "data_dir": Path(data_dir),
        "output_dir": Path(output_dir),
        "smoke": smoke,
        "config": config,
        "policy": policy,
        "jobs": jobs,
        "force": force,
    }
    try:
        if descriptor.id == "parsebench":
            from openreading.evals import parsebench

            return parsebench.run(**kwargs)
        if descriptor.id == "extractbench":
            from openreading.evals import extractbench

            return extractbench.run(**kwargs)
    except ModuleNotFoundError as exc:
        raise _dependency_error(descriptor.id, exc) from exc
    raise BenchmarkProfileError(f"no profile implementation for {descriptor.id}")


def build_official_comparison(
    benchmark_id: str,
    runs: list[OfficialRun],
    *,
    output_dir: str | Path,
) -> OfficialComparison:
    """Ask the publisher to compare successful target reports without rescoring."""

    descriptor = _runnable(benchmark_id)
    successful = tuple(run for run in runs if run.exit_code == 0)
    if len(successful) < 2:
        raise ValueError("an official comparison needs at least two successful target runs")
    pipeline_names = tuple(run.pipeline_name for run in successful)
    kwargs = {"pipeline_names": pipeline_names, "output_dir": Path(output_dir)}
    try:
        if descriptor.id == "parsebench":
            from openreading.evals import parsebench

            return parsebench.compare(**kwargs)
        if descriptor.id == "extractbench":
            from openreading.evals import extractbench

            return extractbench.compare(**kwargs)
    except ModuleNotFoundError as exc:
        raise _dependency_error(descriptor.id, exc) from exc
    raise BenchmarkProfileError(f"no profile implementation for {descriptor.id}")
