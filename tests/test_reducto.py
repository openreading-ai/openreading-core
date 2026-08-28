"""Reducto adapter — REAL captured parse/extract fixtures via an injected fake client (NO
network). Exercises: INLINE sync parse → chunks/blocks, WEBHOOK async (register + resolve with
signature verify + idempotent replay), POLL fallback, /extract → typed_fields with citations,
and the retryable rate-limit codes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openreading.adapters.reducto import ReductoAdapter
from openreading.router import FakeClock, await_result
from openreading.testing import ConformanceCase, check_adapter_conformance
from openreading.types import BlockType, JobState
from openreading.types.errors import RetryableError, TerminalError
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RunContext

FIX = Path(__file__).parent / "fixtures" / "reducto"


def _fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


class FakeReductoClient:
    def __init__(self, verify=True, raise_exc=None, job_result=None):
        self._verify = verify
        self._raise = raise_exc
        self._job_result = job_result
        self.verify_calls = 0
        # last request body actually sent to the vendor's parse endpoint, so tests can assert
        # on what was sent (not just the resulting job.wait_mode).
        self.last_parse_options: dict | None = None

    def parse(self, document, options, is_async):
        self.last_parse_options = options
        if self._raise:
            raise self._raise
        return {"job_id": "job_parse_1"} if is_async else _fixture("parse")

    def extract(self, document, schema, is_async):
        return _fixture("extract")

    def get_job(self, job_id):
        return self._job_result or _fixture("parse")

    def verify_webhook(self, headers, body):
        self.verify_calls += 1
        return self._verify


class Rate1000(Exception):
    code = "1000"


def _req(op="parse", **over) -> OpenReadingRequest:
    body = {
        "document": {"url": "https://example.test/doc.pdf", "mime_type": "application/pdf"},
        "backend": {"id": "reducto", "type": "hosted_api", "operation": op},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


def _run_sync(adapter, req):
    ctx = RunContext()
    return adapter.normalize(adapter.submit(req, ctx), ctx, req)


def test_reducto_conforms():
    # Phase B.1: reducto is remediated — its text channel is a plain projection (C1) and its
    # requested N/D channels are delivered (C6), so both are promoted from advisory to strict.
    check_adapter_conformance(
        ReductoAdapter(client=FakeReductoClient()),
        [ConformanceCase(request=_req(), deterministic=True, label="parse")],
        strict_checks={"C1", "C6"},
        # Ledger T4a (R1/R2/R3): a fresh instance with its own separate fake client — proves
        # resume has no instance-affinity requirement (AC-5) and nothing is cached on self (AC-7).
        adapter_factory=lambda: ReductoAdapter(client=FakeReductoClient()),
    )


def test_inline_parse_maps_chunks_and_blocks():
    resp = _run_sync(ReductoAdapter(client=FakeReductoClient()), _req())
    blocks = resp.document.pages[0].blocks
    assert any(b.type is BlockType.TITLE and b.text == "Loan Application" for b in blocks)
    assert any(b.type is BlockType.TABLE for b in blocks)
    assert any(b.type is BlockType.SIGNATURE for b in blocks)
    # RAG chunks preserved with block ids
    assert len(resp.chunks) == 2
    assert resp.chunks[0].block_ids
    # normalized bbox from {left,top,width,height}
    title = next(b for b in blocks if b.type is BlockType.TITLE)
    assert title.bbox.x == pytest.approx(0.1) and title.bbox.w == pytest.approx(0.5)


def test_webhook_resolves_with_signature_verify_and_idempotent():
    # Ledger T4a: the driver's own `WebhookBus` push-await mechanism was deleted as genuinely
    # dead code (no production call site ever passed one) — `resolve_webhook` is the live,
    # genuinely-implemented mechanism (server/app.py's POST /v1/webhooks/{backend_id} handler),
    # so this test calls it directly rather than routing a scripted event through the deleted bus.
    client = FakeReductoClient(verify=True)
    adapter = ReductoAdapter(client=client)
    req = _req(op="parse", **{"async": {"mode": "async", "webhook_url": "https://me.test/hook"}})
    job = adapter.submit(req, RunContext())
    assert job.wait_mode.value == "webhook" and job.state is JobState.RUNNING
    event = {"job_id": "job_parse_1", "data": _fixture("parse"), "headers": {"svix-id": "x"}}
    job = adapter.resolve_webhook(event, job, RunContext())
    assert job.state is JobState.SUCCEEDED
    assert client.verify_calls == 1  # signature was verified
    # idempotent: a duplicate delivery after terminal is a no-op
    again = adapter.resolve_webhook(event, job, RunContext())
    assert again.state is JobState.SUCCEEDED


def test_webhook_bad_signature_is_terminal():
    adapter = ReductoAdapter(client=FakeReductoClient(verify=False))
    req = _req(op="parse", **{"async": {"mode": "async", "webhook_url": "https://me.test/hook"}})
    job = adapter.submit(req, RunContext())
    with pytest.raises(TerminalError, match="signature"):
        adapter.resolve_webhook(
            {"job_id": "job_parse_1", "data": {}, "headers": {}}, job, RunContext()
        )


async def test_async_poll_fallback_when_no_webhook_url():
    adapter = ReductoAdapter(client=FakeReductoClient(job_result=_fixture("parse")))
    req = _req(op="parse", **{"async": {"mode": "async"}})  # no webhook_url → POLL
    job = adapter.submit(req, RunContext())
    assert job.wait_mode.value == "poll"
    job = await await_result(adapter, job, ctx=RunContext(), deadline_ms=1e9, clock=FakeClock())
    assert job.state is JobState.SUCCEEDED
    resp = adapter.normalize(job, RunContext(), req)
    assert resp.document.pages


def test_webhook_url_forwarded_to_vendor_on_parse():
    """BL-67: without this, the vendor is never told to call back and a WEBHOOK job hangs at
    "running" forever — job.wait_mode flipping to WEBHOOK alone doesn't prove the vendor knows."""
    client = FakeReductoClient()
    adapter = ReductoAdapter(client=client)
    req = _req(op="parse", **{"async": {"mode": "async", "webhook_url": "https://me.test/hook"}})
    adapter.submit(req, RunContext())
    assert client.last_parse_options is not None
    assert client.last_parse_options["webhook_url"] == "https://me.test/hook"
    # the pre-existing jsonbbox option must survive the merge
    assert client.last_parse_options["advanced_options"] == {"table_output_format": "jsonbbox"}


def test_no_webhook_url_omits_field_from_parse_options():
    client = FakeReductoClient()
    adapter = ReductoAdapter(client=client)
    req = _req(op="parse", **{"async": {"mode": "async"}})  # no webhook_url → POLL
    adapter.submit(req, RunContext())
    assert client.last_parse_options is not None
    assert "webhook_url" not in client.last_parse_options


def test_extract_typed_fields_with_citations():
    resp = _run_sync(ReductoAdapter(client=FakeReductoClient()), _req(op="extract"))
    assert resp.typed_fields["invoice_total"].value == "128.50"
    cits = resp.typed_fields["invoice_total"].citations
    assert cits and cits[0].page == 1 and cits[0].bbox is not None


def test_rate_limit_code_maps_to_retryable():
    adapter = ReductoAdapter(client=FakeReductoClient(raise_exc=Rate1000("slow down")))
    with pytest.raises(RetryableError):
        adapter.submit(_req(), RunContext())


def test_cost_is_credit_based():
    adapter = ReductoAdapter(client=FakeReductoClient())
    job = adapter.submit(_req(), RunContext())
    cost = adapter.report_cost(job)
    assert cost.native_unit == "credit" and cost.billing_target == "caller_account"
    assert cost.native_quantity == 1.0


@pytest.mark.live
def test_live_parse():  # pragma: no cover
    from tests.live_helpers import run_live, sample_pdf_request, skip_unless_creds

    skip_unless_creds("reducto")
    resp = run_live("reducto", ReductoAdapter(), sample_pdf_request("reducto"))
    assert resp.document.text or resp.document.markdown
