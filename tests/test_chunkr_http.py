"""Real `_HttpxChunkrClient` request-construction tests (respx, NO network).

The injected-fake tests in test_chunkr.py never exercise the real HTTP client — task creation's
header/body shape and the raw-JSON response boundary are only ever proven against a hand-written
fake, so a request-construction bug (wrong header shape, config not merged, schema key dropped)
would ship invisibly, the same gap shape `test_reducto_http.py` was written to close for reducto's
local-file upload bug. These tests pin the actual wire contract, mirroring that file's shape.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from openreading.adapters.chunkr.adapter import _DEFAULT_BASE_URL, _HttpxChunkrClient
from openreading.types.errors import RetryableError

_PARSE_OK = {"task_id": "task_1", "status": "Processing"}
_EXTRACT_OK = {"task_id": "task_2", "status": "Processing"}


@respx.mock
def test_create_parse_task_merges_document_and_config_with_raw_auth_header():
    route = respx.post(f"{_DEFAULT_BASE_URL}/api/v1/tasks/parse").mock(
        return_value=httpx.Response(200, json=_PARSE_OK)
    )

    out = _HttpxChunkrClient("test-key", _DEFAULT_BASE_URL).create_parse_task(
        {"file_url": "https://example.test/doc.pdf"}, {"webhook_url": "https://cb.test/hook"}
    )

    req = route.calls.last.request
    # chunkr takes the raw key in Authorization, no "Bearer " prefix (unlike open-ocr/qwen-vl).
    assert req.headers.get("authorization") == "test-key"
    body = json.loads(req.content)
    assert body == {
        "file_url": "https://example.test/doc.pdf",
        "webhook_url": "https://cb.test/hook",
    }
    assert out == _PARSE_OK


@respx.mock
def test_create_extract_task_adds_schema_key_alongside_document():
    route = respx.post(f"{_DEFAULT_BASE_URL}/api/v1/tasks/extract").mock(
        return_value=httpx.Response(200, json=_EXTRACT_OK)
    )

    out = _HttpxChunkrClient("test-key", _DEFAULT_BASE_URL).create_extract_task(
        {"file": "base64=="}, {"type": "object", "properties": {}}, {}
    )

    body = json.loads(route.calls.last.request.content)
    assert body["file"] == "base64=="
    assert body["schema"] == {"type": "object", "properties": {}}
    assert out == _EXTRACT_OK


@respx.mock
def test_get_task_fetches_by_id_on_configured_base_url():
    custom_base = "https://chunkr.internal.example:9443"
    respx.get(f"{custom_base}/api/v1/tasks/task_abc").mock(
        return_value=httpx.Response(200, json={"task_id": "task_abc", "status": "Succeeded"})
    )

    # base_url is caller-configurable (self-hosted AGPL deployments point CHUNKR_BASE_URL at
    # their own container) — pin that it's actually threaded through, not hardcoded.
    out = _HttpxChunkrClient("test-key", custom_base).get_task("task_abc")

    assert out["status"] == "Succeeded"


@respx.mock
def test_get_task_error_status_uses_raw_text_not_json():
    # error path must never assume the error body is JSON — chunkr can answer a 500 with plain text.
    respx.get(f"{_DEFAULT_BASE_URL}/api/v1/tasks/bad").mock(
        return_value=httpx.Response(500, text="upstream timeout")
    )

    with pytest.raises(RetryableError) as exc_info:
        _HttpxChunkrClient("test-key", _DEFAULT_BASE_URL).get_task("bad")

    assert "upstream timeout" in str(exc_info.value)


@respx.mock
def test_create_parse_task_raises_cleanly_on_non_json_200_body():
    # Malformed/incomplete response: create_parse_task / create_extract_task / get_task all do a
    # bare `r.json()` on the success path with no guard. This pins today's actual failure mode (an
    # uncaught JSON-decode error, not a silent wrong-shape success) so a future change to that
    # behavior is a deliberate one, not an accidental regression nobody would notice.
    respx.post(f"{_DEFAULT_BASE_URL}/api/v1/tasks/parse").mock(
        return_value=httpx.Response(200, text="<html>not json</html>")
    )

    with pytest.raises(json.JSONDecodeError):
        _HttpxChunkrClient("test-key", _DEFAULT_BASE_URL).create_parse_task(
            {"file_url": "https://example.test/doc.pdf"}, {}
        )
