"""Retain import progress across MCP disconnects without tying parsing to a tool deadline.

Each accepted job starts a detached local supervisor. It waits for the existing store
import lock, converts one document, publishes an artifact, and exits. The supervisor
owns a separate parser process group and closes it on cancellation or ordinary shutdown.
Jobs share the store's single-import discipline; waiting jobs do not load parser models.
Each input grant admits at most four nonterminal jobs and one job per source reference.
Admission is serialized across client processes before starting any supervisor. Refused
starts return retryable busy errors, so repeated tool calls cannot grow a process queue.
The existing synchronous import tool remains available for callers needing its old contract.

Private job records live under jobs/<input-grant>/<job-id>. Status exposes stages, elapsed
seconds and the final receipt, never source paths or parser diagnostics. A cancelled MCP
status request does not cancel the job. An explicit cancellation file is checked during
copying, queue wait and conversion. Publication wins a cancellation race once committed.
Jobs are not retried automatically, including after an interrupted supervisor. Process
identity includes its creation time so a reused PID cannot appear to own an older job.
Abrupt OS termination can leave staging; the next importer sweeps it under the store lock.
Listing discovers retained job IDs after reconnecting, including completed or unreadable records.
Status reads persist failed/stopped recovery when a nonterminal job's supervisor has exited.
Missing or unreadable process identities receive sixty seconds for startup before recovery.
A supervisor holds a per-job lock, so unreadable metadata cannot release a live job's slot.
For example, a launcher crash before identity publication no longer blocks every later import.
Listing performs the same recovery for each returned job, so neither operation is read-only.
Pages follow job-ID order, not creation order. Concurrent new jobs can sort before a cursor;
restart listing to discover them. Each reply contains at most fifty summaries without source paths.
Uninstalling a client has no cancellation hook here. Cancel jobs before uninstalling and
wait for terminal states. Reconnecting with the same roots permits discovery and cancellation.
Optional page progress preserves observed assembly counts across status reads and reconnects.
Legacy v0.1 status is read as v0.2 without a page observation; reads never rewrite terminal records.

Frozen clients dispatch --internal-artifact-job to main, after verifying their runtime.
The job owns a serialized profile, so removing a launcher's temporary profile is safe.
No network endpoint, credential, percentage estimate or provider-specific inference is added.
Trusted launchers can supply ImportExecution and a fixed service_factory to main or run.
Its snapshot belongs to that launcher and must contain no credentials or executable selectors.
External dispatch fails closed without its factory. Core never imports a client package.
The client-only wheel refuses local parser dispatch with a configuration error.
Report uploading before submitting bytes; waiting, receiving, and retaining follow that stage.
Queue contention can retry before submission. Busy after submission never repeats the import.
Cancellation and interrupted external jobs disclose that server processing may continue.
"""

from __future__ import annotations

import fcntl
import heapq
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

import psutil

from openreading.artifacts.intake import directory
from openreading.artifacts.limits import ArtifactError
from openreading.artifacts.models import EngineIdentity, json_bytes
from openreading.artifacts.retained import RetainedService as ArtifactService
from openreading.artifacts.store import safe_read
from openreading.types.import_job import ImportJob, ImportJobList, ImportJobSummary, PageProgress


def _status(path: Path) -> ImportJob:
    value = _read(path)
    if value.get("schema_version") == "0.1" and "page_progress" not in value:
        value = {**value, "schema_version": "0.2"}
    return ImportJob.model_validate(value)


TERMINAL = {"succeeded", "failed", "cancelled"}
MAX_ACTIVE_JOBS = 4
IDENTITY_GRACE_SECONDS = 60


@dataclass(frozen=True)
class ImportExecution:
    """A trusted launcher's child dispatch and validated, nonsecret execution snapshot.

    The launcher owns snapshot validation and credential lookup in both processes.
    For example, an HTTP client records a destination revision, never its bearer token.
    No tool argument can supply this object, and Core never imports code named in it.
    The child must pass its fixed service factory to main; ordinary dispatch refuses it.
    """

    command: tuple[str, ...]
    snapshot: dict

    def __post_init__(self):
        if not self.command or any(
            not isinstance(v, str) or not v or "\x00" in v for v in self.command
        ):
            raise ValueError("Trusted import dispatch requires a command vector")
        encoded = json_bytes(self.snapshot)
        if not isinstance(self.snapshot, dict) or len(encoded) > 16384:
            raise ValueError("Execution snapshot must be a bounded JSON object")
        object.__setattr__(self, "snapshot", json.loads(encoded))


class JobError(ValueError):
    """Fixed job lookup and launch errors never include private exception messages."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)

    def wire(self):
        messages = {
            "job_not_found": "This import job is unavailable under the current input grant.",
            "job_start_failed": "The local import job could not start. Check local disk access.",
            "job_state_invalid": "The retained import job status is unreadable.",
        }
        return {"error": {"code": self.code, "message": messages[self.code], "retryable": False}}


def _write(path: Path, value: dict):
    temporary = path.with_name(f".{uuid.uuid4().hex}.tmp")
    try:
        with directory(path.parent) as fd:
            output = os.open(temporary.name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=fd)
            with os.fdopen(output, "wb") as stream:
                stream.write(json_bytes(value))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary.name, path.name, src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
    finally:
        temporary.unlink(missing_ok=True)


def _read(path: Path) -> dict:
    return json.loads(safe_read(path, 65536))


@contextmanager
def _record_lock(path: Path, *, blocking: bool = True):
    # Never unlink these inodes: replacing one would let two processes own the same lock.
    with directory(path.parent) as parent:
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            fd = os.open(path.name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
        except FileExistsError:
            fd = os.open(path.name, flags, dir_fd=parent)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ArtifactError("artifact_corrupt")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
        else:
            try:
                yield True
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _admission_path(root: Path) -> Path:
    return root.parent / f".admission-{root.name}.lock"


class ImportJobs:
    def __init__(self, service: ArtifactService, *, execution: ImportExecution | None = None):
        self.service = service
        self.execution = execution
        self.root = service.config.artifact_root / "jobs" / service.store.grant
        with directory(self.root, create=True):
            pass

    def start(self, path: str) -> ImportJob:
        with self.service.store.source(path):
            pass
        with self._admission_lock():
            active = 0
            with directory(self.root) as fd, os.scandir(fd) as entries:
                for entry in entries:
                    if not re.fullmatch(r"j1_[0-9a-f]{32}", entry.name):
                        continue
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    try:
                        current = self._observe(entry.name)
                        if current.state in TERMINAL:
                            continue
                        request = _read(self.root / entry.name / "request.json")
                        active += 1
                        if request["path"] == path or active >= MAX_ACTIVE_JOBS:
                            raise ArtifactError("busy")
                    except (JobError, OSError, ValueError, KeyError, TypeError):
                        # An unreadable record cannot prove that its supervisor has exited.
                        raise ArtifactError("busy") from None
            return self._start(path)

    def _admission_lock(self):
        return _record_lock(_admission_path(self.root))

    def _start(self, path: str) -> ImportJob:
        config = self.service.config
        identifier = "j1_" + uuid.uuid4().hex
        root = self.root / identifier
        try:
            root.mkdir(mode=0o700)
            initial = ImportJob(
                schema_version="0.4" if self.execution else "0.3",
                job_id=identifier,
                state="queued",
                stage="queued",
                elapsed_seconds=0.0,
            )
            _write(root / "status.json", initial.wire())
            _write(
                root / "request.json",
                {
                    "path": path,
                    "input_root": str(config.input_root),
                    "artifact_root": str(config.artifact_root),
                    "grant": self.service.store.grant,
                    "limits": asdict(config.limits),
                    "docling": config.docling.wire() if config.docling else None,
                    "identity": self.service.identity.model_dump(mode="json"),
                    "started": time.time(),
                    **({"execution": self.execution.snapshot} if self.execution else {}),
                },
            )
            dispatch = (
                ["--internal-artifact-job"]
                if getattr(sys, "frozen", False)
                else ["-m", "openreading.artifacts.jobs"]
            )
            process = subprocess.Popen(
                [
                    *(self.execution.command if self.execution else [sys.executable, *dispatch]),
                    str(root),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                cwd=config.artifact_root / "worker",
            )
            try:
                created = psutil.Process(process.pid).create_time()
            except psutil.NoSuchProcess:
                created = None
            _write(root / "process.json", {"pid": process.pid, "created": created})
            # Reap children while this client lives; detachment allows normal host shutdown.
            threading.Thread(target=process.wait, daemon=True).start()
            return initial
        except Exception:
            if "process" in locals():
                process.terminate()
                process.wait()
            shutil.rmtree(root, ignore_errors=True)
            raise JobError("job_start_failed") from None

    def _root(self, identifier: str) -> Path:
        if not re.fullmatch(r"j1_[0-9a-f]{32}", identifier):
            raise JobError("job_not_found")
        root = self.root / identifier
        if not root.is_dir() or root.is_symlink():
            raise JobError("job_not_found")
        return root

    def get(self, job_id: str, wait_seconds: float = 0) -> ImportJob:
        if not 0 <= wait_seconds <= 20:
            raise ValueError("Status wait must be between zero and twenty seconds.")
        until = time.monotonic() + wait_seconds
        while True:
            # A reader must not observe status before the supervisor identity is published.
            with self._admission_lock():
                value = self._observe(job_id)
            if value.state in TERMINAL or time.monotonic() >= until:
                return value
            time.sleep(0.1)

    def _observe(self, job_id: str) -> ImportJob:
        root = self._root(job_id)
        try:
            value = _status(root / "status.json")
            if value.job_id != job_id:
                raise ValueError("Job identity mismatch")
            if value.state in TERMINAL:
                return value
            request = _read(root / "request.json")
            value.elapsed_seconds = max(0.0, time.time() - request["started"])
            value.cancel_requested = (root / "cancel").exists()
            try:
                owner = _read(root / "process.json")
                process = psutil.Process(owner["pid"])
                alive = (
                    process.create_time() == owner["created"]
                    and process.status() != psutil.STATUS_ZOMBIE
                )
            except psutil.NoSuchProcess:
                alive = False
            except (OSError, ValueError, KeyError, TypeError, ArtifactError):
                if value.elapsed_seconds < IDENTITY_GRACE_SECONDS:
                    return value
                alive = False
            if not alive:
                # Probe without waiting while holding admission, avoiding lock-order deadlock.
                with _record_lock(root / "supervisor.lock", blocking=False) as abandoned:
                    if not abandoned:
                        return value
                    return self._stopped(root, value, request)
            return value
        except (OSError, ValueError, KeyError, TypeError, ArtifactError, psutil.Error):
            raise JobError("job_state_invalid") from None

    def _stopped(self, root: Path, value: ImportJob, request: dict) -> ImportJob:
        # Read again after observing exit; the child may have published while polled.
        final = _status(root / "status.json")
        if final.state in TERMINAL:
            return final
        value.state, value.stage = "failed", "stopped"
        failure = ArtifactError("parse_failed").envelope().error
        if "execution" in request:
            failure.message = "The local supervisor stopped. Submitted server processing may continue; no retry was sent."
        value.error = failure
        _write(root / "status.json", value.wire())
        return value

    def cancel(self, job_id: str) -> ImportJob:
        current = self.get(job_id)
        if current.state in TERMINAL:
            return current
        root = self._root(job_id)
        _write(root / "cancel", {})
        return self.get(job_id)

    def list(self, limit: int = 20, cursor: str | None = None) -> ImportJobList:
        from openreading.artifacts.search import _binding, _cursor, _offset

        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("List limit must be between one and fifty jobs.")
        binding = _binding(["import_jobs", "0.1", str(self.root), self.service.store.grant, limit])
        start = _offset(cursor, binding, (1 << 128) + 1)
        try:
            with directory(self.root) as fd, os.scandir(fd) as entries:
                # Keep one page in memory even when the retained history is large.
                identifiers = heapq.nsmallest(
                    limit + 1,
                    (
                        entry.name
                        for entry in entries
                        if re.fullmatch(r"j1_[0-9a-f]{32}", entry.name)
                        and int(entry.name[3:], 16) >= start
                        and entry.is_dir(follow_symlinks=False)
                    ),
                )
        except (OSError, ArtifactError):
            raise JobError("job_state_invalid") from None
        rows = []
        for identifier in identifiers[:limit]:
            try:
                value = self.get(identifier)
                rows.append(
                    ImportJobSummary(
                        job_id=identifier, state=value.state, elapsed_seconds=value.elapsed_seconds
                    )
                )
            except JobError as error:
                if error.code == "job_not_found":
                    continue
                rows.append(
                    ImportJobSummary(job_id=identifier, state="unavailable", elapsed_seconds=None)
                )
        continuation = (
            _cursor(binding, int(identifiers[limit - 1][3:], 16) + 1)
            if len(identifiers) > limit
            else None
        )
        return ImportJobList(jobs=rows, next_cursor=continuation)


def run(root: Path, *, service_factory: Callable[[dict], ArtifactService] | None = None) -> int:
    with _record_lock(root / "supervisor.lock", blocking=False) as acquired:
        if not acquired:
            return 2
        return _run(root, service_factory=service_factory)


def _run(root: Path, *, service_factory: Callable[[dict], ArtifactService] | None = None) -> int:
    request = _read(root / "request.json")
    if ("execution" in request) != (service_factory is not None):
        raise ValueError("Job execution must match its launcher's fixed service factory")
    if "execution" in request:
        assert service_factory is not None
        factory = service_factory
    else:
        try:
            from openreading.artifacts.local_jobs import local_service as factory
        except ModuleNotFoundError as error:
            if error.name != "openreading.artifacts.local_jobs":
                raise
            raise ValueError("Local jobs require the parser-enabled Core package") from None

    with _record_lock(_admission_path(root.parent)):
        value = _status(root / "status.json")
        if value.state in TERMINAL:
            return 0
        # A child can outlive a launcher killed before the parent publishes its identity.
        _write(
            root / "process.json", {"pid": os.getpid(), "created": psutil.Process().create_time()}
        )
    service = factory(request)

    class Cancellation(threading.Event):
        def is_set(self):
            return super().is_set() or (root / "cancel").exists()

    cancelled = Cancellation()
    previous = {}
    if threading.current_thread() is threading.main_thread():

        def stop(signum, frame):
            cancelled.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, stop)

    def stage(name):
        value.state, value.stage = "running", name
        value.elapsed_seconds = max(0.0, time.time() - request["started"])
        _write(root / "status.json", value.wire())

    def page_progress(pages: PageProgress):
        value.page_progress = pages
        stage("conversion")

    try:
        if service.store.grant != request[
            "grant"
        ] or service.identity != EngineIdentity.model_validate(request["identity"]):
            raise ArtifactError("engine_identity_unavailable")
        while True:
            if cancelled.is_set():
                raise ArtifactError("cancelled")
            try:
                receipt = service.import_document(
                    request["path"],
                    cancelled=cancelled,
                    progress=stage,
                    page_progress=page_progress,
                )
                break
            except ArtifactError as error:
                if error.code != "busy":
                    raise
                if "execution" in request and value.stage in {
                    "uploading",
                    "waiting",
                    "receiving",
                }:
                    # Retention contention cannot resubmit a possibly billable server parse.
                    raise ArtifactError("parse_failed") from None
                time.sleep(0.1)
        value.state, value.stage, value.receipt = "succeeded", "complete", receipt
        if receipt.schema_version == "0.5":
            value.schema_version = "0.4"
    except Exception as error:
        failure = error if isinstance(error, ArtifactError) else ArtifactError("parse_failed")
        value.state = "cancelled" if failure.code == "cancelled" else "failed"
        detail = failure.envelope().error
        if (
            "execution" in request
            and failure.code in {"cancelled", "parse_failed"}
            and not getattr(failure, "preserve_message", False)
        ):
            detail.message = "Local processing stopped. Submitted server processing may continue; no retry was sent."
        value.stage, value.error = "stopped", detail
    finally:
        service.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    value.elapsed_seconds = max(0.0, time.time() - request["started"])
    value.cancel_requested = cancelled.is_set()
    _write(root / "status.json", value.wire())
    return 0


def main(
    argv: list[str] | None = None,
    *,
    service_factory: Callable[[dict], ArtifactService] | None = None,
) -> int:
    """Run a private import supervisor; exit two on invalid internal configuration."""
    arguments = sys.argv[1:] if argv is None else argv
    try:
        if len(arguments) != 1:
            return 2
        return run(Path(arguments[0]), service_factory=service_factory)
    except Exception:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
