"""Real `_HttpxPulseClient` request-construction tests (respx, NO network).

The injected-fake tests never exercise the real HTTP client, so the local-file path shipped
sending the PDF as base64 inside a JSON body — which Pulse's /extract rejects with
REQ_001 "No file or URL provided" (confirmed live). Per the docs, /extract wants a
MULTIPART file upload (binary `file` field) for local files, and JSON `file_url` for URLs.
These pin that contract.
"""

from __future__ import annotations

import base64
import json

import httpx
import respx

from openreading.adapters.pulse.adapter import _BASE_URL, _HttpxPulseClient

_RAW = b"%PDF-1.4 fake pdf bytes"
_B64 = base64.b64encode(_RAW).decode()
_OK = {"markdown": "# hi"}


@respx.mock
def test_local_file_is_multipart_upload_not_base64_json():
    route = respx.post(f"{_BASE_URL}/extract").mock(return_value=httpx.Response(200, json=_OK))
    _HttpxPulseClient("test-key").extract({"file": _B64, "filename": "doc.pdf"}, {"async": False})
    req = route.calls.last.request
    assert req.headers.get("content-type", "").startswith("multipart/form-data"), req.headers
    assert _RAW in req.content, "raw file bytes must be uploaded"
    assert _B64.encode() not in req.content, "the base64 blob must NOT be sent (that's the bug)"
    assert req.headers.get("x-api-key") == "test-key"


@respx.mock
def test_url_is_json_file_url():
    route = respx.post(f"{_BASE_URL}/extract").mock(return_value=httpx.Response(200, json=_OK))
    _HttpxPulseClient("test-key").extract(
        {"file_url": "https://example.test/doc.pdf"}, {"async": False}
    )
    req = route.calls.last.request
    assert req.headers.get("content-type", "").startswith("application/json"), req.headers
    assert json.loads(req.content)["file_url"] == "https://example.test/doc.pdf"
