"""Chunkr adapter — fault-injection for the branches the happy-path POLL fixtures skip (adapter
was 80%): input variants (file_id reuse, bytes, unsupported), submit error mapping, immediate
Succeeded-on-creation, the poll lifecycle (terminal status, get_task failure, in-progress
reschedule), and webhook resolution (token match/mismatch, already-terminal). All offline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openreading.adapters.chunkr import ChunkrAdapter
from openreading.router.clock import FakeClock
from openreading.router.driver import run_to_completion
from openreading.types import JobState
from openreading.types.enums import WaitMode
from openreading.types.errors import RetryableError, TerminalError
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RunContext

FIX = Path(__file__).parent / "fixtures" / "chunkr"


def _fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


def _req(**over) -> OpenReadingRequest:
    body = {
        "document": {"url": "https://example.com/loan.pdf", "mime_type": "application/pdf"},
        "backend": {"id": "chunkr"},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


class FakeChunkrClient:
    def create_parse_task(self, document, config):
        return {"task_id": "task_parse_1", "status": "Processing"}

    def create_extract_task(self, document, schema, config):
        return {"task_id": "task_extract_1", "status": "Processing"}

    def get_task(self, task_id):
        return _fixture("extract") if "extract" in task_id else _fixture("parse")

    def cancel_task(self, task_id):
        return {"task_id": task_id, "status": "Cancelled"}


class _ScriptedCancel(FakeChunkrClient):
    def __init__(self, effect: Exception | None) -> None:
        self._effect = effect
        self.cancel_calls: list[str] = []

    def cancel_task(self, task_id):
        self.cancel_calls.append(task_id)
        if self._effect is not None:
            raise self._effect
        return {"task_id": task_id, "status": "Cancelled"}


class _RaisingCreate:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def create_parse_task(self, document, config):
        raise self._exc

    def create_extract_task(self, document, schema, config):
        raise self._exc

    def get_task(self, task_id):
        return _fixture("parse")


class _ImmediateSuccess:
    """Small doc: the create call already returns a terminal Succeeded task with output."""

    def create_parse_task(self, document, config):
        return _fixture("parse")  # status Succeeded + output

    def create_extract_task(self, document, schema, config):
        return _fixture("parse")

    def get_task(self, task_id):
        return _fixture("parse")


class _ScriptedPoll:
    """create → Processing; get_task walks a scripted list (a dict, or an Exception to raise)."""

    def __init__(self, jobs: list) -> None:
        self._jobs = list(jobs)
        self._i = 0

    def create_parse_task(self, document, config):
        return {"task_id": "task_parse_1", "status": "Processing"}

    def create_extract_task(self, document, schema, config):
        return {"task_id": "task_extract_1", "status": "Processing"}

    def get_task(self, task_id):
        item = self._jobs[min(self._i, len(self._jobs) - 1)]
        self._i += 1
        if isinstance(item, Exception):
            raise item
        return item


# ---- input variants ---------------------------------------------------------------------------


def test_file_id_input_reuses_prior_task():
    adapter = ChunkrAdapter(client=FakeChunkrClient())
    req = _req(document={"file_id": "task_prev", "mime_type": "application/pdf"})
    job = adapter.submit(req, RunContext())
    assert job.wait_mode is WaitMode.POLL and job.state is JobState.RUNNING


def test_bytes_input_is_accepted():
    adapter = ChunkrAdapter(client=FakeChunkrClient())
    req = _req(document={"bytes_base64": "ZmFrZQ==", "mime_type": "application/pdf"})
    job = adapter.submit(req, RunContext())
    assert job.state is JobState.RUNNING


def test_path_only_input_is_unsupported():
    adapter = ChunkrAdapter(client=FakeChunkrClient())
    req = _req(document={"path": "/local.pdf", "mime_type": "application/pdf"})
    with pytest.raises(TerminalError) as exc:
        adapter.submit(req, RunContext())
    assert exc.value.backend_code == "unsupported_input"


# ---- submit error mapping ---------------------------------------------------------------------


def test_submit_reraises_taxonomy_errors_unchanged():
    adapter = ChunkrAdapter(
        client=_RaisingCreate(TerminalError("no", backend_code="auth_rejected"))
    )
    with pytest.raises(TerminalError) as exc:
        adapter.submit(_req(), RunContext())
    assert exc.value.backend_code == "auth_rejected"


def test_submit_maps_unexpected_error():
    adapter = ChunkrAdapter(client=_RaisingCreate(ValueError("boom")))
    with pytest.raises(TerminalError) as exc:
        adapter.submit(_req(), RunContext())
    assert exc.value.backend_code == "ValueError"


def test_task_already_succeeded_on_creation_finishes_immediately():
    adapter = ChunkrAdapter(client=_ImmediateSuccess())
    job = adapter.submit(_req(), RunContext())
    assert job.state is JobState.SUCCEEDED  # folded in without a poll round-trip
    resp = adapter.normalize(job, RunContext(), _req())
    assert resp.document.pages or resp.document.markdown


# ---- BL-164: cancel calls the vendor -----------------------------------------------------------


def test_cancel_calls_the_vendor_and_marks_the_job_cancelled():
    client = _ScriptedCancel(None)
    adapter = ChunkrAdapter(client=client)
    job = adapter.submit(_req(), RunContext())
    task_id = job.backend_job_id
    out = adapter.cancel(job, RunContext())
    assert client.cancel_calls == [task_id]
    assert out.state is JobState.CANCELLED


def test_cancel_already_processing_400_is_swallowed_not_raised():
    # Chunkr's own precondition is status == "Starting" (still queued); once a task has moved to
    # "Processing" the vendor answers 400, which is an EXPECTED outcome for most real races (the
    # loser has usually started processing by the time a winner is picked) — the adapter must not
    # surface it as a failure.
    client = _ScriptedCancel(TerminalError("cannot be cancelled", backend_code="http_400"))
    adapter = ChunkrAdapter(client=client)
    job = adapter.submit(_req(), RunContext())
    out = adapter.cancel(job, RunContext())  # must not raise
    assert client.cancel_calls == [job.backend_job_id]
    assert out.state is JobState.CANCELLED


def test_cancel_unexpected_error_is_mapped_and_raised():
    # A genuine failure (auth, 5xx, unknown task) is NOT silently swallowed by the adapter itself —
    # it's mapped and raised; engine.py's own `_cancel_loser_job` is the layer that treats this
    # call as best-effort, not this adapter.
    client = _ScriptedCancel(RuntimeError("upstream 500"))
    adapter = ChunkrAdapter(client=client)
    job = adapter.submit(_req(), RunContext())
    with pytest.raises(TerminalError):
        adapter.cancel(job, RunContext())
    assert client.cancel_calls == [job.backend_job_id]


def test_cancel_a_retryable_client_error_stays_retryable():
    # _map_error passes an already-typed RetryableError through unchanged (e.g. a real 5xx via
    # error_for_status) — a genuinely retryable failure must not be re-wrapped into a
    # TerminalError, which would make engine.py's own suppress(...) treat it identically to a
    # terminal one anyway, but the DISTINCTION matters for any future caller of cancel() that
    # inspects the error type.
    client = _ScriptedCancel(RetryableError("upstream 503", backend_code="http_503"))
    adapter = ChunkrAdapter(client=client)
    job = adapter.submit(_req(), RunContext())
    with pytest.raises(RetryableError):
        adapter.cancel(job, RunContext())


def test_cancel_on_an_already_terminal_job_does_not_call_the_vendor():
    client = _ScriptedCancel(None)
    adapter = ChunkrAdapter(client=client)
    job = adapter.submit(_req(), RunContext())
    job.state = JobState.SUCCEEDED
    adapter.cancel(job, RunContext())
    assert client.cancel_calls == []  # nothing to cancel, no vendor call made


# ---- poll lifecycle ---------------------------------------------------------------------------


def test_poll_terminal_status_raises():
    adapter = ChunkrAdapter(client=_ScriptedPoll([{"status": "Failed"}]))
    job = adapter.submit(_req(), RunContext())
    with pytest.raises(TerminalError) as exc:
        adapter.poll(job, RunContext())
    assert exc.value.backend_code == "failed"


def test_poll_get_task_error_is_mapped():
    adapter = ChunkrAdapter(client=_ScriptedPoll([RuntimeError("upstream 500")]))
    job = adapter.submit(_req(), RunContext())
    with pytest.raises((TerminalError, RetryableError)):
        adapter.poll(job, RunContext())


def test_poll_in_progress_reschedules_then_succeeds():
    adapter = ChunkrAdapter(client=_ScriptedPoll([{"status": "Processing"}, _fixture("parse")]))
    job = adapter.submit(_req(), RunContext())
    clock = FakeClock()
    job = run_to_completion(
        adapter, job, ctx=RunContext(), deadline_ms=clock.now_ms() + 120_000, clock=clock
    )
    assert job.state is JobState.SUCCEEDED


# ---- webhook resolution -----------------------------------------------------------------------


def _webhook_req() -> OpenReadingRequest:
    return _req(**{"async": {"mode": "async", "webhook_url": "https://cb/hook"}})


def test_webhook_mismatched_task_id_is_ignored():
    adapter = ChunkrAdapter(client=FakeChunkrClient())
    job = adapter.submit(_webhook_req(), RunContext())
    out = adapter.resolve_webhook({"task_id": "someone_else"}, job, RunContext())
    assert out.state is JobState.RUNNING  # not our task → untouched


def test_webhook_matching_event_with_output_finishes():
    adapter = ChunkrAdapter(client=FakeChunkrClient())
    job = adapter.submit(_webhook_req(), RunContext())
    out = adapter.resolve_webhook(
        {"task_id": "task_parse_1", "data": _fixture("parse")}, job, RunContext()
    )
    assert out.state is JobState.SUCCEEDED
    assert adapter.normalize(out, RunContext(), _req()).document.pages


def test_webhook_on_already_terminal_job_is_a_noop():
    adapter = ChunkrAdapter(client=FakeChunkrClient())
    job = adapter.submit(_webhook_req(), RunContext())
    adapter.resolve_webhook(
        {"task_id": "task_parse_1", "data": _fixture("parse")}, job, RunContext()
    )
    assert job.state is JobState.SUCCEEDED
    # a second event on the finished job returns immediately without re-finishing
    again = adapter.resolve_webhook({"task_id": "task_parse_1", "data": {}}, job, RunContext())
    assert again.state is JobState.SUCCEEDED


def test_webhook_idless_event_against_idless_job_is_ignored():
    # BL-81: BL-70 added an adapter-level guard, independent of the dispatcher's own
    # (server/app.py), so an id-less event can never match an id-less job. Without it,
    # `eid in (job.webhook_token, job.backend_job_id)` is `None in (None, None)` — True by
    # construction — the moment a vendor create-task 2xx response omits its own id field (leaving
    # both `backend_job_id` and `webhook_token` None). That guard had zero direct regression
    # coverage: the dispatcher's own `jid is not None` check (server/app.py) makes this branch
    # structurally unreachable through any server-level test, so it needs its own adapter-level
    # test to protect it against a future regression reached a different way.
    adapter = ChunkrAdapter(client=FakeChunkrClient())
    job = adapter.submit(_webhook_req(), RunContext())
    job.backend_job_id = None  # simulate a create-task 2xx that omitted its own id field
    job.webhook_token = None
    out = adapter.resolve_webhook({}, job, RunContext())  # event carries no task_id key at all
    assert out.state is JobState.RUNNING  # id-less event must not be treated as a match
