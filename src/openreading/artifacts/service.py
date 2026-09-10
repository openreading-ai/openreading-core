"""Import once, then search and read verified page evidence across process restarts.

The service copies an opened source into private staging before launching a disposable
native parser process. Cancellation and the deadline terminate the entire child process
 group before staging is removed. A successful receipt follows file and directory fsync.
Source checkouts record their Git revision and a hash of current Python/schema files. Frozen distributions provide engine identity
through the packager's verified metadata, never a model-supplied tool argument.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

from openreading.artifacts.intake import copy_source, source
from openreading.artifacts.limits import ArtifactError, ProfileConfig
from openreading.artifacts.models import (
    ArtifactManifest,
    EngineIdentity,
    FileRecord,
    ImportReceipt,
    WarningCode,
    artifact_id,
    json_bytes,
)
from openreading.artifacts.store import Store, safe_read
from openreading.artifacts.worker import SETTINGS


def _source_tree_hash(package: Path) -> str:
    # An editable checkout may differ from HEAD; never reuse its predecessor's artifacts.
    digest = hashlib.sha256()
    for path in sorted(package.rglob("*")):
        if path.suffix in {".py", ".json"}:
            digest.update(str(path.relative_to(package)).encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()


def engine_identity() -> EngineIdentity:
    import pymupdf

    package = Path(__file__).resolve().parents[1]
    built = package / "engine-identity.json"
    if built.exists():
        return EngineIdentity.model_validate_json(built.read_bytes())
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=package, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        raise ArtifactError("configuration_required") from None
    return EngineIdentity(
        core_commit=commit,
        core_version=importlib.metadata.version("openreading"),
        backend_version=pymupdf.VersionBind,
        extraction_settings={**SETTINGS, "source_tree_sha256": _source_tree_hash(package)},
    )


def _display_name(relative: str) -> str:
    name = "".join(
        c for c in relative.split("/")[-1] if not unicodedata.category(c).startswith("C")
    )
    return name.encode("utf-8")[:255].decode("utf-8", errors="ignore") or "document.pdf"


class ArtifactService:
    def __init__(self, config: ProfileConfig, *, identity: EngineIdentity | None = None):
        self.config = config
        self.store = Store(config)
        self.identity = identity or engine_identity()

    def load_artifact(self, identifier: str) -> ArtifactManifest:
        return self.store.load(identifier)[0]

    def import_document(
        self, path: str, *, cancelled: threading.Event | None = None
    ) -> ImportReceipt:
        started = time.monotonic()
        with source(self.config.input_root, path) as fd, self.store.import_lock():
            staging = Path(tempfile.mkdtemp(dir=self.config.artifact_root / "staging"))
            try:
                available = self.config.limits.store_bytes - self.store.size()
                digest, changed = copy_source(
                    fd, staging / "source.pdf", self.config.limits.source_bytes, available
                )
                identifier = artifact_id(digest, self.identity)
                if (self.store.documents / identifier).exists():
                    manifest = self.load_artifact(identifier)
                    return self._receipt(manifest, reused=True)
                self._check_time(started, cancelled)
                job = {
                    "directory": str(staging),
                    "pages": self.config.limits.pages,
                    "extraction_bytes": self.config.limits.extraction_bytes,
                    "available": self.config.limits.store_bytes - self.store.size() - 65536,
                }
                if job["available"] < 0:
                    raise ArtifactError("storage_limit")
                (staging / "job.json").write_bytes(json_bytes(job))
                os.chmod(staging / "job.json", 0o600)
                self._worker(staging, started, cancelled)
                response = json.loads(
                    safe_read(staging / "response.json", self.config.limits.extraction_bytes)
                )
                passages = safe_read(
                    staging / "passages.jsonl", self.config.limits.extraction_bytes
                ).splitlines()
                files = {}
                for name in ("source.pdf", "response.json", "passages.jsonl"):
                    contents = safe_read(
                        staging / name,
                        max(self.config.limits.source_bytes, self.config.limits.extraction_bytes),
                    )
                    files[name] = FileRecord(
                        length=len(contents), sha256=hashlib.sha256(contents).hexdigest()
                    )
                warnings: list[WarningCode] = []
                if changed:
                    warnings.append("source_changed")
                if response.get("warnings"):
                    warnings.append("parser_warnings_present")
                manifest = ArtifactManifest(
                    artifact_id=identifier,
                    document_sha256=digest,
                    display_name=_display_name(path),
                    source_relative_path=path,
                    input_grant_sha256=self.store.grant,
                    page_count=response["document"]["page_count"],
                    passage_count=len(passages),
                    engine=self.identity,
                    created_at=datetime.now(UTC).isoformat(),
                    files=files,
                    warnings=warnings,
                )
                encoded = json_bytes(manifest.wire())
                if self.store.size() + len(encoded) > self.config.limits.store_bytes:
                    raise ArtifactError("storage_limit")
                with (staging / "manifest.json").open("xb") as stream:
                    os.chmod(staging / "manifest.json", 0o600)
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                (staging / "job.json").unlink()
                (staging / "result.json").unlink()
                self._check_time(started, cancelled)
                receipt = self._receipt(manifest, reused=False)
                self._fsync(staging)
                os.rename(staging, self.store.documents / identifier)
                self._fsync(self.store.documents)
                return receipt
            except OSError:
                raise ArtifactError("storage_limit") from None
            finally:
                if staging.exists():
                    shutil.rmtree(staging)

    @staticmethod
    def _fsync(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _check_time(self, started: float, cancelled: threading.Event | None) -> None:
        if cancelled is not None and cancelled.is_set():
            raise ArtifactError("cancelled")
        if time.monotonic() - started >= self.config.limits.deadline_seconds:
            raise ArtifactError("timeout")

    def _worker(self, staging: Path, started: float, cancelled: threading.Event | None) -> None:
        command = [sys.executable]
        if getattr(sys, "frozen", False):
            command += ["--internal-artifact-worker"]
        else:
            command += ["-m", "openreading.artifacts.worker"]
        command += ["--job-file", str(staging / "job.json")]
        with subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        ) as process:
            try:
                while process.poll() is None:
                    self._check_time(started, cancelled)
                    time.sleep(0.02)
                self._check_time(started, cancelled)
                if process.returncode:
                    raise ArtifactError("parse_failed")
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        try:
            result = json.loads(safe_read(staging / "result.json", 1024))
            if "error" in result:
                from openreading.artifacts.models import ToolError

                error = ToolError(code=result["error"], message="", retryable=False)
                raise ArtifactError(error.code)
            if result != {"ok": True}:
                raise ValueError("Invalid worker result")
        except (OSError, ValueError):
            raise ArtifactError("parse_failed") from None

    def _receipt(self, manifest: ArtifactManifest, *, reused: bool) -> ImportReceipt:
        result = ImportReceipt(
            artifact_id=manifest.artifact_id,
            display_name=manifest.display_name,
            document_sha256=manifest.document_sha256,
            page_count=manifest.page_count,
            passage_count=manifest.passage_count,
            reused=reused,
            warnings=manifest.warnings,
        )
        if len(json_bytes(result.wire())) > self.config.limits.import_bytes:
            raise ArtifactError("response_too_large")
        return result

    def search(self, artifact_id: str, query: str, limit: int = 5, cursor: str | None = None):
        from openreading.artifacts.search import search

        manifest, passages = self.store.load(artifact_id)
        return search(manifest, passages, query, limit, cursor, self.config.limits.search_bytes)

    def read(self, artifact_id: str, evidence_ids: list[str], cursor: str | None = None):
        from openreading.artifacts.search import read

        manifest, passages = self.store.load(artifact_id)
        return read(manifest, passages, evidence_ids, cursor, self.config.limits.read_bytes)
