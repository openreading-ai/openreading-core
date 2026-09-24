"""Trusted launchers choose detached import implementations without model-controlled code."""

import json
import os
import time

import pytest

from openreading.artifacts import jobs
from tests.test_artifact_document import rich_response
from tests.test_artifact_retention import retain
from tests.test_artifact_retention import retention as retention_fixture


@pytest.fixture(name="retention")
def retained_service(tmp_path):
    yield from retention_fixture.__wrapped__(tmp_path)


def test_custom_execution_is_snapshotted_and_uses_fixed_command(retention, monkeypatch):
    import subprocess

    from openreading.artifacts.jobs import ImportExecution, ImportJobs

    calls = []

    class Process:
        pid = 999999999

        def wait(self):
            return 0

    def launch(command, **kwargs):
        calls.append(command)
        return Process()

    monkeypatch.setattr(subprocess, "Popen", launch)
    execution = ImportExecution(
        ("/trusted/worker", "--internal-server-job"), {"destination": "test"}
    )
    initial = ImportJobs(retention, execution=execution).start("source.md")
    root = retention.config.artifact_root / "jobs" / retention.store.grant / initial.job_id
    assert calls == [["/trusted/worker", "--internal-server-job", str(root)]]
    assert json.loads((root / "request.json").read_text())["execution"] == {"destination": "test"}
    assert jobs.main([str(root)]) == 2

    seen = []

    def factory(request):
        seen.append(request["execution"])

        def external(path, **kwargs):
            kwargs["progress"]("waiting")
            return retain(retention, rich_response())

        monkeypatch.setattr(retention, "import_document", external)
        return retention

    assert jobs.main([str(root)], service_factory=factory) == 0
    status = json.loads((root / "status.json").read_text())
    assert status["state"] == "succeeded"
    assert status["receipt"]["schema_version"] == "0.5"
    assert seen == [{"destination": "test"}]


def test_external_execution_recovery_discloses_uncertain_server_outcome(retention, monkeypatch):
    from openreading.artifacts.jobs import ImportExecution, ImportJobs

    class Process:
        pid = 999999999

        def wait(self):
            return 0

    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *args, **kwargs: Process())
    execution = ImportExecution(("/trusted/worker",), {"destination": "test"})
    manager = ImportJobs(retention, execution=execution)
    initial = manager.start("source.md")
    value = manager.get(initial.job_id)
    assert value.state == "failed"
    assert "may continue" in value.error.message
    assert value.error.retryable is False


@pytest.mark.parametrize("recover_first", [False, True])
def test_supervisor_publishes_identity_or_refuses_an_already_recovered_job(
    retention, monkeypatch, recover_first
):
    class Process:
        pid = 999999999

        def wait(self):
            return 0

    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *args, **kwargs: Process())
    manager = jobs.ImportJobs(retention, execution=jobs.ImportExecution(("/trusted/worker",), {}))
    initial = manager.start("source.md")
    root = manager.root / initial.job_id
    (root / "process.json").unlink()
    request = jobs._read(root / "request.json")
    request["started"] = time.time() - 120
    jobs._write(root / "request.json", request)
    if recover_first:
        assert manager.cancel(initial.job_id).state == "failed"

    def factory(request):
        assert not recover_first, "A delayed supervisor must not restart recovered work"
        owner = jobs._read(root / "process.json")
        assert owner["pid"] == os.getpid()
        assert owner["created"] == jobs.psutil.Process().create_time()
        assert manager.get(initial.job_id).state == "queued"

        def external(path, **kwargs):
            return retain(retention, rich_response())

        monkeypatch.setattr(retention, "import_document", external)
        return retention

    assert jobs.main([str(root)], service_factory=factory) == 0
    result = manager.get(initial.job_id)
    assert result.state == ("failed" if recover_first else "succeeded")
    if recover_first:
        assert "may continue" in result.error.message
        assert not list(retention.store.documents.iterdir())


def test_live_supervisor_with_unreadable_identity_keeps_slot_until_cancelled(
    retention, monkeypatch
):
    from openreading.artifacts.limits import ArtifactError

    class Process:
        pid = 999999999

        def wait(self):
            return 0

    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *args, **kwargs: Process())
    manager = jobs.ImportJobs(retention, execution=jobs.ImportExecution(("/trusted/worker",), {}))
    initial = manager.start("source.md")
    root = manager.root / initial.job_id
    request = jobs._read(root / "request.json")
    request["started"] = time.time() - 120
    jobs._write(root / "request.json", request)

    def factory(request):
        (root / "process.json").write_text("broken JSON")
        assert manager.get(initial.job_id).state == "queued"
        assert jobs.main([str(root)], service_factory=factory) == 2
        with pytest.raises(ArtifactError, match="busy"):
            manager.start("source.md")
        assert manager.cancel(initial.job_id).cancel_requested
        monkeypatch.setattr(
            retention, "import_document", lambda *a, **kw: pytest.fail("cancelled before import")
        )
        return retention

    assert jobs.main([str(root)], service_factory=factory) == 0
    assert manager.get(initial.job_id).state == "cancelled"


def test_external_execution_preserves_a_confirmed_destination_failure(retention, monkeypatch):
    from openreading.artifacts.jobs import ImportExecution, ImportJobs
    from openreading.artifacts.limits import ArtifactError

    class Process:
        pid = 999999999

        def wait(self):
            return 0

    class ConfirmedFailure(ArtifactError):
        preserve_message = True

        def envelope(self):
            value = super().envelope()
            value.error.message = "Core server returned HTTP 401."
            return value

    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *args, **kwargs: Process())
    manager = ImportJobs(retention, execution=ImportExecution(("/trusted/worker",), {}))
    initial = manager.start("source.md")
    root = manager.root / initial.job_id

    def factory(request):
        def external(path, **kwargs):
            raise ConfirmedFailure("parse_failed")

        monkeypatch.setattr(retention, "import_document", external)
        return retention

    assert jobs.main([str(root)], service_factory=factory) == 0
    status = json.loads((root / "status.json").read_bytes())
    assert status["error"]["message"] == "Core server returned HTTP 401."


def test_busy_after_submission_never_repeats_the_external_operation(retention, monkeypatch):
    from openreading.artifacts.jobs import ImportExecution, ImportJobs
    from openreading.artifacts.limits import ArtifactError

    class Process:
        pid = 999999999

        def wait(self):
            return 0

    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *args, **kwargs: Process())
    manager = ImportJobs(retention, execution=ImportExecution(("/trusted/worker",), {}))
    initial = manager.start("source.md")
    root = manager.root / initial.job_id
    calls = []

    def factory(request):
        def external(path, **kwargs):
            calls.append(path)
            kwargs["progress"]("uploading")
            raise ArtifactError("busy" if len(calls) == 1 else "parse_failed")

        monkeypatch.setattr(retention, "import_document", external)
        return retention

    assert jobs.main([str(root)], service_factory=factory) == 0
    assert len(calls) == 1
    status = json.loads((root / "status.json").read_bytes())
    assert status["error"]["retryable"] is False


def test_retaining_contention_recovers_the_cached_external_result(retention, monkeypatch):
    """A local retention lock must not discard a completed server response."""
    from openreading.artifacts.jobs import ImportExecution, ImportJobs
    from openreading.artifacts.limits import ArtifactError

    class Process:
        pid = 999999999

        def wait(self):
            return 0

    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *args, **kwargs: Process())
    manager = ImportJobs(retention, execution=ImportExecution(("/trusted/worker",), {}))
    initial = manager.start("source.md")
    root = manager.root / initial.job_id
    calls = []

    def factory(request):
        def external(path, **kwargs):
            calls.append(path)
            kwargs["progress"]("retaining")
            if len(calls) == 1:
                raise ArtifactError("busy")
            return retain(retention, rich_response())

        monkeypatch.setattr(retention, "import_document", external)
        return retention

    assert jobs.main([str(root)], service_factory=factory) == 0
    assert len(calls) == 2
    status = json.loads((root / "status.json").read_bytes())
    assert status["state"] == "succeeded"


def test_local_job_wire_still_validates_against_its_frozen_version():
    from importlib.resources import files

    import jsonschema

    from openreading.artifacts.models import ImportReceipt
    from openreading.types.import_job import ImportJob

    receipt = ImportReceipt(
        artifact_id="or1_" + "a" * 64,
        display_name="source.md",
        document_sha256="b" * 64,
        passage_count=1,
        reused=False,
        warnings=[],
    )
    value = ImportJob(
        job_id="j1_" + "c" * 32,
        state="succeeded",
        stage="complete",
        elapsed_seconds=0.0,
        receipt=receipt,
    )
    schema = json.loads(files("openreading.schemas").joinpath("import-job.v0.3.json").read_text())
    jsonschema.validate(value.wire(), schema)


@pytest.mark.asyncio
async def test_external_catalog_discloses_network_without_changing_local_default(retention):
    from mcp.shared.memory import create_connected_server_and_client_session

    from openreading.artifacts.jobs import ImportExecution
    from openreading.mcp_server.tools import create_server

    execution = ImportExecution(("/trusted/worker",), {"destination": "test"})
    for selected in (None, execution):
        server = create_server(retention, execution=selected)
        async with create_connected_server_and_client_session(server) as client:
            catalog = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert len(catalog) == 9
        for name in ("openreading_import", "openreading_start_import"):
            assert catalog[name].annotations.openWorldHint is (selected is not None)
        assert catalog["openreading_search"].annotations.openWorldHint is False
        if selected:
            assert "server" in catalog["openreading_import"].description
            assert "may continue" in catalog["openreading_cancel_import"].description
            assert catalog["openreading_import"].annotations.idempotentHint is False
