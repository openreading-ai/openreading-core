"""Real `_HttpxReductoClient` request-construction tests (respx, NO network).

The injected-fake tests in test_reducto.py never exercise the real HTTP client, so a
local-file (base64) intake that sent `document_url: null` went unnoticed. Reducto needs a
two-step flow for local files: POST /upload (multipart) → get a `reducto://…` handle →
POST /parse with that handle as `document_url`. These tests pin that contract.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import httpx
import pytest
import respx

from openreading.adapters.reducto.adapter import _HttpxReductoClient
from openreading.types.errors import TerminalError

_B64 = base64.b64encode(b"%PDF-1.4 fake pdf bytes").decode()
_UPLOAD = {"file_id": "reducto://abc123.pdf", "presigned_url": None}
_PARSE_OK = {"result": {"chunks": []}, "usage": {"num_pages": 1}}


def _signed_webhook_event(secret: str, payload: dict) -> tuple[dict, dict]:
    """A genuinely `Webhook(secret).sign(...)`-produced event dict, with `_raw` populated the way
    `server/app.py`'s webhook route now sets it (BL-82), plus the matching svix-* headers."""
    from svix.webhooks import Webhook

    raw = json.dumps(payload).encode()
    wh = Webhook(secret)
    msg_id = "msg_bl82_test"
    ts = datetime.now(UTC)
    sig = wh.sign(msg_id, ts, raw.decode())
    headers = {"svix-id": msg_id, "svix-timestamp": str(int(ts.timestamp())), "svix-signature": sig}
    event = dict(payload)
    event["_raw"] = raw
    return event, headers


@respx.mock
def test_parse_uploads_local_file_then_sends_handle_as_document_url():
    upload = respx.post("https://platform.reducto.ai/upload").mock(
        return_value=httpx.Response(200, json=_UPLOAD)
    )
    parse = respx.post("https://platform.reducto.ai/parse").mock(
        return_value=httpx.Response(200, json=_PARSE_OK)
    )

    _HttpxReductoClient("test-key").parse({"base64": _B64}, {}, is_async=False)

    assert upload.called, "a local file must be uploaded to /upload before /parse"
    body = json.loads(parse.calls.last.request.content)
    assert body["document_url"] == "reducto://abc123.pdf", body


@respx.mock
def test_parse_with_url_does_not_upload():
    upload = respx.post("https://platform.reducto.ai/upload").mock(
        return_value=httpx.Response(200, json=_UPLOAD)
    )
    parse = respx.post("https://platform.reducto.ai/parse").mock(
        return_value=httpx.Response(200, json=_PARSE_OK)
    )

    _HttpxReductoClient("test-key").parse(
        {"url": "https://example.test/doc.pdf"}, {}, is_async=False
    )

    assert not upload.called, "a URL input must pass straight through, no upload"
    body = json.loads(parse.calls.last.request.content)
    assert body["document_url"] == "https://example.test/doc.pdf", body


@respx.mock
def test_parse_inlines_external_result_url_full_object():
    # Large docs return result as an external URL (expires 1h); the client must fetch + inline it,
    # otherwise result.chunks is absent and we'd silently return an empty parse.
    respx.post("https://platform.reducto.ai/parse").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {"type": "url", "url": "https://storage.reducto.ai/chunks/x.json"},
                "usage": {"num_pages": 42},
            },
        )
    )
    ext = respx.get("https://storage.reducto.ai/chunks/x.json").mock(
        return_value=httpx.Response(
            200, json={"type": "full", "chunks": [{"content": "c", "blocks": []}]}
        )
    )

    raw = _HttpxReductoClient("test-key").parse(
        {"url": "https://example.test/big.pdf"}, {}, is_async=False
    )

    assert ext.called, "external result URL must be fetched"
    assert raw["result"]["type"] == "full" and raw["result"]["chunks"], raw


@respx.mock
def test_parse_inlines_external_result_url_bare_list():
    # Defensive: the external JSON may be a bare chunks list rather than a full result object.
    respx.post("https://platform.reducto.ai/parse").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {"type": "url", "url": "https://storage.reducto.ai/chunks/y.json"},
                "usage": {},
            },
        )
    )
    respx.get("https://storage.reducto.ai/chunks/y.json").mock(
        return_value=httpx.Response(200, json=[{"content": "c", "blocks": []}])
    )

    raw = _HttpxReductoClient("test-key").parse(
        {"url": "https://example.test/big.pdf"}, {}, is_async=False
    )

    assert raw["result"]["chunks"] == [{"content": "c", "blocks": []}], raw


@respx.mock
def test_extract_uploads_local_file_then_sends_handle_as_document_url():
    upload = respx.post("https://platform.reducto.ai/upload").mock(
        return_value=httpx.Response(200, json=_UPLOAD)
    )
    extract = respx.post("https://platform.reducto.ai/extract").mock(
        return_value=httpx.Response(200, json={"result": {"data": {}}})
    )

    _HttpxReductoClient("test-key").extract({"base64": _B64}, {"type": "object"}, is_async=False)

    assert upload.called, "extract on a local file must also upload first"
    body = json.loads(extract.calls.last.request.content)
    assert body["document_url"] == "reducto://abc123.pdf", body


def test_verify_webhook_fails_closed_without_a_configured_secret():
    # BL-50: the client-side backstop must reject, not silently accept, an event when no webhook
    # secret is configured — independent of the server layer (server/app.py never reaches this
    # path today; it builds a fresh client=None adapter so resolve_webhook doesn't re-verify), the
    # backstop's own default must not itself be fail-open.
    assert _HttpxReductoClient("test-key").verify_webhook({}, {}) is False


def test_verify_webhook_accepts_a_genuinely_signed_event():
    # BL-82: verify_webhook always checked body.get("_raw", b"") against the `event` dict
    # resolve_webhook passes straight through — but nothing anywhere ever set event["_raw"], so a
    # genuinely, correctly-signed event was rejected every single time this method was actually
    # reached with a bound client. The two pre-existing resolve_webhook-level tests
    # (test_async_webhook_resolves_with_signature_verify_and_idempotent,
    # test_webhook_bad_signature_is_terminal in tests/test_reducto.py) bind a FakeReductoClient
    # whose own verify_webhook is a canned stub (`return self._verify`), so neither one exercises
    # this method's real body at all. This is the missing positive case: a real client, a real
    # secret, and a genuine Webhook(secret).sign(...) signature, with _raw populated exactly like
    # the now-fixed webhook route produces it.
    secret = "whsec_" + base64.b64encode(b"openreading-bl82-positive-case").decode()
    event, headers = _signed_webhook_event(secret, {"job_id": "job_1", "data": {"ok": True}})
    client = _HttpxReductoClient("test-key", secret)
    assert client.verify_webhook(headers, event) is True


def test_verify_webhook_wraps_a_bad_signature_as_terminal_error():
    # BL-82 part 2: verify_webhook let svix's own WebhookVerificationError escape uncaught — a type
    # outside this codebase's _ADAPTER_ERRORS (server/app.py), unlike every other adapter-error path
    # (e.g. poll()'s own `except Exception as e: raise self._map_error(e) from e` two methods above
    # this one). A genuinely invalid signature must raise this codebase's own structured error.
    secret = "whsec_" + base64.b64encode(b"openreading-bl82-real-secret").decode()
    wrong_secret = "whsec_" + base64.b64encode(b"openreading-bl82-wrong-secret").decode()
    event, headers = _signed_webhook_event(secret, {"job_id": "job_1", "data": {}})
    client = _HttpxReductoClient("test-key", wrong_secret)  # bound to a different secret
    with pytest.raises(TerminalError, match="signature") as exc_info:
        client.verify_webhook(headers, event)
    assert exc_info.value.backend_code == "bad_signature"
