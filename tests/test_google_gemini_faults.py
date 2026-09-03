"""Google Gemini fault injection through plain clients; no HTTP mocking or network access."""

from __future__ import annotations

import base64
import sys

import pytest

from openreading.adapters.google_gemini import GoogleGeminiAdapter
from openreading.adapters.google_gemini import adapter as adapter_mod
from openreading.ledger.header import slim_request
from openreading.types.enums import ResponseState
from openreading.types.errors import MissingCredentialsError, RetryableError, TerminalError
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import ResolvedCredentials, RunContext

PDF_B64 = base64.b64encode(b"%PDF-1.7 fake").decode()


class _FakeClient:
    def __init__(self, response: dict | None = None, exc: Exception | None = None) -> None:
        self.response = response or {
            "status": "completed",
            "model": "gemini-3.6-flash",
            "steps": [{"type": "model_output", "content": [{"type": "text", "text": "ok"}]}],
            "usage": {},
        }
        self.exc = exc

    def interact(self, *, model, input, store, response_format=None):
        if self.exc is not None:
            raise self.exc
        return self.response


def _req(document=None, **over) -> OpenReadingRequest:
    body = {
        "document": document or {"bytes_base64": PDF_B64, "mime_type": "application/pdf"},
        "backend": {"id": "google-gemini"},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


@pytest.mark.parametrize(
    "document",
    [
        {"url": "https://example.com/doc.pdf", "mime_type": "application/pdf"},
        {"path": "/tmp/doc.pdf", "mime_type": "application/pdf"},
    ],
)
def test_url_and_path_inputs_are_rejected(document):
    with pytest.raises(TerminalError) as exc:
        GoogleGeminiAdapter(client=_FakeClient()).submit(_req(document), RunContext())
    assert exc.value.backend_code == "unsupported_input"


def test_malformed_base64_is_rejected_before_client_call():
    with pytest.raises(TerminalError) as exc:
        GoogleGeminiAdapter(client=_FakeClient()).submit(
            _req({"bytes_base64": "not base64!", "mime_type": "application/pdf"}), RunContext()
        )
    assert exc.value.backend_code == "unsupported_input"


def test_missing_api_key_fails_before_real_client_construction(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise AssertionError("real client must not be constructed without a key")

    monkeypatch.setattr(adapter_mod, "_HttpxGeminiClient", _boom)
    with pytest.raises(MissingCredentialsError) as exc:
        GoogleGeminiAdapter()._get_client(RunContext(credentials=ResolvedCredentials(values={})))
    assert exc.value.missing == ["GEMINI_API_KEY"]


def test_client_is_built_from_resolved_credentials(monkeypatch):
    seen = {}

    class _Stub:
        def __init__(self, api_key):
            seen["api_key"] = api_key

    monkeypatch.setattr(adapter_mod, "_HttpxGeminiClient", _Stub)
    client = GoogleGeminiAdapter()._get_client(
        RunContext(credentials=ResolvedCredentials(values={"api_key": "gem-test"}))
    )
    assert isinstance(client, _Stub) and seen == {"api_key": "gem-test"}


def test_health_reports_missing_httpx(monkeypatch):
    monkeypatch.setitem(sys.modules, "httpx", None)
    health = GoogleGeminiAdapter().health()
    assert not health.ready and any("httpx" in dep for dep in health.missing_deps)


def test_taxonomy_error_is_reraised_unchanged():
    original = RetryableError("slow", backend_code="429", retry_after=4.0)
    with pytest.raises(RetryableError) as exc:
        GoogleGeminiAdapter(client=_FakeClient(exc=original)).submit(_req(), RunContext())
    assert exc.value is original


def test_unexpected_client_error_maps_terminal():
    with pytest.raises(TerminalError) as exc:
        GoogleGeminiAdapter(client=_FakeClient(exc=ValueError("bad response"))).submit(
            _req(), RunContext()
        )
    assert exc.value.backend_code == "ValueError"


@pytest.mark.parametrize(
    "response",
    [
        {"status": "completed", "steps": []},
        {
            "status": "completed",
            "steps": [{"type": "model_output", "content": [{"type": "text", "text": "not-json"}]}],
        },
        {
            "status": "completed",
            "steps": [{"type": "model_output", "content": [{"type": "text", "text": "[]"}]}],
        },
    ],
)
def test_extract_marks_missing_or_non_object_json_partial(response):
    req = _req(
        outputs={"markdown": False, "text": False, "blocks": False, "typed_fields": True},
        extraction_schema={"json_schema": {"type": "object", "properties": {}}},
    )
    adapter = GoogleGeminiAdapter(client=_FakeClient(response=response))
    job = adapter.submit(req, RunContext())
    resp = adapter.normalize(job, RunContext(), slim_request(req))
    assert resp.status.state is ResponseState.PARTIAL
    assert resp.status.error is not None
    assert resp.status.error.backend_code == "malformed_response"
    assert resp.typed_fields is None
    assert not any(w.code == "interaction_incomplete" for w in resp.warnings or [])


def _normalize(adapter: GoogleGeminiAdapter, req: OpenReadingRequest):
    return adapter.normalize(adapter.submit(req, RunContext()), RunContext(), slim_request(req))


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_failed_or_cancelled_interaction_is_terminal_at_submit(status):
    """A synchronous vendor failure is a TerminalError at submit (adapters runbook), carrying the
    vendor status as backend_code and the ``errors[]`` message — never a SUCCEEDED job."""
    response = {
        "status": status,
        "steps": [],
        "errors": [{"code": "https://example/quota", "message": "quota exhausted"}],
    }
    with pytest.raises(TerminalError) as exc:
        GoogleGeminiAdapter(client=_FakeClient(response=response)).submit(_req(), RunContext())
    assert exc.value.backend_code == status
    assert "quota exhausted" in str(exc.value)


@pytest.mark.parametrize("status", ["incomplete", "budget_exceeded", "requires_action"])
def test_non_completed_interaction_is_partial_with_status_error(status):
    """Answered-but-not-finished is PARTIAL with ``status.error`` (C10), keeping whatever text
    did arrive; the ``interaction_incomplete`` warning stays as the machine-readable flag."""
    response = {
        "status": status,
        "steps": [{"type": "model_output", "content": [{"type": "text", "text": "# partial"}]}],
        "errors": [{"code": "x", "message": "cut short"}],
    }
    resp = _normalize(GoogleGeminiAdapter(client=_FakeClient(response=response)), _req())
    assert resp.status.state is ResponseState.PARTIAL
    assert resp.status.error is not None
    assert resp.status.error.backend_code == status
    assert "cut short" in (resp.status.error.message or "")
    assert resp.document.markdown == "# partial"
    assert any(w.code == "interaction_incomplete" for w in resp.warnings or [])


def test_non_string_text_content_is_skipped_not_crashed():
    response = {
        "status": "completed",
        "steps": [
            {
                "type": "model_output",
                "content": [{"type": "text", "text": None}, {"type": "text", "text": "ok"}],
            }
        ],
    }
    resp = _normalize(GoogleGeminiAdapter(client=_FakeClient(response=response)), _req())
    assert resp.document.markdown == "ok"


def test_blocks_off_means_no_pages_even_when_table_cells_are_requested():
    """``outputs.blocks=False`` is honoured like every other markdown-derived adapter: table
    cells live on blocks, so there is nowhere to put them and the channel is warned, not
    smuggled back in through ``pages``."""
    response = {
        "status": "completed",
        "steps": [
            {
                "type": "model_output",
                "content": [{"type": "text", "text": "| a | b |\n| --- | --- |\n| 1 | 2 |"}],
            }
        ],
    }
    req = _req(outputs={"blocks": False, "tables": "cells"})
    resp = _normalize(GoogleGeminiAdapter(client=_FakeClient(response=response)), req)
    assert resp.document.pages is None
    codes = {w.code for w in resp.warnings or []}
    assert "table_cells_unavailable" in codes
    assert "page_attribution_unavailable" not in codes


def test_backend_raw_is_the_untouched_interaction():
    resp = _normalize(GoogleGeminiAdapter(client=_FakeClient()), _req())
    assert resp.backend_raw is not None
    assert "_pdf_page_count" not in resp.backend_raw.payload
    assert resp.backend_raw.payload["status"] == "completed"


def test_empty_extraction_object_yields_no_typed_fields():
    response = {
        "status": "completed",
        "steps": [{"type": "model_output", "content": [{"type": "text", "text": "{}"}]}],
    }
    req = _req(
        outputs={"markdown": False, "text": False, "blocks": False, "typed_fields": True},
        extraction_schema={"json_schema": {"type": "object", "properties": {}}},
    )
    resp = _normalize(GoogleGeminiAdapter(client=_FakeClient(response=response)), req)
    assert resp.typed_fields is None
    assert any(w.code == "typed_fields_unavailable" for w in resp.warnings or [])


@pytest.mark.parametrize(
    "mime",
    ["image/png", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"],
)
def test_non_pdf_binary_mime_types_are_rejected_before_the_call(mime):
    """The Interactions ``document`` part is PDF (plus plain-text types); an image or Office
    file would come back as an opaque vendor 400 instead of the adapter's own taxonomy."""
    with pytest.raises(TerminalError) as exc:
        GoogleGeminiAdapter(client=_FakeClient()).submit(
            _req({"bytes_base64": PDF_B64, "mime_type": mime}), RunContext()
        )
    assert exc.value.backend_code == "unsupported_input"
