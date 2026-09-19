"""Owned general execution preserves scope, input identity and complete normalized results."""

import hashlib
import importlib.util
import os

import pytest

from openreading.artifacts.limits import ArtifactError, ProfileConfig
from openreading.artifacts.models import json_bytes
from openreading.artifacts.store import Store
from openreading.mcp_server.execution import ExecutionConfig, ExecutionRefused
from openreading.testing.sample_pdf import build_sample_pdf


def test_owned_execution_is_implemented():
    assert importlib.util.find_spec("openreading.mcp_server.execution_process") is not None


@pytest.fixture
def store(tmp_path):
    root = tmp_path.resolve()
    (root / "input").mkdir()
    (root / "input" / "sample.pdf").write_bytes(build_sample_pdf())
    value = Store(ProfileConfig(root / "input", root / "store"))
    yield value
    value.close()


def authority():
    return ExecutionConfig.from_operator(
        config={
            "version": 1,
            "policy": {"backends": ["pymupdf"]},
            "strategies": {"local": {"backend": "pymupdf"}},
        },
        allowed_backends=["pymupdf"],
        allowed_strategies=["local"],
    )


def request(backend="pymupdf", path="sample.pdf"):
    return {"document": {"path": path}, "backend": {"id": backend}}


def attempt(store, **kwargs):
    from openreading.mcp_server.execution_process import ExecutionAttempt

    return ExecutionAttempt(store, authority(), **kwargs)


@pytest.mark.parametrize("backend", ["pymupdf", None, "strategy:local", "strategy:none"])
def test_real_execution_preserves_source_and_supports_each_dispatch_arm(store, backend):
    worker = attempt(store)
    value = request(backend)
    original = json_bytes(value)
    content = worker.run(value)
    assert content.kind == "normalized_response"
    assert content.payload["backend"]["id"] == "pymupdf"
    assert "OpenReading Test Document" in content.payload["document"]["text"]
    assert content.provenance.source_sha256 == [
        hashlib.sha256((store.config.input_root / "sample.pdf").read_bytes()).hexdigest()
    ]
    assert (
        content.provenance.request_sha256
        == hashlib.sha256(authority().authorize(value).request_json).hexdigest()
    )
    assert content.provenance.config_sha256 == authority().fingerprint
    assert content.provenance.core_commit is None
    assert content.provenance.subjects == {}
    assert json_bytes(value) == original
    assert worker.pid is None
    assert worker.root.stat().st_mode & 0o777 == 0o700
    assert (worker.root / "source").stat().st_mode & 0o777 == 0o600
    assert (worker.root / "response.json").stat().st_mode & 0o777 == 0o600
    assert "backend_raw" not in content.payload
    if backend == "strategy:local":
        assert list((worker.root / "ledger").glob("*.header.json"))
    assert not list((store.config.artifact_root / "staging").iterdir())


def test_authorization_precedes_source_access_and_attempt_creation(store, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unauthorized execution reached source acquisition")

    monkeypatch.setattr(store, "source", forbidden)
    worker = attempt(store)
    with pytest.raises(ExecutionRefused, match="scope_denied"):
        worker.run(request("reducto"))
    assert not worker.root.exists()


@pytest.mark.parametrize("kind", ["missing", "symlink", "directory", "fifo"])
def test_granted_input_refusals_never_start_worker(store, kind):
    target = store.config.input_root / "bad.pdf"
    if kind == "symlink":
        target.symlink_to(store.config.input_root / "sample.pdf")
    elif kind == "directory":
        target.mkdir()
    elif kind == "fifo":
        os.mkfifo(target)
    worker = attempt(store)
    with pytest.raises(ArtifactError) as failure:
        worker.run(request(path=target.name))
    assert failure.value.code == ("input_not_found" if kind == "missing" else "access_denied")
    assert worker.pid is None
    assert not (worker.root / "response.json").exists()


def test_child_ignores_ambient_config_ledger_pythonpath_and_credentials(store, monkeypatch):
    outside = store.config.input_root / "outside-ledger"
    monkeypatch.setenv("OPENREADING_CONFIG", "/missing/secret.yaml")
    monkeypatch.setenv("OPENREADING_LEDGER", str(outside))
    monkeypatch.setenv("PYTHONPATH", "/missing/secret-python")
    monkeypatch.setenv("SECRET_SHOULD_NOT_PASS", "secret-ambient-value")
    worker = attempt(store)
    content = worker.run(request("strategy:local"))
    assert content.payload["status"]["state"] == "succeeded"
    assert not outside.exists()
    for path in worker.root.rglob("*"):
        if path.is_file():
            assert b"secret-ambient-value" not in path.read_bytes()


def test_cancellation_and_timeout_leave_no_result(store):
    from openreading.mcp_server.execution_process import ExecutionError

    worker = attempt(store)

    def cancel():
        if worker.pid is not None:
            raise ExecutionError("cancelled")

    with pytest.raises(ExecutionError, match="^cancelled$"):
        worker.run(request(), check=cancel)
    assert worker.pid is None
    assert not (worker.root / "response.json").exists()
    timed = attempt(store)
    with pytest.raises(ExecutionError, match="^timeout$"):
        timed.run(request(), deadline_seconds=0.001)
    assert timed.pid is None
    assert not (timed.root / "response.json").exists()


@pytest.mark.parametrize("deadline", [0, -1, float("nan"), float("inf"), True])
def test_invalid_operator_deadline_refuses_before_source_access(store, deadline):
    from openreading.mcp_server.execution_process import ExecutionError

    worker = attempt(store)
    with pytest.raises(ExecutionError, match="^invalid_configuration$"):
        worker.run(request(), deadline_seconds=deadline)
    assert not worker.root.exists()


def replace_child(monkeypatch, code):
    """Run a real faulting child while preserving the supervisor's pipes, group and environment."""
    import subprocess
    import sys

    from openreading.mcp_server import execution_process

    original = subprocess.Popen

    def launch(command, **kwargs):
        assert command[1:3] == ["-I", "-m"]
        return original([sys.executable, "-I", "-c", code], **kwargs)

    monkeypatch.setattr(execution_process.subprocess, "Popen", launch)


def test_explicit_child_environment_is_snapshotted_without_ambient_keys(store, monkeypatch):
    import json

    monkeypatch.setenv("UNGRANTED_SECRET", "ambient-secret")
    supplied = {"GRANTED_SECRET": "explicit-secret", "PATH": "/operator/bin"}
    worker = attempt(store, environment=supplied)
    supplied["GRANTED_SECRET"] = "later-secret"
    replace_child(
        monkeypatch,
        """
import json, os, sys
json.load(sys.stdin)
assert os.environ['GRANTED_SECRET'] == 'explicit-secret'
assert os.environ['PATH'] == '/operator/bin'
assert 'UNGRANTED_SECRET' not in os.environ
assert os.environ['HOME'] == os.getcwd() + '/home'
assert os.environ['OPENREADING_LEDGER'] == os.getcwd() + '/ledger'
assert os.environ['OPENREADING_CONFIG'] == ''
with open('environment-ok', 'w') as f: f.write('yes')
""",
    )
    from openreading.mcp_server.execution_process import ExecutionError

    with pytest.raises(ExecutionError, match="^execution_failed$"):
        worker.run(request())  # No normalized response, even though environment checks succeeded.
    assert (worker.root / "environment-ok").read_text() == "yes"
    assert "explicit-secret" not in json.dumps(request())
    for path in worker.root.rglob("*"):
        if path.is_file():
            assert b"explicit-secret" not in path.read_bytes()


@pytest.mark.parametrize("environment", [{"": "x"}, {"a=b": "x"}, {"a": "\0"}, {"a": 2}])
def test_invalid_environment_is_refused(store, environment):
    from openreading.mcp_server.execution_process import ExecutionError

    with pytest.raises(ExecutionError, match="^invalid_configuration$"):
        attempt(store, environment=environment)


def test_changed_copy_is_refused_before_process_start(store, monkeypatch):
    from openreading.mcp_server import execution_process

    original = execution_process.copy_source

    def changed(*args, **kwargs):
        digest, _ = original(*args, **kwargs)
        return digest, True

    def forbidden(*args, **kwargs):
        raise AssertionError("Unstable source reached execution")

    monkeypatch.setattr(execution_process, "copy_source", changed)
    monkeypatch.setattr(execution_process.subprocess, "Popen", forbidden)
    worker = attempt(store)
    with pytest.raises(execution_process.ExecutionError, match="^source_changed$"):
        worker.run(request())
    assert worker.pid is None


@pytest.mark.parametrize(
    "code",
    [
        "raise RuntimeError('PRIVATE-DIAGNOSTIC')",
        "open('response.json','w').write('{}')",
        "open('response.json','w').write('broken PRIVATE-DIAGNOSTIC')",
        "os.symlink('source', 'response.json')",
        "os.mkdir('response.json')",
        "os.mkfifo('response.json')",
    ],
)
def test_bad_output_or_exit_never_returns_content_or_diagnostics(store, monkeypatch, capfd, code):
    from openreading.mcp_server.execution_process import ExecutionError

    replace_child(
        monkeypatch,
        "import json, os, sys\njson.load(sys.stdin)\n"
        "print('PRIVATE-DIAGNOSTIC', flush=True)\n" + code,
    )
    worker = attempt(store)
    with pytest.raises(ExecutionError, match="^execution_failed$") as error:
        worker.run(request())
    assert "PRIVATE-DIAGNOSTIC" not in str(error.value)
    assert worker.pid is None
    captured = capfd.readouterr()
    assert "PRIVATE-DIAGNOSTIC" not in captured.out + captured.err


def test_launch_failure_is_fixed_and_does_not_publish(store, monkeypatch):
    from openreading.mcp_server import execution_process

    def denied(*args, **kwargs):
        raise OSError("private launch diagnostic")

    monkeypatch.setattr(execution_process.subprocess, "Popen", denied)
    worker = attempt(store)
    with pytest.raises(execution_process.ExecutionError, match="^execution_failed$"):
        worker.run(request())
    assert worker.pid is None
    assert not (worker.root / "response.json").exists()


@pytest.mark.parametrize("leader_exits", [False, True])
def test_cleanup_kills_descendants_even_after_leader_exit(store, monkeypatch, leader_exits):
    import psutil

    from openreading.mcp_server.execution_process import ExecutionError

    replace_child(
        monkeypatch,
        """
import json, pathlib, subprocess, sys, time
json.load(sys.stdin)
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
pathlib.Path('child-pid').write_text(str(child.pid))
"""
        + ("" if leader_exits else "time.sleep(60)"),
    )
    worker = attempt(store)

    def cancel():
        if not leader_exits and (worker.root / "child-pid").exists():
            raise ExecutionError("cancelled")

    with pytest.raises(ExecutionError, match="^(cancelled|execution_failed)$"):
        worker.run(request(), check=cancel, deadline_seconds=10)
    child_pid = int((worker.root / "child-pid").read_text())
    try:
        child = psutil.Process(child_pid)
        child.wait(timeout=3)
    except psutil.NoSuchProcess:
        pass
    except psutil.TimeoutExpired:
        assert child.status() == psutil.STATUS_ZOMBIE
    assert worker.pid is None


def test_denied_group_control_retains_handle_for_explicit_retry(store, monkeypatch):
    from openreading.mcp_server import execution_process

    replace_child(monkeypatch, "import time; time.sleep(60)")
    original = execution_process.os.killpg

    def denied(*args):
        raise PermissionError("private permission diagnostic")

    worker = attempt(store)

    def cancel():
        if worker.pid is not None:
            raise execution_process.ExecutionError("cancelled")

    monkeypatch.setattr(execution_process.os, "killpg", denied)
    try:
        with pytest.raises(execution_process.ExecutionError, match="^os_permission_denied$"):
            worker.run(request(), check=cancel)
        assert worker.pid is not None
    finally:
        monkeypatch.setattr(execution_process.os, "killpg", original)
        worker.close()
    assert worker.pid is None


def test_open_descriptor_survives_path_replacement_without_following_link(store, monkeypatch):
    from contextlib import contextmanager

    original = store.source
    path = store.config.input_root / "sample.pdf"
    raw = path.read_bytes()
    outside = store.config.input_root.parent / "outside.pdf"
    outside.write_bytes(b"not the granted document")

    @contextmanager
    def switched(relative):
        with original(relative) as fd:
            path.rename(path.with_suffix(".old"))
            path.symlink_to(outside)
            yield fd

    monkeypatch.setattr(store, "source", switched)
    worker = attempt(store)
    content = worker.run(request())
    assert content.provenance.source_sha256 == [hashlib.sha256(raw).hexdigest()]
    assert (worker.root / "source").read_bytes() == raw
    assert "OpenReading Test Document" in content.payload["document"]["text"]


def test_one_attempt_cannot_overwrite_a_completed_attempt(store):
    from openreading.mcp_server.execution_process import ExecutionError

    worker = attempt(store)
    worker.run(request())
    files = {p: p.read_bytes() for p in worker.root.rglob("*") if p.is_file()}
    with pytest.raises(ExecutionError, match="^execution_failed$"):
        worker.run(request())
    assert all(path.read_bytes() == raw for path, raw in files.items())


def test_cancellation_after_child_completion_returns_no_content(store):
    from openreading.mcp_server.execution_process import ExecutionError

    worker = attempt(store)

    def cancel():
        if worker.pid is None and (worker.root / "response.json").exists():
            raise ExecutionError("cancelled")

    with pytest.raises(ExecutionError, match="^cancelled$"):
        worker.run(request(), check=cancel)
    assert worker.pid is None


def test_private_execution_output_can_be_retained_without_changing_content(store):
    from openreading.artifacts.results import RetainedResults

    worker = attempt(store)
    content = worker.run(request())
    retained = RetainedResults(store)
    receipt = retained.publish(content.kind, content.payload, content.provenance)
    assert retained.load(receipt.result_id).wire() == content.wire()
    assert receipt.content_sha256 == hashlib.sha256(json_bytes(content.wire())).hexdigest()


def test_exit_race_permission_denial_retries_after_reaping(store, monkeypatch):
    import time

    from openreading.mcp_server import execution_process

    replace_child(monkeypatch, "import time; time.sleep(60)")
    worker = attempt(store)
    original = execution_process.os.killpg
    signals = []

    def racing(pid, sig):
        signals.append(pid)
        if len(signals) == 1:
            original(pid, sig)
            # Let the killed leader become waitable before the supervisor re-polls it.
            time.sleep(0.05)
            raise PermissionError("exit race")
        return original(pid, sig)

    monkeypatch.setattr(execution_process.os, "killpg", racing)

    def cancel():
        if worker.pid is not None:
            raise execution_process.ExecutionError("cancelled")

    with pytest.raises(execution_process.ExecutionError, match="^cancelled$"):
        worker.run(request(), check=cancel)
    assert len(signals) == 2
    assert worker.pid is None


def test_large_control_input_remains_cancellable_without_duplicate_bytes(store, monkeypatch):
    import json

    payload = {
        "schema_version": "0.3",
        "status": {"state": "partial"},
        "backend": {"id": "pymupdf", "type": "oss_library"},
        "document": {"text": "café"},
        "warnings": [{"code": "fixture", "message": "partial preserved"}],
    }
    replace_child(
        monkeypatch,
        """
import json, os, sys, time
time.sleep(0.25)
packet = json.load(sys.stdin)
assert len(packet['request']['extraction_schema']['instructions']) == 300000
fd = os.open('response.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as output:
    json.dump("""
        + repr(payload)
        + ", output)\n",
    )
    value = request()
    value["extraction_schema"] = {"instructions": "a" * 300000}
    worker = attempt(store)
    result = worker.run(value, deadline_seconds=10)
    assert result.payload == payload
    assert worker.pid is None
    assert json.loads((worker.root / "response.json").read_bytes()) == payload


@pytest.mark.parametrize(
    "name", ["HOME", "TMPDIR", "XDG_CACHE_HOME", "OPENREADING_CONFIG", "OPENREADING_LEDGER"]
)
def test_reserved_environment_is_refused_without_an_attempt(store, name):
    from openreading.mcp_server.execution_process import ExecutionError

    with pytest.raises(ExecutionError, match="^invalid_configuration$"):
        attempt(store, environment={name: "private-operator-value"})
    assert not list((store.config.artifact_root / "execution").glob("**/source"))
