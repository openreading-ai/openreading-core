"""Detached imports survive client closure and retain grant-bound results or cancellation."""

import json
import time

import pytest

from openreading.artifacts.jobs import ImportJobs, JobError
from openreading.artifacts.limits import ArtifactError, ProfileConfig
from openreading.artifacts.service import ArtifactService
from openreading.testing.sample_pdf import build_sample_pdf


@pytest.fixture
def service(tmp_path):
    root = tmp_path / "input"
    root.mkdir()
    (root / "test.pdf").write_bytes(build_sample_pdf())
    value = ArtifactService(ProfileConfig(root, tmp_path / "store"))
    yield value
    value.close()


def terminal(jobs, identifier):
    until = time.monotonic() + 20
    while time.monotonic() < until:
        result = jobs.get(identifier)
        if result.state in {"succeeded", "failed", "cancelled"}:
            return result
        time.sleep(0.05)
    pytest.fail("Background import did not terminate")


def test_job_survives_service_close_and_can_be_read_after_restart(service):
    jobs = ImportJobs(service)
    started = time.monotonic()
    initial = jobs.start("test.pdf")
    assert time.monotonic() - started < 1
    assert initial.state == "queued"
    service.close()
    restarted = ArtifactService(service.config)
    try:
        result = terminal(ImportJobs(restarted), initial.job_id)
        assert result.state == "succeeded", result
        assert result.receipt.page_count == 2
        assert result.stage == "complete"
        assert result.elapsed_seconds >= 0
        assert result.error is None
        assert "test.pdf" not in json.dumps(initial.wire())
        assert restarted.get_document(result.receipt.artifact_id).fragment_count > 0
    finally:
        restarted.close()


def test_queue_can_be_cancelled_without_waiting_for_other_import(service):
    jobs = ImportJobs(service)
    with service.store.import_lock():
        first = jobs.start("test.pdf")
        time.sleep(0.2)
        reply = jobs.cancel(first.job_id)
        assert reply.cancel_requested
        result = terminal(jobs, first.job_id)
        assert result.state == "cancelled"
        assert result.receipt is None
    assert not list(service.store.documents.glob("*/manifest.json"))


def test_cancel_is_idempotent_after_success(service):
    jobs = ImportJobs(service)
    result = terminal(jobs, jobs.start("test.pdf").job_id)
    assert result.state == "succeeded"
    assert jobs.cancel(result.job_id).wire() == result.wire()


def test_jobs_cannot_escape_input_grant_or_expose_private_errors(service, tmp_path):
    jobs = ImportJobs(service)
    with pytest.raises(ArtifactError, match="access_denied"):
        jobs.start("../outside.pdf")
    (service.config.input_root / "invalid.pdf").write_bytes(b"private-data-not-a-PDF")
    result = terminal(jobs, jobs.start("invalid.pdf").job_id)
    assert result.state == "failed"
    assert result.error.code == "unsupported_format"
    assert "private-data" not in json.dumps(result.wire())
    other = tmp_path / "other"
    other.mkdir()
    switched = ArtifactService(ProfileConfig(other, service.config.artifact_root))
    try:
        with pytest.raises(JobError, match="job_not_found"):
            ImportJobs(switched).get(result.job_id)
    finally:
        switched.close()
    for identifier in ("../other", "", "j1_" + "a" * 32):
        with pytest.raises(JobError, match="job_not_found"):
            jobs.get(identifier)


def test_launch_failure_returns_sanitized_error_and_no_stranded_job(service, monkeypatch):
    import openreading.artifacts.jobs as module

    def fail(*args, **kwargs):
        raise OSError("private-launch-path")

    monkeypatch.setattr(module.subprocess, "Popen", fail)
    jobs = ImportJobs(service)
    with pytest.raises(JobError, match="job_start_failed"):
        jobs.start("test.pdf")
    assert not list(jobs.root.iterdir())


def test_job_wire_models_match_vendored_contract(service):
    import jsonschema

    from openreading.schemas import import_job_schema

    jobs = ImportJobs(service)
    initial = jobs.start("test.pdf")
    result = terminal(jobs, initial.job_id)
    schema = import_job_schema()
    jsonschema.validate(JobError("job_not_found").wire(), schema)
    for value in (initial.wire(), result.wire()):
        jsonschema.validate(value, schema)
        for key in ("job_id", "state", "elapsed_seconds"):
            broken = {**value, key: []}
            with pytest.raises(jsonschema.ValidationError):
                jsonschema.validate(broken, schema)


@pytest.mark.asyncio
async def test_mcp_job_contract_survives_transport_close(service):
    from mcp.shared.memory import create_connected_server_and_client_session

    from openreading.mcp_server.tools import create_server

    async with create_connected_server_and_client_session(create_server(service)) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert not tools["openreading_start_import"].annotations.readOnlyHint
        assert not tools["openreading_start_import"].annotations.idempotentHint
        assert tools["openreading_get_import"].annotations.readOnlyHint
        start = await session.call_tool("openreading_start_import", {"path": "test.pdf"})
        assert not start.isError
        job = json.loads(start.content[0].text)
    async with create_connected_server_and_client_session(create_server(service)) as session:
        response = await session.call_tool(
            "openreading_get_import", {"job_id": job["job_id"], "wait_seconds": 20}
        )
        assert not response.isError
        assert json.loads(response.content[0].text)["state"] == "succeeded"
        missing = await session.call_tool("openreading_cancel_import", {"job_id": "j1_" + "f" * 32})
        assert missing.isError
        assert json.loads(missing.content[0].text)["error"]["code"] == "job_not_found"


def test_cancel_during_real_conversion_reaps_parser_before_terminal_state(service):
    import os

    import psutil
    import pymupdf

    path = service.config.input_root / "long.pdf"
    with pymupdf.open() as document:
        for _ in range(1500):
            page = document.new_page()
            page.insert_text((50, 50), "Synthetic cancellation check")
        document.save(path)
    from dataclasses import replace

    expanded = ArtifactService(
        replace(service.config, limits=replace(service.config.limits, pages=2000))
    )
    try:
        jobs = ImportJobs(expanded)
        job = jobs.start("long.pdf")
        owner = json.loads((jobs.root / job.job_id / "process.json").read_text())
        child = None
        until = time.monotonic() + 10
        while time.monotonic() < until:
            children = psutil.Process(owner["pid"]).children()
            if children:
                child = children[0]
                break
            time.sleep(0.01)
        assert child is not None
        jobs.cancel(job.job_id)
        result = terminal(jobs, job.job_id)
        assert result.state == "cancelled"
        assert result.receipt is None
        assert list((service.config.artifact_root / "staging").iterdir()) == []
        with pytest.raises(ProcessLookupError):
            os.kill(child.pid, 0)
    finally:
        expanded.close()


def test_dead_supervisor_does_not_leave_status_running_forever(service):
    import signal

    import psutil

    jobs = ImportJobs(service)
    with service.store.import_lock():
        job = jobs.start("test.pdf")
        owner = json.loads((jobs.root / job.job_id / "process.json").read_text())
        process = psutil.Process(owner["pid"])
        assert process.create_time() == owner["created"]
        process.send_signal(signal.SIGKILL)
        process.wait(timeout=5)
        result = terminal(jobs, job.job_id)
        assert result.state == "failed"
        assert result.error.code == "parse_failed"


@pytest.mark.asyncio
async def test_background_import_survives_actual_stdio_server_exit(service):
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "openreading.mcp_server.main",
            "--profile",
            "local-document-proof-v1",
            "--input-root",
            str(service.config.input_root),
            "--artifact-root",
            str(service.config.artifact_root),
        ],
    )
    async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as client:
        await client.initialize()
        result = await client.call_tool("openreading_start_import", {"path": "test.pdf"})
        assert not result.isError
        identifier = json.loads(result.content[0].text)["job_id"]
    async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as client:
        await client.initialize()
        result = await client.call_tool(
            "openreading_get_import", {"job_id": identifier, "wait_seconds": 20}
        )
        assert not result.isError
        payload = json.loads(result.content[0].text)
        assert payload["state"] == "succeeded"
        assert payload["receipt"]["page_count"] == 2
