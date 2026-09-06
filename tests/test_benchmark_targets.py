"""Benchmark target execution and official output projection tests."""

from __future__ import annotations

import pytest

from openreading.evals.targets import (
    BenchmarkTarget,
    execute_target,
    project_extract_response,
    project_parse_response,
)


@pytest.mark.parametrize(
    ("value", "kind", "name"),
    [("backend:pymupdf", "backend", "pymupdf"), ("strategy:main", "strategy", "main")],
)
def test_target_requires_an_explicit_namespace(value: str, kind: str, name: str) -> None:
    assert BenchmarkTarget.parse(value) == BenchmarkTarget(kind=kind, name=name)


@pytest.mark.parametrize("value", ["pymupdf", "backend:", "other:x", " strategy: "])
def test_target_rejects_ambiguous_or_empty_names(value: str) -> None:
    with pytest.raises(ValueError, match="target"):
        BenchmarkTarget.parse(value)


def test_execute_target_uses_public_api_for_backend(monkeypatch) -> None:
    calls = []

    def fake_run(source, **kwargs):
        calls.append((source, kwargs))
        return {"status": {"state": "succeeded"}, "document": {}}

    monkeypatch.setattr("openreading.api.run", fake_run)
    result = execute_target(
        "doc.pdf",
        BenchmarkTarget.parse("backend:pymupdf"),
        product="parse",
    )

    assert result["status"]["state"] == "succeeded"
    # No `operation`. That kwarg is the per-backend SUB-operation, so "parse" there is a Textract
    # operation name that does not exist and an Azure model id that does not exist.
    assert calls == [
        (
            "doc.pdf",
            {
                "backend": "pymupdf",
                "config": None,
                # ParseBench reads tables out of `<table>` markup and ignores GFM pipe tables.
                "outputs": {"tables": "html"},
            },
        )
    ]


def test_execute_target_passes_strategy_config_and_extract_schema(monkeypatch) -> None:
    calls = []
    schema = {"type": "object", "properties": {"total": {"type": "number"}}}

    def fake_run(source, **kwargs):
        calls.append((source, kwargs))
        return {"status": {"state": "succeeded"}, "document": {}}

    monkeypatch.setattr("openreading.api.run", fake_run)
    execute_target(
        "doc.pdf",
        BenchmarkTarget.parse("strategy:fields"),
        product="extract",
        config="openreading.yaml",
        extraction_schema=schema,
    )

    # The publisher hands over a bare JSON Schema; `request.extraction_schema` is the wrapper
    # around one and forbids extra keys, so the bare form never reaches a backend.
    assert calls[0][1] == {
        "strategy": "fields",
        "config": "openreading.yaml",
        "extraction_schema": {"json_schema": schema, "citations": True},
        "outputs": {"typed_fields": True},
    }


def test_parse_projection_preserves_pages_markdown_and_layout() -> None:
    response = {
        "document": {
            "markdown": "# Whole",
            "pages": [
                {
                    "page_number": 1,
                    "width": 612,
                    "height": 792,
                    "markdown": "# Page",
                    "blocks": [
                        {
                            "type": "title",
                            "markdown": "# Page",
                            "text": "Page",
                            "bbox": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.1, "page": 1},
                        }
                    ],
                }
            ],
        }
    }

    projected = project_parse_response(response, example_id="case-1", pipeline_name="openreading")

    assert projected["markdown"] == "# Whole"
    assert projected["pages"] == [{"page_index": 0, "markdown": "# Page"}]
    assert projected["layout_pages"][0]["page_number"] == 1
    assert projected["layout_pages"][0]["items"][0]["type"] == "title"
    assert projected["layout_pages"][0]["items"][0]["bbox"] == {
        "x": 0.1,
        "y": 0.2,
        "w": 0.3,
        "h": 0.1,
    }
    # Canonical17 verbatim, because ParseBench's fallback mapper calls `CanonicalLabel(label)`
    # with no normalization. No confidence key: pymupdf-class parsers measure none, and a
    # fabricated 0.0 would be indistinguishable from a measured one.
    assert projected["layout_pages"][0]["items"][0]["layout_segments"] == [
        {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.1, "label": "Title"}
    ]
    assert "score" not in projected["layout_pages"][0]["items"][0]


def test_parse_projection_keeps_a_measured_confidence() -> None:
    response = {
        "document": {
            "pages": [
                {
                    "page_number": 1,
                    "blocks": [
                        {
                            "type": "table",
                            "text": "cell",
                            "confidence": 0.82,
                            "bbox": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0, "page": 1},
                        }
                    ],
                }
            ]
        }
    }

    item = project_parse_response(response, example_id="c", pipeline_name="p")["layout_pages"][0][
        "items"
    ][0]

    assert item["score"] == 0.82
    assert item["layout_segments"][0]["confidence"] == 0.82
    assert item["layout_segments"][0]["label"] == "Table"


def test_extract_projection_preserves_values_and_citations() -> None:
    response = {
        "typed_fields": {
            "total": {
                "value": 19.5,
                "confidence": 0.91,
                "citations": [
                    {
                        "page": 2,
                        "text": "$19.50",
                        "bbox": {
                            "x": 0.5,
                            "y": 0.6,
                            "w": 0.2,
                            "h": 0.1,
                            "page": 2,
                            "polygon": [[0.5, 0.6], [0.7, 0.6]],
                        },
                    }
                ],
            },
            "vendor": {"normalized_value": "Acme", "value": "ACME INC"},
        }
    }

    projected = project_extract_response(response, example_id="case-2", pipeline_name="openreading")

    assert projected["extracted_data"] == {"total": 19.5, "vendor": "Acme"}
    assert projected["field_citations"] == [
        {
            "field_path": "total",
            "page": 2,
            "bbox": [0.5, 0.6, 0.2, 0.1],
            "polygon": [[0.5, 0.6], [0.7, 0.6]],
            "reference_text": "$19.50",
            "confidence": 0.91,
            "source": "openreading",
        }
    ]


def test_page_markdown_assembles_blocks_when_the_page_channel_is_absent() -> None:
    response = {
        "document": {
            "pages": [
                {
                    "page_number": 1,
                    "text": "Region Units\nNorth 120",
                    "blocks": [
                        {"type": "title", "text": "Sales"},
                        {"type": "table", "html": "<table><tr><td>North</td></tr></table>"},
                    ],
                }
            ]
        }
    }

    projected = project_parse_response(response, example_id="c", pipeline_name="p")

    # The table survives as markup. Falling to pages[].text would flatten it and score the
    # backend at zero on a table it actually produced.
    assert projected["pages"][0]["markdown"] == ("Sales\n\n<table><tr><td>North</td></tr></table>")
    assert projected["markdown"] == projected["pages"][0]["markdown"]


def test_page_markdown_keeps_page_text_when_no_block_carries_markup() -> None:
    response = {
        "document": {
            "pages": [
                {
                    "page_number": 1,
                    "text": "plain page text",
                    "blocks": [{"type": "text", "text": "plain page text"}],
                }
            ]
        }
    }

    projected = project_parse_response(response, example_id="c", pipeline_name="p")

    assert projected["pages"][0]["markdown"] == "plain page text"
