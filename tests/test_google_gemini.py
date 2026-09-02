"""Google Gemini adapter fixtures and conformance tests; all calls use a plain fake client."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from openreading.adapters.google_gemini import GoogleGeminiAdapter
from openreading.ledger.header import slim_request
from openreading.testing import ConformanceCase, check_adapter_conformance
from openreading.types import BlockType
from openreading.types.enums import CostBasis
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import ResolvedCredentials, RunContext

FIX = Path(__file__).parent / "fixtures" / "google-gemini"
PDF_B64 = base64.b64encode(b"%PDF-1.7 fake").decode()


def _fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


class FakeGeminiClient:
    """Mode-aware implementation of the adapter's client Protocol."""

    def __init__(self, fixture: str = "parse", raise_exc: Exception | None = None) -> None:
        self.fixture = fixture
        self.raise_exc = raise_exc
        self.last_call: dict | None = None

    def interact(self, *, model, input, store, response_format=None):
        if self.raise_exc is not None:
            raise self.raise_exc
        self.last_call = {
            "model": model,
            "input": input,
            "store": store,
            "response_format": response_format,
        }
        name = "extract" if response_format is not None else self.fixture
        return _fixture(name)


def _req(**over) -> OpenReadingRequest:
    body = {
        "document": {"bytes_base64": PDF_B64, "mime_type": "application/pdf"},
        "backend": {"id": "google-gemini", "type": "hosted_api"},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


def _extract_req() -> OpenReadingRequest:
    return _req(
        outputs={"markdown": False, "text": False, "blocks": False, "typed_fields": True},
        extraction_schema={
            "instructions": "Extract the requested application fields.",
            "json_schema": {
                "type": "object",
                "properties": {
                    "borrower_name": {"type": "string"},
                    "gross_pay": {"type": "number"},
                    "approved": {"type": "boolean"},
                    "line_items": {"type": "array"},
                },
            },
        },
    )


def _run(adapter: GoogleGeminiAdapter, req: OpenReadingRequest):
    ctx = RunContext()
    return adapter.normalize(adapter.submit(req, ctx), ctx, slim_request(req))


def test_google_gemini_conforms_non_deterministic():
    check_adapter_conformance(
        GoogleGeminiAdapter(client=FakeGeminiClient()),
        [
            ConformanceCase(request=_req(), deterministic=False, label="parse"),
            ConformanceCase(request=_extract_req(), deterministic=False, label="extract"),
        ],
        adapter_factory=lambda: GoogleGeminiAdapter(client=FakeGeminiClient()),
        strict_checks={"C1", "C6", "C10"},
    )


def test_parse_sends_inline_document_stateless_and_derives_structure():
    client = FakeGeminiClient()
    adapter = GoogleGeminiAdapter(client=client)
    resp = _run(adapter, _req())

    assert client.last_call is not None
    assert client.last_call["model"] == "gemini-3.6-flash"
    assert client.last_call["store"] is False
    assert client.last_call["response_format"] is None
    assert client.last_call["input"][0] == {
        "type": "document",
        "data": PDF_B64,
        "mime_type": "application/pdf",
    }
    assert client.last_call["input"][1]["type"] == "text"

    assert resp.document.markdown.startswith("# Loan Application")
    assert resp.document.text != resp.document.markdown
    assert "# Loan Application" not in resp.document.text
    assert "Region\tRevenue" in resp.document.text
    assert resp.document.pages and len(resp.document.pages) == 1
    blocks = resp.document.pages[0].blocks
    assert blocks and blocks[0].type is BlockType.TITLE
    assert blocks[-1].type is BlockType.TABLE
    assert blocks[-1].table and blocks[-1].table.rows[-1] == ["North", "4400"]
    assert all(block.bbox is None and block.confidence is None for block in blocks)
    assert resp.channel_provenance == {
        "markdown": "native",
        "text": "derived",
        "blocks": "derived",
        "table_cells": "derived",
    }
    fields = {warning.field for warning in resp.warnings or []}
    assert {"blocks", "block_bbox", "block_confidence"} <= fields
    assert resp.backend_raw and resp.backend_raw.object_class == "google.ai.Interaction"
    assert resp.usage and resp.usage.input_tokens == 2400 and resp.usage.output_tokens == 180


def test_file_id_is_forwarded_as_a_gemini_files_uri():
    client = FakeGeminiClient()
    adapter = GoogleGeminiAdapter(client=client)
    req = _req(document={"file_id": "files/doc-123", "mime_type": "application/pdf"})
    adapter.submit(req, RunContext())
    assert client.last_call is not None
    assert client.last_call["input"][0] == {
        "type": "document",
        "uri": "files/doc-123",
        "mime_type": "application/pdf",
    }


def test_extract_uses_json_schema_and_preserves_json_value_types():
    client = FakeGeminiClient()
    req = _extract_req()
    resp = _run(GoogleGeminiAdapter(client=client), req)

    assert client.last_call is not None
    assert client.last_call["response_format"] == {
        "type": "text",
        "mime_type": "application/json",
        "schema": req.extraction_schema.json_schema,
    }
    prompt = client.last_call["input"][1]["text"]
    assert "Extract the requested application fields." in prompt
    assert resp.typed_fields is not None
    assert resp.typed_fields["gross_pay"].value == 4400.0
    assert resp.typed_fields["gross_pay"].type == "number"
    assert resp.typed_fields["approved"].value is True
    assert resp.typed_fields["line_items"].value == [{"sku": "A-1", "quantity": 2}]
    assert resp.channel_provenance == {"typed_fields": "native"}


def test_runtime_model_override_wins_over_default():
    client = FakeGeminiClient()
    GoogleGeminiAdapter(client=client).submit(_req(), RunContext(runtime={"model": "gemini-x"}))
    assert client.last_call is not None and client.last_call["model"] == "gemini-x"


def test_request_model_override_wins_over_environment_config():
    client = FakeGeminiClient()
    req = _req(backend={"id": "google-gemini", "version": "gemini-requested"})
    GoogleGeminiAdapter(client=client).submit(req, RunContext(runtime={"model": "gemini-env"}))
    assert client.last_call is not None and client.last_call["model"] == "gemini-requested"


def test_cost_is_unknown_but_token_quantity_is_preserved():
    adapter = GoogleGeminiAdapter(client=FakeGeminiClient())
    cost = adapter.report_cost(adapter.submit(_req(), RunContext()))
    assert cost.native_unit == "token"
    assert cost.native_quantity == 2580
    assert cost.cost_usd is None
    assert cost.basis is CostBasis.UNKNOWN
    assert cost.billing_target == "caller_account"


@pytest.mark.live
def test_live_parse():  # pragma: no cover - requires the caller's paid/limited vendor account
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        pytest.skip("GEMINI_API_KEY is not set")
    from openreading.testing.sample_pdf import build_sample_pdf

    req = _req(
        document={
            "bytes_base64": base64.b64encode(build_sample_pdf()).decode(),
            "mime_type": "application/pdf",
        }
    )
    ctx = RunContext(credentials=ResolvedCredentials(values={"api_key": api_key}, source="env"))
    adapter = GoogleGeminiAdapter()
    resp = adapter.normalize(adapter.submit(req, ctx), ctx, slim_request(req))
    assert resp.document.text or resp.document.markdown
