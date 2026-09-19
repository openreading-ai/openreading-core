"""Abrupt supervisor loss cannot strand general execution or its provider children."""

import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import psutil
import pytest


def wait_file(path, process):
    until = time.monotonic() + 10
    while not path.exists():
        assert process.poll() is None, "fixture process exited before publishing its identity"
        assert time.monotonic() < until, "fixture process never published its identity"
        time.sleep(0.01)


def assert_stopped(pid):
    try:
        process = psutil.Process(pid)
        process.wait(timeout=3)
    except psutil.NoSuchProcess:
        return
    except psutil.TimeoutExpired:
        assert process.status() == psutil.STATUS_ZOMBIE


def test_worker_exposes_parent_liveness_guard():
    from openreading.mcp_server import execution_worker

    assert callable(getattr(execution_worker, "watch_parent", None))


def test_parent_liveness_refuses_a_foreign_process_group(monkeypatch):
    from openreading.mcp_server import execution_worker

    monkeypatch.setattr(execution_worker.os, "getpgrp", lambda: -1)

    def forbidden(*args):
        raise AssertionError("Foreign process group armed its liveness channel")

    monkeypatch.setattr(execution_worker.os, "set_inheritable", forbidden)
    with pytest.raises(ValueError, match="own process group"):
        execution_worker.watch_parent(0)


def test_supervisor_sigkill_stops_child_and_provider_grandchild(tmp_path):
    root = tmp_path.resolve()
    script = root / "supervisor.py"
    child = r"""
import pathlib, subprocess, sys, time
from openreading.mcp_server import execution_worker
def blocking_provider(packet):
    provider = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    pathlib.Path('provider-pid').write_text(str(provider.pid))
    while True: time.sleep(1)
execution_worker.execute = blocking_provider
raise SystemExit(execution_worker.main(int(sys.argv[1])))
"""
    script.write_text(
        """
import pathlib, subprocess, sys
from openreading.artifacts.limits import ProfileConfig
from openreading.artifacts.store import Store
from openreading.mcp_server.execution import ExecutionConfig
from openreading.mcp_server.execution_process import ExecutionAttempt
from openreading.testing.sample_pdf import build_sample_pdf
root = pathlib.Path(sys.argv[1])
(root / 'input').mkdir()
(root / 'input' / 'sample.pdf').write_bytes(build_sample_pdf())
store = Store(ProfileConfig(root / 'input', root / 'store'))
original = subprocess.Popen
child_code = """
        + repr(child)
        + """
def launch(command, **kwargs):
    assert '--parent-fd' in command
    parent_fd = command[command.index('--parent-fd') + 1]
    p = original([sys.executable, '-I', '-c', child_code, parent_fd], **kwargs)
    (root / 'worker-pid').write_text(str(p.pid))
    return p
subprocess.Popen = launch
worker = ExecutionAttempt(store, ExecutionConfig.from_operator(allowed_backends=['pymupdf']))
(root / 'workspace').write_text(str(worker.root))
worker.run({'document': {'path': 'sample.pdf'}, 'backend': {'id': 'pymupdf'}})
"""
    )
    supervisor = subprocess.Popen(
        [sys.executable, "-I", str(script), str(root)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    worker_pid = None
    try:
        wait_file(root / "worker-pid", supervisor)
        worker_pid = int((root / "worker-pid").read_text())
        workspace = Path((root / "workspace").read_text())
        wait_file(workspace / "provider-pid", supervisor)
        provider_pid = int((workspace / "provider-pid").read_text())
        supervisor.kill()
        supervisor.wait(timeout=3)
        assert_stopped(worker_pid)
        assert_stopped(provider_pid)
    finally:
        supervisor.kill() if supervisor.poll() is None else None
        supervisor.wait()
        if worker_pid is not None:
            with suppress(ProcessLookupError):
                os.killpg(worker_pid, signal.SIGKILL)
        if supervisor.stderr is not None:
            supervisor.stderr.close()


def test_exchange_sends_every_byte_after_pipe_backpressure():
    from openreading.mcp_server.execution_process import _exchange

    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            'import sys,time; time.sleep(.25); data=sys.stdin.buffer.read(); sys.exit(0 if data==b"x"*300000 else 1)',
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    started = time.monotonic()

    def check():
        assert time.monotonic() - started < 10, "control send stalled"

    try:
        _exchange(child, b"x" * 300000, check)
        assert child.returncode == 0
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        if child.stdin is not None:
            child.stdin.close()


def test_exchange_observes_cancellation_while_pipe_is_full():
    from openreading.mcp_server.execution_process import ExecutionError, _exchange

    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.PIPE
    )
    started = time.monotonic()

    def check():
        if time.monotonic() - started > 0.2:
            raise ExecutionError("cancelled")

    try:
        with pytest.raises(ExecutionError, match="^cancelled$"):
            _exchange(child, b"x" * 300000, check)
    finally:
        child.kill()
        child.wait()
        if child.stdin is not None:
            child.stdin.close()


def test_liveness_eof_signals_the_owned_group_before_exit(monkeypatch):
    from types import SimpleNamespace

    from openreading.mcp_server import execution_worker

    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    signals = []
    monkeypatch.setattr(execution_worker.os, "getpgrp", os.getpid)
    monkeypatch.setattr(execution_worker.os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    def exit_process(status):
        raise SystemExit(status)

    monkeypatch.setattr(execution_worker.os, "_exit", exit_process)
    monkeypatch.setattr(
        execution_worker.threading,
        "Thread",
        lambda *, target, daemon: SimpleNamespace(start=target),
    )
    with pytest.raises(SystemExit) as error:
        execution_worker.watch_parent(read_fd)
    assert error.value.code == 1
    assert signals == [(os.getpid(), signal.SIGKILL)]
    with pytest.raises(OSError):
        os.fstat(read_fd)
