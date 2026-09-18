"""Run one authorized request in an owned process before general MCP publication.

ExecutionAttempt copies a granted source into a new private attempt directory.
The child reauthorizes, verifies the copied bytes, and calls the shared scoped API.
For example, a configured strategy cannot dispatch a leaf outside allowed_backends.
The returned ResultContent preserves complete normalized values and measured input fingerprints.
It adds no physical-page evidence and publishes no retained result or job status itself.

Each attempt has isolated stdout, stderr, working directory, home, temporary files and journal.
A journal records strategy steps for replay. Its private location belongs to this attempt.
Only explicitly supplied operator environment values reach provider SDKs or the credential broker.
Defaults use the system executable search path; operators must pass custom runtime paths explicitly.
Core configuration and ledger variables always point at the captured configuration and private journal.
Operator overrides of HOME, TMPDIR, XDG_CACHE_HOME, OPENREADING_CONFIG and OPENREADING_LEDGER fail with invalid_configuration.
For example, supply PATH for a custom executable location instead of replacing the private home directory.
The parent never mutates its environment. Credential values are not written into control files.
For example, an ambient cloud key in the MCP host does not silently authorize this child.
This is process isolation, not an operating-system sandbox or a prohibition on provider network access.
Installed adapters remain trusted code and may read other files available to the OS user.

Cancellation and optional deadlines cover copying, child input, execution and output validation.
Cleanup kills the owned process group even after its leader exits, then reaps the leader.
A separate liveness pipe lets the child kill its group if this supervisor exits abruptly.
Only the supervisor retains the write end; provider children cannot keep their supervisor alive.
Denied process control preserves the process handle for another explicit close attempt.
Local termination does not establish cancellation of work already submitted to a remote provider.
There is no automatic retry, document cap, execution cache, eviction or memory-bound claim.
Copying refuses a source whose metadata changed, rather than reporting a stable source identity.
Documents and JSON are materialized in memory by the shared API and result validator.

Attempt directories remain under execution/<grant>/<random-id>, outside local-import staging.
The execution_jobs owner records each attempt location; operators remove retained directories deliberately.
An instance accepts one attempt; a second run refuses instead of overwriting its retained files.
The general MCP profile exposes this worker through execution_jobs for parsing and scoped strategy continuation.
Resume copies a hash-bound retained source and journal rather than acquiring the original input again.
The child validates that snapshot before calling the shared resume API under current backend authority.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import re
import select
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from typing import Literal

from openreading.artifacts.intake import copy_source, directory
from openreading.artifacts.models import json_bytes
from openreading.artifacts.result_models import ResultContent, ResultProvenance
from openreading.artifacts.store import Store, safe_read
from openreading.mcp_server.execution import ExecutionConfig

_RESERVED_ENVIRONMENT = frozenset(
    {"HOME", "TMPDIR", "XDG_CACHE_HOME", "OPENREADING_CONFIG", "OPENREADING_LEDGER"}
)


class ExecutionError(Exception):
    """Fixed internal lifecycle errors exclude provider messages, document text and credentials."""

    def __init__(
        self,
        code: Literal[
            "invalid_configuration",
            "execution_failed",
            "source_changed",
            "cancelled",
            "timeout",
            "os_permission_denied",
        ],
    ):
        self.code = code
        super().__init__(code)


class ExecutionAttempt:
    """Own one disposable child and retain its private input and journal for a later job owner."""

    def __init__(
        self,
        store: Store,
        authority: ExecutionConfig,
        *,
        environment: Mapping[str, str] | None = None,
    ):
        self.store, self.authority = store, authority
        self.environment = dict(environment or {})
        if any(
            not isinstance(k, str)
            or not isinstance(v, str)
            or not k
            or k in _RESERVED_ENVIRONMENT
            or "=" in k
            or "\0" in k
            or "\0" in v
            for k, v in self.environment.items()
        ):
            raise ExecutionError("invalid_configuration")
        self.root = store.config.artifact_root / "execution" / store.grant / uuid.uuid4().hex
        self._process: subprocess.Popen | None = None
        self._started = False
        self._liveness_fd: int | None = None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    def close(self):
        """Reap this attempt's group; retain ownership if operating-system control is denied."""
        process = self._process
        if process is None:
            self._close_liveness()
            return
        try:
            process.poll()
            with suppress(ProcessLookupError):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except PermissionError:
                    if process.poll() is None:
                        raise
                    os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            self._process = None
        except PermissionError:
            raise ExecutionError("os_permission_denied") from None
        finally:
            if process.stdin is not None:
                with suppress(OSError):
                    process.stdin.close()
            if self._process is None:
                self._close_liveness()

    def _close_liveness(self):
        if self._liveness_fd is not None:
            os.close(self._liveness_fd)
            self._liveness_fd = None

    def run(
        self,
        value: dict,
        *,
        check: Callable[[], None] | None = None,
        deadline_seconds: float | None = None,
        resume: dict | None = None,
    ) -> ResultContent:
        """Authorize before acquisition and return validated content only after child cleanup."""
        if deadline_seconds is not None and (
            isinstance(deadline_seconds, bool)
            or not math.isfinite(deadline_seconds)
            or deadline_seconds <= 0
        ):
            raise ExecutionError("invalid_configuration")
        started = time.monotonic()

        def observe():
            if check is not None:
                check()
            if deadline_seconds is not None and time.monotonic() - started >= deadline_seconds:
                raise ExecutionError("timeout")

        plan = self.authority.authorize(value)
        observe()
        if self._started:
            raise ExecutionError("execution_failed")
        self._started = True
        relative = plan.request.document.path
        assert relative is not None
        if resume is None:
            with self.store.source(relative) as fd:
                with directory(self.root.parent, create=True):
                    pass
                self.root.mkdir(mode=0o700)
                digest, changed = copy_source(fd, self.root / "source", None, None, check=observe)
            if changed:
                raise ExecutionError("source_changed")
        else:
            from openreading.mcp_server.resume_input import copy_snapshot, verify_snapshot

            if not re.fullmatch(r"[0-9a-f]{32}", resume["directory"]):
                raise ExecutionError("execution_failed")
            with directory(self.root.parent, create=True):
                pass
            self.root.mkdir(mode=0o700)
            copy_snapshot(self.root.parent / resume["directory"], self.root, resume, observe)
            verify_snapshot(self.root, self.authority, resume)
            digest = resume["source_sha256"]
        packet = json_bytes(
            {
                "configuration": json.loads(self.authority.configuration),
                "allowed_backends": sorted(self.authority.allowed_backends),
                "allowed_strategies": sorted(self.authority.allowed_strategies),
                "request": json.loads(plan.request_json),
                "source_sha256": digest,
                **({"resume": resume} if resume is not None else {}),
            }
        )
        self._run_child(packet, "openreading.mcp_server.execution_worker", observe)
        observe()
        try:
            payload = json.loads(safe_read(self.root / "response.json", None))
            content = ResultContent(
                kind="normalized_response",
                payload=payload,
                provenance=ResultProvenance(
                    request_sha256=hashlib.sha256(plan.request_json).hexdigest(),
                    config_sha256=self.authority.fingerprint,
                    core_version=importlib.metadata.version("openreading"),
                    core_commit=None,
                    adapters={payload["backend"]["id"]: payload["backend"].get("version")},
                    source_sha256=[digest],
                    subjects={},
                ),
            )
        except Exception:
            raise ExecutionError("execution_failed") from None
        observe()
        return content

    def _run_child(self, packet: bytes, module: str, observe: Callable[[], None]) -> None:
        """Run a trusted internal module with the same private environment and process ownership."""
        for name in ("home", "tmp", "cache", "ledger"):
            with directory(self.root / name, create=True):
                pass
        environment = {
            "PATH": os.defpath,
            **self.environment,
            "HOME": str(self.root / "home"),
            "TMPDIR": str(self.root / "tmp"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "OPENREADING_CONFIG": "",
            "OPENREADING_LEDGER": str(self.root / "ledger"),
        }
        try:
            observe()
            parent_fd, self._liveness_fd = os.pipe()
            try:
                self._process = subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        "-m",
                        module,
                        "--parent-fd",
                        str(parent_fd),
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    cwd=self.root,
                    env=environment,
                    start_new_session=True,
                    pass_fds=(parent_fd,),
                )
            finally:
                os.close(parent_fd)
            _exchange(self._process, packet, observe)
            if self._process.returncode != 0:
                raise ExecutionError("execution_failed")
        except (OSError, ValueError):
            raise ExecutionError("execution_failed") from None
        finally:
            self.close()


def _exchange(process: subprocess.Popen, packet: bytes, check: Callable[[], None]) -> None:
    """Send complete control bytes while keeping pipe backpressure and process waits cancellable."""
    assert process.stdin is not None
    fd = process.stdin.fileno()
    os.set_blocking(fd, False)
    pending = memoryview(packet)
    # Retrying communicate after a timeout can strand partial stdin on older supported Python.
    # Explicit offsets avoid that dependency while preserving cancellation during backpressure.
    while pending:
        check()
        _, writable, _ = select.select([], [fd], [], 0.1)
        if writable:
            try:
                written = os.write(fd, pending)
            except BlockingIOError:
                continue
            pending = pending[written:]
    process.stdin.close()
    while True:
        check()
        try:
            process.wait(timeout=0.1)
            return
        except subprocess.TimeoutExpired:
            pass
