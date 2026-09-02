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
