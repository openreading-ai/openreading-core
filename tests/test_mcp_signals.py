"""Both stop signals terminate MCP while the client keeps its input pipe open."""

import json
import os
import select
import signal
import subprocess
import sys
from contextlib import suppress

import pytest


@pytest.mark.parametrize("stop", [signal.SIGINT, signal.SIGTERM])
def test_stop_signal_with_open_stdin_exits_130(tmp_path, stop):
    root = tmp_path.resolve() / "input"
    root.mkdir()
    command = [
        sys.executable,
        "-m",
        "openreading.cli",
        "mcp",
        "--profile",
        "local-document-proof-v1",
        "--input-root",
        str(root),
        "--artifact-root",
        str(root.parent / "store"),
    ]
    with subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as process:
        try:
            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            }
            process.stdin.write(json.dumps(request).encode() + b"\n")
            process.stdin.flush()
            assert select.select([process.stdout], [], [], 10)[0], "server did not initialize"
            assert json.loads(process.stdout.readline())["id"] == 1
            os.kill(process.pid, stop)
            assert process.wait(timeout=5) == 130
            assert b"Traceback" not in process.stderr.read()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


@pytest.mark.parametrize(
    "payload",
    [
        {"secret-document-password": "private-content"},
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "openreading_import", "arguments": "private-content"},
        },
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": {"secret-document-password": "private-content"}},
        },
    ],
)
def test_malformed_protocol_does_not_echo_input(tmp_path, payload):
    root = tmp_path.resolve() / "input"
    root.mkdir()
    command = [
        sys.executable,
        "-m",
        "openreading.cli",
        "mcp",
        "--profile",
        "local-document-proof-v1",
        "--input-root",
        str(root),
        "--artifact-root",
        str(root.parent / "store"),
    ]
    result = subprocess.run(
        command,
        input=json.dumps(payload).encode() + b"\n",
        capture_output=True,
        timeout=10,
    )
    assert b"private-content" not in result.stderr + result.stdout
    assert b"secret-document-password" not in result.stderr + result.stdout
    assert result.returncode == 0


@pytest.mark.parametrize("stop", [signal.SIGINT, signal.SIGTERM])
def test_stop_signal_reaps_active_worker_before_releasing_store(tmp_path, stop):
    import time

    from openreading.artifacts.limits import ProfileConfig
    from openreading.artifacts.store import Store
    from openreading.testing.sample_pdf import build_sample_pdf

    root = tmp_path.resolve() / "input"
    root.mkdir()
    (root / "sample.pdf").write_bytes(build_sample_pdf())
    store = root.parent / "store"
    pid_file = root.parent / "worker.pid"
    script = """
import subprocess, sys
from pathlib import Path
from openreading.cli.app import main
from openreading.artifacts import service
from types import SimpleNamespace
original = subprocess.Popen
def slow_worker(command, **kwargs):
    process = original([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
    Path(sys.argv[1]).write_text(str(process.pid))
    return process
service.subprocess = SimpleNamespace(Popen=slow_worker, DEVNULL=subprocess.DEVNULL)
raise SystemExit(main(sys.argv[2:]))
"""
    command = [
        sys.executable,
        "-c",
        script,
        str(pid_file),
        "mcp",
        "--profile",
        "local-document-proof-v1",
        "--input-root",
        str(root),
        "--artifact-root",
        str(store),
    ]
    with subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as process:
        worker = None
        try:

            def send(payload):
                process.stdin.write(json.dumps({"jsonrpc": "2.0", **payload}).encode() + b"\n")
                process.stdin.flush()

            send(
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                }
            )
            assert select.select([process.stdout], [], [], 10)[0]
            assert json.loads(process.stdout.readline())["id"] == 1
            send({"method": "notifications/initialized"})
            send(
                {
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "openreading_import", "arguments": {"path": "sample.pdf"}},
                }
            )
            deadline = time.monotonic() + 10
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert pid_file.exists(), "worker did not start"
            worker = int(pid_file.read_text())
            os.kill(process.pid, stop)
            assert process.wait(timeout=5) == 130
            with pytest.raises(ProcessLookupError):
                os.kill(worker, 0)
            assert list((store / "staging").iterdir()) == []
            with Store(ProfileConfig(root, store)).import_lock():
                pass
            assert b"Traceback" not in process.stderr.read()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            if worker:
                with suppress(ProcessLookupError):
                    os.killpg(worker, signal.SIGKILL)
