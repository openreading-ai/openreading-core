"""Reducto `_normalize_parse` fidelity against the documented response format
(https://docs.reducto.ai/parse/response-format): table HTML routing, block confidence
(a string in Reducto's schema), and the Checkbox block type. Pure, no client needed."""

from __future__ import annotations

import pytest

from openreading.adapters.reducto import ReductoAdapter
from openreading.types import BlockType
from openreading.types.request import Outputs

_BBOX = {"left": 0.1, "top": 0.1, "width": 0.2, "height": 0.05, "page": 1}


def _raw(blocks: list[dict], chunk_content: str = "chunk md") -> dict:
    return {
        "result": {"type": "full", "chunks": [{"content": chunk_content, "blocks": blocks}]},
        "usage": {"num_pages": 1},
    }


def _first_block(blocks: list[dict]):
    resp = ReductoAdapter()._normalize_parse(_raw(blocks), Outputs())
    return resp, resp.document.pages[0].blocks[0]


def test_table_html_routed_to_html_channel_and_text_is_plain():
    html = "<table><tr><th>Amt</th></tr><tr><td>10</td></tr></table>"
    resp, blk = _first_block([{"type": "Table", "content": html, "bbox": _BBOX}])
    assert blk.html and "<table>" in blk.html
    assert blk.text and "<table>" not in blk.text and "Amt" in blk.text and "10" in blk.text
    # the document-level text channel must be plain (no HTML tags)
    assert "<table>" not in (resp.document.text or "")


def test_plain_block_keeps_text_and_has_no_html():
    _, blk = _first_block([{"type": "Text", "content": "Beginning Balance: $1.00", "bbox": _BBOX}])
    assert blk.text == "Beginning Balance: $1.00"
    assert blk.html is None


def test_block_confidence_string_mapped_to_float():
    _, blk = _first_block([{"type": "Text", "content": "hi", "confidence": "high", "bbox": _BBOX}])
    assert blk.confidence == pytest.approx(0.9)


def test_checkbox_maps_to_selection_mark():
    _, blk = _first_block([{"type": "Checkbox", "content": "checked", "bbox": _BBOX}])
    assert blk.type is BlockType.SELECTION_MARK


# --- Phase B.1: derive-library adoption + fidelity fixes ---------------------------------

import json  # noqa: E402


def _jsonbbox(rows: list[list[str]]) -> str:
    """A reducto advanced_options.table_output_format=jsonbbox payload (one table)."""

    def cell(t: str) -> dict:
        return {"text": t, "bbox": {"x": 0.1, "y": 0.1, "width": 0.1, "height": 0.05}}

    return json.dumps([[[cell(t) for t in row] for row in rows]])


def test_jsonbbox_table_builds_native_cells_with_bboxes_and_plain_text():
    content = _jsonbbox([["Date", "Amount"], ["05/01", "100"]])
    _, blk = _first_block([{"type": "Table", "content": content, "bbox": _BBOX}])
    assert blk.table is not None and blk.table.rows == [["Date", "Amount"], ["05/01", "100"]]
    assert blk.table.cells and blk.table.cells[0].bbox is not None  # native cell geometry
    # the flat text channel is a plain projection of the grid, never the JSON blob
    assert "{" not in (blk.text or "") and "\t" in (blk.text or "")
    assert blk.text == "Date\tAmount\n05/01\t100"


def test_markdown_pipe_table_in_text_block_does_not_leak_into_text():
    md = "| Date | Amt |\n| --- | --- |\n| 05/01 | 100 |"
    resp, blk = _first_block([{"type": "Text", "content": md, "bbox": _BBOX}])
    assert "| ---" not in (blk.text or "") and "|" not in (blk.text or "")  # md markup stripped
    assert "Date" in blk.text and "05/01" in blk.text
    assert "| ---" not in (resp.document.text or "")


def test_markdown_heading_content_projected_to_plain_text():
    _, blk = _first_block(
        [{"type": "Section Header", "content": "## Account Summary", "bbox": _BBOX}]
    )
    assert blk.text == "Account Summary" and blk.markdown == "## Account Summary"


def test_granular_parse_confidence_preferred_over_string_enum():
    _, blk = _first_block(
        [
            {
                "type": "Text",
                "content": "hi",
                "confidence": "low",  # coarse string enum
                "granular_confidence": {"extract_confidence": None, "parse_confidence": 0.87},
                "bbox": _BBOX,
            }
        ]
    )
    assert blk.confidence == pytest.approx(0.87)  # numeric granular signal wins


def test_original_page_preserved_in_bbox():
    bbox = {"left": 0.1, "top": 0.1, "width": 0.2, "height": 0.05, "page": 2, "original_page": 7}
    _, blk = _first_block([{"type": "Text", "content": "on page 7", "bbox": bbox}])
    assert blk.bbox is not None and blk.bbox.page == 7  # source page wins over provider renumber


def test_chunk_text_is_plain_for_markdown_chunk_content():
    resp = ReductoAdapter()._normalize_parse(
        _raw([{"type": "Text", "content": "x", "bbox": _BBOX}], chunk_content="# Chunk Heading"),
        Outputs(),
    )
    assert resp.chunks[0].text == "Chunk Heading" and resp.chunks[0].markdown == "# Chunk Heading"


def test_extract_non_dict_result_raises_not_empty():
    from openreading.types.errors import TerminalError

    with pytest.raises(TerminalError):
        ReductoAdapter()._normalize_extract({"result": "a bare string, not a dict"})
