"""Azure DI adapter — tested against REAL captured AnalyzeResult fixtures via an injected fake
client (NO Azure calls). Exercises: LRO POLL (notStarted→running→succeeded) with retry-after,
span-indexed paragraphs→blocks + role mapping, handwriting via style-span overlap, tables→cells,
prebuilt documents[].fields→typed_fields, and the error taxonomy."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from openreading.adapters.azure_document_intelligence import (
    AzureDocumentIntelligenceAdapter,
    PollResp,
)
from openreading.router import FakeClock, await_result
from openreading.testing import ConformanceCase, check_adapter_conformance
from openreading.types import BlockType, JobState, TextType
from openreading.types.errors import RetryableError
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RunContext

FIX = Path(__file__).parent / "fixtures" / "azure-document-intelligence"
DOC_B64 = base64.b64encode(b"%PDF-1.7 fake").decode()


def _fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


class FakeAzureClient:
    """Scripted poll sequence. Each `analyze` starts a FRESH job (resets the poll sequence), so
    the conformance kit's idempotent-resubmit re-drive works like a real second submission."""

    def __init__(self, poll_seq: list[PollResp]):
        self._template = list(poll_seq)
        self._seq: list[PollResp] = []
        self.analyze_calls = []

    def analyze(self, model_id: str, body: dict, content_format: str) -> str:
        self.analyze_calls.append((model_id, content_format))
        self._seq = list(self._template)
        return "https://x.cognitiveservices.azure.com/.../analyzeResults/abc"

    def get(self, operation_location: str) -> PollResp:
        return self._seq.pop(0)


def _succeeded(fixture: str) -> list[PollResp]:
    return [
        PollResp(200, {"status": "running"}, retry_after=1.0),
        PollResp(200, _fixture(fixture)),
    ]


def _resumable_client(poll_seq: list[PollResp]) -> FakeAzureClient:
    """Ledger T4a R1: a fresh instance's client needs a poll sequence loaded WITHOUT ever calling
    `analyze()` on it — the whole point of fresh-instance resume is polling a job this process
    never submitted. `FakeAzureClient` only fills `_seq` inside `analyze()` (modeling "each
    analyze starts a fresh job"), so this pre-loads it directly, standing in for a vendor job that
    already existed before this process ever touched it."""
    client = FakeAzureClient(poll_seq)
    client._seq = list(client._template)
    return client


def _req(op="prebuilt-layout", **over) -> OpenReadingRequest:
    body = {
        "document": {"bytes_base64": DOC_B64, "mime_type": "application/pdf"},
        "backend": {"id": "azure-document-intelligence", "type": "hosted_api", "operation": op},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


async def _run(adapter, req):
    ctx = RunContext()
    job = adapter.submit(req, ctx)
    job = await await_result(adapter, job, ctx=ctx, deadline_ms=1e9, clock=FakeClock())
    return adapter.normalize(job, ctx, req)


def test_azure_conforms():
    # Phase B.3: azure is remediated — the text channel is a plain projection of the native
    # markdown (C1) and every requested N/D channel is delivered (C6), so both are promoted from
    # advisory to strict.
    adapter = AzureDocumentIntelligenceAdapter(client=FakeAzureClient(_succeeded("layout")))
    check_adapter_conformance(
        adapter,
        [ConformanceCase(request=_req(), deterministic=True, label="layout")],
        strict_checks={"C1", "C6"},
        # Ledger T4a (R1/R2/R3): a fresh instance with its own separate fake client — proves
        # resume has no instance-affinity requirement (AC-5) and nothing is cached on self (AC-7).
        adapter_factory=lambda: AzureDocumentIntelligenceAdapter(
            client=_resumable_client(_succeeded("layout"))
        ),
    )


async def test_lro_poll_transitions_running_to_succeeded():
    adapter = AzureDocumentIntelligenceAdapter(client=FakeAzureClient(_succeeded("layout")))
    ctx = RunContext()
    job = adapter.submit(_req(), ctx)
    assert job.state is JobState.RUNNING
    job = await await_result(adapter, job, ctx=ctx, deadline_ms=1e9, clock=FakeClock())
    assert job.state is JobState.SUCCEEDED


async def test_paragraphs_become_blocks_with_roles_and_handwriting():
    adapter = AzureDocumentIntelligenceAdapter(client=FakeAzureClient(_succeeded("layout")))
    resp = await _run(adapter, _req())
    blocks = resp.document.pages[0].blocks
    title = next(b for b in blocks if b.type is BlockType.TITLE)
    assert title.text == "Loan Application"
    # the body paragraph's span overlaps a handwritten style span -> text_type handwriting
    body = next(b for b in blocks if b.text == "Applicant income summary.")
    assert body.text_type is TextType.HANDWRITING


async def test_page_unit_and_polygon_bbox_in_inches():
    adapter = AzureDocumentIntelligenceAdapter(client=FakeAzureClient(_succeeded("layout")))
    resp = await _run(adapter, _req())
    page = resp.document.pages[0]
    assert page.unit.value == "inch" and page.width == 8.5 and page.height == 11.0
    title = next(b for b in page.blocks if b.type is BlockType.TITLE)
    # polygon [1.0,1.0 .. 4.0,1.4] on an 8.5x11 inch page -> x=1/8.5, y=1/11
    assert title.bbox.x == pytest.approx(1.0 / 8.5, abs=1e-4)
    assert title.bbox.y == pytest.approx(1.0 / 11.0, abs=1e-4)
    assert title.bbox.bbox_native.unit.value == "inch"


async def test_table_reassembled_with_headers():
    adapter = AzureDocumentIntelligenceAdapter(client=FakeAzureClient(_succeeded("layout")))
    resp = await _run(adapter, _req())
    table = next(b for b in resp.document.pages[0].blocks if b.type is BlockType.TABLE)
    assert table.table.n_rows == 2 and table.table.n_cols == 2
    assert table.table.rows == [["Region", "Revenue"], ["North", "4400"]]
    assert any(c.is_header and c.text == "Region" for c in table.table.cells)


async def test_markdown_output_mode_sets_document_markdown():
    client = FakeAzureClient(_succeeded("layout"))
    adapter = AzureDocumentIntelligenceAdapter(client=client)
    resp = await _run(adapter, _req())
    assert resp.document.markdown.startswith("# Loan Application")
    assert client.analyze_calls[0][1] == "markdown"  # outputContentFormat=markdown requested


async def test_prebuilt_invoice_fields_to_typed_fields():
    adapter = AzureDocumentIntelligenceAdapter(client=FakeAzureClient(_succeeded("invoice")))
    resp = await _run(adapter, _req(op="prebuilt-invoice"))
    assert resp.document.doc_type.label == "invoice"
    assert resp.typed_fields["VendorName"].value == "Acme Supplies"
    total = resp.typed_fields["InvoiceTotal"]
    assert total.type == "currency"
    assert total.normalized_value == {"amount": 128.5, "currencyCode": "USD"}
    assert total.confidence == 0.95


async def test_selection_mark_becomes_block():
    adapter = AzureDocumentIntelligenceAdapter(client=FakeAzureClient(_succeeded("layout")))
    resp = await _run(adapter, _req())
    assert any(b.type is BlockType.SELECTION_MARK for b in resp.document.pages[0].blocks)


def test_throttling_429_maps_to_retryable_with_retry_after():
    adapter = AzureDocumentIntelligenceAdapter(
        client=FakeAzureClient([PollResp(429, {}, retry_after=5.0)])
    )
    job = adapter.submit(_req(), RunContext())
    with pytest.raises(RetryableError) as exc:
        adapter.poll(job, RunContext())
    assert exc.value.retry_after == 5.0


# --- Phase B.3: derive-library adoption + fidelity fixes ---------------------------------


async def test_default_mode_text_is_derived_plain_not_markdown():
    # P0: markdown keeps native markdown; text is a plain projection (no longer byte-identical).
    adapter = AzureDocumentIntelligenceAdapter(client=FakeAzureClient(_succeeded("layout")))
    resp = await _run(adapter, _req())
    assert resp.document.markdown.startswith("# Loan Application")  # native markdown kept
    assert resp.document.markdown != resp.document.text  # no longer byte-identical
    assert "# Loan" not in resp.document.text  # ATX heading stripped
    assert "| ---" not in resp.document.text and "|" not in resp.document.text  # table markup gone
    assert "Region\tRevenue" in resp.document.text  # table content projected to text (C2)
    assert "Signed by applicant." in resp.document.text
    assert resp.channel_provenance["markdown"] == "native"
    assert resp.channel_provenance["text"] == "derived"


async def test_text_output_mode_is_native_plain_text():
    # P0: analyze/text mode — content is native plain text, markdown absent, provenance text=native.
    client = FakeAzureClient(_succeeded("invoice"))
    adapter = AzureDocumentIntelligenceAdapter(client=client)
    resp = await _run(
        adapter,
        _req(op="prebuilt-invoice", outputs={"markdown": False, "text": True, "blocks": True}),
    )
    assert client.analyze_calls[0][1] == "text"  # outputContentFormat=text requested
    assert resp.document.markdown is None
    assert resp.document.text == "Invoice\nTotal $128.50\nDate 06/30/2026"  # native, verbatim
    assert resp.channel_provenance["text"] == "native"


async def test_block_confidence_derived_from_word_span_overlap():
    # P1: block_confidence=D — min-aggregate the member word confidences via the span-overlap path.
    adapter = AzureDocumentIntelligenceAdapter(client=FakeAzureClient(_succeeded("layout")))
    resp = await _run(adapter, _req())
    title = next(b for b in resp.document.pages[0].blocks if b.type is BlockType.TITLE)
    assert title.confidence == pytest.approx(0.995)  # the one overlapping word's confidence
    # a paragraph with no overlapping words carries no fabricated confidence
    body = next(b for b in resp.document.pages[0].blocks if b.text == "Applicant income summary.")
    assert body.confidence is None


async def test_table_interleaved_before_trailing_paragraph_by_span_order():
    # P2: tables no longer dumped after all paragraphs — order_by_position interleaves on spans.
    adapter = AzureDocumentIntelligenceAdapter(client=FakeAzureClient(_succeeded("layout")))
    resp = await _run(adapter, _req())
    blocks = resp.document.pages[0].blocks
    table_idx = next(i for i, b in enumerate(blocks) if b.type is BlockType.TABLE)
    footer_idx = next(i for i, b in enumerate(blocks) if b.text == "Signed by applicant.")
    assert table_idx < footer_idx  # table (span 47) precedes the footer paragraph (span 100)


async def test_row_header_kind_marks_cell_as_header():
    # P2: rowHeader/columnHeader kinds → is_header (not hard-coded False for row headers).
    adapter = AzureDocumentIntelligenceAdapter(client=FakeAzureClient(_succeeded("layout")))
    resp = await _run(adapter, _req())
    table = next(b for b in resp.document.pages[0].blocks if b.type is BlockType.TABLE).table
    north = next(c for c in table.cells if c.text == "North")
    assert north.is_header is True  # kind == rowHeader
    body_cell = next(c for c in table.cells if c.text == "4400")
    assert not body_cell.is_header


async def test_key_value_pairs_read_into_typed_fields():
    # P2: keyValuePairs (previously unread) → typed_fields, with citations from value geometry.
    adapter = AzureDocumentIntelligenceAdapter(client=FakeAzureClient(_succeeded("layout")))
    resp = await _run(adapter, _req())
    kv = resp.typed_fields["Applicant Name"]
    assert kv.value == "Jane Doe"
    assert kv.confidence == pytest.approx(0.92)
    assert kv.citations and kv.citations[0].page == 1 and kv.citations[0].bbox is not None


async def test_invoice_line_items_recurse_to_nested_native_values():
    # P1: valueArray/valueObject no longer flattened to strings — value is the nested structure.
    adapter = AzureDocumentIntelligenceAdapter(client=FakeAzureClient(_succeeded("invoice")))
    resp = await _run(adapter, _req(op="prebuilt-invoice"))
    items = resp.typed_fields["Items"]
    assert items.value == [
        {"Description": "Widget A", "Amount": {"amount": 100.0, "currencyCode": "USD"}}
    ]


async def test_invoice_field_bounding_regions_become_citations():
    # P1: field boundingRegions → TypedField.citations (page + bbox).
    adapter = AzureDocumentIntelligenceAdapter(client=FakeAzureClient(_succeeded("invoice")))
    resp = await _run(adapter, _req(op="prebuilt-invoice"))
    vendor = resp.typed_fields["VendorName"]
    assert vendor.citations and vendor.citations[0].page == 1
    assert vendor.citations[0].bbox is not None and vendor.citations[0].bbox.page == 1


# --- submit error mapping (the shared _map_error) ----------------------------------------


class _RaisingAnalyzeClient(FakeAzureClient):
    def __init__(self, exc: Exception) -> None:
        super().__init__([])
        self._exc = exc

    def analyze(self, model_id: str, body: dict, content_format: str) -> str:
        raise self._exc


def test_submit_reraises_taxonomy_errors_unchanged():
    boom = RetryableError("throttled", backend_code="429", retry_after=3.0)
    adapter = AzureDocumentIntelligenceAdapter(client=_RaisingAnalyzeClient(boom))
    with pytest.raises(RetryableError) as exc:
        adapter.submit(_req(), RunContext())
    assert exc.value.retry_after == 3.0  # not flattened into a TerminalError


def test_submit_maps_unexpected_error_to_terminal():
    from openreading.types.errors import TerminalError

    adapter = AzureDocumentIntelligenceAdapter(client=_RaisingAnalyzeClient(ValueError("boom")))
    with pytest.raises(TerminalError) as exc:
        adapter.submit(_req(), RunContext())
    assert exc.value.backend_code == "ValueError"  # _map_error uses the exception type name


def test_map_error_never_rewraps_a_taxonomy_error():
    boom = RetryableError("throttled", backend_code="429")
    assert AzureDocumentIntelligenceAdapter()._map_error(boom) is boom


@pytest.mark.live
def test_live_layout():  # pragma: no cover
    from tests.live_helpers import run_live, sample_pdf_request, skip_unless_creds

    skip_unless_creds("azure-document-intelligence")
    adapter = AzureDocumentIntelligenceAdapter()
    resp = run_live(
        "azure-document-intelligence", adapter, sample_pdf_request("azure-document-intelligence")
    )
    assert resp.document.pages
