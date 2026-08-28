"""open-ocr adapter — fault-injection for the branches the happy-path fixtures skip: input
variants (bytes accepted, path/file_id rejected), the schema-extraction guardrail, submit error
mapping, sync-failed responses, the poll lifecycle (terminal status, fetch errors, processing
reschedule), webhook resolution (mismatch/refetch/terminal-noop), disabled outputs, and the
BILLED vs UNKNOWN cost projection. All offline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openreading.adapters.open_ocr import OpenOCRAdapter
from openreading.types import JobState
from openreading.types.cost import CostBasis
from openreading.types.errors import RetryableError, TerminalError, UnsupportedFeatureError
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RunContext

FIX = Path(__file__).parent / "fixtures" / "open-ocr"


def _fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


def _req(**over) -> OpenReadingRequest:
    body = {
        "document": {"url": "https://example.com/loan.png", "mime_type": "image/png"},
        "backend": {"id": "open-ocr"},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


class _ScriptedClient:
    """create returns a scripted response (or raises); get_request walks a scripted list."""

    def __init__(self, create=None, polls=None) -> None:
        self._create = create if create is not None else _fixture("ocr")
        self._polls = list(polls or [])
        self._i = 0
        self.last_body: dict | None = None

    def create_ocr(self, body, idempotency_key):
        self.last_body = body
        if isinstance(self._create, Exception):
            raise self._create
        return self._create

    def get_request(self, request_id):
        item = self._polls[min(self._i, len(self._polls) - 1)]
        self._i += 1
        if isinstance(item, Exception):
            raise item
        return item


def _async_running():
    return {"request_id": "req_async_1", "status": "processing"}


# ---- input variants ---------------------------------------------------------------------------


def test_bytes_input_is_accepted_with_mime_hint():
    client = _ScriptedClient()
    adapter = OpenOCRAdapter(client=client)
    req = _req(document={"bytes_base64": "ZmFrZQ==", "mime_type": "image/png"})
    job = adapter.submit(req, RunContext())
    assert job.state is JobState.SUCCEEDED
    assert client.last_body is not None
    assert client.last_body["input"] == {
        "type": "base64",
        "data_base64": "ZmFrZQ==",
        "mime_type": "image/png",
    }


@pytest.mark.parametrize(
    "document",
    [
        {"path": "/local.png", "mime_type": "image/png"},
        {"file_id": "file_1", "mime_type": "image/png"},
    ],
)
def test_path_and_file_id_are_unsupported(document):
    adapter = OpenOCRAdapter(client=_ScriptedClient())
    with pytest.raises(TerminalError) as exc:
        adapter.submit(_req(document=document), RunContext())
    assert exc.value.backend_code == "unsupported_input"


def test_schema_extraction_is_a_surfaced_unsupported_feature():
    # guardrail 1: a direct-named backend never silently drops the primary structured ask.
    adapter = OpenOCRAdapter(client=_ScriptedClient())
    req = _req(extraction_schema={"json_schema": {"total": "number"}})
    with pytest.raises(UnsupportedFeatureError) as exc:
        adapter.assert_supports(req)
    assert exc.value.feature == "custom_schema_extraction"


# ---- submit error mapping ---------------------------------------------------------------------


def test_submit_reraises_taxonomy_errors_unchanged():
    adapter = OpenOCRAdapter(
        client=_ScriptedClient(create=TerminalError("no", backend_code="insufficient_credits"))
    )
    with pytest.raises(TerminalError) as exc:
        adapter.submit(_req(), RunContext())
    assert exc.value.backend_code == "insufficient_credits"


def test_submit_maps_unexpected_error():
    adapter = OpenOCRAdapter(client=_ScriptedClient(create=ValueError("boom")))
    with pytest.raises(TerminalError) as exc:
        adapter.submit(_req(), RunContext())
    assert exc.value.backend_code == "ValueError"


def test_sync_failed_status_is_terminal_with_backend_message():
    failed = {
        "request_id": "req_f1",
        "status": "failed",
        "error": {"message": "engine returned an error"},
    }
    adapter = OpenOCRAdapter(client=_ScriptedClient(create=failed))
    with pytest.raises(TerminalError) as exc:
        adapter.submit(_req(), RunContext())
    assert exc.value.backend_code == "failed"
    assert "engine returned an error" in str(exc.value)


# ---- poll lifecycle ---------------------------------------------------------------------------


def _async_job(adapter):
    return adapter.submit(_req(**{"async": {"mode": "async"}}), RunContext())


def test_poll_terminal_status_raises():
    adapter = OpenOCRAdapter(
        client=_ScriptedClient(create=_async_running(), polls=[{"status": "failed"}])
    )
    job = _async_job(adapter)
    with pytest.raises(TerminalError) as exc:
        adapter.poll(job, RunContext())
    assert exc.value.backend_code == "failed"


def test_poll_error_is_mapped():
    adapter = OpenOCRAdapter(
        client=_ScriptedClient(create=_async_running(), polls=[RuntimeError("upstream 500")])
    )
    job = _async_job(adapter)
    with pytest.raises((TerminalError, RetryableError)):
        adapter.poll(job, RunContext())


def test_poll_processing_reschedules():
    adapter = OpenOCRAdapter(
        client=_ScriptedClient(create=_async_running(), polls=[{"status": "processing"}])
    )
    job = _async_job(adapter)
    before = job.next_poll_at or 0.0
    job = adapter.poll(job, RunContext())
    assert job.state is JobState.RUNNING and (job.next_poll_at or 0.0) > before


# ---- webhook resolution -----------------------------------------------------------------------


def _webhook_job(adapter):
    return adapter.submit(
        _req(**{"async": {"mode": "async", "webhook_url": "https://cb/hook"}}), RunContext()
    )


def test_webhook_mismatched_request_id_is_ignored():
    adapter = OpenOCRAdapter(client=_ScriptedClient(create=_async_running()))
    job = _webhook_job(adapter)
    out = adapter.resolve_webhook({"request_id": "someone_else"}, job, RunContext())
    assert out.state is JobState.RUNNING  # not our request → untouched


def test_webhook_matching_event_with_data_finishes():
    adapter = OpenOCRAdapter(client=_ScriptedClient(create=_async_running()))
    job = _webhook_job(adapter)
    out = adapter.resolve_webhook(
        {"request_id": "req_async_1", "data": _fixture("ocr")}, job, RunContext()
    )
    assert out.state is JobState.SUCCEEDED
    assert adapter.normalize(out, RunContext(), _req()).document.text


def test_webhook_without_payload_refetches():
    client = _ScriptedClient(create=_async_running(), polls=[_fixture("ocr")])
    adapter = OpenOCRAdapter(client=client)
    job = _webhook_job(adapter)
    out = adapter.resolve_webhook(
        {"request_id": "req_async_1"}, job, RunContext()
    )  # bare notification
    assert out.state is JobState.SUCCEEDED  # result pulled via GET /v1/ocr/{id}


def test_webhook_on_already_terminal_job_is_a_noop():
    adapter = OpenOCRAdapter(client=_ScriptedClient(create=_async_running()))
    job = _webhook_job(adapter)
    adapter.resolve_webhook(
        {"request_id": "req_async_1", "data": _fixture("ocr")}, job, RunContext()
    )
    again = adapter.resolve_webhook({"request_id": "req_async_1", "data": {}}, job, RunContext())
    assert again.state is JobState.SUCCEEDED


def test_webhook_idless_event_against_idless_job_is_ignored():
    # BL-81: BL-70 (sprint 12) added an adapter-level guard, independent of the dispatcher's own
    # (server/app.py), so an id-less event can never match an id-less job. Without it,
    # `eid in (job.webhook_token, job.backend_job_id)` is `None in (None, None)` — True by
    # construction — the moment a vendor create-request 2xx response omits its own id field
    # (leaving both `backend_job_id` and `webhook_token` None). That guard had zero direct
    # regression coverage: the dispatcher's own `jid is not None` check (server/app.py) makes this
    # branch structurally unreachable through any server-level test, so it needs its own
    # adapter-level test to protect it against a future regression reached a different way.
    adapter = OpenOCRAdapter(client=_ScriptedClient(create=_async_running()))
    job = _webhook_job(adapter)
    job.backend_job_id = None  # simulate a create-request 2xx that omitted its own id field
    job.webhook_token = None
    out = adapter.resolve_webhook({}, job, RunContext())  # event carries no request_id key at all
    assert out.state is JobState.RUNNING  # id-less event must not be treated as a match


# ---- disabled outputs -------------------------------------------------------------------------


def test_disabled_outputs_suppress_channels_and_raw():
    adapter = OpenOCRAdapter(client=_ScriptedClient())
    req = _req(outputs={"text": False, "blocks": False, "include_backend_raw": False})
    job = adapter.submit(req, RunContext())
    resp = adapter.normalize(job, RunContext(), req)
    assert resp.document.text is None
    assert resp.backend_raw is None
    assert not resp.warnings  # blocks not requested → no warning needed


# ---- cost -------------------------------------------------------------------------------------


def test_report_cost_is_billed_when_debit_present():
    adapter = OpenOCRAdapter(client=_ScriptedClient())
    job = adapter.submit(_req(), RunContext())
    cost = adapter.report_cost(job)
    assert cost.basis is CostBasis.BILLED  # the platform returns the actual debit
    assert cost.cost_usd == pytest.approx(0.001)
    assert cost.native_unit == "page" and cost.native_quantity == 2.0
    assert cost.billing_target == "caller_account"


def test_report_cost_unknown_before_result():
    adapter = OpenOCRAdapter(client=_ScriptedClient(create=_async_running()))
    job = _async_job(adapter)
    cost = adapter.report_cost(job)  # nothing debited yet → nothing invented
    assert cost.basis is CostBasis.UNKNOWN and cost.cost_usd is None
    assert cost.native_quantity == 1.0
