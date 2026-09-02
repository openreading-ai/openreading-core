"""Mistral OCR adapter happy paths, all offline through an injected Protocol fake.

The fixture mirrors Mistral's documented OCR response fields: pages with one-based ``index``,
Markdown, pixel dimensions, native blocks/bounds/confidence, HTML tables, document annotations,
and ``usage_info.pages_processed``. It remains documented-shape evidence until a keyed live run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from openreading.adapters.mistral_ocr import MistralOCRAdapter
from openreading.ledger.header import slim_request
from openreading.testing import ConformanceCase, check_adapter_conformance
from openreading.types import BlockType, JobState
from openreading.types.enums import CostBasis, WaitMode
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RunContext

FIX = Path(__file__).parent / "fixtures" / "mistral-ocr"


def _fixture(name: str = "ocr") -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


class FakeMistralOCRClient:
    """Plain fake implementing the adapter's client Protocol and recording request bodies."""

    def __init__(self) -> None:
        self.last_body: dict | None = None

    def ocr(self, body: dict) -> dict:
        self.last_body = body
        raw = _fixture()
        if "document_annotation_format" not in body:
            raw.pop("document_annotation", None)
        return raw


def _req(**over) -> OpenReadingRequest:
    body = {
        "document": {"url": "https://example.com/invoice.pdf", "mime_type": "application/pdf"},
        "backend": {"id": "mistral-ocr"},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


def _run(adapter: MistralOCRAdapter, req: OpenReadingRequest):
    job = adapter.submit(req, RunContext())
    assert job.wait_mode is WaitMode.INLINE and job.state is JobState.SUCCEEDED
    return adapter.normalize(job, RunContext(), slim_request(req)), job


def test_mistral_ocr_conforms() -> None:
    check_adapter_conformance(
        MistralOCRAdapter(client=FakeMistralOCRClient()),
        [ConformanceCase(request=_req(), deterministic=True, label="ocr")],
        strict_checks={"C1", "C6"},
        adapter_factory=lambda: MistralOCRAdapter(client=FakeMistralOCRClient()),
    )


def test_submit_uses_inline_document_url_and_default_contract() -> None:
    client = FakeMistralOCRClient()
    adapter = MistralOCRAdapter(client=client)
    job = adapter.submit(_req(), RunContext())

    assert job.wait_mode is WaitMode.INLINE and job.state is JobState.SUCCEEDED
    assert client.last_body == {
        "model": "mistral-ocr-latest",
        "document": {
            "type": "document_url",
            "document_url": "https://example.com/invoice.pdf",
        },
        "include_blocks": True,
        "confidence_scores_granularity": "block",
        "table_format": "html",
    }


def test_runtime_model_and_page_ranges_are_forwarded() -> None:
    client = FakeMistralOCRClient()
    req = _req(pages={"ranges": [{"start": 2, "end": 3}]})
    MistralOCRAdapter(client=client).submit(req, RunContext(runtime={"model": "ocr-test"}))

    assert client.last_body is not None
    assert client.last_body["model"] == "ocr-test"
    assert client.last_body["pages"] == [1, 2]


def test_request_model_override_wins_over_environment_config() -> None:
    client = FakeMistralOCRClient()
    req = _req(backend={"id": "mistral-ocr", "version": "mistral-ocr-requested"})
    MistralOCRAdapter(client=client).submit(req, RunContext(runtime={"model": "mistral-ocr-env"}))
    assert client.last_body is not None
    assert client.last_body["model"] == "mistral-ocr-requested"


@pytest.mark.parametrize(
    ("document", "expected_type", "expected_key", "expected_prefix"),
    [
        (
            {"bytes_base64": "JVBERi0=", "mime_type": "application/pdf"},
            "document_url",
            "document_url",
            "data:application/pdf;base64,JVBERi0=",
        ),
        (
            {"url": "https://example.com/scan.png", "mime_type": "image/png"},
            "image_url",
            "image_url",
            "https://example.com/scan.png",
        ),
        (
            {"bytes_base64": "iVBORw0=", "mime_type": "image/png"},
            "image_url",
            "image_url",
            "data:image/png;base64,iVBORw0=",
        ),
    ],
)
def test_document_shapes(document, expected_type, expected_key, expected_prefix) -> None:
    client = FakeMistralOCRClient()
    MistralOCRAdapter(client=client).submit(_req(document=document), RunContext())
    assert client.last_body is not None
    sent = client.last_body["document"]
    assert sent["type"] == expected_type
    assert sent[expected_key] == expected_prefix


def test_schema_extraction_forwards_json_schema_and_keeps_native_values() -> None:
    client = FakeMistralOCRClient()
    schema = {
        "type": "object",
        "properties": {"invoice_number": {"type": "string"}, "total": {"type": "number"}},
    }
    req = _req(extraction_schema={"json_schema": schema, "instructions": "Extract totals."})
    resp, job = _run(MistralOCRAdapter(client=client), req)

    assert client.last_body is not None
    assert client.last_body["document_annotation_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "openreading_extraction",
            "strict": True,
            "schema": schema,
        },
    }
    assert client.last_body["document_annotation_prompt"] == "Extract totals."
    assert resp.typed_fields is not None
    assert resp.typed_fields["invoice_number"].value == "1001"
    assert resp.typed_fields["total"].value == 42.5
    assert job.raw is not None and job.raw.object_class == "extract"


def test_normalizes_native_pages_blocks_geometry_confidence_and_derived_tables() -> None:
    resp, _ = _run(MistralOCRAdapter(client=FakeMistralOCRClient()), _req())

    assert resp.document.page_count == 2
    assert [p.page_number for p in resp.document.pages or []] == [1, 2]
    assert resp.document.markdown and "Invoice 1001" in resp.document.markdown
    assert resp.document.text and "Bill to Jane Doe." in resp.document.text
    first = (resp.document.pages or [])[0]
    assert first.width == 1700 and first.height == 2200 and first.dpi == 200
    title = next(b for b in first.blocks or [] if b.type is BlockType.TITLE)
    assert title.bbox is not None
    assert title.bbox.x == pytest.approx(85 / 1700)
    assert title.bbox.y == pytest.approx(110 / 2200)
    assert title.confidence == pytest.approx(0.98)
    table = next(b for b in first.blocks or [] if b.type is BlockType.TABLE)
    assert table.table is not None
    assert table.table.rows == [["Item", "Amount"], ["Consulting", "$42.50"]]
    assert "Item\tAmount" in (table.text or "")
    assert resp.channel_provenance == {
        "markdown": "native",
        "text": "derived",
        "blocks": "native",
        "block_bbox": "native",
        "block_confidence": "native",
        "table_cells": "derived",
    }


def test_zero_page_index_maps_to_one_without_shifting_positive_indexes() -> None:
    raw = _fixture()
    raw["pages"][0]["index"] = 0
    raw["pages"] = [raw["pages"][0]]

    class _ZeroClient:
        def ocr(self, body: dict) -> dict:
            return raw

    resp, _ = _run(MistralOCRAdapter(client=_ZeroClient()), _req())
    assert [p.page_number for p in resp.document.pages or []] == [1]


def test_cost_is_estimated_from_pages_and_annotation_mode() -> None:
    adapter = MistralOCRAdapter(client=FakeMistralOCRClient())
    _, parse_job = _run(adapter, _req())
    _, extract_job = _run(adapter, _req(extraction_schema={"json_schema": {"type": "object"}}))

    parse_cost = adapter.report_cost(parse_job)
    extract_cost = adapter.report_cost(extract_job)
    assert parse_cost.native_quantity == 2
    assert parse_cost.cost_usd == pytest.approx(0.008)
    assert extract_cost.cost_usd == pytest.approx(0.01)
    assert parse_cost.basis is CostBasis.ESTIMATED
    assert parse_cost.billing_target == "caller_account"


def test_missing_credentials_names_required_environment_variable() -> None:
    from openreading.types.errors import MissingCredentialsError

    with pytest.raises(MissingCredentialsError) as exc:
        MistralOCRAdapter().submit(_req(), RunContext())
    assert exc.value.missing == ["MISTRAL_API_KEY"]


@pytest.mark.live
def test_live_mistral_ocr() -> None:  # pragma: no cover
    from tests.live_helpers import run_live, sample_pdf_request, skip_unless_creds

    if not os.environ.get("MISTRAL_API_KEY"):
        pytest.skip("set MISTRAL_API_KEY to run Mistral OCR live tests")
    skip_unless_creds("mistral-ocr")
    resp = run_live("mistral-ocr", MistralOCRAdapter(), sample_pdf_request("mistral-ocr"))
    assert resp.document.markdown and resp.document.pages
