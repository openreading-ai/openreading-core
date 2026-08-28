"""Real `_HttpxOpenOCRClient` request-construction tests (respx, NO network).

The injected-fake tests in test_open_ocr.py never exercise the real HTTP client — the Bearer
auth shape, the Idempotency-Key forwarding (the mechanism that keeps a network retry from
double-charging this backend's own metered, billed-per-page cost — see the adapter's module
docstring), and the `_raise` error-envelope parsing are only ever proven against a hand-written
fake. These tests pin the actual wire contract, mirroring `test_reducto_http.py`'s shape.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from openreading.adapters.open_ocr.adapter import _BASE_URL, _HttpxOpenOCRClient
from openreading.types.errors import RetryableError, TerminalError

_OK = {"status": "succeeded", "request_id": "req_1", "extracted_text": "hi"}


@respx.mock
def test_create_ocr_posts_body_with_bearer_auth():
    route = respx.post(f"{_BASE_URL}/v1/ocr").mock(return_value=httpx.Response(200, json=_OK))

    out = _HttpxOpenOCRClient("test-key").create_ocr({"engine": "openocr/tesseract"}, None)

    req = route.calls.last.request
    assert req.headers.get("authorization") == "Bearer test-key"
    assert json.loads(req.content) == {"engine": "openocr/tesseract"}
    assert out == _OK


@respx.mock
def test_create_ocr_forwards_idempotency_key_header_when_present():
    route = respx.post(f"{_BASE_URL}/v1/ocr").mock(return_value=httpx.Response(200, json=_OK))

    _HttpxOpenOCRClient("test-key").create_ocr({"engine": "openocr/tesseract"}, "idem-123")

    assert route.calls.last.request.headers.get("idempotency-key") == "idem-123"


@respx.mock
def test_create_ocr_omits_idempotency_header_when_absent():
    route = respx.post(f"{_BASE_URL}/v1/ocr").mock(return_value=httpx.Response(200, json=_OK))

    _HttpxOpenOCRClient("test-key").create_ocr({"engine": "openocr/tesseract"}, None)

    assert "idempotency-key" not in route.calls.last.request.headers


@respx.mock
def test_get_request_fetches_by_id():
    route = respx.get(f"{_BASE_URL}/v1/ocr/req_42").mock(return_value=httpx.Response(200, json=_OK))

    out = _HttpxOpenOCRClient("test-key").get_request("req_42")

    assert route.called
    assert out == _OK


@respx.mock
def test_create_ocr_structured_error_preserves_type_as_backend_code():
    respx.post(f"{_BASE_URL}/v1/ocr").mock(
        return_value=httpx.Response(
            402, json={"error": {"type": "insufficient_credits", "message": "top up your account"}}
        )
    )

    with pytest.raises(TerminalError) as exc_info:
        _HttpxOpenOCRClient("test-key").create_ocr({"engine": "openocr/tesseract"}, None)

    assert exc_info.value.backend_code == "insufficient_credits"
    assert "top up your account" in str(exc_info.value)


@respx.mock
def test_create_ocr_malformed_non_json_error_body_falls_back_to_raw_text():
    # Malformed/incomplete response: `_raise` assumes the error envelope is
    # {"error": {"type"|"code", "message"}} JSON, but wraps that parse in
    # contextlib.suppress(Exception) precisely because a 5xx can answer with a plain-text body
    # (e.g. a proxy/load-balancer error page) instead of the platform's own JSON shape. Pin that
    # this degrades gracefully to the raw response text, not a crash inside error handling itself.
    respx.post(f"{_BASE_URL}/v1/ocr").mock(
        return_value=httpx.Response(500, text="Service Unavailable")
    )

    with pytest.raises(RetryableError) as exc_info:
        _HttpxOpenOCRClient("test-key").create_ocr({"engine": "openocr/tesseract"}, None)

    assert "Service Unavailable" in str(exc_info.value)
    assert exc_info.value.backend_code == "http_500"
