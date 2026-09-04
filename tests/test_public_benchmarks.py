"""Public benchmark catalog and license-lane policy tests."""

from __future__ import annotations

import pytest

from openreading.evals.benchmarks import (
    BenchmarkTermsError,
    get_benchmark,
    list_benchmarks,
    require_benchmark_terms,
)


def test_catalog_has_runnable_parse_and_extraction_profiles() -> None:
    descriptors = {item.id: item for item in list_benchmarks()}

    assert descriptors["parsebench"].status == "runnable"
    assert descriptors["parsebench"].license_lane == "commercial"
    assert descriptors["parsebench"].package == "parse-bench"
    assert "tables" in descriptors["parsebench"].dimensions

    assert descriptors["extractbench"].status == "runnable"
    assert descriptors["extractbench"].license_lane == "commercial"
    assert descriptors["extractbench"].package == "extract-bench"
    assert "field values" in descriptors["extractbench"].dimensions


def test_catalog_covers_verified_and_restricted_public_corpora() -> None:
    descriptors = {item.id: item for item in list_benchmarks()}

    assert {"doclaynet", "pubtables1m", "cord"} <= descriptors.keys()
    assert descriptors["doclaynet"].license_lane == "commercial"
    assert descriptors["omnidocbench"].license_lane == "research_only"
    assert descriptors["funsd"].license_lane == "research_only"
    assert descriptors["govdocs1"].license_lane == "unverified"
    assert descriptors["fieldbench"].license_lane == "unverified"

    for descriptor in descriptors.values():
        assert descriptor.source_url.startswith("https://")
        assert descriptor.data_license
        assert descriptor.data_license_url.startswith("https://")
        assert descriptor.dimensions


def test_catalog_order_and_lookup_are_stable() -> None:
    first = list_benchmarks()
    second = list_benchmarks()

    assert first == second
    assert [item.id for item in first] == sorted(item.id for item in first)
    assert get_benchmark("ParseBench").id == "parsebench"

    with pytest.raises(KeyError, match="unknown benchmark"):
        get_benchmark("missing")


@pytest.mark.parametrize(
    ("benchmark_id", "research", "unverified", "allowed"),
    [
        ("parsebench", False, False, True),
        ("omnidocbench", False, False, False),
        ("omnidocbench", True, False, True),
        ("govdocs1", False, False, False),
        ("govdocs1", False, True, True),
        ("govdocs1", True, False, False),
    ],
)
def test_license_lanes_require_separate_acknowledgements(
    benchmark_id: str, research: bool, unverified: bool, allowed: bool
) -> None:
    descriptor = get_benchmark(benchmark_id)

    if allowed:
        require_benchmark_terms(
            descriptor,
            allow_research_only=research,
            allow_unverified_terms=unverified,
        )
    else:
        with pytest.raises(BenchmarkTermsError):
            require_benchmark_terms(
                descriptor,
                allow_research_only=research,
                allow_unverified_terms=unverified,
            )
