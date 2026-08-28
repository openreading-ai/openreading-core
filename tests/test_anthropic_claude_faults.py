"""Anthropic Claude adapter — fault-injection for the branches the happy-path fixtures skip
(adapter was 89%): document-input resolution (path read, url-only rejection, unreadable path at
normalize time), health without the SDK, client construction from resolved credentials, the
already-typed-error passthrough, every _map_error class (including the retry-after header parse
and its guard), and the native-batch seams (submit_many + poll error mapping). All offline."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

from openreading.adapters.anthropic_claude import AnthropicClaudeAdapter
from openreading.adapters.anthropic_claude import adapter as adapter_mod
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types import JobState
from openreading.types.enums import WaitMode
from openreading.types.errors import MissingCredentialsError, RetryableError, TerminalError
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RawResult, ResolvedCredentials, RunContext

FIX = Path(__file__).parent / "fixtures" / "anthropic-claude"
PDF_B64 = base64.b64encode(b"%PDF-1.7 fake").decode()


def _fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


def _req(**over) -> OpenReadingRequest:
    body = {
        "document": {"bytes_base64": PDF_B64, "mime_type": "application/pdf"},
        "backend": {"id": "anthropic-claude"},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


class _FakeClient:
    """Serves the parse fixture from every entry point; `raise_exc` turns one into a fault."""

    def __init__(self, raise_on: str | None = None, exc: Exception | None = None) -> None:
        self._raise_on = raise_on
        self._exc = exc or RuntimeError("boom")
        self.last_call: dict | None = None

    def _maybe_raise(self, hook: str) -> None:
        if self._raise_on == hook:
            raise self._exc

    def create(self, *, model, max_tokens, messages, tools=None, tool_choice=None):
        self._maybe_raise("create")
        self.last_call = {"model": model, "messages": messages}
        return _fixture("parse")

    def create_batch(self, requests: list[dict]) -> dict:
        self._maybe_raise("create_batch")
        return {"id": "msgbatch_1", "processing_status": "in_progress"}

    def get_batch(self, batch_id: str) -> dict:
        self._maybe_raise("get_batch")
        return {"id": batch_id, "processing_status": "ended"}

    def get_results(self, batch_id: str) -> list[dict]:
        self._maybe_raise("get_results")
        return [
            {"custom_id": "item-0", "result": {"type": "succeeded", "message": _fixture("parse")}}
        ]


class OverloadedError(Exception):
    """Name-matched by the adapter's retryable set (the SDK classes are matched by class name)."""


class RateLimitError(Exception):
    def __init__(self, message: str, response=None) -> None:
        super().__init__(message)
        if response is not None:
            self.response = response


class _Response:
    def __init__(self, headers) -> None:
        self.headers = headers


class _HeaderlessResponse:
    """A truthy response object that never grew a `.headers` — the AttributeError guard."""


# ---- document input resolution ------------------------------------------------------------


def test_path_input_is_read_and_sent_as_base64(tmp_path):
    pdf = tmp_path / "loan.pdf"
    pdf.write_bytes(build_sample_pdf())
    client = _FakeClient()
    adapter = AnthropicClaudeAdapter(client=client)
    req = _req(document={"path": str(pdf), "mime_type": "application/pdf"})
    ctx = RunContext()
    resp = adapter.normalize(adapter.submit(req, ctx), ctx, req)
    assert client.last_call is not None
    sent = client.last_call["messages"][0]["content"][0]["source"]["data"]
    assert sent == base64.b64encode(pdf.read_bytes()).decode()
    # the same path also feeds pdf_page_count → the exact count, not the citation heuristic (1)
    assert resp.document.page_count == 2


def test_url_only_input_is_unsupported():
    adapter = AnthropicClaudeAdapter(client=_FakeClient())
    req = _req(document={"url": "https://example.com/loan.pdf", "mime_type": "application/pdf"})
    with pytest.raises(TerminalError) as exc:
        adapter.submit(req, RunContext())
    assert exc.value.backend_code == "unsupported_input"


@pytest.mark.parametrize(
    "document",
    [
        {"url": "https://example.com/loan.pdf", "mime_type": "application/pdf"},
        {"path": "/definitely/not/here.pdf", "mime_type": "application/pdf"},
    ],
)
def test_page_count_falls_back_to_citations_when_bytes_are_unavailable(document):
    # normalize must not crash when the PDF bytes cannot be re-read (url input, or a path that
    # vanished between submit and normalize) — page_count degrades to the citation heuristic.
    adapter = AnthropicClaudeAdapter(client=_FakeClient())
    job = adapter.new_job(WaitMode.INLINE, state=JobState.SUCCEEDED)
    job.raw = RawResult(
        payload=_fixture("parse"),
        media_type="application/json",
        encoding="json",
        object_class="parse",
    )
    resp = adapter.normalize(job, RunContext(), _req(document=document))
    assert resp.document.page_count == 1  # one distinct page_location citation


# ---- health / client construction -----------------------------------------------------------


def test_health_reports_the_missing_sdk(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)
    h = AnthropicClaudeAdapter().health()  # no injected client → the SDK import decides
    assert not h.ready and any("anthropic" in d for d in h.missing_deps)


def test_client_is_built_from_the_resolved_api_key(monkeypatch):
    seen: dict = {}

    class _Stub:
        def __init__(self, api_key) -> None:
            seen["api_key"] = api_key

    monkeypatch.setattr(adapter_mod, "_RealAnthropicClient", _Stub)
    ctx = RunContext(credentials=ResolvedCredentials(values={"api_key": "sk-test"}))
    client = AnthropicClaudeAdapter()._get_client(ctx)
    assert isinstance(client, _Stub) and seen["api_key"] == "sk-test"


def test_get_client_raises_missing_credentials_without_api_key(monkeypatch):
    """QA closing pass (ui-app): no `api_key` resolved (the broker found no ANTHROPIC_API_KEY) used
    to construct `_RealAnthropicClient` anyway — `anthropic.Anthropic()` builds fine with no key at
    all, and only raises a raw `TypeError` deep inside the first `messages.create()` call, which
    rendered as an unnamed "Backend error" instead of the named MissingCredentialsError panel every
    correctly-required adapter gets. `_get_client` must now fail the same honest way, before any
    client call, and — crucially — before `_RealAnthropicClient` (which imports the real `anthropic`
    SDK) is even constructed."""

    def _boom(*_a, **_k):  # pragma: no cover - must never be reached
        raise AssertionError("_RealAnthropicClient must not be constructed with no api_key")

    monkeypatch.setattr(adapter_mod, "_RealAnthropicClient", _boom)
    ctx = RunContext(credentials=ResolvedCredentials(values={}))
    with pytest.raises(MissingCredentialsError) as exc:
        AnthropicClaudeAdapter()._get_client(ctx)
    assert "ANTHROPIC_API_KEY" in str(exc.value)
    assert exc.value.missing == ["ANTHROPIC_API_KEY"]


# ---- submit error mapping ---------------------------------------------------------------------


def test_submit_reraises_taxonomy_errors_unchanged():
    boom = TerminalError("nope", backend_code="auth_rejected")
    adapter = AnthropicClaudeAdapter(client=_FakeClient(raise_on="create", exc=boom))
    with pytest.raises(TerminalError) as exc:
        adapter.submit(_req(), RunContext())
    assert exc.value.backend_code == "auth_rejected"  # not remapped to "TerminalError"


@pytest.mark.parametrize(
    "exc,retryable,code",
    [
        (type("AuthenticationError", (Exception,), {})("bad key"), False, "auth_rejected"),
        (type("PermissionDeniedError", (Exception,), {})("no access"), False, "auth_rejected"),
        (type("RequestTooLargeError", (Exception,), {})("nope"), False, "doc_too_large"),
        (ValueError("input exceeds the maximum of 32MB"), False, "doc_too_large"),
        (ValueError("over the page limit"), False, "doc_too_large"),
        (OverloadedError("try later"), True, "OverloadedError"),
        (ValueError("malformed"), False, "ValueError"),
    ],
)
def test_map_error_classification(exc, retryable, code):
    mapped = AnthropicClaudeAdapter()._map_error(exc)
    assert isinstance(mapped, RetryableError if retryable else TerminalError)
    assert mapped.backend_code == code


def test_retry_after_header_is_parsed_onto_the_retryable_error():
    exc = RateLimitError("slow down", response=_Response({"retry-after": "30"}))
    mapped = AnthropicClaudeAdapter()._map_error(exc)
    assert isinstance(mapped, RetryableError) and mapped.retry_after == 30.0


@pytest.mark.parametrize(
    "response",
    [
        _Response({}),  # header absent → float(None) → TypeError
        _Response({"retry-after": "soon"}),  # unparseable → ValueError
        _HeaderlessResponse(),  # no .headers at all → AttributeError
    ],
)
def test_unparseable_retry_after_degrades_to_none_not_a_crash(response):
    mapped = AnthropicClaudeAdapter()._map_error(RateLimitError("slow down", response=response))
    assert isinstance(mapped, RetryableError) and mapped.retry_after is None


# ---- native batch seams -------------------------------------------------------------------


def test_submit_many_reraises_taxonomy_errors_unchanged():
    boom = RetryableError("throttled", backend_code="429")
    adapter = AnthropicClaudeAdapter(client=_FakeClient(raise_on="create_batch", exc=boom))
    with pytest.raises(RetryableError) as exc:
        adapter.submit_many([_req()], RunContext())
    assert exc.value.backend_code == "429"


def test_submit_many_maps_unexpected_error():
    adapter = AnthropicClaudeAdapter(
        client=_FakeClient(raise_on="create_batch", exc=ValueError("boom"))
    )
    with pytest.raises(TerminalError) as exc:
        adapter.submit_many([_req()], RunContext())
    assert exc.value.backend_code == "ValueError"


def test_poll_on_a_single_inline_job_is_a_noop():
    adapter = AnthropicClaudeAdapter(client=_FakeClient())
    job = adapter.submit(_req(), RunContext())  # INLINE → no batch_id in the poll handle
    assert adapter.poll(job, RunContext()) is job and job.state is JobState.SUCCEEDED


def test_poll_get_batch_error_is_mapped():
    adapter = AnthropicClaudeAdapter(
        client=_FakeClient(raise_on="get_batch", exc=RuntimeError("upstream 500"))
    )
    job = adapter.submit_many([_req()], RunContext())
    with pytest.raises(TerminalError) as exc:
        adapter.poll(job, RunContext())
    assert exc.value.backend_code == "RuntimeError"


def test_poll_results_fetch_error_is_mapped():
    adapter = AnthropicClaudeAdapter(
        client=_FakeClient(raise_on="get_results", exc=OverloadedError("results not ready"))
    )
    job = adapter.submit_many([_req()], RunContext())
    with pytest.raises(RetryableError):  # the batch ended, the JSONL fetch is retryable
        adapter.poll(job, RunContext())
