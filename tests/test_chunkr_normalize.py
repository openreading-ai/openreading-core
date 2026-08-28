"""Chunkr `_normalize_parse` / `_normalize_extract` fidelity against the documented ParseTask /
ExtractTask shapes (docs.chunkr.ai/api-references/tasks/*). Pure — no client, no network. Pins the
Phase-B.4 fixes: native JSON extract values (no str() coercion), depth-N nested citations, plain
text projection of HTML/markdown segment content, HTML-table → canonical grid, kept segment-less
pages, and explicit High/Medium/Low rating quantization."""

from __future__ import annotations

import pytest

from openreading.adapters.chunkr import ChunkrAdapter
from openreading.types import BlockType
from openreading.types.request import Outputs

_HTML_TABLE = (
    "<table><tr><td>Region</td><td>Revenue</td></tr><tr><td>North</td><td>4400</td></tr></table>"
)


def _seg(**over) -> dict:
    seg = {
        "segment_id": "seg",
        "segment_type": "Text",
        "page_number": 1,
        "page_width": 612,
        "page_height": 792,
        "bbox": {"left": 72, "top": 58, "width": 260, "height": 24},
    }
    seg.update(over)
    return seg


def _raw_parse(segments: list[dict], pages: list[dict] | None = None) -> dict:
    return {
        "output": {
            "chunks": [{"chunk_id": "chunk_0", "content": "chunk md", "segments": segments}],
            "pages": pages if pages is not None else [{"page_number": 1}],
        },
        "output_usage": {"page_count": len(pages) if pages else 1},
    }


def _parse(segments: list[dict], pages: list[dict] | None = None):
    resp = ChunkrAdapter()._normalize_parse(_raw_parse(segments, pages), Outputs())
    return resp


def _raw_extract(results: dict, metrics: dict | None = None, citations: dict | None = None) -> dict:
    return {
        "output": {
            "results": results,
            "metrics": metrics or {},
            "citations": citations or {},
        },
        "output_usage": {"page_count": 1},
    }


# --- parse: table segment → grid + plain text ------------------------------------------


def test_table_segment_builds_grid_and_plain_tab_joined_text():
    seg = _seg(segment_type="Table", content=_HTML_TABLE, text="Region Revenue North 4400")
    resp = _parse([seg])
    blk = next(b for b in resp.document.pages[0].blocks if b.type is BlockType.TABLE)
    assert blk.table is not None
    assert blk.table.rows == [["Region", "Revenue"], ["North", "4400"]]
    # the flat text channel is the grid projected to tab-joined rows (C1/C2), never HTML
    assert blk.text == "Region\tRevenue\nNorth\t4400"
    assert "<table>" not in (resp.document.text or "")
    # table HTML lands in the html channel (was dropped); markdown is a pipe rendition
    assert blk.html and "<table>" in blk.html
    assert blk.markdown and "|" in blk.markdown and "Region" in blk.markdown


def test_segment_without_text_projects_html_content_to_plain():
    seg = _seg(segment_type="FormRegion", content="<p>Hello <b>world</b></p>")  # no `text`
    resp = _parse([seg])
    blk = resp.document.pages[0].blocks[0]
    assert blk.text == "Hello world"  # projected via html_to_text, not the raw HTML
    assert "<p>" not in (resp.document.text or "")


def test_segment_without_text_projects_markdown_content_to_plain():
    seg = _seg(segment_type="SectionHeader", content="## Account Summary")  # no `text`
    resp = _parse([seg])
    blk = resp.document.pages[0].blocks[0]
    assert blk.text == "Account Summary"  # md markup stripped
    assert blk.markdown == "## Account Summary"  # markdown channel keeps native content


def test_segmentless_pages_are_kept():
    seg = _seg(segment_type="Title", content="# T", text="T", page_number=1)
    pages = [
        {"page_number": 1, "page_width": 612, "page_height": 792, "dpi": 72},
        {"page_number": 2, "page_width": 612, "page_height": 792, "dpi": 72},  # no segments
    ]
    resp = _parse([seg], pages=pages)
    pnos = [p.page_number for p in resp.document.pages]
    assert pnos == [1, 2]  # page 2 no longer vanishes
    page2 = resp.document.pages[1]
    assert not page2.blocks
    assert page2.width == 612 and page2.height == 792
    assert resp.document.page_count == 2


def test_channel_provenance_populated_on_parse():
    resp = _parse([_seg(segment_type="Text", content="hi", text="hi")])
    assert resp.channel_provenance == {
        "markdown": "native",
        "text": "derived",
        "blocks": "native",
        "table_cells": "derived",
    }


# --- extract: native JSON, nested citations, explicit rating ---------------------------


def test_extract_keeps_native_json_types_no_str_coercion():
    resp = ChunkrAdapter()._normalize_extract(
        _raw_extract({"total": "$4,400.00", "applicant": {"name": "Jane Doe", "income": 82000}})
    )
    assert resp.typed_fields["total"].value == "$4,400.00"
    applicant = resp.typed_fields["applicant"].value
    assert applicant == {"name": "Jane Doe", "income": 82000}
    assert isinstance(applicant, dict)  # native JSON, NOT a str() repr of the dict


def test_extract_nested_leaf_citations_reachable():
    resp = ChunkrAdapter()._normalize_extract(
        _raw_extract(
            {"total": "$4,400.00", "applicant": {"name": "Jane Doe", "income": 82000}},
            citations={
                "total": {
                    "citation_id": "cit_0",
                    "page_number": 1,
                    "bboxes": [{"left": 72, "top": 140, "width": 100, "height": 16}],
                },
                "applicant": {  # nested — the leaf citation sits one level down
                    "income": {"citation_id": "cit_1", "page_number": 2, "bboxes": []}
                },
            },
        )
    )
    assert resp.typed_fields["total"].citations[0].page == 1
    app_cits = resp.typed_fields["applicant"].citations
    assert app_cits and any(c.page == 2 for c in app_cits)  # nested-leaf citation reachable (P0)


def test_extract_unknown_rating_preserved_known_mapped():
    resp = ChunkrAdapter()._normalize_extract(
        _raw_extract(
            {"total": "$4,400.00", "score": 5},
            metrics={"total": {"confidence": "High"}, "score": {"confidence": "Exceptional"}},
        )
    )
    assert resp.typed_fields["total"].confidence == pytest.approx(0.9)  # High → 0.9
    # an unknown label is preserved as its native string, not silently dropped to None
    assert resp.typed_fields["score"].confidence == "Exceptional"
