"""AWS Textract adapter — tested against REAL captured Block-graph fixtures via an injected
fake client (NO AWS calls). Exercises: sync Block-graph reassembly (LAYOUT/FORMS/TABLE/
SIGNATURE), async POLL with NextToken pagination, Expense/ID/Lending typed_fields, and the
error taxonomy. A live smoke test is gated behind @pytest.mark.live + AWS creds."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from openreading.adapters.aws_textract import AWSTextractAdapter
from openreading.router import FakeClock, await_result
from openreading.testing import ConformanceCase, check_adapter_conformance
from openreading.types import BlockType, JobState
from openreading.types.errors import RetryableError, TerminalError
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RunContext

FIX = Path(__file__).parent / "fixtures" / "aws-textract"
DOC_B64 = base64.b64encode(b"%PDF-1.7 fake").decode()


def _fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


class FakeTextractClient:
    """Replays fixtures; can be scripted with a queue of get_* responses and an exception to raise
    (for the error-taxonomy tests). Records every method name in `calls` so the async-routing tests
    can assert the operation-appropriate AWS client method was invoked (P1 live-lane-gated fix)."""

    def __init__(self, get_pages=None, raise_exc=None):
        self._get_pages = list(get_pages or [])
        self._raise = raise_exc
        self.calls: list[str] = []

    def _maybe_raise(self):
        if self._raise:
            raise self._raise

    def analyze_document(self, **kw):
        self.calls.append("analyze_document")
        self._maybe_raise()
        return _fixture("analyze_document")

    def detect_document_text(self, **kw):
        self.calls.append("detect_document_text")
        return _fixture("analyze_document")

    def analyze_expense(self, **kw):
        self.calls.append("analyze_expense")
        return _fixture("analyze_expense")

    def analyze_id(self, **kw):
        self.calls.append("analyze_id")
        return _fixture("analyze_id")

    def start_document_analysis(self, **kw):
        self.calls.append("start_document_analysis")
        self._maybe_raise()
        return {"JobId": "job-abc"}

    def get_document_analysis(self, **kw):
        self.calls.append("get_document_analysis")
        return self._get_pages.pop(0)

    # DetectDocumentText async → StartDocumentTextDetection / GetDocumentTextDetection
    def start_document_text_detection(self, **kw):
        self.calls.append("start_document_text_detection")
        self._maybe_raise()
        return {"JobId": "job-detect"}

    def get_document_text_detection(self, **kw):
        self.calls.append("get_document_text_detection")
        return self._get_pages.pop(0)

    # AnalyzeLending async → StartLendingAnalysis / GetLendingAnalysis
    def start_lending_analysis(self, **kw):
        self.calls.append("start_lending_analysis")
        self._maybe_raise()
        return {"JobId": "job-lending"}

    def get_lending_analysis(self, **kw):
        self.calls.append("get_lending_analysis")
        return self._get_pages.pop(0)


class FakeS3:
    def upload(self, data: bytes, key: str) -> dict:
        return {"Bucket": "test-bucket", "Name": key}


class BadDocumentException(Exception):
    pass


class ThrottlingException(Exception):
    pass


def _req(op="AnalyzeDocument", **over) -> OpenReadingRequest:
    body = {
        "document": {"bytes_base64": DOC_B64, "mime_type": "application/pdf"},
        "backend": {"id": "aws-textract", "type": "hosted_api", "operation": op},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


def _run_sync(adapter, req):
    ctx = RunContext()
    job = adapter.submit(req, ctx)
    return adapter.normalize(job, ctx, req)


# --- conformance ------------------------------------------------------------------------


def test_textract_conforms():
    # Phase B.3: textract is remediated — table content reaches document.text as plain tab-joined
    # rows (C1/C2) and its requested N/D channels are delivered (C6), so both are promoted from
    # advisory to strict.
    adapter = AWSTextractAdapter(client=FakeTextractClient())
    check_adapter_conformance(
        adapter,
        [ConformanceCase(request=_req(), deterministic=True, label="analyze_document")],
        strict_checks={"C1", "C6"},
        # Ledger T4a (R1/R2/R3): a fresh instance with its own separate fake client — proves
        # resume has no instance-affinity requirement (AC-5) and nothing is cached on self (AC-7).
        adapter_factory=lambda: AWSTextractAdapter(client=FakeTextractClient()),
    )


def test_textract_resumes_a_job_on_a_fresh_process_equivalent_instance():
    """Ledger T4a's own G6 demo (internal/design/ledger.md §13): resume a Textract job in a fresh
    process. R1/R2/R3 (AC-5/AC-6/AC-7): submit an async AnalyzeDocument job on instance A,
    round-trip the Job through to_dict/json.dumps/json.loads/from_dict, and drive it to SUCCEEDED
    on a brand-new instance B sharing no Python-level state with A — each instance gets its own
    fresh FakeTextractClient/FakeS3, never a shared one."""
    doc = _fixture("analyze_document")
    page = {"JobStatus": "SUCCEEDED", "DocumentMetadata": {"Pages": 1}, "Blocks": doc["Blocks"]}

    def make_adapter() -> AWSTextractAdapter:
        return AWSTextractAdapter(client=FakeTextractClient(get_pages=[dict(page)]), s3=FakeS3())

    req = _req(op="AnalyzeDocument", **{"async": {"mode": "async"}})
    check_adapter_conformance(
        make_adapter(),
        # not deterministic: a resubmit-and-redrive on the SAME instance would pop from the same
        # single-shot fake queue twice — R1's own resume-on-a-DIFFERENT-instance is what this test
        # is actually proving, not idempotent resubmit (already covered by test_textract_conforms).
        [ConformanceCase(request=req, deterministic=False, label="async_resume")],
        strict_checks={"C1", "C6"},
        adapter_factory=make_adapter,
    )


# --- sync Block-graph reassembly --------------------------------------------------------


def test_analyze_document_reassembles_layout_forms_table_signature():
    adapter = AWSTextractAdapter(client=FakeTextractClient())
    resp = _run_sync(adapter, _req())
    blocks = resp.document.pages[0].blocks
    types = [b.type for b in blocks]
    assert BlockType.TITLE in types  # LAYOUT_TITLE -> child LINE text
    title = next(b for b in blocks if b.type is BlockType.TITLE)
    assert title.text == "Loan Application"
    assert title.confidence == pytest.approx(0.994)  # 99.4/100
    # LAYOUT_TEXT
    assert any(
        b.type is BlockType.TEXT and b.text == "Applicant income summary follows." for b in blocks
    )
    # FORMS -> typed_fields (KEY child words -> VALUE child words), colon stripped
    assert resp.typed_fields["Total"].value == "$4,400.00"
    assert resp.typed_fields["Total"].confidence == pytest.approx(0.965)
    # SIGNATURE block
    assert any(b.type is BlockType.SIGNATURE for b in blocks)


def test_table_reassembled_from_cells_with_headers():
    adapter = AWSTextractAdapter(client=FakeTextractClient())
    resp = _run_sync(adapter, _req())
    table = next(b for b in resp.document.pages[0].blocks if b.type is BlockType.TABLE)
    assert table.table.n_rows == 2 and table.table.n_cols == 2
    assert table.table.rows == [["Region", "Revenue"], ["North", "4400"]]
    hdr = next(c for c in table.table.cells if c.row == 0 and c.col == 0)
    assert hdr.text == "Region" and hdr.is_header


def test_bbox_normalized_geometry_is_canonical_with_native():
    adapter = AWSTextractAdapter(client=FakeTextractClient())
    resp = _run_sync(adapter, _req())
    title = next(b for b in resp.document.pages[0].blocks if b.type is BlockType.TITLE)
    bb = title.bbox
    # Textract normalized {Left:0.1,Top:0.05,Width:0.5,Height:0.04} -> canonical passthrough
    assert bb.x == pytest.approx(0.1) and bb.y == pytest.approx(0.05)
    assert bb.w == pytest.approx(0.5) and bb.h == pytest.approx(0.04)
    assert bb.bbox_native.unit.value == "normalized"
    assert bb.polygon is not None  # polygon carried through


# --- async POLL + NextToken pagination --------------------------------------------------


async def test_async_poll_paginates_nexttoken_to_completion():
    doc = _fixture("analyze_document")
    half1 = {"JobStatus": "IN_PROGRESS"}
    half2 = {
        "JobStatus": "SUCCEEDED",
        "DocumentMetadata": {"Pages": 1},
        "Blocks": doc["Blocks"][:10],
        "NextToken": "tok-2",
    }
    half3 = {"JobStatus": "SUCCEEDED", "Blocks": doc["Blocks"][10:]}  # no NextToken -> done
    adapter = AWSTextractAdapter(
        client=FakeTextractClient(get_pages=[half1, half2, half3]), s3=FakeS3()
    )
    req = _req(op="AnalyzeDocument", **{"async": {"mode": "async"}})
    job = adapter.submit(req, RunContext())
    assert job.state is JobState.RUNNING and job.backend_job_id == "job-abc"
    clock = FakeClock()
    job = await await_result(adapter, job, ctx=RunContext(), deadline_ms=1e9, clock=clock)
    assert job.state is JobState.SUCCEEDED
    resp = adapter.normalize(job, RunContext(), req)
    # all blocks reassembled across the two SUCCEEDED pages
    assert any(b.type is BlockType.TITLE for b in resp.document.pages[0].blocks)
    assert any(b.type is BlockType.TABLE for b in resp.document.pages[0].blocks)


# --- typed APIs -------------------------------------------------------------------------


def test_analyze_expense_typed_fields():
    adapter = AWSTextractAdapter(client=FakeTextractClient())
    resp = _run_sync(adapter, _req(op="AnalyzeExpense"))
    assert resp.typed_fields["TOTAL"].value == "128.50"
    assert resp.typed_fields["VENDOR_NAME"].value == "Acme Supplies"
    assert resp.document.pages is None  # pure typed-field response (anyOf via typed_fields)


def test_analyze_id_typed_fields_with_normalized_value():
    adapter = AWSTextractAdapter(client=FakeTextractClient())
    resp = _run_sync(adapter, _req(op="AnalyzeID"))
    dob = resp.typed_fields["DATE_OF_BIRTH"]
    assert dob.value == "01/15/1985"
    assert dob.normalized_value == "1985-01-15T00:00:00"


def test_lending_classification_and_fields():
    adapter = AWSTextractAdapter(client=FakeTextractClient())
    # lending is async-only; unit-test the normalize mapping directly from the fixture
    from openreading.types.runtime import RawResult

    job = adapter.new_job(__import__("openreading.types", fromlist=["WaitMode"]).WaitMode.POLL)
    job.raw = RawResult(payload=_fixture("lending"), object_class="AnalyzeLending", encoding="json")
    resp = adapter.normalize(job, RunContext(), _req(op="AnalyzeLending"))
    assert resp.document.doc_type.label == "PAYSLIPS"  # page classification (lending wedge)
    assert resp.typed_fields["GROSS_PAY"].value == "4400.00"


# --- Phase B.3: derive adoption, merged cells, citations, async routing -----------------


def _normalize_raw(adapter, payload: dict, op: str, req=None):
    """Build a Job carrying `payload` as `op`'s raw result and normalize it directly (mirrors the
    lending unit test) — for exercising normalize() over hand-built provider shapes."""
    from openreading.types import WaitMode
    from openreading.types.runtime import RawResult

    job = adapter.new_job(WaitMode.INLINE, state=JobState.SUCCEEDED)
    job.raw = RawResult(payload=payload, object_class=op, encoding="json")
    return adapter.normalize(job, RunContext(), req or _req(op=op))


def _merged_cell_table_payload() -> dict:
    """A real-shaped Textract AnalyzeDocument payload whose table has a MERGED_CELL spanning the two
    top columns. The TABLE references CELLs via a CHILD relationship and the merged block via a
    MERGED_CELL relationship (the branch the adapter previously never walked)."""
    return {
        "DocumentMetadata": {"Pages": 1},
        "Blocks": [
            {
                "BlockType": "TABLE",
                "Id": "tbl",
                "Page": 1,
                "Confidence": 95.0,
                "Geometry": {"BoundingBox": {"Left": 0.1, "Top": 0.3, "Width": 0.6, "Height": 0.2}},
                "Relationships": [
                    {"Type": "CHILD", "Ids": ["c00", "c01", "c10", "c11"]},
                    {"Type": "MERGED_CELL", "Ids": ["m0"]},
                ],
            },
            {
                "BlockType": "MERGED_CELL",
                "Id": "m0",
                "Page": 1,
                "RowIndex": 1,
                "ColumnIndex": 1,
                "RowSpan": 1,
                "ColumnSpan": 2,
                "Confidence": 96.0,
                "EntityTypes": ["COLUMN_HEADER"],
                "Geometry": {"BoundingBox": {"Left": 0.1, "Top": 0.3, "Width": 0.6, "Height": 0.1}},
                "Relationships": [{"Type": "CHILD", "Ids": ["c00", "c01"]}],
            },
            {
                "BlockType": "CELL",
                "Id": "c00",
                "Page": 1,
                "RowIndex": 1,
                "ColumnIndex": 1,
                "Relationships": [{"Type": "CHILD", "Ids": ["w-fin"]}],
            },
            {
                "BlockType": "WORD",
                "Id": "w-fin",
                "Page": 1,
                "Text": "Financial",
                "Confidence": 96.0,
            },
            {
                "BlockType": "CELL",
                "Id": "c01",
                "Page": 1,
                "RowIndex": 1,
                "ColumnIndex": 2,
                "Relationships": [{"Type": "CHILD", "Ids": ["w-sum"]}],
            },
            {"BlockType": "WORD", "Id": "w-sum", "Page": 1, "Text": "Summary", "Confidence": 96.0},
            {
                "BlockType": "CELL",
                "Id": "c10",
                "Page": 1,
                "RowIndex": 2,
                "ColumnIndex": 1,
                "Confidence": 94.0,
                "Relationships": [{"Type": "CHILD", "Ids": ["w-north"]}],
            },
            {"BlockType": "WORD", "Id": "w-north", "Page": 1, "Text": "North", "Confidence": 94.0},
            {
                "BlockType": "CELL",
                "Id": "c11",
                "Page": 1,
                "RowIndex": 2,
                "ColumnIndex": 2,
                "Confidence": 94.0,
                "Relationships": [{"Type": "CHILD", "Ids": ["w-4400"]}],
            },
            {"BlockType": "WORD", "Id": "w-4400", "Page": 1, "Text": "4400", "Confidence": 94.0},
        ],
    }


def _checkbox_payload() -> dict:
    """A FORMS key/value whose VALUE is a SELECTION_ELEMENT (checkbox) — the real Textract shape
    for checkboxes (a KEY_VALUE_SET VALUE child of BlockType SELECTION_ELEMENT)."""
    return {
        "DocumentMetadata": {"Pages": 1},
        "Blocks": [
            {
                "BlockType": "KEY_VALUE_SET",
                "Id": "kv-key",
                "Page": 1,
                "Confidence": 90.0,
                "EntityTypes": ["KEY"],
                "Geometry": {
                    "BoundingBox": {"Left": 0.1, "Top": 0.1, "Width": 0.2, "Height": 0.03}
                },
                "Relationships": [
                    {"Type": "CHILD", "Ids": ["w-married"]},
                    {"Type": "VALUE", "Ids": ["kv-val"]},
                ],
            },
            {
                "BlockType": "WORD",
                "Id": "w-married",
                "Page": 1,
                "Text": "Married",
                "Confidence": 90.0,
            },
            {
                "BlockType": "KEY_VALUE_SET",
                "Id": "kv-val",
                "Page": 1,
                "Confidence": 91.0,
                "EntityTypes": ["VALUE"],
                "Relationships": [{"Type": "CHILD", "Ids": ["sel"]}],
            },
            {
                "BlockType": "SELECTION_ELEMENT",
                "Id": "sel",
                "Page": 1,
                "Confidence": 91.0,
                "SelectionStatus": "SELECTED",
            },
        ],
    }


def test_table_content_reaches_document_text_as_tab_joined_rows():
    # P0/C2: the TABLE's cell content must appear in document.text (was text=None, silently lost).
    adapter = AWSTextractAdapter(client=FakeTextractClient())
    resp = _run_sync(adapter, _req())
    table = next(b for b in resp.document.pages[0].blocks if b.type is BlockType.TABLE)
    assert table.text == "Region\tRevenue\nNorth\t4400"  # grid → plain, cells tab-joined
    assert "Region\tRevenue" in resp.document.text
    assert "North\t4400" in resp.document.text
    assert resp.document.pages[0].text and "Region" in resp.document.pages[0].text


def test_merged_cell_relationship_walked_gives_true_span_and_text():
    # P0/P9: the MERGED_CELL relationship (dead branch) must be walked so the merged header gets a
    # true col_span=2 at grid origin (0,0), its member cells folded in, covered positions None.
    adapter = AWSTextractAdapter(client=FakeTextractClient())
    resp = _normalize_raw(adapter, _merged_cell_table_payload(), "AnalyzeDocument")
    table = next(b for b in resp.document.pages[0].blocks if b.type is BlockType.TABLE).table
    assert table.n_rows == 2 and table.n_cols == 2
    assert table.rows == [["Financial Summary", None], ["North", "4400"]]
    merged = next(c for c in table.cells if c.row == 0 and c.col == 0)
    assert merged.col_span == 2 and merged.is_header and merged.text == "Financial Summary"
    # the two underlying member cells are folded into the merged cell, not emitted separately
    assert not any(c.row == 0 and c.col == 1 for c in table.cells)


def test_checkbox_renders_as_words_not_bracket_notation():
    # P2/C1: selection marks render as the WORDS checked/unchecked, never [X]/[ ].
    adapter = AWSTextractAdapter(client=FakeTextractClient())
    resp = _normalize_raw(adapter, _checkbox_payload(), "AnalyzeDocument")
    assert resp.typed_fields["Married"].value == "checked"
    assert "[X]" not in str(resp.typed_fields["Married"].value)


def test_operation_reflected_in_backend_info():
    # P2: `operation` must reflect the real op, not a hard-coded 'AnalyzeDocument'.
    adapter = AWSTextractAdapter(client=FakeTextractClient())
    resp = _run_sync(adapter, _req(op="DetectDocumentText"))
    assert resp.backend.operation == "DetectDocumentText"


def test_kv_field_carries_citation_from_value_geometry():
    # P1: KV geometry (Geometry.BoundingBox) → TypedField.citations.
    adapter = AWSTextractAdapter(client=FakeTextractClient())
    resp = _run_sync(adapter, _req())
    total = resp.typed_fields["Total"]
    assert total.citations and total.citations[0].bbox is not None
    assert total.citations[0].page == 1


def test_channel_provenance_populated_on_block_path():
    adapter = AWSTextractAdapter(client=FakeTextractClient())
    resp = _run_sync(adapter, _req())
    prov = resp.channel_provenance
    assert prov["blocks"] == "native" and prov["table_cells"] == "native"
    assert prov["text"] == "derived" and prov["markdown"] == "derived"


def test_expense_line_items_mapped_with_repeat_collection_and_citations():
    # P1: LineItemGroups → typed_fields; repeated field names collect into a list (§4.6), not
    # last-writer-wins; per-field geometry → citations.
    adapter = AWSTextractAdapter(client=FakeTextractClient())
    resp = _run_sync(adapter, _req(op="AnalyzeExpense"))
    assert resp.typed_fields["TOTAL"].value == "128.50"  # summary field unchanged (scalar)
    assert resp.typed_fields["ITEM"].value == ["Widget A", "Widget B"]  # repeats → list
    assert resp.typed_fields["PRICE"].value == ["100.00", "50.00"]
    assert resp.typed_fields["TOTAL"].citations[0].bbox is not None  # summary geometry → citation
    assert resp.typed_fields["ITEM"].citations  # line-item geometry → citation


def test_typed_ops_report_real_page_count():
    # P2: typed ops must read DocumentMetadata.Pages, not hard-code page_count=1.
    adapter = AWSTextractAdapter(client=FakeTextractClient())
    resp = _run_sync(adapter, _req(op="AnalyzeExpense"))
    assert resp.document.page_count == 2


def test_lending_fields_carry_citation_from_geometry():
    # P1: lending ValueDetections geometry → TypedField.citations.
    adapter = AWSTextractAdapter(client=FakeTextractClient())
    resp = _normalize_raw(adapter, _fixture("lending"), "AnalyzeLending")
    gp = resp.typed_fields["GROSS_PAY"]
    assert gp.value == "4400.00"
    assert gp.citations and gp.citations[0].bbox is not None


async def test_async_detect_routes_to_text_detection_apis():
    # P1 (live-lane-gated, verified offline via the fake): DetectDocumentText async must call
    # StartDocumentTextDetection / GetDocumentTextDetection — NOT the generic document-analysis ops.
    doc = _fixture("analyze_document")
    page = {"JobStatus": "SUCCEEDED", "DocumentMetadata": {"Pages": 1}, "Blocks": doc["Blocks"]}
    client = FakeTextractClient(get_pages=[page])
    adapter = AWSTextractAdapter(client=client, s3=FakeS3())
    req = _req(op="DetectDocumentText", **{"async": {"mode": "async"}})
    job = adapter.submit(req, RunContext())
    clock = FakeClock()
    job = await await_result(adapter, job, ctx=RunContext(), deadline_ms=1e9, clock=clock)
    assert job.state is JobState.SUCCEEDED
    assert "start_document_text_detection" in client.calls
    assert "get_document_text_detection" in client.calls
    assert "start_document_analysis" not in client.calls


async def test_async_lending_routes_to_lending_apis():
    # P1 (live-lane-gated, verified offline via the fake): AnalyzeLending async must call
    # StartLendingAnalysis / GetLendingAnalysis and paginate Results (not Blocks).
    lending = _fixture("lending")
    page = {
        "JobStatus": "SUCCEEDED",
        "DocumentMetadata": {"Pages": 1},
        "Results": lending["Results"],
    }
    client = FakeTextractClient(get_pages=[page])
    adapter = AWSTextractAdapter(client=client, s3=FakeS3())
    req = _req(op="AnalyzeLending", **{"async": {"mode": "async"}})
    job = adapter.submit(req, RunContext())
    clock = FakeClock()
    job = await await_result(adapter, job, ctx=RunContext(), deadline_ms=1e9, clock=clock)
    assert job.state is JobState.SUCCEEDED
    assert "start_lending_analysis" in client.calls
    assert "get_lending_analysis" in client.calls
    resp = adapter.normalize(job, RunContext(), req)
    assert resp.document.doc_type.label == "PAYSLIPS"


# --- error taxonomy ---------------------------------------------------------------------


def test_throttling_maps_to_retryable():
    adapter = AWSTextractAdapter(
        client=FakeTextractClient(raise_exc=ThrottlingException("slow down"))
    )
    with pytest.raises(RetryableError):
        adapter.submit(_req(), RunContext())


def test_bad_document_maps_to_terminal():
    adapter = AWSTextractAdapter(
        client=FakeTextractClient(raise_exc=BadDocumentException("corrupt"))
    )
    with pytest.raises(TerminalError):
        adapter.submit(_req(), RunContext())


# --- live smoke (skipped without creds) -------------------------------------------------


@pytest.mark.live
def test_live_analyze_document():  # pragma: no cover
    from tests.live_helpers import run_live, sample_pdf_request, skip_unless_creds

    skip_unless_creds("aws-textract")  # full set: access key + secret + region
    resp = run_live(
        "aws-textract",
        AWSTextractAdapter(),
        sample_pdf_request("aws-textract", operation="DetectDocumentText"),
    )
    assert resp.document.text


@pytest.mark.live
def test_live_async_detect_routing():  # pragma: no cover
    # P1 live-lane-gated: the FIRST keyed textract run proves the async DetectDocumentText path
    # routes to StartDocumentTextDetection / GetDocumentTextDetection against real AWS (a wrong
    # routing would run the pricier document-analysis surface). Needs an S3 bucket for the async
    # staging flow; skips cleanly without creds/bucket.
    import base64
    import os

    from openreading.testing.sample_pdf import build_sample_pdf
    from tests.live_helpers import run_live, skip_unless_creds

    skip_unless_creds("aws-textract")
    if not os.environ.get("OPENREADING_TEXTRACT_S3_BUCKET"):
        pytest.skip("async textract needs OPENREADING_TEXTRACT_S3_BUCKET")
    req = OpenReadingRequest.model_validate(
        {
            "document": {
                "bytes_base64": base64.b64encode(build_sample_pdf()).decode(),
                "mime_type": "application/pdf",
            },
            "backend": {"id": "aws-textract", "operation": "DetectDocumentText"},
            "async": {"mode": "async"},
        }
    )
    resp = run_live("aws-textract", AWSTextractAdapter(), req)
    assert resp.document.text
