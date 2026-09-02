"""Gemini Interactions REST request construction via respx; no network access."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from openreading.adapters.google_gemini.adapter import _HttpxGeminiClient
from openreading.types.errors import RetryableError, TerminalError

_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
_OK = {"id": "interaction_1", "status": "completed", "steps": [], "usage": {}}


@respx.mock
def test_interact_posts_v1beta_body_and_api_key_header():
    route = respx.post(_URL).mock(return_value=httpx.Response(200, json=_OK))
    client = _HttpxGeminiClient("gem-test")
    doc = {"type": "document", "data": "abc", "mime_type": "application/pdf"}
    result = client.interact(
        model="gemini-3.6-flash", input=[doc, {"type": "text", "text": "parse"}], store=False
    )

    request = route.calls.last.request
    assert request.headers["x-goog-api-key"] == "gem-test"
    assert json.loads(request.content) == {
        "model": "gemini-3.6-flash",
        "input": [doc, {"type": "text", "text": "parse"}],
        "store": False,
    }
    assert result == _OK


@respx.mock
def test_interact_forwards_structured_response_format():
    route = respx.post(_URL).mock(return_value=httpx.Response(200, json=_OK))
    response_format = {
        "type": "text",
        "mime_type": "application/json",
        "schema": {"type": "object", "properties": {"total": {"type": "number"}}},
    }
    _HttpxGeminiClient("gem-test").interact(
        model="gemini-3.6-flash",
        input=[{"type": "text", "text": "extract"}],
        store=False,
        response_format=response_format,
    )
    assert json.loads(route.calls.last.request.content)["response_format"] == response_format


@respx.mock
def test_429_is_retryable_and_preserves_retry_after():
    respx.post(_URL).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "7"}, text="quota")
    )
    with pytest.raises(RetryableError) as exc:
        _HttpxGeminiClient("gem-test").interact(model="m", input=[], store=False)
    assert exc.value.backend_code == "http_429" and exc.value.retry_after == 7.0


@respx.mock
def test_401_is_terminal_auth_rejected():
    respx.post(_URL).mock(return_value=httpx.Response(401, text="provider echoed gem-test"))
    with pytest.raises(TerminalError) as exc:
        _HttpxGeminiClient("bad").interact(model="m", input=[], store=False)
    assert exc.value.backend_code == "auth_rejected"
    assert "provider echoed" not in str(exc.value)


@respx.mock
def test_invalid_json_is_terminal_malformed_response():
    respx.post(_URL).mock(return_value=httpx.Response(200, text="not json"))
    with pytest.raises(TerminalError) as exc:
        _HttpxGeminiClient("gem-test").interact(model="m", input=[], store=False)
    assert exc.value.backend_code == "malformed_response"
