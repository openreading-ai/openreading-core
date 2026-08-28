"""Qwen-VL adapter — a real chat-completions response replayed via an injected fake client. The
sample PDF is really rasterized (PyMuPDF) into the model message; the fake returns a captured
'qwenvl html' string which is parsed into blocks with bbox. deterministic=False (VLM generation)."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from openreading.adapters.qwen_vl import QwenVLAdapter
from openreading.testing import ConformanceCase, check_adapter_conformance
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types import BlockType, NativeUnit
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RunContext

fitz = pytest.importorskip("fitz", reason="pymupdf (rasterizer) not installed")
FIX = Path(__file__).parent / "fixtures" / "qwen-vl"
PDF_B64 = base64.b64encode(build_sample_pdf()).decode()


class FakeQwenClient:
    def __init__(self, fixture="html_response", raise_exc=None):
        choice = json.loads((FIX / f"{fixture}.json").read_text())["choices"][0]
        self._content = choice["message"]["content"]
        self._finish = choice.get("finish_reason")  # truncation signal (C10)
        self._raise = raise_exc
        self.calls = []

    def chat(self, image_data_url, prompt, model):
        if self._raise:
            raise self._raise
        self.calls.append((prompt, model, image_data_url[:30]))
        return self._content, self._finish


def _req(**over) -> OpenReadingRequest:
    body = {
        "document": {"bytes_base64": PDF_B64, "mime_type": "application/pdf"},
        "backend": {"id": "qwen-vl", "type": "self_hosted_model"},
        "pages": {"ranges": [{"start": 1, "end": 1}]},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


def _run(adapter, req):
    ctx = RunContext()
    return adapter.normalize(adapter.submit(req, ctx), ctx, req)


def test_qwen_conforms_strict_all_modes():
    """B.2 remediation: every mode passes the kit STRICT on C1 (no markup in text) and C6
    (deliver-or-warn). Each mode uses its own fixture/request because the modes are mutually
    exclusive (one prompt/one fixture per adapter). VLM generation → deterministic=False."""
    strict = {"C1", "C6", "C10"}
    # Ledger T4a §6: qwen-vl is one of the 5 untouched-this-tranche adapters — run through R1/R2
    # too (not assumed), confirming the now-v2 declaration of protocol_version=2 is warranted —
    # R1/R2 genuinely pass, not merely asserted.
    check_adapter_conformance(
        QwenVLAdapter(client=FakeQwenClient("html_response")),
        [ConformanceCase(request=_req(), deterministic=False, label="html")],
        strict_checks=strict,
        adapter_factory=lambda: QwenVLAdapter(client=FakeQwenClient("html_response")),
    )
    check_adapter_conformance(
        QwenVLAdapter(client=FakeQwenClient("markdown_response")),
        [
            ConformanceCase(
                request=_req(outputs={"blocks": False, "markdown": True}),
                deterministic=False,
                label="markdown",
            )
        ],
        strict_checks=strict,
        adapter_factory=lambda: QwenVLAdapter(client=FakeQwenClient("markdown_response")),
    )
    check_adapter_conformance(
        QwenVLAdapter(client=FakeQwenClient("extract_response")),
        [
            ConformanceCase(
                request=_req(
                    extraction_schema={
                        "json_schema": {"type": "object", "properties": {"gross_pay": {}}}
                    }
                ),
                deterministic=False,
                label="extract",
            )
        ],
        strict_checks=strict,
        adapter_factory=lambda: QwenVLAdapter(client=FakeQwenClient("extract_response")),
    )


def test_html_parsed_into_typed_blocks_with_bbox():
    resp = _run(QwenVLAdapter(client=FakeQwenClient()), _req())
    blocks = resp.document.pages[0].blocks
    title = next(b for b in blocks if b.type is BlockType.TITLE)
    assert title.text == "Loan Application"
    assert any(b.type is BlockType.TEXT and b.text == "Applicant income summary." for b in blocks)
    assert any(b.type is BlockType.IMAGE for b in blocks)
    # data-bbox "153 100 700 180" normalized by the rasterized page px dims (612x792pt @150dpi=1275x1650)
    assert title.bbox.x == pytest.approx(153 / 1275, abs=1e-3)
    assert title.bbox.bbox_native.unit is NativeUnit.PIXEL


def test_html_table_parsed_to_cells():
    resp = _run(QwenVLAdapter(client=FakeQwenClient()), _req())
    table = next(b for b in resp.document.pages[0].blocks if b.type is BlockType.TABLE)
    assert table.table.rows == [["Region", "Revenue"], ["North", "4400"]]
    assert table.table.n_rows == 2 and table.table.n_cols == 2


def test_confidence_never_fabricated():
    resp = _run(QwenVLAdapter(client=FakeQwenClient()), _req())
    for b in resp.document.pages[0].blocks:
        assert b.confidence is None  # token-stream VLM: no calibrated scores
    # and the approximate-bbox-space caveat is surfaced
    assert any(w.field == "block_bbox" for w in resp.warnings)


def test_real_rasterization_happens_and_prompt_is_html():
    client = FakeQwenClient()
    QwenVLAdapter(client=client).submit(_req(), RunContext())
    prompt, model, url_prefix = client.calls[0]
    assert prompt == "QwenVL HTML"
    assert url_prefix.startswith("data:image/png;base64")  # real rasterized page sent


def test_extraction_schema_drives_json_typed_fields():
    req = _req(
        extraction_schema={"json_schema": {"type": "object", "properties": {"gross_pay": {}}}}
    )
    resp = _run(QwenVLAdapter(client=FakeQwenClient(fixture="extract_response")), req)
    assert resp.typed_fields["gross_pay"].value == "4400.00"
    assert resp.typed_fields["borrower_name"].value == "Jane Doe"


def test_cold_start_503_maps_to_retryable():
    from openreading.types.errors import RetryableError

    class ColdStart(RetryableError):
        pass

    adapter = QwenVLAdapter(
        client=FakeQwenClient(raise_exc=RetryableError("loading", retry_after=5.0))
    )
    with pytest.raises(RetryableError):
        adapter.submit(_req(), RunContext())


def test_markdown_mode_when_blocks_not_requested():
    client = FakeQwenClient()
    QwenVLAdapter(client=client).submit(
        _req(outputs={"blocks": False, "markdown": True}), RunContext()
    )
    assert client.calls[0][0] == "QwenVL Markdown"


# --- Phase B.2: derive-library adoption + fidelity fixes -----------------------------------

from openreading.types.enums import ResponseState  # noqa: E402


def _md_req():
    return _req(outputs={"blocks": False, "markdown": True})


def test_markdown_mode_text_is_plain_not_raw_gfm():
    """P0/C1: markdown mode must project the model's GFM to PLAIN text via derive.md_to_text —
    the raw markdown (headings, pipe tables) belongs only to the markdown channel."""
    resp = _run(QwenVLAdapter(client=FakeQwenClient(fixture="markdown_response")), _md_req())
    text = resp.document.text
    # no markdown syntax leaks into the text channel
    assert "#" not in text and "|" not in text and "---" not in text
    assert "Loan Application" in text and "Applicant income summary." in text
    # the pipe table becomes tab-joined plain rows (C2 completeness), never pipe markup
    assert "Region\tRevenue" in text and "North\t4400" in text
    # the markdown channel keeps the NATIVE raw GFM
    assert resp.document.markdown.startswith("# Loan Application")
    assert "| Region | Revenue |" in resp.document.markdown


def test_extract_mode_does_not_leak_json_into_text():
    """P0: extract mode must NOT append the fenced JSON blob to the text channel — the typed
    fields already carry the extraction; text/markdown are honestly absent."""
    req = _req(
        extraction_schema={"json_schema": {"type": "object", "properties": {"gross_pay": {}}}}
    )
    resp = _run(QwenVLAdapter(client=FakeQwenClient(fixture="extract_response")), req)
    assert resp.document.text is None  # no JSON blob dumped into text
    assert resp.document.markdown is None
    assert resp.typed_fields["gross_pay"].value == "4400.00"


def test_finish_reason_length_yields_partial_not_succeeded():
    """P0/C10 status.honest: a `finish_reason == length` truncation must surface as PARTIAL (and a
    warning) — never a bare SUCCEEDED with silently incomplete content."""
    resp = _run(QwenVLAdapter(client=FakeQwenClient(fixture="truncated_response")), _req())
    assert resp.status.state is ResponseState.PARTIAL
    assert any("truncat" in (w.message or "").lower() for w in (resp.warnings or []))


def test_finish_reason_stop_stays_succeeded():
    resp = _run(QwenVLAdapter(client=FakeQwenClient(fixture="html_response")), _req())
    assert resp.status.state is ResponseState.SUCCEEDED


def test_html_table_th_and_spans_via_derive():
    """P1: html-mode tables build through derive.html_table_to_table — is_header comes from <th>
    (never fabricated row-0) and rowspan/colspan yield true grid coordinates."""
    resp = _run(QwenVLAdapter(client=FakeQwenClient(fixture="html_spans_response")), _req())
    table = next(b for b in resp.document.pages[0].blocks if b.type is BlockType.TABLE).table
    assert table.n_rows == 3 and table.n_cols == 3
    # header row is the <th> row (row 0), and ONLY that row
    header_rows = {c.row for c in table.cells if c.is_header}
    assert header_rows == {0}
    # the rowspan=2 "North" cell keeps its span and leaves the covered position None
    north = next(c for c in table.cells if c.text == "North")
    assert north.row_span == 2 and north.row == 1 and north.col == 0
    assert table.rows[2][0] is None  # covered by North's rowspan
    assert table.rows == [["Region", "Q1", "Q2"], ["North", "100", "200"], [None, "150", "250"]]


def test_html_table_content_reaches_document_text():
    """P1/C2: html-mode table cell content must appear in document.text (table_to_text), not only
    in the markdown channel."""
    resp = _run(QwenVLAdapter(client=FakeQwenClient(fixture="html_response")), _req())
    text = resp.document.text
    assert "Region\tRevenue" in text and "North\t4400" in text
    assert "<table>" not in text and "|" not in text  # plain projection, no markup


def test_source_page_numbers_preserved_under_subsetting():
    """P2/C9: requesting page 2 must report page_number 2 (and bbox.page 2), not a 1..k renumber."""
    resp = _run(
        QwenVLAdapter(client=FakeQwenClient(fixture="html_response")),
        _req(pages={"ranges": [{"start": 2, "end": 2}]}),
    )
    assert resp.document.pages[0].page_number == 2
    title = next(b for b in resp.document.pages[0].blocks if b.type is BlockType.TITLE)
    assert title.bbox.page == 2


def test_extract_mode_warns_channels_it_cannot_produce():
    """P2/C6 mode-exclusivity: extract mode cannot produce text/markdown/blocks; requested-but-
    unproducible channels must be named in machine-readable warnings (never silently dropped)."""
    req = _req(
        extraction_schema={"json_schema": {"type": "object", "properties": {"gross_pay": {}}}}
    )
    resp = _run(QwenVLAdapter(client=FakeQwenClient(fixture="extract_response")), req)
    warned = {w.field for w in (resp.warnings or [])}
    assert {"text", "markdown", "blocks"} <= warned


def test_channel_provenance_reflects_active_mode():
    html = _run(QwenVLAdapter(client=FakeQwenClient(fixture="html_response")), _req())
    assert html.channel_provenance["blocks"] == "native"
    assert html.channel_provenance["block_bbox"] == "native"
    assert html.channel_provenance["markdown"] == "derived"  # derived from HTML layout
    assert html.channel_provenance["text"] == "derived"
    assert html.channel_provenance["table_cells"] == "derived"

    md = _run(QwenVLAdapter(client=FakeQwenClient(fixture="markdown_response")), _md_req())
    assert md.channel_provenance["markdown"] == "native"  # the model emitted markdown
    assert md.channel_provenance["text"] == "derived"


# --- live (keyed; skipped without QWEN_VL_ENDPOINT) -------------------------------------


@pytest.mark.live
def test_live_liveness_probe():  # pragma: no cover
    """The real OpenAI-compatible GET /v1/models on a self-hosted endpoint: free, non-generating,
    and it also proves WHICH model is loaded, which the endpoint URL alone never could."""
    from openreading.liveness import check_liveness
    from openreading.types.liveness import LivenessStatus
    from tests.live_helpers import skip_unless_creds

    skip_unless_creds("qwen-vl")
    report = check_liveness(QwenVLAdapter())
    assert report.measured is True
    assert report.status is LivenessStatus.LIVE, report.detail
