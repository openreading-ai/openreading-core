"""Real `_HttpxAzureClient` request-construction tests (respx, NO network).

The injected-fake tests in test_azure_document_intelligence.py never exercise the real HTTP
client, so `analyze()`'s unguarded `r.headers["operation-location"]` (a direct dict-key access
with no guard) has zero coverage anywhere: a 202 response missing that header — plausible, not
contrived, since Azure's own LRO contract is what makes the header meaningful in the first
place — produces a bare `KeyError`. It IS caught by `submit()`'s broad exception handler, so this
isn't a raw crash, but it degrades to Python's default `KeyError` string instead of this
codebase's own error taxonomy (`auth_rejected` / `doc_too_large` / retryable-with-`retry_after`).
These tests pin both the client-level failure and its exact shape once `submit()` wraps it,
mirroring `test_reducto_http.py`'s shape.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx

from openreading.adapters.azure_document_intelligence import AzureDocumentIntelligenceAdapter
from openreading.adapters.azure_document_intelligence.adapter import (
    _API_VERSION,
    _HttpxAzureClient,
)
from openreading.types.errors import TerminalError
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RunContext

_ENDPOINT = "https://myres.cognitiveservices.azure.com"
DOC_B64 = base64.b64encode(b"%PDF-1.7 fake").decode()


def _req(op="prebuilt-layout") -> OpenReadingRequest:
    return OpenReadingRequest.model_validate(
        {
            "document": {"bytes_base64": DOC_B64, "mime_type": "application/pdf"},
            "backend": {
                "id": "azure-document-intelligence",
                "type": "hosted_api",
                "operation": op,
            },
        }
    )


@respx.mock
def test_analyze_sends_api_version_and_content_format_params_with_subscription_key_header():
    op_loc = f"{_ENDPOINT}/documentintelligence/documentModels/prebuilt-layout/analyzeResults/op1"
    route = respx.post(
        f"{_ENDPOINT}/documentintelligence/documentModels/prebuilt-layout:analyze"
    ).mock(return_value=httpx.Response(202, headers={"operation-location": op_loc}))

    out = _HttpxAzureClient(_ENDPOINT, "test-key").analyze(
        "prebuilt-layout", {"urlSource": "https://example.test/doc.pdf"}, "markdown"
    )

    req = route.calls.last.request
    assert req.headers.get("ocp-apim-subscription-key") == "test-key"
    assert req.url.params.get("api-version") == _API_VERSION
    assert req.url.params.get("outputContentFormat") == "markdown"
    assert json.loads(req.content) == {"urlSource": "https://example.test/doc.pdf"}
    assert out == op_loc


@respx.mock
def test_analyze_202_missing_operation_location_header_raises_keyerror():
    # Malformed/incomplete response: the ready-made repro this finding names directly.
    respx.post(f"{_ENDPOINT}/documentintelligence/documentModels/prebuilt-layout:analyze").mock(
        return_value=httpx.Response(202, json={})
    )

    with pytest.raises(KeyError, match="operation-location"):
        _HttpxAzureClient(_ENDPOINT, "test-key").analyze(
            "prebuilt-layout", {"urlSource": "https://example.test/doc.pdf"}, "markdown"
        )


@respx.mock
def test_submit_wraps_missing_operation_location_as_terminal_error_with_keyerror_backend_code():
    # Same repro, one layer up: proves the finding's exact claim about what a real caller
    # observes — not a raw crash (submit()'s `except Exception as e: raise self._map_error(e)`
    # does catch it) but an uninformative bare-KeyError message/backend_code, not this codebase's
    # structured taxonomy.
    respx.post(f"{_ENDPOINT}/documentintelligence/documentModels/prebuilt-layout:analyze").mock(
        return_value=httpx.Response(202, json={})
    )
    adapter = AzureDocumentIntelligenceAdapter(client=_HttpxAzureClient(_ENDPOINT, "test-key"))

    with pytest.raises(TerminalError) as exc_info:
        adapter.submit(_req(), RunContext())

    assert exc_info.value.backend_code == "KeyError"
    assert "operation-location" in str(exc_info.value)


@respx.mock
def test_get_parses_status_body_and_retry_after_header():
    op_loc = f"{_ENDPOINT}/documentintelligence/documentModels/prebuilt-layout/analyzeResults/op1"
    respx.get(op_loc).mock(
        return_value=httpx.Response(200, json={"status": "running"}, headers={"retry-after": "2"})
    )

    resp = _HttpxAzureClient(_ENDPOINT, "test-key").get(op_loc)

    assert resp.status_code == 200
    assert resp.body == {"status": "running"}
    assert resp.retry_after == 2.0


@respx.mock
def test_get_handles_empty_body_without_crashing():
    # Malformed/incomplete response: a poll response can legitimately arrive with no body at all
    # (a bare 202 mid-processing) — `get()` guards this with `r.json() if r.content else {}`; pin
    # that an empty body degrades to `{}`, not an uncaught JSON-decode crash on an empty `.json()`
    # call — the graceful counterpart to `analyze()`'s ungraceful header access above.
    op_loc = f"{_ENDPOINT}/documentintelligence/documentModels/prebuilt-layout/analyzeResults/op2"
    respx.get(op_loc).mock(return_value=httpx.Response(202, content=b""))

    resp = _HttpxAzureClient(_ENDPOINT, "test-key").get(op_loc)

    assert resp.body == {}
    assert resp.status_code == 202
