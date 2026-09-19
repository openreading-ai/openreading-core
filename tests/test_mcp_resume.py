"""General resume binds a retained strategy attempt to the current grant and authority."""

import hashlib
import json
from pathlib import Path

import pytest

from openreading.artifacts.results import RetainedResults
from openreading.mcp_server.execution import ExecutionConfig, ExecutionRefused
from openreading.mcp_server.execution_jobs import ExecutionJobError, ExecutionJobs, _read_bound
from tests.test_execution_jobs import jobs, request, terminal
from tests.test_execution_jobs import store as store


def previous(store, batch=False):
    manager = jobs(store)
    initial = (
        manager.start_batch({"requests": [request(), request("strategy:local")]})
        if batch
        else manager.start(request("strategy:local"))
    )
    done = terminal(manager, initial.job_id)
    assert done.state == "succeeded", done.wire()
    name = "attempt-1.json" if batch else "attempt.json"
    path = Path(_read_bound(manager.root / done.job_id, name, store.grant)["directory"])
    return manager, done, path


def inventory(path):
    return {
        str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in path.rglob("*")
        if p.is_file()
    }


@pytest.mark.parametrize("batch", [False, True])
def test_resume_uses_retained_bytes_and_preserves_prior_attempt(store, batch):
    manager, done, path = previous(store, batch)
    before = inventory(path)
    (store.config.input_root / "sample.pdf").unlink()
    initial = manager.start_resume({"job_id": done.job_id, **({"item_index": 1} if batch else {})})
    resumed = terminal(jobs(store), initial.job_id)
    assert resumed.state == "succeeded", resumed.wire()
    assert resumed.receipt.kind == "normalized_response"
    result = RetainedResults(store).load(resumed.receipt.result_id)
    old = RetainedResults(store).load(done.receipt.result_id)
    old_payload = old.payload["items"][1]["response"] if batch else old.payload
    assert result.payload["document"] == old_payload["document"]
    assert result.payload["backend"] == old_payload["backend"]
    assert result.provenance.source_sha256 == old.provenance.source_sha256[-1:]
    assert inventory(path) == before
    again = terminal(manager, manager.start_resume({"job_id": resumed.job_id}).job_id)
    assert again.state == "succeeded", again.wire()


def test_resume_scope_checked_before_snapshot_read(store, monkeypatch):
    manager, done, path = previous(store)
    authority = ExecutionConfig.from_operator(
        config=json.loads(manager.authority.configuration),
        allowed_backends=[],
        allowed_strategies=["local"],
    )
    restricted = ExecutionJobs(store, authority)
    # A missing retained source cannot mask the earlier scope refusal.
    (path / "source").unlink()
    with pytest.raises(ExecutionRefused, match="scope_denied"):
        restricted.start_resume({"job_id": done.job_id})
    assert len(list(manager.root.glob("ej1_*"))) == 1


def test_resume_refuses_nonjournaled_named_parse(store):
    manager = jobs(store)
    done = terminal(manager, manager.start(request()).job_id)
    with pytest.raises(ExecutionJobError, match="resume_unavailable"):
        manager.start_resume({"job_id": done.job_id})
    assert len(list(manager.root.glob("ej1_*"))) == 1


def test_resume_rejects_changed_configuration(store):
    manager, done, _ = previous(store)
    authority = ExecutionConfig.from_operator(
        config={"version": 1, "strategies": {"local": {"steps": [{"backend": "pymupdf"}]}}},
        allowed_backends=["pymupdf"],
        allowed_strategies=["local"],
    )
    with pytest.raises(ExecutionJobError, match="resume_unavailable"):
        ExecutionJobs(store, authority).start_resume({"job_id": done.job_id})


@pytest.mark.parametrize("entry", ["source", "ledger"])
def test_resume_refuses_symlink_in_retained_attempt(store, tmp_path, entry):
    manager, done, path = previous(store)
    victim = path / entry
    destination = tmp_path / ("moved-" + entry)
    victim.rename(destination)
    victim.symlink_to(destination)
    with pytest.raises(ExecutionJobError, match="resume_unavailable"):
        manager.start_resume({"job_id": done.job_id})
    assert len(list(manager.root.glob("ej1_*"))) == 1


def test_resume_nonterminal_step_executes_in_new_journal_only(store):
    manager, done, path = previous(store)
    journal = next((path / "ledger").glob("*.jsonl"))
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    attempted = [row for row in rows if row["status"] == "attempted"]
    assert attempted
    journal.write_text("\n".join(json.dumps(row) for row in attempted) + "\n")
    before = inventory(path)
    fresh = terminal(manager, manager.start_resume({"job_id": done.job_id}).job_id)
    assert fresh.state == "succeeded", fresh.wire()
    newpath = Path(
        _read_bound(manager.root / fresh.job_id, "attempt.json", store.grant)["directory"]
    )
    newrows = [
        json.loads(line) for line in (newpath / "ledger" / journal.name).read_text().splitlines()
    ]
    assert len(newrows) > len(attempted)
    assert newrows[-1]["status"] == "ok"
    assert inventory(path) == before


@pytest.mark.parametrize(
    "mode",
    ["source", "header_request", "header_strategy", "header_run_id", "blob", "attempt_directory"],
)
def test_resume_rejects_corrupt_identity_or_payload(store, mode):
    from openreading.mcp_server.execution_jobs import _write_bound

    manager, done, path = previous(store)
    if mode == "source":
        (path / "source").write_bytes(b"changed")
    elif mode.startswith("header_"):
        header = next((path / "ledger").glob("*.header.json"))
        data = json.loads(header.read_text())
        if mode == "header_request":
            data["slim_request"]["document"]["filename"] = "different.pdf"
        elif mode == "header_strategy":
            data["strategy_name"] = "other"
        else:
            data["run_id"] = "a" * 36
        header.write_text(json.dumps(data))
    elif mode == "blob":
        for blob in (path / "ledger" / "blobs").rglob("*.bin"):
            blob.write_bytes(b"changed")
    else:
        _write_bound(
            manager.root / done.job_id,
            "attempt.json",
            store.grant,
            {"directory": str(path.parent.parent / path.name)},
        )
    if mode == "blob":
        final = terminal(manager, manager.start_resume({"job_id": done.job_id}).job_id)
        assert final.state == "failed", final.wire()
        assert final.error.code == "execution_failed"
        assert final.receipt is None
    else:
        with pytest.raises(ExecutionJobError, match="resume_unavailable"):
            manager.start_resume({"job_id": done.job_id})


def test_resume_refuses_foreign_grant_even_when_job_record_is_copied(store, tmp_path):
    import shutil

    from openreading.artifacts.limits import ProfileConfig
    from openreading.artifacts.store import Store

    manager, done, _ = previous(store)
    other = tmp_path / "other-input"
    other.mkdir()
    with_store = Store(ProfileConfig(other, store.config.artifact_root))
    try:
        foreign = jobs(with_store)
        with pytest.raises(ExecutionJobError, match="job_not_found"):
            foreign.start_resume({"job_id": done.job_id})
        shutil.copytree(manager.root / done.job_id, foreign.root / done.job_id)
        with pytest.raises(ExecutionJobError, match="job_state_invalid"):
            foreign.start_resume({"job_id": done.job_id})
    finally:
        with_store.close()


@pytest.mark.parametrize("index", [None, 0, 5])
def test_batch_resume_requires_a_journaled_item(store, index):
    manager, done, _ = previous(store, True)
    with pytest.raises(ExecutionJobError, match="resume_unavailable"):
        manager.start_resume({"job_id": done.job_id, "item_index": index})


def test_resume_refuses_item_index_on_single_job(store):
    manager, done, _ = previous(store)
    with pytest.raises(ExecutionJobError, match="resume_unavailable"):
        manager.start_resume({"job_id": done.job_id, "item_index": 0})


def test_resume_queue_cancellation_never_starts_attempt(store):
    from openreading.mcp_server.execution_jobs import slot

    manager, done, _ = previous(store)
    with slot(manager.root, 0):
        initial = manager.start_resume({"job_id": done.job_id})
        with pytest.raises(ExecutionJobError, match="resume_unavailable"):
            manager.start_resume({"job_id": initial.job_id})
        manager.cancel(initial.job_id)
        final = terminal(manager, initial.job_id)
    assert final.state == "cancelled"
    assert final.receipt is None
    assert not (manager.root / initial.job_id / "attempt.json").exists()


def test_resume_snapshot_changed_after_admission_refuses_without_publication(store):
    from openreading.mcp_server.execution_jobs import slot

    manager, done, path = previous(store)
    with slot(manager.root, 0):
        initial = manager.start_resume({"job_id": done.job_id})
        (path / "source").write_bytes(b"changed after admission")
    final = terminal(manager, initial.job_id)
    assert final.state == "failed"
    assert final.receipt is None


def test_resume_child_rechecks_snapshot_before_shared_api(store, monkeypatch):
    from openreading.mcp_server import execution_worker as worker
    from openreading.mcp_server.resume_input import inspect_attempt

    manager, done, path = previous(store)
    snapshot = inspect_attempt(path, manager.authority, request("strategy:local"))
    monkeypatch.chdir(path)
    monkeypatch.setattr(
        worker, "resume_run", lambda *_, **__: pytest.fail("dispatch before binding")
    )
    (path / "source").write_bytes(b"changed")
    with pytest.raises(ValueError):
        worker.execute(
            {
                "configuration": json.loads(manager.authority.configuration),
                "allowed_backends": ["pymupdf"],
                "allowed_strategies": ["local"],
                "request": snapshot["request"],
                "source_sha256": snapshot["source_sha256"],
                "resume": snapshot,
            }
        )


def test_resume_pinned_backends_refuse_narrowed_but_runnable_scope(store, monkeypatch):
    from openreading.mcp_server import resume_input

    configuration = {
        "version": 1,
        "strategies": {"local": {"steps": [{"backend": "pymupdf"}, {"backend": "tesseract"}]}},
    }
    authority = ExecutionConfig.from_operator(
        config=configuration,
        allowed_backends=["pymupdf", "tesseract"],
        allowed_strategies=["local"],
    )
    manager = ExecutionJobs(store, authority)
    done = terminal(manager, manager.start(request("strategy:local")).job_id)
    assert done.state == "succeeded", done.wire()
    path = Path(_read_bound(manager.root / done.job_id, "attempt.json", store.grant)["directory"])
    header = json.loads(next((path / "ledger").glob("*.header.json")).read_text())
    assert set(header["pinned_eligible"]) == {"pymupdf", "tesseract"}
    restricted = ExecutionJobs(
        store,
        ExecutionConfig.from_operator(
            config=configuration, allowed_backends=["pymupdf"], allowed_strategies=["local"]
        ),
    )
    assert restricted.authority.authorize(request("strategy:local")).backends == ("pymupdf",)
    reads = []
    original_read = resume_input.safe_read

    def observed_read(file, cap):
        reads.append(file)
        return original_read(file, cap)

    monkeypatch.setattr(resume_input, "safe_read", observed_read)
    before = inventory(store.config.artifact_root)
    try:
        with pytest.raises(ExecutionRefused, match="scope_denied"):
            restricted.start_resume({"job_id": done.job_id})
        assert path / "source" not in reads
        assert inventory(store.config.artifact_root) == before
    finally:
        for job in restricted.list().jobs:
            if job.job_id != done.job_id:
                restricted.cancel(job.job_id)
                terminal(restricted, job.job_id)


def test_resume_refuses_running_status_with_valid_attempt_and_live_owner(store):
    import os

    import psutil

    from openreading.mcp_server.execution_jobs import _write_bound

    manager, done, _ = previous(store)
    root = manager.root / done.job_id
    saved = {name: (root / name).read_bytes() for name in ("status.json", "process.json")}
    active = done.model_copy(
        update={"state": "running", "stage": "executing", "receipt": None, "response_state": None}
    )
    _write_bound(root, "status.json", store.grant, active.wire())
    _write_bound(
        root,
        "process.json",
        store.grant,
        {"pid": os.getpid(), "created": psutil.Process().create_time()},
    )
    try:
        assert manager.get(done.job_id).state == "running"
        before = inventory(store.config.artifact_root)
        with pytest.raises(ExecutionJobError, match="resume_unavailable"):
            manager.start_resume({"job_id": done.job_id})
        assert inventory(store.config.artifact_root) == before
    finally:
        for name, data in saved.items():
            (root / name).write_bytes(data)
        for job in manager.list().jobs:
            if job.job_id != done.job_id:
                manager.cancel(job.job_id)
                terminal(manager, job.job_id)


def test_resume_refuses_valid_attempt_under_foreign_grant(store, monkeypatch):
    import shutil

    from openreading.mcp_server import resume_input
    from openreading.mcp_server.execution_jobs import _write_bound
    from openreading.mcp_server.resume_input import inspect_attempt

    manager, done, path = previous(store)
    foreign = path.parent.parent / ("f" * 64) / path.name
    shutil.copytree(path, foreign)
    assert inspect_attempt(foreign, manager.authority, request("strategy:local"))["run_id"]
    root = manager.root / done.job_id
    _write_bound(root, "attempt.json", store.grant, {"directory": str(foreign)})
    reads = []
    original_read = resume_input.safe_read

    def observed_read(file, cap):
        reads.append(file)
        return original_read(file, cap)

    monkeypatch.setattr(resume_input, "safe_read", observed_read)
    before = inventory(store.config.artifact_root)
    try:
        with pytest.raises(ExecutionJobError, match="resume_unavailable"):
            manager.start_resume({"job_id": done.job_id})
        assert reads == []
        assert inventory(store.config.artifact_root) == before
    finally:
        for job in manager.list().jobs:
            if job.job_id != done.job_id:
                manager.cancel(job.job_id)
                terminal(manager, job.job_id)


def test_resume_parent_rejects_snapshot_identity_before_child_launch(store, monkeypatch):
    from openreading.mcp_server import execution_process
    from openreading.mcp_server.resume_input import inspect_attempt

    manager, _, path = previous(store)
    snapshot = inspect_attempt(path, manager.authority, request("strategy:local"))
    snapshot["source_sha256"] = "0" * 64
    monkeypatch.setattr(
        execution_process.subprocess,
        "Popen",
        lambda *_, **__: pytest.fail("launched child before checking snapshot identity"),
    )
    attempt = execution_process.ExecutionAttempt(store, manager.authority)
    try:
        with pytest.raises(ValueError, match="Resume snapshot identity mismatch"):
            attempt.run(snapshot["request"], resume=snapshot)
    finally:
        attempt.close()


def test_resume_child_refuses_distinct_authorized_packet_request(store, monkeypatch):
    from openreading.mcp_server import execution_worker
    from openreading.mcp_server.resume_input import inspect_attempt

    manager, _, path = previous(store)
    snapshot = inspect_attempt(path, manager.authority, request("strategy:local"))
    different = request("strategy:local")
    different["document"]["filename"] = "different.pdf"
    manager.authority.authorize(different)
    monkeypatch.chdir(path)
    with pytest.raises(ValueError, match="Resume request mismatch"):
        execution_worker.execute(
            {
                "configuration": json.loads(manager.authority.configuration),
                "allowed_backends": ["pymupdf"],
                "allowed_strategies": ["local"],
                "request": different,
                "source_sha256": snapshot["source_sha256"],
                "resume": snapshot,
            }
        )
