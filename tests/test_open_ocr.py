"""open-ocr adapter — tested against documented-shape fixtures via an injected fake client (NO
network). Fixtures mirror the /v1/ocr response shape from open-ocr.com/docs/api (accessed
2026-07-27); they upgrade to captured-live the first time `make verify-live` runs with
OPENOCR_API_KEY. Exercises the sync INLINE path, the async POLL/WEBHOOK paths, engine/lang/
page-count plumbing, the Idempotency-Key pass-through, and the BILLED-basis cost report."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openreading.adapters.open_ocr import OpenOCRAdapter
from openreading.router.clock import FakeClock
from openreading.router.driver import run_to_completion
from openreading.testing import ConformanceCase, check_adapter_conformance
from openreading.types import JobState
from openreading.types.enums import WaitMode
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RunContext

FIX = Path(__file__).parent / "fixtures" / "open-ocr"


def _fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


class FakeOpenOCRClient:
    """Replays the documented-shape fixture over the sync path (create returns the completed
    response — mode:sync). Records the request body and idempotency key for plumbing asserts."""

    def __init__(self) -> None:
        self.last_body: dict | None = None
        self.last_idempotency_key: str | None = None

    def create_ocr(self, body, idempotency_key):
        self.last_body = body
        self.last_idempotency_key = idempotency_key
        return _fixture("ocr")

    def get_request(self, request_id):
        return _fixture("ocr")


class _AsyncFake(FakeOpenOCRClient):
    """create returns 202-style processing; the result arrives via poll (or webhook refetch)."""

    def create_ocr(self, body, idempotency_key):
        super().create_ocr(body, idempotency_key)
        return {"request_id": "req_async_1", "status": "processing"}


def _req(**over) -> OpenReadingRequest:
    body = {
        "document": {"url": "https://example.com/loan.png", "mime_type": "image/png"},
        "backend": {"id": "open-ocr"},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


def _run(adapter, req):
    job = adapter.submit(req, RunContext())
    clock = FakeClock()
    job = run_to_completion(
        adapter, job, ctx=RunContext(), deadline_ms=clock.now_ms() + 120_000, clock=clock
    )
    assert job.state is JobState.SUCCEEDED
    return adapter.normalize(job, RunContext(), req)


# --- conformance -----------------------------------------------------------------------


def test_open_ocr_conforms():
    adapter = OpenOCRAdapter(client=FakeOpenOCRClient())
    # C1 (text is plain — the OCR blob has no markup) and C6 (markdown=D is now DELIVERED,
    # not silently absent) are remediated → promoted to strict violations.
    check_adapter_conformance(
        adapter,
        [ConformanceCase(request=_req(), deterministic=True, label="ocr")],
        strict_checks={"C1", "C6"},
        # Ledger T4a (R1/R2/R3): a fresh instance with its own separate fake client — proves
        # resume has no instance-affinity requirement (AC-5) and nothing is cached on self (AC-7).
        adapter_factory=lambda: OpenOCRAdapter(client=FakeOpenOCRClient()),
    )


# --- markdown (D) delivery + document-level signals (P1/P2) -----------------------------


def test_markdown_delivered_equals_text():
    """P1: markdown=D was silently None. A structure-free backend's markdown is the plain OCR
    text (§4.2 — honest, but must be POPULATED, not absent); provenance marks it derived."""
    resp = _run(OpenOCRAdapter(client=FakeOpenOCRClient()), _req())
    assert resp.document.markdown is not None
    assert resp.document.markdown == resp.document.text  # structure-free: md == text
    assert resp.channel_provenance == {"text": "native", "markdown": "derived"}


def test_markdown_suppressed_when_not_requested():
    resp = _run(OpenOCRAdapter(client=FakeOpenOCRClient()), _req(outputs={"markdown": False}))
    assert resp.document.markdown is None
    assert resp.document.text is not None  # text still delivered


def test_document_confidence_and_language():
    """P2: the overall confidence scalar (dropped when backend_raw off) → document.confidence
    [0,1]; applied_settings.language → Document.language."""
    resp = _run(OpenOCRAdapter(client=FakeOpenOCRClient()), _req())
    assert resp.document.confidence == pytest.approx(0.94)
    assert resp.document.language == ["en"]


def test_document_confidence_survives_backend_raw_off():
    resp = _run(
        OpenOCRAdapter(client=FakeOpenOCRClient()),
        _req(outputs={"include_backend_raw": False}),
    )
    assert resp.backend_raw is None
    assert resp.document.confidence == pytest.approx(0.94)  # no longer dropped with raw off


def test_backend_version_carries_engine_name():
    """§9 compare-hazard: the ~20-engine variance is only visible in backend.version (compare
    keys on id + version) — it must carry the aggregated engine that actually ran."""
    resp = _run(OpenOCRAdapter(client=FakeOpenOCRClient()), _req())
    assert resp.backend.version == "openocr/tesseract"


# --- sync INLINE path ------------------------------------------------------------------


def test_sync_completes_inside_submit():
    adapter = OpenOCRAdapter(client=FakeOpenOCRClient())
    job = adapter.submit(_req(), RunContext())
    assert job.wait_mode is WaitMode.INLINE and job.state is JobState.SUCCEEDED
    assert job.backend_job_id == "req_fix_1"


def test_text_pages_and_billed_usage():
    resp = _run(OpenOCRAdapter(client=FakeOpenOCRClient()), _req())
    assert resp.document.text and "Jane Doe" in resp.document.text
    assert resp.document.page_count == 2
    assert resp.backend.version == "openocr/tesseract"  # which aggregated engine ran
    assert resp.usage is not None
    assert resp.usage.cost_usd == pytest.approx(0.001)  # the ACTUAL debit, not an estimate
    assert resp.usage.cost_basis == "billed"
    assert resp.usage.duration_ms == 148
    # blocks are channel X and requested by default → warned, never fabricated
    assert resp.document.pages is None
    assert any("blocks" in (w.field or "") for w in (resp.warnings or []))


def test_engine_lang_pages_and_idempotency_plumbed():
    client = FakeOpenOCRClient()
    adapter = OpenOCRAdapter(client=client)
    req = _req(
        features={"ocr_languages": ["eng", "fra"]},
        pages={"max_pages": 3},
    )
    ctx = RunContext(runtime={"engine": "openocr/easyocr"}, idempotency_key="ocr-abc123")
    adapter.submit(req, ctx)
    assert client.last_body is not None
    assert client.last_body["engine"] == "openocr/easyocr"  # OPENOCR_ENGINE override
    assert client.last_body["lang"] == ["eng", "fra"]
    assert client.last_body["page_count"] == 3
    assert client.last_idempotency_key == "ocr-abc123"  # never double-charge on retry


def test_default_engine_is_tesseract():
    client = FakeOpenOCRClient()
    OpenOCRAdapter(client=client).submit(_req(), RunContext())
    assert client.last_body is not None
    assert client.last_body["engine"] == "openocr/tesseract"
    assert "mode" not in client.last_body  # sync is the platform default


# --- async POLL / WEBHOOK paths --------------------------------------------------------


def test_async_mode_polls_to_completion():
    adapter = OpenOCRAdapter(client=_AsyncFake())
    req = _req(**{"async": {"mode": "async"}})
    job = adapter.submit(req, RunContext())
    assert job.wait_mode is WaitMode.POLL and job.state is JobState.RUNNING
    clock = FakeClock()
    job = run_to_completion(
        adapter, job, ctx=RunContext(), deadline_ms=clock.now_ms() + 120_000, clock=clock
    )
    assert job.state is JobState.SUCCEEDED
    assert adapter.normalize(job, RunContext(), req).document.text


def test_webhook_mode_when_callback_url_present():
    adapter = OpenOCRAdapter(client=_AsyncFake())
    job = adapter.submit(
        _req(**{"async": {"mode": "async", "webhook_url": "https://cb/hook"}}), RunContext()
    )
    assert job.wait_mode is WaitMode.WEBHOOK and job.webhook_token == "req_async_1"


def test_no_credentials_is_terminal():
    from openreading.types.errors import TerminalError

    adapter = OpenOCRAdapter()  # no injected client, no creds in ctx
    with pytest.raises(TerminalError) as exc:
        adapter.submit(_req(), RunContext())
    assert exc.value.backend_code == "no_credentials"


# --- live (keyed; skipped without OPENOCR_API_KEY) -------------------------------------


@pytest.mark.live
def test_live_ocr():  # pragma: no cover
    from tests.live_helpers import run_live, sample_pdf_request, skip_unless_creds

    skip_unless_creds("open-ocr")
    resp = run_live("open-ocr", OpenOCRAdapter(), sample_pdf_request("open-ocr"))
    assert resp.document.text
