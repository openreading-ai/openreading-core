"""Mistral OCR adapter fault and malformed-response coverage using plain fake clients only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openreading.adapters.mistral_ocr import MistralOCRAdapter
from openreading.types.enums import JobState, ResponseState, WaitMode
from openreading.types.errors import RetryableError, TerminalError
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RawResult, RunContext

FIX = Path(__file__).parent / "fixtures" / "mistral-ocr"


def _fixture() -> dict:
    return json.loads((FIX / "ocr.json").read_text())


def _req(**over) -> OpenReadingRequest:
    body = {
        "document": {"url": "https://example.com/invoice.pdf", "mime_type": "application/pdf"},
        "backend": {"id": "mistral-ocr"},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


class _RaisingClient:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def ocr(self, body: dict) -> dict:
        raise self.exc


def _normalize(raw: dict, req: OpenReadingRequest | None = None):
    adapter = MistralOCRAdapter(client=_RaisingClient(AssertionError("not called")))
    job = adapter.new_job(WaitMode.INLINE, state=JobState.SUCCEEDED)
    job.raw = RawResult(payload=raw, encoding="json", media_type="application/json")
    return adapter.normalize(job, RunContext(), req or _req())


@pytest.mark.parametrize(
    "document",
    [
        {"path": "/local/invoice.pdf", "mime_type": "application/pdf"},
        {"file_id": "uploaded-elsewhere", "mime_type": "application/pdf"},
    ],
)
def test_unsupported_input_is_terminal(document) -> None:
    with pytest.raises(TerminalError) as exc:
        MistralOCRAdapter(client=_RaisingClient(AssertionError("not called"))).submit(
            _req(document=document), RunContext()
        )
    assert exc.value.backend_code == "unsupported_input"


def test_malformed_base64_is_rejected_before_client_call() -> None:
    with pytest.raises(TerminalError) as exc:
        MistralOCRAdapter(client=_RaisingClient(AssertionError("not called"))).submit(
            _req(document={"bytes_base64": "not base64!", "mime_type": "application/pdf"}),
            RunContext(),
        )
    assert exc.value.backend_code == "unsupported_input"


def test_taxonomy_error_is_reraised_unchanged() -> None:
    boom = RetryableError("slow down", backend_code="rate_limit", retry_after=2.0)
    with pytest.raises(RetryableError) as exc:
        MistralOCRAdapter(client=_RaisingClient(boom)).submit(_req(), RunContext())
    assert exc.value is boom


def test_unexpected_client_error_is_mapped_terminal() -> None:
    with pytest.raises(TerminalError) as exc:
        MistralOCRAdapter(client=_RaisingClient(ValueError("bad response"))).submit(
            _req(), RunContext()
        )
    assert exc.value.backend_code == "ValueError"


def test_malformed_document_annotation_is_partial_and_warned() -> None:
    raw = _fixture()
    raw["document_annotation"] = "{not-json"
    req = _req(extraction_schema={"json_schema": {"type": "object"}})
    resp = _normalize(raw, req)

    assert resp.status.state is ResponseState.PARTIAL
    assert resp.typed_fields is None
    assert any(w.code == "typed_fields_malformed" for w in resp.warnings or [])


def test_missing_dimensions_never_fabricates_geometry() -> None:
    raw = _fixture()
    raw["pages"][0].pop("dimensions")
    resp = _normalize(raw)

    first = (resp.document.pages or [])[0]
    assert first.width is None and first.height is None
    assert all(block.bbox is None for block in first.blocks or [])
    assert any(w.code == "block_bbox_unavailable" for w in resp.warnings or [])


def test_invalid_confidence_is_dropped() -> None:
    raw = _fixture()
    raw["pages"][0]["blocks"][0]["confidence_scores"]["average_content_confidence_score"] = 1.4
    resp = _normalize(raw)
    first = (resp.document.pages or [])[0]
    assert (first.blocks or [])[0].confidence is None
    assert any(w.code == "block_confidence_unavailable" for w in resp.warnings or [])


def test_native_table_block_keeps_its_derived_cell_grid() -> None:
    raw = _fixture()
    raw["pages"][0]["tables"] = []
    raw["pages"][0]["blocks"] = [
        {
            "id": "table-1",
            "type": "table",
            "content": (
                "<table><tr><th>Item</th><th>Amount</th></tr>"
                "<tr><td>Consulting</td><td>$42.50</td></tr></table>"
            ),
        }
    ]

    response = _normalize(raw)

    table_block = (response.document.pages or [])[0].blocks[0]
    assert table_block.table is not None
    assert table_block.table.rows == [["Item", "Amount"], ["Consulting", "$42.50"]]


def test_nondict_pages_are_ignored_without_crashing() -> None:
    raw = _fixture()
    raw["pages"] = [None, "bad", raw["pages"][1]]
    resp = _normalize(raw)
    assert [p.page_number for p in resp.document.pages or []] == [2]


def test_requested_typed_fields_missing_is_warned() -> None:
    raw = _fixture()
    raw.pop("document_annotation")
    req = _req(extraction_schema={"json_schema": {"type": "object"}})
    resp = _normalize(raw, req)
    assert resp.typed_fields is None
    assert any(w.code == "typed_fields_unavailable" for w in resp.warnings or [])
