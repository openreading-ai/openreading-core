"""Public benchmark discovery and terms gates.

A benchmark profile connects one publisher's documents and official scorer to
OpenReading. A license lane states whether the publisher's documented dataset
terms support the default commercial evaluation workflow. Discovery is static
and imports no optional benchmark package, so ``benchmark list`` and
``benchmark show`` remain offline.

The catalog keeps scorer-code terms separate from dataset terms. A permissive
repository license never upgrades unclear or source-specific document terms.
Runnable profiles currently use ParseBench and ExtractBench. Cataloged profiles
identify additional public corpora without claiming an execution integration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

BenchmarkStatus = Literal["runnable", "cataloged"]
LicenseLane = Literal["commercial", "research_only", "unverified"]


class BenchmarkTermsError(ValueError):
    """Raised when a restricted profile lacks its explicit acknowledgement."""


@dataclass(frozen=True)
class BenchmarkDescriptor:
    """Static facts needed to discover and select one public benchmark."""

    id: str
    title: str
    status: BenchmarkStatus
    license_lane: LicenseLane
    data_license: str
    data_license_url: str
    code_license: str
    code_license_url: str
    source_url: str
    default_revision: str
    dimensions: tuple[str, ...]
    presets: tuple[str, ...] = ()
    package: str | None = None
    install_extra: str | None = None
    estimated_documents: int | None = None
    estimated_pages: int | None = None


def _descriptor(
    benchmark_id: str,
    title: str,
    lane: LicenseLane,
    data_license: str,
    data_license_url: str,
    source_url: str,
    dimensions: tuple[str, ...],
    *,
    status: BenchmarkStatus = "cataloged",
    code_license: str = "See publisher repository",
    code_license_url: str | None = None,
    revision: str = "publisher default",
    package: str | None = None,
    install_extra: str | None = None,
    documents: int | None = None,
    pages: int | None = None,
) -> BenchmarkDescriptor:
    return BenchmarkDescriptor(
        id=benchmark_id,
        title=title,
        status=status,
        license_lane=lane,
        data_license=data_license,
        data_license_url=data_license_url,
        code_license=code_license,
        code_license_url=code_license_url or source_url,
        source_url=source_url,
        default_revision=revision,
        dimensions=dimensions,
        presets=("smoke", "full") if status == "runnable" else (),
        package=package,
        install_extra=install_extra,
        estimated_documents=documents,
        estimated_pages=pages,
    )


_CATALOG = {
    item.id: item
    for item in (
        _descriptor(
            "cord",
            "CORD",
            "commercial",
            "CC BY 4.0",
            "https://github.com/clovaai/cord/blob/master/LICENSE",
            "https://github.com/clovaai/cord",
            ("receipt text", "field values", "semantic labels"),
            code_license="Apache-2.0",
            documents=11000,
        ),
        _descriptor(
            "docile",
            "DocILE",
            "unverified",
            "Competition and source-specific terms",
            "https://github.com/rossumai/docile/blob/main/LICENSE",
            "https://github.com/rossumai/docile",
            ("field values", "field locations", "line items"),
            code_license="MIT",
        ),
        _descriptor(
            "doclaynet",
            "DocLayNet",
            "commercial",
            "CDLA-Permissive-1.0",
            "https://github.com/DS4SD/DocLayNet/blob/main/LICENSE",
            "https://github.com/DS4SD/DocLayNet",
            ("layout classes", "geometry", "page diversity"),
            code_license="Apache-2.0",
            pages=80863,
        ),
        _descriptor(
            "docubench",
            "DocuBench",
            "unverified",
            "Source-specific document terms",
            "https://github.com/Anni-Zou/DocuBench",
            "https://github.com/Anni-Zou/DocuBench",
            ("field values", "multilingual content", "hard inputs"),
        ),
        _descriptor(
            "extractbench",
            "ExtractBench",
            "commercial",
            "Apache-2.0",
            "https://huggingface.co/datasets/llamaindex/ExtractBench/blob/main/LICENSE",
            "https://github.com/run-llama/ExtractBench",
            ("field values", "record alignment", "word grounding", "page grounding"),
            status="runnable",
            code_license="Apache-2.0",
            code_license_url="https://github.com/run-llama/ExtractBench/blob/main/LICENSE",
            revision="0880af24f579236bff24291bc7f15e18c2fa51e3",
            package="extract-bench",
            install_extra="extractbench",
            documents=370,
            pages=4869,
        ),
        _descriptor(
            "fieldbench",
            "FieldBench",
            "unverified",
            "Mixed source terms with unresolved entries",
            "https://github.com/Zipstack/fieldbench",
            "https://github.com/Zipstack/fieldbench",
            ("field values", "cross-domain extraction"),
        ),
        _descriptor(
            "funsd",
            "FUNSD",
            "research_only",
            "Research and educational use",
            "https://guillaumejaume.github.io/FUNSD/",
            "https://guillaumejaume.github.io/FUNSD/",
            ("form text", "entities", "entity links"),
            documents=199,
        ),
        _descriptor(
            "govdocs1",
            "GovDocs1",
            "unverified",
            "No standard dataset license stated",
            "https://digitalcorpora.org/corpora/file-corpora/govdocs1/",
            "https://digitalcorpora.org/corpora/file-corpora/govdocs1/",
            ("format robustness", "throughput", "failure handling"),
            documents=1000000,
        ),
        _descriptor(
            "kleister-charity",
            "Kleister Charity",
            "unverified",
            "Source and redistribution terms require review",
            "https://github.com/applicaai/kleister-charity",
            "https://github.com/applicaai/kleister-charity",
            ("long documents", "field values", "entity extraction"),
        ),
        _descriptor(
            "ohrbench",
            "OHR-Bench",
            "unverified",
            "Dataset terms require verification",
            "https://github.com/opendatalab/OHR-Bench",
            "https://github.com/opendatalab/OHR-Bench",
            ("retrieval", "generation", "OCR impact"),
        ),
        _descriptor(
            "olmocr-bench",
            "olmOCR Bench",
            "unverified",
            "Benchmark data terms require verification",
            "https://github.com/allenai/olmocr/tree/main/olmocr/bench",
            "https://github.com/allenai/olmocr",
            ("text fidelity", "tables", "reading order", "unit tests"),
            code_license="Apache-2.0",
        ),
        _descriptor(
            "omnidocbench",
            "OmniDocBench",
            "research_only",
            "Research use only",
            "https://github.com/opendatalab/OmniDocBench/blob/main/LICENSE",
            "https://github.com/opendatalab/OmniDocBench",
            ("text", "tables", "formulas", "layout", "reading order"),
            code_license="Apache-2.0",
            pages=1651,
        ),
        _descriptor(
            "parsebench",
            "ParseBench",
            "commercial",
            "Apache-2.0",
            "https://huggingface.co/datasets/llamaindex/ParseBench/blob/main/LICENSE",
            "https://github.com/run-llama/ParseBench",
            ("tables", "charts", "content faithfulness", "semantic formatting", "visual grounding"),
            status="runnable",
            code_license="Apache-2.0",
            code_license_url="https://github.com/run-llama/ParseBench/blob/main/LICENSE",
            revision="parse-bench==1.0.2",
            package="parse-bench",
            install_extra="parsebench",
            documents=1211,
            pages=2078,
        ),
        _descriptor(
            "pubtables1m",
            "PubTables-1M",
            "commercial",
            "CDLA-Permissive-2.0",
            "https://github.com/microsoft/table-transformer/blob/main/LICENSE_DATA.md",
            "https://github.com/microsoft/table-transformer",
            ("table detection", "table structure", "spans", "cell content"),
            code_license="MIT",
            pages=575305,
        ),
        _descriptor(
            "readoc",
            "READoc",
            "unverified",
            "Dataset terms require verification",
            "https://github.com/DongfuJiang/READoc",
            "https://github.com/DongfuJiang/READoc",
            ("structured Markdown", "reading order", "multi-page continuity"),
        ),
        _descriptor(
            "xfund",
            "XFUND",
            "research_only",
            "CC BY-NC-SA 4.0",
            "https://github.com/doc-analysis/XFUND/blob/main/LICENSE",
            "https://github.com/doc-analysis/XFUND",
            ("multilingual forms", "entities", "entity links"),
            code_license="MIT",
        ),
    )
}


def list_benchmarks() -> tuple[BenchmarkDescriptor, ...]:
    """Return descriptors in stable identifier order without loading integrations."""

    return tuple(_CATALOG[key] for key in sorted(_CATALOG))


def get_benchmark(benchmark_id: str) -> BenchmarkDescriptor:
    """Resolve an identifier case-insensitively or raise an actionable ``KeyError``."""

    key = benchmark_id.strip().lower()
    try:
        return _CATALOG[key]
    except KeyError as exc:
        available = ", ".join(sorted(_CATALOG))
        raise KeyError(f"unknown benchmark {benchmark_id!r}; available: {available}") from exc


def require_benchmark_terms(
    descriptor: BenchmarkDescriptor,
    *,
    allow_research_only: bool = False,
    allow_unverified_terms: bool = False,
) -> None:
    """Fail closed unless the selected noncommercial lane is acknowledged by name."""

    if descriptor.license_lane == "research_only" and not allow_research_only:
        raise BenchmarkTermsError(
            f"{descriptor.id} has research-only dataset terms; pass --allow-research-only "
            "after reviewing the publisher terms"
        )
    if descriptor.license_lane == "unverified" and not allow_unverified_terms:
        raise BenchmarkTermsError(
            f"{descriptor.id} has unverified or source-specific dataset terms; pass "
            "--allow-unverified-terms after reviewing every applicable source"
        )
