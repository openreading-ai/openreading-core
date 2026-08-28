"""PyMuPDF adapter — EXECUTED for real against the generated 2-page test PDF (not mocked).
Asserts conformance + that the normalized geometry matches the empirically-verified internal/research/openreading/_data/live_runs.md
coordinates (title span [72, 58.5, 337.66, 85.98] on a 612x792 page; table cells)."""

from __future__ import annotations

import base64
import threading

import pytest

from openreading.adapters.pymupdf import PyMuPDFAdapter
from openreading.derive import md_table_to_table
from openreading.testing import ConformanceCase, check_adapter_conformance
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types import BlockType, NativeOrigin, NativeUnit
from openreading.types.enums import JobState, WaitMode
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RawResult, RunContext

fitz = pytest.importorskip("fitz", reason="pymupdf extra not installed")

PDF_BYTES = build_sample_pdf()
PDF_B64 = base64.b64encode(PDF_BYTES).decode()


def _req(**over) -> OpenReadingRequest:
    body = {
        "document": {"bytes_base64": PDF_B64, "mime_type": "application/pdf"},
        "backend": {"id": "pymupdf", "type": "oss_library"},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


def _run(req):
    a = PyMuPDFAdapter()
    ctx = RunContext()
    job = a.submit(req, ctx)
    return a, a.normalize(job, ctx, req)


def test_pymupdf_conforms():
    check_adapter_conformance(
        PyMuPDFAdapter(),
        [ConformanceCase(request=_req(), deterministic=True, label="test.pdf")],
        strict_checks={"C1", "C6"},
        # Ledger T4a §6: pymupdf is one of the 5 untouched-this-tranche adapters — run through
        # R1/R2 too (not assumed), confirming the now-v2 declaration of protocol_version=2 is
        # warranted — R1/R2 genuinely pass, not merely asserted.
        adapter_factory=lambda: PyMuPDFAdapter(),
    )


def test_health_reports_version():
    h = PyMuPDFAdapter().health()
    assert h.ready and h.version


def test_extracts_two_pages_text_and_title():
    _, resp = _run(_req())
    assert resp.document.page_count == 2
    assert len(resp.document.pages) == 2
    assert "OpenReading Test Document" in resp.document.text
    # the title block is classified TITLE (matches doc metadata title)
    p1 = resp.document.pages[0]
    titles = [b for b in p1.blocks if b.type is BlockType.TITLE]
    assert len(titles) == 1
    assert titles[0].text == "OpenReading Test Document"


def test_title_bbox_matches_live_run_ascender_inflation():
    _, resp = _run(_req())
    title = next(b for b in resp.document.pages[0].blocks if b.type is BlockType.TITLE)
    bb = title.bbox
    # canonical: ascender-inflated top 58.5/792, not the tight 64.14 glyph box
    assert bb.y == pytest.approx(58.5 / 792.0, abs=1e-4)
    assert (bb.y + bb.h) == pytest.approx(85.98 / 792.0, abs=1e-3)
    assert bb.x == pytest.approx(72.0 / 612.0, abs=1e-4)
    # raw geometry preserved verbatim for audit
    assert bb.bbox_native.origin is NativeOrigin.TOP_LEFT
    assert bb.bbox_native.unit is NativeUnit.PDF_POINT
    assert bb.bbox_native.coords[1] == pytest.approx(58.5, abs=1e-2)


def test_table_extracted_with_cells_and_rows():
    _, resp = _run(_req())
    tables = [b for b in resp.document.pages[0].blocks if b.type is BlockType.TABLE]
    assert len(tables) == 1
    t = tables[0].table
    assert t.n_rows == 4 and t.n_cols == 3
    assert t.rows[0] == ["Region", "Units", "Revenue"]
    assert t.rows[3] == ["West", "42", "1650"]
    # cell (0,0) canonical bbox corresponds to native (72,280,192,308)
    c00 = next(c for c in t.cells if c.row == 0 and c.col == 0)
    assert c00.text == "Region" and c00.is_header
    assert c00.bbox.bbox_native.coords == pytest.approx([72.0, 280.0, 192.0, 308.0])
    assert c00.bbox.y == pytest.approx(280.0 / 792.0, abs=1e-4)


def test_table_text_not_double_counted_as_paragraphs():
    _, resp = _run(_req())
    # "Region"/"North" etc. appear only inside the table block, not as stray TEXT blocks
    text_blocks = [b for b in resp.document.pages[0].blocks if b.type is BlockType.TEXT]
    joined = " ".join(b.text for b in text_blocks)
    assert "Region" not in joined and "4400" not in joined


def test_page2_has_image_block():
    _, resp = _run(_req())
    p2 = resp.document.pages[1]
    assert any(b.type is BlockType.IMAGE for b in p2.blocks)


def test_confidence_is_absent_and_warned():
    _, resp = _run(_req())
    for page in resp.document.pages:
        for b in page.blocks:
            assert b.confidence is None  # deterministic parser: never fabricated
    assert any(w.field == "block_confidence" for w in resp.warnings)


def test_backend_raw_preserves_native_dict_and_can_be_dropped():
    _, resp = _run(_req())
    assert resp.backend_raw.object_class == "fitz.Page.get_text.dict"
    assert resp.backend_raw.payload["metadata"]["title"] == "OpenReading Test Document"
    _, resp2 = _run(_req(outputs={"include_backend_raw": False}))
    assert resp2.backend_raw is None


def test_markdown_derived_with_table():
    _, resp = _run(_req())
    assert resp.document.markdown.startswith("# OpenReading Test Document")
    assert "| Region | Units | Revenue |" in resp.document.markdown


def test_page_selection_limits_pages():
    _, resp = _run(_req(pages={"ranges": [{"start": 2, "end": 2}]}))
    assert len(resp.document.pages) == 1
    assert resp.document.pages[0].page_number == 2


# --- Phase B.5: derive adoption — escape, interleave, real Table.header ----------------


def _build_interleave_pdf() -> bytes:
    """A single-page PDF with a title, a bordered 2x2 table high on the page, and a paragraph
    well BELOW the table — the reading-order interleave case the stock sample can't exercise
    (its table is already last). One cell carries a literal '|' to prove pipe escaping."""
    doc = fitz.open()
    doc.set_metadata({"title": "Interleave Doc", "creationDate": "D:20260721000000Z"})
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 80), "Interleave Doc", fontsize=18, fontname="helv")
    tbl = [["Region", "A|B"], ["North", "9"]]
    x0, y0, x1, y1 = 72.0, 120.0, 312.0, 200.0
    rh, cw = (y1 - y0) / 2, (x1 - x0) / 2
    for r in range(2):
        for c in range(2):
            cx0, cy0 = x0 + c * cw, y0 + r * rh
            page.draw_rect(fitz.Rect(cx0, cy0, cx0 + cw, cy0 + rh), color=(0, 0, 0), width=0.8)
            page.insert_text((cx0 + 4, cy0 + rh - 8), tbl[r][c], fontsize=10, fontname="helv")
    page.insert_text((72, 500), "Trailing paragraph after the table body.", fontsize=11)
    data = doc.tobytes()
    doc.close()
    return bytes(data)


def _run_pdf(pdf_bytes: bytes, **over):
    b64 = base64.b64encode(pdf_bytes).decode()
    return _run(_req(document={"bytes_base64": b64, "mime_type": "application/pdf"}, **over))


def _normalize_payload(payload: dict):
    """Drive normalize() directly on a hand-built pymupdf intermediate payload (mirrors the
    shape _extract emits) so we can pin cell/header handling without a real fixture."""
    a = PyMuPDFAdapter()
    job = a.new_job(WaitMode.INLINE, state=JobState.SUCCEEDED)
    job.raw = RawResult(
        payload=payload,
        media_type="application/vnd.openreading.pymupdf+json",
        object_class="fitz.Page.get_text.dict",
        encoding="json_serialized_object",
    )
    return a, a.normalize(job, RunContext(), _req())


def test_reading_order_interleaves_trailing_text_after_table():
    _, resp = _run_pdf(_build_interleave_pdf())
    ordered = sorted(resp.document.pages[0].blocks, key=lambda b: b.reading_order)
    kinds = [b.type for b in ordered]
    assert BlockType.TABLE in kinds
    ti = kinds.index(BlockType.TABLE)
    trailing = next(
        i
        for i, b in enumerate(ordered)
        if b.type is BlockType.TEXT and "Trailing" in (b.text or "")
    )
    assert trailing > ti  # the paragraph below the table comes AFTER it in reading order


def test_markdown_places_table_before_trailing_text():
    _, resp = _run_pdf(_build_interleave_pdf())
    md = resp.document.markdown
    assert md.index("Region") < md.index("Trailing paragraph")  # table md precedes trailing text


def test_pipe_in_cell_is_escaped_in_markdown():
    _, resp = _run_pdf(_build_interleave_pdf())
    md = resp.document.markdown
    assert r"A\|B" in md  # literal '|' escaped, not a raw column break
    assert "A|B" not in md.replace(r"A\|B", "")  # no unescaped survivor
    tbl_md = next(b for b in resp.document.pages[0].blocks if b.type is BlockType.TABLE).markdown
    parsed = md_table_to_table(tbl_md)  # escaped md still round-trips to the real grid
    assert parsed is not None
    assert "A|B" in [c for row in (parsed.rows or []) for c in row]  # unescaped back to cell value


def test_is_header_sourced_from_pymupdf_header_not_row_zero():
    # a contrived table whose pymupdf-detected header is ROW 1, not row 0 — proves is_header is
    # read from Table.header cell geometry, never fabricated as `row == 0`.
    r0 = [[72.0, 100.0, 172.0, 120.0], [172.0, 100.0, 272.0, 120.0]]
    r1 = [[72.0, 120.0, 172.0, 140.0], [172.0, 120.0, 272.0, 140.0]]
    payload = {
        "metadata": {"title": ""},
        "page_count": 1,
        "pages": [
            {
                "number": 0,
                "width": 612.0,
                "height": 792.0,
                "text": "Body A Body B Head A Head B",
                "blocks": [],
                "tables": [
                    {
                        "bbox": [72.0, 100.0, 272.0, 140.0],
                        "row_count": 2,
                        "col_count": 2,
                        "extract": [["Body A", "Body B"], ["Head A", "Head B"]],
                        "cells": r0 + r1,
                        "header": {
                            "external": False,
                            "names": ["Head A", "Head B"],
                            "cells": r1,  # header is the SECOND row
                        },
                    }
                ],
            }
        ],
    }
    _, resp = _normalize_payload(payload)
    tbl = next(b for b in resp.document.pages[0].blocks if b.type is BlockType.TABLE).table

    def cell(rr: int, cc: int):
        return next(x for x in tbl.cells if x.row == rr and x.col == cc)

    assert cell(1, 0).is_header and cell(1, 1).is_header  # the row Table.header names
    assert not cell(0, 0).is_header and not cell(0, 1).is_header  # row 0 is body → NOT header
    assert cell(1, 0).text == "Head A"  # text↔position stays coherent


def test_table_cell_bbox_matches_position_column_major_cells():
    # pymupdf emits t.cells column-major; the (row,col)→bbox mapping must follow geometry so
    # every cell's bbox is its own, not a divmod(i, col_count) mislabel (only (0,0) was right).
    _, resp = _run(_req())
    tbl = next(b for b in resp.document.pages[0].blocks if b.type is BlockType.TABLE).table
    c02 = next(c for c in tbl.cells if c.row == 0 and c.col == 2)
    assert c02.text == "Revenue"
    # (row 0, col 2) native bbox on the sample table is (312,280,432,308), not a shifted one
    assert c02.bbox.bbox_native.coords == pytest.approx([312.0, 280.0, 432.0, 308.0])
    c10 = next(c for c in tbl.cells if c.row == 1 and c.col == 0)
    assert c10.text == "North"
    assert c10.bbox.bbox_native.coords == pytest.approx([72.0, 308.0, 192.0, 336.0])


def test_channel_provenance_marks_native_and_derived():
    _, resp = _run(_req())
    cp = resp.channel_provenance
    assert cp["text"] == "native"
    assert cp["markdown"] == "derived"  # we build the pipe tables + escaped body
    assert cp["blocks"] == "native"
    assert cp["block_bbox"] == "native"
    assert cp["table_cells"] == "native"


def test_block_granularity_declared():
    assert PyMuPDFAdapter().descriptor.output.block_granularity == "paragraph"


# --- thread safety: pymupdf's process-global find_tables() state -----------------------


def _title_y_and_rows() -> tuple[float, float, list]:
    """One full extraction → (native title top, canonical title y, table rows)."""
    _, resp = _run(_req())
    page = resp.document.pages[0]
    title = next(b for b in page.blocks if b.type is BlockType.TITLE)
    table = next(b for b in page.blocks if b.type is BlockType.TABLE).table
    return title.bbox.bbox_native.coords[1], title.bbox.y, table.rows


def _assert_exact_geometry(native_y: float, canon_y: float) -> None:
    assert native_y == pytest.approx(58.5, abs=1e-2)  # ascender-inflated, NOT the 64.35 tight box
    assert canon_y == pytest.approx(58.5 / 792.0, abs=1e-4)


def test_concurrent_extraction_keeps_exact_geometry_and_unpoisoned_global():
    """find_tables() flips pymupdf's PROCESS-GLOBAL small_glyph_heights (not thread-local) for its
    whole duration and restores it non-atomically. Unserialized, a concurrent get_text('dict')
    reads tight glyph boxes (title top 64.35, not 58.5) and interleaved restores leave the flag
    stuck True — every LATER parse, single-threaded or not, is silently wrong with no error.
    Reachable from batch --jobs>1 and the server's concurrent dispatch."""
    n_threads, per_thread = 8, 4
    ready = threading.Barrier(n_threads, timeout=30)
    guard = threading.Lock()
    results: list[tuple[float, float, list]] = []
    errors: list[Exception] = []

    def worker() -> None:
        try:
            ready.wait()  # every thread contends from the same instant
            for _ in range(per_thread):
                out = _title_y_and_rows()
                with guard:
                    results.append(out)
        except Exception as e:  # noqa: BLE001 - re-raised on the main thread as a failure
            with guard:
                errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert not errors, errors
    assert len(results) == n_threads * per_thread
    for native_y, canon_y, rows in results:
        _assert_exact_geometry(native_y, canon_y)
        assert rows[0] == ["Region", "Units", "Revenue"]  # CHARS not clobbered mid-extract
        assert rows[3] == ["West", "42", "1650"]

    _assert_exact_geometry(*_title_y_and_rows()[:2])  # a later SERIAL parse is still correct
