"""Google Document AI adapter — a real index-anchored Document proto replayed via an injected
fake client (NO GCP). Exercises: textAnchor offset resolution into the top-level `text`,
normalizedVertices → canonical bbox, native 0-1 confidence, tables, and entities → typed_fields."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from openreading.adapters.google_document_ai import GoogleDocumentAIAdapter
from openreading.testing import ConformanceCase, check_adapter_conformance
from openreading.types import BlockType
from openreading.types.errors import TerminalError
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RunContext

FIX = Path(__file__).parent / "fixtures" / "google-document-ai"
DOC_B64 = base64.b64encode(b"%PDF-1.7 fake").decode()
# project/processor/location are non-secret config → ctx.runtime (the adapter authenticates by ADC)
CONFIG = {"project_id": "p", "location": "us", "processor_id": "proc"}


def _fixture() -> dict:
    return json.loads((FIX / "process.json").read_text())


class FakeDocAIClient:
    def process(self, processor_name, content, mime_type):
        assert "processors/proc" in processor_name  # processor name assembled from ctx.runtime
        return _fixture()


def _req(**over) -> OpenReadingRequest:
    body = {
        "document": {"bytes_base64": DOC_B64, "mime_type": "application/pdf"},
        "backend": {"id": "google-document-ai", "type": "hosted_api"},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


def _run(adapter, req):
    ctx = RunContext(runtime=dict(CONFIG))
    return adapter.normalize(adapter.submit(req, ctx), ctx, req)


class _DocClient:
    """Replay an arbitrary index-anchored Document proto (built inline in a test), NO GCP."""

    def __init__(self, doc: dict) -> None:
        self._doc = doc

    def process(self, processor_name, content, mime_type):
        return {"document": self._doc}


def _run_doc(doc: dict, **over):
    adapter = GoogleDocumentAIAdapter(client=_DocClient(doc))
    return _run(adapter, _req(**over))


def test_docai_conforms():
    check_adapter_conformance(
        GoogleDocumentAIAdapter(client=FakeDocAIClient()),
        [
            ConformanceCase(
                request=_req(),
                ctx=RunContext(runtime=dict(CONFIG)),
                deterministic=True,
                label="process",
            )
        ],
        strict_checks={"C1", "C6"},  # remediated: no-markup-in-text + deliver-or-warn
        # Ledger T4a §6: google-document-ai is one of the 5 untouched-this-tranche adapters — run
        # through R1/R2 too (not assumed), confirming the now-v2 declaration of protocol_version=2
        # is warranted — R1/R2 genuinely pass, not merely asserted.
        adapter_factory=lambda: GoogleDocumentAIAdapter(client=FakeDocAIClient()),
    )


def test_textanchor_offsets_resolve_paragraph_text():
    resp = _run(GoogleDocumentAIAdapter(client=FakeDocAIClient()), _req())
    blocks = resp.document.pages[0].blocks
    texts = [b.text for b in blocks if b.type is BlockType.TEXT]
    assert "Loan Application" in texts  # resolved via textSegments[0:16] into document.text
    assert "Applicant income summary." in texts
    assert resp.document.text.startswith("Loan Application")


def test_normalized_vertices_become_canonical_bbox_with_native_confidence():
    resp = _run(GoogleDocumentAIAdapter(client=FakeDocAIClient()), _req())
    para = next(b for b in resp.document.pages[0].blocks if b.text == "Loan Application")
    # normalizedVertices min (0.1,0.05) -> canonical passthrough
    assert para.bbox.x == pytest.approx(0.1) and para.bbox.y == pytest.approx(0.05)
    assert para.bbox.bbox_native.unit.value == "normalized"
    # DocAI confidence is already 0-1 (not /100)
    assert para.confidence == pytest.approx(0.99)


def test_table_cells_resolved_via_offsets():
    resp = _run(GoogleDocumentAIAdapter(client=FakeDocAIClient()), _req())
    table = next(b for b in resp.document.pages[0].blocks if b.type is BlockType.TABLE)
    assert table.table.rows == [["Region", "Revenue"], ["North", "4400"]]
    assert any(c.is_header and c.text == "Region" for c in table.table.cells)


def test_entities_become_typed_fields_with_normalized_value():
    resp = _run(GoogleDocumentAIAdapter(client=FakeDocAIClient()), _req())
    gp = resp.typed_fields["gross_pay"]
    assert gp.value == "4400.00"
    assert gp.normalized_value == "4400.00"
    assert gp.confidence == pytest.approx(0.97)


def test_page_dims_and_unit_points():
    resp = _run(GoogleDocumentAIAdapter(client=FakeDocAIClient()), _req())
    page = resp.document.pages[0]
    assert page.width == 612.0 and page.height == 792.0 and page.unit.value == "pdf_point"


def test_missing_config_is_terminal():
    adapter = GoogleDocumentAIAdapter(client=FakeDocAIClient())
    with pytest.raises(TerminalError, match="project_id") as exc:
        adapter.submit(_req(), RunContext())  # no project/processor
    assert exc.value.backend_code == "no_credentials"


# --- Phase B.3: derive adoption + fidelity fixes ---------------------------------------

_NV = {
    "normalizedVertices": [
        {"x": 0.1, "y": 0.1},
        {"x": 0.5, "y": 0.1},
        {"x": 0.5, "y": 0.15},
        {"x": 0.1, "y": 0.15},
    ]
}


def _para(start, end, y=0.1):
    seg = {"endIndex": str(end)} if start == 0 else {"startIndex": str(start), "endIndex": str(end)}
    return {
        "layout": {
            "textAnchor": {"textSegments": [seg]},
            "boundingPoly": {
                "normalizedVertices": [
                    {"x": 0.1, "y": y},
                    {"x": 0.5, "y": y},
                    {"x": 0.5, "y": y + 0.03},
                    {"x": 0.1, "y": y + 0.03},
                ]
            },
        }
    }


def test_utf8_byte_offsets_slice_non_ascii_correctly():
    # "café résumé" = 11 code points but 14 UTF-8 bytes; DocAI reports BYTE offsets.
    doc = {
        "text": "café résumé",
        "pages": [
            {
                "pageNumber": 1,
                "dimension": {"width": 612.0, "height": 792.0, "unit": "POINTS"},
                "paragraphs": [_para(0, 5, y=0.1), _para(6, 14, y=0.2)],
            }
        ],
    }
    resp = _run_doc(doc)
    texts = [b.text for b in resp.document.pages[0].blocks if b.type is BlockType.TEXT]
    assert "café" in texts  # bytes[0:5]
    assert "résumé" in texts  # bytes[6:14] — code-point slicing would garble this to "ésumé"
    assert "ésumé" not in texts


def test_repeated_entity_types_collect_into_list_not_last_writer_wins():
    doc = {
        "text": "",
        "pages": [],
        "entities": [
            {"type": "line_item", "mentionText": "Widget A", "confidence": 0.9},
            {"type": "line_item", "mentionText": "Widget B", "confidence": 0.8},
            {"type": "total", "mentionText": "100.00", "confidence": 0.95},
        ],
    }
    resp = _run_doc(doc, outputs={"typed_fields": True})
    assert resp.typed_fields["line_item"].value == ["Widget A", "Widget B"]  # both kept
    assert resp.typed_fields["total"].value == "100.00"  # single stays scalar


def test_entity_properties_recurse_and_page_anchor_citations():
    doc = {
        "text": "",
        "pages": [{"pageNumber": 1, "dimension": {"width": 612.0, "height": 792.0}}],
        "entities": [
            {
                "type": "invoice",
                "confidence": 0.9,
                "properties": [
                    {"type": "invoice_id", "mentionText": "INV-1"},
                    {"type": "amount", "mentionText": "42.00"},
                    {"type": "tax", "mentionText": "3.00"},
                    {"type": "tax", "mentionText": "1.00"},  # repeated nested → list value
                ],
                "pageAnchor": {
                    "pageRefs": [{"page": "0", "boundingPoly": _NV, "confidence": 0.88}]
                },
            }
        ],
    }
    resp = _run_doc(doc, outputs={"typed_fields": True})
    inv = resp.typed_fields["invoice"]
    assert inv.value == {
        "invoice_id": "INV-1",
        "amount": "42.00",
        "tax": ["3.00", "1.00"],
    }  # nested natural JSON; repeated sub-type collects
    assert inv.citations and inv.citations[0].page == 1  # 0-based pageRef → 1-based
    assert inv.citations[0].bbox is not None
    assert inv.citations[0].confidence == pytest.approx(0.88)


def test_markdown_is_derived_not_a_text_copy():
    resp = _run(GoogleDocumentAIAdapter(client=FakeDocAIClient()), _req())
    md = resp.document.markdown
    assert md != resp.document.text  # zero-derivation copy is gone
    assert "| Region | Revenue |" in md and "| --- " in md  # table rendered as a pipe table
    assert "|" not in (resp.document.text or "")  # native text stays plain
    assert resp.channel_provenance["markdown"] == "derived"
    assert resp.channel_provenance["text"] == "native"


def test_mid_page_table_interleaves_between_paragraphs_by_offset():
    # text offsets: Alpha 0-5, Region 6-12, Revenue 13-20, Omega 21-26
    doc = {
        "text": "Alpha\nRegion\nRevenue\nOmega\n",
        "pages": [
            {
                "pageNumber": 1,
                "dimension": {"width": 612.0, "height": 792.0, "unit": "POINTS"},
                "paragraphs": [_para(0, 5, y=0.1), _para(21, 26, y=0.6)],
                "tables": [
                    {
                        "layout": {
                            "textAnchor": {  # table-level anchor is the reading-order key
                                "textSegments": [{"startIndex": "6", "endIndex": "20"}]
                            },
                            "boundingPoly": {"normalizedVertices": _NV["normalizedVertices"]},
                        },
                        "headerRows": [
                            {
                                "cells": [
                                    {
                                        "layout": {
                                            "textAnchor": {
                                                "textSegments": [
                                                    {"startIndex": "6", "endIndex": "12"}
                                                ]
                                            }
                                        }
                                    },
                                    {
                                        "layout": {
                                            "textAnchor": {
                                                "textSegments": [
                                                    {"startIndex": "13", "endIndex": "20"}
                                                ]
                                            }
                                        }
                                    },
                                ]
                            }
                        ],
                        "bodyRows": [],
                    }
                ],
            }
        ],
    }
    resp = _run_doc(doc)
    blocks = resp.document.pages[0].blocks
    assert [b.type for b in blocks] == [BlockType.TEXT, BlockType.TABLE, BlockType.TEXT]
    assert blocks[0].text == "Alpha" and blocks[2].text == "Omega"
    assert [b.reading_order for b in blocks] == [0, 1, 2]


def test_merged_cells_use_grid_cursor_not_enumeration_indices():
    # A rowspan in row 0 col 0 must push "Z" in row 1 to col 1 (the cursor), not col 0.
    doc = {
        "text": "X\nY\nZ\n",
        "pages": [
            {
                "pageNumber": 1,
                "dimension": {"width": 612.0, "height": 792.0, "unit": "POINTS"},
                "tables": [
                    {
                        "layout": {
                            "boundingPoly": {"normalizedVertices": _NV["normalizedVertices"]}
                        },
                        "headerRows": [],
                        "bodyRows": [
                            {
                                "cells": [
                                    {
                                        "layout": {
                                            "textAnchor": {"textSegments": [{"endIndex": "1"}]}
                                        },
                                        "rowSpan": 2,
                                        "colSpan": 1,
                                    },
                                    {
                                        "layout": {
                                            "textAnchor": {
                                                "textSegments": [
                                                    {"startIndex": "2", "endIndex": "3"}
                                                ]
                                            }
                                        }
                                    },
                                ]
                            },
                            {
                                "cells": [
                                    {
                                        "layout": {
                                            "textAnchor": {
                                                "textSegments": [
                                                    {"startIndex": "4", "endIndex": "5"}
                                                ]
                                            }
                                        }
                                    }
                                ]
                            },
                        ],
                    }
                ],
            }
        ],
    }
    resp = _run_doc(doc)
    table = next(b for b in resp.document.pages[0].blocks if b.type is BlockType.TABLE).table
    assert table.rows == [["X", "Y"], [None, "Z"]]  # cursor skips the rowspan-occupied (1,0)
    z_cell = next(c for c in table.cells if c.text == "Z")
    assert z_cell.col == 1  # enumeration index would (wrongly) place it at col 0


def test_pixel_vertices_fallback_derives_bbox_from_page_dimensions():
    doc = {
        "text": "Hello",
        "pages": [
            {
                "pageNumber": 1,
                "dimension": {"width": 1000.0, "height": 2000.0, "unit": "PIXEL"},
                "paragraphs": [
                    {
                        "layout": {
                            "textAnchor": {"textSegments": [{"endIndex": "5"}]},
                            "boundingPoly": {
                                "vertices": [
                                    {"x": 100, "y": 200},
                                    {"x": 500, "y": 200},
                                    {"x": 500, "y": 400},
                                    {"x": 100, "y": 400},
                                ]
                            },
                        }
                    }
                ],
            }
        ],
    }
    resp = _run_doc(doc)
    para = resp.document.pages[0].blocks[0]
    assert para.bbox is not None  # vertices-only used to yield None
    assert para.bbox.x == pytest.approx(0.1) and para.bbox.y == pytest.approx(0.1)
    assert para.bbox.bbox_native.unit.value == "pixel"


@pytest.mark.live
def test_live_process():  # pragma: no cover
    from tests.live_helpers import run_live, sample_pdf_request, skip_unless_creds

    skip_unless_creds("google-document-ai")
    resp = run_live(
        "google-document-ai", GoogleDocumentAIAdapter(), sample_pdf_request("google-document-ai")
    )
    assert resp.document.text
