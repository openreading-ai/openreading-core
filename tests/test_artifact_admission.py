"""Detached imports bound live supervisors and serialize publication across clients."""

import concurrent.futures
import json
import subprocess
import sys
import threading
import time

import pytest

from openreading.artifacts.jobs import ImportJobs
from openreading.artifacts.limits import ArtifactError
from tests.test_artifact_jobs import service as service_fixture
from tests.test_artifact_jobs import terminal


@pytest.fixture(name="service")
def artifact_service(tmp_path):
    yield from service_fixture.__wrapped__(tmp_path)


def stop_jobs(manager, jobs):
    for job in jobs:
        manager.cancel(job.job_id)
    for job in jobs:
        assert terminal(manager, job.job_id).state == "cancelled"


def orphan_record(manager, *, age, owner):
    from openreading.artifacts.jobs import _write
    from openreading.types.import_job import ImportJob

    identifier = "j1_" + "0" * 32
    root = manager.root / identifier
    root.mkdir(mode=0o700)
    value = ImportJob(job_id=identifier, state="queued", stage="queued", elapsed_seconds=0)
    _write(root / "status.json", value.wire())
    _write(root / "request.json", {"path": "orphan.pdf", "started": time.time() - age})
    if owner is not None:
        (root / "process.json").write_text(owner)
    return identifier


@pytest.mark.parametrize("owner", [None, "broken JSON", "{}"])
@pytest.mark.parametrize("lookup", ["start", "get", "cancel", "list"])
def test_abandoned_identity_publication_recovers_without_blocking_grant(service, owner, lookup):
    manager = ImportJobs(service)
    identifier = orphan_record(manager, age=120, owner=owner)
    started = []
    with service.store.import_lock():
        try:
            if lookup == "start":
                started.append(manager.start("test.pdf"))
            elif lookup == "list":
                assert manager.list().jobs[0].state == "failed"
            else:
                assert getattr(manager, lookup)(identifier).state == "failed"
            result = manager.cancel(identifier)
            assert result.state == "failed"
            assert result.stage == "stopped"
            assert result.error.code == "parse_failed"
            assert not result.error.retryable
            if not started:
                started.append(manager.start("test.pdf"))
        finally:
            stop_jobs(manager, started)


def test_recent_missing_identity_keeps_its_admission_slot(service):
    manager = ImportJobs(service)
    identifier = orphan_record(manager, age=0, owner=None)
    (service.config.input_root / "orphan.pdf").write_bytes(b"synthetic source")
    assert manager.get(identifier).state == "queued"
    with pytest.raises(ArtifactError, match="busy"):
        manager.start("orphan.pdf")
    assert manager.cancel(identifier).cancel_requested


def test_nonterminal_admission_caps_supervisors_and_releases_terminal_slots(service):
    manager = ImportJobs(service)
    started = []
    for index in range(5):
        (service.config.input_root / f"file{index}.pdf").write_bytes(b"queued synthetic source")
    with service.store.import_lock():
        try:
            for index in range(4):
                started.append(manager.start(f"file{index}.pdf"))
            with pytest.raises(ArtifactError, match="busy") as error:
                started.append(manager.start("file4.pdf"))
            assert error.value.envelope().error.retryable
            manager.cancel(started[0].job_id)
            assert terminal(manager, started[0].job_id).state == "cancelled"
            started.append(manager.start("file4.pdf"))
        finally:
            stop_jobs(manager, started)


def test_duplicate_active_path_is_refused_across_manager_instances(service):
    manager = ImportJobs(service)
    started = []
    with service.store.import_lock():
        try:
            started.append(manager.start("test.pdf"))
            with pytest.raises(ArtifactError, match="busy"):
                started.append(ImportJobs(service).start("test.pdf"))
        finally:
            stop_jobs(manager, started)


def test_concurrent_starts_cannot_admit_the_same_path_twice(service):
    manager = ImportJobs(service)
    ready = threading.Barrier(2)
    started = []

    def start():
        ready.wait(timeout=5)
        try:
            return ImportJobs(service).start("test.pdf")
        except ArtifactError as error:
            assert error.code == "busy"
            return None

    with service.store.import_lock(), concurrent.futures.ThreadPoolExecutor(2) as pool:
        try:
            futures = [pool.submit(start) for _ in range(2)]
            started.extend(job for future in futures if (job := future.result(timeout=5)))
            assert len(started) == 1
        finally:
            stop_jobs(manager, started)


def test_status_waits_for_process_identity_publication(service, monkeypatch):
    from openreading.artifacts import jobs as module

    manager = ImportJobs(service)
    publishing = threading.Event()
    release = threading.Event()
    reading = threading.Event()
    completed = threading.Event()
    original = module._write
    identifiers = []
    started = []

    def write(path, value):
        if path.name == "process.json":
            identifiers.append(path.parent.name)
            publishing.set()
            assert release.wait(timeout=5)
        return original(path, value)

    def read():
        reading.set()
        try:
            return ImportJobs(service).get(identifiers[0])
        finally:
            completed.set()

    monkeypatch.setattr(module, "_write", write)
    with service.store.import_lock(), concurrent.futures.ThreadPoolExecutor(2) as pool:
        launching = pool.submit(manager.start, "test.pdf")
        try:
            assert publishing.wait(timeout=5)
            observation = pool.submit(read)
            assert reading.wait(timeout=5)
            # A status reader cannot inspect a half-published supervisor identity.
            assert not completed.wait(timeout=0.1)
            release.set()
            started.append(launching.result(timeout=5))
            assert observation.result(timeout=5).state == "queued"
        finally:
            release.set()
            if not started:
                started.append(launching.result(timeout=5))
            stop_jobs(manager, started)


def test_separate_client_processes_cannot_admit_the_same_path_twice(service, tmp_path):
    script = """
import json, sys, time
from pathlib import Path
from openreading.artifacts.service import ArtifactService
from openreading.artifacts.limits import ArtifactError, ProfileConfig
from openreading.artifacts.jobs import ImportJobs
root, store, barrier, index = map(Path, sys.argv[1:])
service = ArtifactService(ProfileConfig(root, store))
(barrier / str(index)).touch()
until = time.monotonic() + 10
while not (barrier / "go").exists():
    if time.monotonic() >= until:
        raise RuntimeError("Admission test barrier timed out")
    time.sleep(0.01)
try:
    result = ImportJobs(service).start("test.pdf").wire()
except ArtifactError as error:
    result = {"code": error.code}
finally:
    service.close()
print(json.dumps(result))
"""
    manager = ImportJobs(service)
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    processes = []
    accepted = []
    with service.store.import_lock():
        try:
            for index in range(2):
                processes.append(
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-c",
                            script,
                            str(service.config.input_root),
                            str(service.config.artifact_root),
                            str(barrier),
                            str(index),
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                )
            until = time.monotonic() + 10
            while not all((barrier / str(i)).exists() for i in range(2)):
                assert time.monotonic() < until, "Client test processes did not become ready"
                time.sleep(0.01)
            (barrier / "go").touch()
            outcomes = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=10)
                assert process.returncode == 0, stderr
                result = json.loads(stdout)
                outcomes.append(result)
                if "job_id" in result:
                    accepted.append(manager.get(result["job_id"]))
            assert len(accepted) == 1
            assert sum(result == {"code": "busy"} for result in outcomes) == 1
        finally:
            (barrier / "go").touch()
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
            stop_jobs(manager, accepted)
