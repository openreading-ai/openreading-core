"""Pulse `_normalize` fidelity against the documented /extract response shape
(https://docs.runpulse.com/api-reference/endpoint/extract, accessed 2026-07-28): position-based
reading order (P0), the documented `page_number`/`bounding_box` keys + drift guard (P0), table
cells derived from `cell_data` and from markdown pipe tables (P1), and typed_fields extraction —
values + per-field confidence + citation-id→geometry resolution (verified live 2026-07-29) with a
deliver-or-warn fallback (P1). Pure — no client, no network (house pattern
tests/test_reducto_normalize.py)."""

from __future__ import annotations

import pytest

from openreading.adapters.pulse import PulseAdapter
from openreading.types import BlockType
from openreading.types.request import OpenReadingRequest


def _req(**over) -> OpenReadingRequest:
    body = {
        "document": {"url": "https://example.com/loan.pdf", "mime_type": "application/pdf"},
        "backend": {"id": "pulse"},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


def _norm(raw: dict, **over):
    return PulseAdapter()._normalize(raw, _req(**over))


# --- P0: reading order follows position, not provider class grouping --------------------


def test_class_grouped_input_is_reordered_by_position():
    # Pulse groups bounding_boxes by element CLASS (all Titles, then all Texts…). The text channel
    # and reading_order must follow true document position (bbox), not that class grouping.
    raw = {
        "bounding_boxes": {
            "Title": [
                {
                    "id": "t0",
                    "content": "Doc Title",
                    "page": 1,
                    "page_width": 612,
                    "page_height": 792,
                    "bbox": {"left": 72, "top": 40, "width": 200, "height": 20},
                }
            ],
            "Text": [
                {
                    "id": "x1",
                    "content": "Second paragraph",
                    "page": 1,
                    "page_width": 612,
                    "page_height": 792,
                    "bbox": {"left": 72, "top": 300, "width": 300, "height": 16},
                },
                {
                    "id": "x0",
                    "content": "First paragraph",
                    "page": 1,
                    "page_width": 612,
                    "page_height": 792,
                    "bbox": {"left": 72, "top": 100, "width": 300, "height": 16},
                },
            ],
        }
    }
    resp = _norm(raw)
    blocks = resp.document.pages[0].blocks
    assert [b.text for b in blocks] == ["Doc Title", "First paragraph", "Second paragraph"]
    assert [b.reading_order for b in blocks] == [0, 1, 2]
    # the text channel is projected in the same true document order (not class-grouped)
    assert resp.document.text == "Doc Title\n\nFirst paragraph\n\nSecond paragraph"


# --- P0: drift guard — the DOCUMENTED page_number / bounding_box keys --------------------


def test_documented_page_number_and_bounding_box_keys_read_without_degrading_layout():
    # The docs quote `page_number` (not `page`) and `bounding_box` (a normalized 0-1 polygon, not
    # the {left,top,width,height} object). A documented-key response must NOT silently lose layout.
    raw = {
        "bounding_boxes": {
            "Text": [
                {
                    "id": "x0",
                    "content": "on page 3",
                    "page_number": 3,
                    "bounding_box": [0.1, 0.2, 0.5, 0.2, 0.5, 0.4, 0.1, 0.4],
                }
            ]
        }
    }
    page = _norm(raw).document.pages[0]
    assert page.page_number == 3  # page_number honored (drift guard), not defaulted to 1
    blk = page.blocks[0]
    assert blk.bbox is not None and blk.bbox.page == 3
    assert blk.bbox.x == pytest.approx(0.1) and blk.bbox.y == pytest.approx(0.2)
    assert blk.bbox.w == pytest.approx(0.4) and blk.bbox.h == pytest.approx(0.2)


# --- P1: tables → the canonical grid (cell_data + markdown pipe) -------------------------


def test_table_cell_data_builds_native_grid_and_plain_text():
    raw = {
        "bounding_boxes": {
            "Tables": [
                {
                    "table_info": {"id": "tab0", "dimensions": [2, 2]},
                    "page_number": 1,
                    "bounding_box": [0.1, 0.1, 0.9, 0.1, 0.9, 0.5, 0.1, 0.5],
                    "cell_data": [
                        {"row": 0, "column": 0, "content": "Region", "is_header": True},
                        {"row": 0, "column": 1, "content": "Revenue", "is_header": True},
                        {"row": 1, "column": 0, "content": "North"},
                        {"row": 1, "column": 1, "content": "4400"},
                    ],
                }
            ]
        }
    }
    resp = _norm(raw)
    blk = resp.document.pages[0].blocks[0]
    assert blk.type is BlockType.TABLE and blk.id == "tab0"  # id from table_info.id
    assert blk.table is not None
    assert blk.table.rows == [["Region", "Revenue"], ["North", "4400"]]
    assert any(c.is_header for c in blk.table.cells)  # is_header from cell metadata, not row-0
    # the flat text channel is a tab-joined projection of the grid — never markup (C1/C2)
    assert blk.text == "Region\tRevenue\nNorth\t4400"
    assert "|" not in (resp.document.text or "")


def test_table_markdown_pipe_content_builds_grid_and_plain_text():
    raw = {
        "bounding_boxes": {
            "Tables": [
                {
                    "id": "tab0",
                    "content": "| Region | Revenue |\n| --- | --- |\n| North | 4400 |",
                    "page": 1,
                    "page_width": 612,
                    "page_height": 792,
                    "bbox": {"left": 72, "top": 140, "width": 400, "height": 60},
                }
            ]
        }
    }
    blk = _norm(raw).document.pages[0].blocks[0]
    assert blk.table is not None and blk.table.rows == [["Region", "Revenue"], ["North", "4400"]]
    assert blk.text == "Region\tRevenue\nNorth\t4400"  # no pipe markup in the text channel


def test_element_without_geometry_or_text_is_kept_without_bbox():
    # an Images element with no content and no geometry: no bbox fabricated, text stays absent.
    raw = {
        "bounding_boxes": {
            "Images": [{"id": "img0", "page_number": 2}],
            "Text": [
                {
                    "id": "x0",
                    "content": "caption below",
                    "page_number": 2,
                    "bounding_box": [0.1, 0.5, 0.6, 0.5, 0.6, 0.6, 0.1, 0.6],
                }
            ],
        }
    }
    page = _norm(raw).document.pages[0]
    assert page.page_number == 2
    img = next(b for b in page.blocks if b.type is BlockType.FIGURE)
    assert img.bbox is None and img.text is None  # no fabricated geometry, no text


def test_malformed_geometry_and_page_are_tolerated_not_crashed():
    raw = {
        "bounding_boxes": {
            "Text": [
                # non-numeric page → falls back to 1; odd-length polygon → no bbox
                {
                    "id": "a",
                    "content": "bad polygon",
                    "page_number": "n/a",
                    "bounding_box": [0.1, 0.2, 0.3],
                },
                # object bbox but no page_width/height → cannot normalize → no bbox
                {
                    "id": "b",
                    "content": "no page dims",
                    "page": 1,
                    "bbox": {"left": 1, "top": 1, "width": 2, "height": 2},
                },
            ]
        }
    }
    page = _norm(raw).document.pages[0]
    assert page.page_number == 1
    assert all(b.bbox is None for b in page.blocks)  # degraded gracefully, never fabricated


def test_table_entry_with_non_grid_content_falls_back_to_plain_text():
    # a Tables entry with neither cell_data nor a pipe table → no grid; content projected plainly.
    raw = {
        "bounding_boxes": {
            "Tables": [{"id": "tab0", "content": "Region Revenue North 4400", "page_number": 1}]
        }
    }
    blk = _norm(raw).document.pages[0].blocks[0]
    assert blk.type is BlockType.TABLE and blk.table is None
    assert blk.text == "Region Revenue North 4400"


# --- P1 (live-lane-gated): typed_fields deliver-or-warn ---------------------------------


def test_typed_fields_requested_but_unverified_emits_c6_warning():
    # extraction_schema forwarded but Pulse's structured_output response is NOT yet live-verified →
    # typed_fields is honestly absent + a machine-readable C6 warning names it (never silent-empty).
    raw = {
        "markdown": "# Doc",
        "bounding_boxes": {
            "Title": [
                {
                    "id": "t0",
                    "content": "Doc",
                    "page": 1,
                    "page_width": 612,
                    "page_height": 792,
                    "bbox": {"left": 72, "top": 40, "width": 100, "height": 20},
                }
            ]
        },
    }
    resp = _norm(raw, extraction_schema={"json_schema": {"type": "object"}})
    assert resp.typed_fields is None  # not fabricated from unverified keys
    assert "typed_fields_unverified" in {w.code for w in (resp.warnings or [])}
    assert "typed_fields" in {w.field for w in (resp.warnings or [])}


def test_structured_output_values_parse_to_native_typed_fields():
    # DOCUMENTED (deprecated) /extract structured-output shape; values keep native JSON types — no
    # str() coercion (§4.6). The live test confirms the REAL API returns this shape.
    raw = {
        "markdown": "# Doc",
        "structured_output": {"values": {"total": 128.5, "paid": True, "lines": [1, 2]}},
        "bounding_boxes": {},
    }
    resp = _norm(raw, extraction_schema={"json_schema": {"type": "object"}})
    tf = resp.typed_fields
    assert tf is not None
    assert tf["total"].value == 128.5 and tf["paid"].value is True
    assert tf["lines"].value == [1, 2]  # list preserved, not str()-coerced
    assert (resp.channel_provenance or {}).get("typed_fields") == "native"
    assert "typed_fields_unverified" not in {w.code for w in (resp.warnings or [])}
    # no confidence/citations in this raw → those fields stay absent (never fabricated)
    assert tf["total"].confidence is None and tf["total"].citations is None


def test_extract_typed_fields_carry_confidence_and_resolved_citations():
    # LIVE shape (pulse /extract, verified 2026-07-29): structured_output carries `values` PLUS
    # per-field `confidence` (float) and `citations` (comma-separated element-id refs). Confidence
    # → TypedField.confidence; each citation id resolves to page+bbox via bounding_boxes (text
    # elements by `id`+`bounding_box`, table cells by `cell_data[].id`+`location.coordinates`).
    raw = {
        "structured_output": {
            "values": {"bank_name": "FIFTH THIRD BANK", "ending_balance": "$73,924.59"},
            "confidence": {"bank_name": 0.9317, "ending_balance": 0.97},
            "citations": {"bank_name": "txt-99, txt-2", "ending_balance": "tbl-1-r4c2"},
        },
        "bounding_boxes": {
            "Text": [
                {
                    "id": "txt-99",
                    "page_number": 2,
                    "bounding_box": [0.1, 0.1, 0.3, 0.1, 0.3, 0.2, 0.1, 0.2],
                },
                {
                    "id": "txt-2",
                    "page_number": 1,
                    "bounding_box": [0.1, 0.04, 0.3, 0.04, 0.3, 0.05, 0.1, 0.05],
                },
            ],
            "Tables": [
                {
                    "id": "tbl-1",
                    "page_number": 1,
                    "cell_data": [
                        {
                            "id": "tbl-1-r4c2",
                            "location": {
                                "coordinates": [0.29, 0.31, 0.52, 0.31, 0.52, 0.34, 0.29, 0.34]
                            },
                            "content": "$73,924.59",
                        }
                    ],
                }
            ],
        },
    }
    resp = _norm(raw, extraction_schema={"json_schema": {"type": "object"}})
    tf = resp.typed_fields
    assert tf is not None
    bn = tf["bank_name"]
    assert bn.value == "FIFTH THIRD BANK"
    assert bn.confidence == pytest.approx(0.9317)
    assert bn.citations is not None and len(bn.citations) == 2  # both txt ids resolved
    assert {c.bbox.page for c in bn.citations} == {1, 2}  # resolved to real geometry
    eb = tf["ending_balance"]
    assert eb.confidence == pytest.approx(0.97)
    assert eb.citations and eb.citations[0].bbox is not None and eb.citations[0].bbox.page == 1
    assert "typed_fields_unverified" not in {w.code for w in (resp.warnings or [])}


def test_extract_citation_ids_that_do_not_resolve_are_dropped_not_fabricated():
    # an unknown id yields no citation (never a fabricated/empty-geometry Citation); confidence
    # still attaches from the confidence map.
    raw = {
        "structured_output": {
            "values": {"x": "v"},
            "confidence": {"x": 0.5},
            "citations": {"x": "does-not-exist"},
        },
        "bounding_boxes": {},
    }
    tf = _norm(raw, extraction_schema={"json_schema": {"type": "object"}}).typed_fields
    assert tf["x"].confidence == pytest.approx(0.5)
    assert tf["x"].citations is None


# --- content fidelity: the REAL /extract shape (id-prefixed content, Words, figures) --------
# The live /extract response carries, per element, BOTH a clean `original_content` and a
# markdown_with_ids `content` with an "<id>-" prefix ("0a-…"); a per-WORD "Words" class (~700
# entries for a 2-page doc); a "markdown_with_ids" string; and image placeholders. These pin that
# none of that provider machinery leaks into a content channel (C1) or inflates block granularity.


def test_original_content_is_preferred_over_id_prefixed_content():
    # Each element has BOTH keys; the text channel must use `original_content`, never the "0a-"
    # prefixed markdown_with_ids `content`.
    raw = {
        "bounding_boxes": {
            "Text": [
                {
                    "id": "0a",
                    "original_content": "Statement Period Date: 5/1/2014",
                    "content": "0a-Statement Period Date: 5/1/2014",
                    "page_number": 1,
                    "bounding_box": [0.1, 0.1, 0.5, 0.1, 0.5, 0.2, 0.1, 0.2],
                }
            ]
        }
    }
    blk = _norm(raw).document.pages[0].blocks[0]
    assert blk.text == "Statement Period Date: 5/1/2014"  # original_content, not "0a-…"
    assert not (blk.text or "").startswith("0a-")


def test_id_prefixed_content_is_stripped_when_original_content_absent():
    # Degraded shape: only the id-prefixed `content`. The "<id>-" prefix is stripped using the
    # element's own id, so nothing leaks into the text channel.
    raw = {
        "bounding_boxes": {
            "Text": [
                {
                    "id": "ixlc",
                    "content": "ixlc-Fallback line",
                    "page_number": 1,
                    "bounding_box": [0.1, 0.1, 0.5, 0.1, 0.5, 0.2, 0.1, 0.2],
                }
            ]
        }
    }
    blk = _norm(raw).document.pages[0].blocks[0]
    assert blk.text == "Fallback line"


def test_words_and_markdown_with_ids_classes_are_skipped():
    # Pulse returns a per-WORD "Words" breakdown (~one entry per token → ~700 blocks for a 2-page
    # doc) and a "markdown_with_ids" string. Neither is a semantic block; both are skipped so block
    # granularity stays at the semantic-element level (not per-word).
    raw = {
        "bounding_boxes": {
            "Text": [
                {
                    "id": "0a",
                    "original_content": "Hello world",
                    "page_number": 1,
                    "bounding_box": [0.1, 0.1, 0.5, 0.1, 0.5, 0.2, 0.1, 0.2],
                }
            ],
            "Words": [
                {
                    "id": f"w{i}",
                    "original_content": w,
                    "page_number": 1,
                    "bounding_box": [0.1, 0.1, 0.2, 0.1, 0.2, 0.2, 0.1, 0.2],
                }
                for i, w in enumerate(["Hello", "world"])
            ],
            "markdown_with_ids": "0a-Hello world",  # a string, not a block list
        }
    }
    blocks = [b for p in _norm(raw).document.pages for b in (p.blocks or [])]
    assert len(blocks) == 1  # only the one semantic Text block; no per-word blocks
    assert blocks[0].text == "Hello world"


def test_figure_without_original_content_carries_no_id_placeholder_text():
    # Images/Figures elements have no original_content — only a placeholder id in `content`
    # ("ixlc-0"). That must NOT become block text (C1).
    raw = {
        "bounding_boxes": {
            "Images": [
                {
                    "id": "ixlc-0",
                    "content": "ixlc-0",
                    "page_number": 1,
                    "bounding_box": [0.1, 0.3, 0.5, 0.3, 0.5, 0.6, 0.1, 0.6],
                }
            ]
        }
    }
    blk = _norm(raw).document.pages[0].blocks[0]
    assert blk.type is BlockType.FIGURE
    assert not blk.text  # no "ixlc-0" placeholder leaked into the text channel


def test_table_cell_id_prefixes_are_stripped_from_the_grid():
    # cell_data content carries the same "<id>-" prefixes ("0t-Account"); they must be stripped from
    # the grid cells (C1) — never surface in the table or the projected text channel.
    raw = {
        "bounding_boxes": {
            "Tables": [
                {
                    "table_info": {"id": "tab0", "dimensions": [2, 2]},
                    "page_number": 1,
                    "bounding_box": [0.1, 0.1, 0.9, 0.1, 0.9, 0.5, 0.1, 0.5],
                    "cell_data": [
                        {"row": 0, "column": 0, "content": "0t-Account"},
                        {"row": 0, "column": 1, "content": "1t-Amount"},
                        {"row": 1, "column": 0, "content": "2t-Checking"},
                        {"row": 1, "column": 1, "content": "3t-$53,781.48"},
                    ],
                }
            ]
        }
    }
    resp = _norm(raw)
    blk = resp.document.pages[0].blocks[0]
    assert blk.table is not None
    assert blk.table.rows == [["Account", "Amount"], ["Checking", "$53,781.48"]]
    assert "0t-" not in (resp.document.text or "") and "1t-" not in (resp.document.text or "")


# --- channel_provenance ------------------------------------------------------------------


def test_channel_provenance_marks_native_vs_derived():
    raw = {
        "markdown": "# Loan Application\n\n| Region | Revenue |\n| --- | --- |\n| North | 4400 |",
        "bounding_boxes": {
            "Title": [
                {
                    "id": "t0",
                    "content": "Loan Application",
                    "page": 1,
                    "page_width": 612,
                    "page_height": 792,
                    "bbox": {"left": 72, "top": 40, "width": 200, "height": 20},
                }
            ],
            "Tables": [
                {
                    "table_info": {"id": "tab0"},
                    "page_number": 1,
                    "bounding_box": [0.1, 0.2, 0.9, 0.2, 0.9, 0.5, 0.1, 0.5],
                    "cell_data": [
                        {"row": 0, "column": 0, "content": "Region"},
                        {"row": 0, "column": 1, "content": "Revenue"},
                    ],
                }
            ],
        },
    }
    prov = _norm(raw).channel_provenance or {}
    assert prov.get("markdown") == "native"  # Pulse's own markdown
    assert prov.get("text") == "derived"  # platform projection of blocks
    assert prov.get("blocks") == "native"  # from bounding_boxes
    assert prov.get("table_cells") == "derived"  # grid built by the derive layer
