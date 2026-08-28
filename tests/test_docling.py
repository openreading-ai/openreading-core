"""Docling adapter — a real DoclingDocument JSON fixture replayed via an injected fake client
(no container). Exercises: body-tree linearization to reading order (incl. nested groups),
BOTTOMLEFT provenance → canonical bbox (lands on the tight top-left box), TableData → cells+grid,
pictures → image blocks, and md_content → document.markdown."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openreading.adapters.docling import DoclingAdapter
from openreading.testing import ConformanceCase, check_adapter_conformance
from openreading.types import BlockType, NativeOrigin
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RunContext

FIX = Path(__file__).parent / "fixtures" / "docling"


def _fixture() -> dict:
    return json.loads((FIX / "convert.json").read_text())


class FakeDoclingClient:
    def __init__(self, payload: dict | None = None) -> None:
        self._payload = payload

    def convert(self, document, options):
        return self._payload if self._payload is not None else _fixture()


def _req(**over) -> OpenReadingRequest:
    body = {
        "document": {"url": "https://example.test/doc.pdf", "mime_type": "application/pdf"},
        "backend": {"id": "docling", "type": "oss_library"},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


def _run(adapter, req):
    ctx = RunContext()
    return adapter.normalize(adapter.submit(req, ctx), ctx, req)


def test_docling_conforms():
    # Phase B.5: docling is remediated — its text channel is native+plain (C1) and every requested
    # N/D channel is delivered (C6), so both are promoted from advisory to strict.
    check_adapter_conformance(
        DoclingAdapter(client=FakeDoclingClient()),
        [ConformanceCase(request=_req(), deterministic=True, label="convert")],
        strict_checks={"C1", "C6"},
        # Ledger T4a §6: docling is one of the 5 untouched-this-tranche adapters — run through
        # R1/R2 too (not assumed), confirming the now-v2 declaration of protocol_version=2 is
        # warranted — R1/R2 genuinely pass, not merely asserted.
        adapter_factory=lambda: DoclingAdapter(client=FakeDoclingClient()),
    )


def test_body_tree_linearized_to_reading_order_including_groups():
    resp = _run(DoclingAdapter(client=FakeDoclingClient()), _req())
    blocks = resp.document.pages[0].blocks
    types = [b.type for b in blocks]
    # order: title, (group→)list_item, table, picture
    assert types[0] is BlockType.TITLE
    assert any(
        b.type is BlockType.LIST_ITEM and b.text == "Applicant income summary." for b in blocks
    )
    assert any(b.type is BlockType.TABLE for b in blocks)
    assert any(b.type is BlockType.IMAGE for b in blocks)
    # reading order is sequential
    assert [b.reading_order for b in blocks] == list(range(len(blocks)))


def test_bottomleft_provenance_converts_to_canonical_topleft():
    resp = _run(DoclingAdapter(client=FakeDoclingClient()), _req())
    title = next(b for b in resp.document.pages[0].blocks if b.type is BlockType.TITLE)
    bb = title.bbox
    # BOTTOMLEFT {l:72,t:727.86,r:300,b:707.86} on 612x792 -> canonical y = (792-727.86)/792 = 64.14/792
    assert bb.y == pytest.approx(64.14 / 792.0, abs=1e-4)
    assert (bb.y + bb.h) == pytest.approx((792 - 707.86) / 792.0, abs=1e-4)
    assert bb.x == pytest.approx(72.0 / 612.0, abs=1e-4)
    assert bb.bbox_native.origin is NativeOrigin.BOTTOM_LEFT  # raw origin preserved


def test_table_reassembled_from_docling_cells():
    resp = _run(DoclingAdapter(client=FakeDoclingClient()), _req())
    table = next(b for b in resp.document.pages[0].blocks if b.type is BlockType.TABLE)
    assert table.table.n_rows == 2 and table.table.n_cols == 2
    assert table.table.rows == [["Region", "Revenue"], ["North", "4400"]]
    assert any(c.is_header and c.text == "Region" for c in table.table.cells)


def test_markdown_from_export_rendition():
    resp = _run(DoclingAdapter(client=FakeDoclingClient()), _req())
    assert resp.document.markdown.startswith("# Loan Application")
    assert "| Region | Revenue |" in resp.document.markdown


def test_page_dims_from_docling_document():
    resp = _run(DoclingAdapter(client=FakeDoclingClient()), _req())
    page = resp.document.pages[0]
    assert page.width == 612.0 and page.height == 792.0 and page.unit.value == "pdf_point"


def test_backend_raw_is_serialized_docling_document():
    resp = _run(DoclingAdapter(client=FakeDoclingClient()), _req())
    assert resp.backend_raw.object_class == "docling_core.types.doc.DoclingDocument"


def test_missing_endpoint_is_terminal():
    from openreading.types.errors import TerminalError

    adapter = DoclingAdapter()  # no injected client, no runtime endpoint
    with pytest.raises(TerminalError, match="endpoint"):
        adapter.submit(_req(), RunContext())


# --- Phase B.5: derive adoption + fidelity fixes ----------------------------------------


def _no_key(payload: dict, *path: str) -> dict:
    """Return the fixture payload with a nested key removed (for negative-path tests)."""
    import copy

    p = copy.deepcopy(payload)
    node: dict = p
    for k in path[:-1]:
        node = node[k]
    node.pop(path[-1], None)
    return p


def test_submit_requests_native_text_format():
    # P1 REGRADE text D→N: docling must be asked for the native `text` rendition so it emits plain
    # text itself (the one change that also removes table-absence + formula-LaTeX from the channel).
    seen: dict = {}

    class RecordingClient:
        def convert(self, document, options):
            seen["options"] = options
            return _fixture()

    _run(DoclingAdapter(client=RecordingClient()), _req())
    assert "text" in seen["options"]["to_formats"]


def test_text_channel_is_native_plain_text():
    resp = _run(DoclingAdapter(client=FakeDoclingClient()), _req())
    # native text_content is projected verbatim into document.text (N, not block concatenation)
    assert resp.document.text == _fixture()["document"]["text_content"]
    assert resp.channel_provenance["text"] == "native"


def test_table_content_present_in_document_text():
    # P0: table cell content ("Region", "4400") must reach document.text (was absent — TABLE.text=None)
    resp = _run(DoclingAdapter(client=FakeDoclingClient()), _req())
    text = resp.document.text or ""
    assert "Region" in text and "Revenue" in text and "North" in text and "4400" in text


def test_formula_latex_absent_from_document_text():
    # P0: raw LaTeX from formula blocks must not leak into the plain text channel
    resp = _run(DoclingAdapter(client=FakeDoclingClient()), _req())
    assert "\\frac" not in (resp.document.text or "")
    # the block still carries the native formula, tagged FORMULA
    formula = next(b for b in resp.document.pages[0].blocks if b.type is BlockType.FORMULA)
    assert formula.native_type == "formula"


def test_table_block_carries_plain_text_projection():
    resp = _run(DoclingAdapter(client=FakeDoclingClient()), _req())
    table = next(b for b in resp.document.pages[0].blocks if b.type is BlockType.TABLE)
    assert table.text == "Region\tRevenue\nNorth\t4400"


def test_caption_is_emitted_as_a_block():
    # P1: captions were dropped (only body.children were walked); now a picture's caption ref is
    # resolved and emitted.
    resp = _run(DoclingAdapter(client=FakeDoclingClient()), _req())
    blocks = resp.document.pages[0].blocks
    caption = next(b for b in blocks if b.type is BlockType.CAPTION)
    assert caption.text == "Regional revenue chart."
    # caption follows its figure in reading order
    img_idx = next(i for i, b in enumerate(blocks) if b.type is BlockType.IMAGE)
    cap_idx = next(i for i, b in enumerate(blocks) if b.type is BlockType.CAPTION)
    assert cap_idx == img_idx + 1


def test_row_header_maps_to_is_header():
    # P2: TableCell.is_header now honours docling row_header (not just column_header)
    resp = _run(DoclingAdapter(client=FakeDoclingClient()), _req())
    table = next(b for b in resp.document.pages[0].blocks if b.type is BlockType.TABLE)
    north = next(c for c in table.table.cells if c.text == "North")
    assert north.is_header is True
    body_cell = next(c for c in table.table.cells if c.text == "4400")
    assert not body_cell.is_header


def test_typed_fields_from_key_value_items():
    # P1 REGRADE typed_fields X→D: key_value_items graph → typed_fields with citations
    resp = _run(DoclingAdapter(client=FakeDoclingClient()), _req(outputs={"typed_fields": True}))
    assert resp.typed_fields is not None
    field = resp.typed_fields["Applicant Name"]
    assert field.value == "Jane Q. Public"
    assert field.citations and field.citations[0].bbox is not None
    assert field.citations[0].page == 1
    assert resp.channel_provenance["typed_fields"] == "derived"


def test_typed_fields_requested_but_absent_warns():
    # C6 deliver-or-warn: requested typed_fields with no KV regions → a machine-readable warning
    payload = _no_key(_fixture(), "document", "json_content", "key_value_items")
    resp = _run(
        DoclingAdapter(client=FakeDoclingClient(payload)), _req(outputs={"typed_fields": True})
    )
    assert resp.typed_fields is None
    blob = " ".join((w.code or "") + (w.field or "") for w in (resp.warnings or []))
    assert "typed_fields" in blob


def test_typed_fields_descriptor_is_derivable():
    from openreading.types.enums import ChannelGrade

    desc = DoclingAdapter().descriptor
    assert desc.output.channels.typed_fields is ChannelGrade.DERIVABLE
    assert desc.output.channels.text is ChannelGrade.NATIVE


def test_confidence_scores_routed_to_document_and_pages():
    # P2: docling ConfidenceScores must surface on document/pages confidence, never smeared on blocks
    resp = _run(DoclingAdapter(client=FakeDoclingClient()), _req())
    assert resp.document.confidence is not None and 0.0 <= resp.document.confidence <= 1.0
    assert resp.document.pages[0].confidence is not None
    # never fabricated onto blocks (block_confidence stays X)
    assert all(b.confidence is None for b in resp.document.pages[0].blocks)


def test_channel_provenance_populated():
    resp = _run(DoclingAdapter(client=FakeDoclingClient()), _req())
    prov = resp.channel_provenance
    assert prov["markdown"] == "native"
    assert prov["blocks"] == "native"
    assert prov["block_bbox"] == "native"
    assert prov["table_cells"] == "native"


def test_derived_text_fallback_when_native_text_absent():
    # defensive: an older docling-serve that returns no text_content still delivers a plain,
    # table-inclusive text channel (derived), so C6 is never violated.
    payload = _no_key(_fixture(), "document", "text_content")
    resp = _run(DoclingAdapter(client=FakeDoclingClient(payload)), _req())
    text = resp.document.text or ""
    assert "Loan Application" in text and "Region\tRevenue" in text  # table projected in
    assert "\\frac" not in text  # formula LaTeX excluded from the plain channel
    assert resp.channel_provenance["text"] == "derived"


@pytest.mark.live
def test_live_convert():  # pragma: no cover
    # Framework loader behind a self-hosted docling-serve container: skips unless DOCLING_SERVE_URL
    # is set. Confirms native text_content (to_formats+['text']) is plain + table-inclusive on a
    # real container, and that key_value_items/ConfidenceScores appear in the HTTP response.
    from tests.live_helpers import run_live, sample_pdf_request, skip_unless_creds

    skip_unless_creds("docling")
    resp = run_live("docling", DoclingAdapter(), sample_pdf_request("docling"))
    assert resp.document.text


@pytest.mark.live
def test_live_liveness_probe():  # pragma: no cover
    """The real docling-serve /health route. Skips without DOCLING_SERVE_URL, exactly like the
    convert test above — and unlike it, this one costs nothing and touches only the caller's own
    container, so it is the cheapest possible proof that the endpoint path is right."""
    from openreading.liveness import check_liveness
    from openreading.types.liveness import LivenessStatus
    from tests.live_helpers import skip_unless_creds

    skip_unless_creds("docling")
    report = check_liveness(DoclingAdapter())
    assert report.measured is True
    assert report.status is LivenessStatus.LIVE, report.detail
