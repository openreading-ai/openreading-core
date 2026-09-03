"""Mistral OCR's real httpx wire contract, mocked by respx with no network access."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from openreading.adapters.mistral_ocr.adapter import _BASE_URL, _HttpxMistralOCRClient
from openreading.types.errors import RetryableError, TerminalError

_BODY = {
    "model": "mistral-ocr-latest",
    "document": {"type": "document_url", "document_url": "https://example.com/doc.pdf"},
}
_OK = {"pages": [], "model": "mistral-ocr-latest", "usage_info": {"pages_processed": 0}}


@respx.mock
def test_ocr_posts_json_to_v1_ocr_with_bearer_auth() -> None:
    route = respx.post(f"{_BASE_URL}/v1/ocr").mock(return_value=httpx.Response(200, json=_OK))
    out = _HttpxMistralOCRClient("secret-key").ocr(_BODY)

    req = route.calls.last.request
    assert req.headers.get("authorization") == "Bearer secret-key"
    assert req.headers.get("content-type", "").startswith("application/json")
    assert json.loads(req.content) == _BODY
    assert out == _OK


@respx.mock
def test_retryable_http_status_preserves_retry_after() -> None:
    respx.post(f"{_BASE_URL}/v1/ocr").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "3"}, json={"message": "busy"})
    )
    with pytest.raises(RetryableError) as exc:
        _HttpxMistralOCRClient("secret-key").ocr(_BODY)
    assert exc.value.backend_code == "http_429"
    assert exc.value.retry_after == 3.0


@respx.mock
def test_auth_failure_is_terminal_and_secret_is_not_in_message() -> None:
    respx.post(f"{_BASE_URL}/v1/ocr").mock(
        return_value=httpx.Response(401, json={"message": "provider echoed secret-key"})
    )
    with pytest.raises(TerminalError) as exc:
        _HttpxMistralOCRClient("secret-key").ocr(_BODY)
    assert exc.value.backend_code == "auth_rejected"
    assert "secret-key" not in str(exc.value)
    assert "provider echoed" not in str(exc.value)


@respx.mock
def test_structured_error_code_and_message_are_preserved() -> None:
    respx.post(f"{_BASE_URL}/v1/ocr").mock(
        return_value=httpx.Response(422, json={"code": "invalid_document", "message": "bad pdf"})
    )
    with pytest.raises(TerminalError) as exc:
        _HttpxMistralOCRClient("secret-key").ocr(_BODY)
    assert exc.value.backend_code == "invalid_document"
    assert "bad pdf" in str(exc.value)


@respx.mock
def test_success_with_non_json_body_is_terminal() -> None:
    respx.post(f"{_BASE_URL}/v1/ocr").mock(return_value=httpx.Response(200, text="not-json"))
    with pytest.raises(TerminalError) as exc:
        _HttpxMistralOCRClient("secret-key").ocr(_BODY)
    assert exc.value.backend_code == "malformed_response"
