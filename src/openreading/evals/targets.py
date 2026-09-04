"""Benchmark targets and projections into publisher-owned output contracts.

A target is one backend or strategy evaluated by a public benchmark. Explicit
``backend:`` and ``strategy:`` prefixes prevent collisions between identifiers.
Execution always calls ``openreading.api.run``. It never invokes an adapter
directly, so credentials, compliance, retries, normalization, and strategy
traces retain their normal behavior.

Projection functions return plain dictionaries because ParseBench and
ExtractBench are optional dependencies. Their integration modules validate
these dictionaries against the publisher's Pydantic models before scoring.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, cast

from openreading import api

TargetKind = Literal["backend", "strategy"]
BenchmarkProduct = Literal["parse", "extract"]

_PARSEBENCH_LAYOUT_LABELS = {
    "title": "title",
    "section_header": "section-header",
    "header": "page-header",
    "footer": "page-footer",
    "page_number": "text",
    "text": "text",
    "list": "list-item",
    "list_item": "list-item",
    "table": "table",
    "table_cell": "table",
    "figure": "picture",
    "image": "picture",
    "caption": "caption",
    "formula": "formula",
    "code": "code",
    "key_value": "key-value-region",
    "form_field": "form",
    "selection_mark": "checkbox-selected",
    "table_of_contents": "document-index",
}


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


def execute_target(
    source: str,
    target: BenchmarkTarget,
    *,
    product: BenchmarkProduct,
    config: str | None = None,
    policy: dict[str, Any] | None = None,
    extraction_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one publisher case through the public OpenReading API."""

    kwargs: dict[str, Any] = {
        "config": config,
        "operation": product,
        "policy": policy,
    }
    if target.kind == "backend":
        kwargs["backend"] = target.name
    else:
        kwargs["strategy"] = target.name
    if product == "extract":
        if extraction_schema is None:
            raise ValueError("an ExtractBench case requires its extraction schema")
        kwargs["extraction_schema"] = extraction_schema
        kwargs["outputs"] = {"typed_fields": True}
    return api.run(source, **kwargs)


def _page_markdown(page: dict[str, Any]) -> str:
    markdown = page.get("markdown")
    if isinstance(markdown, str):
        return markdown
    text = page.get("text")
    return text if isinstance(text, str) else ""


def _layout_item(block: dict[str, Any]) -> dict[str, Any]:
    item: dict[str, Any] = {
        "type": str(block.get("type") or "text"),
        "md": str(block.get("markdown") or ""),
        "html": str(block.get("html") or ""),
        "value": str(block.get("text") or ""),
    }
    bbox = block.get("bbox")
    if isinstance(bbox, dict) and all(key in bbox for key in ("x", "y", "w", "h")):
        box = {key: float(bbox[key]) for key in ("x", "y", "w", "h")}
        label = _PARSEBENCH_LAYOUT_LABELS.get(item["type"], "text")
        raw_confidence = block.get("confidence")
        confidence = (
            float(raw_confidence)
            if isinstance(raw_confidence, (int, float)) and not isinstance(raw_confidence, bool)
            else 0.0
        )
        item["bbox"] = box
        item["layout_segments"] = [{**box, "label": label, "confidence": confidence}]
        item["score"] = confidence
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
