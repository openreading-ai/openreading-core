"""Own one warm parser process with bounded private control messages and sampled RSS.

A process group includes OCR children. Cancellation, deadline, malformed output, or
monitoring failure kills that group before a job releases ownership. A well-formed input
rejection, such as a password-protected file, keeps the generation and its loaded model.
Idle shutdown reaps the worker; the next explicit job starts a new generation without
retrying failures.
RSS is a sampled process-tree sum, not a hard operating-system memory reservation.
The worker starts in a caller-selected private directory. ONNX Runtime 1.30 writes a
telemetry session file into its working directory, and a client may launch the server anywhere.
"""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import threading
import uuid
from collections.abc import Callable
from contextlib import suppress

from openreading.artifacts.limits import INPUT_REJECTIONS, MESSAGES, ArtifactError
from openreading.artifacts.models import ToolError, json_bytes

STAGES = ("preflight", "conversion", "writing")


class _Rejected(ArtifactError):
    """An input rejection the worker reported cleanly, so its generation stays usable."""


def process_rss(pid: int) -> int:
    import psutil

    parent = psutil.Process(pid)
    total = 0
    for process in [parent, *parent.children(recursive=True)]:
        try:
            total += process.memory_info().rss
        except psutil.NoSuchProcess:
            if process.pid == pid:
                raise
    return total


class WarmWorker:
    def __init__(
        self,
        command: list[str],
        *,
        memory_bytes: int,
        idle_seconds: float,
        rss: Callable[[int], int] = process_rss,
        cwd: str | os.PathLike[str] | None = None,
    ):
        if memory_bytes <= 0 or idle_seconds <= 0:
            raise ValueError("Worker resource limits must be positive.")
        self.command, self.memory_bytes, self.idle_seconds = command, memory_bytes, idle_seconds
        self.rss = rss
        self.cwd = cwd
        self._lock = threading.Lock()
        self._process = None
        self._read_fd = None
        self._timer = None
        self._idle_token = None

    @property
    def pid(self):
        return self._process.pid if self._process is not None else None

    def _start(self):
        if self._process is not None and self._process.poll() is None:
            return
        self._stop()
        read_fd, write_fd = os.pipe()
        try:
            self._process = subprocess.Popen(
                [*self.command, "--control-fd", str(write_fd)],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                pass_fds=(write_fd,),
                start_new_session=True,
                cwd=self.cwd,
            )
            self._read_fd = read_fd
        except BaseException:
            os.close(read_fd)
            raise
        finally:
            os.close(write_fd)

    def _stop(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._idle_token = None
        if self._process is not None:
            process, self._process = self._process, None
            # Kill the group even if its leader exited while leaving an OCR child alive.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            if process.stdin is not None:
                process.stdin.close()
        if self._read_fd is not None:
            os.close(self._read_fd)
            self._read_fd = None

    def close(self):
        with self._lock:
            self._stop()

    def _arm_idle(self, token):
        self._idle_token = token
        self._timer = threading.Timer(self.idle_seconds, self._idle, (token,))
        self._timer.daemon = True
        self._timer.start()

    def _idle(self, token):
        with self._lock:
            if self._idle_token == token:
                self._stop()

    def run(
        self, job: dict, *, check: Callable[[], None], progress: Callable[[str], None] | None = None
    ):
        if not self._lock.acquire(blocking=False):
            raise ArtifactError("busy")
        try:
            check()
            self._idle_token = None
            if self._timer is not None:
                self._timer.cancel()
            self._start()
            assert self._process is not None and self._process.stdin is not None
            assert self._read_fd is not None
            identifier = uuid.uuid4().hex
            data = json_bytes({**job, "id": identifier}) + b"\n"
            if len(data) > 8192:
                raise ArtifactError("parse_failed")
            self._process.stdin.write(data)
            self._process.stdin.flush()
            pending = b""
            last_stage = -1
            while True:
                check()
                try:
                    rss = self.rss(self._process.pid)
                    if type(rss) is not int or rss < 0:
                        raise ValueError
                except Exception:
                    if self._process.poll() is not None:
                        raise ArtifactError("parse_failed") from None
                    raise ArtifactError("worker_monitor_failed") from None
                if rss > self.memory_bytes:
                    raise ArtifactError("memory_limit")
                readable, _, _ = select.select([self._read_fd], [], [], 0.1)
                if not readable:
                    continue
                chunk = os.read(self._read_fd, 8193 - len(pending))
                if not chunk:
                    raise ArtifactError("parse_failed")
                pending += chunk
                if len(pending) > 8192:
                    raise ArtifactError("parse_failed")
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    result = json.loads(line)
                    if not isinstance(result, dict) or result.get("id") != identifier:
                        raise ArtifactError("parse_failed")
                    if set(result) == {"id", "stage"} and result["stage"] in STAGES:
                        stage = STAGES.index(result["stage"])
                        if stage <= last_stage:
                            raise ArtifactError("parse_failed")
                        last_stage = stage
                        if progress is not None:
                            progress(result["stage"])
                    elif set(result) == {"id", "error"} and result["error"] in MESSAGES:
                        code = ToolError(code=result["error"], message="", retryable=False).code
                        if code in INPUT_REJECTIONS and not pending:
                            check()
                            self._arm_idle(identifier)
                            raise _Rejected(code)
                        raise ArtifactError(code)
                    elif (
                        result == {"id": identifier, "ok": True}
                        and result["ok"] is True
                        and not pending
                    ):
                        check()
                        self._arm_idle(identifier)
                        return
                    else:
                        raise ArtifactError("parse_failed")
        except _Rejected as rejection:
            raise ArtifactError(rejection.code) from None
        except BaseException as error:
            self._stop()
            if isinstance(error, (ArtifactError, KeyboardInterrupt, SystemExit)):
                raise
            raise ArtifactError("parse_failed") from None
        finally:
            self._lock.release()
