"""Real `_HttpxQwenClient` request-construction tests (respx, NO network).

The injected-fake tests in test_qwen_vl.py never exercise the real HTTP client — the OpenAI-
compatible chat body shape, the optional Bearer auth (qwen-vl is `auth="none"` by descriptor;
BYO endpoints are commonly unauthenticated), and the response-parsing boundary are only ever
proven against a hand-written fake. `chat()`'s own `r.json()["choices"][0]` is the identical
unguarded-access shape as `_HttpxAzureClient.analyze()`'s `r.headers["operation-location"]`
(both a direct index/key access on an assumed-present response field, no guard) — the same bug
class, just not yet named as its own finding. These tests pin the actual wire contract, mirroring
`test_reducto_http.py`'s shape.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from openreading.adapters.qwen_vl.adapter import _HttpxQwenClient
from openreading.types.errors import RetryableError, TerminalError

_ENDPOINT = "http://localhost:8000/v1"
_OK = {"choices": [{"message": {"content": "<div>hi</div>"}, "finish_reason": "stop"}]}


@respx.mock
def test_chat_posts_openai_compatible_body_with_bearer_auth():
    route = respx.post(f"{_ENDPOINT}/chat/completions").mock(
        return_value=httpx.Response(200, json=_OK)
    )

    content, finish_reason = _HttpxQwenClient(_ENDPOINT, "test-key").chat(
        "data:image/png;base64,abc", "QwenVL HTML", "Qwen/Qwen3-VL-8B-Instruct"
    )

    req = route.calls.last.request
    assert req.headers.get("authorization") == "Bearer test-key"
    body = json.loads(req.content)
    assert body["model"] == "Qwen/Qwen3-VL-8B-Instruct"
    assert body["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                {"type": "text", "text": "QwenVL HTML"},
            ],
        }
    ]
    assert content == "<div>hi</div>"
    assert finish_reason == "stop"


@respx.mock
def test_chat_without_api_key_omits_authorization_header():
    # descriptor declares auth="none" — a BYO endpoint is commonly unauthenticated; the client
    # must not send a bogus "Bearer None" header in that case.
    route = respx.post(f"{_ENDPOINT}/chat/completions").mock(
        return_value=httpx.Response(200, json=_OK)
    )

    _HttpxQwenClient(_ENDPOINT).chat("data:image/png;base64,abc", "prompt", "model-x")

    assert "authorization" not in route.calls.last.request.headers


@respx.mock
def test_chat_503_raises_retryable_with_fixed_retry_after():
    # cold-start signal: a self-hosted endpoint loading weights answers 503 while warming up.
    respx.post(f"{_ENDPOINT}/chat/completions").mock(return_value=httpx.Response(503))

    with pytest.raises(RetryableError) as exc_info:
        _HttpxQwenClient(_ENDPOINT, "test-key").chat("data:image/png;base64,abc", "p", "m")

    assert exc_info.value.backend_code == "503"
    assert exc_info.value.retry_after == 5.0


@respx.mock
def test_chat_401_raises_terminal_auth_rejected():
    respx.post(f"{_ENDPOINT}/chat/completions").mock(
        return_value=httpx.Response(401, text="invalid api key")
    )

    with pytest.raises(TerminalError) as exc_info:
        _HttpxQwenClient(_ENDPOINT, "bad-key").chat("data:image/png;base64,abc", "p", "m")

    assert exc_info.value.backend_code == "auth_rejected"


@respx.mock
def test_chat_empty_choices_raises_indexerror():
    # Malformed/incomplete response: `choice = r.json()["choices"][0]` assumes at least one
    # choice is always present with no guard — the same unguarded-access bug class this sprint's
    # finding names for `_HttpxAzureClient.analyze()`'s missing-header access. An OpenAI-compatible
    # server can legitimately answer 200 with an empty choices list (e.g. a content-filter path on
    # some servers) — pin today's actual behavior (an uncaught IndexError) rather than leaving it
    # untested.
    respx.post(f"{_ENDPOINT}/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": []})
    )

    with pytest.raises(IndexError):
        _HttpxQwenClient(_ENDPOINT, "test-key").chat("data:image/png;base64,abc", "p", "m")
