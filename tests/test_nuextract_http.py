"""Real `_HttpxNuExtractClient` request-construction tests (respx, NO network).

The injected-fake tests never exercise the real HTTP client, so job creation shipped POSTing the
document as a raw `application/octet-stream` body — which the live platform rejects with an
empty HTTP 500 (confirmed live). Per the platform docs (`curl -F "file=@…"`), job creation wants
a MULTIPART file upload. These pin that contract for both operations.
"""

from __future__ import annotations

import httpx
import respx

from openreading.adapters.nuextract.adapter import _DEFAULT_BASE_URL, _HttpxNuExtractClient

_RAW = b"%PDF-1.4 fake pdf bytes"
_JOB = {"jobId": "job_123"}


def _client() -> _HttpxNuExtractClient:
    return _HttpxNuExtractClient("test-key", _DEFAULT_BASE_URL)


@respx.mock
def test_content_job_is_multipart_upload_not_octet_stream():
    route = respx.post(f"{_DEFAULT_BASE_URL}/api/content-extraction/jobs").mock(
        return_value=httpx.Response(200, json=_JOB)
    )
    _client().create_content_job(_RAW, filename="scan.pdf")
    req = route.calls.last.request
    assert req.headers.get("content-type", "").startswith("multipart/form-data"), req.headers
    assert _RAW in req.content, "raw file bytes must be uploaded as a multipart part"
    assert b'filename="scan.pdf"' in req.content  # extension drives platform format detection
    assert req.headers.get("authorization") == "Bearer test-key"
    assert "temperature=0" in str(req.url)  # extraction determinism param preserved


@respx.mock
def test_structured_job_is_multipart_upload():
    route = respx.post(f"{_DEFAULT_BASE_URL}/api/structured-extraction/proj-1/jobs").mock(
        return_value=httpx.Response(200, json=_JOB)
    )
    _client().create_structured_job("proj-1", _RAW, filename="w2.png")
    req = route.calls.last.request
    assert req.headers.get("content-type", "").startswith("multipart/form-data"), req.headers
    assert _RAW in req.content and b'filename="w2.png"' in req.content


@respx.mock
def test_filename_defaults_when_absent():
    route = respx.post(f"{_DEFAULT_BASE_URL}/api/content-extraction/jobs").mock(
        return_value=httpx.Response(200, json=_JOB)
    )
    _client().create_content_job(_RAW)
    assert b'filename="document.pdf"' in route.calls.last.request.content
