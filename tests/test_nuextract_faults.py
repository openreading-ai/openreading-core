"""NuExtract adapter — fault-injection for the branches the happy-path POLL fixtures skip:
input variants (URL/path rejected — no URL intake), submit error mapping, the poll lifecycle
(terminal status + temp-project cleanup, status/result fetch failures, unknown-status
reschedule), best-effort cleanup suppression, the PARTIAL validation-error path, disabled
outputs, and token-cost reporting. All offline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openreading.adapters.nuextract import NuExtractAdapter
from openreading.router.clock import FakeClock
from openreading.router.driver import run_to_completion
from openreading.types import JobState
from openreading.types.enums import ResponseState
from openreading.types.errors import RetryableError, TerminalError
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RunContext

FIX = Path(__file__).parent / "fixtures" / "nuextract"

TEMPLATE = {"total": "number"}


def _fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


def _req(**over) -> OpenReadingRequest:
    body = {
        "document": {"bytes_base64": "ZmFrZQ==", "mime_type": "application/pdf"},
        "backend": {"id": "nuextract"},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


def _extract_req(**over) -> OpenReadingRequest:
    return _req(
        backend={"id": "nuextract", "operation": "extract"},
        extraction_schema={"json_schema": TEMPLATE},
        **over,
    )


class _ScriptedClient:
    """create → jobId; get_job_status walks a scripted list (a dict, or an Exception to raise);
    the result endpoints serve fixtures (or raise); delete_project may raise."""

    def __init__(
        self, statuses, *, result=None, result_exc=None, delete_exc=None, cancel_exc=None
    ) -> None:
        self._statuses = list(statuses)
        self._i = 0
        self._result = result
        self._result_exc = result_exc
        self._delete_exc = delete_exc
        self._cancel_exc = cancel_exc
        self.deleted: list[str] = []
        self.cancel_calls: list[str] = []

    def create_project(self, name, template, instructions):
        return {"id": "proj_1"}

    def create_structured_job(self, project_id, input_bytes, *, filename="document.pdf"):
        return {"jobId": "job_extract_1"}

    def create_content_job(self, input_bytes, *, filename="document.pdf"):
        return {"jobId": "job_parse_1"}

    def get_job_status(self, job_id):
        item = self._statuses[min(self._i, len(self._statuses) - 1)]
        self._i += 1
        if isinstance(item, Exception):
            raise item
        return item

    def get_structured_result(self, job_id):
        if self._result_exc:
            raise self._result_exc
        return self._result or _fixture("extract")

    def get_content_result(self, job_id):
        if self._result_exc:
            raise self._result_exc
        return self._result or _fixture("parse")

    def delete_project(self, project_id):
        if self._delete_exc:
            raise self._delete_exc
        self.deleted.append(project_id)

    def cancel_job(self, job_id):
        self.cancel_calls.append(job_id)
        if self._cancel_exc:
            raise self._cancel_exc
        return {}


class _RaisingCreate:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def create_project(self, name, template, instructions):
        raise self._exc

    def create_structured_job(self, project_id, input_bytes, *, filename="document.pdf"):
        raise self._exc

    def create_content_job(self, input_bytes, *, filename="document.pdf"):
        raise self._exc

    def get_job_status(self, job_id):
        return {"status": "completed"}

    def get_structured_result(self, job_id):
        return _fixture("extract")

    def get_content_result(self, job_id):
        return _fixture("parse")

    def delete_project(self, project_id):
        return None


def _completed():
    return _ScriptedClient([{"status": "completed"}])


# ---- input variants ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "document",
    [
        {"url": "https://example.com/loan.pdf", "mime_type": "application/pdf"},
        {"path": "/local.pdf", "mime_type": "application/pdf"},
        {"file_id": "file_1", "mime_type": "application/pdf"},
    ],
)
def test_non_bytes_input_is_unsupported(document):
    adapter = NuExtractAdapter(client=_completed())
    with pytest.raises(TerminalError) as exc:
        adapter.submit(_req(document=document), RunContext())
    assert exc.value.backend_code == "unsupported_input"  # the platform has no URL intake


# ---- submit error mapping ---------------------------------------------------------------------


def test_submit_reraises_taxonomy_errors_unchanged():
    adapter = NuExtractAdapter(
        client=_RaisingCreate(TerminalError("no", backend_code="auth_rejected"))
    )
    with pytest.raises(TerminalError) as exc:
        adapter.submit(_req(), RunContext())
    assert exc.value.backend_code == "auth_rejected"


def test_submit_maps_unexpected_error():
    adapter = NuExtractAdapter(client=_RaisingCreate(ValueError("boom")))
    with pytest.raises(TerminalError) as exc:
        adapter.submit(_extract_req(), RunContext())
    assert exc.value.backend_code == "ValueError"


# ---- poll lifecycle ---------------------------------------------------------------------------


def test_poll_terminal_status_raises_and_cleans_temp_project():
    client = _ScriptedClient([{"status": "failed"}])
    adapter = NuExtractAdapter(client=client)
    job = adapter.submit(_extract_req(), RunContext())
    with pytest.raises(TerminalError) as exc:
        adapter.poll(job, RunContext())
    assert exc.value.backend_code == "failed"
    assert client.deleted == ["proj_1"]  # temp project not leaked on failure


def test_poll_status_error_is_mapped():
    adapter = NuExtractAdapter(client=_ScriptedClient([RuntimeError("upstream 500")]))
    job = adapter.submit(_req(), RunContext())
    with pytest.raises((TerminalError, RetryableError)):
        adapter.poll(job, RunContext())


def test_poll_failed_job_surfaces_the_result_endpoint_detail():
    # Confirmed live: a quota-blocked job goes status="failed" in ~100ms with NO detail in the
    # status body — the WHY ("QuotaExceeded … upgrade your plan", HTTP 403) lives only on the
    # result endpoint. A bare "NuExtract job failed" is undiagnosable; the detail must surface.
    client = _ScriptedClient(
        [{"status": "failed"}],
        result_exc=TerminalError(
            "Quota exceeded, please upgrade your plan", backend_code="auth_rejected"
        ),
    )
    adapter = NuExtractAdapter(client=client)
    job = adapter.submit(_req(), RunContext())
    with pytest.raises(TerminalError) as exc:
        adapter.poll(job, RunContext())
    assert exc.value.backend_code == "failed"
    assert "Quota exceeded" in str(exc.value)  # the actionable reason, not a bare "job failed"


def test_poll_result_fetch_error_is_mapped():
    adapter = NuExtractAdapter(
        client=_ScriptedClient([{"status": "completed"}], result_exc=RuntimeError("boom"))
    )
    job = adapter.submit(_req(), RunContext())
    with pytest.raises((TerminalError, RetryableError)):
        adapter.poll(job, RunContext())


def test_poll_unknown_status_reschedules_then_succeeds():
    client = _ScriptedClient([{"status": "queued"}, {"status": "running"}, {"status": "completed"}])
    adapter = NuExtractAdapter(client=client)
    job = adapter.submit(_req(), RunContext())
    clock = FakeClock()
    job = run_to_completion(
        adapter, job, ctx=RunContext(), deadline_ms=clock.now_ms() + 120_000, clock=clock
    )
    assert job.state is JobState.SUCCEEDED


def test_cancel_calls_the_vendor_and_marks_the_job_cancelled():
    client = _ScriptedClient([{"status": "running"}])
    adapter = NuExtractAdapter(client=client)
    job = adapter.submit(_req(), RunContext())
    job_id = job.backend_job_id
    out = adapter.cancel(job, RunContext())
    assert client.cancel_calls == [job_id]
    assert out.state is JobState.CANCELLED


def test_cancel_on_an_extract_job_also_cleans_the_temp_project():
    client = _ScriptedClient([{"status": "running"}])
    adapter = NuExtractAdapter(client=client)
    job = adapter.submit(_extract_req(), RunContext())
    adapter.cancel(job, RunContext())
    assert client.cancel_calls == [job.backend_job_id]
    assert client.deleted == ["proj_1"]  # temp project not leaked on cancel either


def test_cancel_unexpected_error_is_mapped_and_raised():
    # Not silently swallowed by the adapter itself — mapped and raised; engine.py's own
    # `_cancel_loser_job` is the layer that treats this call as best-effort, not this adapter.
    client = _ScriptedClient([{"status": "running"}], cancel_exc=RuntimeError("upstream 500"))
    adapter = NuExtractAdapter(client=client)
    job = adapter.submit(_req(), RunContext())
    with pytest.raises(TerminalError):
        adapter.cancel(job, RunContext())
    assert client.cancel_calls == [job.backend_job_id]


def test_cancel_a_retryable_client_error_stays_retryable():
    # _map_error passes an already-typed RetryableError through unchanged — a genuinely
    # retryable failure (e.g. a real 5xx via error_for_status) must not be re-wrapped into a
    # TerminalError.
    client = _ScriptedClient(
        [{"status": "running"}],
        cancel_exc=RetryableError("upstream 503", backend_code="http_503"),
    )
    adapter = NuExtractAdapter(client=client)
    job = adapter.submit(_req(), RunContext())
    with pytest.raises(RetryableError):
        adapter.cancel(job, RunContext())


def test_cancel_on_an_already_terminal_job_does_not_call_the_vendor():
    client = _ScriptedClient([{"status": "running"}])
    adapter = NuExtractAdapter(client=client)
    job = adapter.submit(_req(), RunContext())
    job.state = JobState.SUCCEEDED
    adapter.cancel(job, RunContext())
    assert client.cancel_calls == []


def test_cleanup_failure_is_suppressed():
    client = _ScriptedClient([{"status": "completed"}], delete_exc=RuntimeError("409"))
    adapter = NuExtractAdapter(client=client)
    job = adapter.submit(_extract_req(), RunContext())
    job = adapter.poll(job, RunContext())
    assert job.state is JobState.SUCCEEDED  # the result is in hand; cleanup is best-effort


# ---- PARTIAL on backend validation error ------------------------------------------------------


def test_extract_validation_error_is_partial():
    raw = {
        **_fixture("extract"),
        "result": {},
        "error": {"message": "output failed template validation", "errorCode": "invalid_output"},
    }
    adapter = NuExtractAdapter(client=_ScriptedClient([{"status": "completed"}], result=raw))
    job = adapter.submit(_extract_req(), RunContext())
    job = adapter.poll(job, RunContext())
    resp = adapter.normalize(job, RunContext(), _extract_req())
    assert resp.status.state is ResponseState.PARTIAL
    assert resp.status.error is not None
    assert resp.status.error.backend_code == "invalid_output"
    assert resp.typed_fields is None  # empty result → no fields, never fabricated


# ---- disabled outputs -------------------------------------------------------------------------


def test_disabled_outputs_suppress_channels_and_raw():
    adapter = NuExtractAdapter(client=_completed())
    req = _req(
        outputs={"markdown": False, "text": False, "blocks": False, "include_backend_raw": False}
    )
    job = adapter.submit(req, RunContext())
    job = adapter.poll(job, RunContext())
    resp = adapter.normalize(job, RunContext(), req)
    assert resp.document.markdown is None and resp.document.text is None
    assert resp.backend_raw is None
    assert not resp.warnings  # blocks not requested → no warning needed


# ---- cost -------------------------------------------------------------------------------------


def test_report_cost_projects_token_usage():
    adapter = NuExtractAdapter(client=_completed())
    job = adapter.submit(_req(), RunContext())
    pre = adapter.report_cost(job)  # before the result lands there is nothing to meter
    assert pre.native_quantity == 0.0 and pre.cost_usd is None
    job = adapter.poll(job, RunContext())
    cost = adapter.report_cost(job)
    assert cost.native_unit == "token" and cost.native_quantity == 1040.0
    assert cost.cost_usd is None  # pricing not public — usage reported, rate never invented
    assert cost.billing_target == "caller_account"
