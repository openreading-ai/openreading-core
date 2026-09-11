"""Real child processes prove bounded IPC, reuse, cancellation, and process cleanup."""

import os
import time

import pytest

from openreading.artifacts.limits import ArtifactError


@pytest.fixture
def command(tmp_path):
    import sys

    child = tmp_path / "child.py"
    child.write_text("""import os,sys,json,time
fd=int(sys.argv[-1])
for line in sys.stdin:
 job=json.loads(line)
 print("native diagnostic",flush=True)
 if job.get("slow"):time.sleep(20)
 if job.get("bad"):
  os.write(fd,b'x'*9000+b'\\n');continue
 result={"id":job["id"],"ok":True}
 if job.get("wrong"):result["id"]="wrong"
 os.write(fd,(json.dumps(result)+"\\n").encode())
""")
    return [sys.executable, str(child)]


def make_worker(command, **kwargs):
    from openreading.artifacts.supervisor import WarmWorker

    return WarmWorker(command, memory_bytes=128 * 1024**2, idle_seconds=0.15, **kwargs)


def test_worker_reuses_one_process_and_idle_shutdown_reaps_it(command):
    worker = make_worker(command)
    try:
        worker.run({}, check=lambda: None)
        pid = worker.pid
        worker.run({}, check=lambda: None)
        assert worker.pid == pid
        deadline = time.monotonic() + 3
        while worker.pid is not None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert worker.pid is None
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        worker.close()


@pytest.mark.parametrize("job", [{"bad": True}, {"wrong": True}])
def test_invalid_worker_control_data_terminates_the_generation(command, job):
    worker = make_worker(command)
    try:
        with pytest.raises(ArtifactError, match="parse_failed"):
            worker.run(job, check=lambda: None)
        assert worker.pid is None
        worker.run({}, check=lambda: None)
    finally:
        worker.close()


def test_cancelled_import_reaps_child_before_returning(command):
    worker = make_worker(command)
    started = time.monotonic()

    def check():
        if time.monotonic() - started > 0.1:
            raise ArtifactError("cancelled")

    try:
        with pytest.raises(ArtifactError, match="cancelled"):
            worker.run({"slow": True}, check=check)
        assert worker.pid is None
    finally:
        worker.close()


def test_memory_limit_and_monitoring_failure_never_disable_supervision(command):
    for sample, code in [
        (lambda pid: 2**40, "memory_limit"),
        (lambda pid: (_ for _ in ()).throw(OSError()), "worker_monitor_failed"),
    ]:
        worker = make_worker(command, rss=sample)
        try:
            with pytest.raises(ArtifactError, match=code):
                worker.run({"slow": True}, check=lambda: None)
            assert worker.pid is None
        finally:
            worker.close()


def test_success_flag_requires_boolean(command, tmp_path):
    child = tmp_path / "child.py"
    child.write_text(child.read_text().replace('"ok":True', '"ok":1'))
    worker = make_worker(command)
    try:
        with pytest.raises(ArtifactError, match="parse_failed"):
            worker.run({}, check=lambda: None)
    finally:
        worker.close()


def test_progress_and_concurrent_request_do_not_replace_active_worker(command, tmp_path):
    import threading

    child = tmp_path / "child.py"
    child.write_text(
        child.read_text().replace(
            ' if job.get("slow"):',
            """ for stage in ("preflight","conversion","writing"):
  os.write(fd,(json.dumps({"id":job["id"],"stage":stage})+"\\n").encode())
 if job.get("slow"):""",
        )
    )
    worker = make_worker(command)
    seen = []
    active = threading.Event()
    release = threading.Event()
    errors = []

    def progress(stage):
        seen.append(stage)
        active.set()
        release.wait(3)

    def run():
        try:
            worker.run({}, check=lambda: None, progress=progress)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert active.wait(3)
        with pytest.raises(ArtifactError, match="busy"):
            worker.run({}, check=lambda: None)
    finally:
        release.set()
        thread.join(3)
        worker.close()
    assert not errors
    assert seen == ["preflight", "conversion", "writing"]


def test_clean_input_rejection_keeps_the_warm_converter(command, tmp_path):
    from openreading.artifacts.supervisor import WarmWorker

    child = tmp_path / "child.py"
    child.write_text(
        child.read_text().replace(
            ' if job.get("slow"):',
            """ if job.get("code"):
  os.write(fd,(json.dumps({"id":job["id"],"error":job["code"]})+"\\n").encode());continue
 if job.get("slow"):""",
        )
    )
    worker = WarmWorker(command, memory_bytes=128 * 1024**2, idle_seconds=30)
    try:
        worker.run({}, check=lambda: None)
        pid = worker.pid
        with pytest.raises(ArtifactError, match="password_required"):
            worker.run({"code": "password_required"}, check=lambda: None)
        assert worker.pid == pid
        worker.run({}, check=lambda: None)
        assert worker.pid == pid
        with pytest.raises(ArtifactError, match="parse_failed"):
            worker.run({"code": "parse_failed"}, check=lambda: None)
        assert worker.pid is None
    finally:
        worker.close()


def test_worker_runs_from_its_private_directory(command, tmp_path, monkeypatch):
    from openreading.artifacts.supervisor import WarmWorker

    # ONNX Runtime writes a telemetry session file relative to its working directory.
    child = tmp_path / "child.py"
    child.write_text(
        child.read_text().replace(
            " job=json.loads(line)", " job=json.loads(line)\n open('probe','w').close()"
        )
    )
    private, elsewhere = tmp_path / "private", tmp_path / "elsewhere"
    private.mkdir()
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    worker = WarmWorker(command, memory_bytes=128 * 1024**2, idle_seconds=30, cwd=private)
    try:
        worker.run({}, check=lambda: None)
    finally:
        worker.close()
    assert (private / "probe").exists()
    assert not (elsewhere / "probe").exists()
