"""Durable general jobs survive client disconnects and retain exact scoped responses."""

import importlib.util
import json
import os
import time

import pytest

from openreading.artifacts.limits import ProfileConfig
from openreading.artifacts.results import RetainedResults
from openreading.artifacts.store import Store
from openreading.mcp_server.execution import ExecutionConfig, ExecutionRefused
from openreading.testing.sample_pdf import build_sample_pdf


@pytest.fixture
def store(tmp_path):
    root = tmp_path.resolve()
    (root / "input").mkdir()
    (root / "input" / "sample.pdf").write_bytes(build_sample_pdf())
    value = Store(ProfileConfig(root / "input", root / "store"))
    yield value
    value.close()


def jobs(store, **kwargs):
    from openreading.mcp_server.execution_jobs import ExecutionJobs

    config = ExecutionConfig.from_operator(
        config={"version": 1, "strategies": {"local": {"backend": "pymupdf"}}},
        allowed_backends=["pymupdf"],
        allowed_strategies=["local"],
    )
    return ExecutionJobs(store, config, **kwargs)


def request(backend="pymupdf"):
    return {"document": {"path": "sample.pdf"}, "backend": {"id": backend}}


def terminal(manager, job_id):
    end = time.monotonic() + 30
    while time.monotonic() < end:
        value = manager.get(job_id, wait_seconds=0.2)
        if value.state in {"succeeded", "failed", "cancelled"}:
            return value
    pytest.fail("General execution job did not terminate")


def test_durable_execution_jobs_exist():
    assert importlib.util.find_spec("openreading.mcp_server.execution_jobs") is not None


@pytest.mark.parametrize("backend", ["pymupdf", "strategy:local"])
def test_detached_execution_reconnect_and_exact_retention(store, backend):
    manager = jobs(store)
    first = manager.start(request(backend))
    assert first.state == "queued"
    restarted = jobs(store)
    assert first.job_id in [j.job_id for j in restarted.list().jobs]
    last = terminal(restarted, first.job_id)
    assert last.state == "succeeded", last.wire()
    assert last.response_state == "succeeded"
    assert last.receipt is not None
    retained = RetainedResults(store).load(last.receipt.result_id)
    assert "OpenReading Test Document" in retained.payload["document"]["text"]
    assert RetainedResults.receipt(last.receipt.result_id, retained) == last.receipt
    assert last.receipt.kind == "normalized_response"
    assert "sample.pdf" not in json.dumps(last.wire())
    assert restarted.cancel(first.job_id).wire() == last.wire()
    root = manager.root / first.job_id
    assert root.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in root.glob("*.json"))


def test_authority_checked_before_reading_or_creating_job(store, monkeypatch):
    manager = jobs(store)
    monkeypatch.setattr(
        store, "source", lambda *_: pytest.fail("source acquired before authorization")
    )
    with pytest.raises(ExecutionRefused, match="scope_denied"):
        manager.start(request("reducto"))
    assert not list(manager.root.glob("ej1_*"))


def test_queued_cancel_and_deadline_never_publish_or_start_attempt(store):
    from openreading.mcp_server.execution_jobs import slot

    manager = jobs(store)
    with slot(manager.root, 0):
        queued = manager.start(request())
        assert manager.cancel(queued.job_id).cancel_requested
        stopped = terminal(manager, queued.job_id)
        timed = jobs(store, deadline_seconds=0.15)
        expired = terminal(timed, timed.start(request()).job_id)
    assert stopped.state == "cancelled"
    assert stopped.error.code == "cancelled"
    assert stopped.receipt is None
    assert expired.state == "failed"
    assert expired.error.code == "timeout"
    assert not list((store.config.artifact_root / "execution").glob("**/source"))
    assert not list(RetainedResults(store).root.glob("*.json"))


def test_explicit_environment_never_serialized_in_job_files(store, monkeypatch):
    monkeypatch.setenv("MCP_AMBIENT_SECRET", "ambient-never-inherited")
    manager = jobs(store, environment={"ONLY_EXPLICIT": "private-value-123"})
    final = terminal(manager, manager.start(request()).job_id)
    assert final.state == "succeeded"
    for path in manager.root.rglob("*"):
        if path.is_file():
            assert b"private-value-123" not in path.read_bytes()
            assert b"ambient-never-inherited" not in path.read_bytes()


def test_foreign_copied_and_corrupt_status_are_refused_and_listing_continues(store, tmp_path):
    import shutil

    from openreading.mcp_server.execution_jobs import ExecutionJobError

    manager = jobs(store)
    done = terminal(manager, manager.start(request()).job_id)
    other_input = tmp_path.resolve() / "other"
    other_input.mkdir()
    other = Store(ProfileConfig(other_input, store.config.artifact_root))
    try:
        foreign = jobs(other)
        with pytest.raises(ExecutionJobError, match="job_not_found"):
            foreign.get(done.job_id)
        shutil.copytree(manager.root / done.job_id, foreign.root / done.job_id)
        with pytest.raises(ExecutionJobError, match="job_state_invalid"):
            foreign.get(done.job_id)
    finally:
        other.close()
    (manager.root / done.job_id / "status.json").write_text("{}")
    with pytest.raises(ExecutionJobError, match="job_state_invalid"):
        manager.get(done.job_id)
    assert manager.list().jobs[0].state == "unavailable"


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_status_special_files_refused_without_hanging(store, kind):
    from openreading.mcp_server.execution_jobs import ExecutionJobError

    manager = jobs(store)
    done = terminal(manager, manager.start(request()).job_id)
    path = manager.root / done.job_id / "status.json"
    path.unlink()
    if kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.symlink_to(manager.root / done.job_id / "request.json")
    with pytest.raises(ExecutionJobError, match="job_state_invalid"):
        manager.get(done.job_id)


def prepared(manager, suffix="1"):
    """Prepare accepted control records without starting a provider for lifecycle fault injection."""
    from openreading.mcp_server.execution_jobs import _write_bound
    from openreading.types.execution_job import ExecutionJob

    root = manager.root / ("ej1_" + suffix * 32)
    root.mkdir()
    _write_bound(
        root,
        "status.json",
        manager.store.grant,
        ExecutionJob(job_id=root.name, state="queued", stage="queued", elapsed_seconds=0).wire(),
    )
    _write_bound(
        root,
        "request.json",
        manager.store.grant,
        {
            "input_root": str(manager.store.config.input_root),
            "artifact_root": str(manager.store.config.artifact_root),
            "configuration": json.loads(manager.authority.configuration),
            "allowed_backends": sorted(manager.authority.allowed_backends),
            "allowed_strategies": sorted(manager.authority.allowed_strategies),
            "request": request(),
            "started": time.time(),
            "deadline_seconds": None,
            "concurrency": 1,
            "environment_names": [],
        },
    )
    _write_bound(root, "process.json", manager.store.grant, {"pid": os.getpid(), "created": -1.0})
    return root


def content(state="failed"):
    from openreading.artifacts.result_models import ResultContent, ResultProvenance

    return ResultContent(
        kind="normalized_response",
        payload={
            "schema_version": "0.3",
            "status": {"state": state},
            "backend": {"id": "synthetic", "type": "oss_library"},
            "document": {"text": ""},
            "warnings": [{"code": "unavailable", "message": "No text"}],
            "typed_fields": {"backend_raw": {"value": None}},
        },
        provenance=ResultProvenance(
            request_sha256="a" * 64,
            config_sha256="b" * 64,
            core_version="test",
            core_commit=None,
            adapters={"synthetic": None},
            source_sha256=[],
            subjects={},
        ),
    )


@pytest.mark.parametrize("state", ["succeeded", "partial", "failed", "processing"])
def test_supervisor_retains_response_state_without_rewriting_payload(store, monkeypatch, state):
    from openreading.mcp_server import execution_jobs as module

    manager = jobs(store)
    root = prepared(manager)
    expected = content(state)
    monkeypatch.setattr(module.ExecutionAttempt, "run", lambda *_args, **_kwargs: expected)
    assert module.run(root) == 0
    final = manager.get(root.name)
    assert final.state == "succeeded"
    assert final.response_state == state
    assert RetainedResults(store).load(final.receipt.result_id).wire() == expected.wire()


@pytest.mark.parametrize("when", ["before", "after"])
def test_cancel_publication_race_has_one_terminal_outcome(store, monkeypatch, when):
    from openreading.mcp_server import execution_jobs as module

    manager = jobs(store)
    root = prepared(manager)
    original_write = module._write_bound
    original_publish = module.RetainedResults.publish
    monkeypatch.setattr(module.ExecutionAttempt, "run", lambda *_args, **_kwargs: content())

    def write(path, name, grant, payload):
        original_write(path, name, grant, payload)
        if when == "before" and name == "publication.json":
            original_write(root, "cancel.json", grant, {})

    def publish(*args, **kwargs):
        result = original_publish(*args, **kwargs)
        if when == "after":
            original_write(root, "cancel.json", store.grant, {})
        return result

    monkeypatch.setattr(module, "_write_bound", write)
    monkeypatch.setattr(module.RetainedResults, "publish", publish)
    module.run(root)
    final = manager.get(root.name)
    assert final.state == ("cancelled" if when == "before" else "succeeded")
    assert final.cancel_requested
    assert bool(final.receipt) == (when == "after")
    assert len(list(RetainedResults(store).root.glob("*.json"))) == (when == "after")


@pytest.mark.parametrize("when", ["before", "after"])
def test_death_at_publication_recovers_only_committed_result(store, monkeypatch, when):
    from openreading.mcp_server import execution_jobs as module

    class Crash(BaseException):
        pass

    manager = jobs(store)
    root = prepared(manager)
    expected = content()
    monkeypatch.setattr(module.ExecutionAttempt, "run", lambda *_args, **_kwargs: expected)
    original = module.RetainedResults.publish

    def crash(*args, **kwargs):
        if when == "after":
            original(*args, **kwargs)
        raise Crash

    monkeypatch.setattr(module.RetainedResults, "publish", crash)
    with pytest.raises(Crash):
        module.run(root)
    final = manager.get(root.name)
    assert final.state == ("failed" if when == "before" else "succeeded")
    if when == "before":
        assert final.error.code == "interrupted"
        assert final.receipt is None
    else:
        assert final.response_state == "failed"
        assert RetainedResults(store).load(final.receipt.result_id).wire() == expected.wire()
    assert manager.get(root.name).wire() == final.wire()


@pytest.mark.parametrize(
    "error",
    ["scope_denied", "source_changed", "os_permission_denied", "secret exception", "provider_busy"],
)
def test_execution_failures_are_terminal_sanitized_and_never_retried(store, monkeypatch, error):
    from openreading.mcp_server import execution_jobs as module
    from openreading.mcp_server.execution_process import ExecutionError

    manager = jobs(store)
    root = prepared(manager)
    called = []

    def fail(*args, **kwargs):
        called.append(1)
        if error == "scope_denied":
            raise ExecutionRefused(error)
        if error == "secret exception":
            raise RuntimeError("secret exception")
        if error == "provider_busy":
            raise BlockingIOError("private provider busy")
        raise ExecutionError(error)

    monkeypatch.setattr(module.ExecutionAttempt, "run", fail)
    assert module.run(root) == 1
    final = manager.get(root.name)
    assert final.state == "failed"
    assert final.error.code == (
        "execution_failed" if error in {"secret exception", "provider_busy"} else error
    )
    assert "secret exception" not in json.dumps(final.wire())
    assert called == [1]
    assert final.receipt is None


def test_supervisor_reauthorizes_before_worker_run(store, monkeypatch):
    from openreading.mcp_server import execution_jobs as module

    manager = jobs(store)
    root = prepared(manager)
    packet = module._read_bound(root, "request.json", store.grant)
    packet["request"] = request("reducto")
    module._write_bound(root, "request.json", store.grant, packet)
    monkeypatch.setattr(
        module.ExecutionAttempt, "run", lambda *_a, **_kw: pytest.fail("unscoped worker start")
    )
    assert module.run(root) == 1
    assert manager.get(root.name).error.code == "scope_denied"


def test_pagination_cursors_bind_grant_and_limit(store, tmp_path):
    from openreading.artifacts.limits import ArtifactError

    manager = jobs(store)
    roots = [prepared(manager, s) for s in ["1", "2", "3"]]
    first = manager.list(limit=2)
    assert [r.job_id for r in first.jobs] == [r.name for r in roots[:2]]
    assert first.next_cursor
    last = manager.list(limit=2, cursor=first.next_cursor)
    assert [r.job_id for r in last.jobs] == [roots[2].name]
    assert last.next_cursor is None
    with pytest.raises(ArtifactError, match="invalid_cursor"):
        manager.list(limit=1, cursor=first.next_cursor)
    other_input = tmp_path.resolve() / "other"
    other_input.mkdir()
    other = Store(ProfileConfig(other_input, store.config.artifact_root))
    try:
        with pytest.raises(ArtifactError, match="invalid_cursor"):
            jobs(other).list(limit=2, cursor=first.next_cursor)
    finally:
        other.close()


@pytest.mark.parametrize(
    "options",
    [
        {"concurrency": 0},
        {"concurrency": True},
        {"deadline_seconds": 0},
        {"deadline_seconds": float("inf")},
        {"environment": {"BAD=KEY": "x"}},
    ],
)
def test_bad_operator_options_refused_before_job_creation(store, options):
    from openreading.mcp_server.execution_process import ExecutionError

    with pytest.raises(ExecutionError, match="invalid_configuration"):
        jobs(store, **options)
    assert not list(store.config.artifact_root.glob("execution-jobs/**/ej1_*"))


def test_client_process_exit_does_not_cancel_accepted_job(store, tmp_path):
    import subprocess
    import sys

    script = """
import json,sys
from pathlib import Path
from openreading.artifacts.limits import ProfileConfig
from openreading.artifacts.store import Store
from openreading.mcp_server.execution import ExecutionConfig
from openreading.mcp_server.execution_jobs import ExecutionJobs
store=Store(ProfileConfig(Path(sys.argv[1]),Path(sys.argv[2])))
manager=ExecutionJobs(store,ExecutionConfig.from_operator(allowed_backends=['pymupdf']))
print(json.dumps(manager.start({'document':{'path':'sample.pdf'},'backend':{'id':'pymupdf'}}).wire()))
"""
    client = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            script,
            str(store.config.input_root),
            str(store.config.artifact_root),
        ],
        capture_output=True,
        timeout=10,
    )
    assert client.returncode == 0, client.stderr
    accepted = json.loads(client.stdout)
    assert accepted["state"] == "queued"
    assert terminal(jobs(store), accepted["job_id"]).state == "succeeded"


@pytest.mark.parametrize("action", ["cancel", "kill", "terminate"])
def test_running_job_stops_owned_worker_and_recovers_without_retry(store, monkeypatch, action):
    import signal
    import subprocess
    import sys
    from contextlib import suppress

    import psutil

    from openreading.mcp_server import execution_jobs as module

    child_code = """
import pathlib,subprocess,sys,time
from openreading.mcp_server import execution_worker
def block(packet):
    provider=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
    pathlib.Path(sys.argv[2],'provider-pid').write_text(str(provider.pid))
    while True: time.sleep(1)
execution_worker.execute=block
raise SystemExit(execution_worker.main(int(sys.argv[1])))
"""
    supervisor_code = """
import pathlib,subprocess,sys
from openreading.mcp_server import execution_jobs
root=pathlib.Path(sys.argv[1])
original=subprocess.Popen
child_code=CHILD_CODE
def launch(command,**kwargs):
    fd=command[command.index('--parent-fd')+1]
    p=original([sys.executable,'-I','-c',child_code,fd,str(root)],**kwargs)
    (root/'worker-pid').write_text(str(p.pid))
    return p
subprocess.Popen=launch
assert sys.stdin.buffer.read(1)==b'1'
raise SystemExit(execution_jobs.run(root))
""".replace("CHILD_CODE", repr(child_code))
    original = subprocess.Popen

    def launch(command, **kwargs):
        return original([sys.executable, "-I", "-c", supervisor_code, command[-1]], **kwargs)

    monkeypatch.setattr(module.subprocess, "Popen", launch)
    manager = jobs(store)
    accepted = manager.start(request())
    root = manager.root / accepted.job_id
    owner = module._read_bound(root, "process.json", store.grant)["pid"]
    worker_pid = None
    try:
        until = time.monotonic() + 15
        while not (root / "provider-pid").exists():
            assert time.monotonic() < until, "Fault provider did not start"
            time.sleep(0.02)
        worker_pid = int((root / "worker-pid").read_text())
        provider_pid = int((root / "provider-pid").read_text())
        assert manager.get(accepted.job_id).state == "running"
        if action == "cancel":
            manager.cancel(accepted.job_id)
        else:
            os.kill(owner, signal.SIGKILL if action == "kill" else signal.SIGTERM)
        final = terminal(manager, accepted.job_id)
        assert final.state == ("failed" if action == "kill" else "cancelled")
        assert final.error.code == ("interrupted" if action == "kill" else "cancelled")
        assert final.receipt is None
        for pid in [worker_pid, provider_pid]:
            try:
                process = psutil.Process(pid)
                process.wait(timeout=5)
            except psutil.NoSuchProcess:
                pass
            except psutil.TimeoutExpired:
                assert process.status() == psutil.STATUS_ZOMBIE
        assert not list(RetainedResults(store).root.glob("*.json"))
        assert len(list(manager.root.glob("ej1_*"))) == 1
    finally:
        with suppress(ProcessLookupError):
            os.kill(owner, signal.SIGKILL)
        if worker_pid is not None:
            with suppress(ProcessLookupError):
                os.killpg(worker_pid, signal.SIGKILL)


def test_valid_json_byte_edit_cannot_change_retained_status(store):
    from openreading.mcp_server.execution_jobs import ExecutionJobError

    manager = jobs(store)
    root = prepared(manager)
    path = root / "status.json"
    raw = json.loads(path.read_bytes())
    raw["payload"]["elapsed_seconds"] = 123.0
    path.write_text(json.dumps(raw))
    with pytest.raises(ExecutionJobError, match="job_state_invalid"):
        manager.get(root.name)


def test_terminal_supervisor_entry_cannot_repeat_provider_execution(store, monkeypatch):
    from openreading.mcp_server import execution_jobs as module

    manager = jobs(store)
    root = prepared(manager)
    calls = []

    def perform(*args, **kwargs):
        calls.append(1)
        return content()

    monkeypatch.setattr(module.ExecutionAttempt, "run", perform)
    assert module.run(root) == 0
    before = (root / "status.json").read_bytes()
    try:
        repeated = module.run(root)
    except Exception as error:
        repeated = type(error).__name__
    assert repeated == 0
    assert calls == [1]
    assert (root / "status.json").read_bytes() == before


def test_busy_slot_waits_then_runs_without_requeueing_provider_errors(store):
    from openreading.mcp_server.execution_jobs import slot

    manager = jobs(store)
    with slot(manager.root, 0):
        first = manager.start(request())
        # Observe after the supervisor has had time to reach lock acquisition.
        value = manager.get(first.job_id, wait_seconds=1.0)
        assert value.state == "queued", value.wire()
        assert value.receipt is None
    assert terminal(manager, first.job_id).state == "succeeded"


def test_additional_operator_slot_allows_work_while_first_slot_is_busy(store):
    from openreading.mcp_server.execution_jobs import slot

    manager = jobs(store, concurrency=2)
    with slot(manager.root, 0):
        assert terminal(manager, manager.start(request()).job_id).state == "succeeded"


def test_slot_refuses_competing_owner_without_touching_work(store):
    from openreading.mcp_server.execution_jobs import slot

    manager = jobs(store)
    with slot(manager.root, 0):
        with pytest.raises(BlockingIOError), slot(manager.root, 0):
            pytest.fail("Competing owner acquired the same execution slot")
        with slot(manager.root, 1):
            pass


def test_denied_cleanup_restores_signal_handlers_and_closes_grant(store, monkeypatch):
    import signal

    from openreading.mcp_server import execution_jobs as module
    from openreading.mcp_server.execution_process import ExecutionError

    manager = jobs(store)
    root = prepared(manager)
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    closed = []
    original = module.Store.close

    def close(value):
        closed.append(value)
        original(value)

    def denied(*args, **kwargs):
        raise ExecutionError("os_permission_denied")

    monkeypatch.setattr(module.Store, "close", close)
    monkeypatch.setattr(module.ExecutionAttempt, "run", denied)
    monkeypatch.setattr(module.ExecutionAttempt, "close", denied)
    try:
        with pytest.raises(ExecutionError, match="os_permission_denied"):
            module.run(root)
        assert len(closed) == 1
        assert {sig: signal.getsignal(sig) for sig in before} == before
        assert manager.get(root.name).error.code == "os_permission_denied"
    finally:
        for sig, handler in before.items():
            signal.signal(sig, handler)


@pytest.mark.parametrize("field", ["content_bytes", "content_sha256", "kind"])
def test_recovery_refuses_intent_metadata_mismatch_after_valid_result_load(
    store, monkeypatch, field
):
    from openreading.mcp_server import execution_jobs as module

    manager = jobs(store)
    root = prepared(manager)
    expected = content()
    results = RetainedResults(store)
    receipt = results.publish(expected.kind, expected.payload, expected.provenance)
    retained_before = results.path(receipt.result_id).read_bytes()
    claimed = receipt.wire()
    claimed[field] = {
        "content_bytes": receipt.content_bytes + 1,
        "content_sha256": "0" * 64,
        "kind": "comparison_report",
    }[field]
    # Rebind the control checksum so this reaches semantic receipt validation after a valid load.
    module._write_bound(root, "publication.json", store.grant, {"receipt": claimed})
    original_load = module.RetainedResults.load
    loaded = []

    def load(instance, identifier):
        value = original_load(instance, identifier)
        loaded.append(identifier)
        assert value.wire() == expected.wire()
        return value

    monkeypatch.setattr(module.RetainedResults, "load", load)
    final = manager.get(root.name)
    assert loaded == [receipt.result_id]
    assert final.state == "failed"
    assert final.error.code == "interrupted"
    assert final.receipt is None
    assert results.path(receipt.result_id).read_bytes() == retained_before


@pytest.mark.parametrize("lookup", ["get", "list"])
def test_lookup_persists_recovery_once_after_supervisor_exit(store, lookup):
    manager = jobs(store)
    root = prepared(manager)
    path = root / "status.json"
    before = path.read_bytes()
    if lookup == "get":
        assert manager.get(root.name).state == "failed"
    else:
        assert manager.list().jobs[0].state == "failed"
    after = path.read_bytes()
    assert before != after
    assert manager.get(root.name).error.code == "interrupted"
    assert path.read_bytes() == after


@pytest.mark.parametrize(
    "name", ["HOME", "TMPDIR", "XDG_CACHE_HOME", "OPENREADING_CONFIG", "OPENREADING_LEDGER"]
)
def test_job_rejects_reserved_environment_before_creating_records(store, name):
    from openreading.mcp_server.execution_process import ExecutionError

    before = set(store.config.artifact_root.rglob("*"))
    with pytest.raises(ExecutionError, match="^invalid_configuration$"):
        jobs(store, environment={name: "private-operator-value"})
    assert set(store.config.artifact_root.rglob("*")) == before
