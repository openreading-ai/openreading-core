"""Persist general execution through a detached supervisor under the original input grant.

Each accepted parse or batch creates one ej1 job; repeating start creates new work rather than reusing output.
Batch requests authorize every item before acquiring sources and retain the shared ordered batch-result envelope.
Each batch holds one concurrency slot; its isolated item attempts execute serially under the same cancellation checks.
For example, two duplicate source requests produce two batch items instead of a deduplicated import.
Empty input retains a batch with status.state=failed and the warning code empty_batch.
Job state=succeeded means publication only, including publication of an empty batch with a failed aggregate status.
The supervisor acquires an operator-configured concurrency slot before starting an ExecutionAttempt.
Disconnecting the MCP client leaves accepted work running. Explicit cancellation and deadlines include queue time.
No provider is automatically retried after supervisor interruption, even when no receipt was returned.

Public status separates successful result retention from a normalized response or batch aggregate outcome.
A valid failed response is retained unchanged with job state succeeded and response_state failed.
Publication records its expected receipt first, then commits through RetainedResults and writes terminal status.
After supervisor death, recovery recognizes only an integrity-verified committed result matching that intent.
An uncommitted attempt becomes interrupted. This does not establish remote-provider cancellation.
Status lookup and listing persist recovered terminal status, so neither operation is read-only.
For example, get records interrupted after discovering a dead supervisor without a committed result.
Cancellation observed before publication prevents retention; committed publication wins a concurrent cancellation request.

Private control records bind the grant and job identity; payload hashes detect accidental byte edits.
Reads reject foreign records and special files. Source copying occurs after queueing, using the then-current granted bytes.
Source references and explicit configuration remain private; credential environment values never enter these records.
The detached process receives only the operator-supplied environment and private runtime directories.
Its execution worker owns a separate process group and watches supervisor liveness for abrupt-death cleanup.
Process identity includes creation time, preventing a reused PID from masquerading as an older supervisor.
The store is trusted against malicious rewriting by its OS owner, as with retained document artifacts.

Job history, source snapshots and journals persist without automatic eviction or an uninstall hook.
Listing uses job-ID order with grant-bound cursors and fifty summaries per reply at most.
Concurrent new jobs can sort before a cursor; restart listing to discover them.
The general MCP profile exposes these jobs without an input-size cap or provider readiness claim.
Frozen-client dispatch and scoped resume remain separate integration work.
"""

from __future__ import annotations

import fcntl
import hashlib
import heapq
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Literal, cast

import psutil
from pydantic import TypeAdapter

from openreading.artifacts.intake import directory
from openreading.artifacts.jobs import _write
from openreading.artifacts.limits import ArtifactError, ProfileConfig
from openreading.artifacts.models import json_bytes
from openreading.artifacts.result_models import ResultContent, ResultReceipt
from openreading.artifacts.results import RetainedResults
from openreading.artifacts.search import _binding, _cursor, _offset
from openreading.artifacts.store import Store, safe_read
from openreading.mcp_server.batch_execution import authorize_batch, execute_batch
from openreading.mcp_server.execution import ExecutionConfig, ExecutionRefused
from openreading.mcp_server.execution_process import ExecutionAttempt, ExecutionError
from openreading.types.execution_job import (
    ExecutionFault,
    ExecutionFaultCode,
    ExecutionJob,
    ExecutionJobGet,
    ExecutionJobList,
    ExecutionJobListRequest,
    ExecutionJobSummary,
    ResponseOutcome,
)

TERMINAL = {"succeeded", "failed", "cancelled"}
_PATTERN = r"ej1_[0-9a-f]{32}"


class ExecutionJobError(Exception):
    """Lookup and startup failures carry fixed codes without private exception details."""

    def __init__(self, code: Literal["job_not_found", "job_state_invalid", "job_start_failed"]):
        self.code = code
        super().__init__(code)


@contextmanager
def slot(root: Path, index: int) -> Iterator[None]:
    """Acquire one nonblocking slot without following or replacing an existing lock entry."""
    with directory(root) as parent:
        fd = os.open(
            f"slot-{index}",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=parent,
        )
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("Invalid execution slot")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _write_bound(root: Path, name: str, grant: str, payload: dict):
    _write(
        root / name,
        {
            "format": "execution-job-record.v0.1",
            "grant": grant,
            "job_id": root.name,
            "payload": payload,
            "payload_sha256": hashlib.sha256(json_bytes(payload)).hexdigest(),
        },
    )


def _read_bound(root: Path, name: str, grant: str, cap: int | None = 65536) -> dict:
    (root / name).lstat()
    value = json.loads(safe_read(root / name, cap))
    if (
        not isinstance(value, dict)
        or set(value) != {"format", "grant", "job_id", "payload", "payload_sha256"}
        or value["format"] != "execution-job-record.v0.1"
        or value["grant"] != grant
        or value["job_id"] != root.name
        or not isinstance(value["payload"], dict)
        or value["payload_sha256"] != hashlib.sha256(json_bytes(value["payload"])).hexdigest()
    ):
        raise ValueError("Invalid bound execution record")
    return value["payload"]


def _cancelled(root: Path, grant: str) -> bool:
    try:
        _read_bound(root, "cancel.json", grant)
    except FileNotFoundError:
        return False
    return True


def _status(root: Path, grant: str) -> ExecutionJob:
    value = ExecutionJob.model_validate(_read_bound(root, "status.json", grant))
    if value.job_id != root.name:
        raise ValueError("Job status identity mismatch")
    return value


def _alive(owner: dict) -> bool:
    if type(owner.get("pid")) is not int or type(owner.get("created")) is not float:
        raise ValueError("Invalid process identity")
    try:
        process = psutil.Process(owner["pid"])
        return (
            process.create_time() == owner["created"] and process.status() != psutil.STATUS_ZOMBIE
        )
    except psutil.NoSuchProcess:
        return False


class ExecutionJobs:
    """Accept explicitly authorized jobs; existing grant history is readable without execution scope."""

    def __init__(
        self,
        store: Store,
        authority: ExecutionConfig,
        *,
        environment: Mapping[str, str] | None = None,
        deadline_seconds: float | None = None,
        concurrency: int = 1,
    ):
        if (
            type(concurrency) is not int
            or concurrency < 1
            or (
                deadline_seconds is not None
                and (
                    isinstance(deadline_seconds, bool)
                    or not math.isfinite(deadline_seconds)
                    or deadline_seconds <= 0
                )
            )
        ):
            raise ExecutionError("invalid_configuration")
        # Reuse environment validation without creating an attempt directory or acquiring input.
        self.environment = ExecutionAttempt(store, authority, environment=environment).environment
        self.store, self.authority = store, authority
        self.deadline_seconds, self.concurrency = deadline_seconds, concurrency
        self.root = store.config.artifact_root / "execution-jobs" / store.grant
        with directory(self.root, create=True):
            pass

    def start(self, value: dict) -> ExecutionJob:
        plan = self.authority.authorize(value)
        return self._start(json.loads(plan.request_json), "parse")

    def start_batch(self, value: dict) -> ExecutionJob:
        requests = authorize_batch(self.authority, value)
        return self._start({"requests": requests}, "batch")

    def _start(self, value: dict, operation: str) -> ExecutionJob:
        requests = value["requests"] if operation == "batch" else [value]
        for item in requests:
            with self.store.source(item["document"]["path"]):
                pass
        root = self.root / ("ej1_" + uuid.uuid4().hex)
        process = None
        try:
            root.mkdir(mode=0o700)
            initial = ExecutionJob(
                job_id=root.name, state="queued", stage="queued", elapsed_seconds=0
            )
            _write_bound(root, "status.json", self.store.grant, initial.wire())
            _write_bound(
                root,
                "request.json",
                self.store.grant,
                {
                    "input_root": str(self.store.config.input_root),
                    "artifact_root": str(self.store.config.artifact_root),
                    "configuration": json.loads(self.authority.configuration),
                    "allowed_backends": sorted(self.authority.allowed_backends),
                    "allowed_strategies": sorted(self.authority.allowed_strategies),
                    "request": value,
                    "operation": operation,
                    "started": time.time(),
                    "deadline_seconds": self.deadline_seconds,
                    "concurrency": self.concurrency,
                    "environment_names": sorted(self.environment),
                },
            )
            for name in ("home", "tmp", "cache"):
                (root / name).mkdir(mode=0o700)
            process = subprocess.Popen(
                [sys.executable, "-I", "-m", "openreading.mcp_server.execution_jobs", str(root)],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                cwd=root,
                env={
                    "PATH": os.defpath,
                    **self.environment,
                    "HOME": str(root / "home"),
                    "TMPDIR": str(root / "tmp"),
                    "XDG_CACHE_HOME": str(root / "cache"),
                    "OPENREADING_CONFIG": "",
                    "OPENREADING_LEDGER": str(root / "ledger"),
                },
            )
            _write_bound(
                root,
                "process.json",
                self.store.grant,
                {
                    "pid": process.pid,
                    "created": psutil.Process(process.pid).create_time(),
                },
            )
            # The child waits until ownership is persisted, preventing an untracked acceptance race.
            assert process.stdin is not None
            process.stdin.write(b"1")
            process.stdin.close()
            threading.Thread(target=process.wait, daemon=True).start()
            return initial
        except Exception:
            if process is not None:
                with suppress(ProcessLookupError):
                    process.kill()
                process.wait()
                if process.stdin is not None:
                    process.stdin.close()
            # Keep failed preparation auditable; no provider can start before the ownership handshake.
            raise ExecutionJobError("job_start_failed") from None

    def _root(self, job_id: str) -> Path:
        if not isinstance(job_id, str) or re.fullmatch(_PATTERN, job_id) is None:
            raise ExecutionJobError("job_not_found")
        root = self.root / job_id
        try:
            with directory(root):
                pass
        except (OSError, ArtifactError):
            raise ExecutionJobError("job_not_found") from None
        return root

    def get(self, job_id: str, wait_seconds: float = 0) -> ExecutionJob:
        """Return status, persisting recovery when the recorded supervisor has exited."""
        ExecutionJobGet(job_id=job_id, wait_seconds=wait_seconds)
        root = self._root(job_id)
        until = time.monotonic() + wait_seconds
        try:
            while True:
                value = _status(root, self.store.grant)
                if value.state in TERMINAL:
                    return value
                request = _read_bound(root, "request.json", self.store.grant, None)
                value.elapsed_seconds = max(value.elapsed_seconds, time.time() - request["started"])
                value.cancel_requested = _cancelled(root, self.store.grant)
                if not _alive(_read_bound(root, "process.json", self.store.grant)):
                    final = _status(root, self.store.grant)
                    if final.state in TERMINAL:
                        return final
                    return self._recover(root, value)
                if time.monotonic() >= until:
                    return value
                time.sleep(0.05)
        except ExecutionJobError:
            raise
        except Exception:
            raise ExecutionJobError("job_state_invalid") from None

    def _recover(self, root: Path, value: ExecutionJob) -> ExecutionJob:
        try:
            intent = _read_bound(root, "publication.json", self.store.grant)
        except FileNotFoundError:
            intent = None
        if intent is not None:
            receipt = ResultReceipt.model_validate(intent["receipt"])
            results = RetainedResults(self.store)
            try:
                content = results.load(receipt.result_id)
            except Exception:
                content = None
            # A valid result does not bind the separate intent's claimed size, digest or kind.
            if content is not None and results.receipt(receipt.result_id, content) == receipt:
                value.state, value.stage = "succeeded", "complete"
                value.receipt = receipt
                value.response_state = _response_state(content)
                value.error = None
                _write_bound(
                    root,
                    "status.json",
                    self.store.grant,
                    ExecutionJob.model_validate(value.wire()).wire(),
                )
                return value
        value.state, value.stage = "failed", "stopped"
        value.error = ExecutionFault(code="interrupted")
        _write_bound(root, "status.json", self.store.grant, value.wire())
        return value

    def cancel(self, job_id: str) -> ExecutionJob:
        current = self.get(job_id)
        if current.state not in TERMINAL:
            _write_bound(self._root(job_id), "cancel.json", self.store.grant, {})
            current = self.get(job_id)
        return current

    def list(self, limit: int = 20, cursor: str | None = None) -> ExecutionJobList:
        """Return summaries through get, including its persistent recovery of interrupted jobs."""
        ExecutionJobListRequest(limit=limit, cursor=cursor)
        binding = _binding(["execution_jobs", "0.1", str(self.root), self.store.grant, limit])
        offset = _offset(cursor, binding, (1 << 128) + 1)
        with directory(self.root) as fd, os.scandir(fd) as entries:
            identifiers = heapq.nsmallest(
                limit + 1,
                (
                    entry.name
                    for entry in entries
                    if re.fullmatch(_PATTERN, entry.name)
                    and int(entry.name[4:], 16) >= offset
                    and entry.is_dir(follow_symlinks=False)
                ),
            )
        rows = []
        for job_id in identifiers[:limit]:
            try:
                value = self.get(job_id)
                rows.append(
                    ExecutionJobSummary(
                        job_id=job_id, state=value.state, elapsed_seconds=value.elapsed_seconds
                    )
                )
            except ExecutionJobError:
                rows.append(
                    ExecutionJobSummary(job_id=job_id, state="unavailable", elapsed_seconds=None)
                )
        return ExecutionJobList(
            jobs=rows,
            next_cursor=_cursor(binding, int(identifiers[limit - 1][4:], 16) + 1)
            if len(identifiers) > limit
            else None,
        )


def _response_state(content: ResultContent) -> ResponseOutcome:
    status = content.payload["status"]
    if not isinstance(status, dict):
        raise ValueError("Invalid response status")
    return TypeAdapter(ResponseOutcome).validate_python(status["state"])


def run(root: Path) -> int:
    """Execute one accepted job after the launching process persists its ownership record."""
    grant = root.parent.name
    request = _read_bound(root, "request.json", grant, None)
    store = Store(ProfileConfig(Path(request["input_root"]), Path(request["artifact_root"])))
    if (
        store.grant != grant
        or root != store.config.artifact_root / "execution-jobs" / grant / root.name
    ):
        store.close()
        raise ValueError("Job grant does not match its retained input directory")
    authority = ExecutionConfig.from_operator(
        config=request["configuration"],
        allowed_backends=request["allowed_backends"],
        allowed_strategies=request["allowed_strategies"],
    )
    manager = ExecutionJobs(
        store,
        authority,
        environment={k: os.environ[k] for k in request["environment_names"]},
        deadline_seconds=request["deadline_seconds"],
        concurrency=request["concurrency"],
    )
    value = _status(root, grant)
    if value.state != "queued":
        store.close()
        return 0 if value.state == "succeeded" else 1
    started = time.monotonic()
    age = max(0.0, time.time() - request["started"])
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}

    def check():
        if stopping or _cancelled(root, grant):
            raise ExecutionError("cancelled")
        if (
            manager.deadline_seconds is not None
            and age + time.monotonic() - started >= manager.deadline_seconds
        ):
            raise ExecutionError("timeout")

    def save():
        value.elapsed_seconds = max(value.elapsed_seconds, age + time.monotonic() - started)
        value.cancel_requested = stopping or _cancelled(root, grant)
        _write_bound(root, "status.json", grant, ExecutionJob.model_validate(value.wire()).wire())

    attempt = ExecutionAttempt(store, authority, environment=manager.environment)
    try:
        operation = request.get("operation", "parse")
        if operation == "batch":
            authorize_batch(authority, request["request"])
        elif operation == "parse":
            authority.authorize(request["request"])
        else:
            raise ExecutionRefused("invalid_request")
        while True:
            check()
            acquired = False
            for index in range(manager.concurrency):
                # Only acquisition may mean busy; provider exceptions must not requeue the request.
                lock = slot(manager.root, index)
                try:
                    lock.__enter__()
                except BlockingIOError:
                    continue
                acquired = True
                try:
                    check()
                    value.state, value.stage = "running", "executing"
                    save()
                    _write_bound(root, "attempt.json", grant, {"directory": str(attempt.root)})
                    if operation == "batch":

                        def record_attempt(index: int, path: Path):
                            _write_bound(
                                root, f"attempt-{index}.json", grant, {"directory": str(path)}
                            )

                        content = execute_batch(
                            store,
                            authority,
                            request["request"],
                            environment=manager.environment,
                            check=check,
                            on_attempt=record_attempt,
                        )
                    else:
                        content = attempt.run(request["request"], check=check)
                    check()
                    value.stage = "publishing"
                    save()
                    record = RetainedResults(store).record(content)
                    identifier = "orr1_" + hashlib.sha256(json_bytes(record.wire())).hexdigest()
                    results = RetainedResults(store)
                    receipt = results.receipt(identifier, content)
                    _write_bound(root, "publication.json", grant, {"receipt": receipt.wire()})
                    check()
                    value.receipt = results.publish(
                        content.kind, content.payload, content.provenance
                    )
                    value.response_state = _response_state(content)
                    value.state, value.stage = "succeeded", "complete"
                    save()
                finally:
                    lock.__exit__(None, None, None)
                break
            if acquired:
                return 0
            time.sleep(0.05)
    except (ExecutionError, ExecutionRefused, ArtifactError) as error:
        code = error.code if error.code in ExecutionFaultCode.__args__ else "execution_failed"
        value.state = "cancelled" if code == "cancelled" else "failed"
        value.stage = "stopped"
        value.error = ExecutionFault(code=cast(ExecutionFaultCode, code))
        if code == "cancelled":
            stopping = True
        save()
        return 1
    except Exception:
        value.state, value.stage = "failed", "stopped"
        value.error = ExecutionFault(code="execution_failed")
        save()
        return 1
    finally:
        try:
            attempt.close()
        finally:
            store.close()
            for sig, handler in previous.items():
                signal.signal(sig, handler)


if __name__ == "__main__":
    if sys.stdin.buffer.read(1) != b"1":
        raise SystemExit(1)
    try:
        raise SystemExit(run(Path(sys.argv[1])))
    except Exception:
        raise SystemExit(1) from None
