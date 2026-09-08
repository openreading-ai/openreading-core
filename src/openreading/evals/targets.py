"""Benchmark targets and projections into publisher-owned output contracts.

A target is one backend or strategy evaluated by a public benchmark. Explicit
``backend:`` and ``strategy:`` prefixes prevent collisions between identifiers.
Execution always calls ``openreading.api.run``. It never invokes an adapter
directly, so credentials, caller scope, retries, normalization, and strategy
traces retain their normal behavior.

Projection functions return plain dictionaries because ParseBench and
ExtractBench are optional dependencies. Their integration modules validate
these dictionaries against the publisher's Pydantic models before scoring.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal, cast

from openreading import api

TargetKind = Literal["backend", "strategy"]
BenchmarkProduct = Literal["parse", "extract"]

# ParseBench resolves a label mapper by provider key, and a provider it does not know falls
# through to its CanonicalPassthroughMapper. That mapper calls `CanonicalLabel(label)` with no
# normalization, so a segment whose label is not verbatim Canonical17 raises
# UnknownRawLayoutLabelError and its page's layout evidence is lost. These are the enum's own
# values (`parse_bench.schemas.layout_ontology.CanonicalLabel`), not a lowercase spelling of them.
# `tests/test_benchmark_publisher_contract.py` re-checks every value against the installed enum.
_PARSEBENCH_LAYOUT_LABELS = {
    "title": "Title",
    "section_header": "Section-header",
    "header": "Page-header",
    "footer": "Page-footer",
    "page_number": "Text",
    "text": "Text",
    "list": "List-item",
    "list_item": "List-item",
    "table": "Table",
    "table_cell": "Table",
    "figure": "Picture",
    "image": "Picture",
    "caption": "Caption",
    "formula": "Formula",
    "code": "Code",
    "key_value": "Key-Value Region",
    "form_field": "Form",
    "selection_mark": "Checkbox-Selected",
    "table_of_contents": "Document Index",
}
_PARSEBENCH_DEFAULT_LABEL = "Text"


@dataclass(frozen=True)
class BenchmarkTarget:
    """One explicitly namespaced backend or strategy benchmark subject."""

    kind: TargetKind
    name: str

    @classmethod
    def parse(cls, value: str) -> BenchmarkTarget:
        """Parse ``backend:NAME`` or ``strategy:NAME`` and reject ambiguous input."""

        prefix, separator, name = value.strip().partition(":")
        if separator != ":" or prefix not in {"backend", "strategy"} or not name.strip():
            raise ValueError(
                f"invalid benchmark target {value!r}; use backend:NAME or strategy:NAME"
            )
        return cls(kind=cast(TargetKind, prefix), name=name.strip())

    @property
    def reference(self) -> str:
        """Return the stable CLI spelling used in artifacts and reports."""

        return f"{self.kind}:{self.name}"


def _config_identity(config) -> str | None:
    """A canonical digest of the configuration at `config`, or the raw value when there is none
    to read. Formatting and key order do not change it; any meaningful content change does."""
    if not config:
        return None
    from openreading import config as config_module

    try:
        loaded = config_module.load(config)
    except ValueError:
        return config
    return loaded.content_hash if loaded is not None else config


def pipeline_name(
    benchmark_id: str,
    target: BenchmarkTarget,
    *,
    config,
) -> str:
    """Name the publisher pipeline one target plus one configuration produces.

    The publisher keys its artifact directory, its resume logic, and its leaderboard rows on this
    name, so two runs that share it share a directory. The benchmark id is part of the hashed
    identity for that reason: `benchmark run parsebench --target backend:pymupdf` and
    `benchmark run extractbench --target backend:pymupdf` default to the same `--output-dir`, and
    without the id they would write parse results and extract results into one directory and then
    The config is in it because the same backend under a different `openreading.yaml`, and so
    under a different backend policy, is a different measurement. Stable ordering keeps a
    rerun's name identical so the publisher can resume rather than redo.
    """

    identity = json.dumps(
        {
            "benchmark": benchmark_id,
            "target": target.reference,
            # The file's CONTENT, never its path. The publisher keys its artifact directory and
            # its resume on this name, so hashing the path let an edited policy reuse results
            # measured under the previous one while labelling them as the current run. A file that
            # will not load contributes its path, because refusing to name a pipeline is not this
            # function's job — the loader raises for the caller a moment later.
            "config": _config_identity(config),
        },
        sort_keys=True,
    )
    suffix = hashlib.sha256(identity.encode()).hexdigest()[:10]
    safe_name = "".join(char if char.isalnum() else "_" for char in target.name)
    return f"openreading_{target.kind}_{safe_name}_{suffix}"


def execute_target(
    source: str,
    target: BenchmarkTarget,
    *,
    product: BenchmarkProduct,
    config=None,
    extraction_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one publisher case through the public OpenReading API."""

    # `operation=` is deliberately absent. It is `request.backend.operation`, a per-backend
    # SUB-operation ("AnalyzeExpense", "prebuilt-layout", "vlm"), not the benchmark's product.
    # Forcing "parse" there made aws-textract raise `unknown Textract operation 'parse'` on every
    # document and made azure-document-intelligence request a model id that does not exist. Left
    # unset, each adapter picks its own default and derives extract-vs-parse from the presence of
    # `extraction_schema`, which is what a plain `openreading parse` already does.
    kwargs: dict[str, Any] = {
        "config": config,
    }
    if target.kind == "backend":
        kwargs["backend"] = target.name
    else:
        kwargs["strategy"] = target.name
    if product == "parse":
        # ParseBench grades tables by pulling `<table>` out of the Markdown
        # (`parse_bench.evaluation.metrics.parse.table_extraction.extract_html_tables`). A GFM pipe
        # table is invisible to it, so a backend left on the default `tables: "markdown"` scores
        # zero on GriTS, TEDS and record match while having produced a perfectly good table. Ask
        # for the shape the scorer reads. A backend that cannot render table HTML is unaffected
        # and says so in `warnings[]`.
        kwargs["outputs"] = {"tables": "html"}
    if product == "extract":
        if extraction_schema is None:
            raise ValueError("an ExtractBench case requires its extraction schema")
        # The publisher hands over a bare JSON Schema. `request.extraction_schema` is the wrapper
        # around one (`json_schema` / `instructions` / `citations`) and forbids extra keys, so a
        # bare schema fails request validation before any backend runs. `citations` is on because
        # ExtractBench scores word and page grounding F1, which is only measurable when per-field
        # geometry is asked for; a backend that cannot ground says so in `warnings[]`.
        kwargs["extraction_schema"] = {"json_schema": extraction_schema, "citations": True}
        kwargs["outputs"] = {"typed_fields": True}
    return api.run(source, **kwargs)


def _string(value: Any) -> str:
    """The value when it is a non-blank string, otherwise the empty string."""

    return value if isinstance(value, str) and value.strip() else ""


def _page_markdown(page: dict[str, Any]) -> str:
    """Per-page markdown, assembled from blocks when the backend fills no page channel.

    ``pages[].markdown`` is native for only a few backends (LlamaParse, Mistral, MinerU). Falling
    straight from a missing one to ``pages[].text`` hands ParseBench's table and formatting rules
    a flat string, and the backend scores zero on markup it did produce. Block-level ``markdown``
    and ``html`` are populated much more widely (Chunkr segment content, Reducto block content,
    Azure and MinerU table HTML), so a page that carries markup in even one block is assembled
    from its blocks in reading order instead. A page whose blocks carry only plain text gains
    nothing from that and keeps the page's own text.
    """

    markdown = _string(page.get("markdown"))
    if markdown:
        return markdown
    blocks = [block for block in (page.get("blocks") or []) if isinstance(block, dict)]
    if any(_string(block.get("markdown")) or _string(block.get("html")) for block in blocks):
        rendered = [
            _string(block.get("markdown"))
            or _string(block.get("html"))
            or _string(block.get("text"))
            for block in blocks
        ]
        return "\n\n".join(piece for piece in rendered if piece)
    return _string(page.get("text"))


def _layout_item(block: dict[str, Any]) -> dict[str, Any]:
    item: dict[str, Any] = {
        "type": str(block.get("type") or "text"),
        "md": str(block.get("markdown") or ""),
        "html": str(block.get("html") or ""),
        "value": str(block.get("text") or ""),
    }
    bbox = block.get("bbox")
    if isinstance(bbox, dict) and all(key in bbox for key in ("x", "y", "w", "h")):
        # The canonical bbox is already normalized to [0,1] against the page, which is the space
        # LayoutSegmentIR documents for normalized page coords. Its `page` key is dropped because
        # the segment already sits inside its own page's layout entry.
        box = {key: float(bbox[key]) for key in ("x", "y", "w", "h")}
        segment: dict[str, Any] = {
            **box,
            "label": _PARSEBENCH_LAYOUT_LABELS.get(item["type"], _PARSEBENCH_DEFAULT_LABEL),
        }
        item["bbox"] = box
        item["layout_segments"] = [segment]
        raw_confidence = block.get("confidence")
        if isinstance(raw_confidence, (int, float)) and not isinstance(raw_confidence, bool):
            segment["confidence"] = float(raw_confidence)
            item["score"] = float(raw_confidence)
        # A deterministic parser reports no confidence, and both publisher fields are optional
        # for exactly that case ("None for parse-pipeline items"). Omitting is the channel
        # contract: a fabricated 0.0 is indistinguishable from a measured one, and ParseBench's
        # own layout adapters read a falsy confidence as 1.0 in some paths and 0.0 in others.
    return item


def project_parse_response(
    response: dict[str, Any], *, example_id: str, pipeline_name: str
) -> dict[str, Any]:
    """Project Markdown, pages, and canonical layout into ParseBench's IR."""

    document = response.get("document") or {}
    raw_pages = document.get("pages") or []
    pages: list[dict[str, Any]] = []
    layout_pages: list[dict[str, Any]] = []
    for index, raw_page in enumerate(raw_pages):
        if not isinstance(raw_page, dict):
            continue
        page_number = raw_page.get("page_number")
        if not isinstance(page_number, int) or page_number < 1:
            page_number = index + 1
        markdown = _page_markdown(raw_page)
        pages.append({"page_index": page_number - 1, "markdown": markdown})
        layout_page: dict[str, Any] = {
            "page_number": page_number,
            "md": markdown,
            "text": str(raw_page.get("text") or ""),
            "items": [
                _layout_item(block)
                for block in (raw_page.get("blocks") or [])
                if isinstance(block, dict)
            ],
        }
        if isinstance(raw_page.get("width"), (int, float)):
            layout_page["width"] = float(raw_page["width"])
        if isinstance(raw_page.get("height"), (int, float)):
            layout_page["height"] = float(raw_page["height"])
        layout_pages.append(layout_page)

    markdown = document.get("markdown")
    if not isinstance(markdown, str):
        markdown = "\n\n".join(page["markdown"] for page in pages)
    return {
        "task_type": "parse",
        "example_id": example_id,
        "pipeline_name": pipeline_name,
        "pages": pages,
        "layout_pages": layout_pages,
        "markdown": markdown,
    }


def project_extract_response(
    response: dict[str, Any], *, example_id: str, pipeline_name: str
) -> dict[str, Any]:
    """Project typed values and canonical evidence into ExtractBench's IR."""

    extracted: dict[str, Any] = {}
    citations: list[dict[str, Any]] = []
    for field_path, field in (response.get("typed_fields") or {}).items():
        if not isinstance(field, dict):
            continue
        normalized = field.get("normalized_value")
        extracted[field_path] = normalized if normalized is not None else field.get("value")
        field_confidence = field.get("confidence")
        for citation in field.get("citations") or []:
            if not isinstance(citation, dict):
                continue
            bbox = citation.get("bbox")
            page = citation.get("page")
            if not isinstance(page, int) and isinstance(bbox, dict):
                page = bbox.get("page")
            projected: dict[str, Any] = {
                "field_path": field_path,
                "page": page if isinstance(page, int) and page >= 1 else 1,
                "reference_text": citation.get("text"),
                "source": "openreading",
            }
            if isinstance(bbox, dict) and all(key in bbox for key in ("x", "y", "w", "h")):
                projected["bbox"] = [float(bbox[key]) for key in ("x", "y", "w", "h")]
                if isinstance(bbox.get("polygon"), list):
                    projected["polygon"] = bbox["polygon"]
            confidence = citation.get("confidence", field_confidence)
            if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
                projected["confidence"] = float(confidence)
            citations.append({key: value for key, value in projected.items() if value is not None})
    return {
        "task_type": "extract",
        "example_id": example_id,
        "pipeline_name": pipeline_name,
        "extracted_data": extracted,
        "field_citations": citations,
    }
