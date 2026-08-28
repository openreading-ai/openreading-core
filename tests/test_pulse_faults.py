"""Pulse adapter — fault-injection for the error/edge branches the happy-path fixtures skip
(adapter was 75%): bytes vs path input, submit error mapping, the async POLL lifecycle
(in-progress → success, terminal status, get_job failure), and normalize tolerating degenerate
bounding boxes / non-dict extraction data. All offline via an injected fake client."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openreading.adapters.pulse import PulseAdapter
from openreading.router.clock import FakeClock
from openreading.router.driver import run_to_completion
from openreading.types import JobState
from openreading.types.enums import WaitMode
from openreading.types.errors import RetryableError, TerminalError
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RawResult, RunContext

FIX = Path(__file__).parent / "fixtures" / "pulse"


def _fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


def _req(**over) -> OpenReadingRequest:
    body = {
        "document": {"url": "https://example.com/loan.pdf", "mime_type": "application/pdf"},
        "backend": {"id": "pulse"},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


class FakePulseClient:
    """Returns the documented success fixture regardless of args (sync inline path)."""

    def extract(self, document, config):
        return _fixture("extract")

    def get_result(self, url):
        return _fixture("extract")

    def get_job(self, extraction_id):
        return _fixture("extract")


class _RaisingExtractClient:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def extract(self, document, config):
        raise self._exc

    def get_result(self, url):
        return _fixture("extract")

    def get_job(self, extraction_id):
        return _fixture("extract")


class _AsyncClient:
    """async submit → extraction stub; get_job returns scripted statuses (a dict, or an Exception
    to raise) in sequence, holding on the last one."""

    def __init__(self, jobs: list, *, cancel_effect: Exception | None = None) -> None:
        self._jobs = list(jobs)
        self._i = 0
        self._cancel_effect = cancel_effect
        self.cancel_calls: list[str] = []

    def extract(self, document, config):
        return {"extraction_id": "x1"}

    def get_result(self, url):
        return _fixture("extract")

    def get_job(self, extraction_id):
        item = self._jobs[min(self._i, len(self._jobs) - 1)]
        self._i += 1
        if isinstance(item, Exception):
            raise item
        return item

    def cancel_job(self, extraction_id):
        self.cancel_calls.append(extraction_id)
        if self._cancel_effect is not None:
            raise self._cancel_effect
        return {"job_id": extraction_id, "message": "Job cancelled successfully"}


# ---- input handling ---------------------------------------------------------------------------


def test_bytes_input_is_accepted_inline():
    adapter = PulseAdapter(client=FakePulseClient())
    req = _req(document={"bytes_base64": "ZmFrZQ==", "mime_type": "application/pdf"})
    job = adapter.submit(req, RunContext())
    assert job.wait_mode is WaitMode.INLINE and job.state is JobState.SUCCEEDED


def test_path_only_input_is_unsupported():
    adapter = PulseAdapter(client=FakePulseClient())
    req = _req(document={"path": "/local.pdf", "mime_type": "application/pdf"})
    with pytest.raises(TerminalError) as exc:
        adapter.submit(req, RunContext())
    assert exc.value.backend_code == "unsupported_input"


# ---- submit error mapping ---------------------------------------------------------------------


def test_submit_reraises_taxonomy_errors_unchanged():
    boom = TerminalError("nope", backend_code="auth_rejected")
    adapter = PulseAdapter(client=_RaisingExtractClient(boom))
    with pytest.raises(TerminalError) as exc:
        adapter.submit(_req(), RunContext())
    assert exc.value.backend_code == "auth_rejected"  # not remapped


def test_submit_maps_unexpected_error_to_terminal():
    adapter = PulseAdapter(client=_RaisingExtractClient(ValueError("boom")))
    with pytest.raises(TerminalError) as exc:
        adapter.submit(_req(), RunContext())
    assert exc.value.backend_code == "ValueError"  # _map_error uses the exception type name


# ---- async POLL lifecycle ---------------------------------------------------------------------


def _async_req() -> OpenReadingRequest:
    return _req(**{"async": {"mode": "async"}})


def test_async_submit_returns_poll_job_that_completes():
    adapter = PulseAdapter(client=_AsyncClient([{"status": "processing"}, _fixture("extract")]))
    job = adapter.submit(_async_req(), RunContext())
    assert job.wait_mode is WaitMode.POLL and job.backend_job_id == "x1"
    clock = FakeClock()
    job = run_to_completion(
        adapter, job, ctx=RunContext(), deadline_ms=clock.now_ms() + 120_000, clock=clock
    )
    assert job.state is JobState.SUCCEEDED  # in-progress poll rescheduled, then succeeded


def test_poll_terminal_status_raises():
    adapter = PulseAdapter(client=_AsyncClient([{"status": "failed"}]))
    job = adapter.submit(_async_req(), RunContext())
    with pytest.raises(TerminalError) as exc:
        adapter.poll(job, RunContext())
    assert exc.value.backend_code == "failed"


def test_poll_get_job_error_is_mapped():
    adapter = PulseAdapter(client=_AsyncClient([RuntimeError("upstream 500")]))
    job = adapter.submit(_async_req(), RunContext())
    with pytest.raises((TerminalError, RetryableError)):
        adapter.poll(job, RunContext())


# ---- BL-164: cancel calls the vendor -----------------------------------------------------------


def test_cancel_calls_the_vendor_and_marks_the_job_cancelled():
    client = _AsyncClient([{"status": "processing"}])
    adapter = PulseAdapter(client=client)
    job = adapter.submit(_async_req(), RunContext())
    out = adapter.cancel(job, RunContext())
    assert client.cancel_calls == ["x1"]
    assert out.state is JobState.CANCELLED


def test_cancel_unexpected_error_is_mapped_and_raised():
    # Not silently swallowed by the adapter itself — mapped and raised; engine.py's own
    # `_cancel_loser_job` is the layer that treats this call as best-effort, not this adapter.
    client = _AsyncClient([{"status": "processing"}], cancel_effect=RuntimeError("upstream 500"))
    adapter = PulseAdapter(client=client)
    job = adapter.submit(_async_req(), RunContext())
    with pytest.raises(TerminalError):
        adapter.cancel(job, RunContext())
    assert client.cancel_calls == ["x1"]


def test_cancel_a_retryable_client_error_stays_retryable():
    # _map_error passes an already-typed RetryableError through unchanged — a genuinely
    # retryable failure (e.g. a real 5xx via error_for_status) must not be re-wrapped into a
    # TerminalError.
    client = _AsyncClient(
        [{"status": "processing"}],
        cancel_effect=RetryableError("upstream 503", backend_code="http_503"),
    )
    adapter = PulseAdapter(client=client)
    job = adapter.submit(_async_req(), RunContext())
    with pytest.raises(RetryableError):
        adapter.cancel(job, RunContext())


def test_cancel_on_an_already_terminal_job_does_not_call_the_vendor():
    client = _AsyncClient([{"status": "processing"}])
    adapter = PulseAdapter(client=client)
    job = adapter.submit(_async_req(), RunContext())
    job.state = JobState.SUCCEEDED
    adapter.cancel(job, RunContext())
    assert client.cancel_calls == []


# ---- normalize tolerates degenerate geometry / non-dict extraction ----------------------------


def test_normalize_tolerates_degenerate_bboxes_and_nondict_schema():
    raw = {
        "bounding_boxes": {
            "Title": [
                {"id": "t1", "content": "No bbox at all", "page": 1},  # bbox missing → None
                {  # bbox present but no page dimensions → None
                    "id": "t2",
                    "content": "Zero page dims",
                    "page": 1,
                    "bbox": {"left": 1, "top": 1, "width": 5, "height": 5},
                },
            ],
            "markdown_with_ids": "not-a-list",  # non-class key is skipped
        },
        "schema_data": ["not", "a", "dict"],  # non-dict extraction → {} typed_fields
        "markdown": "# Doc",
    }
    adapter = PulseAdapter(client=FakePulseClient())
    job = adapter.new_job(WaitMode.INLINE, state=JobState.SUCCEEDED)
    job.raw = RawResult(
        payload=raw, media_type="application/json", encoding="json", object_class="extract"
    )
    resp = adapter.normalize(job, RunContext(), _req())
    blocks = resp.document.pages[0].blocks
    assert len(blocks) == 2 and all(b.bbox is None for b in blocks)
    assert not resp.typed_fields  # non-dict schema_data ignored, not crashed
